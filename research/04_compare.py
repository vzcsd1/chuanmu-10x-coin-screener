#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""04 · 爆拉组 vs 对照组：逐条件命中率对比

对每个条件 c 计算：
  召回率 P(c | 爆拉)  —— 爆拉前有多少比例满足 c（漏报的反面）
  误报率 P(c | 对照)  —— 平静期有多少比例也满足 c（噪音）
  精确率 P(爆拉 | c)  —— 满足 c 的样本里有多少真的爆拉了
  区分度 = P(c|爆拉) - P(c|对照)
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

BOOL_CONDS = [
    ("箱体宽<=12%", "c_box12"),
    ("已突破箱体", "c_breakout"),
    ("量比>=1.5", "c_vol15"),
    ("收盘>EMA50", "c_trend"),
    ("量能三台阶", "c_stairs"),
    ("4h量增", "c_v4up"),
    ("24h量增", "c_v24up"),
    ("48h波动15-25%", "c_move48_15_25"),
    ("箱体突破信号", "signal_fired"),
]

NUM_COLS = [
    ("箱体宽度%", "box_width_pct"),
    ("量比", "volume_ratio"),
    ("48h波动", "move48"),
    ("箱体持续(小时)", "box_age_h"),
    ("项目box_score", "box_score"),
    ("24h成交额(USD)", "quote_vol_24h"),
    ("4h/前4h量能比", "vol_stairs_ratio"),
]


def main():
    df = pd.read_csv(DATA / "features.csv")
    df = df.dropna(subset=["label"])
    pump = df[df["label"] == 1]
    ctrl = df[df["label"] == 0]
    print(f"样本：爆拉组 {len(pump)} | 对照组 {len(ctrl)} | 基准爆拉率 "
          f"{len(pump)/len(df)*100:.1f}%\n")

    rows = []
    base_rate = len(pump) / len(df)
    for name, col in BOOL_CONDS:
        if col not in df:
            continue
        p = pump[col].mean()
        c = ctrl[col].mean()
        prec = pump[col].sum() / max(1, (pump[col].sum() + ctrl[col].sum()))
        rows.append({"条件": name, "爆拉命中率": p, "对照命中率": c,
                     "区分度": p - c, "精确率": prec,
                     "提升倍数": prec / base_rate,
                     "爆拉命中数": int(pump[col].sum())})
    t = pd.DataFrame(rows).sort_values("区分度", ascending=False)

    def fmt(x):
        return f"{x*100:6.1f}%"
    print("=" * 88)
    print(f"{'条件':<18}{'爆拉命中率':>10}{'对照命中率':>11}{'区分度':>9}"
          f"{'精确率':>9}{'提升':>8}{'命中数':>7}")
    print("-" * 88)
    for _, r in t.iterrows():
        print(f"{r['条件']:<18}{fmt(r['爆拉命中率']):>10}{fmt(r['对照命中率']):>11}"
              f"{r['区分度']*100:>8.1f}{fmt(r['精确率']):>9}"
              f"{r['提升倍数']:>7.2f}x{r['爆拉命中数']:>7}")
    print("=" * 88)
    print(f"（基准爆拉率 {base_rate*100:.1f}%；提升倍数 = 精确率 / 基准率，1.00x 表示无信息量）")

    print("\n【数值型特征对比（中位数）】")
    print(f"{'特征':<18}{'爆拉组':>14}{'对照组':>14}{'倍数':>10}")
    print("-" * 58)
    for name, col in NUM_COLS:
        if col not in df:
            continue
        a, b = pump[col].median(), ctrl[col].median()
        ratio = (a / b) if (b and np.isfinite(b) and b != 0) else float("nan")
        print(f"{name:<18}{a:>14.4g}{b:>14.4g}{ratio:>10.2f}")

    print("\n【现有规则的实际效果】")
    for thr in (3, 4, 5, 6):
        if "box_score" not in df:
            break
        sel = df[df["box_score"] >= thr]
        if len(sel) == 0:
            print(f"  box_score>={thr}: 无样本")
            continue
        tp = int((sel["label"] == 1).sum())
        prec = tp / len(sel)
        rec = tp / max(1, len(pump))
        print(f"  box_score>={thr}: 选出 {len(sel):>4} 个 | 其中爆拉 {tp:>3} | "
              f"精确率 {prec*100:5.1f}% | 召回 {rec*100:5.1f}%")

    # 条件数（旧脚本口径：>=4 条入选）
    hits = df[[c for _, c in BOOL_CONDS if c in df]].sum(axis=1)
    print()
    for thr in (4, 5, 6):
        sel = df[hits >= thr]
        if len(sel) == 0:
            continue
        tp = int((sel["label"] == 1).sum())
        print(f"  命中条件数>={thr}: 选出 {len(sel):>4} | 爆拉 {tp:>3} | "
              f"精确率 {tp/len(sel)*100:5.1f}% | 召回 {tp/max(1,len(pump))*100:5.1f}%")

    # —— 按涨幅分档：越大的行情，条件是否不同？——
    print("\n【按涨幅分档的条件命中率（爆拉组内）】")
    tiers = [(10, "10x+"), (5, "5-10x"), (3, "3-5x"), (2, "2-3x"), (1, "2-3x(全)")]
    print(f"{'条件':<18}" + "".join(f"{n:>10}" for _, n in tiers[:-1]))
    print("-" * 58)
    gp = pump.copy()
    for name, col in BOOL_CONDS:
        if col not in gp:
            continue
        vals = []
        for th, _ in tiers[:-1]:
            sub = gp[gp["gain"] >= th]
            vals.append(f"{sub[col].mean()*100:9.1f}%" if len(sub) else "        -")
        print(f"{name:<18}" + "".join(vals))

    # —— 流动性分档：越小的币是否越容易拉？——
    if "quote_vol_24h" in df and df["quote_vol_24h"].notna().any():
        print("\n【按 24h 现货成交额分档的爆拉率】")
        q = df.dropna(subset=["quote_vol_24h"]).copy()
        edges = [0, 1e5, 5e5, 2e6, 1e7, 1e8, 1e12]
        labels = ["<10万", "10-50万", "50-200万", "200万-1000万", "1000万-1亿", ">1亿"]
        q["tier"] = pd.cut(q["quote_vol_24h"], bins=edges, labels=labels)
        g = q.groupby("tier", observed=True)["label"].agg(["mean", "size"])
        for idx, r in g.iterrows():
            print(f"  {str(idx):<14} 样本 {int(r['size']):>4} | 爆拉率 {r['mean']*100:5.1f}%")

    t.to_csv(DATA / "cond_compare.csv", index=False, encoding="utf-8-sig")
    print(f"\n-> {DATA/'cond_compare.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
