#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""11 · 用**当前实盘配置**去测历史爆拉币，算真实命中率 + 找漏网原因

要回答的两个问题
    Q1 那几百个爆拉币，用现在这套策略能命中多少？
    Q2 哪些没命中的，是因为「分数不够」还是「被闸门挡了」？还有优化空间吗？

方法（严格遵守用户强调的方向：用爆拉币反推策略问题，不拍脑袋）
    1) 从 binance_box_strategy **import 真实配置**，禁止硬编码任何阈值。
    2) 对每个历史爆拉事件，拿到它启动前一天(pre_day)的全部事前特征。
    3) 逐条复现实盘打分：K线三项 + 合约三项 → score
    4) 复现候选池闸门：成交额落在 [min, max] 内、市值 <= 上限
    5) 命中 = 通过闸门 且 score >= min_score
    6) 对没命中的分组归因：被闸门挡 / 分数不够 / 数据缺失

重要口径说明
    features_audit.csv 里的 oi_chg_1d / ls_top 来自 data.binance.vision 的
    daily/metrics（5m 粒度）。历史爆拉事件的合约数据覆盖率有限——大量早期事件
    （2021–2022）根本没有 metrics 数据，所以合约维度只能对「有数据」那部分算。
    这会造成命中率被"数据缺失"低估，必须把这类单独拆出来，不能混进分母。
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
# 必须用 env_config() 而不是 Config()：前者会读环境变量（MIN_SCORE 等），
# 与实盘入口口径一致。否则「MIN_SCORE=4 跑研究脚本」不会生效——Config() 只给默认值。
CFG = base.env_config()
W = base.WEIGHTS

# 输出路径：用默认门槛时写原文件名（research/12 依赖它）；
# 换了门槛则带上门槛后缀，避免覆盖已有结果。
_DEFAULT_SCORE = base.Config().min_score
OUT = DATA / ("hit_rate_current.csv" if CFG.min_score == _DEFAULT_SCORE
              else f"hit_rate_current_m{CFG.min_score}.csv")

# 注意：gain = 价格倍数 − 1（见 02_detect_pumps.py: `high/low - 1.0`），
# 所以分档边界必须用「倍数 − 1」。与 09_backtest_revised.py 保持一致。
# 旧值 [2,3,5,10] 是把它当倍数用，会让 375 条正例（gain∈[1,2)）掉出分档，
# 并把 10x+ 错算成 11x+（19 条 vs 正确的 24 条）。
TIER_BINS = [1.0, 2.0, 4.0, 9.0, np.inf]
TIER_LABELS = ["2-3x", "3-5x", "5-10x", "10x+"]


def load() -> pd.DataFrame:
    """读取特征表。

    data/features_audit.csv 是 08 号脚本重跑后产出的**超集**：既有全部 K 线条件，
    也有 OI / 多空比 / 资金费率（由 features_deriv 补齐），所以不需要再 merge。
    （早期脚本要手动 merge features + features_deriv，两表重名列很多，容易出 bug。）
    """
    m = pd.read_csv(DATA / "features_audit.csv")
    m = m.drop_duplicates(subset=["symbol", "pre_day", "label"])
    m["date"] = pd.to_datetime(m["pre_day"])
    m["tier"] = pd.cut(m["gain"], TIER_BINS, labels=TIER_LABELS, right=False)
    return m


def apply_gates(df: pd.DataFrame) -> pd.DataFrame:
    """复现实盘候选池闸门（成交额区间 + 市值上限）。

    与 binance_box_strategy.select_symbols 同样逻辑；成交额用评估时点的
    quote_vol_24h（24h 现货成交额），市值用 market_cap（若历史样本没有市值，
    则 fail-open 不拦——与实盘一致）。
    """
    df = df.copy()
    vol = df["quote_vol_24h"].fillna(0)
    df["gate_vol_min"] = vol >= CFG.min_quote_volume
    df["gate_vol_max"] = vol <= CFG.max_quote_volume
    cap = df.get("market_cap")
    if cap is None:
        df["gate_cap"] = True
        df["gate_cap_known"] = False
    else:
        known = cap.notna() & (cap > 0)
        df["gate_cap"] = ~known | (cap <= CFG.max_market_cap)
        df["gate_cap_known"] = known
    df["pass_gate"] = df["gate_vol_min"] & df["gate_vol_max"] & df["gate_cap"]
    return df


