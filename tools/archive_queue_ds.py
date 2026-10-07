#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""archive_queue_ds · 全量归档队列：清单规范化、冲突阻断、分块自动续传

配合 `tools/archive_download_ds.py` 使用（下载原语在那里）。本文件负责：

1. **规范化**：glm 的清单里有已知字段缺陷（metrics 的 symbol 全空、日期只剩日号；
   fundingRate 的 symbol 带类别后缀；spot 5m/1h 清单混了别的周期）。
   本工具**以 URL 路径为准**重新解析出 市场/类别/币种/周期/起止日，并与清单字段交叉核验。
2. **来源身份统一**：`futures` 与 `futures-um` 视为同一身份（U 本位），规范名 `futures`。
   这样首批 18 份不必搬动、也不会被重复下载。
3. **消费前校验**：symbol 非空、日期完整合法、路径字段与 URL 匹配、状态键唯一。
   **两条不同 URL 映射到同一键 = 冲突**，该分片整组剔除并报告，不静默去重。
4. **排序**：按 (类别, 周期, 市场, 报价, 起始日, 币种) —— 保证**按时间片轮转所有币种**，
   不会只把字母表前几个币下载齐。
5. **连续采集**：分块自动继续，限流后按保存时间等待并自动探测恢复，
   磁盘低于保留量则停下报告，不删除已存资料。

用法：
  py -3.10 tools/archive_queue_ds.py build --part "reports/data_inventory_glm/remote_manifest_part_*.csv"
  py -3.10 tools/archive_queue_ds.py run --chunk 5000 --max-minutes 20 --min-free-gib 20
  py -3.10 tools/archive_queue_ds.py status
