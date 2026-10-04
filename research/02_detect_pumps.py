#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""02 · 从全量日线中检测"爆拉事件"

定义（刻意避免上帝视角）：
  - 波段低点 T：峰前 60 天内的最低 low
  - 峰值 P：30 日滚动涨幅创局部新高的那一天（±10 天窗口内最高）
  - 涨幅 = high[P] / low[T] - 1
  - 启动日 I：T 之后第一根"收盘较 T 收盘涨 >= 15%"的 K 线
  - 评估日 = I 的前一天（最后一根已收盘 K 线）—— 策略只能看到这一天及之前的数据

输出 data/pumps.json
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RAW = DATA / "1d"
OUT = DATA / "pumps.json"

TIERS = [(10.0, "10x+"), (5.0, "5-10x"), (3.0, "3-5x"), (2.0, "2-3x"), (1.0, "1-2x")]
MIN_GAIN = 1.0          # 最低门槛：翻倍（放宽后）
LOOKBACK_TROUGH = 60
IGNITE_MOVE = 0.15      # 启动判定：收盘较低点收盘 +15%


def find_events(sym: str, df: pd.DataFrame) -> list[dict]:
    df = df.sort_values("open_time").reset_index(drop=True)
    n = len(df)
    if n < LOOKBACK_TROUGH + 30:
        return []
    close = df["close"].to_numpy(float)
    high = df["high"].to_numpy(float)
    low = df["low"].to_numpy(float)
    dt = df["dt"].to_numpy()

    # 30 日滚动涨幅（用于定位峰值）
    ret30 = np.full(n, np.nan)
    ret30[30:] = close[30:] / close[:-30] - 1.0

    events: list[dict] = []
    i = LOOKBACK_TROUGH
    while i < n - 1:
        # 局部峰值：±10 天窗口内 ret30 最高，且达到门槛
        lo_w, hi_w = max(30, i - 10), min(n, i + 11)
        if np.isnan(ret30[i]) or ret30[i] < MIN_GAIN:
            i += 1
            continue
        seg = ret30[lo_w:hi_w]
        if np.nanmax(seg) > ret30[i]:
            i += 1
            continue

        # 波段低点
        t_lo = max(0, i - LOOKBACK_TROUGH)
        t = int(np.argmin(low[t_lo:i + 1])) + t_lo
        if t >= i:
            i += 1
            continue
        gain = high[i] / low[t] - 1.0
        if gain < MIN_GAIN:
            i += 1
            continue

        # 启动日：T 之后第一根收盘较 T 收盘 +15%
        base = close[t]
        ig = None
        for j in range(t + 1, i + 1):
            if close[j] >= base * (1 + IGNITE_MOVE):
                ig = j
                break
        if ig is None or ig < 1:
            i += 1
            continue

        days = (dt[i] - dt[t]) / np.timedelta64(1, "D")
        events.append({
            "symbol": sym,
            "trough_date": str(pd.Timestamp(dt[t]).date()),
            "trough_price": float(low[t]),
            "ignite_date": str(pd.Timestamp(dt[ig]).date()),
            "peak_date": str(pd.Timestamp(dt[i]).date()),
            "peak_price": float(high[i]),
            "gain": float(gain),
            "days_to_peak": int(days),
            "pre_day": str(pd.Timestamp(dt[ig - 1]).date()),
            "pre_close": float(close[ig - 1]),
        })
        i = i + 20          # 跳过重叠窗口
    return events


def main():
    files = sorted(RAW.glob("*.parquet"))
    print(f"[detect] 读入 {len(files)} 个币的日线")
    all_events: list[dict] = []
    for k, p in enumerate(files, 1):
        try:
            df = pd.read_parquet(p)
        except Exception as exc:  # noqa: BLE001
            print(f"  !! {p.stem}: {exc}")
            continue
        if "dt" not in df.columns:
            ot = pd.to_numeric(df["open_time"], errors="coerce").to_numpy(float)
            # 逐行归一：微秒量级（>1e14）除以 1000，兼容同一文件混用毫秒/微秒
            ot = np.where(ot > 1e14, ot / 1000.0, ot)
            df["open_time"] = ot
            df = df[(ot >= 1.4e12) & (ot <= 2.1e12)]      # 2014-06 ~ 2036
            df["dt"] = pd.to_datetime(df["open_time"].astype("int64"), unit="ms", utc=True)
        df = df[["open_time", "high", "low", "close", "dt"]]
        ev = find_events(p.stem, df)
        all_events.extend(ev)
        if k % 150 == 0:
            print(f"  ... {k}/{len(files)} 已发现 {len(all_events)} 个事件")

    all_events.sort(key=lambda e: -e["gain"])
    OUT.write_text(json.dumps(all_events, indent=1, ensure_ascii=False), encoding="utf-8")

    print(f"\n[detect] 合计 {len(all_events)} 个爆拉事件（>=翻倍）-> {OUT}")
    print("\n分档统计：")
    for th, name in TIERS:
        c = sum(1 for e in all_events if e["gain"] >= th)
        print(f"   >= {name:<7} {c:>4} 个")
    print(f"\n覆盖币种 {len({e['symbol'] for e in all_events})} 个")
    yrs = {}
    for e in all_events:
        yrs[e["peak_date"][:4]] = yrs.get(e["peak_date"][:4], 0) + 1
    print("按年份：", dict(sorted(yrs.items())))
    print("\n涨幅前 25：")
    for e in all_events[:25]:
        print(f"   {e['symbol']:<14} {e['pre_day']} 起 | {e['trough_date']}~{e['peak_date']}"
              f" | {e['gain']+1:>7.2f}x | {e['days_to_peak']:>3}天")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
