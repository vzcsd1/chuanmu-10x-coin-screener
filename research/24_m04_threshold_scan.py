#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""24 · M04 杠杆超载当否决：阈值扫描 + 缺失口径（A21 落地）

为什么重做 A22 的那个检验
-------------------------
A22 测 M04 用的是「gate 池 vs 整个 score>=5 池」。但 **`oi_value` 只覆盖 51.9%**，
而且缺失不是随机的——**缺失 ⟺ 这个币没有合约市场**（实测 533/533 完全对齐 `has_deriv=False`）。
这一组的爆拉率是 **85.4%**，有合约那组只有 **53.0%**。

于是「爆拉率 4/4 降」这个结论被**构成效应**污染了：否决规则顺手把那批
「没有合约、爆拉率极高」的样本一起剔掉了，掉的不是杠杆，是「有没有合约」。

**正确做法：只在「OI 可评估」的子集里比**（同分布内的比较），
再单独报告「缺失怎么处理」对池子的影响。两件事必须分开说。

两个要回答的问题
----------------
1. **阈值敏感吗**：50 / 60 / 67 / 75 / 80 分位，哪个区间还站得住？
2. **缺失怎么算**：`pass`（放行但标记，fail-open）还是 `drop`（不放行）？

口径（与 A20/A22 一致）
----------------------
四折 = 2024-01-01 / 2024-07-01 / 2025-01-01 / 2025-07-01，**阈值只从训练段取**；
池子 = `score >= 5`；收益 = `ret_net_7`。

    py -3.10 research/24_m04_threshold_scan.py
    py -3.10 research/24_m04_threshold_scan.py --write
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUT = ROOT / "reports" / "m04_scan_ds"

SPLITS = ["2024-01-01", "2024-07-01", "2025-01-01", "2025-07-01"]
QS = [0.50, 0.60, 0.67, 0.75, 0.80]
MIN_SCORE = 5
MIN_BASE, MIN_GATE = 30, 20


def build() -> pd.DataFrame:
    hr = pd.read_csv(DATA / "hit_rate_current.csv")
    hr["pre_day"] = pd.to_datetime(hr["pre_day"], utc=True)
    fr = pd.read_csv(DATA / "forward_returns.csv")
    fr["pre_day"] = pd.to_datetime(fr["pre_day"], utc=True)
    fr = fr[["symbol", "pre_day", "ret_net_7"]].drop_duplicates(subset=["symbol", "pre_day"])
    df = hr.merge(fr, on=["symbol", "pre_day"], how="left").rename(columns={"ret_net_7": "y_ret"})
    df["oi_qv"] = df["oi_value"] / df["quote_vol_24h"].replace(0, np.nan)
    return df.sort_values("pre_day").reset_index(drop=True)


def stats(sub: pd.DataFrame) -> dict:
    v = pd.to_numeric(sub["y_ret"], errors="coerce").dropna().sort_values()
    return {
        "n": len(sub),
        "爆拉率": float(sub["label"].mean()) if len(sub) else np.nan,
        "ret中位": float(v.median()) if len(v) else np.nan,
        "ret均值": float(v.mean()) if len(v) else np.nan,
        "均值去1": float(v.iloc[:-1].mean()) if len(v) > 1 else np.nan,
    }


# ---------------------------------------------------------------- 第一部分：同分布比较

def scan_honest(df: pd.DataFrame) -> pd.DataFrame:
    """只在「OI 可评估」子集里比：基线 = 池内 has_deriv 的行，gate = 其中低杠杆的行。"""
    rows = []
    for q in QS:
        for sp in SPLITS:
            s = pd.Timestamp(sp, tz="UTC")
            tr, te = df[df["pre_day"] < s], df[df["pre_day"] >= s]
            if len(tr) < 200 or len(te) < 100:
                continue
            thr = tr["oi_qv"].quantile(q)              # 阈值只从训练段取
            pool = te[te["score"] >= MIN_SCORE]
            base = pool[pool["oi_qv"].notna()]          # 只在可评估子集里比
            gate = base[base["oi_qv"] <= thr]
            if len(base) < MIN_BASE or len(gate) < MIN_GATE:
                continue
            b, g = stats(base), stats(gate)
            rows.append({
                "q": q, "split": sp, "阈值": round(float(thr), 4),
                "占_可评估集": len(gate) / len(base),
                "n_base": len(base), "n_gate": len(gate),
                # 爆拉捕获率：gate 里留下的爆拉数 / 基线里的爆拉数
                "捕获率": (len(gate) * g["爆拉率"]) / max(len(base) * b["爆拉率"], 1e-9),
                "爆拉率_基线": b["爆拉率"], "爆拉率_否决": g["爆拉率"],
                "ret中位_基线": b["ret中位"], "ret中位_否决": g["ret中位"],
                "ret均值_基线": b["ret均值"], "ret均值_否决": g["ret均值"],
                "均值去1_基线": b["均值去1"], "均值去1_否决": g["均值去1"],
            })
    d = pd.DataFrame(rows)
    if d.empty:
        return d
    for k, b_ in (("爆拉率", "爆拉率"), ("ret中位", "ret中位"),
                  ("ret均值", "ret均值"), ("均值去1", "均值去1")):
        d[f"Δ{k}"] = d[f"{b_}_否决"] - d[f"{b_}_基线"]
    return d


