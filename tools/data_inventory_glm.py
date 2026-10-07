#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""全量归档清单（任务规格：tasks/glm53f_full_manifest.md）。

取代 v1 的抽样口径：修复 metrics/funding 的符号与日期解析、现货周期混用，
对旧清单可复用的行离线重解析（不重新联网），其余目录完整分页枚举。
产物：reports/data_inventory_glm_full/ 下的不可变分片 CSV + index + audit，
分片通过校验后写 .ready 标记才可交给下载器；半写分片不可消费。

解析原则：**目录为真相、文件名交叉核验**——symbol/interval 取自键路径目录段，
period 取自文件名尾部并与目录交叉核对；任一不一致记入 parse_failures.csv。

子命令：
  reuse    离线重解析 v1 旧清单 -> staging_*.csv + parse_failures.csv（不联网）
  remote   完整枚举缺失范围（断点续跑，按 scope 独立状态，两个 URL 不共享状态）
  finalize 分片化 + 校验 + ready 标记 + index/audit（可重复运行，幂等）
  rawscan  盘点 data/archive_raw 已下载文件并与清单对照
  summary  汇总 ready 分片、剩余未枚举范围
  verify   由分片重算 index（rows/bytes/sha256）
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OLD_OUT = ROOT / "reports" / "data_inventory_glm"        # v1 历史快照（只读复用）
OUT = ROOT / "reports" / "data_inventory_glm_full"       # v2 产物
RAW = DATA / "archive_raw"

S3 = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
CDN = "https://data.binance.vision"
NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "chuanmu-inventory/0.2"})

MANIFEST_FIELDS = ["source_url", "market", "dataset", "symbol", "interval",
                   "period_start", "period_end", "remote_size_bytes",
                   "checksum_url", "discovered_at_utc"]
PROGRESS = OUT / "remote_progress_full.json"

# scope -> (符号层前缀, market, dataset, interval, kind, 需要枚举?)
# kind: monthly=YYYY-MM 文件, daily=YYYY-MM-DD 文件
# 需要枚举=False 的 scope 只靠 reuse 离线复用（较粗周期只保留必要补充）
SCOPES: dict[str, dict] = {
    "spot_klines_1d_monthly":   {"prefix": "data/spot/monthly/klines", "market": "spot", "dataset": "klines", "interval": "1d", "kind": "monthly", "enum": False},
    "spot_klines_1h_monthly":   {"prefix": "data/spot/monthly/klines", "market": "spot", "dataset": "klines", "interval": "1h", "kind": "monthly", "enum": False},
    "spot_klines_5m_monthly":   {"prefix": "data/spot/monthly/klines", "market": "spot", "dataset": "klines", "interval": "5m", "kind": "monthly", "enum": True},
    "spot_klines_5m_daily":     {"prefix": "data/spot/daily/klines", "market": "spot", "dataset": "klines", "interval": "5m", "kind": "daily", "enum": True},
    "fut_um_klines_1h_monthly": {"prefix": "data/futures/um/monthly/klines", "market": "futures-um", "dataset": "klines", "interval": "1h", "kind": "monthly", "enum": False},
    "fut_um_klines_5m_monthly": {"prefix": "data/futures/um/monthly/klines", "market": "futures-um", "dataset": "klines", "interval": "5m", "kind": "monthly", "enum": False},
    "fut_um_klines_5m_daily":   {"prefix": "data/futures/um/daily/klines", "market": "futures-um", "dataset": "klines", "interval": "5m", "kind": "daily", "enum": True},
    "fut_um_metrics_daily":     {"prefix": "data/futures/um/daily/metrics", "market": "futures-um", "dataset": "metrics", "interval": "", "kind": "daily", "enum": True},
    "fut_um_funding_monthly":   {"prefix": "data/futures/um/monthly/fundingRate", "market": "futures-um", "dataset": "fundingRate", "interval": "", "kind": "monthly", "enum": False},
    "cm_klines_5m_monthly":     {"prefix": "data/futures/cm/monthly/klines", "market": "futures-cm", "dataset": "klines", "interval": "5m", "kind": "monthly", "enum": True},
    "cm_klines_5m_daily":       {"prefix": "data/futures/cm/daily/klines", "market": "futures-cm", "dataset": "klines", "interval": "5m", "kind": "daily", "enum": True},
    "cm_funding_monthly":       {"prefix": "data/futures/cm/monthly/fundingRate", "market": "futures-cm", "dataset": "fundingRate", "interval": "", "kind": "monthly", "enum": True},
    "cm_metrics_daily":         {"prefix": "data/futures/cm/daily/metrics", "market": "futures-cm", "dataset": "metrics", "interval": "", "kind": "daily", "enum": True},
}
# v1 旧清单 -> 解析后按真实 interval 归入的 scope（spot_5m 里混着非 USDT 的 1d 行）
REUSE_MAP = {
    "spot_1d": ["spot_klines_1d_monthly"],
    "spot_1h": ["spot_klines_1h_monthly", "spot_klines_1d_monthly"],
    "spot_5m": ["spot_klines_5m_monthly", "spot_klines_1d_monthly"],
    "fut_1h": ["fut_um_klines_1h_monthly"],
    "fut_5m": ["fut_um_klines_5m_monthly"],
    "funding": ["fut_um_funding_monthly"],
    "metrics_daily": ["fut_um_metrics_daily"],
}
INTERVAL_RE = re.compile(r"^(1s|1m|3m|5m|15m|30m|1h|2h|4h|6h|8h|12h|1d|3d|1w|1mo)$")
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SYMBOL_RE = re.compile(r"^\S+$")  # 归档存在非 ASCII 符号（如 币安人生USDT）；路径已按 / 切分
WORKERS_DEFAULT = 4
DELAY_DEFAULT = 0.12


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------- 解析（纯函数）--

def _period_from(stem: str, expect: str, kind: str):
    """文件名 = expect + "-" + period；取 period 并按 kind 校验格式与真实日期。"""
    if not stem.startswith(expect + "-"):
        return None
    period = stem[len(expect) + 1:]
    regex = MONTH_RE if kind == "monthly" else DAY_RE
    if not regex.match(period):
        return None
    try:
        datetime.strptime(period + ("-01" if kind == "monthly" else ""),
                          "%Y-%m-%d")
    except ValueError:
        return None
    return period


