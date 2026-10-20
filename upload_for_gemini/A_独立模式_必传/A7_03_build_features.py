#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""03 · 在"启动前一天"复现项目评分条件（爆拉组 vs 对照组）

关键纪律：
  - 只用评估日及其之前的已收盘 K 线，不含任何未来信息
  - 直接调用项目自带的 binance_box_strategy.indicators / box_score，不另写一套

用法：
  py -3.10 research/03_build_features.py prep     # 生成 1h 下载清单并下载
  py -3.10 research/03_build_features.py run      # 计算特征
"""
from __future__ import annotations

import argparse
import io
import json
import math
import random
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import binance_box_strategy as base  # noqa: E402

DATA = ROOT / "data"
H1 = DATA / "1h"
CDN = "https://data.binance.vision"
KLINE = "data/spot/monthly/klines"
COLS = ["open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore"]

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "chuanmu-research/0.1"})
CFG = base.Config()
PRE_WINDOW_DAYS = 45
CONTROL_PER_SYMBOL = 6
CONTROL_EXCLUDE_DAYS = 45


def _months_before(d: pd.Timestamp, back_days: int) -> list[str]:
    start = (d - pd.Timedelta(days=back_days)).replace(day=1)
    out, cur = [], start
    while cur <= d:
        out.append(f"{cur.year:04d}-{cur.month:02d}")
        cur = (cur + pd.offsets.MonthBegin(1))
    return sorted(set(out))


def _parse_zip(content: bytes):
    z = zipfile.ZipFile(io.BytesIO(content))
    names = [n for n in z.namelist() if n.endswith(".csv")]
    if not names:
        return None
    with z.open(names[0]) as fh:
        raw = fh.read().decode("utf-8", "replace")
    lines = [l for l in raw.splitlines() if l.strip()]
    if lines and not lines[0][:1].isdigit():
        lines = lines[1:]
    if not lines:
        return None
    ncol = len(lines[0].split(","))
    if ncol < 11:
        return None
    use = COLS[: min(ncol, 12)]
    df = pd.read_csv(io.StringIO("\n".join(lines)), header=None, names=use,
                     on_bad_lines="skip", engine="c")
    df = df[[c for c in COLS[:11] if c in df.columns]].copy()
    for c in df.columns:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["open_time", "close"])
    if df.empty:
        return None
    if df["open_time"].iloc[0] > 1e14:
        df["open_time"] = df["open_time"] // 1000
    df["open_time"] = df["open_time"].astype("int64")
    return df


def _download_one(sym: str, month: str) -> bool:
    dst = H1 / f"{sym}-{month}.parquet"
    if dst.exists():
        return True
    url = f"{CDN}/{KLINE}/{sym}/1h/{sym}-1h-{month}.zip"
    try:
        r = SESSION.get(url, timeout=90)
        if r.status_code != 200 or len(r.content) < 200:
            return False
        df = _parse_zip(r.content)
        if df is None or df.empty:
            return False
        df.insert(0, "symbol", sym)
        df.to_parquet(dst, index=False, compression="zstd")
        return True
    except Exception:  # noqa: BLE001
        return False


def cmd_prep(args):
    pumps = json.loads((DATA / "pumps.json").read_text(encoding="utf-8"))
    pumps = [e for e in pumps if e["gain"] >= args.min_gain]
    print(f"[prep] 爆拉事件（>={args.min_gain+1:.0f}x）: {len(pumps)} 个")
    if args.top:
        pumps = pumps[: args.top]
        print(f"[prep] 取前 {len(pumps)} 个")

    tasks: set[tuple[str, str]] = set()
    for e in pumps:
        d = pd.Timestamp(e["pre_day"])
        for m in _months_before(d, PRE_WINDOW_DAYS):
            tasks.add((e["symbol"], m))

    # 对照组：同币种、随机日期、且距任何爆拉窗口 > CONTROL_EXCLUDE_DAYS 天
    by_sym: dict[str, list[pd.Timestamp]] = {}
    for e in pumps:
        by_sym.setdefault(e["symbol"], []).append(pd.Timestamp(e["pre_day"]))
    rng = random.Random(20261004)
    controls = []
    for sym, pre_days in by_sym.items():
        try:
            df = pd.read_parquet(DATA / "1d" / f"{sym}.parquet", columns=["dt"])
        except Exception:  # noqa: BLE001
            continue
        all_days = pd.to_datetime(df["dt"]).dt.tz_localize(None).dt.normalize()
        lo, hi = all_days.min() + pd.Timedelta(days=90), all_days.max() - pd.Timedelta(days=60)
        if lo >= hi:
            continue
        # 先枚举全部合规日期，再均匀抽样 —— 避免拒绝采样效率过低
        span = (hi - lo).days
        valid = []
        for off in range(span + 1):
            cand = lo + pd.Timedelta(days=off)
            if any(abs((cand - p).days) < CONTROL_EXCLUDE_DAYS for p in pre_days):
                continue
            valid.append(cand)
        if not valid:
            continue
        pick = min(CONTROL_PER_SYMBOL, len(valid))
        for cand in rng.sample(valid, pick):
            controls.append({"symbol": sym, "pre_day": str(cand.date()), "gain": 0.0,
                             "is_control": True})
            for m in _months_before(cand, PRE_WINDOW_DAYS):
                tasks.add((sym, m))

    (DATA / "control_days.json").write_text(json.dumps(controls, indent=1, ensure_ascii=False),
                                            encoding="utf-8")
    jobs = [{"symbol": s, "month": m} for s, m in sorted(tasks)]
    (DATA / "jobs_1h.json").write_text(json.dumps(jobs, indent=1, ensure_ascii=False),
                                       encoding="utf-8")
    print(f"[prep] 对照组日期 {len(controls)} 个（每币最多 {CONTROL_PER_SYMBOL} 个）")
    print(f"[prep] 1h 下载任务 {len(jobs):,} 个 -> {DATA/'jobs_1h.json'}")
    print("       下一步： py -3.10 research/00_fetch.py klines1h --jobs data/jobs_1h.json")
    return 0


def _load_1h(sym: str, pre_day: pd.Timestamp) -> pd.DataFrame | None:
    months = _months_before(pre_day, PRE_WINDOW_DAYS)
    frames = []
    for m in months:
        p = H1 / f"{sym}-{m}.parquet"
        if p.exists():
            try:
                frames.append(pd.read_parquet(p))
            except Exception:  # noqa: BLE001
                pass
    if not frames:
        return None
    df = pd.concat(frames, ignore_index=True).drop_duplicates(subset="open_time")
    df = df.sort_values("open_time").reset_index(drop=True)
    df["dt"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    cutoff = pd.Timestamp(pre_day, tz="UTC") + pd.Timedelta(hours=23)
    df = df[df["dt"] <= cutoff].reset_index(drop=True)
    return df if len(df) >= 120 else None


def _features(sym: str, pre_day: pd.Timestamp, label: int) -> dict | None:
    raw = _load_1h(sym, pre_day)
    if raw is None:
        return None
    candles = raw[["open_time", "open", "high", "low", "close", "volume"]].to_numpy(float).tolist()
    df = base.indicators(candles, CFG)
    if len(df) < CFG.box_period + 5:
        return None
    row = df.iloc[-2]                      # 最后一根已收盘
    if any(pd.isna(row[c]) for c in ["box_high", "box_low", "avg_volume", "atr", "ema"]):
        return None

    box_width = (row.box_high - row.box_low) / row.close if row.close else np.nan
    closed = df.iloc[:-1]
    v4 = closed["volume"].tail(4).sum()
    prev4 = closed["volume"].tail(8).head(4).sum()
    v24 = closed["volume"].tail(24).sum()
    prev24 = closed["volume"].tail(48).head(24).sum()
    v0 = closed["volume"].tail(4).sum()
    v1 = closed["volume"].tail(8).head(4).sum()
    v2 = closed["volume"].tail(12).head(4).sum()
    low48, high48 = closed["low"].tail(48).min(), closed["high"].tail(48).max()
    move48 = (high48 - low48) / low48 if low48 else np.nan

    # 箱体已持续多少小时（box_width 连续 <= 12%）
    age = 0
    for j in range(len(df) - 2, max(CFG.box_period, 0), -1):
        r = df.iloc[j]
        if pd.isna(r.box_high) or not r.close:
            break
        if (r.box_high - r.box_low) / r.close <= 0.12:
            age += 1
        else:
            break

    score, details = base.box_score(df, CFG)
    sig = base.signal(sym, df, CFG)

    # 成交额分档用（base.indicators 不保留 quote_volume，直接从原始 1h 取）
    raw_closed = raw.iloc[:-1]
    qv24 = float(raw_closed["quote_volume"].tail(24).sum()) if "quote_volume" in raw_closed else np.nan
    qv4 = float(raw_closed["quote_volume"].tail(4).sum()) if "quote_volume" in raw_closed else np.nan

    return {
        "symbol": sym, "pre_day": str(pre_day.date()), "label": label,
        "price": float(row.close),
        # —— 项目现有条件（可复现部分）——
        "c_box12": bool(box_width <= 0.12),
        "box_width_pct": float(box_width * 100) if np.isfinite(box_width) else None,
        "c_breakout": bool(row.close > row.box_high),
        "c_vol15": bool(row.volume >= row.avg_volume * 1.5),
        "volume_ratio": float(row.volume / row.avg_volume) if row.avg_volume else None,
        "c_trend": bool(row.close > row.ema),
        "c_stairs": bool(v0 > v1 > v2),
        "c_v4up": bool(v4 > prev4),
        "c_v24up": bool(v24 > prev24),
        "c_move48_15_25": bool(np.isfinite(move48) and 0.15 < move48 < 0.25),
        "move48": float(move48) if np.isfinite(move48) else None,
        "box_age_h": int(age),
        "box_score": int(score),
        "signal_fired": bool(sig is not None),
        # —— 量能绝对值（代替市值，做流动性分档）——
        "quote_vol_24h": qv24,
        "quote_vol_4h": qv4,
        "vol_stairs_ratio": float(v0 / v1) if v1 else None,
    }


def cmd_run(args):
    pumps = json.loads((DATA / "pumps.json").read_text(encoding="utf-8"))
    pumps = [e for e in pumps if e["gain"] >= args.min_gain]
    if args.top:
        pumps = pumps[: args.top]
    controls = json.loads((DATA / "control_days.json").read_text(encoding="utf-8"))

    jobs = [(e["symbol"], pd.Timestamp(e["pre_day"]), 1, e["gain"]) for e in pumps]
    jobs += [(c["symbol"], pd.Timestamp(c["pre_day"]), 0, 0.0) for c in controls]
    print(f"[run] 爆拉组 {len(pumps)} + 对照组 {len(controls)} = {len(jobs)} 个评估点")

    rows = []
    for i, (s, d, lab, gn) in enumerate(jobs, 1):
        r = _features(s, d, lab)
        if r:
            r["gain"] = gn
            rows.append(r)
        if i % 100 == 0:
            print(f"   {i}/{len(jobs)} 有效 {len(rows)}")

    out = pd.DataFrame(rows)
    out.to_csv(DATA / "features.csv", index=False, encoding="utf-8-sig")
    print(f"[run] 有效样本 {len(out)} -> {DATA/'features.csv'}")
    print(out["label"].value_counts().to_dict())
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prep")
    p.set_defaults(func=cmd_prep)
    p.add_argument("--min-gain", dest="min_gain", type=float, default=1.0)
    p.add_argument("--top", type=int, default=0)
    p = sub.add_parser("run")
    p.set_defaults(func=cmd_run)
    p.add_argument("--min-gain", dest="min_gain", type=float, default=1.0)
    p.add_argument("--top", type=int, default=0)
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
