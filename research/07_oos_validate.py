#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""07 · 样本外验证：确认修订后的权重不是过拟合

做法：
  1. 按时间切分：训练集 pre_day < 2025-01-01，测试集 >= 2025-01-01
  2. 逐条件区分度：训练集 vs 测试集，看排名是否稳定
  3. 原规则（box_score 口径） vs 修订规则，分别在训练/测试集上评估
  4. 逻辑回归（仅用"决策时可知"的特征）训练 → 测试集 AUC / 精确率 / 召回率

严格排除前视特征：oi_chg_fwd2d（未来 2 日 OI 变化）、gain（结果标签）一律不进模型。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.preprocessing import StandardScaler

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import binance_box_strategy as base  # noqa: E402

DATA = ROOT / "data"
SPLIT = pd.Timestamp("2025-01-01")
W = base.WEIGHTS

# 决策时可知的特征（已剔除 oi_chg_fwd2d / gain）
FEATURES = [
    "c_trend", "c_breakout", "c_vol15", "c_box12", "c_stairs", "c_v4up",
    "c_v24up", "c_move48_15_25", "box_age_h", "volume_ratio", "box_width_pct",
    "oi_chg_1d", "oi_chg_3d", "oi_chg_7d", "ls_count", "ls_top", "taker",
    "ls_count_chg_3d", "ls_top_chg_3d", "funding_last", "funding_mean_7d",
    "funding_neg_ratio_7d",
]
BOOL_FEATS = ["c_trend", "c_breakout", "c_vol15", "c_box12", "c_stairs",
              "c_v4up", "c_v24up", "c_move48_15_25"]


def load():
    k = pd.read_csv(DATA / "features.csv")
    d = pd.read_csv(DATA / "features_deriv.csv")
    d = d.drop(columns=[c for c in ["label", "gain"] if c in d.columns])
    m = k.merge(d, on=["symbol", "pre_day"], how="inner")
    m["date"] = pd.to_datetime(m["pre_day"])
    return m


def revised_scores(df: pd.DataFrame) -> pd.DataFrame:
    """按 binance_box_strategy.WEIGHTS 打分 —— 与实盘同一真源，不再另写一套权重。"""
    s = np.zeros(len(df))
    s += W["c_trend"] * df["c_trend"].astype(float)
    s += W["c_breakout"] * df["c_breakout"].astype(float)
    s += W["c_v24up"] * df["c_v24up"].astype(float)
    s += W["oi_chg_1d"] * (df["oi_chg_1d"] > base.OI_1D_THRESHOLD).astype(float)
    s += W["oi_chg_3d"] * (df["oi_chg_3d"] > base.OI_3D_THRESHOLD).astype(float)
    s += W["ls_top"] * (df["ls_top"] < base.LS_TOP_THRESHOLD).astype(float)

    out = df.copy()
    out["revised_score"] = s
    out["revised_pass"] = s >= 6
    return out


def eval_rule(df: pd.DataFrame, mask: pd.Series, name: str, base: float) -> dict:
    sel = df[mask]
    if len(sel) == 0:
        return {"规则": name, "样本": 0}
    tp = int((sel["label"] == 1).sum())
    prec = tp / len(sel)
    rec = tp / max(1, int((df["label"] == 1).sum()))
    return {"规则": name, "样本": len(sel), "命中爆拉": tp, "精确率": prec,
            "提升倍数": prec / base, "召回率": rec}


