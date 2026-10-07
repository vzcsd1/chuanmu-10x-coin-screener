#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""原始归档内容审计（任务：tasks/glm53f_archive_content_audit.md）。

只读：不改策略、不碰 ds 采集文件、不联网下载。单进程，预算：≤96 样本、
解压总预算 256 MiB、单文件 32 MiB（超限中止并标 partial_budget，不算通过）。

输入：ds 状态快照（data/archive_raw/_state.json，记录 file_exists）∩ index_v2 范围；
分层确定性选样（市场/数据集/月日归档/年代/特殊符号），种子固定可重跑。

检查（csv 模块流式整文件，不静默修正，保留原值与行号）：
  表头/无表头、列数变化、时间列秒/毫秒/微秒/字符串变体与文件内混用、
  文件名-内容日期一致性、时间乱序/重复、空值/非有限数、
  K线 high/low 与 open/close 关系及成交量为负、费率负值（合法，不报异常）。

子命令：sample / audit / fields / summary / all
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import re
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "archive_raw"
STATE = RAW / "_state.json"
REV = ROOT / "reports" / "data_inventory_glm_full" / "revisions" / "r2"
OUT = ROOT / "reports" / "archive_content_audit_glm"

MAX_SAMPLES = 96
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MARKET_ALIAS = {"futures": "futures-um", "spot": "spot", "futures-cm": "futures-cm"}
SPECIAL_SYMBOLS = {"ALPACAUSDT", "FTTUSDT", "SRMUSDT", "CVCUSDT", "KLAYUSDT"}
SEED_NOTE = "deterministic: 层内按键排序后等距取样（无随机数）"
DELISTED_HINTS = ("ALPACAUSDT", "FTTUSDT", "SRMUSDT")

KNOWN_HEADERS = {
    "open_time", "open", "high", "low", "close", "volume", "close_time",
    "quote_volume", "count", "trades", "taker_buy_volume", "taker_buy_base",
    "taker_buy_quote_volume", "taker_buy_quote", "ignore", "symbol",
    "sum_open_interest", "sum_open_interest_value", "create_time",
    "count_toptrader_long_short_ratio", "sum_toptrader_long_short_ratio",
    "count_long_short_ratio", "sum_taker_long_short_vol_ratio",
    "calc_time", "funding_interval_hours", "last_funding_rate",
}


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_STRDATE_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
                    "%Y-%m-%d %H:%M", "%Y-%m-%d")


def parse_time_cell(v: str) -> tuple[str, float | None]:
    """时间单元格分类，返回 (类别, 毫秒)。

    类别：ns/us/ms/s（数字量级）、strdate（**合法字符串日期**，按已知格式解析）、
    str（无法解析的字符串，异常）、toosmall、empty、nonfinite。
    合法字符串日期保留原值由调用方负责；此处只做解析。
    """
    t = v.strip()
    if not t:
        return "empty", None
    try:
        x = float(t)
    except ValueError:
        for fmt in _STRDATE_FORMATS:
            try:
                dt = datetime.strptime(t, fmt)
                return "strdate", dt.replace(tzinfo=timezone.utc).timestamp() * 1000
            except ValueError:
                continue
        return "str", None
    if not math.isfinite(x):
        return "nonfinite", None
    if x > 1e17:
        return "ns", x / 1e6
    if x > 1e14:
        return "us", x / 1e3
    if x > 1e11:
        return "ms", x
    if x > 1e9:
        return "s", x * 1e3
    return "toosmall", None


def time_scale(v: str) -> str:
    """兼容旧接口：只返回类别。"""
    return parse_time_cell(v)[0]


# ------------------------------------------------------------------ sample --

