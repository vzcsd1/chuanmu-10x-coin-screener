#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""B4 第 1 步（ds）：历史归档接入核对 + 最小离线试跑。

本工具只做 STRATEGY_REVIEW.md 第 9.6 节第 1 步：
  · 覆盖与未知项映射（队列覆盖 / 文件台账核验 / 真实行覆盖 三层分开）
  · 先定样本（pilot_manifest.json）再算分，不看后续收益挑月份
  · 明确时间输入契约（原始值/单位/规范时间/实际采用点/可取得时间依据）
  · 核对当前取点契约（box_score 的 df.iloc[-2] 与量增 iloc[:-1]）
  · 保留全部去向（每个预定标的/时点都有输出，含跳过/未知原因）
  · 隔离未来结果（评分入口不接受未来标签；截止时间之后的数据不改变当时评分）
  · 实际跑一次并记录文件数/时点数/读取字节/耗时/峰值内存/输出大小

硬边界（违反即失败）：
  · 不联网：运行期安装 socket 守卫，任何外连直接抛错并计数
  · 不改生产评分：只 import binance_box_strategy 的 indicators/box_score/deriv_score
  · 不写生产文件：只写输出目录（默认 reports/b4_stage1_ds/r2，**不覆盖 r1 冻结产物**）
  · 不扩大为全历史研究：只跑 pilot_manifest.json 里明示的小段

运行（PowerShell，项目根目录）：
  py -3.10 tools/b4_replay_ds.py all           # 全量（含 coverage 百万路径扫描）
  py -3.10 tools/b4_replay_ds.py r2            # 返修批：只跑小段 + 差异 + 哈希（不重做盘点）