def main():
    m = load()
    print(f"合并后样本 {len(m)}（爆拉 {int(m['label'].sum())} / 对照 {int((m['label']==0).sum())}）")
    print(f"时间范围 {m['date'].min().date()} ~ {m['date'].max().date()}")

    tr = m[m["date"] < SPLIT].reset_index(drop=True)
    te = m[m["date"] >= SPLIT].reset_index(drop=True)
    for tag, d in (("训练集(<2025)", tr), ("测试集(>=2025)", te)):
        print(f"  {tag}: {len(d)} 样本 | 爆拉 {int(d['label'].sum())} | 基准率 {d['label'].mean()*100:.1f}%")

    # —— 1) 逐条件区分度：训练 vs 测试 ——
    conds = [
        ("收盘>EMA50", lambda d: d["c_trend"]),
        ("OI 1日增>10%", lambda d: d["oi_chg_1d"] > 0.10),
        ("OI 3日增>20%", lambda d: d["oi_chg_3d"] > 0.20),
        ("大户持仓多空比<1", lambda d: d["ls_top"] < 1.0),
        ("24h量增", lambda d: d["c_v24up"]),
        ("已突破箱体", lambda d: d["c_breakout"]),
        ("多空比3日下降", lambda d: d["ls_top_chg_3d"] < 0),
        ("箱体持续>=72h", lambda d: d["box_age_h"] >= 72),
        ("量比>=1.5", lambda d: d["c_vol15"]),
        ("箱体宽<=12%", lambda d: d["c_box12"]),
        ("当期费率>=0", lambda d: d["funding_last"] >= 0),
    ]
    print(f"\n{'条件':<20}{'训练区分度':>12}{'测试区分度':>12}{'方向一致':>10}")
    print("-" * 56)
    rows = []
    for name, fn in conds:
        diffs = []
        for d in (tr, te):
            p = fn(d[d["label"] == 1]).mean()
            c = fn(d[d["label"] == 0]).mean()
            diffs.append(p - c)
        same = "OK" if (diffs[0] * diffs[1] > 0) else "!! 反向"
        rows.append({"条件": name, "训练区分度": diffs[0], "测试区分度": diffs[1], "方向一致": same})
        print(f"{name:<20}{diffs[0]*100:>11.1f}{diffs[1]*100:>12.1f}{same:>12}")
    pd.DataFrame(rows).to_csv(DATA / "oos_conditions.csv", index=False, encoding="utf-8-sig")

    # —— 2) 原规则 vs 修订规则 ——
    print("\n【原规则 vs 修订规则（分训练/测试）】")
    res = []
    for tag, d in (("训练", tr), ("测试", te)):
        base = d["label"].mean()
        res.append({**eval_rule(d, d["box_score"] >= 5, f"{tag}·原规则 box_score>=5", base), "集": tag})
        res.append({**eval_rule(d, d["box_score"] >= 3, f"{tag}·原规则 box_score>=3", base), "集": tag})
        dr = revised_scores(d)
        res.append({**eval_rule(dr, dr["revised_pass"], f"{tag}·修订规则", base), "集": tag})
    t = pd.DataFrame(res)
    print(f"{'规则':<26}{'样本':>7}{'爆拉':>7}{'精确率':>9}{'提升':>8}{'召回':>8}")
    print("-" * 66)
    for _, r in t.iterrows():
        if r.get("样本", 0) == 0:
            print(f"{r['规则']:<26}{'—':>7}")
            continue
        print(f"{r['规则']:<26}{int(r['样本']):>7}{int(r['命中爆拉']):>7}"
              f"{r['精确率']*100:>8.1f}%{r['提升倍数']:>7.2f}x{r['召回率']*100:>7.1f}%")
    t.to_csv(DATA / "oos_rules.csv", index=False, encoding="utf-8-sig")

    # —— 3) 逻辑回归 ——
    print("\n【逻辑回归（仅用决策时可知特征）】")
    X = m[FEATURES].copy()
    for c in BOOL_FEATS:
        X[c] = X[c].astype(float)
    X = X.replace([np.inf, -np.inf], np.nan)
    X = X.fillna(X.median(numeric_only=True))

    def fit_eval(train_idx, test_idx, tag):
        sc = StandardScaler().fit(X.iloc[train_idx])
        clf = LogisticRegression(max_iter=2000, C=0.5, class_weight="balanced")
        clf.fit(sc.transform(X.iloc[train_idx]), m["label"].iloc[train_idx])
        prob = clf.predict_proba(sc.transform(X.iloc[test_idx]))[:, 1]
        y = m["label"].iloc[test_idx].to_numpy()
        auc = roc_auc_score(y, prob)
        base = y.mean()
        out = {"切分": tag, "AUC": auc, "测试基准率": base, "测试样本": len(y)}
        for pct in (1, 2, 5, 10):
            k = max(1, int(len(y) * pct / 100))
            top = np.argsort(-prob)[:k]
            prec = y[top].mean()
            out[f"Top{pct}%精确率"] = prec
            out[f"Top{pct}%提升"] = prec / base
        return out, clf, sc

    tr_idx = np.where((m["date"] < SPLIT).to_numpy())[0]
    te_idx = np.where((m["date"] >= SPLIT).to_numpy())[0]
    r1, clf, sc = fit_eval(tr_idx, te_idx, "训练<2025 → 测试>=2025")
    print(f"  {r1['切分']}：AUC {r1['AUC']:.3f}（测试基准率 {r1['测试基准率']*100:.1f}%，n={r1['测试样本']}）")
    for pct in (1, 2, 5, 10):
        print(f"     Top{pct:>2}%：精确率 {r1[f'Top{pct}%精确率']*100:5.1f}% | "
              f"提升 {r1[f'Top{pct}%提升']:4.2f}x")

    # 全样本 AUC（仅作参考，非样本外）
    sc2 = StandardScaler().fit(X)
    clf2 = LogisticRegression(max_iter=2000, C=0.5, class_weight="balanced").fit(
        sc2.transform(X), m["label"])
    print(f"  （参考）全样本内 AUC {roc_auc_score(m['label'], clf2.predict_proba(sc2.transform(X))[:,1]):.3f}")

    # 特征重要性
    coef = pd.Series(clf.coef_[0], index=FEATURES).sort_values(key=np.abs, ascending=False)
    print("\n  逻辑回归系数（按绝对值排序，正=偏爆拉）：")
    for k, v in coef.items():
        print(f"    {k:<22}{v:>8.3f}")

    pd.DataFrame([r1]).to_csv(DATA / "oos_model.csv", index=False, encoding="utf-8-sig")
    print(f"\n-> {DATA/'oos_conditions.csv'} / oos_rules.csv / oos_model.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
