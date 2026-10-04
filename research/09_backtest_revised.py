#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""09 · 反向回测：修订后的策略能否在暴涨前识别出历史爆拉币

要回答的问题
    权重全改之后，用这套新评分去测历史上那些爆拉币「暴涨前」的数据，
    到底对不对得上？（能不能在启动之前就把它们挑出来）

数据（只读本地，不联网）
    data/features.csv        评估时点(pre_day)的 K 线条件，爆拉组 + 平静期对照组
    data/features_deriv.csv  同一时点的 OI 增速 / 多空比 / 资金费率
    两者按 (symbol, pre_day) 合并。全部是「启动前一天」的已收盘数据，无未来信息。

评分
    直接 import binance_box_strategy.WEIGHTS —— 与实盘同一真源，杜绝两套口径。
    K 线侧：收盘>EMA50(+5) / 突破箱体(+2) / 24h量增(+1)     满分 8
    合约侧：OI1日增>10%(+4) / OI3日增>20%(+3) / 大户持仓多空比<1(+3)
            / 多空比3日下降(+1)                              满分 11
    合计满分 19。缺合约数据的样本只算 K 线分，并在表中单独标注。

输出
    data/backtest_scores.csv      每个样本的新旧分数、标签、涨幅
    data/backtest_thresholds.csv  阈值扫描：精确率 / 召回 / 提升倍数
    data/backtest_tiers.csv       按涨幅分档的命中率（行情越大是否越能识别）
    data/backtest_cases.csv       个案明细（TUT / ALPACA / SYN ...）
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import binance_box_strategy as base  # noqa: E402

DATA = ROOT / "data"
SPLIT = pd.Timestamp("2025-01-01")
W = base.WEIGHTS

KLINE_KEYS = ["c_trend", "c_breakout", "c_v24up"]
DERIV_KEYS = ["oi_chg_1d", "oi_chg_3d", "ls_top"]
KLINE_MAX = sum(W[k] for k in KLINE_KEYS)          # 8
DERIV_MAX = sum(W[k] for k in DERIV_KEYS)          # 10

TIER_BINS = [1.0, 2.0, 4.0, 9.0, np.inf]
TIER_LABELS = ["2-3x", "3-5x", "5-10x", "10x+"]


def load() -> pd.DataFrame:
    k = pd.read_csv(DATA / "features.csv")
    d = pd.read_csv(DATA / "features_deriv.csv")
    d = d.drop(columns=[c for c in ["label", "gain"] if c in d.columns])
    d = d.drop_duplicates(subset=["symbol", "pre_day"])
    k = k.drop_duplicates(subset=["symbol", "pre_day", "label"])
    m = k.merge(d, on=["symbol", "pre_day"], how="left")
    m["date"] = pd.to_datetime(m["pre_day"])
    return m


def score_kline(df: pd.DataFrame) -> np.ndarray:
    """K 线侧评分（全部样本可算）。"""
    s = np.zeros(len(df))
    s += W["c_trend"] * df["c_trend"].fillna(False).astype(float).to_numpy()
    s += W["c_breakout"] * df["c_breakout"].fillna(False).astype(float).to_numpy()
    s += W["c_v24up"] * df["c_v24up"].fillna(False).astype(float).to_numpy()
    return s


def score_deriv(df: pd.DataFrame) -> np.ndarray:
    """合约侧评分；三项数据不全的样本返回 NaN（不假装它是 0 分）。

    注意：原先还含「多空比 3 日下降」。把口径从"全市场账户比"对齐到实盘用的
    "大户持仓比"之后，实测区分度只剩 +1.5pp（噪音），已移出评分。
    """
    oi1, oi3, lst = df["oi_chg_1d"], df["oi_chg_3d"], df["ls_top"]
    have = oi1.notna() & oi3.notna() & lst.notna()
    s = np.zeros(len(df))
    s += W["oi_chg_1d"] * (oi1 > base.OI_1D_THRESHOLD).astype(float).to_numpy()
    s += W["oi_chg_3d"] * (oi3 > base.OI_3D_THRESHOLD).astype(float).to_numpy()
    s += W["ls_top"] * (lst < base.LS_TOP_THRESHOLD).astype(float).to_numpy()
    return np.where(have.to_numpy(), s, np.nan)


def add_scores(m: pd.DataFrame) -> pd.DataFrame:
    m = m.copy()
    m["score_kline"] = score_kline(m)
    m["score_deriv"] = score_deriv(m)
    m["score_full"] = m["score_kline"] + m["score_deriv"].fillna(0)
    m["has_deriv"] = m["score_deriv"].notna()
    m["tier"] = pd.cut(m["gain"].where(m["label"] == 1), TIER_BINS, labels=TIER_LABELS,
                       right=False)
    return m