def cmd_sample(_args) -> int:
    """分层确定性选样：层 = (market, dataset, kind, era)；特殊符号单列。"""
    OUT.mkdir(parents=True, exist_ok=True)
    raw = json.loads(STATE.read_text(encoding="utf-8"))
    snapshot_utc = now_utc()
    state_updated = raw.get("updated_at_utc", "")
    candidates: list[dict] = []
    for key, rec in raw.get("records", {}).items():
        parts = str(key).split("|")
        parts += [""] * (6 - len(parts))
        alias, dataset, symbol, interval, ps, pe = parts[:6]
        market = MARKET_ALIAS.get(alias, alias)
        path = Path(str(rec.get("path", "")))
        if not path.exists() or not path.name.endswith(".zip"):
            continue
        # kind 以**文件名**为准：状态键的 period 粒度不可靠（v1 遗留把月度记成单日）
        m = re.search(r"-(\d{4}-\d{2})(-\d{2})?\.zip$", path.name)
        if m:
            kind = "monthly" if not m.group(2) else "daily"
            file_period = m.group(1) + (m.group(2) or "")
        else:
            kind = "monthly" if len(ps) == 7 else "daily"
            file_period = ps
        era = "old" if file_period < "2022" else ("mid" if file_period < "2025" else "new")
        special = ("non_ascii" if not symbol.isascii()
                   else "delisted" if any(h in symbol for h in DELISTED_HINTS)
                   else "mixflag" if (market, dataset, symbol) in
                   {("spot", "klines", "KLAYUSDT")} else "normal")
        candidates.append({"key": key, "market": market, "dataset": dataset,
                           "kind": kind, "era": era, "symbol": symbol,
                           "period": ps, "path": str(path), "special": special,
                           "status": rec.get("status", "")})
    candidates.sort(key=lambda c: c["key"])
    layers: dict[tuple, list[dict]] = {}
    for c in candidates:
        layers.setdefault((c["market"], c["dataset"], c["kind"], c["era"]), []).append(c)
    picked: list[dict] = []
    PER_LAYER = 4
    for layer in sorted(layers):
        pool = layers[layer]
        if not pool:
            continue
        take = min(PER_LAYER, len(pool))
        step = max(1, len(pool) // take)
        picked += pool[::step][:take]
    for flag in ("non_ascii", "delisted", "mixflag"):
        pool = [c for c in candidates if c["special"] == flag]
        picked += pool[:4]
    # 去重 + 截断
    seen: set[str] = set()
    uniq = []
    for c in picked:
        if c["key"] not in seen:
            seen.add(c["key"])
            uniq.append(c)
    uniq = uniq[:MAX_SAMPLES]
    cols = ["key", "market", "dataset", "kind", "era", "symbol", "period",
            "special", "path", "status", "selection_rule", "snapshot_utc"]
    with (OUT / "sample_manifest.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for c in uniq:
            w.writerow({**c, "selection_rule": SEED_NOTE, "snapshot_utc": snapshot_utc})
    meta = {"snapshot_utc": snapshot_utc, "state_updated_at_utc": state_updated,
            "candidates": len(candidates), "picked": len(uniq),
            "layers": {str(k): len(v) for k, v in sorted(layers.items())},
            "note": "cm/现货部分层无本地样本时如实缺层，见 coverage.csv"}
    (OUT / "sample_manifest_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[sample] 候选 {len(candidates)} -> 选样 {len(uniq)}（层 {len(layers)}）")
    return 0


# ------------------------------------------------------------------- audit --

class Budget:
    def __init__(self):
        self.total = 0

    def take(self, n: int, file_bytes: int, file_cap: int) -> bool:
        """总预算与单文件预算都按累计值判断；单块超过上限同样中止。"""
        if self.total + n > MAX_TOTAL_BYTES:
            return False
        if file_bytes + n > file_cap:
            return False
        self.total += n
        return True


def cmd_audit(_args) -> int:
    manifest = pd.read_csv(OUT / "sample_manifest.csv", dtype=str,
                           keep_default_na=False)
    budget = Budget()
    checks: list[dict] = []
    anomalies: list[dict] = []

    def add_anom(sample_key, path, member, line_no, kind, detail, value=""):
        anomalies.append({"sample_key": sample_key, "file": path, "member": member,
                          "line_no": line_no, "kind": kind, "detail": detail,
                          "value": str(value)[:120]})

    for _, s in manifest.iterrows():
        path = Path(s["path"])
        fc = {"key": s["key"], "market": s["market"], "dataset": s["dataset"],
              "kind": s["kind"], "symbol": s["symbol"], "period": s["period"],
              "file": s["path"], "rows_read": 0, "bytes_read": 0,
              "header_present": "", "time_unit_dominant": "",
              "time_unit_counts": "", "unit_mix_rows": 0, "min_dt": "",
              "max_dt": "", "date_match": "", "unsorted_count": 0,
              "dup_time_count": 0, "col_changes": 0, "null_or_nonfinite": 0,
              "ohlc_violations": 0, "neg_funding_count": 0, "status": "ok",
              "notes": ""}
        try:
            zf = zipfile.ZipFile(path)
        except Exception as exc:
            fc["status"] = "zip_open_failed"
            fc["notes"] = f"{type(exc).__name__}: {exc}"
            checks.append(fc)
            continue
        members = [n for n in zf.namelist() if n.endswith(".csv")]
        if not members:
            fc["status"] = "no_csv_member"
            checks.append(fc)
            continue
        member = members[0]
        try:
            with zf.open(member) as fh:
                text = io.TextIOWrapper(fh, encoding="utf-8", errors="replace")
                reader = csv.reader(text)
                header = None
                header_done = False
                first_data = True
                scales = Counter()
                mixed_rows = 0
                prev_ms = None
                seen_ms: set[float] = set()
                seen_times: set[str] = set()
                dup = unsorted = 0
                base_cols = None
                min_ms = max_ms = None
                file_start_line = True
                line_no = 0
                partial = False
                for row in reader:
                    line_no += 1
                    try:
                        chunk_est = len(",".join(row)) + 1
                    except Exception:
                        chunk_est = 32
                    if not budget.take(chunk_est, fc["bytes_read"], MAX_FILE_BYTES):
                        partial = True
                        fc["status"] = "partial_budget"
                        fc["notes"] = f"解压预算中止于第 {line_no} 行（不代表整文件通过）"
                        break
                    fc["bytes_read"] += chunk_est
                    if not header_done:
                        flat = {c.strip().lower() for c in row if c.strip()}
                        if flat & KNOWN_HEADERS and first_data is True and header is None \
                                and not row[0].strip().lstrip("-").isdigit():
                            header = [c.strip().lower() for c in row]
                            fc["header_present"] = "yes"
                            header_done = True
                            continue
                        if header is None:
                            fc["header_present"] = "no"
                            header_done = True
                        else:
                            header_done = True
                    if not any(cell.strip() for cell in row):
                        continue
                    fc["rows_read"] += 1
                    if base_cols is None:
                        base_cols = len(row)
                    elif len(row) != base_cols:
                        fc["col_changes"] += 1
                        if fc["col_changes"] <= 3:
                            add_anom(s["key"], s["path"], member, line_no,
                                     "column_count_change",
                                     f"{base_cols}->{len(row)}", row[:3])
                    is_kline = s["dataset"] == "klines"
                    is_funding = s["dataset"] == "fundingRate"
                    time_cell = ""
                    if header and "open_time" in header:
                        time_cell = row[header.index("open_time")]
                    elif header and ("create_time" in header or "calc_time" in header):
                        tt = "create_time" if "create_time" in header else "calc_time"
                        time_cell = row[header.index(tt)]
                    elif header and "time" in header:
                        time_cell = row[header.index("time")]
                    else:
                        # funding 无表头：calc_time 在第一列（05 解析顺序）
                        time_cell = row[0] if row else ""
                    sc, ms = parse_time_cell(time_cell)
                    scales[sc] += 1
                    if sc in ("ns", "us", "ms", "s", "strdate"):
                        if min_ms is None or ms < min_ms:
                            min_ms = ms
                        if max_ms is None or ms > max_ms:
                            max_ms = ms
                        dom = scales.most_common(1)[0][0]
                        if sc != dom and scales[sc] / max(1, sum(scales.values())) > 0.01:
                            mixed_rows += 1
                            if mixed_rows <= 3:
                                add_anom(s["key"], s["path"], member, line_no,
                                         "time_unit_mix", f"dominant={dom}", time_cell)
                        if prev_ms is not None and ms is not None and ms < prev_ms:
                            unsorted += 1
                        if ms is not None:
                            if ms in seen_ms:
                                dup += 1
                            seen_ms.add(ms)
                        prev_ms = ms
                        dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc) \
                            if ms else None
                        if dt:
                            d = dt.strftime("%Y-%m-%d")
                            if min_ms is not None and (min_ms == ms or max_ms == ms):
                                pass
                            seen_times.add(d)
                    else:
                        if sc in ("str", "nonfinite", "toosmall"):
                            # 只有无法解析的时间才算异常；合法字符串日期不报
                            fc["null_or_nonfinite"] += 1
                            if fc["null_or_nonfinite"] <= 3:
                                add_anom(s["key"], s["path"], member, line_no,
                                         "time_unparseable", sc, time_cell)
                    for i, cell in enumerate(row):
                        c = cell.strip()
                        if c == "":
                            fc["null_or_nonfinite"] += 1
                        elif i != (header.index("symbol") if header and "symbol" in header else -1):
                            try:
                                x = float(c)
                                if not math.isfinite(x):
                                    fc["null_or_nonfinite"] += 1
                            except ValueError:
                                pass
                    if is_kline and len(row) >= 11:
                        try:
                            o, h, l, c = (float(row[1]), float(row[2]),
                                          float(row[3]), float(row[4]))
                            vol = float(row[5]) if row[5].strip() else 0.0
                            qv = float(row[7]) if len(row) > 7 and row[7].strip() else 0.0
                            if h < max(o, c) - 1e-12 or l > min(o, c) + 1e-12:
                                fc["ohlc_violations"] += 1
                                if fc["ohlc_violations"] <= 3:
                                    add_anom(s["key"], s["path"], member, line_no,
                                             "ohlc_relation", f"h={h} l={l} o={o} c={c}")
                            if vol < 0 or qv < 0:
                                fc["ohlc_violations"] += 1
                        except ValueError:
                            pass
                    if is_funding and len(row) >= 3:
                        try:
                            rate = float(row[2])
                            if rate < 0:
                                fc["neg_funding_count"] += 1
                        except ValueError:
                            pass
                    first_data = False
        except Exception as exc:
            fc["status"] = "parse_failed"
            fc["notes"] = f"{type(exc).__name__}: {exc}"
            checks.append(fc)
            continue
        # 单位主导与混合统计
        if scales:
            fc["time_unit_dominant"] = scales.most_common(1)[0][0]
            fc["time_unit_counts"] = ";".join(f"{k}:{v}" for k, v in sorted(scales.items()))
        fc["unit_mix_rows"] = mixed_rows
        if dup:
            fc["dup_time_count"] = dup
            add_anom(s["key"], s["path"], member, 0, "duplicate_time", f"{dup} 对")
        if unsorted:
            fc["unsorted_count"] = unsorted
            add_anom(s["key"], s["path"], member, 0, "unsorted_time", f"{unsorted} 次")
        # 文件名-内容日期一致性
        period = s["period"]
        try:
            if s["kind"] == "monthly":
                exp_start = period + "-01"
                exp_days = {exp_start[:7]}
                got = {d[:7] for d in seen_times}
            else:
                exp_days = {period}
                got = seen_times
            fc["min_dt"] = datetime.fromtimestamp(min_ms / 1000, tz=timezone.utc)\
                .strftime("%Y-%m-%d") if min_ms else ""
            fc["max_dt"] = datetime.fromtimestamp(max_ms / 1000, tz=timezone.utc)\
                .strftime("%Y-%m-%d") if max_ms else ""
            ok = got and got.issubset(exp_days) and exp_days & got
            fc["date_match"] = "yes" if ok else "no"
            if not ok:
                add_anom(s["key"], s["path"], member, 0, "date_mismatch",
                         f"期望 {sorted(exp_days)} 实得 {sorted(got)[:4]}")
        except Exception as exc:
            fc["date_match"] = "unchecked"
            fc["notes"] = (fc["notes"] + ";" if fc["notes"] else "") + \
                f"日期核对失败 {type(exc).__name__}"
        if fc["status"] == "ok" and partial:
            fc["status"] = "partial_budget"
        checks.append(fc)
        if budget.total >= MAX_TOTAL_BYTES:
            print("[audit] 总预算已用尽，剩余样本本轮未检查", flush=True)
            break
    cols = list(checks[0].keys()) if checks else ["key"]
    with (OUT / "file_checks.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(checks)
    acols = ["sample_key", "file", "member", "line_no", "kind", "detail", "value"]
    with (OUT / "anomalies.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=acols)
        w.writeheader()
        w.writerows(anomalies)
    print(f"[audit] 检查 {len(checks)} 文件 / 解压 {budget.total/1e6:.1f}MB / "
          f"异常 {len(anomalies)} 条")
    return 0


# ------------------------------------------------------------------ fields --

FIELD_USAGE = [
    # dataset, 字段/位置, 样本证据, 单位证据, 现有使用位置, 角色, 机制备注, 状态
    # 状态约定：确认（样本/代码可复核）、假设（机制未经数据检验）、待核实（单位/语义未取证）
    ("klines", "col5 volume", "现货/合约 K 线第 6 列", "推断为基础币数量；**本轮未取官方文档，待核实**",
     "binance_box_strategy.py:318 量比; research/03_build_features.py:198-204",
     "量比/量增评分输入（当前在用）",
     "[假设] 与报价额分离可识别控盘小币刷量结构", "假设；单位待核实"),
    ("klines", "col7 quote_volume", "第 8 列", "推断为报价币计；**待核实**",
     "research/03_build_features.py:224", "流动性分档（当前在用）",
     "[假设] volume 与 quote_volume 背离可识别刷量", "假设；单位待核实"),
    ("klines", "col9-10 taker_buy_base/quote", "第 10-11 列",
     "推断为主动买入（基础币/报价币）；**待核实**",
     "现有研究未读取（全库检索无 taker 引用）", "未用",
     "[假设] 洗盘回拉前主动买压占比先反转；启动前 72h 主动买入抬升。"
     "最小验证：本地 1h 对 1,402 事件 pre_day 前 72h vs 同币对照期；"
     "注意 2025 测试集已被规则选择使用，验证须用冻结后的新时期数据", "假设；单位待核实"),
    ("klines", "col8 trades", "第 9 列", "笔数；**待核实**（是否含拆单）",
     "现有研究未读取", "未用",
     "[假设] 笔均单量变化 = 大户参与度；单字段不下结论", "假设；单位待核实"),
    ("metrics", "sum_open_interest", "表头命名", "推断为持仓数量（U 本位为基础币，"
     "币本位为该币计）；**待核实**",
     "research/05_deriv_features.py:44,88 存为 oi_amt_last；评分未用",
     "存储未用于结论",
     "[假设] 数量/金额背离可分离『真增仓』与『价格推高』；检验必须引入价格对照，"
     "比值单独不能定论", "假设；单位待核实"),
    ("metrics", "sum_open_interest_value", "表头命名", "持仓价值；U 本位为 USDT 计"
     "（推断，待核实）",
     "research/05_deriv_features.py:86-88; binance_box_strategy.py:445-449",
     "OI 增速评分唯一来源（当前在用）",
     "[事实] 评分链只用金额口径；价格波动计入 OI 增速为 STRATEGY_REVIEW 第 2 节已标注的既有判断",
     "确认（代码）；单位待核实"),
    ("metrics", "count_toptrader_long_short_ratio", "表头命名", "大户账户数比（推断）；**待核实**",
     "research/05_deriv_features.py:45 存为 ls_count；未用于任何结论", "存储未用",
     "[假设] 账户数与持仓比背离可区分『户数挤兑』与『仓位集中』", "假设；语义待核实"),
    ("metrics", "sum_toptrader_long_short_ratio", "表头命名", "大户持仓比——2026-10-04 已与在线"
     " topLongShortPositionRatio 同源实测（BTC 1.8094 vs 全市场 1.2252，见 MEMORY）",
     "research/05_deriv_features.py:46,92-93; binance_box_strategy.py:454-498",
     "大户持仓多空比评分来源（当前在用）", "口径勿与账户数比混用（既有教训 +7.7pp→+0.6pp）",
     "确认（同源实测）"),
    ("metrics", "count_long_short_ratio", "表头命名", "全市场账户数比（推断）；**待核实**",
     "research/05_deriv_features.py:47 存为 ls_count_last/mean；未用", "存储未用",
     "[事实] 散户账户口径曾致 +7.7pp 误判（KNOWLEDGE 3.2）；仅作对照", "确认（教训在案）"),
    ("metrics", "sum_taker_long_short_vol_ratio", "表头命名", "主动买卖量比；**待核实**",
     "research/05_deriv_features.py:47-48 存为 taker；未用于任何结论", "存储未用",
     "[假设] 洗盘回拉前 taker 买卖比先反转；与 K 线 taker 字段交叉验证；"
     "验证须用冻结后新时期数据", "假设；单位待核实"),
    ("metrics", "create_time", "表头命名；**实测存在毫秒与字符串两种格式**"
     "（2021-07~2022-12 部分文件为字符串，本轮 8/42 样本）",
     "毫秒时间戳 或 'YYYY-MM-DD HH:MM:SS' 字符串（样本证据）",
     "research/05_deriv_features.py:86 取当日最后一行，**不读该列本身**", "日末快照",
     "[事实] 『日末』取数使日内洗盘路径不可见；5 分钟粒度保留在原始文件；"
     "未来日内研究须双格式兼容", "确认（样本）"),
    ("fundingRate", "last_funding_rate", "表头命名", "小数（-0.000125 = -0.0125%；样本证据）",
     "research/05_deriv_features.py:120-133 解析；评分未用（实测噪音）", "未用",
     "[事实] 负费率合法（样本 653 行）；费率由负转正被 841 样本否定，不作门槛", "确认（样本）"),
    ("fundingRate", "funding_interval_hours", "表头命名（部分文件出现）",
     "小时数（推断 4/8）；**语义与出现范围待核实**",
     "现有解析保留列但未用", "未用",
     "[假设] 结算频率变化影响费率累积速度；跨期比较需归一", "假设；语义待核实"),
    ("fundingRate", "calc_time/time", "表头或无表头首列", "毫秒（样本证据）；"
     "是否存在字符串变体**待核实**",
     "research/05_deriv_features.py:129-131 首行推断单位", "结算时间",
     "[事实] 首行推断与 K 线同款风险（混合文件会错读）；funding 内部混用未见样本",
     "确认（代码风险）；变体待核实"),
]


def cmd_fields(_args) -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    cols = ["dataset", "field", "sample_evidence", "unit_evidence", "used_by",
            "current_role", "mechanism_note", "status"]
    with (OUT / "field_usage.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerows(FIELD_USAGE)
    print(f"[fields] field_usage.csv {len(FIELD_USAGE)} 行")
    return 0


# ----------------------------------------------------------------- summary --

def cmd_summary(_args) -> int:
    fc = pd.read_csv(OUT / "file_checks.csv", dtype=str, keep_default_na=False) \
        if (OUT / "file_checks.csv").exists() else pd.DataFrame()
    an = pd.read_csv(OUT / "anomalies.csv", dtype=str, keep_default_na=False) \
        if (OUT / "anomalies.csv").exists() else pd.DataFrame()
    sm = pd.read_csv(OUT / "sample_manifest.csv", dtype=str, keep_default_na=False)
    # coverage.csv：每层的选样/实查/状态
    cov_rows = []
    if len(sm):
        for (market, dataset, kind, era), g in sm.groupby(
                ["market", "dataset", "kind", "era"]):
            checked = fc[fc["key"].isin(g["key"])] if len(fc) else g.iloc[0:0]
            statuses = checked["status"].value_counts().to_dict() if len(checked) else {}
            cov_rows.append({"market": market, "dataset": dataset, "kind": kind,
                             "era": era, "candidates_in_pool": "",
                             "sampled": len(g), "checked": len(checked),
                             "statuses": json.dumps(statuses, ensure_ascii=False)})
    cov_cols = ["market", "dataset", "kind", "era", "candidates_in_pool",
                "sampled", "checked", "statuses"]
    with (OUT / "coverage.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cov_cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(cov_rows)
    lines = ["# 原始资料内容审计（glm5.3f）", "", f"生成：{now_utc()}", "",
             f"样本 {len(sm)}（分层见 sample_manifest_meta.json）；"
             f"已检查 {len(fc)}；异常 {len(an)} 条；覆盖层见 coverage.csv", ""]
    if len(fc):
        for col in ("status", "time_unit_dominant", "date_match", "header_present"):
            vc = fc[col].value_counts().to_dict() if col in fc else {}
            lines.append(f"- {col}: {vc}")
    lines.append("")
    if len(an):
        by = an["kind"].value_counts().to_dict()
        lines.append(f"异常分型：{by}")
        for kind in by:
            ex = an[an["kind"] == kind].iloc[0]
            lines.append(f"  - {kind} 例：{ex['file']}:{ex['line_no']} {ex['detail'][:80]}")
    lines += ["", "复跑：py -3.10 tools/archive_content_audit_glm.py all", ""]
    (OUT / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"[summary] -> {OUT / 'summary.md'}（coverage.csv {len(cov_rows)} 层）")
    return 0


def cmd_all(_args) -> int:
    cmd_sample(_args)
    cmd_audit(_args)
    cmd_fields(_args)
    cmd_summary(_args)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="原始归档内容审计（只读，有预算）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("sample", "audit", "fields", "summary", "all"):
        sub.add_parser(name)
    args = ap.parse_args()
    return {"sample": cmd_sample, "audit": cmd_audit, "fields": cmd_fields,
            "summary": cmd_summary, "all": cmd_all}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