"""
from __future__ import annotations

import argparse
import calendar
import csv
import glob
import json
import logging
import os
import re
import shutil
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import archive_download_ds as ad  # noqa: E402

OUT_DIR = ROOT / "data" / "archive_raw"
REPORT_DIR = ROOT / "reports" / "archive_download_ds_full"
QUEUE_CSV = REPORT_DIR / "queue.csv"
CONFLICT_CSV = REPORT_DIR / "conflicts.csv"
BUILD_JSON = REPORT_DIR / "build_summary.json"
PROGRESS_JSON = REPORT_DIR / "progress.json"
RUN_LOG = REPORT_DIR / "run.log"
PID_FILE = REPORT_DIR / "download.pid"
RAMP_JSON = REPORT_DIR / "ramp.json"          # 逐档档位（跨重启保持）
BLOCKS_JSONL = REPORT_DIR / "blocks.jsonl"    # 每块实测指标（供报告引用）


def paths_for(version: str) -> dict[str, Path]:
    """版本化交付路径。`v1` 用原名（兼容旧产物），其余加 `_<版本>` 后缀。"""
    if version in ("", "v1"):
        return {"queue": QUEUE_CSV, "conflicts": CONFLICT_CSV,
                "build": BUILD_JSON, "progress": PROGRESS_JSON,
                "log": RUN_LOG, "pid": PID_FILE,
                "ramp": RAMP_JSON, "blocks": BLOCKS_JSONL}
    return {"queue": REPORT_DIR / f"queue_{version}.csv",
            "conflicts": REPORT_DIR / f"conflicts_{version}.csv",
            "build": REPORT_DIR / f"build_summary_{version}.json",
            "progress": REPORT_DIR / f"progress_{version}.json",
            "log": REPORT_DIR / f"run_{version}.log",
            "pid": REPORT_DIR / f"download_{version}.pid",
            "ramp": REPORT_DIR / f"ramp_{version}.json",
            "blocks": REPORT_DIR / f"blocks_{version}.jsonl"}


# ---------------------------------------------------------------------------
# 逐档提速（**块边界换档**，不重启进程）
# ---------------------------------------------------------------------------
# 档位是**本轮运行参数**，不是来源额度声明。50/75/100 请求/秒都是本地测试上限。
RAMP_TIERS: list[dict[str, Any]] = [
    {"name": "A", "workers": 8,  "interval_sec": 0.02,   "rps": 50},
    {"name": "B", "workers": 16, "interval_sec": 0.02,   "rps": 50},
    {"name": "C", "workers": 16, "interval_sec": 0.0134, "rps": 75},
    {"name": "D", "workers": 32, "interval_sec": 0.01,   "rps": 100},
]
TIER_INDEX: dict[str, int] = {t["name"]: i for i, t in enumerate(RAMP_TIERS)}


def stop_path(out_dir: Path) -> Path:
    """停止哨兵文件路径（与下载锁同目录，便于一起清理）。"""
    return Path(out_dir) / ad.STOP_NAME


def stop_requested(out_dir: Path, since: float | None = None) -> bool:
    """是否收到「请求停止」。

    `since` 之前的哨兵视为**陈旧**（上次异常退出遗留），不算数——
    否则一次强杀留下的哨兵会把下一次启动立刻打死。
    """
    try:
        st = stop_path(out_dir).stat()
    except OSError:
        return False
    if since is not None and st.st_mtime < since - 1.0:
        return False
    return True


def clear_stop(out_dir: Path) -> bool:
    """消费（删除）哨兵；返回是否真的删掉了。"""
    try:
        stop_path(out_dir).unlink()
        return True
    except OSError:
        return False


def wait_with_stop(out_dir: Path, since: float, seconds: float,
                   step: float = 10.0) -> bool:
    """**可被停止哨兵打断**的等待；返回 True 表示收到停止请求。

    长暂停期间只轮询一个本地文件，不做任何网络探测。
    """
    end = time.perf_counter() + max(0.0, float(seconds))
    while True:
        if stop_requested(out_dir, since):
            return True
        left = end - time.perf_counter()
        if left <= 0:
            return False
        time.sleep(max(0.05, min(step, left)))


class RampController:
    """块边界逐档提速 + 平台期判定。

    判定口径（任务书第 2 节）：
      * 每档用一个**完整块**的实测吞吐判断；吞吐用**含等待的墙钟**做分母。
      * 上探档比基线档改善不足 `min_improve`（约 10%），**连续 `plateau_windows`
        个窗口**都如此 → 视为平台期，**退回较低档并锁定**，不再继续加线程。
      * 资料类别变化 → 保守重判：退回最低档重来（不把 metrics 的高并发硬套大 K 线）。
      * 出错 / 被限流暂停的块（`ok=False`）**不参与判定**——否则「休息了几小时」
        会被误读成「这一档变慢了」。
    """

    def __init__(self, tiers: list[dict[str, Any]] | None = None, level: int = 0,
                 locked: bool = False, baseline: float | None = None,
                 category: str = "", plateau: int = 0, samples: int = 0,
                 min_improve: float = 0.10, plateau_windows: int = 2):
        self.tiers = list(tiers or RAMP_TIERS)
        self.level = max(0, min(int(level or 0), len(self.tiers) - 1))
        self.locked = bool(locked)
        self.baseline = baseline
        self.category = category or ""
        self.plateau = int(plateau or 0)
        self.samples = int(samples or 0)
        self.min_improve = float(min_improve)
        self.plateau_windows = int(plateau_windows)

    @property
    def tier(self) -> dict[str, Any]:
        return self.tiers[self.level]

    def to_dict(self) -> dict[str, Any]:
        return {"level": self.level, "locked": self.locked,
                "baseline": self.baseline, "category": self.category,
                "plateau": self.plateau, "samples": self.samples}

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None, **kw: Any) -> "RampController":
        raw = raw or {}
        return cls(level=int(raw.get("level", 0) or 0),
                   locked=bool(raw.get("locked", False)),
                   baseline=raw.get("baseline"), category=str(raw.get("category") or ""),
                   plateau=int(raw.get("plateau", 0) or 0),
                   samples=int(raw.get("samples", 0) or 0), **kw)

    def observe(self, rate: float | None, category: str = "",
                ok: bool = True) -> dict[str, Any]:
        """喂入一个块的实测吞吐；返回判定 `{"action", ...}`，action ∈ hold/up/revert/reset。"""
        top = len(self.tiers) - 1
        if not ok:
            return {"action": "hold", "reason": "本块有错误/暂停，不参与判定"}
        if category and self.category and category != self.category:
            prev = self.tier["name"]
            self.level, self.locked, self.baseline = 0, False, None
            self.plateau, self.samples = 0, 0
            self.category = category
            return {"action": "reset", "from": prev, "to": self.tier["name"],
                    "reason": f"资料类别 {prev}→{category}，保守重判回最低档"}
        if category:
            self.category = category
        if rate is None or rate <= 0:
            return {"action": "hold", "reason": "本块无有效吞吐"}

        if self.baseline is None:
            # 本档第一个干净样本 → 记为基线，并上探一档
            self.baseline = float(rate)
            self.samples = 1
            if self.locked or self.level >= top:
                self.locked = True
                return {"action": "hold", "reason": "已锁定或已到最高档"}
            frm = self.tier["name"]
            self.level += 1
            return {"action": "up", "from": frm, "to": self.tier["name"],
                    "baseline": self.baseline}

        # 本档是「上探档」：与基线比较
        self.samples += 1
        improve = (float(rate) - self.baseline) / self.baseline if self.baseline else 1.0
        if improve >= self.min_improve:
            self.plateau = 0
            self.baseline = float(rate)
            if self.locked or self.level >= top:
                self.locked = True
                return {"action": "hold", "improve": round(improve, 4),
                        "reason": "已锁定或已到最高档"}
            frm = self.tier["name"]
            self.level += 1
            return {"action": "up", "from": frm, "to": self.tier["name"],
                    "improve": round(improve, 4)}
        self.plateau += 1
        if self.plateau >= self.plateau_windows:
            frm = self.tier["name"]
            self.level = max(0, self.level - 1)
            self.locked, self.baseline = True, None
            return {"action": "revert", "from": frm, "to": self.tier["name"],
                    "improve": round(improve, 4),
                    "reason": f"连续 {self.plateau} 窗改善不足 "
                              f"{self.min_improve:.0%}，视为平台期"}
        return {"action": "hold", "improve": round(improve, 4),
                "reason": f"改善 {improve:.1%} < {self.min_improve:.0%}，待第二窗"}


def process_rss_bytes() -> int:
    """当前进程工作集（RSS，字节）。用 ctypes，不引入新依赖；取不到返回 0。

    ⚠️ 必须显式声明 `argtypes/restype`：`GetCurrentProcess()` 返回的是伪句柄 (-1)，
    不声明的话 ctypes 会按 32 位 int 传递而被截断，调用直接返回 0（实测踩过）。
    """
    try:
        import ctypes
        from ctypes import wintypes

        class _PMC(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        k32.GetCurrentProcess.argtypes = []
        ps = ctypes.WinDLL("psapi", use_last_error=True)
        ps.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PMC),
                                            ctypes.c_uint]
        ps.GetProcessMemoryInfo.restype = ctypes.c_int
        pmc = _PMC()
        pmc.cb = ctypes.sizeof(pmc)
        if ps.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb):
            return int(pmc.WorkingSetSize)
    except Exception:  # noqa: BLE001
        pass
    return 0


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return dict(json.loads(path.read_text(encoding="utf-8")) or {})
    except Exception:  # noqa: BLE001
        return {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    """每块追加一行指标；写入失败不影响采集。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _old_queue_total() -> int:
    """旧（v1）队列分母，用于进度对照。读不到则返回 0。"""
    try:
        return int(json.loads(BUILD_JSON.read_text(encoding="utf-8"))["queue_rows"])
    except Exception:  # noqa: BLE001
        return 0