def evaluate(df: pd.DataFrame, mask: pd.Series, name: str, scope: str) -> dict:
    sel = df[mask]
    n_pump = int((df["label"] == 1).sum())
    base_rate = df["label"].mean()
    if len(sel) == 0:
        return {"口径": scope, "规则": name, "样本": 0}
    tp = int((sel["label"] == 1).sum())
    prec = tp / len(sel)
    return {"口径": scope, "规则": name, "样本": len(sel), "命中爆拉": tp,
            "精确率": prec, "提升倍数": prec / base_rate if base_rate else np.nan,
            "召回率": tp / max(1, n_pump), "基准率": base_rate}


def main() -> int:
    m = load()
    m = add_scores(m)
    m.to_csv(DATA / "backtest_scores.csv", index=False, encoding="utf-8-sig")

    pump = m[m["label"] == 1]
    ctrl = m[m["label"] == 0]
    print("=" * 100)
    print(f"样本 {len(m)}（爆拉 {len(pump)} / 对照 {len(ctrl)}）  "
          f"基准爆拉率 {m['label'].mean()*100:.1f}%")
    print(f"其中带合约侧数据的 {int(m['has_deriv'].sum())} 个"
          f"（爆拉 {int(pump['has_deriv'].sum())} / 对照 {int(ctrl['has_deriv'].sum())}）")
    print(f"时间范围 {m['date'].min().date()} ~ {m['date'].max().date()}")

    # —— 1) 分数分布：爆拉组 vs 对照组 ——
    print("\n" + "=" * 100)
    print("【一】分数分布 —— 爆拉组是不是普遍比对照组高？")
    print("-" * 100)
    print(f"{'口径':<22}{'组':<8}{'均值':>8}{'中位':>8}{'25分位':>9}{'75分位':>9}{'最高':>8}")
    for col, tag, mx in (("score_kline", f"仅K线(满分{KLINE_MAX})", KLINE_MAX),
                         ("score_full", f"完整(满分{KLINE_MAX + DERIV_MAX})", KLINE_MAX + DERIV_MAX)):
        for name, d in (("爆拉", pump), ("对照", ctrl)):
            v = d[col]
            print(f"{tag:<22}{name:<8}{v.mean():>8.2f}{v.median():>8.1f}"
                  f"{v.quantile(.25):>9.1f}{v.quantile(.75):>9.1f}{v.max():>8.0f}")

    # —— 2) 阈值扫描 ——
    print("\n" + "=" * 100)
    print("【二】阈值扫描 —— 分数 >= 门槛时，选出来的币里有多少真会爆拉？")
    print("-" * 100)
    rows = []
    print(f"{'口径':<12}{'门槛':>5}{'样本':>7}{'命中爆拉':>9}{'精确率':>9}{'提升':>8}{'召回':>8}")
    print("-" * 100)
    for scope, col, mx in (("仅K线", "score_kline", KLINE_MAX),
                           ("完整", "score_full", KLINE_MAX + DERIV_MAX)):
        for th in range(mx + 1):
            r = evaluate(m, m[col] >= th, f">= {th}", scope)
            rows.append(r)
            if r["样本"] == 0:
                continue
            print(f"{scope:<12}{th:>5}{r['样本']:>7}{r['命中爆拉']:>9}"
                  f"{r['精确率']*100:>8.1f}%{r['提升倍数']:>7.2f}x{r['召回率']*100:>7.1f}%")
        print("-" * 100)
    pd.DataFrame(rows).to_csv(DATA / "backtest_thresholds.csv", index=False, encoding="utf-8-sig")

    # —— 2.5) 合约侧增量检验 ——
    # 关键问题：新加的 OI / 多空比到底有没有用？
    # 必须在"同样有合约数据"的子集上比较，否则拿不到合约数据的样本会被
    # 当成 0 分，人为压低，两个口径就不可比了。
    print("\n【二·补】合约侧增量检验 —— 在同样有合约数据的子集上比较两个口径")
    print("-" * 100)
    sub = m[m["has_deriv"]].copy()
    auc_rows = []
    print(f"子集 {len(sub)} 样本（爆拉 {int((sub['label']==1).sum())} / "
          f"对照 {int((sub['label']==0).sum())}），基准率 {sub['label'].mean()*100:.1f}%")
    for tag, dd in (("全样本", m), ("有合约数据子集", sub)):
        y = dd["label"].to_numpy()
        auc_k = roc_auc_score(y, dd["score_kline"].to_numpy())
        auc_f = roc_auc_score(y, dd["score_full"].to_numpy())
        auc_rows.append({"集合": tag, "样本": len(dd),
                         "仅K线AUC": auc_k, "完整AUC": auc_f, "AUC增量": auc_f - auc_k})
        print(f"  {tag:<14} n={len(dd):<5} 仅K线 AUC {auc_k:.3f} | "
              f"完整 AUC {auc_f:.3f} | 增量 {auc_f - auc_k:+.3f}")
    print("  （AUC 0.5 = 随机；越高说明排序能力越强。增量 >0 表示合约条件确实带来信息）")

    print(f"\n  {'门槛':>5}{'仅K线精确':>11}{'仅K线召回':>11}{'完整精确':>11}{'完整召回':>11}"
          f"{'精确增量':>11}")
    print("  " + "-" * 60)
    for th in (5, 6, 7, 8, 10):
        a = evaluate(sub, sub["score_kline"] >= th, f"K>={th}", "同子集")
        b = evaluate(sub, sub["score_full"] >= th, f"F>={th}", "同子集")
        if a["样本"] == 0 or b["样本"] == 0:
            continue
        print(f"  {th:>5}{a['精确率']*100:>10.1f}%{a['召回率']*100:>10.1f}%"
              f"{b['精确率']*100:>10.1f}%{b['召回率']*100:>10.1f}%"
              f"{(b['精确率']-a['精确率'])*100:>+10.1f}%")
    pd.DataFrame(auc_rows).to_csv(DATA / "backtest_auc.csv", index=False, encoding="utf-8-sig")

    # —— 3) 与改之前的原规则对比 ——
    print("\n【三】与改之前的原规则对比（原 box_score 口径，满分 6）")
    print("-" * 100)
    print(f"{'规则':<30}{'样本':>7}{'命中爆拉':>9}{'精确率':>9}{'提升':>8}{'召回':>8}")
    print("-" * 100)
    cmp_rows = []
    for th in (3, 4, 5):
        r = evaluate(m, m["box_score"] >= th, f"原规则 box_score >= {th}", "原规则")
        cmp_rows.append(r)
        print(f"{r['规则']:<30}{r['样本']:>7}{r['命中爆拉']:>9}"
              f"{r['精确率']*100:>8.1f}%{r['提升倍数']:>7.2f}x{r['召回率']*100:>7.1f}%")
    for th in (6, 8, 10, 12):
        r = evaluate(m, m["score_full"] >= th, f"新规则 完整 >= {th}", "新规则")
        cmp_rows.append(r)
        print(f"{r['规则']:<30}{r['样本']:>7}{r['命中爆拉']:>9}"
              f"{r['精确率']*100:>8.1f}%{r['提升倍数']:>7.2f}x{r['召回率']*100:>7.1f}%")
    for th in (5, 6, 7):
        r = evaluate(m, m["score_kline"] >= th, f"新规则 仅K线 >= {th}", "新规则")
        cmp_rows.append(r)
        print(f"{r['规则']:<30}{r['样本']:>7}{r['命中爆拉']:>9}"
              f"{r['精确率']*100:>8.1f}%{r['提升倍数']:>7.2f}x{r['召回率']*100:>7.1f}%")

    # —— 4) 按涨幅分档 ——
    print("\n【四】按涨幅分档 —— 行情越大，越容易被识别出来吗？")
    print("-" * 100)
    tier_rows = []
    print(f"{'涨幅档':<10}{'爆拉数':>8}{'平均K线分':>11}{'平均完整分':>11}"
          f"{'K线>=5':>9}{'完整>=8':>9}{'完整>=10':>10}")
    print("-" * 100)
    for tier in TIER_LABELS:
        d = pump[pump["tier"] == tier]
        if len(d) == 0:
            continue
        rec = {"涨幅档": tier, "爆拉数": len(d),
               "平均K线分": d["score_kline"].mean(),
               "平均完整分": d["score_full"].mean(),
               "K线>=5命中率": (d["score_kline"] >= 5).mean(),
               "完整>=8命中率": (d["score_full"] >= 8).mean(),
               "完整>=10命中率": (d["score_full"] >= 10).mean()}
        tier_rows.append(rec)
        print(f"{tier:<10}{len(d):>8}{d['score_kline'].mean():>11.2f}"
              f"{d['score_full'].mean():>11.2f}{(d['score_kline']>=5).mean()*100:>8.1f}%"
              f"{(d['score_full']>=8).mean()*100:>8.1f}%{(d['score_full']>=10).mean()*100:>9.1f}%")
    pd.DataFrame(tier_rows).to_csv(DATA / "backtest_tiers.csv", index=False, encoding="utf-8-sig")

    # —— 5) 训练 / 测试切分 ——
    print("\n【五】样本外检验（训练 pre_day < 2025-01-01，测试 >= 2025-01-01）")
    print("-" * 100)
    print(f"{'集':<8}{'样本':>7}{'基准率':>9}{'规则':<24}{'样本':>7}{'精确率':>9}{'提升':>8}{'召回':>8}")
    print("-" * 100)
    oos_rows = []
    for tag, d in (("训练", m[m["date"] < SPLIT]), ("测试", m[m["date"] >= SPLIT])):
        for name, mask in (("原规则 box_score>=5", d["box_score"] >= 5),
                           ("原规则 box_score>=3", d["box_score"] >= 3),
                           ("新规则 完整>=8", d["score_full"] >= 8),
                           ("新规则 仅K线>=5", d["score_kline"] >= 5)):
            r = evaluate(d, mask, name, tag)
            oos_rows.append(r)
            if r["样本"] == 0:
                print(f"{tag:<8}{len(d):>7}{d['label'].mean()*100:>8.1f}%{name:<24}{'—':>7}")
                continue
            print(f"{tag:<8}{len(d):>7}{d['label'].mean()*100:>8.1f}%{name:<24}"
                  f"{r['样本']:>7}{r['精确率']*100:>8.1f}%{r['提升倍数']:>7.2f}x{r['召回率']*100:>7.1f}%")
        print("-" * 100)
    pd.DataFrame(oos_rows + cmp_rows).to_csv(DATA / "backtest_rules.csv", index=False,
                                             encoding="utf-8-sig")

    # —— 6) 个案明细 ——
    print("\n【六】个案明细 —— 那些著名的爆拉，启动前一天长什么样？")
    print("-" * 100)
    cases = pump.sort_values("gain", ascending=False).head(20)
    cols = ["symbol", "pre_day", "gain", "score_kline", "score_deriv", "score_full",
            "c_trend", "c_breakout", "c_v24up", "oi_chg_1d", "oi_chg_3d", "ls_top"]
    show = cases[cols].copy()
    print(f"{'标的':<14}{'启动前':<12}{'涨幅':>7}{'K线分':>7}{'合约分':>8}{'总分':>6}"
          f"{'趋势':>5}{'突破':>5}{'OI1日':>9}{'OI3日':>9}{'大户比':>8}")
    print("-" * 100)
    for _, r in show.iterrows():
        oi1 = f"{r['oi_chg_1d']*100:+.0f}%" if pd.notna(r["oi_chg_1d"]) else "-"
        oi3 = f"{r['oi_chg_3d']*100:+.0f}%" if pd.notna(r["oi_chg_3d"]) else "-"
        lst = f"{r['ls_top']:.2f}" if pd.notna(r["ls_top"]) else "-"
        sd = f"{r['score_deriv']:.0f}" if pd.notna(r["score_deriv"]) else "-"
        print(f"{r['symbol'].replace('USDT',''):<14}{r['pre_day']:<12}{r['gain']:>6.1f}x"
              f"{r['score_kline']:>7.0f}{sd:>8}{r['score_full']:>6.0f}"
              f"{'是' if r['c_trend'] else '-':>5}{'是' if r['c_breakout'] else '-':>5}"
              f"{oi1:>9}{oi3:>9}{lst:>8}")
    cases.to_csv(DATA / "backtest_cases.csv", index=False, encoding="utf-8-sig")

    # —— 7) 漏报分析 ——
    print("\n【七】漏报分析 —— 高分规则漏掉了哪些大行情？")
    print("-" * 100)
    miss = pump[(pump["gain"] >= 4.0) & (pump["score_full"] < 8)]
    print(f"5x 以上共 {int((pump['gain']>=4.0).sum())} 个，其中完整分 <8 的有 {len(miss)} 个"
          f"（{len(miss)/max(1,int((pump['gain']>=4.0).sum()))*100:.0f}%）")
    if len(miss):
        print("  这些币在启动前 24h 的 1 日涨幅中位 "
              f"{miss['gain'].median():.1f}x —— 说明它们启动前毫无征兆，属于'无预警型'")
        print("  漏报最严重的 10 个：")
        for _, r in miss.sort_values("gain", ascending=False).head(10).iterrows():
            print(f"    {r['symbol'].replace('USDT',''):<12}{r['gain']:>6.1f}x  "
                  f"K线{r['score_kline']:.0f} 合约{'-' if pd.isna(r['score_deriv']) else int(r['score_deriv'])}  "
                  f"趋势={'是' if r['c_trend'] else '否'}")
    print("\n" + "=" * 100)
    print(f"-> {DATA/'backtest_scores.csv'} / backtest_thresholds.csv / "
          f"backtest_tiers.csv / backtest_cases.csv / backtest_rules.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