def summarize(d: pd.DataFrame) -> pd.DataFrame:
    out = []
    for q, g in d.groupby("q"):
        out.append({
            "q": q, "折数": len(g), "占比": g["占_可评估集"].mean(),
            "捕获率": g["捕获率"].mean(),
            "爆拉率↑": int((g["Δ爆拉率"] > 0).sum()),
            "中位↑": int((g["Δret中位"] > 0).sum()),
            "均值↑": int((g["Δret均值"] > 0).sum()),
            "去1↑": int((g["Δ均值去1"] > 0).sum()),
            "Δ爆拉率": g["Δ爆拉率"].mean(), "Δ中位": g["Δret中位"].mean(),
            "Δ均值": g["Δret均值"].mean(), "Δ去1": g["Δ均值去1"].mean(),
        })
    return pd.DataFrame(out).sort_values("q").reset_index(drop=True)


# ---------------------------------------------------------------- 第二部分：缺失口径

def missing_policy(df: pd.DataFrame) -> pd.DataFrame:
    """整池口径：pass（缺失放行）vs drop（缺失剔除）对池子规模与质量的影响。"""
    pool = df[df["score"] >= MIN_SCORE]
    known, miss = pool[pool["oi_qv"].notna()], pool[pool["oi_qv"].isna()]
    rows = []
    for lab, g in (("整池（现状）", pool), ("OI 可评估", known), ("OI 缺失（=无合约）", miss)):
        s = stats(g)
        rows.append({"组": lab, "n": s["n"], "占整池": s["n"] / len(pool),
                     "爆拉率": s["爆拉率"], "ret中位": s["ret中位"], "ret均值": s["ret均值"]})
    return pd.DataFrame(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    df = build()
    print(f"[m04] 样本 {len(df)}（爆拉 {int(df['label'].sum())}）")
    pool = df[df["score"] >= MIN_SCORE]
    print(f"[m04] 池子 {len(pool)}｜oi_qv 覆盖 {pool['oi_qv'].notna().mean():.1%}\n")

    M = missing_policy(df)
    print("=" * 90)
    print("0) 先看清一件事：OI 缺失不是随机的，它等价于「这个币没有合约市场」")
    print("=" * 90)
    print(f"{'组':<18} {'n':>5} {'占整池':>8} {'爆拉率':>8} {'7日中位':>9} {'7日均值':>9}")
    print("-" * 90)
    for r in M.to_dict("records"):
        print(f"{r['组']:<18} {r['n']:>5} {r['占整池']:>7.1%} {r['爆拉率']:>7.1%} "
              f"{r['ret中位']:>8.2%} {r['ret均值']:>8.2%}")
    print()
    print("  → 所以「拿 gate 池去比整个池」是错的：它顺手剔掉了爆拉率最高的一批，")
    print("    掉的不是「高杠杆」，是「有没有合约」。A22 的「爆拉率 4/4 降」是构成效应。")
    print()

    d = scan_honest(df)
    if d.empty:
        print("[m04] 没有可比的折")
        return 1
    S = summarize(d)
    print("=" * 90)
    print("1) 同分布比较（只在 OI 可评估的子集里）：阈值敏感性")
    print("=" * 90)
    print(f"{'分位':>5} {'折数':>4} {'占比':>7} {'捕获率':>7} {'爆拉率↑':>8} {'中位↑':>7} {'均值↑':>7} "
          f"{'去1↑':>6} {'Δ爆拉率':>9} {'Δ中位':>8} {'Δ均值':>8} {'Δ去1':>8}")
    print("-" * 90)
    for r in S.to_dict("records"):
        print(f"{r['q']*100:>4.0f}% {r['折数']:>4} {r['占比']:>6.1%} {r['捕获率']:>6.1%} "
              f"{r['爆拉率↑']:>7}/4 {r['中位↑']:>6}/4 {r['均值↑']:>6}/4 {r['去1↑']:>5}/4 "
              f"{r['Δ爆拉率']*100:>8.2f}pp {r['Δ中位']*100:>7.2f}pp "
              f"{r['Δ均值']*100:>7.2f}pp {r['Δ去1']*100:>7.2f}pp")
    print()

    print("=" * 90)
    print("2) 判定（帕累托口径：每一折 7 日中位不降 **且** 爆拉率不降）")
    print("=" * 90)
    verdict = []
    for q, g in d.groupby("q"):
        both = int(((g["Δret中位"] >= 0) & (g["Δ爆拉率"] >= 0)).sum())
        ok = both == len(g)
        verdict.append((q, both, len(g), g["占_可评估集"].mean(), ok))
        print(f"  q={q*100:>4.0f}%  两者同时不降 {both}/{len(g)}"
              f"｜可评估集内保留 {g['占_可评估集'].mean():.1%}"
              f"  → {'✅ 帕累托通过' if ok else ('⚠ 半数通过' if both >= len(g)/2 else '❌ 不通过')}")
    print()
    best = [v for v in verdict if v[4]]
    if best:
        q0 = max(best, key=lambda x: x[0])
        print(f"  → 站得住的最高阈值是 **q={q0[0]*100:.0f}%**（越松越安全，越紧越激进）。")
    else:
        print("  → 没有任何阈值做到「每一折中位与爆拉率都不降」。")
        print("    **M04 不能当否决用**；它最多是一个「排序/提示」特征。")
    print()
    print("  说明：可评估集内保留比例 ≈ 阈值本身（q=67% 就是留下 67% 的行），")
    print("        所以阈值越高越保守、收益空间越小，但越不容易伤害召回。")
    print()

    if args.write:
        OUT.mkdir(parents=True, exist_ok=True)
        d.to_csv(OUT / "scan_honest.csv", index=False, encoding="utf-8-sig")
        M.to_csv(OUT / "missing_policy.csv", index=False, encoding="utf-8-sig")
        S.to_csv(OUT / "summary.csv", index=False, encoding="utf-8-sig")
        print(f"[write] {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
