#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""06 · 合约侧特征对比：OI 增速 / 多空比 / 资金费率 / 成交额

回答两个问题：
  1. 这些项目当前完全没用的维度，有没有区分度？
  2. 把它们加进评分，能不能把现有规则的召回率拉起来？
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def main():
    d = pd.read_csv(DATA / "features_deriv.csv")
    pump, ctrl = d[d["label"] == 1], d[d["label"] == 0]
    base = len(pump) / len(d)
    print(f"样本：爆拉 {len(pump)} | 对照 {len(ctrl)} | 基准爆拉率 {base*100:.1f}%\n")

    # —— 数值型：中位数对比 ——
    nums = [
        ("OI 市值/名义(USD)", "oi_value"),
        ("OI 1日变化", "oi_chg_1d"),
        ("OI 3日变化", "oi_chg_3d"),
        ("OI 7日变化", "oi_chg_7d"),
        ("OI 未来2日变化", "oi_chg_fwd2d"),
        ("账户多空比", "ls_count"),
        ("大户持仓多空比", "ls_top"),
        ("吃单买卖比", "taker"),
        ("多空比3日变化", "ls_count_chg_3d"),
        ("资金费率(当期)", "funding_last"),
        ("资金费率7日均", "funding_mean_7d"),
        ("7日负费率占比", "funding_neg_ratio_7d"),
        ("7日最低费率", "funding_min_7d"),
    ]
    print(f"{'指标':<20}{'爆拉组中位':>14}{'对照组中位':>14}{'差异':>12}")
    print("-" * 62)
    for name, col in nums:
        if col not in d:
            continue
        a, b = pump[col].median(), ctrl[col].median()
        if not (np.isfinite(a) and np.isfinite(b)):
            continue
        print(f"{name:<20}{a:>14.5g}{b:>14.5g}{a-b:>12.5g}")

    # —— 布尔条件 ——
    d["c_oi_up3d"] = d["oi_chg_3d"] > 0.20
    d["c_oi_up7d"] = d["oi_chg_7d"] > 0.30
    d["c_oi_up1d"] = d["oi_chg_1d"] > 0.10
    d["c_ls_below1"] = d["ls_count"] < 1.0
    d["c_ls_top_below1"] = d["ls_top"] < 1.0
    d["c_ls_falling"] = d["ls_count_chg_3d"] < 0
    d["c_funding_neg7d"] = d["funding_neg_ratio_7d"] > 0
    d["c_funding_neg_heavy"] = d["funding_neg_ratio_7d"] >= 0.3
    d["c_funding_pos_now"] = d["funding_last"] >= 0

    conds = [
        ("OI 3日增>20%", "c_oi_up3d"),
        ("OI 7日增>30%", "c_oi_up7d"),
        ("OI 1日增>10%", "c_oi_up1d"),
        ("账户多空比<1(空头多)", "c_ls_below1"),
        ("大户持仓多空比<1", "c_ls_top_below1"),
        ("多空比3日下降", "c_ls_falling"),
        ("7日内出现过负费率", "c_funding_neg7d"),
        ("负费率占比>=30%", "c_funding_neg_heavy"),
        ("当期费率>=0", "c_funding_pos_now"),
    ]
    pump, ctrl = d[d["label"] == 1], d[d["label"] == 0]
    rows = []
    for name, col in conds:
        p, c = pump[col].mean(), ctrl[col].mean()
        prec = pump[col].sum() / max(1, pump[col].sum() + ctrl[col].sum())
        rows.append({"条件": name, "爆拉命中率": p, "对照命中率": c, "区分度": p - c,
                     "精确率": prec, "提升倍数": prec / base, "命中数": int(pump[col].sum())})
    t = pd.DataFrame(rows).sort_values("区分度", ascending=False)

    print(f"\n{'条件':<22}{'爆拉命中率':>10}{'对照命中率':>11}{'区分度':>9}"
          f"{'精确率':>9}{'提升':>8}{'命中数':>7}")
    print("-" * 82)
    for _, r in t.iterrows():
        print(f"{r['条件']:<22}{r['爆拉命中率']*100:>9.1f}%{r['对照命中率']*100:>10.1f}%"
              f"{r['区分度']*100:>8.1f}{r['精确率']*100:>8.1f}%"
              f"{r['提升倍数']:>7.2f}x{r['命中数']:>7}")

    # —— 组合：控盘结构 + 合约侧 ——
    print("\n【合约侧条件与 K 线条件组合的效果】")
    k = pd.read_csv(DATA / "features.csv")
    key = ["symbol", "pre_day"]
    merged = k.merge(d[key + [c for _, c in conds] + ["oi_chg_3d", "ls_count"]],
                     on=key, how="inner")
    if len(merged):
        m_pump = merged[merged["label"] == 1]
        m_base = len(m_pump) / len(merged)
        print(f"  （可合并样本 {len(merged)}，基准爆拉率 {m_base*100:.1f}%）")
        for label, mask in [
            ("仅 box_score>=3", merged["box_score"] >= 3),
            ("仅 收盘>EMA50", merged["c_trend"]),
            ("box_score>=3 且 OI3日增>20%", (merged["box_score"] >= 3) & merged["c_oi_up3d"]),
            ("box_score>=3 且 多空比<1", (merged["box_score"] >= 3) & merged["c_ls_below1"]),
            ("box_score>=3 且 OI3日增>20% 且 多空比<1",
             (merged["box_score"] >= 3) & merged["c_oi_up3d"] & merged["c_ls_below1"]),
            ("趋势 + OI3日增>20% + 多空比<1",
             merged["c_trend"] & merged["c_oi_up3d"] & merged["c_ls_below1"]),
        ]:
            sel = merged[mask]
            if len(sel) == 0:
                print(f"  {label:<40} 无样本")
                continue
            tp = int((sel["label"] == 1).sum())
            prec = tp / len(sel)
            rec = tp / max(1, len(m_pump))
            print(f"  {label:<40} 选出 {len(sel):>4} | 爆拉 {tp:>3} | "
                  f"精确 {prec*100:5.1f}% | 提升 {prec/m_base:4.2f}x | 召回 {rec*100:5.1f}%")

    t.to_csv(DATA / "deriv_compare.csv", index=False, encoding="utf-8-sig")
    print(f"\n-> {DATA/'deriv_compare.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