# 市场规范名：futures-um 与 futures 是同一身份（U 本位）
MARKET_ALIAS = {"futures-um": "futures", "futures": "futures",
                "futures-cm": "futures-cm", "spot": "spot"}

MARKET_RANK = {"futures": 0, "spot": 1, "futures-cm": 2}
DATASET_RANK = {"fundingRate": 0, "metrics": 1, "klines": 2}
INTERVAL_RANK = {"5m": 0, "1m": 0, "1h": 1, "1d": 2}

_RE_SPOT_KLINES = re.compile(
    r"^/data/spot/(monthly|daily)/klines/(?P<sym>[^/]+)/(?P<iv>[^/]+)/(?P<stem>[^/]+)\.zip$")
_RE_FUT_KLINES = re.compile(
    r"^/data/futures/(?P<um>um|cm)/(monthly|daily)/klines/(?P<sym>[^/]+)/"
    r"(?P<iv>[^/]+)/(?P<stem>[^/]+)\.zip$")
_RE_METRICS = re.compile(
    r"^/data/futures/(?P<um>um|cm)/daily/metrics/(?P<sym>[^/]+)/(?P<stem>[^/]+)\.zip$")
_RE_FUNDING = re.compile(
    r"^/data/futures/(?P<um>um|cm)/monthly/fundingRate/(?P<sym>[^/]+)/(?P<stem>[^/]+)\.zip$")

_RE_MONTH = re.compile(r"^\d{4}-\d{2}$")
_RE_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# URL → 真实身份
# ---------------------------------------------------------------------------
@dataclass
class Parsed:
    market: str = ""
    dataset: str = ""
    symbol: str = ""
    interval: str = ""
    period_start: str = ""
    period_end: str = ""
    problem: str = ""


def _month_bounds(period: str) -> tuple[str, str] | None:
    y, m = int(period[:4]), int(period[5:7])
    if not 1 <= m <= 12:
        return None
    last = calendar.monthrange(y, m)[1]
    return f"{y:04d}-{m:02d}-01", f"{y:04d}-{m:02d}-{last:02d}"


def _day_bounds(period: str) -> tuple[str, str] | None:
    try:
        datetime.strptime(period, "%Y-%m-%d")
    except ValueError:
        return None
    return period, period


# 文件名形态按数据类型分别匹配 —— 日期自身含 `-`，不能用 rsplit 切分
_RE_STEM_KLINES = re.compile(
    r"^(?P<sym>.+)-(?P<iv>[0-9]+[mhdw])-(?P<period>\d{4}-\d{2}(?:-\d{2})?)$")
_RE_STEM_METRICS = re.compile(
    r"^(?P<sym>.+)-metrics-(?P<period>\d{4}-\d{2}-\d{2})$")
_RE_STEM_FUNDING = re.compile(
    r"^(?P<sym>.+)-fundingRate-(?P<period>\d{4}-\d{2})$")


def _match_stem(pattern: re.Pattern, stem: str) -> tuple[str, str, str] | None:
    m = pattern.match(stem)
    if not m:
        return None
    return m.group("sym"), m.groupdict().get("iv", ""), m.group("period")


def parse_source_url(url: str) -> Parsed:
    """只认已核实的官方归档 URL 形态；其余一律记为 problem，不猜。"""
    from urllib.parse import urlsplit
    path = urlsplit(str(url or "")).path

    m = _RE_FUNDING.match(path)
    if m:
        stem = _match_stem(_RE_STEM_FUNDING, m.group("stem"))
        if not stem:
            return Parsed(problem="fundingRate 文件名不符")
        sym, _, period = stem
        bounds = _month_bounds(period) if _RE_MONTH.match(period) else None
        if not bounds:
            return Parsed(problem=f"fundingRate 周期无法解析：{period}")
        if sym != m.group("sym"):
            return Parsed(problem=f"路径币种({m.group('sym')}) 与文件名币种({sym}) 不一致")
        return Parsed(market=f"futures-{m.group('um')}", dataset="fundingRate",
                      symbol=sym, interval="",
                      period_start=bounds[0], period_end=bounds[1])

    m = _RE_METRICS.match(path)
    if m:
        stem = _match_stem(_RE_STEM_METRICS, m.group("stem"))
        if not stem:
            return Parsed(problem="metrics 文件名不符")
        sym, _, period = stem
        bounds = _day_bounds(period) if _RE_DAY.match(period) else None
        if not bounds:
            return Parsed(problem=f"metrics 日期无法解析：{period}")
        if sym != m.group("sym"):
            return Parsed(problem=f"路径币种({m.group('sym')}) 与文件名币种({sym}) 不一致")
        return Parsed(market=f"futures-{m.group('um')}", dataset="metrics",
                      symbol=sym, interval="",
                      period_start=bounds[0], period_end=bounds[1])

    for regex, market_prefix in ((_RE_FUT_KLINES, "futures"), (_RE_SPOT_KLINES, "spot")):
        m = regex.match(path)
        if not m:
            continue
        stem = _match_stem(_RE_STEM_KLINES, m.group("stem"))
        if not stem:
            return Parsed(problem="K 线文件名不符")
        sym, iv, period = stem
        if sym != m.group("sym"):
            return Parsed(problem=f"路径币种({m.group('sym')}) 与文件名币种({sym}) 不一致")
        if iv != m.group("iv"):
            return Parsed(problem=f"路径周期({m.group('iv')}) 与文件名周期({iv}) 不一致")
        bounds = _month_bounds(period) if _RE_MONTH.match(period) else (
            _day_bounds(period) if _RE_DAY.match(period) else None)
        if not bounds:
            return Parsed(problem=f"K 线周期无法解析：{period}")
        if market_prefix == "futures":
            market = f"futures-{m.group('um')}"
        else:
            market = "spot"
        return Parsed(market=market, dataset="klines", symbol=sym, interval=iv,
                      period_start=bounds[0], period_end=bounds[1])

    return Parsed(problem="无法识别的归档 URL 形态")


