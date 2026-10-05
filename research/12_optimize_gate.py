#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""12 · 反推优化：当前门槛是不是把「K线=5」这一整类有信息的样本误杀了？

发现（见 11 号脚本）
    K线分=5 意味着「收盘>EMA50」成立，但没有突破箱体、也没有 24h 量增。
    这一组的历史爆拉率是 **68.0%**（282 爆拉 / 133 对照），明显高于全样本基准 58.6%。
    也就是说 K线=5 本身携带正信息，不该被判为"不够格"。

    但当时 min_score=6 把所有 K线=5 的样本一刀切掉，除非合约侧能补分。
    （2026-10-05：门槛已改为 5；本脚本保留 4/5/6 三档对照，供重新评估用。）
    结果：282 个 K线=5 的爆拉币里只有 27 个命中（9.6%）。

问题
    这是"门槛设错"还是"权重设错"？
    - 如果是**权重**问题（趋势 5 分本身给少了），应该调权重
    - 如果是**门槛**问题（6 分是一刀切的错误分界），应该调门槛

方法（沿用用户强调的方向：用爆拉币反推，不拍脑袋）
    在**同一子集**上比较所有候选口径的 精确率 / 召回 / 提升倍数，
    并分别报告「有合约数据」和「无合约数据」，避免用缺失数据污染结论。
    提升倍数 = 精确率 / 该子集基准率。**它不是万能换算尺**：同样识别能力下，
    基准率（正例占比）越低，提升倍数越大——正例占比从 50% 降到 10%，提升倍数可以
    从约 1.6x 变成约 3.08x，而策略并没有变强。**跨口径比较时必须同时核对样本构成。**
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import binance_box_strategy as base  # noqa: E402

DATA = ROOT / "data"


def load() -> pd.DataFrame:
    m = pd.read_csv(DATA / "hit_rate_current.csv")
    return m


def metrics(sel: pd.DataFrame, pool: pd.DataFrame) -> dict:
    """在 pool 子集上评估 sel 这个选择规则。"""
    n_pump = int((pool["label"] == 1).sum())
    if len(sel) == 0:
        return {"样本": 0, "命中爆拉": 0, "精确率": np.nan, "提升": np.nan, "召回": np.nan}
    tp = int((sel["label"] == 1).sum())
    br = pool["label"].mean()
    return {"样本": len(sel), "命中爆拉": tp, "精确率": tp / len(sel),
            "提升": (tp / len(sel)) / br if br else np.nan,
            "召回": tp / max(1, n_pump)}


def row(name: str, sel: pd.DataFrame, pool: pd.DataFrame, scope: str) -> dict:
    r = metrics(sel, pool)
    r.update({"口径": scope, "规则": name})
    return r