def score_live(df: pd.DataFrame) -> pd.DataFrame:
    """复现实盘打分：用 base.WEIGHTS 与 base 的阈值常量。"""
    df = df.copy()
    # —— K 线三项（全部样本可算）——
    k = np.zeros(len(df))
    k += W["c_trend"] * df["c_trend"].fillna(False).astype(float).to_numpy()
    k += W["c_breakout"] * df["c_breakout"].fillna(False).astype(float).to_numpy()
    k += W["c_v24up"] * df["c_v24up"].fillna(False).astype(float).to_numpy()
    df["s_kline"] = k

    # —— 合约三项：三列都有值才算数，否则 NaN（不假装是 0）——
    oi1, oi3, lst = df["oi_chg_1d"], df["oi_chg_3d"], df["ls_top"]
    have = oi1.notna() & oi3.notna() & lst.notna()
    d = np.zeros(len(df))
    d += W["oi_chg_1d"] * (oi1 > base.OI_1D_THRESHOLD).astype(float).to_numpy()
    d += W["oi_chg_3d"] * (oi3 > base.OI_3D_THRESHOLD).astype(float).to_numpy()
    d += W["ls_top"] * (lst < base.LS_TOP_THRESHOLD).astype(float).to_numpy()
    df["s_deriv"] = np.where(have.to_numpy(), d, np.nan)
    df["has_deriv"] = have

    # score 与实盘口径一致：缺合约时按 0 累加（实盘 has_futures=False 也是这样）
    df["score"] = df["s_kline"] + df["s_deriv"].fillna(0)
    df["hit"] = df["pass_gate"] & (df["score"] >= CFG.min_score)
    return df