def normalize_entry(row: dict[str, Any]) -> tuple[ad.Entry | None, str, list[str]]:
    """返回 (规范化条目, 状态, 提示)。状态 ∈ ok / problem / field_mismatch。"""
    url = str(row.get("source_url", "")).strip()
    parsed = parse_source_url(url)
    if parsed.problem:
        return None, "problem", [parsed.problem]
    if not parsed.symbol:
        return None, "problem", ["symbol 为空"]

    notes: list[str] = []
    market = MARKET_ALIAS.get(parsed.market, parsed.market)
    if str(row.get("market", "")).strip() not in ("", parsed.market, market):
        notes.append(f"清单 market={row.get('market')} 与 URL={parsed.market} 不一致")
    for col, actual in (("dataset", parsed.dataset), ("symbol", parsed.symbol),
                        ("interval", parsed.interval)):
        listed = str(row.get(col, "")).strip()
        if listed and listed != actual:
            notes.append(f"清单 {col}={listed} 与 URL 解析={actual} 不一致")

    size = row.get("remote_size_bytes")
    try:
        size_val = int(float(size)) if str(size).strip() else None
    except (TypeError, ValueError):
        size_val = None

    entry = ad.Entry(source_url=url, market=market, dataset=parsed.dataset,
                     symbol=parsed.symbol, interval=parsed.interval,
                     period_start=parsed.period_start, period_end=parsed.period_end,
                     remote_size_bytes=size_val,
                     checksum_url=str(row.get("checksum_url", "")).strip() or url + ".CHECKSUM",
                     discovered_at_utc=str(row.get("discovered_at_utc", "")).strip())
    return entry, ("field_mismatch" if notes else "ok"), notes


# ---------------------------------------------------------------------------
# 队列构建
# ---------------------------------------------------------------------------
def order_key(e: ad.Entry) -> tuple:
    return (DATASET_RANK.get(e.dataset, 9),
            INTERVAL_RANK.get(e.interval, 9),
            MARKET_RANK.get(e.market, 9),
            0 if e.symbol.endswith("USDT") else 1,
            e.period_start,
            e.symbol)


def build_queue(part_globs: list[str], extra_manifests: list[str] = (),
                version: str = "v1", ready_only: bool = False) -> dict[str, Any]:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    _paths = paths_for(version)
    queue_csv, conflict_csv, build_json = (_paths["queue"], _paths["conflicts"],
                                           _paths["build"])
    rows: list[dict[str, Any]] = []
    files: list[Path] = []
    for pattern in part_globs:
        matched = sorted(Path(p) for p in glob.glob(pattern))
        if ready_only:
            # 只接受带 `.ready` 标记的分片——否则 glob 会把 staging_/audit/index
            # 等非分片 CSV 也吞进来，污染队列。
            # **只过滤 glob 匹配**：显式 `--extra` 是调用方点名要的，不适用该门槛。
            matched = [p for p in matched if Path(str(p) + ".ready").exists()]
        files.extend(matched)
    files.extend(Path(p) for p in extra_manifests)
    for p in files:
        if not p.exists():
            continue
        with p.open(encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                row["_src"] = p.name
                rows.append(row)

    key_urls: dict[str, set[str]] = defaultdict(set)
    key_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_url: dict[str, tuple[ad.Entry, str, list[str]]] = {}
    stats = {"input_rows": len(rows), "ok": 0, "problem": 0, "field_mismatch": 0,
             "duplicate_url": 0}
    problems: list[dict[str, Any]] = []

    for row in rows:
        url = str(row.get("source_url", "")).strip()
        if not url:
            continue
        if url in by_url:
            stats["duplicate_url"] += 1
            continue
        entry, status, notes = normalize_entry(row)
        if entry is None:
            stats["problem"] += 1
            problems.append({"source_url": url, "kind": "problem",
                             "detail": "; ".join(notes), "src": row.get("_src", "")})
            continue
        stats[status] += 1
        if notes:
            problems.append({"source_url": url, "kind": "field_mismatch",
                             "detail": "; ".join(notes), "src": row.get("_src", "")})
        by_url[url] = (entry, status, notes)
        key_urls[entry.key].add(url)
        key_rows[entry.key].append(row)

    # 冲突：同一状态键对应多条**不同** URL → 整组剔除并报告
    conflicts: list[dict[str, Any]] = []
    blocked_keys: set[str] = set()
    for key, urls in key_urls.items():
        if len(urls) > 1:
            blocked_keys.add(key)
            conflicts.append({"key": key, "n_urls": len(urls),
                              "urls": " | ".join(sorted(urls)[:5])})

    entries = [e for url, (e, _, _) in by_url.items() if e.key not in blocked_keys]
    entries.sort(key=order_key)

    with queue_csv.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=ad.FIELDS, extrasaction="ignore")
        w.writeheader()
        for e in entries:
            w.writerow({"source_url": e.source_url, "market": e.market,
                        "dataset": e.dataset, "symbol": e.symbol,
                        "interval": e.interval, "period_start": e.period_start,
                        "period_end": e.period_end,
                        "remote_size_bytes": e.remote_size_bytes or "",
                        "checksum_url": e.checksum_url,
                        "discovered_at_utc": e.discovered_at_utc})
    with conflict_csv.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["key", "n_urls", "urls"])
        w.writeheader()
        for c in conflicts:
            w.writerow(c)

    by_cat: dict[str, int] = defaultdict(int)
    bytes_cat: dict[str, int] = defaultdict(int)
    for e in entries:
        tag = f"{e.market}/{e.dataset}/{e.interval or '-'}"
        by_cat[tag] += 1
        bytes_cat[tag] += e.remote_size_bytes or 0
    summary = {
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "version": version,
        "input_files": [p.name for p in files],
        "stats": stats,
        "queue_rows": len(entries),
        "queue_bytes": sum(e.remote_size_bytes or 0 for e in entries),
        "blocked_keys": len(blocked_keys),
        "conflicts": len(conflicts),
        "by_category": {k: {"files": by_cat[k], "bytes": bytes_cat[k]}
                        for k in sorted(by_cat)},
        "problem_samples": problems[:20],
    }
    build_json.write_text(json.dumps(summary, ensure_ascii=False, indent=1),
                          encoding="utf-8")
    return summary