def parse_archive_key(key: str, size: int = 0) -> dict | None:
    """从归档键路径解析清单字段；目录为真相、文件名交叉核验。失败返回 None。"""
    parts = key.split("/")
    name = parts[-1]
    if not name.endswith(".zip"):
        return None
    stem = name[:-4]

    def ok(market, dataset, interval, kind, symbol, period):
        if not SYMBOL_RE.match(symbol or "") or not symbol:
            return None
        return {"market": market, "dataset": dataset, "interval": interval,
                "kind": kind, "symbol": symbol, "period": period, "size": size}

    # 现货 K 线：data/spot/{monthly|daily}/klines/{SYM}/{ITV}/{SYM}-{ITV}-{period}.zip
    if len(parts) == 7 and parts[1] == "spot" and parts[2] in ("monthly", "daily") \
            and parts[3] == "klines":
        sym, itv, kind = parts[4], parts[5], parts[2]
        period = _period_from(stem, f"{sym}-{itv}", kind)
        if period is None or not INTERVAL_RE.match(itv):
            return None
        return ok("spot", "klines", itv, kind, sym, period)
    # 合约 K 线：data/futures/{um|cm}/{monthly|daily}/klines/{SYM}/{ITV}/{...}
    if len(parts) == 8 and parts[1] == "futures" and parts[2] in ("um", "cm") \
            and parts[4] == "klines":
        sym, itv, kind = parts[5], parts[6], parts[3]
        period = _period_from(stem, f"{sym}-{itv}", kind)
        if period is None or not INTERVAL_RE.match(itv):
            return None
        return ok(f"futures-{parts[2]}", "klines", itv, kind, sym, period)
    # metrics 日度：data/futures/{um|cm}/daily/metrics/{SYM}/{SYM}-metrics-YYYY-MM-DD.zip
    if len(parts) == 7 and parts[1] == "futures" and parts[2] in ("um", "cm") \
            and parts[3] == "daily" and parts[4] == "metrics":
        sym = parts[5]
        period = _period_from(stem, f"{sym}-metrics", "daily")
        if period is None:
            return None
        return ok(f"futures-{parts[2]}", "metrics", "", "daily", sym, period)
    # funding 月度：data/futures/{um|cm}/monthly/fundingRate/{SYM}/{SYM}-fundingRate-YYYY-MM.zip
    if len(parts) == 7 and parts[1] == "futures" and parts[2] in ("um", "cm") \
            and parts[3] == "monthly" and parts[4] == "fundingRate":
        sym = parts[5]
        period = _period_from(stem, f"{sym}-fundingRate", "monthly")
        if period is None:
            return None
        return ok(f"futures-{parts[2]}", "fundingRate", "", "monthly", sym, period)
    return None


def parse_url(url: str, size: int = 0) -> dict | None:
    prefix = f"{CDN}/"
    return parse_archive_key(url[len(prefix):], size) if url.startswith(prefix) else None


