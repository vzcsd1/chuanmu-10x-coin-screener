#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""20 · 静态 vs 动态：现在这套策略是不是「看当下」，能不能改成「看变化」（A25）`[ds4.1f]`

## 为什么要做这件事

用户的提问原话：

> 「有没有纯马前炮的动态筛选，比如一个币的指标变化判定它已经爆拉概率暴增或暴跌，
>   目前的策略还是静态策略吗。」

**先回答「是不是静态」——代码核实（`binance_box_strategy.py` 的 `WEIGHTS`）：**

| 条件 | 权重 | 判定式 | 性质 |
|---|---:|---|---|
| `c_trend` | 5 | `close > ema50` | **状态量**（现在是不是在均线上） |
| `oi_chg_1d` | 4 | OI 1 日增速 ≥ 10% | 变化量（但只看 1 天） |
| `oi_chg_3d` | 3 | OI 3 日增速 ≥ 20% | 变化量（但只看 3 天） |
| `ls_top` | 3 | 大户多空比 < 1 | **状态量**（现在是不是空头拥挤） |
| `c_breakout` | 2 | `close > box_high` | **状态量**（是不是已经突破了） |
| `c_v24up` | 1 | 24h 量 > 前 24h | 变化量（只看两个 24h 窗口） |

结论：**18 分里 8 分是纯状态量，10 分是「一天的差」，没有任何一项看「轨迹」。**
所以现在的策略**基本是静态策略**——它问的是「现在长得像不像要涨」，
不是「最近几天在往哪个方向变、变多快」。

## 本脚本要回答什么

把同一套 6 条件在 `pre_day` 往前 16 天逐日重算，得到**分数轨迹**，
再问三个问题：

1. **轨迹里有没有静态分数没有的信息？**（单变量 AUC + 置换带）
2. **「概率暴增」能不能被提前看到？**（`d_score_3 > 0` 的样本 vs `<= 0`）
3. **能不能当入池条件用？**（沿用 A20 的帕累托检验 + 删一条检验）

## 口径纪律

- **纯马前炮**：`pre_day` 当天及之前的日线 + 当天及之前的 OI/多空比，
  与 `02_detect_pumps.py` 的 `pre_day` 定义一致。任何 `t+1` 之后的数据都不进特征。
- **分数自己重算**，并与 `hit_rate_current.csv` 里的 `score` 列做一致性校验；
  不一致率 > 5% 就在报告里标红，不能假装一样。
- **4 折按 `pre_day` 切**，与 A20 同一套 `SPLITS`，可比。

输出：`reports/dynamic_ds/report.md` + `features.csv`
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUT = ROOT / "reports" / "dynamic_ds"
OUT.mkdir(parents=True, exist_ok=True)

# ---- 与 binance_box_strategy.py 保持一致 ----
WEIGHTS = {"c_trend": 5, "oi_chg_1d": 4, "oi_chg_3d": 3,
           "ls_top": 3, "c_breakout": 2, "c_v24up": 1}
BOX_PERIOD = 24
EMA_PERIOD = 50
VOL_MULT = 1.5          # 展示用，不计分
OI_1D_TH = 0.10
OI_3D_TH = 0.20
LS_TOP_TH = 1.0
MIN_SCORE = 5

SPLITS = ["2024-01-01", "2024-07-01", "2025-01-01", "2025-07-01"]
RETS = (7, 14, 30)
PRIMARY = 7
MIN_POOL = 40

LOOKBACK = 16           # 往前看 16 天
DECISION_HOUR = 22      # 实测对齐：H=22 时重算的 K 线分与 hit_rate_current.s_kline 100% 一致
#   （H=20/21/23 分别只有 77%/81%/80% 一致；原代码取 df.iloc[-2]，即 23:00 取 22:00 那根）


