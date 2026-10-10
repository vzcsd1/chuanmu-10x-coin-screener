#!/usr/bin/env python3
"""历史双向研究（ds，2026-10-09）——原版完整历史对照 + 候选压缩诊断。

依据：tasks/ds_history_research_20261009.md、OBSERVATION_PLAN.md 第 14 节、
STRATEGY_REVIEW.md 第 9 节。执行参数冻结在 reports/history_research_ds/experiment_card.md。

硬边界（违反即失败）：
  · 不联网：运行期安装 socket 守卫，任何外连直接抛错并计数
  · 不改生产评分：只 import binance_box_strategy 的 WEIGHTS/Config/indicators/box_score/deriv_score
  · 不写生产文件、不覆盖旧成果：只写 reports/history_research_ds/
  · 不重启归档、不动 results/rate_limit_state.json
  · 事件库独立于评分；评分入口接触不到事件表与未来价格

运行（PowerShell，项目根目录）：
  py -3.10 -u tools/history_research_ds.py all
  py -3.10 -u tools/history_research_ds.py universe
  py -3.10 -u tools/history_research_ds.py events
  py -3.10 -u tools/history_research_ds.py baseline          # 续算：已完成对象跳过
  py -3.10 -u tools/history_research_ds.py reverse
  py -3.10 -u tools/history_research_ds.py attention
  py -3.10 -u tools/history_research_ds.py integrity
  py -3.10 -u tools/history_research_ds.py forward       # 信号后果（5m 参考价优先）
  py -3.10 -u tools/history_research_ds.py retention     # 保留刻度/重复提醒/占用时间
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
import zipfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import b4_replay_ds as R            # noqa: E402  已验收接入：取点/缺失/延迟语义唯一真源
import binance_box_strategy as B    # noqa: E402  生产评分：只读引用

ARCHIVE = R.ARCHIVE
OUT = ROOT / "reports" / "history_research_ds"
SHARDS = OUT / "shards"
EV_DIR = OUT / "events"
SIG_DIR = OUT / "signals"


def shards_dir(lag_hours: int):
    """逐币分片按 lag 分子目录隔离；否则 lag=1 会静默复用 lag=0 的结果。"""
    return SHARDS / f"lag{int(lag_hours)}"


def signals_dir(lag_hours: int):
    return SIG_DIR / f"lag{int(lag_hours)}"
CACHE_DIR = OUT / "cache"

CFG = R.CFG
INTERVAL_MS = R.INTERVAL_MS
LOOKBACK = CFG.lookback
BOX = CFG.box_period
ATR_P = CFG.atr_period
EMA_P = CFG.ema_period
VOLMUL = CFG.volume_multiplier
MIN_SCORE = CFG.min_score
W = dict(R.WEIGHTS)
MAX_SCORE = R.MAX_SCORE
A_EMA = 2.0 / (EMA_P + 1.0)
DERIV_LOOKBACK = R.DERIV_LOOKBACK
DERIV_MIN_OI_OBS = R.DERIV_MIN_OI_OBS
OI_1D_TH = B.OI_1D_THRESHOLD
OI_3D_TH = B.OI_3D_THRESHOLD
LS_TOP_TH = B.LS_TOP_THRESHOLD

MAIN_START = "2022-01-01T00:00:00Z"
MAIN_END = "2026-09-30T23:00:00Z"
MAIN_START_MS = R.parse_utc(MAIN_START)
MAIN_END_MS = R.parse_utc(MAIN_END)
N_HOURS = (MAIN_END_MS - MAIN_START_MS) // INTERVAL_MS + 1
GRID_MS = MAIN_START_MS + np.arange(N_HOURS, dtype=np.int64) * INTERVAL_MS

EARLIER_START = "2020-10-01T00:00:00Z"     # 附表用（不做主比较）
EXPLORE_END = "2024-12-31T23:00:00Z"       # 探索段
REVIEW_START = "2025-01-01T00:00:00Z"      # 后段历史复核

WINDOWS = {"7d": 168, "30d": 720, "90d": 2160}
EVENT_MERGE_GAP_H = 48

EVENT_TIERS = (10.0, 5.0, 3.0, 2.5, 2.0)
HOUR_MS = INTERVAL_MS

# 状态编码（compact shard）
ST_SELECTED = 0
ST_BELOW = 1
ST_SKIP_SHORT = 2
ST_SKIP_GAP = 3
ST_SKIP_NODATA = 4
ST_NOT_OBSERVABLE = 5
STATUS_NAMES = {
    ST_SELECTED: "selected",
    ST_BELOW: "scored_below_threshold",
    ST_SKIP_SHORT: "skipped:kline_short_history",
    ST_SKIP_GAP: "skipped:kline_gap",
    ST_SKIP_NODATA: "skipped:no_kline_data",
    ST_NOT_OBSERVABLE: "not_observable",
}

METRIC_FIELDS = ["create_time", "symbol", "sum_open_interest", "sum_open_interest_value",
                 "count_toptrader_long_short_ratio", "sum_toptrader_long_short_ratio",
                 "count_long_short_ratio", "sum_taker_long_short_vol_ratio"]
EPOCH_ORD = date(1970, 1, 1).toordinal()


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


def _guard() -> R.NetworkGuard:
    """子进程也要装守卫：Windows spawn 不会继承父进程的 patch。"""
    g = R.NetworkGuard()
    g.install()
    return g


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ===========================================================================
# 归档枚举与读取（按币，不一次载入全库）
# ===========================================================================

def usdt_spot_symbols() -> list[str]:
    base = ARCHIVE / "spot" / "klines"
    if not base.is_dir():
        return []
    return sorted(p.name for p in base.iterdir()
                  if p.is_dir() and p.name.endswith("USDT") and (p / "1h").is_dir())


def usdt_futures_symbols() -> list[str]:
    base = ARCHIVE / "futures" / "klines"
    if not base.is_dir():
        return []
    return sorted(p.name for p in base.iterdir()
                  if p.is_dir() and p.name.endswith("USDT") and (p / "1h").is_dir())


def usdt_metrics_symbols() -> list[str]:
    base = ARCHIVE / "futures" / "metrics"
    if not base.is_dir():
        return []
    return sorted(p.name for p in base.iterdir()
                  if p.is_dir() and p.name.endswith("USDT") and (p / "no_interval").is_dir())


def spot_months(symbol: str) -> list[str]:
    d = ARCHIVE / "spot" / "klines" / symbol / "1h"
    if not d.is_dir():
        return []
    out = sorted({m.group(1) for p in d.glob("*.zip")
                  if (m := re.search(r"(\d{4}-\d{2})", p.name))})
    return out


def metrics_days(symbol: str) -> list[str]:
    d = ARCHIVE / "futures" / "metrics" / symbol / "no_interval"
    if not d.is_dir():
        return []
    return sorted({m.group(1) for p in d.glob("*.zip")
                   if (m := re.search(r"(\d{4}-\d{2}-\d{2})", p.name))})


def month_list(a: str, b: str) -> list[str]:
    cur = datetime.strptime(a, "%Y-%m").date()
    end = datetime.strptime(b, "%Y-%m").date()
    out = []
    while cur <= end:
        out.append(f"{cur.year:04d}-{cur.month:02d}")
        cur = date(cur.year + (1 if cur.month == 12 else 0), 1 if cur.month == 12 else cur.month + 1, 1)
    return out


def day_list(a: str, b: str) -> list[str]:
    cur = datetime.strptime(a, "%Y-%m-%d").date()
    end = datetime.strptime(b, "%Y-%m-%d").date()
    out = []
    while cur <= end:
        out.append(cur.strftime("%Y-%m-%d"))
        cur = date.fromordinal(cur.toordinal() + 1)
    return out


def spot_months_interval(symbol: str, interval: str) -> list[str]:
    """任意周期的现货月分片清单（`read_spot` 只服务 1h，这里是 5m 等周期用）。"""
    d = ARCHIVE / "spot" / "klines" / symbol / interval
    if not d.is_dir():
        return []
    return sorted({m.group(1) for p in d.glob("*.zip")
                   if (m := re.search(r"(\d{4}-\d{2})", p.name))})


def read_spot_interval(symbol: str, interval: str, months: list[str]) -> np.ndarray:
    """读现货任意周期月分片 → (N,6) float64 [ts,o,h,l,c,v]，按 ts 升序。

    与 `read_spot` 同一实现，只把周期参数化；用于信号后果的 5 分钟参考价与路径。
    """
    rows: list[tuple] = []
    base = ARCHIVE / "spot" / "klines" / symbol / interval
    for ym in months:
        p = base / f"{symbol}-{interval}-{ym}.zip"
        if not p.exists():
            continue
        try:
            with zipfile.ZipFile(p) as zf:
                names = [n for n in zf.namelist() if n.endswith(".csv")]
                if not names:
                    continue
                text = zf.read(names[0]).decode("utf-8", "replace")
        except (zipfile.BadZipFile, OSError):
            continue
        for ln in text.splitlines():
            if not ln or len(ln) < 12:
                continue
            parts = ln.split(",")
            if len(parts) < 6:
                continue
            ts = R.normalize_epoch_ms(parts[0])
            if ts is None:
                continue
            try:
                rows.append((ts, float(parts[1]), float(parts[2]), float(parts[3]),
                             float(parts[4]), float(parts[5])))
            except ValueError:
                continue
    if not rows:
        return np.empty((0, 6), dtype=np.float64)
    arr = np.asarray(rows, dtype=np.float64)
    arr = arr[np.argsort(arr[:, 0], kind="stable")]
    _, keep = np.unique(arr[:, 0], return_index=True)
    return arr[np.sort(keep)]


def read_spot(symbol: str, months: list[str]) -> np.ndarray:
    """读现货 1h 月分片 → (N,6) float64，列为 [ts,o,h,l,c,v]，按 ts 升序。"""
    rows: list[tuple] = []
    base = ARCHIVE / "spot" / "klines" / symbol / "1h"
    for ym in months:
        p = base / f"{symbol}-1h-{ym}.zip"
        if not p.exists():
            continue
        try:
            with zipfile.ZipFile(p) as zf:
                names = [n for n in zf.namelist() if n.endswith(".csv")]
                if not names:
                    continue
                text = zf.read(names[0]).decode("utf-8", "replace")
        except (zipfile.BadZipFile, OSError):
            continue
        for ln in text.splitlines():
            if not ln or len(ln) < 12:
                continue
            parts = ln.split(",")
            if len(parts) < 6:
                continue
            ts = R.normalize_epoch_ms(parts[0])
            if ts is None:
                continue
            try:
                rows.append((ts, float(parts[1]), float(parts[2]), float(parts[3]),
                             float(parts[4]), float(parts[5])))
            except ValueError:
                continue
    if not rows:
        return np.empty((0, 6), dtype=np.float64)
    arr = np.asarray(rows, dtype=np.float64)
    arr = arr[np.argsort(arr[:, 0], kind="stable")]
    _, keep = np.unique(arr[:, 0], return_index=True)
    return arr[np.sort(keep)]


def _ms_from_date_str(s: str) -> int | None:
    """'YYYY-MM-DD HH:MM:SS' → epoch ms（UTC）。固定宽度，手工解析，避免 pandas 开销。"""
    if len(s) < 19 or s[4] != "-" or s[10] != " ":
        return None
    try:
        y = int(s[0:4]); mo = int(s[5:7]); d = int(s[8:10])
        hh = int(s[11:13]); mi = int(s[14:16]); ss = int(s[17:19])
    except ValueError:
        return None
    return ((date(y, mo, d).toordinal() - EPOCH_ORD) * 86400 + hh * 3600 + mi * 60 + ss) * 1000


def read_metrics_hourly(symbol: str, days: list[str]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """只取整点行（minute==0 且 second==0）。返回 (dt_ms, oi_value, ls_top)。"""
    base = ARCHIVE / "futures" / "metrics" / symbol / "no_interval"
    dts: list[int] = []
    ois: list[float] = []
    lss: list[float] = []
    for day in days:
        p = base / f"{symbol}-metrics-{day}.zip"
        if not p.exists():
            continue
        try:
            with zipfile.ZipFile(p) as zf:
                names = [n for n in zf.namelist() if n.endswith(".csv")]
                if not names:
                    continue
                text = zf.read(names[0]).decode("utf-8", "replace")
        except (zipfile.BadZipFile, OSError):
            continue
        lines = text.split("\n")
        if not lines:
            continue
        idx = {name: i for i, name in enumerate(lines[0].split(","))}
        if "create_time" not in idx:
            idx = {name: i for i, name in enumerate(METRIC_FIELDS)}
        ci = idx.get("create_time", 0)
        oi_i = idx.get("sum_open_interest_value")
        ls_i = idx.get("sum_toptrader_long_short_ratio")
        for ln in lines[1:]:
            if len(ln) < 19 or ln[14:16] != "00" or ln[17:19] != "00":
                continue
            parts = ln.split(",")
            if len(parts) <= max(ci, oi_i if oi_i is not None else 0, ls_i if ls_i is not None else 0):
                continue
            ts = _ms_from_date_str(parts[ci].strip())
            if ts is None:
                continue
            try:
                oi = float(parts[oi_i]) if oi_i is not None else float("nan")
            except (ValueError, IndexError):
                oi = float("nan")
            try:
                ls = float(parts[ls_i]) if ls_i is not None else float("nan")
            except (ValueError, IndexError):
                ls = float("nan")
            dts.append(ts); ois.append(oi); lss.append(ls)
    if not dts:
        return (np.empty(0, dtype=np.int64), np.empty(0), np.empty(0))
    dt = np.asarray(dts, dtype=np.int64)
    oi = np.asarray(ois, dtype=np.float64)
    ls = np.asarray(lss, dtype=np.float64)
    order = np.argsort(dt, kind="stable")
    dt, oi, ls = dt[order], oi[order], ls[order]
    # drop_duplicates(dt, keep='last')
    last = np.ones(len(dt), dtype=bool)
    last[:-1] = dt[1:] != dt[:-1]
    return dt[last], oi[last], ls[last]


# ===========================================================================
# 向量化评估器（与 b4_replay_ds.score_timepoint 逐字段等价，见 tests）
# ===========================================================================

def kline_frame(c: np.ndarray) -> dict:
    """整段 1h K 线一次算指标。因果（shift/rolling/ewm），等价于窗口内重算；EMA 例外见修正。"""
    import pandas as pd
    n = len(c)
    ts = c[:, 0].astype(np.int64)
    h = c[:, 2]; lo = c[:, 3]; cl = c[:, 4]; v = c[:, 5]
    s = pd.Series
    prev_c = s(cl).shift(1)
    tr = pd.concat([s(h) - s(lo), (s(h) - prev_c).abs(), (s(lo) - prev_c).abs()], axis=1).max(axis=1)
    atr = tr.rolling(ATR_P).mean().to_numpy()
    ema_full = s(cl).ewm(span=EMA_P, adjust=False).mean().to_numpy()
    box_high = s(h).shift(1).rolling(BOX).max().to_numpy()
    box_low = s(lo).shift(1).rolling(BOX).min().to_numpy()
    avg_vol = s(v).shift(1).rolling(BOX).mean().to_numpy()
    v24 = s(v).rolling(24).sum().to_numpy()
    prev24 = s(v).shift(24).rolling(24).sum().to_numpy()
    return dict(n=n, ts=ts, h=h, lo=lo, cl=cl, v=v, atr=atr, ema_full=ema_full,
                box_high=box_high, box_low=box_low, avg_vol=avg_vol, v24=v24, prev24=prev24)


def kline_scores(fr: dict, idx: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """对采用点下标 idx 批量算 K 线分。返回 (score(-1=不可算), trend, breakout)。"""
    cl = fr["cl"]
    bh = fr["box_high"][idx]
    bl = fr["box_low"][idx]
    av = fr["avg_vol"][idx]
    at = fr["atr"][idx]
    ema_full = fr["ema_full"]
    start = np.maximum(0, idx - (LOOKBACK - 2))          # 窗口 = 最后 (lookback-1)=239 根
    delta = cl[start] - ema_full[start]
    ema = ema_full[idx] + np.power(1.0 - A_EMA, (idx - start).astype(np.float64)) * delta
    ok = (np.isfinite(bh) & np.isfinite(bl) & np.isfinite(av) & np.isfinite(at)
          & np.isfinite(ema) & np.isfinite(cl[idx]))
    trend = (cl[idx] > ema) & ok
    breakout = (cl[idx] > bh) & ok
    v24 = fr["v24"][idx]; p24 = fr["prev24"][idx]
    v24up = np.isfinite(v24) & np.isfinite(p24) & (v24 > p24) & ok
    score = (W["c_trend"] * trend + W["c_breakout"] * breakout + W["c_v24up"] * v24up).astype(np.int64)
    score = np.where(ok, score, -1)
    return score, trend, breakout


def deriv_scores(dt: np.ndarray, oi: np.ndarray, ls: np.ndarray,
                 decision_ms: np.ndarray, lag_hours: int = 0) -> dict:
    """合约侧批量。语义与 R.deriv_as_of 逐条对齐（窗口/过滤/≥25/可得截止/合法零）。"""
    d = decision_ms.astype(np.int64)
    N = len(d)
    cutoff = d - lag_hours * INTERVAL_MS
    oi_pos = np.flatnonzero(np.isfinite(oi) & (oi > 0)) if len(oi) else np.empty(0, dtype=np.int64)
    ls_pos = np.flatnonzero(np.isfinite(ls)) if len(ls) else np.empty(0, dtype=np.int64)
    oi_dt = dt[oi_pos] if len(oi_pos) else np.empty(0, dtype=np.int64)
    oi_vals = oi[oi_pos] if len(oi_pos) else np.empty(0)
    ls_vals = ls[ls_pos] if len(ls_pos) else np.empty(0)

    has_obs = np.zeros(N, dtype=bool)
    oi_cnt = np.zeros(N, dtype=np.int64)
    ls_cnt = np.zeros(N, dtype=np.int64)
    oi_chg_1d = np.full(N, np.nan); oi_chg_3d = np.full(N, np.nan)
    ls_top = np.full(N, np.nan)
    ok_oi = np.zeros(N, dtype=bool); ok_ls = np.zeros(N, dtype=bool)
    oi_as_of = np.full(N, -1, dtype=np.int64)
    ls_as_of = np.full(N, -1, dtype=np.int64)

    if len(dt):
        i = np.searchsorted(dt, cutoff, side="right") - 1
        has_obs = i >= 0
        w0 = np.maximum(0, i - (DERIV_LOOKBACK - 1))
        w1 = i
        if len(oi_pos):
            oi_cnt = np.where(has_obs, np.searchsorted(oi_pos, w1, side="right")
                              - np.searchsorted(oi_pos, w0, side="left"), 0)
            cpos = np.searchsorted(oi_pos, w1, side="right") - 1
            cps = np.clip(cpos, 0, len(oi_pos) - 1)
            cur = np.where(cpos >= 0, oi_pos[cps], -1)
            cur = np.where((cur >= w0) & (cur <= w1) & has_obs, cur, -1)
            cur_ts = np.where(cur >= 0, dt[np.where(cur >= 0, cur, 0)], -1)
            cur_oi = np.where(cur >= 0, oi_vals[np.where(cpos >= 0, cps, 0)], np.nan)
            for back_h, out in ((24, oi_chg_1d), (72, oi_chg_3d)):
                base_ts = cur_ts - back_h * INTERVAL_MS
                p = np.searchsorted(oi_dt, base_ts, side="right") - 1
                ps = np.clip(p, 0, len(oi_pos) - 1)
                pidx = np.where(p >= 0, oi_pos[ps], -1)
                got = (pidx >= w0) & (pidx >= 0)
                base_val = np.where(got, oi_vals[np.where(p >= 0, ps, 0)], np.nan)
                out[:] = np.where(got, cur_oi / base_val - 1.0, np.nan)
            sel = (oi_cnt >= DERIV_MIN_OI_OBS) & (cur >= 0)
            ok_oi = sel & np.isfinite(oi_chg_1d) & np.isfinite(oi_chg_3d)
            oi_as_of = np.where(sel, cur_ts, -1)
        if len(ls_pos):
            ls_cnt = np.where(has_obs, np.searchsorted(ls_pos, w1, side="right")
                              - np.searchsorted(ls_pos, w0, side="left"), 0)
            cl = np.searchsorted(ls_pos, w1, side="right") - 1
            cls = np.clip(cl, 0, len(ls_pos) - 1)
            curl = np.where(cl >= 0, ls_pos[cls], -1)
            curl = np.where((curl >= w0) & (curl <= w1) & has_obs, curl, -1)
            got_ls = curl >= 0
            ls_top = np.where(got_ls, ls_vals[np.where(cl >= 0, cls, 0)], np.nan)
            ls_as_of = np.where(got_ls, dt[np.where(got_ls, curl, 0)], -1)
            ok_ls = got_ls

    pts = ((np.isfinite(oi_chg_1d) & (oi_chg_1d >= OI_1D_TH)) * W["oi_chg_1d"]
           + (np.isfinite(oi_chg_3d) & (oi_chg_3d >= OI_3D_TH)) * W["oi_chg_3d"]
           + (np.isfinite(ls_top) & (ls_top < LS_TOP_TH)) * W["ls_top"]).astype(np.int64)
    status = np.where(ok_oi & ok_ls, 0, np.where(ok_oi | ok_ls, 1, 2))   # 0=ok 1=partial 2=failed
    status = np.where(has_obs, status, 2)
    return dict(points=pts, status=status, oi_chg_1d=oi_chg_1d, oi_chg_3d=oi_chg_3d,
                ls_top=ls_top, oi_cnt=oi_cnt, ls_cnt=ls_cnt, oi_as_of=oi_as_of,
                ls_as_of=ls_as_of, has_obs=has_obs)


DERIV_STATUS_NAMES = {0: "ok", 1: "partial", 2: "failed"}


def evaluate(symbol: str, candles: np.ndarray, mdt: np.ndarray, oi: np.ndarray, ls: np.ndarray,
             has_futures: bool, decision_ms: np.ndarray, lag_hours: int = 0) -> dict:
    """对一组决策小时输出紧凑状态。返回 dict of arrays（长度 = len(decision_ms)）。"""
    d = decision_ms.astype(np.int64)
    n = len(d)
    fr = kline_frame(candles) if len(candles) else None
    ts = fr["ts"] if fr else np.empty(0, dtype=np.int64)

    adopted = np.full(n, -1, dtype=np.int64)
    has_candle = np.zeros(n, dtype=bool)
    any_earlier = np.zeros(n, dtype=bool)
    n_closed = np.zeros(n, dtype=np.int64)
    if len(ts):
        target = d - INTERVAL_MS
        pos = np.searchsorted(ts, target)
        pos_safe = np.clip(pos, 0, len(ts) - 1)
        has_candle = (pos < len(ts)) & (ts[pos_safe] == target)
        adopted = np.where(has_candle, pos_safe, -1)
        earlier = np.searchsorted(ts, target, side="right") - 1
        any_earlier = earlier >= 0
        n_closed = np.where(any_earlier, earlier + 1, 0)

    score = np.full(n, -1, dtype=np.int64)
    trend = np.zeros(n, dtype=bool)
    breakout = np.zeros(n, dtype=bool)
    if has_candle.any():
        sc, tr_, br_ = kline_scores(fr, np.clip(adopted, 0, max(len(ts) - 1, 0)))
        score = np.where(has_candle, sc, -1)
        trend = np.where(has_candle, tr_, False)
        breakout = np.where(has_candle, br_, False)

    short = has_candle & (n_closed < (BOX + 3 - 1))
    scored = has_candle & ~short & (score >= 0)
    score = np.where(scored, score, -1)
    trend = np.where(scored, trend, False)
    breakout = np.where(scored, breakout, False)

    if has_futures:
        if len(mdt):
            dv = deriv_scores(mdt, oi, ls, d, lag_hours)
            dpts = dv["points"]; dstat = dv["status"]
            dchg1 = dv["oi_chg_1d"]; dchg3 = dv["oi_chg_3d"]; dlst = dv["ls_top"]
            d_oi_cnt = dv["oi_cnt"]
        else:
            dpts = np.zeros(n, dtype=np.int64); dstat = np.full(n, 2, dtype=np.int8)
            dchg1 = np.full(n, np.nan); dchg3 = np.full(n, np.nan); dlst = np.full(n, np.nan)
            d_oi_cnt = np.zeros(n, dtype=np.int64)
    else:
        dpts = np.zeros(n, dtype=np.int64); dstat = np.full(n, -1, dtype=np.int8)
        dchg1 = np.full(n, np.nan); dchg3 = np.full(n, np.nan); dlst = np.full(n, np.nan)
        d_oi_cnt = np.zeros(n, dtype=np.int64)

    total = np.where(scored, score + dpts, -1)
    selected = scored & (total >= MIN_SCORE)
    status = np.where(selected, ST_SELECTED,
                      np.where(scored, ST_BELOW,
                               np.where(~has_candle,
                                        np.where(any_earlier, ST_SKIP_GAP, ST_SKIP_NODATA),
                                        ST_SKIP_SHORT))).astype(np.int8)
    return dict(status=status, score_kline=score.astype(np.int8),
                score_deriv=dpts.astype(np.int8), score_total=total.astype(np.int8),
                selected=selected, trend=trend, breakout=breakout,
                deriv_status=dstat.astype(np.int8), oi_chg_1d=dchg1, oi_chg_3d=dchg3,
                ls_top=dlst, oi_valid_obs=d_oi_cnt, n_closed=n_closed)


# ===========================================================================
# 独立事件库
# ===========================================================================

def detect_events(candles: np.ndarray) -> list[dict]:
    """从现货 1h 独立重建上涨事件（不依赖评分、不依赖任何未来标签输入给评分）。"""
    import pandas as pd
    n = len(candles)
    if n < 60:
        return []
    ts = candles[:, 0].astype(np.int64)
    high = candles[:, 2]; low = candles[:, 3]; close = candles[:, 4]

    def fwd_max(win: int) -> tuple[np.ndarray, np.ndarray]:
        """返回 (fwd[t]=max(high[t+1..t+win])，窗口不足时用已有数据；mature[t]=窗口是否走满)。

        `min_periods=1`：数据末端不足 win 根时仍给出「已发生的」最高价，
        否则归档最后 30 天永远没有事件。是否走满由 mature 单独记录（未走满=未到期，不算失败）。
        """
        rev = pd.Series(high[::-1])
        A = rev.rolling(win, min_periods=1).max().to_numpy()[::-1]   # A[t] = max(high[t..t+win-1])
        fwd = np.full(n, np.nan)
        if n >= 2:
            fwd[:n - 1] = A[1:]
        mature = np.zeros(n, dtype=bool)
        if n > win:
            mature[:n - win] = True
        return fwd, mature

    m = {}
    mature = {}
    for key, win in WINDOWS.items():
        f, mt = fwd_max(win)
        m[key] = f / close
        mature[key] = mt & np.isfinite(f)

    inwave = np.isfinite(m["30d"]) & (m["30d"] >= 2.0)
    idx = np.flatnonzero(inwave)
    if len(idx) == 0:
        return []
    # 连续游程
    segs = []
    s = idx[0]; prev = idx[0]
    for k in idx[1:]:
        if k - prev > 1:
            segs.append((s, prev)); s = k
        prev = k
    segs.append((s, prev))
    # 段间隔 <= 48h 合并（同时记录本事件由几个原始段合并而来）
    merged = [(segs[0][0], segs[0][1], 1)]
    for a, b in segs[1:]:
        if a - merged[-1][1] <= EVENT_MERGE_GAP_H:
            merged[-1] = (merged[-1][0], b, merged[-1][2] + 1)
        else:
            merged.append((a, b, 1))

    out = []
    for a, b, nseg in merged:
        m30 = m["30d"][a:b + 1]
        k = int(np.nanargmax(m30))
        peak_i = a + k
        mx30 = float(m30[k])
        mx7 = float(np.nanmax(m["7d"][a:b + 1]))
        mx90 = float(np.nanmax(m["90d"][a:b + 1]))
        tier = 2.0
        for t in EVENT_TIERS:
            if mx30 >= t:
                tier = t
                break
        seg_lo = float(np.nanmin(low[a:peak_i + 1])) if peak_i >= a else float(low[a])
        max_dd = float(seg_lo / close[a] - 1.0)
        new_high_after_dd = False
        if peak_i > a:
            run_max = close[a]
            dd = 0.0
            for j in range(a, peak_i + 1):
                run_max = max(run_max, close[j])
                dd = min(dd, close[j] / run_max - 1.0)
                if dd <= -0.20 and high[j] >= run_max * 1.0:
                    new_high_after_dd = True
        out.append({
            "start_ms": int(ts[a]), "end_ms": int(ts[b]),
            "peak_ms": int(ts[peak_i]),
            "max_m30": round(mx30, 4), "max_m7": round(mx7, 4) if np.isfinite(mx7) else None,
            "max_m90": round(mx90, 4) if np.isfinite(mx90) else None,
            "tier": tier, "n_segments": nseg, "span_hours": int(b - a + 1),
            "mature_30d": bool(mature["30d"][a]), "mature_90d": bool(mature["90d"][a]),
            "ref_close": float(close[a]),
            "max_drawdown_to_peak": round(max_dd, 4),
            "new_high_after_20pct_dd": bool(new_high_after_dd),
        })
    return out


def forward_path(candles: np.ndarray, ref_idx: int) -> dict:
    """信号可用之后的前向路径。参考价 = 采用 K 线收盘（1h 近似，不冒充成交价）。

    紧凑编码（每窗 4 元组）：[最高价倍数, 最高收盘倍数, 最大回撤, 是否走满]
    """
    n = len(candles)
    out = {}
    ref_close = float(candles[ref_idx, 4])
    for key, win in WINDOWS.items():
        hi = min(ref_idx + win, n - 1)
        seg = candles[ref_idx + 1:hi + 1]
        if len(seg) == 0:
            out[key] = [None, None, None, 0]
            continue
        mx = float(seg[:, 2].max()); mc = float(seg[:, 4].max()); mn = float(seg[:, 3].min())
        out[key] = [round(mx / ref_close, 4), round(mc / ref_close, 4),
                    round(mn / ref_close - 1.0, 4), int(ref_idx + win <= n - 1)]
    out["ref_close"] = round(ref_close, 8)
    return out


def quote_volume_24h(candles: np.ndarray, ref_idx: int) -> float | None:
    """归档可证的 24h 报价成交额近似 Σ close×volume（最近 24 根）。**不是历史闸门**。"""
    if ref_idx < 23:
        return None
    seg = candles[ref_idx - 23:ref_idx + 1]
    return float(np.sum(seg[:, 4] * seg[:, 5]))


# ===========================================================================
# 覆盖核对
# ===========================================================================

def cmd_universe() -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    spot = usdt_spot_symbols()
    fut = set(usdt_futures_symbols())
    met = set(usdt_metrics_symbols())
    rows = []
    for s in spot:
        mo = spot_months(s)
        dy = metrics_days(s)
        rows.append({
            "symbol": s,
            "spot_1h_months": len(mo),
            "spot_1h_first": mo[0] if mo else None,
            "spot_1h_last": mo[-1] if mo else None,
            "has_futures_market": s in fut,
            "metrics_days": len(dy),
            "metrics_first": dy[0] if dy else None,
            "metrics_last": dy[-1] if dy else None,
        })
    fut_only = sorted(fut - set(spot))
    met_only = sorted(met - set(spot))
    doc = {
        "generated_at_utc": R.ms_to_iso(int(time.time() * 1000)),
        "main_range": {"start": MAIN_START, "end": MAIN_END, "hours": int(N_HOURS)},
        "counts": {
            "spot_usdt_symbols": len(spot),
            "futures_usdt_symbols": len(fut),
            "metrics_usdt_symbols": len(met),
            "same_name_all_three": len(set(spot) & fut & met),
            "spot_with_futures": sum(1 for r in rows if r["has_futures_market"]),
            "spot_without_futures": sum(1 for r in rows if not r["has_futures_market"]),
            "futures_without_spot": len(fut_only),
            "metrics_without_spot": len(met_only),
        },
        "examples_futures_without_spot": fut_only[:30],
        "examples_metrics_without_spot": met_only[:30],
        "unknowns": [
            "历史市值：归档无序列，无法重建当时的市值闸门",
            "当时交易资格：文件存在不等于当时该现货对在交易",
            "公开可得时间：归档未记录分片发布时刻，只有下载时间",
            "更名/换币（KLAY→KAIA 等）：未做身份映射，只表现为序列缺口",
            "metrics 全市场覆盖自 2021-12-01 起（2020 仅 BTCUSDT 等少数）",
        ],
        "symbols": rows,
    }
    (OUT / "coverage.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"universe: {len(spot)} spot / {len(fut)} futures / {len(met)} metrics；"
        f"有合约 {doc['counts']['spot_with_futures']}，无合约 {doc['counts']['spot_without_futures']}")
    return doc


# ===========================================================================
# 事件库（逐币并行）
# ===========================================================================

def _events_worker(sym: str) -> dict:
    g = _guard()
    months = spot_months(sym)
    if not months:
        g.uninstall()
        return {"symbol": sym, "n_events": 0, "rows": 0, "network_blocked": g.blocked}
    candles = read_spot(sym, months)
    evs = detect_events(candles)
    for e in evs:
        e["symbol"] = sym
    EV_DIR.mkdir(parents=True, exist_ok=True)
    with (EV_DIR / f"{sym}.jsonl").open("w", encoding="utf-8") as fh:
        for e in evs:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")
    g.uninstall()
    return {"symbol": sym, "n_events": len(evs), "rows": int(len(candles)),
            "network_blocked": g.blocked,
            "first": R.ms_to_iso(int(candles[0, 0])) if len(candles) else None,
            "last": R.ms_to_iso(int(candles[-1, 0])) if len(candles) else None}


def cmd_events(workers: int = 12) -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    EV_DIR.mkdir(parents=True, exist_ok=True)
    syms = usdt_spot_symbols()
    log(f"events: 开始，{len(syms)} 个对象，workers={workers}")
    t0 = time.perf_counter()
    res = []
    done = 0
    with ProcessPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_events_worker, s): s for s in syms}
        for f in as_completed(futs):
            res.append(f.result())
            done += 1
            if done % 100 == 0:
                log(f"events: {done}/{len(syms)}")
    total = sum(r["n_events"] for r in res)
    summary = {
        "generated_at_utc": R.ms_to_iso(int(time.time() * 1000)),
        "symbols": len(syms), "total_events": total,
        "elapsed_sec": round(time.perf_counter() - t0, 2),
        "merge_gap_hours": EVENT_MERGE_GAP_H,
        "windows_hours": WINDOWS,
        "per_symbol": sorted(res, key=lambda r: -r["n_events"]),
    }
    (OUT / "events_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1),
                                             encoding="utf-8")
    log(f"events: 完成，事件总数 {total}，耗时 {summary['elapsed_sec']}s")
    return summary


# ===========================================================================
# 原版逐时点重放（逐币并行、可续算）
# ===========================================================================

def _baseline_worker(args: tuple) -> dict:
    sym, lag_hours, overwrite = args
    g = _guard()
    sd = shards_dir(lag_hours)
    sd.mkdir(parents=True, exist_ok=True)
    shard = sd / f"{sym}.npz"
    meta = sd / f"{sym}.json"
    if shard.exists() and meta.exists() and not overwrite:
        try:
            m = json.loads(meta.read_text(encoding="utf-8"))
            g.uninstall()
            return {"symbol": sym, "skipped": True, "network_blocked": g.blocked, **m}
        except Exception:  # noqa: BLE001
            pass
    months_all = spot_months(sym)
    if not months_all:
        g.uninstall()
        return {"symbol": sym, "skipped": False, "n_hours": 0, "note": "no_spot_1h",
                "network_blocked": g.blocked}
    months = [m for m in months_all if m >= "2021-11"]
    candles = read_spot(sym, months)
    if len(candles) == 0:
        g.uninstall()
        return {"symbol": sym, "skipped": False, "n_hours": 0, "note": "no_spot_rows",
                "network_blocked": g.blocked}
    ts0 = int(candles[0, 0]); tsN = int(candles[-1, 0])
    h_first = max(0, int(math.ceil((ts0 + INTERVAL_MS - MAIN_START_MS) / INTERVAL_MS)))
    h_last = min(int(N_HOURS) - 1, int((tsN + INTERVAL_MS - MAIN_START_MS) // INTERVAL_MS))
    if h_last < h_first:
        g.uninstall()
        return {"symbol": sym, "skipped": False, "n_hours": 0, "note": "outside_main_range",
                "network_blocked": g.blocked,
                "candle_span": [R.ms_to_iso(ts0), R.ms_to_iso(tsN)]}
    dms = GRID_MS[h_first:h_last + 1]

    has_fut = R.futures_1h_dir(sym).is_dir()
    mdt = oi = ls = None
    m_days = 0
    if has_fut:
        days_all = metrics_days(sym)
        days = [d for d in days_all if d >= "2021-12-20"]
        m_days = len(days)
        mdt, oi, ls = read_metrics_hourly(sym, days)
    res = evaluate(sym, candles, mdt if mdt is not None else np.empty(0, dtype=np.int64),
                   oi if oi is not None else np.empty(0), ls if ls is not None else np.empty(0),
                   has_fut, dms, lag_hours=lag_hours)

    # —— 选中片段 + 前向路径 ——
    sel = res["selected"]
    episodes = []
    if sel.any():
        idxs = np.flatnonzero(sel)
        breaks = np.flatnonzero(np.diff(idxs) > 1)
        starts = np.concatenate([[0], breaks + 1])
        ends = np.concatenate([breaks, [len(idxs) - 1]])
        for a, b in zip(starts, ends):
            s_h = int(idxs[a]); e_h = int(idxs[b])
            s_ms = int(dms[s_h])
            # 采用 K 线下标（ts == s_ms - 1h）
            ref = int(np.searchsorted(candles[:, 0], s_ms - INTERVAL_MS))
            if ref >= len(candles) or int(candles[ref, 0]) != s_ms - INTERVAL_MS:
                continue
            fp = forward_path(candles, ref)
            tot = res["score_total"][s_h:e_h + 1]
            episodes.append({
                "symbol": sym, "start_ms": s_ms, "start_utc": R.ms_to_iso(s_ms),
                "end_utc": R.ms_to_iso(int(dms[e_h])),
                "n_hours": int(e_h - s_h + 1),
                "score_total_max": int(tot.max()), "score_total_min": int(tot.min()),
                "score_kline": int(res["score_kline"][s_h]),
                "score_deriv": int(res["score_deriv"][s_h]),
                "trend": int(res["trend"][s_h]), "breakout": int(res["breakout"][s_h]),
                "deriv_status": (DERIV_STATUS_NAMES.get(int(res["deriv_status"][s_h]), "na")
                                 if int(res["deriv_status"][s_h]) >= 0 else "no_futures"),
                "qv24_approx": quote_volume_24h(candles, ref),
                "fwd": fp,
            })

    SHARDS.mkdir(parents=True, exist_ok=True)
    sigd = signals_dir(lag_hours)
    sigd.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        shard,
        h_first=np.int64(h_first), n_hours=np.int64(len(dms)),
        status=res["status"], score_kline=res["score_kline"],
        score_deriv=res["score_deriv"], score_total=res["score_total"],
        selected=res["selected"], trend=res["trend"], breakout=res["breakout"],
        deriv_status=res["deriv_status"],
    )
    with (sigd / f"{sym}.jsonl").open("w", encoding="utf-8") as fh:
        for ep in episodes:
            fh.write(json.dumps(ep, ensure_ascii=False) + "\n")
    meta_doc = {
        "symbol": sym, "h_first": int(h_first), "n_hours": int(len(dms)),
        "span_start_utc": R.ms_to_iso(int(dms[0])), "span_end_utc": R.ms_to_iso(int(dms[-1])),
        "has_futures_market": bool(has_fut),
        "candles_loaded": int(len(candles)), "metrics_days_read": int(m_days),
        "n_selected_hours": int(sel.sum()), "n_episodes": len(episodes),
        "status_counts": {STATUS_NAMES[k]: int((res["status"] == k).sum()) for k in STATUS_NAMES},
        "deriv_status_counts": {DERIV_STATUS_NAMES.get(k, str(k)): int((res["deriv_status"] == k).sum())
                                for k in sorted(set(res["deriv_status"].tolist()))},
        "lag_hours": lag_hours,
    }
    meta.write_text(json.dumps(meta_doc, ensure_ascii=False, indent=1), encoding="utf-8")
    g.uninstall()
    return {"symbol": sym, "skipped": False, "network_blocked": g.blocked, **meta_doc}


def cmd_baseline(workers: int = 12, lag_hours: int = 0, overwrite: bool = False,
                 limit: int | None = None) -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    SHARDS.mkdir(parents=True, exist_ok=True)
    SIG_DIR.mkdir(parents=True, exist_ok=True)
    syms = usdt_spot_symbols()
    if limit:
        syms = syms[:limit]
    log(f"baseline: 开始，{len(syms)} 个对象，workers={workers}，lag={lag_hours}h，"
        f"主时段 {MAIN_START}~{MAIN_END}")
    t0 = time.perf_counter()
    res = []
    done = 0
    with ProcessPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_baseline_worker, (s, lag_hours, overwrite)): s for s in syms}
        for f in as_completed(futs):
            res.append(f.result())
            done += 1
            if done % 50 == 0 or done == len(syms):
                log(f"baseline: {done}/{len(syms)}  已用 {time.perf_counter()-t0:.0f}s")
    total_hours = sum(r.get("n_hours", 0) for r in res)
    summary = {
        "generated_at_utc": R.ms_to_iso(int(time.time() * 1000)),
        "main_range": {"start": MAIN_START, "end": MAIN_END, "grid_hours": int(N_HOURS)},
        "lag_hours": lag_hours, "workers": workers,
        "symbols": len(syms), "symbols_skipped_reused": sum(1 for r in res if r.get("skipped")),
        "total_symbol_hours": int(total_hours),
        "n_selected_hours": int(sum(r.get("n_selected_hours", 0) for r in res)),
        "n_episodes": int(sum(r.get("n_episodes", 0) for r in res)),
        "elapsed_sec": round(time.perf_counter() - t0, 2),
        "per_symbol": sorted(res, key=lambda r: -r.get("n_hours", 0)),
    }
    (OUT / f"baseline_summary_lag{lag_hours}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"baseline: 完成 {len(syms)} 对象 / {total_hours} 小时格 / "
        f"选中 {summary['n_selected_hours']} 小时 / 片段 {summary['n_episodes']}，"
        f"耗时 {summary['elapsed_sec']}s")
    return summary


# ===========================================================================
# 信号后果（任务书 §4 第 1 组）：参考价 / 等待时间 / 路径事实 / 四维分开
# ===========================================================================
#
# 与 `forward_path()`（逐币分片内联的紧凑版）的关系：
#   · `forward_path` 用**信号小时 1h 收盘**作参考价，只给 4 元组，用于底账内联。
#   · 本模块按冻结契约另算**信号可用之后第一个完整 5 分钟 K 线的开盘价**作参考价，
#     并**按粒度分组**（有 5m 覆盖 → 5m；否则回退下一根 1h 开盘 → 1h），
#     另报**等待时间**与**路径事实**。两者不混称同精度，各自成表。
#
# 「本根不计入未来」沿用事件库同一约定：参考 K 线自己的盘中高点发生在参考价之后，
# 但按冻结口径**不计入**，属**保守**处理（少算一段涨幅，不会凭空造赢家）。

FWD_DIR = OUT / "forward"
FWD_WAIT_MULTS = (1.5, 2.0)      # 等待时间只对这两档报；2× 是事件识别下限，不是入池门槛
FWD_DD_THRESHOLD = -0.20         # 「先跌」的待验证设置，与事件库同一阈值


def forward_dir(lag_hours: int):
    return FWD_DIR / f"lag{int(lag_hours)}"


def forward_from_series(c: np.ndarray, ref_ms: int, windows: dict | None = None) -> dict | None:
    """从一条已排序的 K 线序列算信号后果。`c` 列 = [ts,o,h,l,c,v]。

    参考价 = `open_time >= ref_ms` 的第一根 K 线的**开盘价**（即信号可用后第一根完整 K 线）。
    未来 = **严格在该参考 K 线之后**的 K 线。
    窗口成熟度按参考 K 线之后是否走满 win 小时判定；未走满记 `mature=False`（未到期，不算失败）。
    """
    windows = windows or WINDOWS
    n = len(c)
    if n == 0:
        return None
    i = int(np.searchsorted(c[:, 0], float(ref_ms), side="left"))
    if i >= n:
        return None
    ref_open_ms = float(c[i, 0])
    ref = float(c[i, 1])
    if not (ref > 0) or not np.isfinite(ref):
        return None
    last_ms = float(c[-1, 0])
    out: dict = {
        "ref_price": round(ref, 10),
        "ref_open_utc": R.ms_to_iso(int(ref_open_ms)),
        "ref_lag_min": int((ref_open_ms - float(ref_ms)) // 60_000),
        "last_bar_utc": R.ms_to_iso(int(last_ms)),
    }
    for key, win in windows.items():
        t_end = ref_open_ms + win * INTERVAL_MS
        hi = int(np.searchsorted(c[:, 0], t_end, side="right"))
        seg = c[i + 1:hi]
        mature = bool(last_ms >= t_end)
        if len(seg) == 0:
            out[key] = {"max_touch": None, "max_close": None, "max_dd": None,
                        "mature": mature, "n_bars": 0}
            continue
        out[key] = {
            "max_touch": round(float(seg[:, 2].max()) / ref, 4),
            "max_close": round(float(seg[:, 4].max()) / ref, 4),
            "max_dd": round(float(seg[:, 3].min()) / ref - 1.0, 4),
            "mature": mature, "n_bars": int(len(seg)),
        }
    # —— 等待时间 + 路径事实（只在 30 天主窗内判定）——
    t30 = ref_open_ms + WINDOWS["30d"] * INTERVAL_MS
    hi30 = int(np.searchsorted(c[:, 0], t30, side="right"))
    seg = c[i + 1:hi30]
    waits = {}
    for mult in FWD_WAIT_MULTS:
        hit = np.flatnonzero(seg[:, 2] >= ref * mult) if len(seg) else np.empty(0, dtype=int)
        waits[f"wait_hours_to_{mult}x"] = (
            round(float(seg[int(hit[0]), 0] - ref_open_ms) / INTERVAL_MS, 3) if len(hit) else None)
    out["waits"] = waits
    out["first_2x_hours"] = waits["wait_hours_to_2.0x"]
    # 首次触及 2× 之前是否先跌 ≥20%（用 2× 之前的收盘序列滚动高点算回撤）
    if len(seg) and waits["wait_hours_to_2.0x"] is not None:
        k2 = int(np.flatnonzero(seg[:, 2] >= ref * 2.0)[0])
        pre = seg[:k2 + 1]
        if len(pre) == 0:
            out["dd_before_2x"] = None
        else:
            run = np.maximum.accumulate(pre[:, 4])
            dd = float((pre[:, 4] / run - 1.0).min())
            out["dd_before_2x"] = round(dd, 4)
    else:
        out["dd_before_2x"] = None
    # 四维分开（不混成互斥六类）
    m30 = out.get("30d") or {}
    touch = m30.get("max_touch")
    out["dim"] = {
        "data_state": "complete" if m30.get("n_bars") else "missing",
        "maturity": "mature" if m30.get("mature") else "immature",
        "gain": ("reach_2x" if (touch is not None and touch >= 2.0)
                 else "reach_1_5x_only" if (touch is not None and touch >= 1.5)
                 else "below_1_5x"),
        "path": ("none" if touch is None
                 else "reach_2x_after_dd" if (out["dd_before_2x"] is not None
                                              and out["dd_before_2x"] <= FWD_DD_THRESHOLD)
                 else "reach_2x_no_dd" if touch >= 2.0
                 else "no_2x_dd" if (m30.get("max_dd") is not None
                                     and m30["max_dd"] <= FWD_DD_THRESHOLD)
                 else "no_2x_shallow"),
    }
    return out


def _forward_worker(args: tuple) -> dict:
    sym, lag_hours = args
    g = _guard()
    sig = signals_dir(lag_hours) / f"{sym}.jsonl"
    if not sig.exists():
        g.uninstall()
        return {"symbol": sym, "skipped": True, "n_episodes": 0, "network_blocked": g.blocked}
    eps = [json.loads(ln) for ln in sig.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if not eps:
        g.uninstall()
        return {"symbol": sym, "skipped": False, "n_episodes": 0, "network_blocked": g.blocked}
    months5 = spot_months_interval(sym, "5m")
    c5 = read_spot_interval(sym, "5m", months5) if months5 else np.empty((0, 6), dtype=np.float64)
    c1 = None
    rows = []
    n_5m = n_1h = n_none = 0
    for ep in eps:
        ref_ms = int(ep["start_ms"])
        fp = forward_from_series(c5, ref_ms) if len(c5) else None
        gran = "5m"
        if fp is None:
            if c1 is None:
                c1 = read_spot(sym, spot_months(sym))
            fp = forward_from_series(c1, ref_ms) if len(c1) else None
            gran = "1h"
        if fp is None:
            # 参考根之后**没有任何 K 线**（只可能发生在该对象最后一个决策小时：5m 与 1h
            # 归档都已到头）。这不是失败，而是**无后续可观察** → 如实记 no_reference，
            # 让「片段总数 = 各去向之和」对得上账，不静默丢弃。
            n_none += 1
            rows.append({
                "symbol": sym, "start_utc": ep["start_utc"], "start_ms": ref_ms,
                "granularity": "none", "n_hours": ep["n_hours"],
                "score_total_max": ep["score_total_max"],
                "deriv_status": ep.get("deriv_status"),
                "has_futures": ep.get("deriv_status") not in (None, "no_futures"),
                "ref_price": None, "ref_open_utc": None, "ref_lag_min": None,
                "last_bar_utc": None,
                **{k: {"max_touch": None, "max_close": None, "max_dd": None,
                       "mature": False, "n_bars": 0} for k in WINDOWS},
                "waits": {f"wait_hours_to_{m}x": None for m in FWD_WAIT_MULTS},
                "first_2x_hours": None, "dd_before_2x": None,
                "dim": {"data_state": "no_reference", "maturity": "unknown",
                        "gain": "no_reference", "path": "no_reference"},
            })
            continue
        if gran == "5m":
            n_5m += 1
        else:
            n_1h += 1
        rows.append({
            "symbol": sym, "start_utc": ep["start_utc"], "start_ms": ref_ms,
            "granularity": gran, "n_hours": ep["n_hours"],
            "score_total_max": ep["score_total_max"],
            "deriv_status": ep.get("deriv_status"),
            "has_futures": ep.get("deriv_status") not in (None, "no_futures"),
            **fp,
        })
    d = forward_dir(lag_hours)
    d.mkdir(parents=True, exist_ok=True)
    with (d / f"{sym}.jsonl").open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    g.uninstall()
    return {"symbol": sym, "skipped": False, "n_episodes": len(rows),
            "gran_5m": n_5m, "gran_1h": n_1h, "no_ref": n_none,
            "network_blocked": g.blocked}


def _pct(values: list[float], q: float) -> float | None:
    arr = np.asarray([v for v in values if v is not None and np.isfinite(v)], dtype=float)
    return round(float(np.percentile(arr, q)), 4) if len(arr) else None


def _describe(values: list[float]) -> dict:
    arr = np.asarray([v for v in values if v is not None and np.isfinite(v)], dtype=float)
    if len(arr) == 0:
        return {"n": 0}
    return {"n": int(len(arr)), "mean": round(float(arr.mean()), 4),
            "p25": round(float(np.percentile(arr, 25)), 4),
            "median": round(float(np.median(arr)), 4),
            "p75": round(float(np.percentile(arr, 75)), 4),
            "p90": round(float(np.percentile(arr, 90)), 4),
            "max": round(float(arr.max()), 4)}


def load_forward(lag_hours: int = 0) -> list[dict]:
    out = []
    for p in sorted(forward_dir(lag_hours).glob("*.jsonl")):
        for ln in p.read_text(encoding="utf-8").splitlines():
            if ln.strip():
                out.append(json.loads(ln))
    return out


def cmd_forward(workers: int = 12, lag_hours: int = 0, limit: int | None = None) -> dict:
    """信号后果：每个**选中片段**（首次信号）的参考价、等待时间、路径事实与四维标签。"""
    OUT.mkdir(parents=True, exist_ok=True)
    FWD_DIR.mkdir(parents=True, exist_ok=True)
    syms = usdt_spot_symbols()
    if limit:
        syms = syms[:limit]
    log(f"forward: 开始，{len(syms)} 个对象，workers={workers}，lag={lag_hours}h")
    t0 = time.perf_counter()
    res = []
    done = 0
    with ProcessPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_forward_worker, (s, lag_hours)): s for s in syms}
        for f in as_completed(futs):
            res.append(f.result())
            done += 1
            if done % 50 == 0 or done == len(syms):
                log(f"forward: {done}/{len(syms)}  已用 {time.perf_counter()-t0:.0f}s")
    rows = load_forward(lag_hours)
    summary = summarize_forward(rows)
    summary.update({
        "generated_at_utc": R.ms_to_iso(int(time.time() * 1000)),
        "lag_hours": lag_hours, "workers": workers,
        "symbols": len(syms),
        "symbols_with_episodes": sum(1 for r in res if r.get("n_episodes")),
        "elapsed_sec": round(time.perf_counter() - t0, 2),
        "network_blocked_attempts": int(sum(r.get("network_blocked", 0) for r in res)),
    })
    (OUT / f"forward_summary_lag{lag_hours}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    write_forward_tables(rows, lag_hours)
    log(f"forward: {len(rows)} 片段；达到 2× {summary['outcome']['reach_2x']} / "
        f"仅 1.5× {summary['outcome']['reach_1_5x_only']} / 未及 1.5× {summary['outcome']['below_1_5x']}，"
        f"耗时 {summary['elapsed_sec']}s")
    return summary


def summarize_forward(rows: list[dict]) -> dict:
    """把逐片段结果聚合成「选中后分别怎样」的可读结果。"""
    from collections import Counter
    def bucket(r):
        return r["dim"]["gain"]

    outcome = Counter(bucket(r) for r in rows)
    path = Counter(r["dim"]["path"] for r in rows)
    gran = Counter(r["granularity"] for r in rows)
    data_state = Counter(r["dim"]["data_state"] for r in rows)
    maturity = Counter(r["dim"]["maturity"] for r in rows)

    def split(rs):
        return {
            "n": len(rs),
            "outcome": dict(Counter(bucket(r) for r in rs)),
            "path": dict(Counter(r["dim"]["path"] for r in rs)),
            "max_touch_30d": _describe([(r.get("30d") or {}).get("max_touch") for r in rs]),
            "max_dd_30d": _describe([(r.get("30d") or {}).get("max_dd") for r in rs]),
            "wait_hours_to_2x": _describe([r["waits"].get("wait_hours_to_2.0x") for r in rs]),
            "wait_hours_to_1_5x": _describe([r["waits"].get("wait_hours_to_1.5x") for r in rs]),
        }

    explore = [r for r in rows if r["start_utc"] < REVIEW_START]
    review = [r for r in rows if r["start_utc"] >= REVIEW_START]
    keys = ("reach_2x", "reach_1_5x_only", "below_1_5x", "no_reference")
    outcome_fixed = {k: outcome.get(k, 0) for k in keys}
    return {
        "n_episodes": len(rows),
        "outcome": outcome_fixed,
        # 对账：各去向之和必须等于片段总数（含无后续的 no_reference）
        "outcome_reconciles": sum(outcome_fixed.values()) == len(rows),
        "path_fact": dict(path),
        "data_state": dict(data_state),
        "maturity": dict(maturity),
        "ref_granularity": dict(gran),
        "overall": split(rows),
        "by_granularity": {g: split([r for r in rows if r["granularity"] == g])
                           for g in sorted(gran)},
        "by_segment": {"explore_2022_2024": split(explore), "review_2025_2026": split(review)},
        "by_has_futures": {"with_perp": split([r for r in rows if r.get("has_futures")]),
                           "no_perp": split([r for r in rows if not r.get("has_futures")])},
        "by_score": {f"score_total_max>={s}": split([r for r in rows if r["score_total_max"] >= s])
                     for s in (5, 8, 10, 12)},
        "note": ("参考价 = 信号可用后第一根完整 K 线开盘价；有 5m 覆盖用 5m，否则回退 1h 并分组。"
                 "「本根不计入未来」= 保守口径。最高触及/收盘维持/最大回撤/等待时间分列，"
                 "最高倍数**不是**已实现收益。`no_reference` = 该片段之后归档里再无任何 K 线"
                 "（只发生在对象最后一个决策小时），属**无后续可观察**，不计入上涨/未涨，"
                 "也不当失败；它使「各去向之和 = 片段总数」成立。"),
    }


def write_forward_tables(rows: list[dict], lag_hours: int) -> None:
    """落盘可读表：逐片段明细（CSV）+ 结果交叉表（CSV）。"""
    cols = ["symbol", "start_utc", "granularity", "n_hours", "score_total_max",
            "deriv_status", "ref_price", "ref_open_utc", "ref_lag_min",
            "wait_hours_to_1.5x", "wait_hours_to_2.0x", "dd_before_2x",
            "dim_data_state", "dim_maturity", "dim_gain", "dim_path"]
    for key in ("7d", "30d", "90d"):
        cols += [f"{key}_max_touch", f"{key}_max_close", f"{key}_max_dd", f"{key}_mature"]
    import csv as _csv
    with (OUT / f"forward_detail_lag{lag_hours}.csv").open("w", encoding="utf-8",
                                                           newline="") as fh:
        w = _csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            rec = {c: r.get(c) for c in ("symbol", "start_utc", "granularity", "n_hours",
                                         "score_total_max", "deriv_status", "ref_price",
                                         "ref_open_utc", "ref_lag_min", "dd_before_2x")}
            rec["wait_hours_to_1.5x"] = r["waits"].get("wait_hours_to_1.5x")
            rec["wait_hours_to_2.0x"] = r["waits"].get("wait_hours_to_2.0x")
            rec["dim_data_state"] = r["dim"]["data_state"]
            rec["dim_maturity"] = r["dim"]["maturity"]
            rec["dim_gain"] = r["dim"]["gain"]
            rec["dim_path"] = r["dim"]["path"]
            for key in ("7d", "30d", "90d"):
                blk = r.get(key) or {}
                rec[f"{key}_max_touch"] = blk.get("max_touch")
                rec[f"{key}_max_close"] = blk.get("max_close")
                rec[f"{key}_max_dd"] = blk.get("max_dd")
                rec[f"{key}_mature"] = blk.get("mature")
            w.writerow(rec)
    # 交叉表：观察成熟度 × 涨幅达成 × 路径事实
    from collections import Counter
    cross = Counter((r["dim"]["maturity"], r["dim"]["gain"], r["dim"]["path"]) for r in rows)
    lines = ["maturity,gain,path,n"]
    for (mt, gn, pa), n in sorted(cross.items()):
        lines.append(f"{mt},{gn},{pa},{n}")
    (OUT / f"forward_crosstab_lag{lag_hours}.csv").write_text("\n".join(lines), encoding="utf-8")


# ===========================================================================
# 保留刻度 + 重复提醒 + 占用时间（任务书 §4 第 3 组补充）
# ===========================================================================

def retention_scale(covered: dict, base_n: int, max_rank: int,
                    pcts: tuple = (100, 95, 90, 80)) -> dict:
    """由「名次 <= n 能覆盖多少已捕捉事件」的曲线反算保留刻度。

    `covered[n]` = 原版已捕捉事件中，最好名次 <= n 的事件数。
    返回每个保留比例所需的最小关注数 `required_n`；若在 `max_rank` 内够不到，
    `required_n=None` 且 `unreachable_within_max_rank=True`（**如实报告够不到**，不放大）。
    这些只是完整曲线的**阅读刻度**，不是用户已批准的漏选容忍线。
    """
    scale = {}
    for pct in pcts:
        need = math.ceil(base_n * pct / 100.0)
        hit = next((n for n in range(1, max_rank + 1) if covered.get(n, 0) >= need), None)
        scale[f"{pct}%"] = {"target_events": need, "required_n": hit,
                            "unreachable_within_max_rank": hit is None}
    return scale


def cmd_retention(lag_hours: int = 0, max_rank: int = 60) -> dict:
    """在原版已捕捉事件保留 100%/95%/90%/80% 时各需关注多少对象；
    另报重复提醒次数与片段占用时间。这些只是**阅读刻度**，不是获批漏选容忍线。"""
    from collections import Counter
    mat = load_matrices(lag_hours)
    syms = mat["symbols"]; sidx = {s: j for j, s in enumerate(syms)}
    total = mat["total"]; selected = mat["selected"]
    events = load_events()
    rank = rank_matrix(total)

    def best_rank(e):
        j = sidx.get(e["symbol"])
        if j is None or not mat["present"][j]:
            return None
        t0 = e["start_ms"]
        w0 = max(0, int((t0 - 30 * 86400_000 - MAIN_START_MS) // INTERVAL_MS))
        w1 = min(int(N_HOURS) - 1, int((t0 - MAIN_START_MS) // INTERVAL_MS))
        if w1 < 0 or w0 > w1:
            return None
        m = int(rank[w0:w1 + 1, j].min())
        return m if m < 32767 else None

    # 原版「已捕捉」= 事件起点前 30 天窗口内至少入选过一小时
    captured, uncaptured = [], []
    for e in events:
        j = sidx.get(e["symbol"])
        if j is None or not mat["present"][j]:
            continue
        t0 = e["start_ms"]
        w0 = max(0, int((t0 - 30 * 86400_000 - MAIN_START_MS) // INTERVAL_MS))
        w1 = min(int(N_HOURS) - 1, int((t0 - MAIN_START_MS) // INTERVAL_MS))
        if w1 < 0 or w0 > w1:
            continue
        if selected[w0:w1 + 1, j].any():
            captured.append(e)
        else:
            uncaptured.append(e)
    base_n = len(captured)
    ranks = [best_rank(e) for e in captured]
    covered = {n: sum(1 for m in ranks if m is not None and m <= n) for n in range(1, max_rank + 1)}
    scale = retention_scale(covered, base_n, max_rank)
    # 重复提醒：事件前 30 天窗口内该对象的选中片段数
    eps_by_sym = {}
    for ep in load_signals(lag_hours):
        eps_by_sym.setdefault(ep["symbol"], []).append((ep["start_ms"], ep["n_hours"]))
    reminders = []
    occupancy_h = []
    for e in captured:
        lst = eps_by_sym.get(e["symbol"], [])
        t0 = e["start_ms"]
        lo, hi = t0 - 30 * 86400_000, t0
        cnt = sum(1 for (s, _) in lst if lo <= s <= hi)
        reminders.append(cnt)
    for lst in eps_by_sym.values():
        for (_, nh) in lst:
            occupancy_h.append(nh)
    doc = {
        "generated_at_utc": R.ms_to_iso(int(time.time() * 1000)),
        "lag_hours": lag_hours,
        "max_rank": max_rank,
        "baseline_captured_events": base_n,
        "baseline_uncaptured_events": len(uncaptured),
        "retention_scale": scale,
        "covered_by_n": covered,
        "reminders_per_event_30d": _describe(reminders),
        "episode_occupancy_hours": _describe(occupancy_h),
        "episode_occupancy_days": _describe([h / 24.0 for h in occupancy_h]),
        "note": ("保留刻度只是完整曲线的**阅读刻度**，不是用户已批准的漏选容忍线；"
                 "后段表现可能达不到探索段刻度，须如实报告。"),
    }
    (OUT / f"retention_lag{lag_hours}.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"retention: 原版已捕捉 {base_n} 事件；保留刻度 " +
        " ".join(f"{k}={v['required_n']}" for k, v in scale.items()))
    return doc


# ===========================================================================
# 聚合：矩阵、反向漏选、关注数量曲线、完整性
# ===========================================================================

def load_matrices(lag_hours: int = 0) -> dict:
    """把逐币分片装配成全局矩阵（对齐小时格）。"""
    syms = usdt_spot_symbols()
    S = len(syms)
    total = np.full((int(N_HOURS), S), -1, dtype=np.int8)      # -1 = 不可评分
    selected = np.zeros((int(N_HOURS), S), dtype=bool)
    trend = np.zeros((int(N_HOURS), S), dtype=bool)
    status = np.full((int(N_HOURS), S), ST_NOT_OBSERVABLE, dtype=np.int8)
    present = np.zeros(S, dtype=bool)
    for j, s in enumerate(syms):
        p = shards_dir(lag_hours) / f"{s}.npz"
        if not p.exists():
            continue
        with np.load(p) as z:
            hf = int(z["h_first"]); nh = int(z["n_hours"])
            total[hf:hf + nh, j] = z["score_total"]
            selected[hf:hf + nh, j] = z["selected"]
            trend[hf:hf + nh, j] = z["trend"]
            status[hf:hf + nh, j] = z["status"]
        present[j] = True
    return {"symbols": syms, "total": total, "selected": selected, "trend": trend,
            "status": status, "present": present}


def _rank_rows(vals: np.ndarray, scoreable: np.ndarray) -> np.ndarray:
    """按行算名次（1=最好，稳定排序→并列按列序=symbol 升序）。不可评分记 32767。

    分块处理以限制峰值内存（整表 argsort 会瞬时占用数百 MB）。
    """
    n_rows = vals.shape[0]
    rank = np.empty(vals.shape, dtype=np.int16)
    step = 1024
    for a in range(0, n_rows, step):
        b = min(n_rows, a + step)
        blk = vals[a:b]
        order = np.argsort(-blk, axis=1, kind="stable")
        rk = np.empty(order.shape, dtype=np.int16)
        rows = np.arange(order.shape[0])[:, None]
        rk[rows, order] = np.arange(1, order.shape[1] + 1, dtype=np.int16)[None, :]
        rk[~scoreable[a:b]] = 32767
        rank[a:b] = rk
    return rank


def rank_matrix(total: np.ndarray) -> np.ndarray:
    """每小时横截面名次。分母 = 该小时全部可评分对象（不能先筛低分再排名）。"""
    return _rank_rows(total.astype(np.int16), total >= 0)


def random_rank_matrix(seed: int, total: np.ndarray) -> np.ndarray:
    """固定随机种子的对照排序（每行一个随机排列）。"""
    rng = np.random.default_rng(seed)
    rnd = rng.random(total.shape, dtype=np.float32)
    return _rank_rows(rnd, total >= 0)


def load_events() -> list[dict]:
    out = []
    for p in sorted(EV_DIR.glob("*.jsonl")):
        for ln in p.read_text(encoding="utf-8").splitlines():
            if ln.strip():
                out.append(json.loads(ln))
    return out


def load_signals(lag_hours: int = 0) -> list[dict]:
    out = []
    for p in sorted(signals_dir(lag_hours).glob("*.jsonl")):
        for ln in p.read_text(encoding="utf-8").splitlines():
            if ln.strip():
                out.append(json.loads(ln))
    return out


def cmd_reverse(lag_hours: int = 0) -> dict:
    """独立赢家漏选：每个事件的去向、首次选中、提前量、剩余机会。"""
    mat = load_matrices(lag_hours)
    syms = mat["symbols"]; sidx = {s: j for j, s in enumerate(syms)}
    total = mat["total"]; selected = mat["selected"]; status = mat["status"]
    events = load_events()
    rows = []
    for e in events:
        s = e["symbol"]
        j = sidx.get(s)
        rec = dict(e)
        if j is None or not mat["present"][j]:
            rec.update({"disposition": "不可观察", "first_selected_utc": None,
                        "lead_hours": None, "reason": "no_baseline_shard"})
            rows.append(rec); continue
        t0 = e["start_ms"]
        w0 = max(0, int((t0 - 30 * 86400_000 - MAIN_START_MS) // INTERVAL_MS))
        w1 = min(int(N_HOURS) - 1, int((t0 - MAIN_START_MS) // INTERVAL_MS))
        if w1 < 0 or w0 > w1:
            rec.update({"disposition": "不可观察", "first_selected_utc": None,
                        "lead_hours": None, "reason": "event_outside_main_range"})
            rows.append(rec); continue
        selwin = selected[w0:w1 + 1, j]
        stwin = status[w0:w1 + 1, j]
        totwin = total[w0:w1 + 1, j]
        first = None
        if selwin.any():
            first = w0 + int(np.argmax(selwin))
        mid = None
        we = min(int(N_HOURS) - 1, int((e["end_ms"] - MAIN_START_MS) // INTERVAL_MS))
        if we > w1:
            selmid = selected[w1 + 1:we + 1, j]
            if selmid.any():
                mid = w1 + 1 + int(np.argmax(selmid))
        rec["first_selected_utc"] = R.ms_to_iso(int(GRID_MS[first])) if first is not None else None
        rec["lead_hours"] = (w1 - first) if first is not None else None
        rec["mid_selected_utc"] = R.ms_to_iso(int(GRID_MS[mid])) if mid is not None else None
        rec["best_total_in_window"] = int(totwin.max()) if (totwin >= 0).any() else None
        rec["n_scored_in_window"] = int((totwin >= 0).sum())
        rec["n_hours_in_window"] = int(w1 - w0 + 1)
        if first is not None:
            rec["disposition"] = "及时发现" if first <= w1 else "出现过晚"
            rec["reason"] = None
        else:
            if (totwin >= 0).sum() == 0:
                codes, cnts = np.unique(stwin, return_counts=True)
                dom = STATUS_NAMES[int(codes[int(np.argmax(cnts))])]
                rec["disposition"] = "缺资料"
                rec["reason"] = f"窗口内无可评分小时，主状态={dom}"
            else:
                rec["disposition"] = "低分或排名靠后"
                rec["reason"] = f"窗口内最高总分 {rec['best_total_in_window']} < 门槛 {MIN_SCORE}"
        rows.append(rec)
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / f"reverse_events_lag{lag_hours}.jsonl").open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    from collections import Counter
    disp = Counter(r["disposition"] for r in rows)
    tier_disp: dict = {}
    for r in rows:
        tier_disp.setdefault(str(r["tier"]), Counter())[r["disposition"]] += 1
    summary = {
        "generated_at_utc": R.ms_to_iso(int(time.time() * 1000)),
        "n_events": len(rows), "disposition": dict(disp),
        "by_tier": {k: dict(v) for k, v in sorted(tier_disp.items(), key=lambda kv: -float(kv[0]))},
        "note": "范围过滤（闸门）历史不可重建 → 记为 unknown；24h 报价成交额近似只作敏感性",
    }
    (OUT / f"reverse_summary_lag{lag_hours}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"reverse: {len(rows)} 事件 → {dict(disp)}")
    return summary


def cmd_attention(lag_hours: int = 0, max_rank: int = 60) -> dict:
    """关注数量完整对照：评分 / 简单趋势 / 3 随机种子。"""
    mat = load_matrices(lag_hours)
    syms = mat["symbols"]; sidx = {s: j for j, s in enumerate(syms)}
    total = mat["total"]; trend = mat["trend"]
    events = load_events()
    scoreable = total >= 0

    def curve(rank: np.ndarray) -> dict:
        # 事件覆盖：事件窗口内该 symbol 的最好名次
        covered = {n: 0 for n in range(1, max_rank + 1)}
        ranks = []
        for e in events:
            j = sidx.get(e["symbol"])
            if j is None or not mat["present"][j]:
                ranks.append(None); continue
            t0 = e["start_ms"]
            w0 = max(0, int((t0 - 30 * 86400_000 - MAIN_START_MS) // INTERVAL_MS))
            w1 = min(int(N_HOURS) - 1, int((t0 - MAIN_START_MS) // INTERVAL_MS))
            if w1 < 0 or w0 > w1:
                ranks.append(None); continue
            col = rank[w0:w1 + 1, j]
            m = int(col.min())
            ranks.append(m if m < 32767 else None)
        for m in ranks:
            if m is None:
                continue
            for n in range(m, max_rank + 1):
                covered[n] += 1
        return covered, ranks

    out = {}
    rank_score = rank_matrix(total)
    covered, ranks = curve(rank_score)
    out["score"] = {"covered_by_n": covered, "event_best_rank": ranks}
    # 简单趋势：trend 优先，其余按 symbol 升序（stable）
    covered_t, ranks_t = curve(_rank_rows(trend.astype(np.int16), scoreable))
    out["trend"] = {"covered_by_n": covered_t, "event_best_rank": ranks_t}
    for i in range(3):
        seed = 20261009 + i
        c, r = curve(random_rank_matrix(seed, total))
        out[f"random_{seed}"] = {"covered_by_n": c, "event_best_rank": r}
    # 每小时数量统计（评分）
    sel_per_hour = mat["selected"].sum(axis=1)
    out["load"] = {
        "selected_per_hour_mean": float(sel_per_hour.mean()),
        "selected_per_hour_median": float(np.median(sel_per_hour)),
        "selected_per_hour_p90": float(np.percentile(sel_per_hour, 90)),
        "selected_per_hour_max": int(sel_per_hour.max()),
        "scoreable_per_hour_mean": float(scoreable.sum(axis=1).mean()),
        "scoreable_per_hour_median": float(np.median(scoreable.sum(axis=1))),
    }
    (OUT / f"attention_lag{lag_hours}.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    # 可读曲线（评分 vs 随机均值）
    lines = ["n,score_covered,trend_covered,random_mean_covered"]
    for n in range(1, max_rank + 1):
        rm = np.mean([out[f"random_{20261009+i}"]["covered_by_n"][n] for i in range(3)])
        lines.append(f"{n},{covered[n]},{covered_t[n]},{rm:.1f}")
    (OUT / f"attention_curve_lag{lag_hours}.csv").write_text("\n".join(lines), encoding="utf-8")
    log(f"attention: 事件 {len(events)}；每小时选中 均值 {out['load']['selected_per_hour_mean']:.1f} / "
        f"中位 {out['load']['selected_per_hour_median']:.0f} / P90 {out['load']['selected_per_hour_p90']:.0f} / "
        f"最大 {out['load']['selected_per_hour_max']}")
    return out


def cmd_integrity(lag_hours: int = 0) -> dict:
    """完整性：所有去向对账、未知/未成熟、实际范围、原始分片引用。"""
    mat = load_matrices(lag_hours)
    syms = mat["symbols"]; status = mat["status"]
    from collections import Counter
    cnt = Counter()
    for k, name in STATUS_NAMES.items():
        cnt[name] = int((status == k).sum())
    total_cells = int(N_HOURS) * len(syms)
    events = load_events()
    imm = sum(1 for e in events if not e["mature_30d"])
    doc = {
        "generated_at_utc": R.ms_to_iso(int(time.time() * 1000)),
        "grid_hours": int(N_HOURS), "symbols": len(syms),
        "total_cells": total_cells,
        "status_counts": dict(cnt),
        "sum_check": sum(cnt.values()) == total_cells,
        "present_shards": int(mat["present"].sum()),
        "events_total": len(events),
        "events_immature_30d": imm,
        "events_immature_90d": sum(1 for e in events if not e["mature_90d"]),
        "unknown_scope": ["历史市值", "当时交易资格", "分片发布时刻", "更名/换币身份"],
        "note": "not_observable = 该小时该对象在归档中无可用 K 线（未观测），不是「未入选」",
    }
    (OUT / f"integrity_lag{lag_hours}.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1),
                                                        encoding="utf-8")
    log(f"integrity: cells={total_cells} 对账={doc['sum_check']}；"
        f"事件 {len(events)}（30 天未成熟 {imm}）")
    return doc


# ===========================================================================
# CLI
# ===========================================================================

def main() -> int:
    ap = argparse.ArgumentParser(description="历史双向研究（离线，只写 reports/history_research_ds/）")
    ap.add_argument("cmd", choices=["universe", "events", "baseline", "reverse",
                                    "attention", "integrity", "forward", "retention", "all"])
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--lag-hours", type=int, default=0)
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--limit", type=int, default=None, help="只跑前 N 个对象（冒烟）")
    args = ap.parse_args()

    guard = R.NetworkGuard()
    guard.install()
    try:
        if args.cmd == "universe":
            cmd_universe()
        elif args.cmd == "events":
            cmd_events(args.workers)
        elif args.cmd == "baseline":
            cmd_baseline(args.workers, args.lag_hours, args.overwrite, args.limit)
        elif args.cmd == "reverse":
            cmd_reverse(args.lag_hours)
        elif args.cmd == "attention":
            cmd_attention(args.lag_hours)
        elif args.cmd == "integrity":
            cmd_integrity(args.lag_hours)
        elif args.cmd == "forward":
            cmd_forward(args.workers, args.lag_hours, args.limit)
        elif args.cmd == "retention":
            cmd_retention(args.lag_hours)
        else:
            cmd_universe()
            cmd_events(args.workers)
            cmd_baseline(args.workers, args.lag_hours, args.overwrite, args.limit)
            cmd_reverse(args.lag_hours)
            cmd_attention(args.lag_hours)
            cmd_integrity(args.lag_hours)
            cmd_forward(args.workers, args.lag_hours, args.limit)
            cmd_retention(args.lag_hours)
    finally:
        guard.uninstall()
        log(f"网络外连拦截计数 = {guard.blocked}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