def month_end(ym: str) -> str:
    y, m = int(ym[:4]), int(ym[5:7])
    return (datetime(y + (m == 12), m % 12 + 1, 1) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")


def month_of(day: str) -> str:
    return day[:7]


# --------------------------------------------------------------- S3（列举）--

def _s3_get(params: dict) -> requests.Response:
    r = None
    for attempt in (1, 2, 3):
        try:
            r = SESSION.get(S3, params=params, timeout=30)
        except (requests.exceptions.ProxyError, requests.exceptions.ConnectionError,
                requests.exceptions.Timeout, requests.exceptions.ChunkedEncodingError) as exc:
            wait = 5 * attempt
            print(f"  [网络] {type(exc).__name__}，等待 {wait}s 后"
                  f"{'重试' if attempt < 3 else '停止（断点已保存）'}", flush=True)
            if attempt < 3:
                time.sleep(wait)
                continue
            raise
        if r.status_code == 200:
            return r
        wait = int(r.headers.get("Retry-After") or 30 * attempt)
        print(f"  [限流] HTTP {r.status_code}，等待 {wait}s 后"
              f"{'重试' if attempt < 3 else '停止（断点已保存）'}", flush=True)
        if attempt < 3:
            time.sleep(wait)
    r.raise_for_status()
    raise RuntimeError(f"S3 列举持续失败：{params.get('prefix')}")


def s3_list(prefix: str, delimiter: str | None = None, delay: float = DELAY_DEFAULT):
    contents: list[tuple[str, int]] = []
    prefixes: list[str] = []
    token: str | None = None
    pages = 0
    while True:
        params: dict = {"list-type": "2", "prefix": prefix, "max-keys": "1000"}
        if delimiter:
            params["delimiter"] = delimiter
        if token:
            params["continuation-token"] = token
        root = ET.fromstring(_s3_get(params).text)
        contents += [(c.find("s3:Key", NS).text, int(c.find("s3:Size", NS).text))
                     for c in root.findall(".//s3:Contents", NS)]
        prefixes += [p.find("s3:Prefix", NS).text
                     for p in root.findall(".//s3:CommonPrefixes", NS)]
        pages += 1
        token_el = root.find("s3:NextContinuationToken", NS)
        trunc = root.find("s3:IsTruncated", NS)
        if token_el is None or (trunc is not None and trunc.text != "true"):
            return contents, prefixes, pages
        token = token_el.text
        time.sleep(delay)


def list_universe(prefix: str) -> list[str]:
    """符号目录列举；规范化前缀避免双斜杠（双斜杠会导致 S3 返回空）。"""
    _, prefixes, _ = s3_list(prefix.rstrip("/") + "/", delimiter="/")
    return sorted(p.rstrip("/").rsplit("/", 1)[-1] for p in prefixes)


def load_progress() -> dict:
    if PROGRESS.exists():
        return json.loads(PROGRESS.read_text(encoding="utf-8"))
    return {"started_at_utc": now_utc(), "scopes": {}, "universes": {}}


def save_progress(p: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    tmp = PROGRESS.with_suffix(".tmp")
    tmp.write_text(json.dumps(p, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(PROGRESS)


# ------------------------------------------------------------------- local --

def _ts_unit(series: pd.Series) -> tuple[str, pd.Series]:
    v = pd.to_numeric(series, errors="coerce").dropna()
    if v.empty:
        return "unknown", series
    us = int((v > 1e14).sum())
    ms = int(((v > 1e11) & (v <= 1e14)).sum())
    norm = series.copy()
    if us and ms:
        return "mixed", norm
    if us:
        return "us", norm.astype("float64") / 1000.0
    return "ms", norm


def scan_kline_file(path: Path, interval: str, symbol: str, period: str | None) -> dict:
    row = {"file": str(path.relative_to(ROOT)), "dataset": "klines", "symbol": symbol,
           "interval": interval, "size_bytes": path.stat().st_size, "rows": 0,
           "ts_min_utc": "", "ts_max_utc": "", "ts_unit": "", "months_expected": 0,
           "months_present": 0, "months_missing": 0, "dup_keys": 0,
           "readable": False, "note": "", "months_list": ""}
    try:
        df = pd.read_parquet(path)
        row["readable"] = True
    except Exception as exc:
        row["note"] = f"读取失败:{type(exc).__name__}"
        return row
    row["rows"] = len(df)
    if "open_time" not in df.columns or df.empty:
        row["note"] = "缺 open_time 或空文件"
        return row
    unit, norm = _ts_unit(df["open_time"])
    row["ts_unit"] = unit
    dup = int(norm.duplicated().sum())
    row["dup_keys"] = dup
    ts = pd.to_datetime(norm.astype("int64"), unit="ms", utc=True, errors="coerce").dropna()
    if ts.empty:
        row["note"] = "open_time 全部无效"
        return row
    row["ts_min_utc"] = ts.min().strftime("%Y-%m-%d")
    row["ts_max_utc"] = ts.max().strftime("%Y-%m-%d")
    present = sorted({d.strftime("%Y-%m") for d in ts})
    row["months_present"] = len(present)
    row["months_list"] = ";".join(present)
    expected = [period] if period else sorted({d.strftime("%Y-%m") for d in ts})
    row["months_expected"] = len(expected)
    missing = [m for m in expected if m not in set(present)]
    row["months_missing"] = len(missing)
    notes = []
    if missing:
        notes.append("缺月:" + ",".join(missing[:6]) + ("…" if len(missing) > 6 else ""))
    if unit == "mixed":
        notes.append("毫秒/微秒混用")
    if dup:
        notes.append(f"重复open_time {dup}行")
    if interval == "1h" and len(df) < 400:
        notes.append(f"行数偏少({len(df)})")
    row["note"] = "; ".join(notes)
    return row


def cmd_local(_args) -> int:
    """v1 本地盘点保留（研究数据目录），另算 data/archive_raw 用 rawscan。"""
    (OLD_OUT).mkdir(parents=True, exist_ok=True)
    one_d = sorted((DATA / "1d").glob("*.parquet"))
    one_h = sorted((DATA / "1h").glob("*.parquet"))
    print(f"[local] 日线 {len(one_d)} + 小时线 {len(one_h)} 个文件…", flush=True)
    rows: list[dict] = []
    stem_re = re.compile(r"^(.+)-(\d{4}-\d{2})$")

    def one_h_parts(p: Path) -> tuple[str, str | None]:
        m = stem_re.match(p.stem)
        return (m.group(1), m.group(2)) if m else (p.stem, None)

    with ThreadPoolExecutor(max_workers=8) as ex:
        rows += list(ex.map(lambda p: scan_kline_file(p, "1d", p.stem, None), one_d))
        rows += list(ex.map(lambda p: scan_kline_file(p, "1h", *one_h_parts(p)), one_h))
    cols = ["file", "dataset", "symbol", "interval", "size_bytes", "rows",
            "ts_min_utc", "ts_max_utc", "ts_unit", "months_expected",
            "months_present", "months_missing", "dup_keys", "readable", "note"]
    with (OLD_OUT / "inventory.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"[local] inventory.csv {len(rows)} 行 -> {OLD_OUT}")
    return 0


# ------------------------------------------------------------------- reuse --

def scope_of_parsed(p: dict) -> str | None:
    for name, cfg in SCOPES.items():
        if (cfg["market"], cfg["dataset"], cfg["interval"], cfg["kind"]) == \
                (p["market"], p["dataset"], p["interval"], p["kind"]):
            return name
    return None


def cmd_reuse(_args) -> int:
    """离线重解析 v1 旧清单：URL 路径为真相，修复 symbol/interval/period。"""
    OUT.mkdir(parents=True, exist_ok=True)
    staged_rows: dict[str, list[dict]] = {s: [] for s in SCOPES}
    seen: dict[str, set[str]] = {s: set() for s in SCOPES}
    failures: list[dict] = []
    for part, targets in REUSE_MAP.items():
        path = OLD_OUT / f"remote_manifest_part_{part}.csv"
        if not path.exists():
            print(f"[reuse] 缺 {path.name}，跳过")
            continue
        mf = pd.read_csv(path)
        for _, r in mf.iterrows():
            url, size = str(r["source_url"]), int(r["remote_size_bytes"])
            p = parse_url(url, size)
            if p is None:
                failures.append({"source": part, "source_url": url, "reason": "路径解析失败"})
                continue
            scope = scope_of_parsed(p)
            if scope is None or scope not in targets:
                failures.append({"source": part, "source_url": url,
                                 "reason": f"解析为 {p['market']}/{p['dataset']}/"
                                           f"{p['interval']}/{p['kind']}，不在 {targets}"})
                continue
            if url in seen[scope]:
                continue
            seen[scope].add(url)
            staged_rows[scope].append({
                "source_url": url, "market": p["market"], "dataset": p["dataset"],
                "symbol": p["symbol"], "interval": p["interval"],
                "period_start": p["period"] + ("-01" if p["kind"] == "monthly" else ""),
                "period_end": month_end(p["period"]) if p["kind"] == "monthly" else p["period"],
                "remote_size_bytes": size,
                "checksum_url": url + ".CHECKSUM",
                "discovered_at_utc": str(r.get("discovered_at_utc", ""))})
    for scope, rows in staged_rows.items():
        if not rows:
            continue
        rows.sort(key=lambda r: (r["symbol"], r["period_start"]))
        with (OUT / f"staging_{scope}.csv").open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=MANIFEST_FIELDS)
            w.writeheader()
            w.writerows(rows)
    with (OUT / "parse_failures.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["source", "source_url", "reason"])
        w.writeheader()
        w.writerows(failures)
    summary_counts = {s: len(r) for s, r in staged_rows.items() if r}
    (OUT / "reuse_counts.json").write_text(
        json.dumps(summary_counts, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[reuse] 复用行 {sum(summary_counts.values())} / 解析失败 {len(failures)}；"
          f"按 scope：{summary_counts}")
    return 0


# ------------------------------------------------------------------ remote --

def staging_symbols(scope: str) -> set[str]:
    path = OUT / f"staging_{scope}.csv"
    if not path.exists():
        return set()
    df = pd.read_csv(path, usecols=["symbol"])
    return set(df["symbol"].astype(str))


def enumerate_scope(scope: str, progress: dict, delay: float, workers: int) -> None:
    cfg = SCOPES[scope]
    pcfg = progress["scopes"].setdefault(scope, {})
    uni_key = cfg["prefix"]
    if uni_key not in progress["universes"]:
        progress["universes"][uni_key] = list_universe(cfg["prefix"])
        print(f"[remote] {scope}: 归档符号 {len(progress['universes'][uni_key])} 个", flush=True)
    universe = progress["universes"][uni_key]
    done_symbols = staging_symbols(scope) if pcfg.get("resume_by_symbol", True) \
        else set()
    targets = sorted(set(universe) - done_symbols)
    pcfg.update({"universe": len(universe), "target": len(targets),
                 "staged": len(done_symbols), "interval": cfg["interval"],
                 "kind": cfg["kind"], "prefix": cfg["prefix"]})
    if not targets:
        pcfg["done"] = True
        save_progress(progress)
        print(f"[remote] {scope}: staging 已覆盖全部符号，跳过", flush=True)
        return
    part = OUT / f"staging_{scope}.csv"
    mode_append = part.exists()
    f = part.open("a" if mode_append else "w", newline="", encoding="utf-8-sig")
    w = csv.DictWriter(f, fieldnames=MANIFEST_FIELDS)
    if not mode_append:
        w.writeheader()
    state = {"files": 0, "bytes": 0}
    lock = __import__("threading").Lock()

    def work(sym: str) -> tuple[int, int]:
        sub = f"{cfg['prefix']}/{sym}/" + (f"{cfg['interval']}/" if cfg["interval"] else "")
        contents, _, _ = s3_list(sub, delay=delay)
        rows = []
        nf = nb = 0
        stamp = now_utc()
        for key, size in contents:
            p = parse_archive_key(key, size)
            if p is None or p["interval"] != cfg["interval"] or p["symbol"] != sym:
                with lock:
                    failures_append(scope, key)
                continue
            rows.append({"source_url": f"{CDN}/{key}", "market": p["market"],
                         "dataset": p["dataset"], "symbol": p["symbol"],
                         "interval": p["interval"],
                         "period_start": p["period"] + ("-01" if p["kind"] == "monthly" else ""),
                         "period_end": month_end(p["period"]) if p["kind"] == "monthly" else p["period"],
                         "remote_size_bytes": size,
                         "checksum_url": f"{CDN}/{key}.CHECKSUM",
                         "discovered_at_utc": stamp})
            nf += 1
            nb += size
        return rows, nf, nb

    failures_path = OUT / "parse_failures.csv"
    seen_failures: set[str] = set()

    def failures_append(scope: str, key: str) -> None:
        if key in seen_failures:
            return
        seen_failures.add(key)
        header = not failures_path.exists()
        with failures_path.open("a", newline="", encoding="utf-8-sig") as ff:
            w2 = csv.DictWriter(ff, fieldnames=["source", "source_url", "reason"])
            if header:
                w2.writeheader()
            w2.writerow({"source": scope, "source_url": f"{CDN}/{key}",
                         "reason": "枚举行解析失败或与目录不符"})

    try:
        chunk = max(1, workers * 4)
        for lo in range(0, len(targets), chunk):
            batch = targets[lo:lo + chunk]
            with ThreadPoolExecutor(max_workers=workers) as ex:
                for rows, nf, nb in ex.map(work, batch):
                    if rows:
                        w.writerows(rows)
                    state["files"] += nf
                    state["bytes"] += nb
            pcfg["next_index"] = lo + len(batch)
            pcfg["files"] = pcfg.get("files", 0) + state["files"]
            pcfg["bytes"] = pcfg.get("bytes", 0) + state["bytes"]
            state = {"files": 0, "bytes": 0}
            save_progress(progress)
            print(f"[remote] {scope}: {pcfg['next_index']}/{len(targets)} 符号", flush=True)
        pcfg["done"] = True
        pcfg["finished_at_utc"] = now_utc()
    finally:
        f.close()
        pcfg["files"] = pcfg.get("files", 0) + state["files"]
        pcfg["bytes"] = pcfg.get("bytes", 0) + state["bytes"]
        save_progress(progress)


def probe_extensions(progress: dict, delay: float) -> None:
    """轻量合约资料：只探存在性 + 每符号抽样体积，列扩展清单，不阻塞基础范围。"""
    rows = []
    probes = [
        "data/futures/um/monthly/markPriceKlines",
        "data/futures/um/monthly/indexPriceKlines",
        "data/futures/um/monthly/premiumIndexKlines",
        "data/futures/um/daily/bookDepth",
        "data/futures/um/daily/bookTicker",
    ]
    for prefix in probes:
        try:
            universe = list_universe(prefix)
            sample = universe[::20][:6]
            files = bytes_total = 0
            intervals: set[str] = set()
            for sym in sample:
                contents, _, _ = s3_list(f"{prefix}/{sym}/", delay=delay)
                for key, size in contents:
                    if key.endswith(".zip"):
                        files += 1
                        bytes_total += size
                        seg = key.split("/")
                        if len(seg) >= 3:
                            intervals.add(seg[-2])
            rows.append({"prefix": prefix, "symbols": len(universe),
                         "sample_symbols": len(sample), "sample_files": files,
                         "sample_bytes": bytes_total,
                         "intervals_seen": ";".join(sorted(intervals)),
                         "note": "universe 为空（目录不存在或无符号目录）"
                                 if not universe else "抽样探针，未全量枚举"})
        except Exception as exc:
            rows.append({"prefix": prefix, "symbols": 0, "sample_symbols": 0,
                         "sample_files": 0, "sample_bytes": 0, "intervals_seen": "",
                         "note": f"探测失败: {type(exc).__name__}: {exc}"})
    pd.DataFrame(rows).to_csv(OUT / "extension_datasets.csv", index=False,
                              encoding="utf-8-sig")
    print(f"[remote] 扩展数据集探针 {len(rows)} 项 -> extension_datasets.csv", flush=True)


def cmd_remote(args) -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    progress = load_progress()
    names = [n.strip() for n in args.only.split(",") if n.strip()]
    order = [s for s in SCOPES if SCOPES[s]["enum"]]
    targets = [n for n in order if not names or n in names]
    try:
        for scope in targets:
            enumerate_scope(scope, progress, args.delay, args.workers)
        if not names or "extensions" in names:
            probe_extensions(progress, args.delay)
    except Exception as exc:
        save_progress(progress)
        print(f"[remote] 中断：{type(exc).__name__}: {exc}\n"
              f"断点已保存，续跑: py -3.10 tools/data_inventory_glm.py remote", flush=True)
        return 2
    print("[remote] 完成", flush=True)
    return 0


# ---------------------------------------------------------------- finalize --

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def dedupe_daily(daily: pd.DataFrame, monthly: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """月度覆盖去重：**同 market+dataset+symbol+interval** 的月度才剔除对应日度行。

    返回 (kept, dropped)。键含 symbol——v1 曾漏掉 symbol 导致 A 币月度误删 B 币日度。
    """
    if daily.empty or monthly.empty:
        return daily, daily.iloc[0:0]
    covered = {(m, d, s, i, str(p)[:7]) for m, d, s, i, p in zip(
        monthly["market"], monthly["dataset"], monthly["symbol"],
        monthly["interval"], monthly["period_start"])}
    keys = list(zip(daily["market"], daily["dataset"], daily["symbol"],
                    daily["interval"], daily["period_start"].str[:7]))
    mask = pd.Series([k in covered for k in keys], index=daily.index)
    return daily[~mask].copy(), daily[mask].copy()


def validate_shard(rows: list[dict], kind: str) -> tuple[bool, dict]:
    """分片校验：字段非空、period 为真实日期（月度分片 period_start 须为月初）、
    interval 与 dataset 匹配、URL 唯一、体积为正。period_start/end 一律完整日期。"""
    checks = {"rows": len(rows), "empty_symbol": 0, "bad_period": 0,
              "bad_interval": 0, "dup_url": 0, "zero_size": 0}
    seen: set[str] = set()
    for r in rows:
        symbol = str(r.get("symbol") or "")
        interval = str(r.get("interval") or "")
        if interval in ("nan", "None"):
            interval = ""
        if not symbol or not SYMBOL_RE.match(symbol) or symbol in ("nan", "None"):
            checks["empty_symbol"] += 1
        ps, pe = str(r.get("period_start") or ""), str(r.get("period_end") or "")
        try:
            datetime.strptime(ps, "%Y-%m-%d")
            datetime.strptime(pe, "%Y-%m-%d")
            if kind == "monthly" and not ps.endswith("-01"):
                checks["bad_period"] += 1
            if kind == "monthly" and (DAY_RE.match(ps) is None or DAY_RE.match(pe) is None):
                checks["bad_period"] += 1
        except ValueError:
            checks["bad_period"] += 1
        if r.get("dataset") == "klines" and not INTERVAL_RE.match(interval):
            checks["bad_interval"] += 1
        if r.get("dataset") != "klines" and interval:
            checks["bad_interval"] += 1
        if r["source_url"] in seen:
            checks["dup_url"] += 1
        seen.add(r["source_url"])
        try:
            if int(r["remote_size_bytes"]) <= 0:
                checks["zero_size"] += 1
        except (TypeError, ValueError):
            checks["zero_size"] += 1
    passed = (checks["empty_symbol"] == 0 and checks["bad_period"] == 0
              and checks["bad_interval"] == 0 and checks["dup_url"] == 0
              and checks["zero_size"] == 0 and checks["rows"] > 0)
    return passed, checks


def cmd_finalize(args) -> int:
    """把各 scope 的 staging 切成按年分块的不可变分片；校验通过才写 .ready。

    v2（revisions/<ver>/）：修复月度覆盖键缺 symbol 导致的跨币误删——月度覆盖键
    为 market+dataset+symbol+interval+月。旧分片不可变、不原地替换；本轮只把
    **行集发生变化**的日度分片重写到 revisions 目录，index_v2 给出 shard_path 与
    supersedes 映射，月度及未变化分片沿用原路径。
    """
    OUT.mkdir(parents=True, exist_ok=True)
    version = getattr(args, "version", "r2")
    rev_dir = OUT / "revisions" / version
    rev_dir.mkdir(parents=True, exist_ok=True)
    names = [n.strip() for n in args.only.split(",") if n.strip()]
    progress = load_progress()
    restored_total = 0
    diff_rows: list[dict] = []
    regen: dict[str, dict] = {}   # shard 名 -> 新 meta（仅行集变化的分片）

    for scope, cfg in SCOPES.items():
        if names and scope not in names:
            continue
        path = OUT / f"staging_{scope}.csv"
        if not path.exists():
            continue
        df = pd.read_csv(path, dtype=str, keep_default_na=False)
        df["remote_size_bytes"] = pd.to_numeric(df["remote_size_bytes"], errors="coerce")\
            .fillna(0).astype("int64")
        done = bool(progress["scopes"].get(scope, {}).get("done")) \
            or not SCOPES[scope]["enum"]
        if not done and SCOPES[scope]["enum"]:
            print(f"[finalize] {scope} 枚举未完成，跳过（staging {len(df)} 行待续）")
            continue
        kept, dropped = df, df.iloc[0:0]
        if cfg["kind"] == "daily" and cfg["dataset"] == "klines":
            monthly_path = OUT / f"staging_{scope.replace('_daily', '_monthly')}.csv"
            monthly = pd.read_csv(monthly_path, dtype=str, keep_default_na=False) \
                if monthly_path.exists() else pd.DataFrame(columns=df.columns)
            kept, dropped = dedupe_daily(df, monthly)
            # 旧 buggy 键（无 symbol）下的被剔集，用于还原「误删」明细
            if not monthly.empty:
                old_months = {(m, d, i, str(p)[:7]) for m, d, i, p in zip(
                    monthly["market"], monthly["dataset"], monthly["interval"],
                    monthly["period_start"])}
                old_keys = list(zip(df["market"], df["dataset"], df["interval"],
                                    df["period_start"].str[:7]))
                dropped_old = df[[k in old_months for k in old_keys]]
                restored = pd.concat([dropped_old, dropped]).drop_duplicates(
                    subset=["source_url"], keep=False)
            else:
                restored = df.iloc[0:0]
            if len(restored):
                restored_total += len(restored)
                for _, r in restored.iterrows():
                    diff_rows.append({"scope": scope, "symbol": r["symbol"],
                                      "interval": r["interval"],
                                      "period_start": r["period_start"],
                                      "period_end": r["period_end"],
                                      "source_url": r["source_url"],
                                      "reason": "旧键缺 symbol 被跨币误删；"
                                                "同币同月度未覆盖，恢复保留"})
            df = kept
        if df.empty:
            print(f"[finalize] {scope}: 无行，跳过")
            continue
        df = df.sort_values(["symbol", "period_start"])
        df["year"] = df["period_start"].str[:4]
        for year, g in df.groupby("year"):
            shard_name = f"{cfg['market']}_{cfg['dataset']}_" \
                         f"{cfg['interval'] or 'na'}_{cfg['kind']}_{year}.csv"
            rows = g.drop(columns=["year"]).to_dict("records")
            passed, checks = validate_shard(rows, cfg["kind"])
            if not passed:
                print(f"[finalize] {scope} {year} 校验失败 {checks}，未交付")
                continue
            old_shard = OUT / shard_name
            new_rows = {r["source_url"] for r in rows}
            unchanged = False
            if old_shard.exists():
                old_df = pd.read_csv(old_shard, dtype=str, keep_default_na=False)
                unchanged = set(old_df["source_url"]) == new_rows \
                    and len(old_df) == len(rows)
            if unchanged:
                continue  # 行集与旧分片一致：沿用原文件，不重写
            new_shard = rev_dir / shard_name
            tmp = new_shard.with_suffix(".tmp")
            g.drop(columns=["year"]).to_csv(tmp, index=False, encoding="utf-8-sig")
            tmp.replace(new_shard)
            meta = {"shard": shard_name,
                    "shard_path": str(new_shard.relative_to(ROOT)),
                    "scope": scope, "market": cfg["market"], "dataset": cfg["dataset"],
                    "interval": cfg["interval"], "kind": cfg["kind"], "year": year,
                    "rows": len(rows), "bytes_sum": int(g["remote_size_bytes"].sum()),
                    "symbols": int(g["symbol"].nunique()),
                    "period_min": str(g["period_start"].min()),
                    "period_max": str(g["period_end"].max()),
                    "unique_urls": checks["rows"] - checks["dup_url"],
                    "sha256": _sha256(new_shard), "ready_at": now_utc(),
                    "version": version,
                    "supersedes": str(old_shard.relative_to(ROOT)) if old_shard.exists() else "",
                    "restored_rows": int((g["source_url"].isin(
                        restored["source_url"])).sum()) if len(restored) else 0}
            Path(str(new_shard) + ".ready").write_text(
                json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
            regen[shard_name] = meta
            print(f"[finalize] {scope} {year}: 重写 {len(rows)} 行"
                  f"（恢复 {meta['restored_rows']}）-> {new_shard.relative_to(ROOT)}")
        print(f"[finalize] {scope}: 完成")

    # index_v2.csv：全部 ready 分片（变化的指向 revisions，未变化沿用原路径+原 sha）
    old_index = OUT / "index.csv"
    cols = ["shard", "shard_path", "scope", "market", "dataset", "interval", "kind",
            "year", "rows", "bytes_sum", "symbols", "period_min", "period_max",
            "unique_urls", "sha256", "ready_at", "version", "supersedes",
            "restored_rows"]
    index_rows: list[dict] = []
    if old_index.exists():
        old = pd.read_csv(old_index, dtype=str, keep_default_na=False)
        for _, r in old.iterrows():
            if r["shard"] in regen:
                index_rows.append(regen[r["shard"]])
            else:
                index_rows.append({**r.to_dict(), "shard_path": f"reports/data_inventory_glm_full/{r['shard']}",
                                   "version": "r1", "supersedes": "", "restored_rows": 0})
    for name, meta in regen.items():
        if all(r["shard"] != name for r in index_rows):
            index_rows.append(meta)
    index_rows.sort(key=lambda r: r["shard"])
    with (rev_dir / "index_v2.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(index_rows)
    pd.DataFrame(diff_rows).to_csv(rev_dir / "dedupe_diff.csv", index=False,
                                   encoding="utf-8-sig")
    corrections = [
        "# 清单修正 r2", "",
        f"生成时间：{now_utc()}", "",
        "1. 月度覆盖去重键补上 symbol（market+dataset+symbol+interval+月）：",
        f"   修复跨币误删，恢复保留 {restored_total} 行（逐行见 dedupe_diff.csv）。",
        "   分月度/市场/周期隔离测试见 tests/test_inventory_manifest.py。",
        "2. list_universe/probe_extensions 双斜杠修复后重探：结论见 probe_evidence.json，",
        "   旧「五类皆空」结论作废（以本轮实测为准，注明日期）。",
        "3. rawscan 改读 ds 状态文件（_state.json），按 market|dataset|symbol|interval|周期",
        "   键匹配，处理 futures↔futures-um 别名；匹配/文件存在/来源校验分开报告。",
        "", "旧分片与 index.csv 原样保留；本目录 index_v2.csv 为消费真源。",
    ]
    (rev_dir / "corrections.md").write_text("\n".join(corrections), encoding="utf-8")
    print(f"[finalize] 版本 {version}: 重写分片 {len(regen)}，恢复 {restored_total} 行；"
          f"index_v2.csv {len(index_rows)} 项 -> {rev_dir}")
    return 0


# ----------------------------------------------------------------- rawscan --

MARKET_ALIAS = {"futures": "futures-um", "spot": "spot", "futures-cm": "futures-cm"}


def cmd_rawscan(_args) -> int:
    """读 ds 下载器状态文件（data/archive_raw/_state.json），以**最终 index_v2**为分母。

    剩余未下载 = index_v2 行数（去重后正式清单）− 状态已覆盖的清单行数。
    不得用去重前的 staging 行数冒充剩余量（月度+日度重叠行会被重复计）。
    「来源校验通过」= 读取状态中已有的 checksum_status 字段，本次未实测。
    匹配兼容 ds 状态键的裸月/裸日格式（月度清单行归一到 YYYY-MM 比对）。
    快照范围 = 状态文件里已写入的记录（仅 ds 已尝试的类别），不是全部清单。
    只读；零匹配/部分匹配只如实报告，不触发任何重新下载。
    """
    OUT.mkdir(parents=True, exist_ok=True)
    rev_dir = OUT / "revisions" / "r2"
    rev_dir.mkdir(parents=True, exist_ok=True)
    state_path = RAW / "_state.json"
    snapshot = now_utc()
    if not state_path.exists():
        print(f"[rawscan] 状态文件不存在: {state_path}")
        return 1
    raw = json.loads(state_path.read_text(encoding="utf-8"))
    records = raw.get("records") or {}

    idx_path = rev_dir / "index_v2.csv"
    if not idx_path.exists():
        idx_path = OUT / "index.csv"
    idx = pd.read_csv(idx_path, dtype=str, keep_default_na=False)

    # 状态记录解析（不联网、不改状态）
    recs: list[dict] = []
    counts = {"records": len(records), "alias_futures": 0,
              "matched_daily_form": 0, "matched_bare_month_form": 0,
              "legacy_single_day_in_monthly": 0, "not_matched": 0,
              "file_exists": 0,
              "checksum_verified_from_state": 0, "checksum_other": 0}
    for key, rec in records.items():
        parts = str(key).split("|")
        parts += [""] * (6 - len(parts))
        alias, dataset, symbol, interval, ps, pe = parts[:6]
        market = MARKET_ALIAS.get(alias, alias)
        if alias == "futures":
            counts["alias_futures"] += 1
        recs.append({"key": key, "market": market, "dataset": dataset,
                     "symbol": symbol, "interval": interval, "ps": ps, "pe": pe,
                     "rec": rec})

    # 读取分片，建正式清单键集（月度裸月 + 日度完整日期），同时累计总行数
    index_day_keys: set[tuple] = set()
    index_month_keys: set[tuple] = set()
    shard_rows = 0
    for _, r in idx.iterrows():
        shard = ROOT / str(r.get("shard_path") or
                           f"reports/data_inventory_glm_full/{r['shard']}")
        if not shard.exists():
            print(f"[rawscan] 警告：分片缺失 {shard}")
            continue
        df = pd.read_csv(shard, dtype=str, keep_default_na=False,
                         usecols=["market", "dataset", "symbol", "interval",
                                  "period_start", "period_end"])
        shard_rows += len(df)
        if str(r["kind"]) == "monthly":
            index_month_keys |= set(zip(
                df["market"], df["dataset"], df["symbol"], df["interval"],
                df["period_start"].str[:7], df["period_end"].str[:7]))
        else:
            index_day_keys |= set(zip(
                df["market"], df["dataset"], df["symbol"], df["interval"],
                df["period_start"], df["period_end"]))

    # 状态记录逐条判定（三种形态），并构建已覆盖键集合
    remain_counter: dict[tuple, int] = {}
    matched_keys: set[tuple] = set()
    out_rows: list[dict] = []
    for item in recs:
        base = (item["market"], item["dataset"], item["symbol"], item["interval"])
        k_day = base + (item["ps"], item["pe"])
        k_month = base + (item["ps"][:7], item["pe"][:7])
        rec = item["rec"]
        if k_day in index_day_keys:
            match = "daily"
            matched_keys.add(k_day)
            counts["matched_daily_form"] += 1
        elif k_month in index_month_keys:
            match = "monthly(bare)"
            matched_keys.add(k_month)
            counts["matched_bare_month_form"] += 1
        elif item["ps"] == item["pe"] and DAY_RE.match(item["ps"] or "") and \
                base + (item["ps"][:7], item["ps"][:7]) in index_month_keys:
            # 首批验证的旧键：单日记录，但对应月份已在月度清单覆盖
            match = "legacy_single_day"
            matched_keys.add(base + (item["ps"][:7], item["ps"][:7]))
            counts["legacy_single_day_in_monthly"] += 1
        else:
            match = ""
            counts["not_matched"] += 1
        rec_path = Path(str(rec.get("path", "")))
        if rec_path.exists():
            counts["file_exists"] += 1
        cks = str(rec.get("checksum_status", ""))
        if cks == "verified":
            counts["checksum_verified_from_state"] += 1
        else:
            counts["checksum_other"] += 1
        out_rows.append({"key": item["key"], "market": item["market"],
                         "dataset": item["dataset"], "symbol": item["symbol"],
                         "interval": item["interval"],
                         "period_start": item["ps"], "period_end": item["pe"],
                         "match": match, "status": rec.get("status", ""),
                         "bytes": rec.get("bytes", 0), "checksum_status": cks,
                         "file_exists": rec_path.exists()})

    # 剩余量：逐分片统计不在 matched_keys 的正式清单行
    matched_index_rows = 0
    for _, r in idx.iterrows():
        shard = ROOT / str(r.get("shard_path") or
                           f"reports/data_inventory_glm_full/{r['shard']}")
        if not shard.exists():
            continue
        df = pd.read_csv(shard, dtype=str, keep_default_na=False,
                         usecols=["market", "dataset", "symbol", "interval",
                                  "period_start", "period_end"])
        if str(r["kind"]) == "monthly":
            keys = list(zip(df["market"], df["dataset"], df["symbol"],
                            df["interval"], df["period_start"].str[:7],
                            df["period_end"].str[:7]))
        else:
            keys = list(zip(df["market"], df["dataset"], df["symbol"],
                            df["interval"], df["period_start"], df["period_end"]))
        flags = [k not in matched_keys for k in keys]
        matched_index_rows += sum(1 for f in flags if not f)
        for f, (_, rr) in zip(flags, df.iterrows()):
            if f:
                mk = (rr["market"], rr["dataset"])
                remain_counter[mk] = remain_counter.get(mk, 0) + 1
    remaining = shard_rows - matched_index_rows

    pd.DataFrame(out_rows).to_csv(rev_dir / "archive_raw_inventory.csv",
                                  index=False, encoding="utf-8-sig")
    summary = {
        "snapshot_utc": snapshot,
        "index_used": str(idx_path.relative_to(ROOT)),
        "index_rows": len(idx),
        "index_source_rows": shard_rows,
        "boundary_note": "状态快照仅覆盖 ds 已写入的记录（当前为 futures-um 的 "
                         "metrics/funding/klines 首批与早期验证），不代表全部类别；"
                         "下载器运行中时之后的写入不在本快照内。"
                         "「校验通过」= 读取状态既有 checksum_status 字段，本次未实测。"
                         "剩余量分母为 index_v2（去重后正式清单）；未触发任何重新下载。",
        "counts": counts,
        "matched_index_rows": matched_index_rows,
        "remaining_rows_official": remaining,
        "remaining_by_market_dataset": {f"{k[0]}|{k[1]}": v
                                        for k, v in sorted(remain_counter.items())},
        "state_updated_at_utc": raw.get("updated_at_utc", ""),
    }
    (rev_dir / "rawscan_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[rawscan] 快照 {snapshot}，索引 {idx_path.name}（{len(idx)} 分片/"
          f"{shard_rows} 行）：状态记录 {counts['records']}，覆盖清单行 "
          f"{matched_index_rows}（日度形 {counts['matched_daily_form']} / 裸月形 "
          f"{counts['matched_bare_month_form']} / 旧单日 "
          f"{counts['legacy_single_day_in_monthly']}），未匹配记录 "
          f"{counts['not_matched']}；文件存在 {counts['file_exists']}；"
          f"校验通过(读状态) {counts['checksum_verified_from_state']}；"
          f"正式剩余未下载 {remaining} 行")
    return 0


def cmd_summary(_args) -> int:
    progress = load_progress()
    lines = ["# 全量归档清单（v2）", "", f"生成时间：{now_utc()}", ""]
    lines += ["## ready 分片（可交给下载器）", ""]
    if (OUT / "index.csv").exists():
        idx = pd.read_csv(OUT / "index.csv")
        for _, r in idx.iterrows():
            lines.append(f"- `{r['shard']}`：{int(r['rows'])} 行 / "
                         f"{int(r['symbols'])} 币 / {r['period_min']}~{r['period_max']} / "
                         f"sha256 {str(r['sha256'])[:12]}…")
        lines.append("")
        lines.append(f"合计 {len(idx)} 个分片、{int(idx['rows'].sum())} 行、"
                     f"{int(idx['bytes_sum'].sum())/1e9:.2f}GB")
    else:
        lines.append("- 无（先跑 reuse/finalize）")
    lines += ["", "## 枚举状态（scope 级，完成≠抽样）", ""]
    for scope, cfg in SCOPES.items():
        pc = progress["scopes"].get(scope, {})
        if not cfg["enum"]:
            staged = len(staging_symbols(scope))
            lines.append(f"- {scope}：仅离线复用（较粗周期补充），staging {staged} 符号")
            continue
        lines.append(f"- {scope}：{'✅ 完成' if pc.get('done') else '⏳ 未完成'}"
                     f"（universe {pc.get('universe')}，本轮枚举 {pc.get('files', 0)} 文件）")
    lines += ["", "## 未查范围（不冒充完整）", "",
              "- 现货非 USDT 的 1d/1h 月度目录未枚举（较粗周期补充未要求全量）；",
              "- 扩展轻量数据集见 extension_datasets.csv（抽样探针，未全量）；",
              "- 冻结时点：清单覆盖到枚举时归档已发布的最新完整周期，之后的发布属于下次刷新。"]
    (OUT / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"[summary] -> {OUT / 'summary.md'}")
    return 0


# ------------------------------------------------------------------ verify --

def cmd_verify(args) -> int:
    ok = True
    idx_path = Path(args.index) if args.index else (OUT / "index.csv")
    if not idx_path.exists():
        print(f"verify: 无 {idx_path}")
        return 1
    idx = pd.read_csv(idx_path, dtype=str, keep_default_na=False)
    for _, r in idx.iterrows():
        shard = ROOT / str(r.get("shard_path") or f"reports/data_inventory_glm_full/{r['shard']}")
        if not shard.exists():
            print(f"verify: {r['shard']} 文件缺失（{shard}）")
            ok = False
            continue
        df = pd.read_csv(shard, dtype=str, keep_default_na=False)
        sha = _sha256(shard)
        passed, checks = validate_shard(df.to_dict("records"), str(r["kind"]))
        if len(df) != int(r["rows"]) or sha != r["sha256"] or not passed:
            print(f"verify: {r['shard']} 不一致 rows {len(df)}/{r['rows']} "
                  f"sha {sha[:12]}/{str(r['sha256'])[:12]} checks {checks}")
            ok = False
    print("verify:", "PASS" if ok else "FAIL",
          f"（{len(idx)} 个 ready 分片全部由明细重算：{idx_path.name}）")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="全量归档清单（v2）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("local")
    sub.add_parser("reuse")
    p = sub.add_parser("remote")
    p.add_argument("--delay", type=float, default=DELAY_DEFAULT)
    p.add_argument("--workers", type=int, default=WORKERS_DEFAULT)
    p.add_argument("--only", default="")
    p = sub.add_parser("finalize")
    p.add_argument("--only", default="")
    p.add_argument("--version", default="r2")
    sub.add_parser("rawscan")
    sub.add_parser("summary")
    p = sub.add_parser("verify")
    p.add_argument("--index", default="", help="指定索引文件；默认 reports/.../index.csv")
    args = ap.parse_args()
    fn = {"local": cmd_local, "reuse": cmd_reuse, "remote": cmd_remote,
          "finalize": cmd_finalize, "rawscan": cmd_rawscan,
          "summary": cmd_summary, "verify": cmd_verify}[args.cmd]
    return fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