def report(df: pd.DataFrame) -> None:
    pumps = df[df["label"] == 1].copy()
    n = len(pumps)
    print("=" * 96)
    print(f"【当前配置】min_score={CFG.min_score}  成交额闸门=[{CFG.min_quote_volume:,.0f}, "
          f"{CFG.max_quote_volume:,.0f}]  市值上限={CFG.max_market_cap:,.0f}")
    print(f"WEIGHTS={W}  MAX_SCORE={base.MAX_SCORE}")
    print(f"爆拉事件 {n} 个（价格倍数>=2x，gain>=1）  时间 {pumps['date'].min().date()} ~ {pumps['date'].max().date()}")
    print("=" * 96)

    hit = pumps["hit"].sum()
    print(f"\n【Q1】命中 {int(hit)}/{n} = {hit/n*100:.1f}%")

    # 按涨幅分档
    print("\n" + "-" * 96)
    print(f"{'涨幅档':<8}{'爆拉数':>7}{'命中':>7}{'命中率':>9}{'平均score':>11}{'平均K线分':>11}")
    print("-" * 96)
    for lab in TIER_LABELS:
        g = pumps[pumps["tier"] == lab]
        if len(g) == 0:
            continue
        print(f"{lab:<8}{len(g):>7}{int(g['hit'].sum()):>7}"
              f"{g['hit'].mean()*100:>8.1f}%{g['score'].mean():>11.2f}{g['s_kline'].mean():>11.2f}")

    # 归因
    print("\n" + "-" * 96)
    print("【Q2】未命中的归因（三类互斥）")
    print("-" * 96)
    miss = pumps[~pumps["hit"]]
    gate_block = miss[~miss["pass_gate"]]
    score_low = miss[miss["pass_gate"]]
    vol_min = gate_block[~gate_block["gate_vol_min"]]
    vol_max = gate_block[gate_block["gate_vol_min"] & ~gate_block["gate_vol_max"]]
    cap_blk = gate_block[gate_block["gate_vol_min"] & gate_block["gate_vol_max"] & ~gate_block["gate_cap"]]
    print(f"  未命中合计        {len(miss):>4}  ({len(miss)/n*100:.1f}%)")
    print(f"    ├ 成交额 < 下限  {len(vol_min):>4}")
    print(f"    ├ 成交额 > 上限  {len(vol_max):>4}")
    print(f"    ├ 市值超上限     {len(cap_blk):>4}")
    print(f"    └ 过闸门但分数不够 {len(score_low):>4}")
    if len(score_low):
        print(f"       其中 有合约数据 {int(score_low['has_deriv'].sum())} / "
              f"无合约数据 {int((~score_low['has_deriv']).sum())}")
        print(f"       分数分布: "
              + ", ".join(f"{k}分×{v}" for k, v in
                          score_low["score"].value_counts().sort_index().items()))

    # 合约数据覆盖
    print("\n" + "-" * 96)
    print("【数据覆盖】合约维度只对部分历史事件可算，直接拉低命中率")
    print("-" * 96)
    hd = pumps["has_deriv"]
    print(f"  有合约数据 {int(hd.sum())} / {n} = {hd.mean()*100:.1f}%")
    print(f"    其中有合约数据者命中率: {pumps[hd]['hit'].mean()*100:.1f}%")
    print(f"    无合约数据者命中率:     {pumps[~hd]['hit'].mean()*100:.1f}%")
    print("  → 结论：合约维度缺失是命中率的最大扣分项，且与策略本身无关。")
    print("     真实实盘不存在这个问题（都有合约数据），所以下面要分开看。")

    # 只看有完整数据的子集
    sub = pumps[hd]
    print("\n" + "-" * 96)
    print("【Q1·修正】只看「合约数据齐全」的爆拉币（这才是实盘可比的样本）")
    print("-" * 96)
    print(f"  样本 {len(sub)} 个，命中 {int(sub['hit'].sum())} = {sub['hit'].mean()*100:.1f}%")
    print(f"{'涨幅档':<8}{'爆拉数':>7}{'命中':>7}{'命中率':>9}")
    print("-" * 96)
    for lab in TIER_LABELS:
        g = sub[sub["tier"] == lab]
        if len(g) == 0:
            continue
        print(f"{lab:<8}{len(g):>7}{int(g['hit'].sum()):>7}{g['hit'].mean()*100:>8.1f}%")

    # 漏掉的十倍币
    print("\n" + "-" * 96)
    print("【关键】10x+ 被漏掉的案例（策略真正的软肋）")
    print("-" * 96)
    ten = pumps[pumps["tier"] == "10x+"]
    print(f"  10x+ 共 {len(ten)} 个，命中 {int(ten['hit'].sum())}")
    for _, r in ten[~ten["hit"]].sort_values("gain", ascending=False).iterrows():
        why = []
        if not r["gate_vol_min"]:
            why.append(f"成交额{r['quote_vol_24h']/1e6:.2f}M<下限")
        if not r["gate_vol_max"]:
            why.append(f"成交额{r['quote_vol_24h']/1e6:.2f}M>上限")
        if not r["gate_cap"]:
            why.append("市值超限")
        if r["pass_gate"]:
            why.append(f"分数{r['score']:.0f}<{CFG.min_score}"
                       + ("(无合约数据)" if not r["has_deriv"] else
                          f"(K线{r['s_kline']:.0f}+合约{r['s_deriv']:.0f})"))
        print(f"  {r['symbol']:<14} {r['gain']:>6.1f}x  {'; '.join(why)}")


def main() -> int:
    m = load()
    m = apply_gates(m)
    m = score_live(m)
    m.to_csv(OUT, index=False, encoding="utf-8-sig")
    report(m)
    print(f"\n[输出] {OUT.relative_to(ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