# ---------------------------------------------------------------------------
# 运行
# ---------------------------------------------------------------------------
def _setup_logging(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(log_path, encoding="utf-8"),
                  logging.StreamHandler(sys.stdout)])


def load_queue(path: Path = QUEUE_CSV) -> list[ad.Entry]:
    return ad.load_manifest(path)


# 本轮**确定性失败**（重试不会变好）：本轮不再重复挑取，避免无人值守时原地空转。
# 网络类 `failed_transient` 不在此列——它可能只是临时抖动，留在队列里下轮自动重试。
_DEFER_STATUSES = (ad.S_NOT_FOUND, ad.S_ANOMALY)

# 完成判定四态
C_DONE = "done"                  # 文件在 + 台账结论明确（verified / 来源无校验）+ 大小相符
C_MISSING = "missing"            # 目标文件不存在 → 需下载
C_NEEDS_VERIFY = "needs_verify"  # 文件在但台账缺结论 → 只补校验，不重下
C_BAD = "bad"                    # 台账判定不符 / 大小与台账不符 → 需重下


def classify_entry(entry: ad.Entry, out_dir: Path, state: ad.State) -> str:
    """**便宜**的四态判定（只 stat + 查台账，不解压、不联网）。

    旧实现只看"文件在不在"，于是「文件在但根本没有台账」「台账判定不符」
    都被当成已完成，缺口永久留存。这里要求**结论明确**才算完成；
    全量 sha256 复核仍由独立 `verify` 命令承担，不放进每块的循环。
    """
    p = out_dir / entry.relpath()
    try:
        exists = p.exists()
    except OSError:
        exists = False
    if not exists:
        # 正式文件不在，但**待复核字节**可能在（校验曾被限流暂停拦下）→
        # 不是"缺文件"：只补一次校验就能转正，**不要重下整份**。
        # 先用台账过滤再 stat：这个函数在启动时要跑满整条队列（百万次），
        # 无条件多一次 stat 会让启动扫描时间翻倍。
        rec0 = state.records.get(entry.key)
        if rec0 and rec0.get("checksum_status") == ad.CHECK_FETCH_FAILED:
            try:
                if ad.pending_path_for(out_dir, entry).exists():
                    return C_NEEDS_VERIFY
            except OSError:
                pass
        return C_MISSING
    rec = state.records.get(entry.key)
    if not rec:
        return C_NEEDS_VERIFY          # 有文件没台账 → 不能算完成
    ck = rec.get("checksum_status")
    if ck == ad.CHECK_MISMATCH:
        return C_BAD
    try:
        size = int(rec.get("bytes") or 0)
        if size and size != p.stat().st_size:
            return C_BAD                # 大小与台账不符 → 坏文件，重下
    except (OSError, TypeError, ValueError):
        return C_NEEDS_VERIFY
    if ck in ad.CHECK_CONCLUSIVE:
        return C_DONE
    return C_NEEDS_VERIFY              # checksum_fetch_failed / n/a


def build_done_set(entries: list[ad.Entry], out_dir: Path,
                   state: ad.State) -> set[str]:
    """**启动时扫一次**得出已完成集，之后按增量维护，不再每块全表 stat。"""
    return {e.key for e in entries
            if classify_entry(e, out_dir, state) == C_DONE}


def _pending(entries: list[ad.Entry], done: set[str],
             exclude: set[str] | frozenset[str] = frozenset()) -> list[ad.Entry]:
    """未完成项 = 不在已完成集、也不在本轮搁置集里的项。

    `exclude` 是本轮已判定为确定性失败的键——**跳过它们才能让窗口向前滑动**，
    否则若某块首项永远失败，`todo[:chunk]` 每轮都取到同一批，采集永远推不到后面的币种。
    """
    return [e for e in entries if e.key not in done and e.key not in exclude]