"""
from __future__ import annotations

import argparse
import csv
import functools
import hashlib
import io
import json
import math
import os
import re
import socket
import sys
import time
import tracemalloc
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
ARCHIVE = ROOT / "data" / "archive_raw"
QUEUE_CSV = ROOT / "reports" / "archive_download_ds_full" / "queue_v3.csv"
PROGRESS_V3 = ROOT / "reports" / "archive_download_ds_full" / "progress_v3.json"
R1_DIR = ROOT / "reports" / "b4_stage1_ds"          # 旧（r1）冻结交付：只读，不覆盖
OUT_DIR = R1_DIR / "r2"                              # 默认输出：独立子目录
REVIEW_DIR = ROOT / "reports" / "b4_stage1_review"   # 主代理审核目录：只读


def set_out_dir(path: "str | Path") -> Path:
    """切换输出目录。默认 r2 子目录，绝不覆盖 r1 顶层冻结产物（D5）。"""
    global OUT_DIR
    OUT_DIR = Path(path)
    return OUT_DIR


sys.path.insert(0, str(ROOT))
import binance_box_strategy as B  # noqa: E402

CFG = B.Config()
INTERVAL_MS = 3_600_000  # 1h

# 评分口径常量：直接取自生产模块，避免两套漂移
WEIGHTS = dict(B.WEIGHTS)
MAX_SCORE = B.MAX_SCORE
MIN_SCORE = CFG.min_score
LOOKBACK = CFG.lookback

# 合约侧接口窗口（D1 返修）：线上 limit=DERIV_LOOKBACK，只返回最近 N 条 1h 观测
DERIV_LOOKBACK = B.DERIV_LOOKBACK        # 200
DERIV_PERIOD = B.DERIV_PERIOD            # "1h"
DERIV_MIN_OI_OBS = 25                    # 生产：有效 OI 观测 >= 25 才允许算 oi_chg

# K 线读取过去跨度：lookback 根 1h（≈10 天）+ 余量
KLINE_WARMUP_DAYS = int(math.ceil(LOOKBACK / 24.0)) + 2

# metrics 侧字段映射（与生产 fetch_deriv_context 的口径对齐）
OI_VALUE_FIELD = "sum_open_interest_value"          # 生产：sumOpenInterestValue
OI_AMOUNT_FIELD = "sum_open_interest"               # 数量，仅记录，不参与评分
LS_TOP_FIELD = "sum_toptrader_long_short_ratio"     # 生产：topLongShortPositionRatio（大户持仓比）
LS_TOP_ACCOUNT_FIELD = "count_toptrader_long_short_ratio"  # 大户账户比：禁止冒充大户持仓比
LS_GLOBAL_FIELD = "count_long_short_ratio"          # 全市场账户比：禁止冒充大户持仓比

METRIC_COLS = [OI_AMOUNT_FIELD, OI_VALUE_FIELD, LS_TOP_ACCOUNT_FIELD, LS_TOP_FIELD,
               LS_GLOBAL_FIELD, "sum_taker_long_short_vol_ratio"]


# ---------------------------------------------------------------------------
# 运行期网络守卫
# ---------------------------------------------------------------------------

class NetworkBlocked(RuntimeError):
    pass


class NetworkGuard:
    """拦截任何外连尝试。只在进程内生效，不修改系统设置。"""

    def __init__(self) -> None:
        self.blocked = 0
        self._orig_connect = None
        self._orig_create = None

    def install(self) -> None:
        guard = self
        self._orig_connect = socket.socket.connect
        self._orig_create = socket.create_connection

        def _connect(sock, address):  # noqa: ANN001
            guard.blocked += 1
            raise NetworkBlocked(f"外连被拦截: {address!r}")

        def _create(address, *a, **kw):  # noqa: ANN001
            guard.blocked += 1
            raise NetworkBlocked(f"外连被拦截: {address!r}")

        socket.socket.connect = _connect
        socket.create_connection = _create

    def uninstall(self) -> None:
        if self._orig_connect is not None:
            socket.socket.connect = self._orig_connect
        if self._orig_create is not None:
            socket.create_connection = self._orig_create


# ---------------------------------------------------------------------------
# 时间解析（唯一真源：本文件内一套；不复制第二套漂移实现）
# ---------------------------------------------------------------------------

def normalize_epoch_ms(value) -> int | None:
    """把任意时间输入归一为毫秒 epoch（UTC）。

    规则（沿用 research/02_detect_pumps.py 的逐行归一思路并显式化）：
      · 纯数字：>1e17 或 <1e8 → 非法；1e14..1e17 → 微秒（/1000）；
        1e11..1e14 → 毫秒；1e8..1e11 → 秒（*1000）
      · 字符串：先尝试数值规则，再按 ISO/常见格式解析为 UTC
    非法或缺失返回 None —— **绝不填 0**。
    """
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        num = float(value)
        if not (num == num) or num in (float("inf"), float("-inf")):
            return None
        return _scale_numeric(num)
    text = str(value).strip()
    if not text:
        return None
    try:
        num = float(text)
    except ValueError:
        num = None
    if num is not None:
        return _scale_numeric(num)
    try:
        ts = pd.Timestamp(text)
    except (ValueError, TypeError):
        return None
    if ts is pd.NaT:
        return None
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return int(ts.tz_convert("UTC").value // 10 ** 6)


def _scale_numeric(num: float) -> int | None:
    if not (num == num) or num in (float("inf"), float("-inf")):
        return None
    if num >= 1e17 or num < 1e8:
        return None
    if num >= 1e14:                     # 微秒
        return int(round(num / 1000.0))
    if num >= 1e11:                     # 毫秒
        return int(round(num))
    return int(round(num * 1000.0))     # 秒


def ms_to_iso(ms: int | None) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def parse_utc(text: str) -> int:
    ts = pd.Timestamp(text)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return int(ts.tz_convert("UTC").value // 10 ** 6)


# ---------------------------------------------------------------------------
# 归档读取（按币/分片，不一次载入全库）
# ---------------------------------------------------------------------------

@dataclass
class ReadLog:
    files: list = field(default_factory=list)
    compressed_bytes: int = 0
    uncompressed_bytes: int = 0
    rows: int = 0

    def add(self, path: Path, uncompressed: int, rows: int) -> None:
        self.files.append(str(path.relative_to(ROOT)) if path.is_absolute() else str(path))
        self.compressed_bytes += path.stat().st_size
        self.uncompressed_bytes += uncompressed
        self.rows += rows

    def as_dict(self) -> dict:
        return {"files": list(self.files), "n_files": len(self.files),
                "compressed_bytes": self.compressed_bytes,
                "uncompressed_bytes": self.uncompressed_bytes, "rows": self.rows}


def futures_1h_dir(symbol: str) -> Path:
    return ARCHIVE / "futures" / "klines" / symbol / "1h"


def metrics_dir(symbol: str) -> Path:
    return ARCHIVE / "futures" / "metrics" / symbol / "no_interval"


def spot_1h_path(symbol: str, ym: str) -> Path:
    return ARCHIVE / "spot" / "klines" / symbol / "1h" / f"{symbol}-1h-{ym}.zip"


def metrics_path(symbol: str, day: str) -> Path:
    return ARCHIVE / "futures" / "metrics" / symbol / "no_interval" / f"{symbol}-metrics-{day}.zip"


def _zip_csv_text(path: Path) -> tuple[str, str]:
    with zipfile.ZipFile(path) as zf:
        names = [n for n in zf.namelist() if n.endswith(".csv")]
        if not names:
            raise ValueError(f"压缩包内无 CSV: {path}")
        raw = zf.read(names[0])
    return raw.decode("utf-8", "replace"), names[0]


def read_spot_1h(symbol: str, months: list[str], log: ReadLog | None = None) -> list[list[float]]:
    """读现货 1h 月分片 → [[ts_ms, open, high, low, close, volume], ...]（按 ts 升序）。

    币安 kline CSV 首列是 open_time；允许带表头；允许毫秒/微秒混用（逐行归一）。
    """
    rows: list[list[float]] = []
    for ym in months:
        path = spot_1h_path(symbol, ym)
        if not path.exists():
            continue
        text, _ = _zip_csv_text(path)
        lines = [ln for ln in text.splitlines() if ln.strip()]
        n_rows = 0
        for ln in lines:
            parts = ln.split(",")
            if len(parts) < 6:
                continue
            ts = normalize_epoch_ms(parts[0])
            if ts is None:                       # 表头或非法行：跳过，不填零
                continue
            try:
                row = [float(ts)] + [float(parts[i]) for i in range(1, 6)]
            except ValueError:
                continue
            rows.append(row)
            n_rows += 1
        if log is not None:
            log.add(path, len(text.encode("utf-8")), n_rows)
    rows.sort(key=lambda r: r[0])
    return rows


def read_metrics(symbol: str, days: list[str], log: ReadLog | None = None) -> pd.DataFrame | None:
    """读 U 本位合约 metrics 日分片（5m 粒度）→ DataFrame，dt 为 UTC。

    create_time 是**字符串日期**（"YYYY-MM-DD HH:MM:SS"），本函数显式按 UTC 解析。
    """
    frames = []
    for day in days:
        path = metrics_path(symbol, day)
        if not path.exists():
            continue
        text, _ = _zip_csv_text(path)
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if len(lines) < 2:
            if log is not None:
                log.add(path, len(text.encode("utf-8")), 0)
            continue
        header = lines[0].split(",")
        if header[0].strip() != "create_time":     # 无表头的老格式：补列名
            header = ["create_time", "symbol"] + [f"c{i}" for i in range(len(header) - 2)]
            body = lines
        else:
            body = lines[1:]
        df = pd.read_csv(io.StringIO("\n".join([",".join(header)] + body)))
        frames.append(df)
        if log is not None:
            log.add(path, len(text.encode("utf-8")), len(body))
    if not frames:
        return None
    df = pd.concat(frames, ignore_index=True)
    if "create_time" not in df.columns:
        return None
    parsed = df["create_time"].map(normalize_epoch_ms)
    df["dt"] = pd.to_datetime(parsed, unit="ms", utc=True)
    for col in METRIC_COLS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        else:
            df[col] = float("nan")
    df = df.dropna(subset=["dt"]).sort_values("dt").drop_duplicates("dt", keep="last")
    return df.reset_index(drop=True)


# ---------------------------------------------------------------------------
# 取点契约：把「当时可知的闭合 K 线」变成 box_score 能吃的 df
# ---------------------------------------------------------------------------

def kline_df_as_of(closed: list[list[float]], decision_ms: int, cfg=CFG) -> pd.DataFrame:
    """按生产取点契约构造 df。

    生产 public_selection 用 fetch_ohlcv(limit=cfg.lookback)：
      df 的最后一根是**正在形成**的那根，box_score 读 df.iloc[-2]（上一根闭合的）。
    历史归档只有已闭合 K 线，所以：
      窗口 = 最后 (lookback-1) 根已闭合 K 线，末尾**追加 1 根占位行**代表决策时刻正在形成的那根。
    占位行的 OHLCV 不参与 iloc[-2] 的任何计算（见 tests 的不变性用例），
    因此等价于线上「最后一根是形成中、iloc[-2] 是刚闭合那根」的输入。
    """
    n = max(cfg.lookback - 1, 1)
    window = closed[-n:]
    last_close = float(window[-1][4])
    placeholder = [float(decision_ms), last_close, last_close, last_close, last_close, 0.0]
    return B.indicators(window + [placeholder], cfg)


def closed_candles(rows: list[list[float]], decision_ms: int) -> list[list[float]]:
    """只保留在决策时刻**已经闭合**的 K 线：open_time + 1h <= decision。"""
    return [r for r in rows if r[0] + INTERVAL_MS <= decision_ms]


def _isfinite_col(df: pd.DataFrame, column: str) -> pd.Series:
    """逐值 isfinite（排除 NaN 与 ±inf），与生产 `math.isfinite` 口径一致。

    注意与 `notna()` 的区别：`inf` 不是 NaN，但生产会把它滤掉（D1 反例）。
    """
    arr = pd.to_numeric(df[column], errors="coerce").to_numpy(dtype="float64")
    return pd.Series(np.isfinite(arr), index=df.index)


def _is_pos_col(df: pd.DataFrame, column: str) -> pd.Series:
    arr = pd.to_numeric(df[column], errors="coerce").to_numpy(dtype="float64")
    return pd.Series(np.isfinite(arr) & (arr > 0), index=df.index)


def deriv_as_of(df: pd.DataFrame | None, decision_ms: int, lag_hours: int = 0,
                lookback: int = DERIV_LOOKBACK) -> dict:
    """按生产 `_fetch_deriv_context_live` 的口径，从 metrics 5m 归档重建合约侧输入。

    对齐点（D1/D4 返修，2026-10-09）：
      · **可得截止唯一**：采用点、24h/72h 基准、尾部不可用计数、状态，全部以
        `decision_ms - lag_hours*1h` 为界（D4）。未可得的数据不得影响当前输出。
      · **接口窗口**：线上 `limit=DERIV_LOOKBACK`（200 条 1h）只返回最近 N 条；
        这里同样只取「可得截止之前」的最后 `lookback` 条整点观测（D1）。
      · **OI 过滤 + 最小观测数**：`isfinite(v) and v > 0`，且有效观测 **>= 25** 条
        才允许算 oi_chg；否则 oi_chg 保持 None（不可算，**不是 0 分证据**）（D1）。
      · **LS 过滤**：只要求 `isfinite(ratio)`，**合法零值保留**（不自行改成必须正数）（D1）。
      · 24h/72h 基准在**同一窗口、同一过滤后**的序列里回退取最后一个 <= 截止的观测。

    仍与线上不同的地方（未变，必须标注）：
      · 线上用 /futures/data/* 的 1h 端点；这里用 metrics 的 5m 数据取整点行近似。
        未逐点比对是否数值相等（需联网，本批禁止）。
      · 线上端点只保留最近 30 天；metrics 归档可覆盖更早，属"归档更全"，不改口径。
      · 公开可得延迟未知：lag_hours=0 假设"整点观测在整点即可得"，属**显式假设**。
    """
    out: dict = {
        "status": "failed", "oi_chg_1d": None, "oi_chg_3d": None,
        "ls_top": None, "ls_chg_3d": None,
        "oi_as_of": None, "ls_as_of": None,
        "oi_stale_tail": 0, "ls_stale_tail": 0,
        "ls_source_field": None, "oi_source_field": OI_VALUE_FIELD,
        "alternatives": {"ls_top_account": None, "ls_global": None, "oi_amount": None},
        "assumptions": [f"deriv_lag_hours={lag_hours}",
                        f"endpoint_limit={lookback}（线上 DERIV_LOOKBACK）",
                        f"oi_min_valid_obs={DERIV_MIN_OI_OBS}（线上同款门槛）",
                        "metrics 5m 取整点行近似 1h 端点"],
        "oi_valid_obs": 0, "ls_valid_obs": 0, "oi_min_obs_required": DERIV_MIN_OI_OBS,
        "window": None, "reason": None,
    }
    if df is None or len(df) == 0:
        out["reason"] = "no_metrics_files"
        return out
    hourly = df[df["dt"].dt.minute == 0]
    if len(hourly) == 0:
        out["reason"] = "no_hourly_aligned_rows"
        return out
    # 可得截止：延迟与无延迟都用同一个界（D4）
    cutoff = decision_ms - lag_hours * INTERVAL_MS
    cutoff_ts = pd.Timestamp(cutoff, unit="ms", tz="UTC")
    available = hourly[hourly["dt"] <= cutoff_ts]
    if len(available) == 0:
        out["reason"] = "no_observation_at_or_before_cutoff"
        return out
    # 接口窗口：线上只返回最近 lookback 条（D1）
    window = available.tail(lookback)
    raw_ts = [int(t.value // 10 ** 6) for t in window["dt"]]
    out["window"] = {
        "available_rows": int(len(available)), "window_rows": int(len(window)),
        "endpoint_limit": int(lookback), "cutoff_utc": ms_to_iso(cutoff),
        "window_first_utc": ms_to_iso(raw_ts[0]), "window_last_utc": ms_to_iso(raw_ts[-1]),
    }

    def stale_tail(after_ms: int) -> int:
        """采用点之后、可得截止之前的观测数（都是当时已存在却用不了的）。"""
        return sum(1 for t in raw_ts if t > after_ms)

    # OI：先按生产口径过滤（有限且 >0），再要求 >= 25 条有效观测
    oi_valid = window[_is_pos_col(window, OI_VALUE_FIELD)]
    out["oi_valid_obs"] = int(len(oi_valid))
    # LS：只要求有限——**合法零值保留**（生产语义）
    ls_valid = window[_isfinite_col(window, LS_TOP_FIELD)]
    out["ls_valid_obs"] = int(len(ls_valid))
    if len(oi_valid) == 0 and len(ls_valid) == 0:
        out["reason"] = "no_usable_observation_at_or_before_cutoff"
        return out

    ok_oi = False
    if len(oi_valid) >= DERIV_MIN_OI_OBS:
        cur_oi_row = oi_valid.iloc[-1]
        cur_oi_ts = int(cur_oi_row["dt"].value // 10 ** 6)
        oi_val = float(cur_oi_row[OI_VALUE_FIELD])
        out["oi_as_of"] = cur_oi_ts
        out["alternatives"]["oi_amount"] = _finite_or_none(cur_oi_row[OI_AMOUNT_FIELD])
        has_1d = has_3d = False
        for back_h, key in ((24, "oi_chg_1d"), (72, "oi_chg_3d")):
            past = oi_valid[oi_valid["dt"] <= pd.Timestamp(cur_oi_ts - back_h * INTERVAL_MS,
                                                           unit="ms", tz="UTC")]
            if len(past):
                out[key] = oi_val / float(past.iloc[-1][OI_VALUE_FIELD]) - 1.0
                has_1d = has_1d or key == "oi_chg_1d"
                has_3d = has_3d or key == "oi_chg_3d"
        out["oi_stale_tail"] = stale_tail(cur_oi_ts)
        ok_oi = has_1d and has_3d

    ok_ls = False
    if len(ls_valid):
        cur_ls_row = ls_valid.iloc[-1]
        cur_ls_ts = int(cur_ls_row["dt"].value // 10 ** 6)
        out["ls_top"] = float(cur_ls_row[LS_TOP_FIELD])
        out["ls_source_field"] = LS_TOP_FIELD
        out["ls_as_of"] = cur_ls_ts
        past = ls_valid[ls_valid["dt"] <= pd.Timestamp(cur_ls_ts - 72 * INTERVAL_MS,
                                                       unit="ms", tz="UTC")]
        if len(past):
            out["ls_chg_3d"] = float(cur_ls_row[LS_TOP_FIELD]) - float(past.iloc[-1][LS_TOP_FIELD])
        out["ls_stale_tail"] = stale_tail(cur_ls_ts)
        ok_ls = True
    out["alternatives"]["ls_top_account"] = _finite_or_none(window.iloc[-1][LS_TOP_ACCOUNT_FIELD])
    out["alternatives"]["ls_global"] = _finite_or_none(window.iloc[-1][LS_GLOBAL_FIELD])

    out["status"] = "ok" if (ok_oi and ok_ls) else ("partial" if (ok_oi or ok_ls) else "failed")
    if out["status"] != "ok":
        missing = []
        if not ok_oi:
            missing.append(f"oi_chg_1d/3d(valid_obs={len(oi_valid)}<{DERIV_MIN_OI_OBS})"
                           if len(oi_valid) < DERIV_MIN_OI_OBS else "oi_chg_1d/3d(窗口内基准不足)")
        if not ok_ls:
            missing.append("ls_top")
        out["reason"] = "missing:" + "+".join(missing)
    return out


def _finite_or_none(value):
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num != num or num in (float("inf"), float("-inf")):
        return None
    return num


# ---------------------------------------------------------------------------
# 单时点评分（评分入口不接受任何未来标签/涨幅/事件名单）
# ---------------------------------------------------------------------------

def _count_window_holes(open_times: list[int]) -> int:
    """窗口内相邻 K 线之间 > 1h 的空档个数（**中间缺口**；末端缺口另记 gap_hours）。"""
    return sum(1 for a, b in zip(open_times, open_times[1:]) if b - a > INTERVAL_MS)


def score_timepoint(symbol: str, decision_ms: int, candles: list[list[float]],
                    metrics_df: pd.DataFrame | None, has_futures: bool,
                    deriv_lag_hours: int = 0) -> dict:
    """对单个 (标的, 决策时刻) 输出完整去向。只接受「当时可知」的输入。

    `has_futures` = 归档中存在该标的的 U 本位永续 K 线（对应线上 future_by_base）。
    `metrics_df` = 合约指标归档；两者分开，因为「有合约」不等于「指标可重建」。

    与生产 `public_selection` 的流程对齐（D2/D3 返修，2026-10-09）：
      生产顺序是 ①K 线长度闸门 → ②box_score → ③合约取数 → ④总分与门槛。
      因此 **K 线未通过闸门的标的，线上根本不会进入评分，也就不可能入选**。
      本函数仍会算合约侧用于**诊断**，但把它与"入选信号"严格分开命名。
    """
    rec: dict = {
        "symbol": symbol,
        "decision_time_utc": ms_to_iso(decision_ms),
        "decision_ms": decision_ms,
        "has_futures_market": has_futures,
        "reasons": [],
    }

    # —— K 线侧 ——
    closed = closed_candles(candles, decision_ms)
    kline = {"status": "no_kline_data", "n_closed_candles": len(closed),
             "last_closed_open_time_utc": None, "gap_hours": None,
             "score": None, "details": {}, "adopted_candle_utc": None,
             "scored": False, "df_rows": None,
             "warmup_required_rows": CFG.box_period + 3,
             "adopted_is_immediate_prev": None, "n_window_holes": None,
             "window_closed_candles": None}
    if closed:
        last = closed[-1]
        kline["last_closed_open_time_utc"] = ms_to_iso(int(last[0]))
        gap_ms = decision_ms - (last[0] + INTERVAL_MS)
        kline["gap_hours"] = round(gap_ms / INTERVAL_MS, 3)
        if gap_ms > INTERVAL_MS:      # 决策时刻与最后一根闭合 K 线之间有空档
            kline["status"] = "kline_gap"
            rec["reasons"].append(f"kline_gap:{kline['gap_hours']}h")
        else:
            window_closed = closed[-(CFG.lookback - 1):]
            df = kline_df_as_of(closed, decision_ms)
            kline["df_rows"] = int(len(df))
            kline["window_closed_candles"] = len(window_closed)
            kline["n_window_holes"] = _count_window_holes([int(r[0]) for r in window_closed])
            # 预热门槛按**真实输入经 indicators 后的行数**判定（含末尾占位行），
            # 与生产 `len(indicators(candles)) < box_period + 3` 同口径（D2）。
            if len(df) < CFG.box_period + 3:
                kline["status"] = "kline_short_history"
                rec["reasons"].append(
                    f"kline_short_history:df_rows={len(df)}<{CFG.box_period + 3}")
            else:
                score, details = B.box_score(df, CFG)
                kline["status"] = "ok"
                kline["scored"] = True
                kline["score"] = int(score)
                kline["details"] = {k: _finite_or_none(v) for k, v in details.items()}
                adopted_ts = int(df.iloc[-2]["ts"])
                kline["adopted_candle_utc"] = ms_to_iso(adopted_ts)
                # 撤销「ok 恒为 decision−1h」的无条件说法：有中间/末端缺口时采用点会更早
                kline["adopted_is_immediate_prev"] = bool(adopted_ts == decision_ms - INTERVAL_MS)
    else:
        rec["reasons"].append("no_closed_candle_at_decision")

    # —— 合约侧（即使 K 线未评分也算，供诊断；见下方 diagnostic_only 标注）——
    if not has_futures:
        deriv = {"status": "no_futures", "oi_chg_1d": None, "oi_chg_3d": None,
                 "ls_top": None, "ls_chg_3d": None, "oi_as_of": None, "ls_as_of": None,
                 "oi_stale_tail": 0, "ls_stale_tail": 0,
                 "ls_source_field": None, "alternatives": {},
                 "assumptions": [], "reason": "no_usdt_perp_archive"}
    else:
        deriv = deriv_as_of(metrics_df, decision_ms, lag_hours=deriv_lag_hours)
        if deriv["status"] != "ok":
            rec["reasons"].append(f"deriv_{deriv['status']}:{deriv.get('reason')}")

    deriv_points, deriv_details = B.deriv_score(
        deriv.get("oi_chg_1d"), deriv.get("oi_chg_3d"),
        deriv.get("ls_top"), deriv.get("ls_chg_3d"))

    # 未知不等于零：K 线未评分时 score_kline / score_total 记 None，不偷偷变成 0（D3）
    kline_points = int(kline["score"]) if kline["scored"] else None
    deriv_points_int = int(deriv_points)     # 生产语义：合约侧缺项计 0 分
    total = (kline_points + deriv_points_int) if kline["scored"] else None
    selected = bool(kline["scored"] and total is not None and total >= MIN_SCORE)
    if selected:
        screening_status = "selected"
    elif kline["scored"]:
        screening_status = "scored_below_threshold"
    else:
        screening_status = f"skipped:{kline['status']}"

    rec.update({
        "kline": kline,
        "deriv": deriv,
        "score_kline": kline_points,                     # None = 未评分（未知，不是 0）
        "score_deriv": deriv_points_int,
        "score_deriv_diagnostic_only": bool(not kline["scored"]),
        "score_total": total,                            # None = K 线未评分 → 总分不可得
        "score_total_observed_parts": {"kline": kline_points, "deriv": deriv_points_int},
        "max_score": MAX_SCORE,
        "deriv_details": {k: _finite_or_none(v) for k, v in deriv_details.items()},
        "min_score": MIN_SCORE,
        "selected": selected,
        "screening_status": screening_status,
        "threshold_compare": _threshold_compare(kline["scored"], total, deriv_points_int),
        "conditions": _conditions(kline, deriv_details),
        "completeness": {
            "kline_scored": kline["scored"],
            "deriv_status": deriv["status"],
            "gate_market_cap": "unknown",       # 历史市值不可重建
            "gate_eligibility": "unknown",      # 当时交易资格不可重建
            "scope_note": "本行只验证同一可观察子集(K线+合约)上的评分，不等于复现线上全流程",
        },
        "field_adoption": {
            "kline_last_closed_open_time_utc": kline["last_closed_open_time_utc"],
            "kline_adopted_candle_utc": kline.get("adopted_candle_utc"),
            "kline_adopted_is_immediate_prev": kline.get("adopted_is_immediate_prev"),
            "deriv_oi_as_of_utc": ms_to_iso(deriv.get("oi_as_of")),
            "deriv_ls_as_of_utc": ms_to_iso(deriv.get("ls_as_of")),
            "market_observation_time_utc": ms_to_iso(decision_ms),
            "deriv_availability_cutoff_utc": ms_to_iso(decision_ms - deriv_lag_hours * INTERVAL_MS),
            "publicly_available_time_utc": "unknown(见 input_contract.md 假设)",
            "archive_download_time_utc": _archive_download_time(),
        },
    })
    return rec


def _threshold_compare(kline_scored: bool, total: int | None, deriv_points: int) -> dict:
    """门槛比较。K 线未评分时明确标注「不构成入选」，不做静默的零分比较（D3）。"""
    if kline_scored:
        return {"observed_total": total, "min_score": MIN_SCORE,
                "meets_threshold": bool(total is not None and total >= MIN_SCORE),
                "is_selection_signal": True,
                "note": "K 线已评分，总分达到门槛即入选"}
    return {"observed_total": None, "min_score": MIN_SCORE, "meets_threshold": None,
            "is_selection_signal": False,
            "deriv_points_diagnostic_only": deriv_points,
            "note": "K 线未评分（线上会在此处跳过、不进入评分）→ 不构成入选；"
                    "合约侧分数仅为诊断，不是「观察到的零分」也不是入选信号"}


def _conditions(kline: dict, deriv_details: dict) -> str:
    hits = []
    d = kline.get("details") or {}
    if d.get("c_trend"):
        hits.append("收盘>EMA50")
    if deriv_details.get("hit_oi_1d"):
        hits.append("OI 1日增>10%")
    if deriv_details.get("hit_oi_3d"):
        hits.append("OI 3日增>20%")
    if deriv_details.get("hit_ls_top"):
        hits.append("大户持仓多空比<1")
    if d.get("c_breakout"):
        hits.append("突破箱体")
    if d.get("c_v24up"):
        hits.append("24h量增")
    return ",".join(hits) or "无"


_DOWNLOAD_TIME = None


def _archive_download_time() -> str:
    global _DOWNLOAD_TIME
    if _DOWNLOAD_TIME is None:
        stamp = "unknown"
        try:
            data = json.loads(PROGRESS_V3.read_text(encoding="utf-8"))
            stamp = data.get("updated_at_utc") or "unknown"
        except Exception:  # noqa: BLE001
            pass
        _DOWNLOAD_TIME = f"{stamp}(v3 队列完成时间，见 progress_v3.json)"
    return _DOWNLOAD_TIME


# ---------------------------------------------------------------------------
# 覆盖核对
# ---------------------------------------------------------------------------

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _queue_scan(index: set[str] | None = None, sample_missing: int = 8) -> dict:
    """**单次遍历**队列：同时得到「队列覆盖」聚合与「队列→本地文件」逐份对账。

    传 index 时做逐份对账（分母=队列总数）；不传则只做队列聚合。
    """
    agg: dict = {}
    total = 0
    missing_total = 0
    hit: set[str] = set()
    for row in _iter_queue():
        total += 1
        key = f"{row['market']}|{row['dataset']}|{row['interval'] or '-'}"
        slot = agg.setdefault(key, {"count": 0, "symbols": set(), "min_start": None,
                                    "max_end": None, "exists": 0, "missing": []})
        slot["count"] += 1
        slot["symbols"].add(row["symbol"])
        ps, pe = row["period_start"], row["period_end"]
        if ps and (slot["min_start"] is None or ps < slot["min_start"]):
            slot["min_start"] = ps
        if pe and (slot["max_end"] is None or pe > slot["max_end"]):
            slot["max_end"] = pe
        if index is not None:
            try:
                rel = _downloader_entry(row).relpath().as_posix()
            except Exception:  # noqa: BLE001
                rel = None
            if rel is not None and rel in index:
                slot["exists"] += 1
                hit.add(rel)
            else:
                missing_total += 1
                if len(slot["missing"]) < sample_missing:
                    slot["missing"].append(row["source_url"])
    queue = {"total_rows": total, "by_dataset": {}}
    recon = {"by_dataset": {}, "missing_total": missing_total,
             "note": "逐份对账：队列每一行 → 下载器权威路径 → 是否在本地路径索引中"}
    if index is not None:
        extra = sorted(index - hit)
        recon["extra_local_files"] = len(extra)
        recon["extra_local_examples"] = extra[:10]
        recon["extra_local_note"] = "本地有载荷文件、但队列里没有对应行的文件（不影响本批只读结论）"
    for key, slot in sorted(agg.items()):
        queue["by_dataset"][key] = {"count": slot["count"], "symbols": len(slot["symbols"]),
                                    "min_period_start": slot["min_start"],
                                    "max_period_end": slot["max_end"]}
        if index is not None:
            recon["by_dataset"][key] = {"queue": slot["count"], "local_exists": slot["exists"],
                                        "missing": slot["count"] - slot["exists"],
                                        "missing_examples": slot["missing"]}
    return {"queue": queue, "reconciliation": recon}


def _iter_queue():
    with QUEUE_CSV.open(encoding="utf-8-sig", newline="") as fh:
        yield from csv.DictReader(fh)


def _queue_aggregate() -> dict:
    return _queue_scan(index=None)["queue"]


def _is_payload_name(name: str) -> bool:
    """归档载荷文件名：`X.zip` 或 safe_seg 给非 ASCII 名追加的 `X.zip~<hash>`。"""
    if name.endswith(".zip"):
        return True
    base, sep, tail = name.rpartition("~")
    return bool(sep) and base.endswith(".zip") and len(tail) == 10 and all(
        c in "0123456789abcdef" for c in tail)


def _local_census() -> dict:
    """全库文件普查：按 市场|类别|周期 统计本地实际落盘载荷文件数。

    只做目录遍历 + 计数，不解压、不联网。同时建立相对路径集合供逐份对账用。
    """
    census: dict[str, int] = {}
    leftovers = 0
    total = 0
    index: set[str] = set()
    for root, dirs, files in os.walk(ARCHIVE):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        rel = Path(root).relative_to(ARCHIVE).parts
        for f in files:
            if f.endswith(".stale") or ".stale-" in f or f.endswith(".pending") or f.endswith(".part"):
                leftovers += 1
                continue
            if not _is_payload_name(f):
                continue
            if len(rel) != 4:
                continue
            index.add("/".join(rel + (f,)))
            market, dataset, _symbol, interval = rel
            if interval == "no_interval":
                interval = ""
            key = f"{market}|{dataset}|{interval or '-'}"
            census[key] = census.get(key, 0) + 1
            total += 1
    return {"total_payload_files": total, "leftover_or_stale_files": leftovers,
            "by_dataset": dict(sorted(census.items())), "_index": index}


def _local_aggregate() -> dict:
    def dirs(base: Path, depth_names) -> list[str]:
        if not base.is_dir():
            return []
        return sorted(p.name for p in base.iterdir() if p.is_dir())

    def month_files(path: Path) -> list[str]:
        if not path.is_dir():
            return []
        return sorted(p.name for p in path.glob("*") if _is_payload_name(p.name))

    spot_syms = dirs(ARCHIVE / "spot" / "klines", None)
    fut_syms = dirs(ARCHIVE / "futures" / "klines", None)
    met_syms = dirs(ARCHIVE / "futures" / "metrics", None)
    spot_1h = [s for s in spot_syms if (ARCHIVE / "spot" / "klines" / s / "1h").is_dir()]
    fut_1h = [s for s in fut_syms if (ARCHIVE / "futures" / "klines" / s / "1h").is_dir()]
    met_any = [s for s in met_syms if (ARCHIVE / "futures" / "metrics" / s / "no_interval").is_dir()]

    n_spot_1h_files = sum(len(month_files(ARCHIVE / "spot" / "klines" / s / "1h")) for s in spot_1h)
    n_fut_1h_files = sum(len(month_files(ARCHIVE / "futures" / "klines" / s / "1h")) for s in fut_1h)
    n_met_files = sum(len(month_files(ARCHIVE / "futures" / "metrics" / s / "no_interval")) for s in met_any)

    return {
        "spot_symbols_any": len(spot_syms),
        "spot_symbols_with_1h": len(spot_1h),
        "spot_1h_files": n_spot_1h_files,
        "futures_symbols_any": len(fut_syms),
        "futures_symbols_with_1h": len(fut_1h),
        "futures_1h_files": n_fut_1h_files,
        "metrics_symbols": len(met_any),
        "metrics_files": n_met_files,
        "spot1h_without_futures_metrics": sorted(set(spot_1h) - set(met_any))[:50],
        "spot1h_without_futures_metrics_count": len(set(spot_1h) - set(met_any)),
        "futures_metrics_without_spot1h_count": len(set(met_any) - set(spot_1h)),
    }


def _ledger_check(sample: int = 400) -> dict:
    """文件台账核验：抽 N 份队列项，核对本地文件存在 + 字节数 + 台账状态。"""
    state_path = ARCHIVE / "_state.json"
    records = {}
    if state_path.exists():
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
            records = data.get("records", {})
        except Exception:  # noqa: BLE001
            records = {}
    checked = present = size_match = ledger_ok = 0
    mismatches = []
    step = 1
    with QUEUE_CSV.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        for i, row in enumerate(reader):
            if i % max(1, (1_000_272 // sample)) != 0:
                continue
            checked += 1
            rel = _queue_row_to_relpath(row)
            path = ARCHIVE / rel if rel else None
            if path is None or not path.exists():
                mismatches.append({"row": i, "reason": "missing", "url": row["source_url"]})
                continue
            present += 1
            declared = int(row["remote_size_bytes"] or 0)
            if declared and path.stat().st_size == declared:
                size_match += 1
            else:
                mismatches.append({"row": i, "reason": "size", "declared": declared,
                                   "actual": path.stat().st_size})
            if records:
                key = _queue_row_to_state_key(row)
                if key in records and records[key].get("checksum_status") == "verified":
                    ledger_ok += 1
            if checked >= sample:
                break
    return {"sampled": checked, "present": present, "size_match": size_match,
            "ledger_verified": ledger_ok, "mismatches": mismatches[:20],
            "note": "抽样核验，不等于逐份全量校验"}


_AD_MOD = None


def _downloader_entry(row: dict):
    """复用下载器自己的 Entry（唯一真源），不另写一套会漂移的路径映射。

    对 `safe_seg` 加一层纯函数缓存：队列里 market/dataset/symbol/interval 大量重复，
    缓存后逐份对账从"每行 5 次正则"降到"每个唯一片段 1 次"，语义完全不变。
    """
    global _AD_MOD
    if _AD_MOD is None:
        sys.path.insert(0, str(ROOT / "tools"))
        import archive_download_ds as ad  # noqa: E402
        ad.safe_seg = functools.lru_cache(maxsize=2_000_000)(ad.safe_seg)
        _AD_MOD = ad
    return _AD_MOD.Entry.from_row(row)


def _queue_row_to_relpath(row: dict) -> str | None:
    """把队列行映射到本地相对路径（复用 tools/archive_download_ds.py 的落盘布局）。"""
    try:
        return _downloader_entry(row).relpath().as_posix()
    except Exception:  # noqa: BLE001
        return None


def _queue_row_to_state_key(row: dict) -> str:
    try:
        return _downloader_entry(row).key
    except Exception:  # noqa: BLE001
        interval = row["interval"] or ""
        return f"{row['market']}|{row['dataset']}|{row['symbol']}|{interval}|{row['period_start']}|{row['period_end']}"


def build_coverage(full: bool = True) -> dict:
    census = _local_census()
    index = census.pop("_index")
    scan = _queue_scan(index=index if full else None)
    queue = scan["queue"]
    recon = scan["reconciliation"] if full else {"skipped": "full=False（测试用轻量模式）"}
    cov = {
        "generated_at_utc": ms_to_iso(int(time.time() * 1000)),
        "queue": queue,
        "local_census": census,
        "queue_vs_local_reconciliation": recon,
        "local": _local_aggregate(),
        "ledger_sample_check": _ledger_check(),
        "levels": {
            "queue_coverage": "队列计划了什么（queue_v3.csv）",
            "file_ledger_verification": "队列每一行 → 权威路径 → 本地文件是否存在 + 字节数 + 台账校验状态",
            "real_row_coverage": "分片内真实数据行（pilot 小段逐行解析，见 scoring_output.jsonl）",
        },
        "identity": _identity_map(),
        "unknowns": [
            "历史市值：归档中没有历史市值序列，无法重建当时的市值闸门",
            "当时交易资格：归档只证明文件存在，不等于当时该现货对在交易",
            "公开可得时间：归档未记录每份分片的发布时间，只有下载时间",
            "现货/合约同一标的的关联：按 base 资产名匹配，未逐币核对换币与更名",
            "metrics 覆盖起点：全市场 metrics 归档实际从 2021-12-01 开始",
        ],
    }
    return cov


def _identity_map() -> dict:
    spot_1h = {p.name for p in (ARCHIVE / "spot" / "klines").iterdir()
               if (ARCHIVE / "spot" / "klines" / p.name / "1h").is_dir()} \
        if (ARCHIVE / "spot" / "klines").is_dir() else set()
    fut_1h = {p.name for p in (ARCHIVE / "futures" / "klines").iterdir()
              if (ARCHIVE / "futures" / "klines" / p.name / "1h").is_dir()} \
        if (ARCHIVE / "futures" / "klines").is_dir() else set()
    met = {p.name for p in (ARCHIVE / "futures" / "metrics").iterdir()
           if (ARCHIVE / "futures" / "metrics" / p.name / "no_interval").is_dir()} \
        if (ARCHIVE / "futures" / "metrics").is_dir() else set()
    same = sorted(spot_1h & fut_1h & met)
    spot_only = sorted(spot_1h - fut_1h - met)
    fut_only = sorted(fut_1h - spot_1h)
    return {
        "same_name_all_three_count": len(same),
        "spot_only_count": len(spot_only),
        "futures_without_spot_1h_count": len(fut_only),
        "note": "身份匹配只用「同名 USDT 交易对」；不处理更名/换币（如 KLAY→KAIA），"
                "因此同名匹配为保守下界，未匹配部分记入未知。",
        "examples_spot_only": spot_only[:20],
        "examples_futures_without_spot": fut_only[:20],
    }


# ---------------------------------------------------------------------------
# 覆盖缺口扫描（只 glob 文件系统，不解压）
# ---------------------------------------------------------------------------

def _month_range(a: str, b: str) -> list[str]:
    cur = pd.Timestamp(a + "-01")
    end = pd.Timestamp(b + "-01")
    out = []
    while cur <= end:
        out.append(f"{cur.year:04d}-{cur.month:02d}")
        cur = cur + pd.offsets.MonthBegin(1)
    return out


def _day_range(a: str, b: str) -> list[str]:
    cur = pd.Timestamp(a)
    end = pd.Timestamp(b)
    out = []
    while cur <= end:
        out.append(cur.strftime("%Y-%m-%d"))
        cur = cur + pd.Timedelta(days=1)
    return out


def scan_gaps(symbols: list[str], max_report: int = 12) -> dict:
    out = {"spot_1h_month_gaps": {}, "metrics_day_gaps": {}}
    for sym in symbols:
        d = ARCHIVE / "spot" / "klines" / sym / "1h"
        have = sorted({m.group(1) for p in d.glob("*.zip")
                       if (m := re.search(r"(\d{4}-\d{2})", p.name))}) if d.is_dir() else []
        if len(have) >= 3:
            miss = [m for m in _month_range(have[0], have[-1]) if m not in set(have)]
            if miss:
                out["spot_1h_month_gaps"][sym] = {"have": len(have), "first": have[0],
                                                  "last": have[-1], "missing": miss[:max_report],
                                                  "missing_count": len(miss)}
        m = ARCHIVE / "futures" / "metrics" / sym / "no_interval"
        have_d = sorted({x.group(1) for p in m.glob("*.zip")
                         if (x := re.search(r"(\d{4}-\d{2}-\d{2})", p.name))}) if m.is_dir() else []
        if len(have_d) >= 3:
            miss_d = [x for x in _day_range(have_d[0], have_d[-1]) if x not in set(have_d)]
            if miss_d:
                runs = []
                for x in miss_d:
                    if runs and (pd.Timestamp(x) - pd.Timestamp(runs[-1][-1])).days == 1:
                        runs[-1].append(x)
                    else:
                        runs.append([x])
                out["metrics_day_gaps"][sym] = {
                    "have": len(have_d), "first": have_d[0], "last": have_d[-1],
                    "missing_count": len(miss_d),
                    "runs": [{"from": r[0], "to": r[-1], "days": len(r)} for r in runs][:max_report],
                }
    return out


# ---------------------------------------------------------------------------
# pilot manifest
# ---------------------------------------------------------------------------

def build_manifest() -> dict:
    """按格式/边界/缺失覆盖选样，理由写在每段里；不看后续收益。"""
    segs = [
        ("S01", "AAVEUSDT", "2021-12-01T00:00:00Z",
         "metrics 全市场归档覆盖起点（2021-12-01）；检验衍生侧预热不足时能否正确标不完整"),
        ("S02", "AAVEUSDT", "2024-06-01T00:00:00Z",
         "同一标的不同年代对照；检验同币跨年可复算"),
        ("S03", "KLAYUSDT", "2024-10-28T00:00:00Z",
         "同月现货 1d 存在微秒时间戳（KLAYUSDT-1d-2024-10）；检验时间单位归一"),
        ("S04", "ACAUSDT", "2024-06-01T00:00:00Z",
         "现货有 1h、无 U 本位永续归档（无 futures klines / 无 metrics）→ 无合约路径"),
        ("S05", "WIFUSDT", "2024-03-05T00:00:00Z",
         "现货上线初期（1h 归档自 2024-03）→ K 线预热不足"),
        ("S06", "JASMYUSDT", "2022-04-20T00:00:00Z",
         "现货 1h 自 2021-11、合约指标自 2022-04-20 → 现货/合约上市错位"),
        ("S07", "ARKMUSDT", "2023-07-28T00:00:00Z",
         "新上市标的的指标覆盖起点；检验起点附近缺失"),
        ("S08", "1INCHUSDT", "2024-06-01T00:00:00Z",
         "现货+合约+metrics 全量覆盖对照"),
        ("S09", "GALAUSDT", "2024-06-01T00:00:00Z",
         "现货+合约+metrics 全量覆盖对照（第二个）"),
        ("S10", "ORDIUSDT", "2023-11-07T00:00:00Z",
         "metrics 覆盖起点当日；检验起点当日能否评分"),
        ("S11", "1000SATSUSDT", "2023-12-12T00:00:00Z",
         "标的名带数字前缀（1000X 合约），检验现货/合约身份映射"),
        ("S12", "FTTUSDT", "2022-12-01T00:00:00Z",
         "现货 1h 归档在 2022-12 起连续缺月（退市/停摆）→ 序列缺口路径"),
    ]
    segments = []
    for sid, sym, start, reason in segs:
        start_ms = parse_utc(start)
        segments.append({
            "id": sid, "symbol": sym, "start_utc": start,
            "points": 48, "interval": "1h",
            "end_utc": ms_to_iso(start_ms + 47 * INTERVAL_MS),
            "selection_reason": reason,
        })
    return {
        "generated_by": "tools/b4_replay_ds.py",
        "method": "STRATEGY_REVIEW.md 第 9.6 节第 1 步（接入核对与小段试跑）",
        "selection_rule": "按格式/时间边界/缺失覆盖选样；不看后续收益、不按涨幅挑月份",
        "constraints": {"max_segments": 12, "max_points_per_segment": 48,
                        "note": "这是接入试验的计算边界，不代表缩小后续研究市场"},
        "scoring": {"weights": WEIGHTS, "max_score": MAX_SCORE, "min_score": MIN_SCORE,
                    "lookback": LOOKBACK, "timeframe": CFG.timeframe,
                    "box_period": CFG.box_period, "ema_period": CFG.ema_period,
                    "atr_period": CFG.atr_period, "volume_multiplier": CFG.volume_multiplier,
                    "source": "binance_box_strategy.WEIGHTS / Config"},
        "segments": segments,
    }


# ---------------------------------------------------------------------------
# 运行
# ---------------------------------------------------------------------------

def _months_for_window(start_ms: int, end_ms: int, warmup_days: int = KLINE_WARMUP_DAYS) -> list[str]:
    a = pd.Timestamp(start_ms - warmup_days * 86_400_000, unit="ms", tz="UTC")
    b = pd.Timestamp(end_ms, unit="ms", tz="UTC")
    out = []
    cur = a.replace(day=1)
    while cur <= b:
        out.append(f"{cur.year:04d}-{cur.month:02d}")
        cur = cur + pd.offsets.MonthBegin(1)
    return sorted(set(out))


def metrics_warmup_days(deriv_lag_hours: int = 0) -> int:
    """metrics 需要读到的过去跨度：**必须覆盖实际请求窗口**（DERIV_LOOKBACK 条 1h）
    再加可得延迟余量，否则 pilot 会少读数据、把"没读到"误报成"数据缺失"（D1 要求）。"""
    return int(math.ceil((DERIV_LOOKBACK + max(deriv_lag_hours, 0)) / 24.0)) + 2


def _days_for_window(start_ms: int, end_ms: int, warmup_days: int = 12) -> list[str]:
    a = pd.Timestamp(start_ms - warmup_days * 86_400_000, unit="ms", tz="UTC")
    b = pd.Timestamp(end_ms, unit="ms", tz="UTC")
    return [d.strftime("%Y-%m-%d") for d in pd.date_range(a.normalize(), b.normalize(), freq="D")]


def peak_working_set_mb() -> float | None:
    """Windows: 进程峰值工作集（GetProcessMemoryInfo）。非 Windows 返回 None。"""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]

        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
        fn = ctypes.windll.psapi.GetProcessMemoryInfo
        fn.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS), wintypes.DWORD]
        fn.restype = wintypes.BOOL
        ok = fn(ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb)
        if not ok:
            return None
        return round(counters.PeakWorkingSetSize / (1024 * 1024), 2)
    except Exception:  # noqa: BLE001
        return None


def run_pilot(manifest: dict, deriv_lag_hours: int = 0, limit_points: int | None = None,
              out_dir: "str | Path | None" = None) -> dict:
    """跑小段并写产物。`out_dir` 显式传入时只写该目录（默认 r2），绝不写 r1 顶层（D5）。"""
    out = Path(out_dir) if out_dir is not None else OUT_DIR
    out.mkdir(parents=True, exist_ok=True)
    guard = NetworkGuard()
    guard.install()
    tracemalloc.start()
    t0 = time.perf_counter()

    m_warmup = metrics_warmup_days(deriv_lag_hours)
    records: list[dict] = []
    raw_refs: list[dict] = []
    per_segment: list[dict] = []
    total_log = ReadLog()
    for seg in manifest["segments"]:
        start_ms = parse_utc(seg["start_utc"])
        n_points = seg["points"] if limit_points is None else min(seg["points"], limit_points)
        end_ms = start_ms + (n_points - 1) * INTERVAL_MS
        symbol = seg["symbol"]

        klog = ReadLog()
        months = _months_for_window(start_ms, end_ms)
        candles = read_spot_1h(symbol, months, klog)

        has_futures = futures_1h_dir(symbol).is_dir()
        has_metrics = metrics_dir(symbol).is_dir()
        mlog = ReadLog()
        days = _days_for_window(start_ms, end_ms, warmup_days=m_warmup) if has_metrics else []
        metrics_df = read_metrics(symbol, days, mlog) if has_metrics else None

        seg_records = []
        for k in range(n_points):
            decision_ms = start_ms + k * INTERVAL_MS
            rec = score_timepoint(symbol, decision_ms, candles, metrics_df, has_futures,
                                  deriv_lag_hours=deriv_lag_hours)
            rec["segment_id"] = seg["id"]
            seg_records.append(rec)
        records.extend(seg_records)

        for log, kind in ((klog, "spot_1h"), (mlog, "metrics")):
            for rel in log.files:
                raw_refs.append({"segment": seg["id"], "kind": kind, "path": rel,
                                 "sha256": _sha256(ROOT / rel)})
        total_log.files.extend(klog.files + mlog.files)
        total_log.compressed_bytes += klog.compressed_bytes + mlog.compressed_bytes
        total_log.uncompressed_bytes += klog.uncompressed_bytes + mlog.uncompressed_bytes
        total_log.rows += klog.rows + mlog.rows

        statuses: dict = {}
        for r in seg_records:
            key = f"kline={r['kline']['status']}|deriv={r['deriv']['status']}"
            statuses[key] = statuses.get(key, 0) + 1
        per_segment.append({
            "id": seg["id"], "symbol": symbol, "points": n_points,
            "has_futures_market": has_futures,
            "has_metrics_archive": has_metrics,
            "metrics_days_read": len(days),
            "kline_files": klog.files, "kline_candles_loaded": len(candles),
            "metrics_files": mlog.files,
            "status_counts": statuses,
            "selected_count": sum(1 for r in seg_records if r["selected"]),
            "screening_status_counts": _count_by(r["screening_status"] for r in seg_records),
            "score_total_min": min((r["score_total"] for r in seg_records
                                    if r["score_total"] is not None), default=None),
            "score_total_max": max((r["score_total"] for r in seg_records
                                    if r["score_total"] is not None), default=None),
        })

    elapsed = time.perf_counter() - t0
    cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    guard.uninstall()

    # 输出
    jsonl_path = out / "scoring_output.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    (out / "raw_refs.json").write_text(
        json.dumps({"note": "实际读取的原始分片及其 SHA256", "refs": raw_refs},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    cost = {
        "out_dir": str(out.relative_to(ROOT)) if str(out).startswith(str(ROOT)) else str(out),
        "network_blocked_attempts": guard.blocked,
        "segments": len(manifest["segments"]),
        "scoring_timepoints": len(records),
        "files_read": len(total_log.files),
        "compressed_bytes_read": total_log.compressed_bytes,
        "uncompressed_bytes_read": total_log.uncompressed_bytes,
        "rows_read": total_log.rows,
        "elapsed_sec": round(elapsed, 3),
        "peak_memory_mb_method": "tracemalloc(Python 分配) + Windows GetProcessMemoryInfo PeakWorkingSetSize",
        "peak_python_traced_mb": round(peak / (1024 * 1024), 2),
        "peak_working_set_mb": peak_working_set_mb(),
        "output_scoring_jsonl_bytes": jsonl_path.stat().st_size,
        "deriv_lag_hours": deriv_lag_hours,
        "metrics_warmup_days": m_warmup,
        "kline_warmup_days": KLINE_WARMUP_DAYS,
        "measured_not_estimated": ["files_read", "compressed_bytes_read", "uncompressed_bytes_read",
                                   "rows_read", "elapsed_sec", "scoring_timepoints",
                                   "output_scoring_jsonl_bytes"],
    }
    (out / "cost.json").write_text(json.dumps(cost, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "segment_summary.json").write_text(
        json.dumps(per_segment, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"records": records, "cost": cost, "per_segment": per_segment, "out_dir": str(out)}


def _count_by(values) -> dict:
    counts: dict = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts


def cmd_coverage() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    guard = NetworkGuard()
    guard.install()
    t0 = time.perf_counter()
    cov = build_coverage()
    symbols = [s["symbol"] for s in build_manifest()["segments"]]
    cov["pilot_symbol_gaps"] = scan_gaps(symbols)
    cov["scan_elapsed_sec"] = round(time.perf_counter() - t0, 3)
    cov["network_blocked_attempts"] = guard.blocked
    cov["peak_working_set_mb"] = peak_working_set_mb()
    cov["cost_note"] = ("全库文件遍历 + 队列 100 万行逐份对账；"
                        "峰值工作集含 100 万条路径索引，属测量值不是估算")
    guard.uninstall()
    (OUT_DIR / "coverage.json").write_text(json.dumps(cov, ensure_ascii=False, indent=1),
                                           encoding="utf-8")
    print(json.dumps({"queue_total": cov["queue"]["total_rows"],
                      "local": cov["local"], "ledger": cov["ledger_sample_check"],
                      "missing_total": cov["queue_vs_local_reconciliation"]["missing_total"],
                      "extra_local_files": cov["queue_vs_local_reconciliation"].get("extra_local_files"),
                      "gaps_spot": len(cov["pilot_symbol_gaps"]["spot_1h_month_gaps"]),
                      "gaps_metrics": len(cov["pilot_symbol_gaps"]["metrics_day_gaps"]),
                      "elapsed_sec": cov["scan_elapsed_sec"],
                      "peak_working_set_mb": cov["peak_working_set_mb"]},
                     ensure_ascii=False, indent=1))


def build_input_contract() -> dict:
    """每个字段的：原始值/单位、规范时间、实际采用点、可取得时间依据。"""
    return {
        "generated_by": "tools/b4_replay_ds.py",
        "principle": "市场观测时间 / 公开可得时间 / 归档下载时间 三者分开；未知就标未知，不填零",
        "spot_klines_1h": {
            "archive": "data/archive_raw/spot/klines/<SYMBOL>/1h/<SYMBOL>-1h-<YYYY-MM>.zip",
            "csv": "无表头（个别月份可能有表头，解析时按首列可解析性判定并跳过表头行）",
            "raw_columns": ["open_time", "open", "high", "low", "close", "volume",
                            "close_time", "quote_volume", "count",
                            "taker_buy_base", "taker_buy_quote", "ignore"],
            "used_columns": ["open_time", "open", "high", "low", "close", "volume"],
            "unit": {"open_time": "epoch（毫秒；同文件可能出现微秒，逐行归一）",
                     "open/high/low/close": "计价币（USDT）",
                     "volume": "基础币数量"},
            "normalized_time": "UTC 毫秒 epoch；µs→ms 规则：数值 > 1e14 视为微秒并除以 1000",
            "adopted_time": "采用「决策时刻已经闭合」的最后一根 1h K 线（open_time + 1h <= 决策时刻）",
            "warmup_gate": {
                "rule": f"按**真实输入经 indicators 后的行数**判定：len(df) < box_period+3 "
                        f"(={CFG.box_period + 3}) 视为预热不足、不评分",
                "note": "占位行（代表形成中那根）计入行数；线上 fetch_ohlcv 也含形成中那根，"
                        "故回放必须把占位行算进去（D2）。缺口会改变实际采用点，"
                        "**不保证 ok 恒为 decision−1h**。",
                "kline_warmup_days_read": KLINE_WARMUP_DAYS,
            },
            "availability_basis": "月分片的发布时刻未记录，只有归档下载时间；本批按"
                                  "「已闭合即可用」的保守口径，不假设分片何时发布",
            "live_equivalent": "public_selection 用 fetch_ohlcv(symbol,'1h',limit=cfg.lookback=240)",
        },
        "futures_metrics_5m": {
            "archive": "data/archive_raw/futures/metrics/<SYMBOL>/no_interval/<SYMBOL>-metrics-<YYYY-MM-DD>.zip",
            "csv": "有表头；5 分钟一行，一天约 288 行",
            "raw_columns": ["create_time", "symbol", "sum_open_interest", "sum_open_interest_value",
                            "count_toptrader_long_short_ratio", "sum_toptrader_long_short_ratio",
                            "count_long_short_ratio", "sum_taker_long_short_vol_ratio"],
            "field_mapping": {
                "oi_value": {"archive_field": "sum_open_interest_value",
                             "live_field": "sumOpenInterestValue（/futures/data/openInterestHist）",
                             "unit": "USDT 名义价值", "scored": True},
                "oi_amount": {"archive_field": "sum_open_interest",
                              "unit": "基础币数量（张/币）", "scored": False,
                              "note": "与价值含义不同；本批只记录，绝不替代价值口径"},
                "ls_top": {"archive_field": "sum_toptrader_long_short_ratio",
                           "live_field": "topLongShortPositionRatio（大户持仓多空比）",
                           "unit": "无量纲比值", "scored": True},
                "ls_top_account": {"archive_field": "count_toptrader_long_short_ratio",
                                   "unit": "无量纲比值", "scored": False,
                                   "note": "大户**账户**比；禁止冒充大户持仓比"},
                "ls_global": {"archive_field": "count_long_short_ratio",
                              "unit": "无量纲比值", "scored": False,
                              "note": "全市场**账户**比；禁止冒充大户持仓比"},
            },
            "normalized_time": "create_time 是字符串 'YYYY-MM-DD HH:MM:SS'，按 UTC 解析为毫秒 epoch",
            "adopted_time": "只取整点行（minute==0）；先按可得截止截断，再取接口窗口（最后 "
                            f"{DERIV_LOOKBACK} 条），然后 OI 按「有限且 >0」过滤、要求有效观测 >= "
                            f"{DERIV_MIN_OI_OBS} 条才允许算 oi_chg；LS 只要求有限（**合法零值保留**）",
            "endpoint_window": {
                "live_limit": DERIV_LOOKBACK,
                "note": "线上 /futures/data/* 的 limit=DERIV_LOOKBACK；回放同样只取最近 N 条，"
                        "超出窗口的旧观测不得参与加分",
            },
            "availability_cutoff": "统一以 decision_ms - lag_hours*1h 为界；采用点、24h/72h 基准、"
                                   "尾部不可用计数、状态全部遵守同一边界（D4）",
            "history_windows": {"oi_chg_1d": "采用点 vs 采用点-24h（同为可用整点观测）",
                                "oi_chg_3d": "采用点 vs 采用点-72h",
                                "ls_chg_3d": "采用点 vs 采用点-72h（差值，仅展示不计分）"},
            "status_semantics": {"ok": "oi_chg_1d 与 oi_chg_3d 均可算（有效 OI 观测 >= 25）且 ls_top 可用",
                                 "partial": "两者之一可用", "failed": "都不可用"},
            "availability_basis": "⚠ 假设：整点观测在整点即可得（deriv_lag_hours=0）。"
                                  "线上端点只保留最近 30 天，归档覆盖更早——属归档更全，不改口径。",
            "not_verified": "未逐点比对 metrics 整点行与线上 /futures/data/* 1h 端点是否数值相等",
        },
        "gate_fields_not_reconstructable": {
            "market_cap": "归档无历史市值序列 → 当时的市值闸门不可重建",
            "eligibility": "归档只证明文件存在 → 当时该现货对是否在交易不可重建",
            "trading_status": "退市/更名（如 KLAY→KAIA）未做身份映射 → 记入未知",
            "consequence": "本批只验证「同一可观察子集（K线+合约）上的评分」，"
                           "不宣称复现了线上全流程；分母不因缺资料消失",
        },
        "time_semantics": {
            "market_observation_time": "K 线/指标的观测时刻（UTC）",
            "publicly_available_time": "未记录；按显式假设处理，不能当作已证明",
            "archive_download_time": "v3 队列完成时间，见 progress_v3.json（2026-10-08T12:24:50Z）",
        },
    }


def cmd_contract() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "input_contract.json").write_text(
        json.dumps(build_input_contract(), ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"input_contract.json -> {OUT_DIR}")


def cmd_manifest() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    man = build_manifest()
    (OUT_DIR / "pilot_manifest.json").write_text(json.dumps(man, ensure_ascii=False, indent=1),
                                                 encoding="utf-8")
    print(f"manifest: {len(man['segments'])} segments -> {OUT_DIR/'pilot_manifest.json'}")


def build_structure_cases() -> dict:
    """真实结构案例：可定位到具体文件与行，作为输入契约的实证依据。"""
    cases: dict = {"note": "全部为本地归档中真实存在的结构案例；未用合成数据冒充历史覆盖"}

    # 1) 毫秒/微秒混用：KLAYUSDT 现货 1d 2024-10
    p = ARCHIVE / "spot" / "klines" / "KLAYUSDT" / "1d" / "KLAYUSDT-1d-2024-10.zip"
    entry = {"path": str(p.relative_to(ROOT)), "exists": p.exists()}
    if p.exists():
        text, name = _zip_csv_text(p)
        lines = [ln for ln in text.splitlines() if ln.strip()]
        rows = []
        for idx, ln in enumerate(lines, 1):
            raw = ln.split(",")[0]
            norm = normalize_epoch_ms(raw)
            rows.append({"line": idx, "raw_open_time": raw, "normalized_ms": norm,
                         "normalized_utc": ms_to_iso(norm)})
        micro = [r for r in rows if r["raw_open_time"].isdigit() and len(r["raw_open_time"]) >= 15]
        entry.update({"csv_member": name, "total_rows": len(rows),
                      "microsecond_rows": micro, "first_row": rows[0] if rows else None,
                      "sha256": _sha256(p)})
    cases["ms_microsecond_mixed"] = entry

    # 2) 合法零值 / 缺失尾部：KLAYUSDT metrics 2024-10-27..29（KLAY→KAIA 过渡期）
    rows = []
    for day in ("2024-10-27", "2024-10-28", "2024-10-29"):
        mp = metrics_path("KLAYUSDT", day)
        if not mp.exists():
            rows.append({"day": day, "exists": False})
            continue
        text, name = _zip_csv_text(mp)
        lines = [ln for ln in text.splitlines() if ln.strip()]
        sample = []
        for ln in lines[1:4]:
            parts = ln.split(",")
            sample.append({"create_time": parts[0], "sum_open_interest": parts[2],
                           "sum_open_interest_value": parts[3],
                           "sum_toptrader_long_short_ratio": parts[5]})
        rows.append({"day": day, "exists": True, "csv_member": name,
                     "data_rows": max(len(lines) - 1, 0), "sample": sample,
                     "sha256": _sha256(mp)})
    cases["zero_and_missing_tail"] = {
        "symbol": "KLAYUSDT", "days": rows,
        "reading": "文件存在（覆盖为绿）但数据行极少且 OI 价值=0、大户持仓比=NaN；"
                   "评分侧必须判为不可用，不得填零计分",
    }

    # 3) 序列缺口：FTTUSDT 现货 1h 缺月（退市/停摆）
    gaps = scan_gaps(["FTTUSDT", "CVCUSDT"])
    cases["sequence_gap"] = gaps

    # 4) 上市边界：首根 1h K 线时间
    listing = {}
    for sym in ("WIFUSDT", "ORDIUSDT", "1000SATSUSDT", "JASMYUSDT", "ARKMUSDT"):
        d = ARCHIVE / "spot" / "klines" / sym / "1h"
        files = sorted(d.glob("*.zip")) if d.is_dir() else []
        if not files:
            continue
        text, _ = _zip_csv_text(files[0])
        first = next((ln for ln in text.splitlines() if ln.strip()), None)
        first_ms = normalize_epoch_ms(first.split(",")[0]) if first else None
        listing[sym] = {"first_file": str(files[0].relative_to(ROOT)),
                        "first_candle_open_utc": ms_to_iso(first_ms)}
    cases["listing_boundary"] = listing

    # 5) 三种多空比口径不同：禁止互相冒充
    sym, day, hour = "AAVEUSDT", "2024-06-01", "12:00:00"
    df = read_metrics(sym, [day])
    entry = {"symbol": sym, "day": day, "hour_utc": hour}
    if df is not None:
        h = df[(df["dt"].dt.minute == 0) & (df["create_time"].astype(str).str.endswith(hour))]
        if len(h):
            r = h.iloc[-1]
            entry.update({
                "sum_toptrader_long_short_ratio(大户持仓比,计分)": _finite_or_none(r[LS_TOP_FIELD]),
                "count_toptrader_long_short_ratio(大户账户比,禁用于计分)": _finite_or_none(r[LS_TOP_ACCOUNT_FIELD]),
                "count_long_short_ratio(全市场账户比,禁用于计分)": _finite_or_none(r[LS_GLOBAL_FIELD]),
            })
    cases["ls_ratio_variants"] = entry

    # 6) OI 数量 vs 价值（同源不同含义）
    entry = {"symbol": sym, "day": day, "hour_utc": hour}
    if df is not None:
        h = df[(df["dt"].dt.minute == 0) & (df["create_time"].astype(str).str.endswith(hour))]
        if len(h):
            r = h.iloc[-1]
            entry.update({"sum_open_interest(数量)": _finite_or_none(r[OI_AMOUNT_FIELD]),
                          "sum_open_interest_value(价值,计分)": _finite_or_none(r[OI_VALUE_FIELD])})
    cases["oi_amount_vs_value"] = entry
    return cases


def cmd_cases() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    guard = NetworkGuard()
    guard.install()
    cases = build_structure_cases()
    cases["network_blocked_attempts"] = guard.blocked
    guard.uninstall()
    (OUT_DIR / "structure_cases.json").write_text(json.dumps(cases, ensure_ascii=False, indent=1),
                                                  encoding="utf-8")
    print(json.dumps({k: (v if not isinstance(v, dict) else "ok") for k, v in cases.items()},
                     ensure_ascii=False, indent=1))


def cmd_pilot(limit_points: int | None, deriv_lag_hours: int) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    man = build_manifest()
    (OUT_DIR / "pilot_manifest.json").write_text(json.dumps(man, ensure_ascii=False, indent=1),
                                                 encoding="utf-8")
    res = run_pilot(man, deriv_lag_hours=deriv_lag_hours, limit_points=limit_points, out_dir=OUT_DIR)
    print(json.dumps(res["cost"], ensure_ascii=False, indent=1))


# ---------------------------------------------------------------------------
# D1–D5 独立复现（合成内存输入；与主代理审核用同一批构造）
# ---------------------------------------------------------------------------

class _FakeFutures:
    """只实现生产用到的两个 /futures/data/* 端点；不联网。

    与 `reports/b4_stage1_review/probes.py` 的假接口同构，用来把回放结果
    与生产 `_fetch_deriv_context_live` 在同一批输入上对照。
    """

    def __init__(self, frame: pd.DataFrame) -> None:
        self.frame = frame

    def market(self, symbol: str) -> dict:
        return {"id": "TESTUSDT"}

    def fapiDataGetOpenInterestHist(self, params: dict) -> list[dict]:
        return [{"timestamp": int(row["dt"].value // 1_000_000),
                 "sumOpenInterestValue": row[OI_VALUE_FIELD]}
                for _, row in self.frame.iterrows()][-params["limit"]:]

    def fapiDataGetTopLongShortPositionRatio(self, params: dict) -> list[dict]:
        return [{"timestamp": int(row["dt"].value // 1_000_000),
                 "longShortRatio": row[LS_TOP_FIELD]}
                for _, row in self.frame.iterrows()][-params["limit"]:]


def _deriv_view(row: dict) -> dict:
    """把回放/生产输出裁成可逐项比较的一小组字段（含 deriv_score 实际得分）。"""
    view = {"status": row.get("status")}
    for key in ("oi_chg_1d", "oi_chg_3d", "ls_top", "oi_as_of", "ls_as_of",
                "oi_stale_tail", "ls_stale_tail"):
        view[key] = _finite_or_none(row.get(key))
    view["score"] = int(B.deriv_score(row.get("oi_chg_1d"), row.get("oi_chg_3d"),
                                      row.get("ls_top"), row.get("ls_chg_3d"))[0])
    return view


def _probe_metrics(offsets) -> pd.DataFrame:
    """与审核反例同构的合成 metrics：24h 内 OI 翻倍，LS 固定 0.8。"""
    T = parse_utc("2024-06-01T00:00:00Z")
    rows = []
    for offset in offsets:
        rows.append({
            "dt": pd.Timestamp(T + offset * INTERVAL_MS, unit="ms", tz="UTC"),
            OI_VALUE_FIELD: 200.0 if offset >= -23 else 100.0,
            OI_AMOUNT_FIELD: 1.0,
            LS_TOP_FIELD: 0.8,
            LS_TOP_ACCOUNT_FIELD: 2.5,
            LS_GLOBAL_FIELD: 2.0,
        })
    return pd.DataFrame(rows)


def _probe_candles(n: int) -> list[list[float]]:
    T = parse_utc("2024-06-01T00:00:00Z")
    return [[float(T - (n - i) * INTERVAL_MS), 100.0, 100.0, 100.0, 100.0, 100.0]
            for i in range(n)]


def build_d1_d5_probes() -> dict:
    """D1–D5 返修后的独立复现：同一批合成输入同时喂回放与生产假接口。

    这是**对抗性**证据：如果回放仍与生产契约不一致，这里会直接显示
    `equal=false` 或边界不符，而不是只靠自述。
    """
    T = parse_utc("2024-06-01T00:00:00Z")
    guard = NetworkGuard()
    guard.install()
    try:
        out: dict = {
            "note": "合成内存输入，不联网；回放 deriv_as_of vs 生产 _fetch_deriv_context_live",
            "contract_source": {
                "deriv_lookback": DERIV_LOOKBACK, "deriv_period": DERIV_PERIOD,
                "min_oi_obs": DERIV_MIN_OI_OBS, "box_period": CFG.box_period,
                "warmup_required_rows": CFG.box_period + 3, "lookback": CFG.lookback,
            },
        }

        def compare_deriv(frame: pd.DataFrame, **kw) -> dict:
            replay = deriv_as_of(frame, T, **kw)
            live = B._fetch_deriv_context_live(_FakeFutures(frame), "TEST/USDT:USDT", CFG)
            rv, lv = _deriv_view(replay), _deriv_view(live)
            return {"replay": rv, "live_fake_endpoint": lv, "equal": rv == lv}

        # D1-a 稀疏 OI：只有 3 个 OI 观测（< 25）→ 两侧都不得算 oi_chg
        out["D1a_sparse_oi_below_min_obs"] = compare_deriv(_probe_metrics([-72, -24, 0]))

        # D1-b 接口窗口：超出 DERIV_LOOKBACK 的旧观测不得参与加分
        old = _probe_metrics(range(-299, 1))
        old[OI_VALUE_FIELD] = [100.0 if o < -220 else (200.0 if o == -220 else 0.0)
                               for o in range(-299, 1)]
        out["D1b_endpoint_window"] = compare_deriv(old)

        # D1-c LS 尾部 inf：生产按「有限」过滤后回退到前一有效点
        inf_frame = _probe_metrics(range(-99, 1)).copy()
        inf_frame.loc[inf_frame.index[-1], LS_TOP_FIELD] = float("inf")
        out["D1c_ls_inf_tail"] = compare_deriv(inf_frame)

        # D1-d LS 合法零值：必须保留（不得改成必须正数）
        zero_ls = _probe_metrics(range(-99, 1)).copy()
        zero_ls[LS_TOP_FIELD] = 0.0
        out["D1d_ls_legal_zero"] = compare_deriv(zero_ls)

        # D4 可得截止：lag=1h 时 T 行尚未可得，加载与否都不得改变输出
        healthy = _probe_metrics(range(-99, 1))
        full = deriv_as_of(healthy, T, lag_hours=1)
        trunc = deriv_as_of(healthy.iloc[:-1], T, lag_hours=1)
        out["D4_delay_cutoff"] = {
            "lag1_with_unavailable_T_row": {
                "oi_stale_tail": full["oi_stale_tail"], "ls_stale_tail": full["ls_stale_tail"],
                "oi_as_of_utc": ms_to_iso(full["oi_as_of"]), "ls_as_of_utc": ms_to_iso(full["ls_as_of"]),
                "status": full["status"]},
            "lag1_without_T_row": {
                "oi_stale_tail": trunc["oi_stale_tail"], "ls_stale_tail": trunc["ls_stale_tail"],
                "oi_as_of_utc": ms_to_iso(trunc["oi_as_of"]), "ls_as_of_utc": ms_to_iso(trunc["ls_as_of"]),
                "status": trunc["status"]},
            "whole_output_equal": full == trunc,
        }

        # D2 K 线预热边界：25 / 26 / 27 根闭合（占位行计入）
        def kline_case(n: int) -> dict:
            candles = _probe_candles(n)
            rec = score_timepoint("TESTUSDT", T, candles, None, False)
            return {"closed_candles": n, "df_rows": rec["kline"]["df_rows"],
                    "replay_status": rec["kline"]["status"],
                    "replay_score_kline": rec["score_kline"]}

        warm = _probe_candles(26)
        warm[-1][2] = warm[-1][4] = 200.0
        rec26 = score_timepoint("TESTUSDT", T, warm, None, False)
        live_input = kline_df_as_of(warm, T)
        out["D2_kline_warmup_boundary"] = {
            "closed_25": kline_case(25),
            "closed_26": {**kline_case(26),
                          "live_input_rows": len(live_input),
                          "live_passes_length_check": len(live_input) >= CFG.box_period + 3,
                          "live_box_score": int(B.box_score(live_input, CFG)[0]),
                          "replay_score_kline_with_breakout": rec26["score_kline"]},
            "closed_27": kline_case(27),
            "required_df_rows": CFG.box_period + 3,
        }

        # D3 未评分不得 selected：K 线不足 + 合约满分
        short = score_timepoint("TESTUSDT", T, _probe_candles(20),
                                _probe_metrics(range(-99, 1)), True)
        out["D3_unscored_not_selected"] = {
            "kline_status": short["kline"]["status"],
            "selected": short["selected"],
            "screening_status": short["screening_status"],
            "score_kline": short["score_kline"],
            "score_deriv": short["score_deriv"],
            "score_total": short["score_total"],
            "threshold_compare": short["threshold_compare"],
            "live_would_skip_before_derivatives": 21 < CFG.box_period + 3,
        }

        # D5 输出目录：默认不得指向 r1 顶层
        out["D5_output_isolation"] = {
            "default_out_dir": str(OUT_DIR.relative_to(ROOT)),
            "r1_dir": str(R1_DIR.relative_to(ROOT)),
            "default_is_r1_top_level": OUT_DIR == R1_DIR,
            "default_inside_r2": OUT_DIR == R1_DIR / "r2",
        }
        out["network_blocked_attempts"] = guard.blocked
    finally:
        guard.uninstall()
    return out


def cmd_probes() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    probes = build_d1_d5_probes()
    (OUT_DIR / "d1_d5_probes.json").write_text(json.dumps(probes, ensure_ascii=False, indent=1),
                                               encoding="utf-8")
    summary = {k: (v.get("equal") if isinstance(v, dict) and "equal" in v else "see json")
               for k, v in probes.items()}
    print(json.dumps(summary, ensure_ascii=False, indent=1))


# ---------------------------------------------------------------------------
# 与 r1 的逐条差异
# ---------------------------------------------------------------------------

def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def build_diff_vs_r1(r1_path: Path | None = None, r2_path: Path | None = None,
                     max_rows_in_md: int = 60) -> dict:
    """逐条比对 r1 与 r2 的 576 时点输出，说明变化来自**输入修复**而非策略改善。"""
    r1_path = Path(r1_path) if r1_path else R1_DIR / "scoring_output.jsonl"
    r2_path = Path(r2_path) if r2_path else OUT_DIR / "scoring_output.jsonl"
    r1 = _load_jsonl(r1_path)
    r2 = _load_jsonl(r2_path)
    key = lambda r: (r.get("segment_id"), r.get("symbol"), r.get("decision_time_utc"))

    r1_by = {key(r): r for r in r1}
    r2_by = {key(r): r for r in r2}
    only_r1 = sorted(set(r1_by) - set(r2_by))
    only_r2 = sorted(set(r2_by) - set(r1_by))

    def r1_screening(rec: dict) -> str:
        if rec.get("selected"):
            return "selected"
        return "selected" if rec.get("kline", {}).get("status") == "ok" else \
            f"skipped:{rec.get('kline', {}).get('status')}"

    tracked = ("kline_status", "kline_score", "score_kline", "score_deriv",
               "score_total", "selected", "deriv_status")
    changed_rows: list[dict] = []
    counts = {field: 0 for field in tracked}
    for k in sorted(set(r1_by) & set(r2_by)):
        a, b = r1_by[k], r2_by[k]
        after = {
            "kline_status": b["kline"]["status"],
            "kline_score": b["kline"]["score"],
            "score_kline": b.get("score_kline"),
            "score_deriv": b.get("score_deriv"),
            "score_total": b.get("score_total"),
            "selected": b.get("selected"),
            "deriv_status": b["deriv"]["status"],
        }
        before = {
            "kline_status": a["kline"]["status"],
            "kline_score": a["kline"]["score"],
            "score_kline": a.get("score_kline"),
            "score_deriv": a.get("score_deriv"),
            "score_total": a.get("score_total"),
            "selected": a.get("selected"),
            "deriv_status": a["deriv"]["status"],
        }
        diff_fields = {f: {"before": before[f], "after": after[f]}
                       for f in tracked if before[f] != after[f]}
        if diff_fields:
            for f in diff_fields:
                counts[f] += 1
            changed_rows.append({
                "key": {"segment": k[0], "symbol": k[1], "decision_time_utc": k[2]},
                "before": before, "after": after, "diff": diff_fields,
                "reason_after": list(b.get("reasons", [])),
                "deriv_window_after": (b.get("deriv") or {}).get("window"),
            })

    def status_histogram(records, key_fn):
        hist: dict = {}
        for r in records:
            hist[key_fn(r)] = hist.get(key_fn(r), 0) + 1
        return dict(sorted(hist.items()))

    out = {
        "note": "r1→r2 的差异来自历史接入修复（D1–D5），不是策略或参数改动；"
                "门槛/权重/生产评分未改。",
        "r1_path": str(r1_path.relative_to(ROOT)) if str(r1_path).startswith(str(ROOT)) else str(r1_path),
        "r2_path": str(r2_path.relative_to(ROOT)) if str(r2_path).startswith(str(ROOT)) else str(r2_path),
        "rows_r1": len(r1), "rows_r2": len(r2),
        "keys_only_in_r1": len(only_r1), "keys_only_in_r2": len(only_r2),
        "keys_only_in_r1_examples": [{"segment": k[0], "symbol": k[1], "utc": k[2]} for k in only_r1[:20]],
        "keys_only_in_r2_examples": [{"segment": k[0], "symbol": k[1], "utc": k[2]} for k in only_r2[:20]],
        "changed_rows": len(changed_rows),
        "changed_field_counts": counts,
        "selected_r1": sum(1 for r in r1 if r.get("selected")),
        "selected_r2": sum(1 for r in r2 if r.get("selected")),
        "selected_that_were_unscored_r1": sum(
            1 for r in r1 if r.get("selected") and r["kline"]["status"] != "ok"),
        "selected_that_are_unscored_r2": sum(
            1 for r in r2 if r.get("selected") and r["kline"]["status"] != "ok"),
        "kline_status_histogram_r1": status_histogram(r1, lambda r: r["kline"]["status"]),
        "kline_status_histogram_r2": status_histogram(r2, lambda r: r["kline"]["status"]),
        "deriv_status_histogram_r1": status_histogram(r1, lambda r: r["deriv"]["status"]),
        "deriv_status_histogram_r2": status_histogram(r2, lambda r: r["deriv"]["status"]),
        "screening_status_histogram_r2": status_histogram(r2, lambda r: r.get("screening_status", "?")),
        "adopted_not_immediate_prev_r1": sum(
            1 for r in r1 if r["kline"]["status"] == "ok"
            and r["field_adoption"]["kline_adopted_candle_utc"] != ms_to_iso(r["decision_ms"] - INTERVAL_MS)),
        "adopted_not_immediate_prev_r2": sum(
            1 for r in r2 if r["kline"]["status"] == "ok"
            and not r["kline"].get("adopted_is_immediate_prev", False)),
        "changed_rows_detail": changed_rows,
        "changed_rows_detail_truncated_in_md": len(changed_rows) > max_rows_in_md,
    }
    return out


def cmd_diff() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    diff = build_diff_vs_r1()
    (OUT_DIR / "diff_vs_r1.json").write_text(json.dumps(diff, ensure_ascii=False, indent=1),
                                             encoding="utf-8")
    lines = [
        "# r1 → r2 逐条差异（576 时点，同一份清单）", "",
        f"- 行数：r1 {diff['rows_r1']} → r2 {diff['rows_r2']}"
        f"（仅 r1 有 {diff['keys_only_in_r1']}，仅 r2 有 {diff['keys_only_in_r2']}）",
        f"- 变化行数：{diff['changed_rows']}",
        f"- selected：r1 {diff['selected_r1']} → r2 {diff['selected_r2']}",
        f"- r1 中「未评分却 selected」：{diff['selected_that_were_unscored_r1']} → "
        f"r2 {diff['selected_that_are_unscored_r2']}",
        f"- K 线状态 r1：`{diff['kline_status_histogram_r1']}`",
        f"- K 线状态 r2：`{diff['kline_status_histogram_r2']}`",
        f"- 合约状态 r1：`{diff['deriv_status_histogram_r1']}`",
        f"- 合约状态 r2：`{diff['deriv_status_histogram_r2']}`",
        f"- 去向（r2）：`{diff['screening_status_histogram_r2']}`",
        f"- 采用点≠decision−1h：r1 {diff['adopted_not_immediate_prev_r1']} → "
        f"r2 {diff['adopted_not_immediate_prev_r2']}",
        "", "## 字段变化计数", "",
        "| 字段 | 变化行数 |", "|---|---:|",
    ]
    for field, count in diff["changed_field_counts"].items():
        lines.append(f"| {field} | {count} |")
    lines += ["", "## 变化行明细（最多 60 条，完整见 diff_vs_r1.json）", "",
              "| 段 | 标的 | 决策(UTC) | 变化字段 | before → after |",
              "|---|---|---|---|---|"]
    for row in diff["changed_rows_detail"][:60]:
        k = row["key"]
        fields = "; ".join(
            f"{f}: {v['before']}→{v['after']}" for f, v in row["diff"].items())
        lines.append(f"| {k['segment']} | {k['symbol']} | {k['decision_time_utc']} | "
                     f"{'; '.join(row['diff'])} | {fields} |")
    (OUT_DIR / "diff_vs_r1.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({k: diff[k] for k in ("rows_r1", "rows_r2", "changed_rows",
                                           "selected_r1", "selected_r2",
                                           "selected_that_were_unscored_r1",
                                           "selected_that_are_unscored_r2")},
                     ensure_ascii=False, indent=1))


# ---------------------------------------------------------------------------
# 哈希：证明旧交付 / 生产文件 / 审核目录没变
# ---------------------------------------------------------------------------

def build_hashes() -> dict:
    baseline_path = OUT_DIR / "r1_baseline_hashes.json"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8")) if baseline_path.exists() else {}
    groups = {
        "r1_frozen_delivery": baseline.get("r1_frozen_delivery", {}),
        "production_files_readonly": baseline.get("production_files_readonly", {}),
        "reviewer_dir_readonly": baseline.get("reviewer_dir_readonly", {}),
    }
    result: dict = {"baseline_path": str(baseline_path.relative_to(ROOT)) if baseline else None,
                    "note": "expected 来自返修前记录的基线；actual 为本次运行后的实测值。",
                    "groups": {}}
    for group, expected in groups.items():
        rows = {}
        for rel, exp in sorted(expected.items()):
            path = ROOT / rel
            act = _sha256(path) if path.exists() else None
            rows[rel] = {"expected": exp, "actual": act, "unchanged": exp == act}
        result["groups"][group] = {
            "files": rows,
            "all_unchanged": all(v["unchanged"] for v in rows.values()) if rows else None,
        }
    result["groups"]["r2_outputs"] = {
        "files": {str(p.relative_to(ROOT)): _sha256(p)
                  for p in sorted(OUT_DIR.rglob("*")) if p.is_file()},
        "all_unchanged": None,
    }
    result["groups"]["tool_sources"] = {
        "files": {rel: _sha256(ROOT / rel) for rel in
                  ("tools/b4_replay_ds.py", "tests/test_b4_replay_ds.py")},
        "all_unchanged": None,
        "note": "本次返修预期修改，故不参与 unchanged 判定。",
    }
    result["all_readonly_unchanged"] = all(
        result["groups"][g]["all_unchanged"] for g in groups)
    return result


def cmd_hashes() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    hashes = build_hashes()
    (OUT_DIR / "hashes.json").write_text(json.dumps(hashes, ensure_ascii=False, indent=1),
                                         encoding="utf-8")
    print(json.dumps({g: v["all_unchanged"] for g, v in hashes["groups"].items()},
                     ensure_ascii=False, indent=1))


def cmd_r2(limit_points: int | None, deriv_lag_hours: int) -> None:
    """返修批：manifest + contract + cases + pilot + probes + diff + hashes。

    **不跑 coverage**：覆盖盘点未改，复用 r1 证据（`reports/b4_stage1_ds/coverage.json`），
    不重做百万路径扫描（任务书第 19 行）。
    """
    cmd_manifest()
    cmd_contract()
    cmd_cases()
    cmd_pilot(limit_points, deriv_lag_hours)
    cmd_probes()
    cmd_diff()
    cmd_hashes()


def main() -> int:
    ap = argparse.ArgumentParser(description="B4 第 1 步：历史接入核对与小段试跑（离线）")
    ap.add_argument("cmd", choices=["coverage", "manifest", "cases", "contract", "pilot",
                                    "probes", "diff", "hashes", "r2", "all"])
    ap.add_argument("--limit-points", type=int, default=None,
                    help="每段评分时点上限（用于冒烟测试；默认按 manifest）")
    ap.add_argument("--deriv-lag-hours", type=int, default=0,
                    help="衍生侧公开可得延迟假设（小时）")
    ap.add_argument("--out-dir", default=None,
                    help="显式输出目录（默认 reports/b4_stage1_ds/r2，不覆盖 r1 冻结产物）")
    args = ap.parse_args()
    if args.out_dir:
        set_out_dir(args.out_dir)
    if args.cmd == "coverage":
        cmd_coverage()
    elif args.cmd == "manifest":
        cmd_manifest()
    elif args.cmd == "cases":
        cmd_cases()
    elif args.cmd == "contract":
        cmd_contract()
    elif args.cmd == "pilot":
        cmd_pilot(args.limit_points, args.deriv_lag_hours)
    elif args.cmd == "probes":
        cmd_probes()
    elif args.cmd == "diff":
        cmd_diff()
    elif args.cmd == "hashes":
        cmd_hashes()
    elif args.cmd == "r2":
        cmd_r2(args.limit_points, args.deriv_lag_hours)
    else:
        cmd_manifest()
        cmd_contract()
        cmd_coverage()
        cmd_cases()
        cmd_pilot(args.limit_points, args.deriv_lag_hours)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