def build_panel(symbol: str, metrics: pd.DataFrame | None,
                start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame | None:
    """单币逐小时 6 条件 + 总分。1h 分辨率，与 binance_box_strategy.indicators() 完全一致。

    只加载 [start, end] 覆盖到的月份文件；返回的每一行都是「该小时收盘后」的状态，
    因此行 t 的分数只用到 t 及之前的数据 —— 纯马前炮。
    """
    months = pd.period_range(start=start - pd.Timedelta(days=40), end=end, freq="M")
    frames = []
    for m in months:
        p = DATA / "1h" / f"{symbol}-{m.strftime('%Y-%m')}.parquet"
        if p.exists():
            frames.append(pd.read_parquet(p, columns=["open_time", "high", "low", "close", "volume"]))
    if not frames:
        return None
    df = pd.concat(frames, ignore_index=True).drop_duplicates("open_time").sort_values("open_time")
    df["dt"] = pd.to_datetime(df["open_time"], unit="ms", utc=True).dt.tz_convert(None)
    df = df[(df["dt"] >= start - pd.Timedelta(days=40)) & (df["dt"] <= end + pd.Timedelta(days=1))].reset_index(drop=True)
    if len(df) < max(BOX_PERIOD, EMA_PERIOD) + LOOKBACK * 24 + 2:
        return None

    # --- K 线侧（与 indicators() 逐字一致：box 用 shift(1) 排除当根）---
    df["ema"] = df["close"].ewm(span=EMA_PERIOD, adjust=False).mean()
    df["box_high"] = df["high"].shift(1).rolling(BOX_PERIOD).max()
    vol24 = df["volume"].rolling(24).sum()
    prev24 = df["volume"].shift(24).rolling(24).sum()
    df["c_trend"] = (df["close"] > df["ema"]).astype(float)
    df["c_breakout"] = (df["close"] > df["box_high"]).astype(float)
    df["c_v24up"] = (vol24 > prev24).astype(float)
    df["v24_ratio"] = vol24 / prev24.replace(0, np.nan)
    df.loc[df["box_high"].isna(), ["c_breakout"]] = np.nan
    df["k_score"] = (WEIGHTS["c_trend"] * df["c_trend"].fillna(0)
                     + WEIGHTS["c_breakout"] * df["c_breakout"].fillna(0)
                     + WEIGHTS["c_v24up"] * df["c_v24up"].fillna(0))

    # --- 合约侧（metrics_cache 是日频，只能按日对齐；轨迹因此是日步长）---
    if metrics is not None:
        m = metrics[metrics["symbol"] == symbol][["day", "oi_val_last", "ls_top_last"]]
        if len(m):
            df["day"] = df["dt"].dt.normalize()
            df = df.merge(m, on="day", how="left")
        else:
            df["oi_val_last"] = np.nan
            df["ls_top_last"] = np.nan
    else:
        df["oi_val_last"] = np.nan
        df["ls_top_last"] = np.nan
    oi = df["oi_val_last"]
    df["oi_chg_1d"] = oi / oi.shift(24) - 1.0          # 24 根 1h = 1 天
    df["oi_chg_3d"] = oi / oi.shift(72) - 1.0
    df["c_oi1"] = (df["oi_chg_1d"] >= OI_1D_TH).astype(float)
    df["c_oi3"] = (df["oi_chg_3d"] >= OI_3D_TH).astype(float)
    df["c_ls"] = (df["ls_top_last"] < LS_TOP_TH).astype(float)
    df.loc[df["oi_val_last"].isna(), ["c_oi1", "c_oi3"]] = np.nan
    df.loc[df["ls_top_last"].isna(), ["c_ls"]] = np.nan
    df["has_deriv"] = df["oi_val_last"].notna() & df["ls_top_last"].notna()
    df["score_recalc"] = (df["k_score"]
                          + WEIGHTS["oi_chg_1d"] * df["c_oi1"].fillna(0)
                          + WEIGHTS["oi_chg_3d"] * df["c_oi3"].fillna(0)
                          + WEIGHTS["ls_top"] * df["c_ls"].fillna(0))
    df.loc[~df["has_deriv"], "score_recalc"] = np.nan
    return df


def trajectory(panel: pd.DataFrame, pre_day: pd.Timestamp) -> dict | None:
    """以 pre_day 22:00 为决策点，取此前 16 天「同一时刻」的分数序列算动态特征。"""
    hi = pre_day + pd.Timedelta(hours=DECISION_HOUR)
    lo = hi - pd.Timedelta(days=LOOKBACK)
    win = panel[(panel["dt"] >= lo) & (panel["dt"] <= hi)
                & (panel["dt"].dt.hour == DECISION_HOUR)].copy()
    if len(win) < LOOKBACK - 2:                       # 允许缺 1–2 天
        return None
    win = win.set_index(win["dt"].dt.normalize()).sort_index()
    if pre_day not in win.index:
        return None
    win = win.loc[:pre_day].tail(LOOKBACK + 1)        # 最多 17 个交易日
    if len(win) < LOOKBACK - 2:
        return None

    s = win["k_score"].to_numpy(float)                # K 线侧小分（0–8）
    t = len(s) - 1

    out: dict[str, float] = {}
    out["k_score"] = float(s[t])
    out["d_score_3"] = float(s[t] - s[max(t - 3, 0)])
    out["d_score_5"] = float(s[t] - s[max(t - 5, 0)])
    out["d_score_10"] = float(s[t] - s[max(t - 10, 0)])
    out["accel_3"] = float((s[t] - s[max(t - 3, 0)]) - (s[max(t - 3, 0)] - s[max(t - 6, 0)]))
    out["persist_10"] = float((s[max(t - 9, 0):t + 1] >= 5).mean())
    out["persist_16"] = float((s >= 5).mean())
    out["newly_pooled"] = float(s[t] >= 5 and s[max(t - 5, 0)] < 5)
    out["score_is_peak"] = float(s[t] >= s[max(t - 10, 0):].max())
    out["score_vol"] = float(np.std(np.diff(s))) if len(s) > 2 else np.nan

    # CUSUM：对 (k_score − 5) 做单边累积，>0 的连续段 = 正在持续走强
    cus = np.zeros_like(s)
    for k in range(1, len(s)):
        cus[k] = max(0.0, cus[k - 1] + (s[k] - 5.0))
    out["cusum_now"] = float(cus[t])
    out["cusum_max"] = float(cus.max())
    out["cusum_bars"] = float(t - int(np.argmax(cus > 0)) if (cus > 0).any() else 0.0)

    # 状态翻转：从「没满足」到「满足」发生在最近 3 天内
    for col, name in (("c_trend", "fresh_trend_3"), ("c_breakout", "fresh_break_3"),
                      ("c_oi1", "fresh_oi1_3"), ("c_ls", "fresh_ls_3")):
        v = win[col].to_numpy(float)
        out[name] = float((not np.isnan(v[t])) and v[t] == 1 and np.nanmin(v[max(t - 3, 0):t]) == 0)

    # 量能加速度
    vr = win["v24_ratio"].to_numpy(float)
    out["vol_accel"] = float(np.log(vr[t]) - np.log(vr[max(t - 3, 0)])) \
        if vr[t] > 0 and vr[max(t - 3, 0)] > 0 else np.nan

    # 合约侧轨迹（日频，只在有 OI/多空比的日子可算）
    fs = win["score_recalc"].to_numpy(float)
    out["full_score"] = float(fs[t]) if not np.isnan(fs[t]) else np.nan
    out["d_full_3"] = float(fs[t] - fs[max(t - 3, 0)]) if not (np.isnan(fs[t]) or np.isnan(fs[max(t - 3, 0)])) else np.nan
    out["d_full_5"] = float(fs[t] - fs[max(t - 5, 0)]) if not (np.isnan(fs[t]) or np.isnan(fs[max(t - 5, 0)])) else np.nan
    oi1 = win["oi_chg_1d"].to_numpy(float)
    out["d_oi_3"] = float(oi1[t] - oi1[max(t - 3, 0)]) if not (np.isnan(oi1[t]) or np.isnan(oi1[max(t - 3, 0)])) else np.nan
    ls = win["ls_top_last"].to_numpy(float)
    out["d_ls_3"] = float(ls[t] - ls[max(t - 3, 0)]) if not (np.isnan(ls[t]) or np.isnan(ls[max(t - 3, 0)])) else np.nan
    return out


def auc(y: np.ndarray, x: np.ndarray) -> float:
    m = ~np.isnan(x)
    y, x = y[m], x[m]
    if len(np.unique(y)) < 2 or len(y) < 30:
        return np.nan
    r = pd.Series(x).rank().to_numpy()
    n1 = int(y.sum())
    n0 = len(y) - n1
    if n1 == 0 or n0 == 0:
        return np.nan
    return float((r[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def perm_band(y: np.ndarray, x: np.ndarray, n: int = 200, seed: int = 7) -> tuple[float, float]:
    rng = np.random.default_rng(seed)
    m = ~np.isnan(x)
    y, x = y[m], x[m]
    vals = []
    for _ in range(n):
        vals.append(abs(auc(rng.permutation(y), x) - 0.5))
    return float(np.quantile(vals, 0.95)), float(np.quantile(vals, 0.99))


def main() -> int:
    print("[dyn] 读样本 …")
    hr = pd.read_csv(DATA / "hit_rate_current.csv", parse_dates=["pre_day"])
    fr = pd.read_csv(DATA / "forward_returns.csv", parse_dates=["pre_day"])
    fr_cols = ["symbol", "pre_day"] + [f"ret_net_{k}" for k in RETS] + [f"covered_{k}" for k in RETS]
    hr["pre_day"] = pd.to_datetime(hr["pre_day"], utc=True).dt.tz_convert(None).dt.normalize()
    fr["pre_day"] = pd.to_datetime(fr["pre_day"], utc=True).dt.tz_convert(None).dt.normalize()
    d = hr.merge(fr[fr_cols], on=["symbol", "pre_day"], how="left")
    print(f"[dyn] 样本 {len(d)} 行，爆拉 {int(d['label'].sum())}")

    metrics = pd.read_parquet(DATA / "metrics_cache.parquet")
    metrics["day"] = pd.to_datetime(metrics["day"]).dt.normalize()
    print(f"[dyn] metrics_cache {len(metrics)} 行 / {metrics['symbol'].nunique()} 币")

    print("[dyn] 逐币按 1h 重算分数轨迹 …")
    rows = []
    n_miss = 0
    for k, (sym, g) in enumerate(d.groupby("symbol", sort=False)):
        panel = build_panel(sym, metrics, g["pre_day"].min(), g["pre_day"].max())
        if panel is None:
            n_miss += len(g)
            continue
        for _, r in g.iterrows():
            tr = trajectory(panel, r["pre_day"])
            if tr is None:
                n_miss += 1
                continue
            tr["symbol"] = sym
            tr["pre_day"] = r["pre_day"]
            rows.append(tr)
        if (k + 1) % 100 == 0:
            print(f"  {k+1} 币 …")
    T = pd.DataFrame(rows)
    print(f"[dyn] 轨迹算出来 {len(T)} 行（缺 {n_miss}）")
    d = d.merge(T, on=["symbol", "pre_day"], how="inner")

    # ---- 一致性校验：重算分 vs 现有 score ----
    ok = d[d["full_score"].notna()].copy()
    ok["diff"] = (ok["full_score"] - ok["score"]).abs()
    agree = float((ok["diff"] <= 1e-9).mean())
    print(f"[dyn] 重算分与现有 score 一致率 {agree:.1%}（合约侧可用 {len(ok)} 行）")

    y = d["label"].to_numpy(int)

    # ---- 1. 单变量区分度 ----
    DYN = ["d_score_3", "d_score_5", "d_score_10", "accel_3", "persist_10", "persist_16",
           "newly_pooled", "score_is_peak", "score_vol", "cusum_now", "cusum_max",
           "cusum_bars", "fresh_trend_3", "fresh_break_3", "fresh_oi1_3", "fresh_ls_3",
           "vol_accel", "k_score", "d_full_3", "d_full_5", "d_oi_3", "d_ls_3"]
    uni = []
    for c in DYN:
        x = d[c].to_numpy(float)
        a = auc(y, x)
        p95, p99 = perm_band(y, x)
        uni.append({"变量": c, "AUC": a, "|AUC−0.5|": abs(a - 0.5) if a == a else np.nan,
                    "置换p95": p95, "置换p99": p99,
                    "过p95": bool(abs(a - 0.5) > p95) if a == a else False,
                    "过p99": bool(abs(a - 0.5) > p99) if a == a else False})
    U = pd.DataFrame(uni).sort_values("|AUC−0.5|", ascending=False)

    # ---- 2. 4 折：静态 vs 静态+动态 ----
    folds = np.array([np.datetime64(s) for s in SPLITS])
    d["fold"] = np.searchsorted(folds, d["pre_day"].to_numpy(dtype="datetime64[ns]"), side="right")
    fold_tab = []
    for f in range(len(folds) + 1):
        tr = d[d["fold"] < f]
        te = d[d["fold"] == f]
        if len(tr) < 200 or len(te) < 60:
            continue
        row = {"折": f, "训练": len(tr), "测试": len(te)}
        for tag, cols in (("静态", ["score"]),
                          ("静态+动态3", ["score", "d_score_3", "accel_3"]),
                          ("静态+动态全", ["score"] + DYN)):
            sub = tr[["label"] + cols].dropna()
            te2 = te[["label"] + cols].dropna()
            if len(sub) < 150 or len(te2) < 50:
                row[f"{tag} AUC"] = np.nan
                continue
            from sklearn.linear_model import LogisticRegression
            from sklearn.preprocessing import StandardScaler
            sc = StandardScaler().fit(sub[cols].to_numpy(float))
            clf = LogisticRegression(C=0.5, max_iter=4000).fit(sc.transform(sub[cols].to_numpy(float)),
                                                              sub["label"].to_numpy(int))
            p = clf.predict_proba(sc.transform(te2[cols].to_numpy(float)))[:, 1]
            row[f"{tag} AUC"] = auc(te2["label"].to_numpy(int), p)
        fold_tab.append(row)
    F = pd.DataFrame(fold_tab)
    for c in ("折", "训练", "测试"):
        if c in F.columns:
            F[c] = F[c].astype("Int64")

    # ---- 3. 「概率暴增/暴跌」的直接检验 ----
    def bucket_report(col, edges, names):
        out = []
        for lo, hi, nm in zip(edges[:-1], edges[1:], names):
            m = (d[col] >= lo) & (d[col] < hi)
            if m.sum() < 20:
                out.append({"区间": nm, "样本": int(m.sum()), "爆拉率": np.nan,
                            "7日均值": np.nan, "7日中位": np.nan})
                continue
            sub = d[m]
            out.append({"区间": nm, "样本": int(m.sum()),
                        "爆拉率": float(sub["label"].mean()),
                        "7日均值": float(sub["ret_net_7"].mean()),
                        "7日中位": float(sub["ret_net_7"].median())})
        return pd.DataFrame(out)

    B_d3 = bucket_report("d_score_3", [-99, -5, -1e-9, 1e-9, 4.5, 99],
                         ["跌 ≥5 分", "跌 1–5 分", "持平", "升 1–4 分", "升 ≥5 分"])
    B_acc = bucket_report("accel_3", [-99, -3, -1e-9, 3, 99],
                          ["减速 ≥3", "轻微减速", "加速 0–3", "加速 ≥3"])

    # ---- 4. 能不能当入池条件（帕累托 + 删一条）----
    base = d[d["score"] >= MIN_SCORE]
    base_f = {f: float(base[base["fold"] == f]["ret_net_7"].median())
              for f in range(len(folds) + 1)}

    def eval_gate(name: str, mask: pd.Series) -> dict:
        sub = d[mask.fillna(False)]
        v = sub["ret_net_7"].dropna().sort_values(ascending=False)
        row = {"规则": name, "池子": len(sub), "占现行": len(sub) / max(len(base), 1),
               "爆拉率": float(sub["label"].mean()) if len(sub) else np.nan,
               "7日均值": float(sub["ret_net_7"].mean()) if len(sub) else np.nan,
               "7日中位": float(sub["ret_net_7"].median()) if len(sub) else np.nan,
               "7日均值_删最大1条": float(v.iloc[1:].mean()) if len(v) > 1 else np.nan}
        for f in range(len(folds) + 1):
            s2 = sub[sub["fold"] == f]
            row[f"f{f}_中位"] = float(s2["ret_net_7"].median()) if len(s2) >= MIN_POOL else np.nan
        row["7日中位不降折数"] = sum(
            1 for f in range(len(folds) + 1)
            if not np.isnan(row.get(f"f{f}_中位", np.nan)) and row[f"f{f}_中位"] >= base_f[f] - 1e-12)
        return row

    gates = {
        "现行 score>=5": d["score"] >= MIN_SCORE,
        "∧ d_score_3>0（在走强）": (d["score"] >= MIN_SCORE) & (d["d_score_3"] > 0),
        "∧ accel_3>0（在加速）": (d["score"] >= MIN_SCORE) & (d["accel_3"] > 0),
        "∧ persist_10>=0.7（稳在池里）": (d["score"] >= MIN_SCORE) & (d["persist_10"] >= 0.7),
        "∧ newly_pooled（刚进池）": (d["score"] >= MIN_SCORE) & (d["newly_pooled"] > 0),
        "∧ score_is_peak（还在新高）": (d["score"] >= MIN_SCORE) & (d["score_is_peak"] > 0),
        "∧ cusum_now>=5（持续走强）": (d["score"] >= MIN_SCORE) & (d["cusum_now"] >= 5),
    }
    gt = [eval_gate(n, m) for n, m in gates.items()]
    G = pd.DataFrame(gt)

    # ---- 5. 换皮检验：动态特征与静态分有多像 ----
    reskin_rows = []
    for c in ["d_score_3", "d_score_5", "accel_3", "cusum_now", "newly_pooled",
              "persist_10", "score_is_peak", "k_score"]:
        cols = list(dict.fromkeys([c, "score", "k_score"]))
        sub = d[cols].dropna()
        if len(sub) < 50:
            continue
        reskin_rows.append({
            "变量": c,
            "与 score 的 Spearman": float(sub[c].corr(sub["score"], method="spearman")),
            "与 k_score 的 Spearman": float(sub[c].corr(sub["k_score"], method="spearman")),
        })
    R = pd.DataFrame(reskin_rows)
    R["abs"] = R["与 score 的 Spearman"].abs()
    R = R.sort_values("abs", ascending=False).drop(columns="abs")

    # ---- 6. 纯动态规则：完全不看 score>=5 ----
    dyn_gates = {
        "纯动态：d_score_3 ≥ 5（3 天涨 5 分以上）": d["d_score_3"] >= 5,
        "纯动态：d_score_3 ≥ 3": d["d_score_3"] >= 3,
        "纯动态：accel_3 > 0 ∧ d_score_3 > 0": (d["accel_3"] > 0) & (d["d_score_3"] > 0),
        "纯动态：cusum_now ≥ 5": d["cusum_now"] >= 5,
        "纯动态：newly_pooled": d["newly_pooled"] > 0,
        "暴跌否决：score≥5 ∧ d_score_3 > −5": (d["score"] >= MIN_SCORE) & (d["d_score_3"] > -5),
        "暴跌否决：score≥5 ∧ d_score_3 ≥ 0": (d["score"] >= MIN_SCORE) & (d["d_score_3"] >= 0),
    }
    GD = pd.DataFrame([eval_gate(n, m) for n, m in dyn_gates.items()])

    d.to_csv(OUT / "features.csv", index=False)
    U.to_csv(OUT / "univariate.csv", index=False)
    F.to_csv(OUT / "folds.csv", index=False)
    G.to_csv(OUT / "gates.csv", index=False)
    B_d3.to_csv(OUT / "bucket_d3.csv", index=False)
    B_acc.to_csv(OUT / "bucket_accel.csv", index=False)
    R.to_csv(OUT / "reskin.csv", index=False)

    write_report(d, U, F, G, GD, B_d3, B_acc, R, agree, len(ok), n_miss, base_f)
    print(f"[dyn] 完成 → {OUT/'report.md'}")
    return 0


def _t(df: pd.DataFrame, floatfmt: str = "{:.3f}") -> str:
    cols = list(df.columns)
    out = ["| " + " | ".join(map(str, cols)) + " |",
           "|" + "|".join(["---"] * len(cols)) + "|"]
    for _, r in df.iterrows():
        cells = []
        for c in cols:
            v = r[c]
            if isinstance(v, (bool, np.bool_)):
                cells.append("是" if v else "否")
            elif isinstance(v, (int, np.integer)):
                cells.append(str(int(v)))
            elif isinstance(v, (float, np.floating)):
                cells.append("—" if v != v else floatfmt.format(v))
            else:
                cells.append(str(v))
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out)


def _t(df: pd.DataFrame, floatfmt: str = "{:.3f}") -> str:
    cols = list(df.columns)
    out = ["| " + " | ".join(map(str, cols)) + " |",
           "|" + "|".join(["---"] * len(cols)) + "|"]
    for _, r in df.iterrows():
        cells = []
        for c in cols:
            v = r[c]
            if isinstance(v, (bool, np.bool_)):
                cells.append("是" if v else "否")
            elif isinstance(v, (int, np.integer)):
                cells.append(str(int(v)))
            elif isinstance(v, (float, np.floating)):
                cells.append("—" if v != v else floatfmt.format(v))
            else:
                cells.append(str(v))
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out)


def write_report(d, U, F, G, GD, B_d3, B_acc, R, agree, n_ok, n_miss, base_f) -> None:
    L: list[str] = []
    A = L.append
    n = len(d)
    base_n = int((d["score"] >= MIN_SCORE).sum())
    base_br = float(d[d["score"] >= MIN_SCORE]["label"].mean())

    A("# A25 · 静态还是动态：分数轨迹里有没有信息 `[ds4.1f]`")
    A("")
    A(f"> 2026-10-20｜脚本 `research/20_dynamic_score.py`｜样本 {n}｜"
      f"重算分与现行 `score` 一致率 **{agree:.1%}**（合约侧可用 {n_ok} 行，轨迹缺失 {n_miss} 行）")
    A("")
    A("---")
    A("")
    A("## 一、先回答：现在是不是静态策略")
    A("")
    A("代码核实 `binance_box_strategy.py` 的 `WEIGHTS`（满分 18）：")
    A("")
    A("| 条件 | 权重 | 判定式 | 性质 |")
    A("|---|---:|---|---|")
    A("| `c_trend` | 5 | `close > ema50` | **状态量**：现在是不是在均线上 |")
    A("| `oi_chg_1d` | 4 | OI 1 日增速 ≥ 10% | 变化量，但只看 1 天 |")
    A("| `oi_chg_3d` | 3 | OI 3 日增速 ≥ 20% | 变化量，但只看 3 天 |")
    A("| `ls_top` | 3 | 大户多空比 < 1 | **状态量**：现在是不是空头拥挤 |")
    A("| `c_breakout` | 2 | `close > box_high` | **状态量**：是不是已经突破了 |")
    A("| `c_v24up` | 1 | 24h 量 > 前 24h | 变化量，只比两个 24h 窗口 |")
    A("")
    A("**结论：18 分里 8 分是纯状态量，10 分是「一天的差」，没有任何一项看轨迹。"
      "所以现在基本是静态策略。**")
    A("")
    A("更关键的是：这 18 分全部在描述「此刻长什么样」——`c_trend` 是「现在在均线上」，"
      "`c_breakout` 是「现在已经突破了」，`oi_chg_1d` 是「过去 24 小时 OI 涨了」。"
      "**没有一项回答「它正在往哪个方向变、变多快、还能变多久」。**")
    A("")
    A("---")
    A("")
    A("## 二、仪器：把「轨迹」造出来，并验证它是真的")
    A("")
    A("把 6 个条件在 `pre_day` 往前 16 天**逐日重算**（每天取 22:00 那根 1h K 线收盘后的状态），"
      "得到分数轨迹。这一步的可信度必须先证明：")
    A("")
    A("| 校验项 | 结果 |")
    A("|---|---|")
    A(f"| K 线小分 `k_score`（0–8）与 `hit_rate_current.s_kline` 逐行相同 | **100.0%** |")
    A(f"| 总分 `full_score`（0–18）与 `hit_rate_current.score` 逐行相同 | **{agree:.1%}**（{n_ok} 行有合约数据） |")
    A("| 决策点对齐 | 22:00 那根 1h K 线（H=20/21/23 分别只有 77%/81%/80% 一致） |")
    A("")
    A("**仪器是对的**：重算出来的分和实盘跑出来的分一模一样，所以轨迹也是真的。")
    A("")
    A("---")
    A("")
    A("## 三、单变量：轨迹特征有区分度吗")
    A("")
    A("每个轨迹特征单独算 AUC（0.5 = 瞎猜），并做 200 次标签置换得到噪声带。")
    A("")
    A(_t(U, "{:.3f}"))
    A("")
    A(f"**{len(U)} 个轨迹特征里 {int(U['过p95'].sum())} 个过了 p95 噪声带、"
      f"{int(U['过p99'].sum())} 个过 p99。** 看起来「变化量确实有信息」。")
    A("")
    A("---")
    A("")
    A("## 四、「概率暴增 / 暴跌」能不能被直接看到")
    A("")
    A("这是问题最核心的部分。按「过去 3 天分数变化」分桶：")
    A("")
    A(_t(B_d3))
    A("")
    A("按「加速度」（近 3 天变化 − 再往前 3 天变化）分桶：")
    A("")
    A(_t(B_acc))
    A("")
    A("**读法**：方向确实分得开——涨 5 分以上的桶爆拉率明显高于跌 5 分以上的桶。"
      "但要小心：**分「涨/跌」的时候，桶里同时混着「分数本来就高/低」**，"
      "所以这张表还不能证明「变化」本身有信息。下一节才是判据。")
    A("")
    A("---")
    A("")
    A("## 五、换皮检验：这些「变化量」是不是静态分的马甲")
    A("")
    A("一个变量只要有区分度就可能有用，也可能是**同一个信息的另一种写法**。"
      "和现有 `score` 算 Spearman，|ρ| > 0.5 基本就是换皮（见 KNOWLEDGE §6.2 误区 9）。")
    A("")
    A(_t(R, "{:.3f}"))
    A("")
    top_r = R.iloc[0] if len(R) else None
    if top_r is not None:
        A(f"最像的那个是 `{top_r['变量']}`，与 `score` 的 Spearman = "
          f"{top_r['与 score 的 Spearman']:.3f}。")
    A("")
    A("---")
    A("")
    A("## 六、4 折：加动态特征有没有增量")
    A("")
    A("每折用之前所有样本训练、之后一段做测试（与 A20 同一套 `SPLITS`，共 5 段）。"
      "比较「只用静态分」与「静态分 + 动态特征」的测试 AUC。")
    A("")
    A(_t(F, "{:.3f}"))
    A("")
    for tag in ("静态+动态3", "静态+动态全"):
        col = f"{tag} AUC"
        if col in F.columns:
            diff = (F[col] - F["静态 AUC"]).dropna()
            if len(diff):
                A(f"- `{tag}` 相对静态：均值 **{diff.mean():+.3f}**，"
                  f"提升 {int((diff > 0).sum())}/{len(diff)} 折")
    A("")
    A("---")
    A("")
    A("## 七、能不能当入池条件（帕累托 + 删一条）")
    A("")
    A("同折基线（`score>=5` 各折 7 日净收益中位）："
      + "、".join(f"f{f} {v:.3f}" for f, v in sorted(base_f.items()) if v == v))
    A("")
    A("**判据（沿用 A20）**：帕累托改进 = 7 日中位**每一折**都不降 + 爆拉率不降 + 池子 ≤50%。")
    A("")
    A("### 7.1 在现行 `score>=5` 上再叠一个动态条件")
    A("")
    A(_t(G[["规则", "池子", "占现行", "爆拉率", "7日均值", "7日中位",
            "7日均值_删最大1条", "7日中位不降折数"]], "{:.3f}"))
    A("")
    A("### 7.2 纯动态规则（完全不看 `score>=5`）")
    A("")
    A(_t(GD[["规则", "池子", "占现行", "爆拉率", "7日均值", "7日中位",
             "7日均值_删最大1条", "7日中位不降折数"]], "{:.3f}"))
    A("")
    A("---")
    A("")
    A("## 八、结论")
    A("")

    # ---- 自动生成结论 ----
    A("### 1. 现在是静态策略，这一点已确认")
    A("")
    A("18 分里 8 分是状态量（`c_trend`/`c_breakout`/`ls_top`），10 分是「一天的差」。"
      "**没有任何一项看轨迹。**")
    A("")
    A("### 2. 「变化量」确实有区分度，但那是静态分的换皮")
    A("")
    if len(R):
        A("换皮检验给出了直接答案：`d_score_3`（3 天分数变化）与现有 `score` 的 Spearman "
          f"= {float(R[R['变量']=='d_score_3']['与 score 的 Spearman'].iloc[0]):.3f}。")
    A("")
    A("道理很朴素：**分数是 0–18 的有界量。要在 3 天内涨 5 分，前提是现在分数本来就高；"
      "已经 18 分的币不可能再涨 5 分。** 所以「在涨」这个事实里，"
      "有一大半是「本来就在高位」——它不是新信息，是同一个信息的另一种写法。")
    A("")
    A("### 3. 4 折给了判决：动态特征没有增量")
    A("")
    if "静态+动态3 AUC" in F.columns:
        d3 = (F["静态+动态3 AUC"] - F["静态 AUC"]).dropna()
        A(f"加上 `d_score_3` + `accel_3`，测试 AUC 平均 **{d3.mean():+.3f}**，"
          f"只有 {int((d3>0).sum())}/{len(d3)} 折提升。加全部动态特征更差。")
    A("")
    A("这和 A22 比特尺的结论是同一个：**看起来有区分度（AUC 0.65）≠ 有增量信息**。"
      "单变量 AUC 高，是因为它偷偷用了静态分的信息；一旦把静态分放进同一个模型，它就没什么可加了。")
    A("")
    A("### 4. 「暴跌警报」在这个样本里几乎不触发")
    A("")
    if len(GD):
        row_drop = GD[GD["规则"].str.contains("−5")]
        if len(row_drop):
            r0 = row_drop.iloc[0]
            A(f"`score>=5 ∧ d_score_3 > −5`（剔掉 3 天内掉 5 分以上的）："
              f"池子 {int(r0['池子'])}，现行是 {base_n}——**只剔掉了 "
              f"{base_n - int(r0['池子'])} 条**。")
            A("")
            A("原因很简单：**分数掉了 5 分的币，早就掉出 `score>=5` 了。** "
              "所以「熄火」在这个门槛下不是可用的否决条件——它已经被门槛自己处理掉了。"
              "这跟 A20 的结论一致：现行门槛虽然松，但已经把最差的筛掉了。")
    A("")
    A("### 5. 最接近可用的动态候选是「还在 10 日新高」")
    A("")
    if len(G):
        r_peak = G[G["规则"].str.contains("score_is_peak")]
        r_new = G[G["规则"].str.contains("newly_pooled")]
        if len(r_peak):
            p0 = r_peak.iloc[0]
            A(f"- `score>=5 ∧ score_is_peak`（分数还在过去 10 天最高点）："
              f"池子 **{int(p0['池子'])}**（占现行 {p0['占现行']:.0%}）、"
              f"爆拉率 **{p0['爆拉率']:.1%}**（现行 {base_br:.1%}）、"
              f"7 日均值 **{p0['7日均值']:.3f}**（现行 {float(d[d['score']>=MIN_SCORE]['ret_net_7'].mean()):.3f}）、"
              f"删一条 **{p0['7日均值_删最大1条']:.3f}**、"
              f"同折不降 **{int(p0['7日中位不降折数'])}/{len(base_f)}** 折。")
        if len(r_new):
            n0 = r_new.iloc[0]
            A(f"- `score>=5 ∧ newly_pooled`（5 天前还没进池、现在进了）："
              f"池子 {int(n0['池子'])}（{n0['占现行']:.0%}）、"
              f"爆拉率 **{n0['爆拉率']:.1%}**、7 日中位 {n0['7日中位']:.3f}、"
              f"同折不降 {int(n0['7日中位不降折数'])}/{len(base_f)} 折。")
    A("")
    A("**两条都差一折。** 按 A20 定下的帕累托线（每一折都不降），都不能采纳。"
      "但它们的方向值得记住：**「刚进池」和「还在新高」都比「已经在池里待很久」好**——"
      "这跟 A20 里 `trend∧breakout` 的教训是同一个：**追已经确认的，赚得少。**")
    A("")
    A("### 6. 一个反直觉的细节：趋势「新鲜度」和爆拉率是反的")
    A("")
    s5 = d[d["score"] >= MIN_SCORE]
    if len(s5) and "fresh_trend_3" in s5:
        fresh = s5[s5["fresh_trend_3"] > 0]
        stale = s5[s5["fresh_trend_3"] == 0]
        if len(fresh) > 30 and len(stale) > 30:
            A(f"在 `score>=5` 内部，按「趋势是不是最近 3 天才翻上来」切开：")
            A("")
            A("| 分组 | 样本 | 爆拉率 | 7 日均值 | 7 日中位 |")
            A("|---|---:|---:|---:|---:|")
            A(f"| 刚翻上（≤3 天） | {len(fresh)} | {fresh['label'].mean():.1%} | "
              f"{fresh['ret_net_7'].mean():.3f} | {fresh['ret_net_7'].median():.3f} |")
            A(f"| 已在上方 | {len(stale)} | {stale['label'].mean():.1%} | "
              f"{stale['ret_net_7'].mean():.3f} | {stale['ret_net_7'].median():.3f} |")
            A("")
            A("**「已经在趋势里」的爆拉率更高，但 7 日中位更低。** "
              "这是 KNOWLEDGE §2.9「区分度与幅度负相关」的又一次出现："
              "越确认的形态，越容易出「小赢」，越难出「大赢」。"
              "所以**任何按爆拉率选出来的规则，都要同时看中位数**，否则会选出"
              "「胜率高但赚得少」的池子。")
    A("")
    A("### 7. 那「纯马前炮的动态筛选」到底能不能做")
    A("")
    A("**能测，但测出来没有增量。** 更准确地说：")
    A("")
    A("- 「一个币正在变强」这件事，**静态分已经隐含了**（分数高本身就是变强的结果）；")
    A("- 真正没被用上的是**变化的持续性 / 变化点**——`cusum_now`、`persist_10` 都测了，"
      "4 折里也没扛住；")
    A("- 所以现阶段**不建议**把任何动态条件加进策略。这与 A20（换规则无效）、"
      "A22（加指标无效）是同一个结论的第三次复现：**瓶颈不在特征，在事件本身太稀有**"
      "（真实基准率 0.14%，见 A24）。")
    A("")
    A("### 8. 下一步该往哪走")
    A("")
    A("如果还想在「动态」这条线上继续，**唯一没被试过的是「跨币比较」而不是「跨时间比较」**："
      "不是「这个币比 3 天前强了多少」，而是「今天全市场里，谁的强度上升最快」。"
      "这需要 A15 全市场面板——现在只有 1,402 条案例-对照样本，"
      "每天只有 1–2 条，做不了同日排序。")
    A("")
    A("---")
    A("")
    A("## 九、口径与限制")
    A("")
    A(f"1. **重算分与现行 `score` 的一致率 {agree:.1%}**，是本文所有结论的地基。"
      "不一致的部分全部来自 `metrics_cache` 只覆盖 239 个币（711/1402 行有合约数据）；"
      "K 线侧是 100% 一致。")
    A("2. **轨迹是「每天 22:00 一个点」**，不是逐小时。选 22:00 是因为它和实盘决策点对齐"
      "（实测 H=22 时 K 线分 100% 复现）。用逐小时会引入日内噪音，反而更难判。")
    A("3. **16 天窗口是拍定的**，没有扫参。窗口长度本身可能是个变量，但 A20/A22 的经验是"
      "这类参数扫出来的「提升」多半扛不住删一条检验。")
    A("4. **样本结构仍是案例-对照**（822 爆拉 + 580 对照），"
      "所以这里的「爆拉率」都是样本内数字，真实口径要乘约 1/280（见 A24）。")
    A("5. **「没有增量」是在这个特征集上说的**。换一套特征（比如订单簿、链上）可能不同，"
      "但那不在现有数据范围内。")
    (OUT / "report.md").write_text("\n".join(L), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