def auto_compact_every(total: int) -> int:
    """快照整理阈值：整理一次要写**整库**，阈值太小会变成平方级开销。

    38k 条记录的快照已 19 MB；100 万条约 500 MB。若仍按 5000 行整理一次，
    全量采集要写数百 GB。按总量取 1/20（下限 5000），把整理次数压到几十次。
    """
    return max(5000, int(total) // 20)


def run_queue(chunk: int, max_minutes: float, min_free_gib: float,
              interval_sec: float, max_chunks: int = 0,
              max_hours: float = 0.0, workers: int = 1,
              version: str = "v1", compact_every: int = 0,
              ramp: str = "auto", tier: str | None = None,
              inflight_bytes_budget: int = 0) -> int:
    p = paths_for(version)
    _setup_logging(p["log"])
    out_dir = OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    p["pid"].parent.mkdir(parents=True, exist_ok=True)
    p["pid"].write_text(str(os.getpid()), encoding="utf-8")

    # 记下进程启动时刻：早于它的停止哨兵视为陈旧，避免上次强杀遗留的哨兵打死本次启动
    started = time.time()
    if stop_path(out_dir).exists() and not stop_requested(out_dir, started):
        logging.warning("发现启动前遗留的停止哨兵（已过期），清除后继续")
        clear_stop(out_dir)

    entries = load_queue(p["queue"])
    if not entries:
        logging.error("队列为空：先跑 build（版本 %s）", version)
        return 2
    workers = max(1, int(workers or 1))

    # —— 逐档提速：档位从持久化文件继续（**跨重启保持**）——
    auto_ramp = str(ramp or "auto").lower() != "off"
    rc = RampController.from_dict(_read_json(p["ramp"]))
    if tier:
        key = str(tier).strip().upper()
        if key not in TIER_INDEX:
            logging.error("未知档位 %s（可选 %s）", tier, "/".join(TIER_INDEX))
            return 2
        rc.level = TIER_INDEX[key]
        rc.locked = True                       # 显式钉档 → 不再自动上探
        logging.info("档位被显式钉在 %s，关闭自动上探", key)
    if auto_ramp:
        cur_workers = int(rc.tier["workers"])
        cur_interval = float(rc.tier["interval_sec"])
        logging.info("逐档提速：起始档 %s（%d 路 / %.4fs，约 %d 请求/秒）%s",
                     rc.tier["name"], cur_workers, cur_interval, rc.tier.get("rps", 0),
                     "，已锁定" if rc.locked else "")
    else:
        cur_workers, cur_interval = workers, float(interval_sec)
        logging.info("逐档提速已关闭：固定 %d 路 / %.4fs", cur_workers, cur_interval)

    logging.info("队列 %d 项（版本 %s）；chunk=%d 并发=%d 间隔=%.4fs 保留磁盘=%.1f GiB",
                 len(entries), version, chunk, cur_workers, cur_interval, min_free_gib)

    compact = int(compact_every or 0) or auto_compact_every(len(entries))
    opts = ad.Options(out_dir=out_dir, state_path=out_dir / ad.STATE_NAME,
                      status_csv=None, max_files=0, max_bytes=0,
                      max_minutes=max_minutes, min_free_gib=min_free_gib,
                      interval_sec=cur_interval, retries=2, workers=cur_workers,
                      compact_every=compact)
    if inflight_bytes_budget:
        opts.inflight_bytes_budget = int(inflight_bytes_budget)
    logging.info("状态快照整理阈值 compact_every=%d（总 %d 项）；在途字节预算 %.0f MiB",
                 compact, len(entries), opts.inflight_bytes_budget / 2 ** 20)

    # 启动时**扫一次**得出已完成集；之后按增量维护，不再每块全表 stat
    t0 = time.time()
    state = ad.State(opts.state_path, compact_every=compact)
    done = build_done_set(entries, out_dir, state)
    scan_sec = time.time() - t0
    logging.info("启动扫描：已完成 %d / %d（%.1f%%），扫描耗时 %.1f 秒",
                 len(done), len(entries), 100.0 * len(done) / max(1, len(entries)),
                 scan_sec)

    # 完成字节在启动时按台账算一次，之后随下载**增量累加**。
    # 不能每块从 `state.records` 重算：`state` 是本块开始时加载的，
    # 本块刚下的文件不在里面 → 永远少算最后一块（实测偏差约 12%）。
    done_bytes = sum(int((state.records.get(k) or {}).get("bytes") or 0)
                     for k in done)

    old_total = _old_queue_total()
    chunk_no = 0
    total_new = total_bytes = total_requests = 0
    errors: Counter = Counter()
    deferred: set[str] = set()   # 本轮确定性失败项，跳过以保证向前推进
    no_progress = 0              # 连续「零进展」块数，用于兜底防止空转
    pause_events = 0             # 本轮限流暂停次数（含每次等待时长）
    pause_seconds = 0.0
    last_block = {"category": "", "rate": None, "sec": 0.0}
    try:
        while True:
            # —— 停止哨兵：块边界检查，命中即保存退出 ——
            if stop_requested(out_dir, started):
                clear_stop(out_dir)
                logging.info("收到停止请求（哨兵），当前块已完成，保存退出")
                break

            chunk_no += 1
            if max_chunks and chunk_no > max_chunks:
                logging.info("达到 chunk 上限 %d，正常退出", max_chunks)
                break
            if max_hours and (time.time() - started) / 3600.0 >= max_hours:
                logging.info("达到运行时长上限 %.1f 小时，正常退出", max_hours)
                break

            free = shutil.disk_usage(out_dir).free
            if free < min_free_gib * 2 ** 30:
                logging.error("磁盘可用 %.2f GiB 低于保留量 %.1f GiB，暂停采集并保留已存资料",
                              free / 2**30, min_free_gib)
                break

            state = ad.State(opts.state_path, compact_every=compact)  # 取最新暂停状态
            host = ad.host_of(entries[0].source_url)
            if state.is_paused(host):
                until = state.paused_until(host)
                remaining = (until - ad.now_ms()) / 1000.0 + 5.0
                # 上限 1 小时：到点重读状态，兼顾"来源延长了期限"和可打断
                wait = max(5.0, min(remaining, 3600.0))
                pause_events += 1
                logging.warning("%s 处于限流暂停（来源=%s），等待 %.0f 秒后自动探测恢复"
                                "（期间可被停止哨兵打断）",
                                host, state.pause_source(host) or "?", wait)
                t_wait = time.perf_counter()
                interrupted = wait_with_stop(out_dir, started, wait)
                pause_seconds += time.perf_counter() - t_wait
                if interrupted:
                    clear_stop(out_dir)
                    logging.info("等待限流恢复期间收到停止请求，保存退出")
                    break
                continue

            todo = _pending(entries, done, deferred)
            if not todo:
                if deferred:
                    logging.warning("无更多可推进项：本轮 %d 项确定性失败已登记"
                                    "（未找到/内容异常，可下次运行重试）", len(deferred))
                else:
                    logging.info("队列已无未完成项，采集结束")
                break
            batch = todo[:chunk]

            # —— 块边界换档：只改下一块的 Options，不重启进程 ——
            opts.workers = cur_workers
            opts.interval_sec = cur_interval
            ran_tier = rc.tier["name"] if auto_ramp else "fixed"
            ran_workers, ran_interval = cur_workers, cur_interval
            logging.info("第 %d 块：未完成 %d（另 %d 项本轮已搁置），本块 %d；档位=%s %d 路/%.4fs",
                         chunk_no, len(todo), len(deferred), len(batch),
                         ran_tier, ran_workers, ran_interval)
            t_block = time.perf_counter()
            results, summary = ad.run_fetch(batch, opts)
            block_sec = time.perf_counter() - t_block
            block_requests = int(getattr(opts, "last_request_count", 0) or 0)
            total_requests += block_requests
            logging.info("第 %d 块结果（%.0f 秒，%d 请求）：%s",
                         chunk_no, block_sec, block_requests, summary)

            block_new = block_bytes = 0
            for r in results:
                st = r["status"]
                if st in (ad.S_SUCCESS, ad.S_COMPLETE):
                    if r["key"] not in done:          # 本轮新完成 → 累加字节
                        done.add(r["key"])
                        done_bytes += int(r.get("bytes") or 0)
                    if st == ad.S_SUCCESS:
                        total_new += 1
                        total_bytes += r["bytes"]
                        block_new += 1
                        block_bytes += int(r["bytes"] or 0)
                else:
                    errors[st] += 1
                    if st in _DEFER_STATUSES:
                        deferred.add(r["key"])

            progressed = sum(1 for r in results
                             if r["status"] in (ad.S_SUCCESS, ad.S_COMPLETE))
            block_errors = sum(1 for r in results
                               if r["status"] not in (ad.S_SUCCESS, ad.S_COMPLETE))
            # ⚠️ 必须按**实际结果状态**判定是否被限流。
            # 不能写 `"限流暂停" in summary`——汇总串里永远含字面量「限流暂停 0」，
            # 那样每一块都会被误判成"被限流"，逐档判定永远拿不到干净样本，
            # 顺带让旧代码的 no_progress 兜底彻底失效。
            paused_block = any(r["status"] == ad.S_PAUSED for r in results)

            # 本块的**主导类别**：用于"资料类别变化则保守重判"
            cat = Counter(f"{e.market}/{e.dataset}/{e.interval or '-'}"
                          for e in batch).most_common(1)
            category = cat[0][0] if cat else ""
            rate = (block_new / block_sec) if block_sec > 0 else 0.0
            bps = (block_bytes / block_sec) if block_sec > 0 else 0.0

            # —— 平台期判定：只在**干净块**上判（无错误、无暂停、有进展）——
            ramp_action: dict[str, Any] = {"action": "off"}
            if auto_ramp:
                clean = (not paused_block) and block_errors == 0 and progressed > 0
                ramp_action = rc.observe(rate, category, ok=clean)
                if ramp_action.get("action") in ("up", "revert", "reset"):
                    logging.info("逐档判定：%s", ramp_action)
                    _write_json(p["ramp"], rc.to_dict())
                    cur_workers = int(rc.tier["workers"])
                    cur_interval = float(rc.tier["interval_sec"])
                    logging.info("下一块档位 → %s（%d 路 / %.4fs）",
                                 rc.tier["name"], cur_workers, cur_interval)
                elif clean:
                    _write_json(p["ramp"], rc.to_dict())

            _append_jsonl(p["blocks"], {
                "chunk": chunk_no, "at_utc": ad.utcnow_iso(),
                "tier": ran_tier,                      # 本块**实际使用**的档位
                "workers": ran_workers, "interval_sec": ran_interval,
                "next_tier": rc.tier["name"] if auto_ramp else "fixed",
                "category": category, "wall_sec": round(block_sec, 2),
                "confirmed": progressed, "new_files": block_new,
                "new_bytes": block_bytes, "requests": block_requests,
                "rate_files_per_sec": round(rate, 3),
                "rate_bytes_per_sec": round(bps, 1),
                "errors": {k: v for k, v in Counter(
                    r["status"] for r in results
                    if r["status"] not in (ad.S_SUCCESS, ad.S_COMPLETE)).items()},
                "paused": paused_block, "ramp": ramp_action,
                "rss_bytes": process_rss_bytes(),
            })

            p["progress"].write_text(json.dumps({
                "updated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "version": version,
                "queue_total": len(entries),          # 新分母
                "old_queue_total": old_total,          # 旧分母（对照）
                "completed": len(done),
                "remaining": len(entries) - len(done),
                "completed_bytes": done_bytes,
                "deferred_this_run": len(deferred),
                "errors_this_run": dict(errors),
                "run_new_files": total_new, "run_new_bytes": total_bytes,
                "run_requests": total_requests,
                "tier": ran_tier,                      # 最近一块实际使用的档位
                "next_tier": rc.tier["name"] if auto_ramp else "fixed",
                "ramp_locked": bool(rc.locked),
                "workers": ran_workers, "interval_sec": ran_interval,
                "last_block_rate": round(rate, 3),
                "last_block_category": category,
                "pause_events": pause_events, "pause_seconds": round(pause_seconds, 1),
                "rss_bytes": process_rss_bytes(),
                "chunks": chunk_no, "elapsed_sec": round(time.time() - started, 1),
                "startup_scan_sec": round(scan_sec, 1),
                "disk_free_gib": round(shutil.disk_usage(out_dir).free / 2**30, 2),
            }, ensure_ascii=False, indent=1), encoding="utf-8")

            if paused_block:
                no_progress = 0
                continue  # 下一轮会按保存时间等待

            # 兜底：连续多块零进展（网络持续失败等）→ 停下报告，不再空转
            if progressed == 0:
                no_progress += 1
                if no_progress >= 3:
                    logging.error("连续 %d 块无任何进展，停止采集以免空转（请检查网络/代理）",
                                  no_progress)
                    break
            else:
                no_progress = 0

            if not batch:
                break
    except KeyboardInterrupt:
        logging.warning("收到中断，已保存进度")
    finally:
        if auto_ramp:
            _write_json(p["ramp"], rc.to_dict())   # 档位跨重启保持
        try:
            p["pid"].unlink()
        except OSError:
            pass
    logging.info("退出：新下 %d 份 / %d 字节 / %d 请求，耗时 %.0f 秒（含暂停 %.0f 秒）；"
                 "错误分布 %s",
                 total_new, total_bytes, total_requests, time.time() - started,
                 pause_seconds, dict(errors))
    return 0


def status(version: str = "v1") -> int:
    """只读统计。

    **先读队列、后读状态**：队列是 280 MB 的 CSV，要读约 30 秒。若先读状态，
    这 30 秒里下载器新落的文件会被随后的扫盘看见、却不在快照里 →
    被误报成 `needs_verify`（实测假阳性可达数百条）。反过来读能把窗口压到几秒。
    """
    p = paths_for(version)
    entries = load_queue(p["queue"]) if p["queue"].exists() else []
    state = ad.State(OUT_DIR / ad.STATE_NAME)
    if not entries:
        print(f"没有队列文件（版本 {version}），先跑 build")
        return 1
    done = build_done_set(entries, OUT_DIR, state)
    kinds = Counter(classify_entry(e, OUT_DIR, state) for e in entries)
    done_bytes = sum(int((state.records.get(k) or {}).get("bytes") or 0) for k in done)
    old = _old_queue_total()
    print(f"版本 {version} | 队列 {len(entries):,}（旧分母 {old:,}）")
    print(f"已完成 {len(done):,} | 剩余 {len(entries) - len(done):,} | "
          f"完成字节 {done_bytes:,}")
    print(f"四态：{dict(kinds)}")
    print(f"磁盘可用 {shutil.disk_usage(OUT_DIR).free / 2**30:.1f} GiB")
    if state.paused:
        for host, info in state.paused.items():
            print(f"暂停中 {host} → {ad.human_ts(info.get('until_ms'))}")
    if p["progress"].exists():
        print("进度快照：", json.loads(p["progress"].read_text(encoding="utf-8")))
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="全量归档队列：规范化 + 分块自动续传")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_build = sub.add_parser("build", help="规范化并生成队列")
    p_build.add_argument("--part", action="append", default=[],
                         help="glm 分片清单 glob，可重复")
    p_build.add_argument("--extra", action="append", default=[],
                         help="额外清单路径，可重复")
    p_build.add_argument("--version", default="v1",
                         help="交付版本名；非 v1 时输出 queue_<版本>.csv 等")
    p_build.add_argument("--ready-only", action="store_true",
                         help="只接受带 .ready 标记的分片（防 staging 等误入）")

    p_run = sub.add_parser("run", help="连续分块采集")
    p_run.add_argument("--chunk", type=int, default=5000)
    p_run.add_argument("--max-minutes", type=float, default=20.0)
    p_run.add_argument("--min-free-gib", type=float, default=20.0)
    p_run.add_argument("--interval-sec", type=float, default=1.0)
    p_run.add_argument("--max-chunks", type=int, default=0)
    p_run.add_argument("--max-hours", type=float, default=0.0)
    p_run.add_argument("--workers", type=int, default=1,
                       help="并发路数；1=单路。多路共享总节奏/预算/暂停与单一状态写入")
    p_run.add_argument("--version", default="v1", help="读哪个版本的队列")
    p_run.add_argument("--compact-every", type=int, default=0,
                       help="状态快照整理阈值；0=按队列总量自动（推荐）")
    p_run.add_argument("--ramp", choices=["auto", "off"], default="auto",
                       help="auto=块边界逐档提速（默认）；off=用 --workers/--interval-sec 固定")
    p_run.add_argument("--tier", default="",
                       help="把档位钉在 A/B/C/D（关闭自动上探）")
    p_run.add_argument("--inflight-mib", type=int, default=0,
                       help="同时在途响应字节预算（MiB）；0=用下载器默认 512")

    p_status = sub.add_parser("status", help="查看进度")
    p_status.add_argument("--version", default="v1")

    p_stop = sub.add_parser("stop", help="请求采集进程在当前块结束后保存退出")
    p_stop.add_argument("--version", default="v1")

    p_clr = sub.add_parser("clear-stop", help="移除停止哨兵（清除误留的停止请求）")
    p_clr.add_argument("--version", default="v1")

    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    if args.cmd == "build":
        parts = args.part or [str(ROOT / "reports" / "data_inventory_glm"
                                  / "remote_manifest_part_*.csv")]
        summary = build_queue(parts, args.extra, version=args.version,
                              ready_only=args.ready_only)
        print(json.dumps({k: summary[k] for k in
                          ("version", "stats", "queue_rows", "queue_bytes",
                           "conflicts")},
                         ensure_ascii=False, indent=1))
        p = paths_for(args.version)
        print(f"队列 -> {p['queue']}\n冲突 -> {p['conflicts']}\n摘要 -> {p['build']}")
        return 0
    if args.cmd == "run":
        return run_queue(args.chunk, args.max_minutes, args.min_free_gib,
                         args.interval_sec, args.max_chunks, args.max_hours,
                         workers=args.workers, version=args.version,
                         compact_every=args.compact_every,
                         ramp=args.ramp, tier=args.tier or None,
                         inflight_bytes_budget=int(args.inflight_mib) * 2 ** 20)
    if args.cmd == "stop":
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        sp = stop_path(OUT_DIR)
        sp.write_text(json.dumps({"requested_at_utc": ad.utcnow_iso(),
                                  "by": "archive_queue_ds stop"}, ensure_ascii=False),
                      encoding="utf-8")
        print(f"已创建停止哨兵 {sp}")
        print("运行中的采集进程会在**当前块结束后**保存状态并正常退出。")
        print("若没有进程在跑，请用 clear-stop 清除，避免下次启动被误停。")
        return 0
    if args.cmd == "clear-stop":
        removed = clear_stop(OUT_DIR)
        print(f"{'已删除' if removed else '未发现'}停止哨兵 {stop_path(OUT_DIR)}")
        return 0
    return status(args.version)


if __name__ == "__main__":
    raise SystemExit(main())