def main() -> int:
    m = load()
    pumps = m[m["label"] == 1]
    print("=" * 104)
    print("反推优化：找被误杀的样本类")
    print("=" * 104)

    scopes = {
        "全样本": m,
        "有合约": m[m["has_deriv"]],
        "无合约": m[~m["has_deriv"]],
    }
    for name, pool in scopes.items():
        br = pool["label"].mean()
        n_p = int((pool["label"] == 1).sum())
        print(f"  {name:<8} 样本 {len(pool):>5}  爆拉 {n_p:>4}  基准爆拉率 {br*100:.1f}%")

    # ---- 候选规则 ----
    print("\n" + "=" * 104)
    print("方案对比（均先过候选池闸门 pass_gate）")
    print("=" * 104)
    rows = []
    rules = [
        ("score>=6（旧门槛）", lambda d: d["score"] >= 6),
        ("score>=5（当前门槛）", lambda d: d["score"] >= 5),
        ("score>=4（待验证）", lambda d: d["score"] >= 4),
        ("K线>=5", lambda d: d["s_kline"] >= 5),
        ("K线>=5 且 (K线>=6 或 有合约)", lambda d: (d["s_kline"] >= 5) &
         ((d["s_kline"] >= 6) | d["has_deriv"])),
        ("K线>=5 且 合约分>0", lambda d: (d["s_kline"] >= 5) & (d["s_deriv"].fillna(0) > 0)),
        ("K线>=6", lambda d: d["s_kline"] >= 6),
        ("K线>=6 或 (K线=5 且 合约>=4)", lambda d: (d["s_kline"] >= 6) |
         ((d["s_kline"] == 5) & (d["s_deriv"].fillna(0) >= 4))),
    ]
    print(f"{'口径':<8}{'规则':<34}{'样本':>7}{'命中爆拉':>9}{'精确率':>9}{'提升':>8}{'召回':>8}")
    print("-" * 104)
    for scope, pool in scopes.items():
        gated = pool[pool["pass_gate"]]
        for rname, fn in rules:
            sel = gated[fn(gated)]
            r = row(rname, sel, pool, scope)
            rows.append(r)
            if r["样本"] == 0:
                print(f"{scope:<8}{rname:<34}{'0':>7}")
                continue
            print(f"{scope:<8}{rname:<34}{r['样本']:>7}{r['命中爆拉']:>9}"
                  f"{r['精确率']*100:>8.1f}%{r['提升']:>7.2f}x{r['召回']*100:>7.1f}%")
        print("-" * 104)
    pd.DataFrame(rows).to_csv(DATA / "optimize_thresholds.csv", index=False, encoding="utf-8-sig")

    # ---- 逐 K 线分档：信息量 ----
    print("\n" + "=" * 104)
    print("K 线分每一档的信息量（爆拉率 = 该档爆拉数 / 该档总数）")
    print("=" * 104)
    print(f"{'K线分':>6}{'爆拉':>7}{'对照':>7}{'合计':>7}{'该档爆拉率':>12}{'相对基准':>11}")
    br_all = m["label"].mean()
    for k in sorted(m["s_kline"].unique()):
        g = m[m["s_kline"] == k]
        a, b = int((g["label"] == 1).sum()), int((g["label"] == 0).sum())
        if a + b == 0:
            continue
        print(f"{k:>6.0f}{a:>7}{b:>7}{a+b:>7}{a/(a+b)*100:>11.1f}%"
              f"{(a/(a+b)/br_all):>10.2f}x")
    print(f"{'基准':>6}{int((m['label']==1).sum()):>7}{int((m['label']==0).sum()):>7}"
          f"{len(m):>7}{br_all*100:>11.1f}%{1.0:>10.2f}x")

    # ---- K线=5 细分：能不能事前再切开 ----
    print("\n" + "=" * 104)
    print("K 线=5 内部能不能再切开？（用事前可得的连续量做细分）")
    print("=" * 104)
    k5 = m[(m["s_kline"] == 5) & m["pass_gate"]].copy()
    print(f"K线=5 且过闸门: 爆拉 {int((k5['label']==1).sum())} / 对照 "
          f"{int((k5['label']==0).sum())}，爆拉率 {k5['label'].mean()*100:.1f}%")
    for col, lab, bins in [
        ("ret_30d", "前30日涨幅", [-np.inf, -0.15, -0.05, 0.05, 0.15, np.inf]),
        ("dd_from_60d_high", "距60日高点", [-np.inf, -0.35, -0.25, -0.15, -0.05, np.inf]),
        ("ret_7d", "前7日涨幅", [-np.inf, -0.05, 0.0, 0.05, 0.10, np.inf]),
        ("quote_vol_24h", "24h成交额", [0, 1e6, 3e6, 1e7, 3e7, np.inf]),
        ("oi_chg_1d", "OI 1日增", [-np.inf, 0.0, 0.05, 0.10, 0.20, np.inf]),
    ]:
        if col not in k5.columns:
            continue
        s = k5[k5[col].notna()]
        if len(s) < 30:
            print(f"  {lab}: 有效样本不足({len(s)})")
            continue
        g = s.groupby(pd.cut(s[col], bins, right=False), observed=True)
        print(f"  {lab}:")
        for iv, gg in g:
            a, b = int((gg["label"] == 1).sum()), int((gg["label"] == 0).sum())
            if a + b < 8:
                continue
            print(f"    {str(iv):<24} n={a+b:>4} 爆拉{a:>4} 爆拉率{a/(a+b)*100:>6.1f}%")

    print("\n[输出] data/optimize_thresholds.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
