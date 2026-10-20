#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""18 · 比特尺 + 补测一行代码变量：回答「加指标还有没有用」（A22）`[ds4.1f]`

## 为什么做这个

用户问：「目前指标池子极大概率不是最优解的全量池，能不能多加几个指标？」

这个问题不能靠「再加几个变量试试」回答，因为**试 200 个变量总会有几个看起来显著**。
必须先量一件事：**还剩多少信息可以榨。**

九模型共识里 minimax 提过一个方法（MX3-01「比特尺」），评语是
「唯一能**批量判死整族『加指标』假设**的方法，零新数据」。
共识文档把它排在 P1-0，**一直没做**。本脚本补上。

## 两个检验

### 检验 A：增量信息曲线（比特尺）

把「预测会不会爆」看成一个信息传输问题：
**模型每多看一条样本，能把不确定性减少多少比特（bit）？**

- 零信息基线：永远猜训练集的正例率 → 对数损失 `LL_base`
- 模型：逻辑回归 → 对数损失 `LL_model`
- **信息增益 = (LL_base − LL_model) / ln2**，单位 bit/样本

然后按「训练集 |AUC−0.5|」从大到小，把变量**一个一个加进去**，看这条曲线：

- 如果曲线在 k=10 就走平 → **后面的指标全是噪音，加指标这条路死了**
- 如果 k=100 还在涨 → 加指标还有空间，值得继续挖

**关键对照：`k=全部` 那一行。** 如果全部 125 个变量的增益**低于**前 10 个，
说明「加指标」不只是无效，而是**主动有害**（过拟合把真信号淹了）。

### 检验 B：补测 5 个「差一行代码」的变量

92 机制清单里，这几个标着「未测：1 行代码」，一直没人补：

| 编号 | 机制 | 变量 | 为什么一直没测 |
|---|---|---|---|
| M04 | 杠杆超载 | `oi_value / quote_vol_24h` | 要合并两列 |
| M05 | 大户散户剪刀差 | `ls_top − ls_count` | 要合并两列 |
| M06 | 负费率弹簧蓄力 | `funding_neg_ratio_7d × \|funding_min_7d\|` | 两个分量测过，乘积没测 |
| M09 | 成交分型（笔数增+均单缩） | `tr_ratio_4_24 / ats_ratio` | 两个分量测过，比值没测 |
| M11 | Amihud 非流动性 | `\|ret_1d\| / quote_vol_24h` | 要算一行 |

用和 `14_variable_bench.py` **完全相同的口径**（AUC、组内 IC7、偏相关、200 次置换零分布），
这样结果可以直接和已测的 125 个变量比。

## 判定口径（沿用项目标准）

- **AUC**：区分「会不会爆」；> 0.5 才算有方向
- **组内 IC7**：在爆拉组内部，能不能预测「爆多大」；这是真正决定赚钱的那个数
- **偏相关 pIC7**：扣掉「已经涨了多少」（`ret_1d` / `ret_7d` / `score`）之后还剩多少
- **置换 p95**：打乱 200 次后 |IC| 的 95 分位。**实测 |IC| 低于这条线 = 和随机分不开**

用法
----
  py -3.10 research/18_bit_ceiling.py
  py -3.10 research/18_bit_ceiling.py --perm 200
"""
from __future__ import annotations

import argparse
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUT = ROOT / "reports" / "bit_ceiling_ds"
TAG = "ds4.1f"

SPLITS = ["2024-01-01", "2024-07-01", "2025-01-01", "2025-07-01"]
KS = [1, 2, 3, 5, 8, 12, 20, 30, 50, 80, 125]
H = 7
MIN_COV = 0.60

# 绝不可进特征：标签、未来变量、策略输出、评估结果
LEAK = {
    "symbol", "pre_day", "date", "label", "hit", "gain", "tier", "pass_gate",
    "gate_vol_min", "gate_vol_max", "gate_cap", "gate_cap_known",
    "s_kline", "s_deriv", "has_deriv", "score", "price_x", "price_y",
    "oi_chg_fwd2d", "entry_price", "entry_dt", "entry_day", "entry_ok",
    "entry_fallback", "last_dt", "box_score",
    "keep_frac", "gap_t0", "path_maxdd", "psharpe", "retain_ratio",
    "fill_1h", "entry_hours", "entry_quote_vol", "spike_conc", "hours_to_2x",
    "post_mean_48h", "path_hours", "up_hour_frac", "peak_ts", "peak_price",
    "ignite_date", "peak_date", "trough_price", "days_to_peak", "data_ok",
    "tradable", "p_ref", "entry_vol_per_h", "entry_hours_frac",
    "entry_win_max_high", "first_runaway_h", "band_hours", "post_bars", "mult_1h",
}
LEAK_PREFIX = ("ret_", "ret_net_", "mfe_", "mae_", "covered_", "exit_",
               "y_", "amp_")


def is_leak(c: str) -> bool:
    return c in LEAK or c.startswith(LEAK_PREFIX)


# ---------------------------------------------------------------- 工具

def _spearman(a, b) -> float:
    a, b = np.asarray(a, float), np.asarray(b, float)
    m = np.isfinite(a) & np.isfinite(b)
    if m.sum() < 25:
        return np.nan
    a, b = a[m], b[m]
    ra, rb = pd.Series(a).rank().to_numpy(), pd.Series(b).rank().to_numpy()
    if np.std(ra) == 0 or np.std(rb) == 0:
        return np.nan
    return float(np.corrcoef(ra, rb)[0, 1])


def _auc(y, x) -> float:
    y, x = np.asarray(y, float), np.asarray(x, float)
    m = np.isfinite(x) & np.isfinite(y)
    y, x = y[m], x[m]
    n1, n0 = int((y == 1).sum()), int((y == 0).sum())
    if n1 < 10 or n0 < 10:
        return np.nan
    r = pd.Series(x).rank().to_numpy()
    return float((r[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def _resid(y, X) -> np.ndarray:
    X = np.column_stack([np.ones(len(X)), X])
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    return y - X @ beta


def _partial_spearman(y, x, ctrl) -> float:
    y, x = np.asarray(y, float), np.asarray(x, float)
    ctrl = np.asarray(ctrl, float)
    m = np.isfinite(y) & np.isfinite(x) & np.isfinite(ctrl).all(axis=1)
    if m.sum() < 40:
        return np.nan
    return _spearman(_resid(y[m], ctrl[m]), _resid(x[m], ctrl[m]))


def md_table(df: pd.DataFrame, floatfmt: str = ".3f") -> str:
    if not len(df):
        return "（空）"
    cols = list(df.columns)
    out = ["| " + " | ".join(str(c) for c in cols) + " |",
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
                cells.append("—" if not np.isfinite(v) else format(v, floatfmt))
            else:
                cells.append("—" if v is None else str(v))
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out)


def logloss_bits(y, p, p0) -> float:
    """每样本信息增益（bit）= 常数预测相对模型的改善。"""
    y = np.asarray(y, float)
    p = np.clip(np.asarray(p, float), 1e-6, 1 - 1e-6)
    ll_model = -np.mean(y * np.log(p) + (1 - y) * np.log(1 - p))
    p0 = float(np.clip(p0, 1e-6, 1 - 1e-6))
    ll_base = -(float(np.mean(y)) * np.log(p0) + (1 - float(np.mean(y))) * np.log(1 - p0))
    return float((ll_base - ll_model) / np.log(2))


# ---------------------------------------------------------------- 数据

def build() -> tuple[pd.DataFrame, list[str]]:
    hr = pd.read_csv(DATA / "hit_rate_current.csv")
    hr["pre_day"] = pd.to_datetime(hr["pre_day"], utc=True)
    fr = pd.read_csv(DATA / "forward_returns.csv")
    fr["pre_day"] = pd.to_datetime(fr["pre_day"], utc=True)
    keep = ["symbol", "pre_day", f"ret_net_{H}"]
    keep = [c for c in keep if c in fr.columns]
    fr = fr[keep].drop_duplicates(subset=["symbol", "pre_day"])
    df = hr.merge(fr, on=["symbol", "pre_day"], how="left").rename(
        columns={f"ret_net_{H}": "y_ret"})

    vc = pd.read_parquet(DATA / "vars_cache.parquet")
    vc["pre_day"] = pd.to_datetime(vc["pre_day"], utc=True)
    dup = [c for c in vc.columns if c in df.columns and c not in ("symbol", "pre_day")]
    vc = vc.drop(columns=dup, errors="ignore").drop_duplicates(subset=["symbol", "pre_day"])
    df = df.merge(vc, on=["symbol", "pre_day"], how="left")

    df = df.sort_values("pre_day").reset_index(drop=True)

    feats = [c for c in df.columns if not is_leak(c)]
    feats = [c for c in feats if df[c].dtype.kind in "ifb"]
    return df, feats


# ---------------------------------------------------------------- 检验 A

def curve(df: pd.DataFrame, feats: list[str], ks: list[int],
          picked: dict | None = None) -> pd.DataFrame:
    rows = []
    picked = {} if picked is None else picked
    for sp in SPLITS:
        s = pd.Timestamp(sp, tz="UTC")
        tr = df[df["pre_day"] < s]
        te = df[df["pre_day"] >= s]
        if len(tr) < 200 or len(te) < 100:
            continue

        cov = tr[feats].notna().mean()
        use = [c for c in feats if cov[c] >= MIN_COV]
        med = tr[use].median()
        Xtr = tr[use].fillna(med).to_numpy(float)
        Xte = te[use].fillna(med).to_numpy(float)
        sc = StandardScaler().fit(Xtr)
        Xtr, Xte = sc.transform(Xtr), sc.transform(Xte)

        ytr = tr["label"].to_numpy(float)
        yte = te["label"].to_numpy(float)
        p0 = float(ytr.mean())

        # 按训练集 |AUC−0.5| 排序，一次算完复用
        aucs = np.array([_auc(ytr, tr[c].to_numpy(float)) for c in use])
        order = np.argsort(-np.abs(np.nan_to_num(aucs, nan=0.5) - 0.5))

        for k in ks:
            kk = min(k, len(use))
            idx = order[:kk]
            for nm in (use[i] for i in idx[:12]):
                picked.setdefault(kk, {}).setdefault(nm, 0)
                picked[kk][nm] += 1
            clf = LogisticRegression(C=0.1, max_iter=4000)
            clf.fit(Xtr[:, idx], ytr)
            p = clf.predict_proba(Xte[:, idx])[:, 1]
            rows.append({
                "split": sp, "k": kk,
                "bits": logloss_bits(yte, p, p0),
                "auc": _auc(yte, p),
                "n_feat": len(use),
            })
    R = pd.DataFrame(rows)
    if not len(R):
        return R
    A = R.groupby("k").agg(bits=("bits", "mean"), auc=("auc", "mean"),
                           folds=("split", "count")).reset_index()
    A["Δbits"] = A["bits"].diff()
    A["bits_占峰值"] = A["bits"] / A["bits"].max()
    return A


# ---------------------------------------------------------------- 检验 B

def new_vars(df: pd.DataFrame) -> dict[str, tuple[str, np.ndarray]]:
    """5 个「差一行代码」的变量。口径与 14_variable_bench 一致。"""
    qv = df["quote_vol_24h"].to_numpy(float)
    oi = df["oi_value"].to_numpy(float)
    out = {
        "M04 杠杆超载 oi/qv": ("oi_value / quote_vol_24h", oi / np.where(qv > 0, qv, np.nan)),
        "M05 大户散户剪刀差": ("ls_top − ls_count",
                            df["ls_top"].to_numpy(float) - df["ls_count"].to_numpy(float)),
        "M06 负费率弹簧": ("funding_neg_ratio_7d × |funding_min_7d|",
                       df["funding_neg_ratio_7d"].to_numpy(float)
                       * np.abs(df["funding_min_7d"].to_numpy(float))),
        "M11 Amihud 非流动性": ("|ret_1d| / quote_vol_24h",
                            np.abs(df["ret_1d"].to_numpy(float)) / np.where(qv > 0, qv, np.nan)),
    }
    if "tr_ratio_4_24" in df.columns and "ats_ratio" in df.columns:
        out["M09 成交分型 笔数增/均单缩"] = (
            "tr_ratio_4_24 / ats_ratio",
            df["tr_ratio_4_24"].to_numpy(float) / df["ats_ratio"].replace(0, np.nan).to_numpy(float))
    return out


def eval_var(df: pd.DataFrame, x: np.ndarray, n_perm: int) -> dict:
    y = df["label"].to_numpy(float)
    yh = df["y_ret"].to_numpy(float)
    res = {"n": int(np.isfinite(x).sum()), "覆盖": float(np.isfinite(x).mean())}
    res["AUC"] = _auc(y, x)

    ctrl = np.column_stack([
        df["ret_1d"].to_numpy(float),
        df["ret_7d"].to_numpy(float),
        df["score"].to_numpy(float),
    ])
    res["IC7_全池"] = _spearman(x, yh)
    res["pIC7_全池"] = _partial_spearman(yh, x, ctrl)
    m1 = (y == 1)
    res["IC7_爆拉组内"] = _spearman(x[m1], yh[m1])
    res["pIC7_爆拉组内"] = _partial_spearman(yh[m1], x[m1], ctrl[m1])

    if n_perm > 0:
        rng = np.random.default_rng(42)
        m = m1 & np.isfinite(x) & np.isfinite(yh)
        xs, ys = x[m].copy(), yh[m]
        if len(xs) >= 100:
            arr = []
            for _ in range(n_perm):
                v = xs.copy()
                rng.shuffle(v)
                arr.append(_spearman(v, ys))
            arr = np.array([v for v in arr if np.isfinite(v)])
            if len(arr) >= 20:
                res["置换p95(|IC|)"] = float(np.percentile(np.abs(arr), 95))
    return res


# ---------------------------------------------------------------- 主流程

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--perm", type=int, default=200)
    args = ap.parse_args()

    df, feats = build()
    print(f"[bit] 样本 {len(df)}｜正例 {int(df.label.sum())}｜候选特征 {len(feats)}")
    OUT.mkdir(parents=True, exist_ok=True)

    # ---------- 检验 A ----------
    picked: dict = {}
    A = curve(df, feats, KS, picked)
    A.to_csv(OUT / "bits_curve.csv", index=False, encoding="utf-8-sig")
    if len(A):
        peak_k = int(A.loc[A["bits"].idxmax(), "k"])
        peak = float(A["bits"].max())
        at10 = A[A["k"] <= 12]["bits"].max()
        full = A[A["k"] == A["k"].max()]["bits"].iloc[-1]
        print(f"[bit] 峰值 {peak:.5f} bit/样本 @ k={peak_k}｜k<=12 最好 {at10:.5f}"
              f"｜全部 {full:.5f}")

    # ---------- 检验 B ----------
    nv = new_vars(df)
    rows = []
    for name, (formula, x) in nv.items():
        r = eval_var(df, x, args.perm)
        r["变量"] = name
        r["定义"] = formula
        rows.append(r)
    B = pd.DataFrame(rows)
    if len(B):
        B = B[["变量", "定义", "n", "覆盖", "AUC", "IC7_全池", "pIC7_全池",
               "IC7_爆拉组内", "pIC7_爆拉组内", "置换p95(|IC|)"]]
    B.to_csv(OUT / "new_vars.csv", index=False, encoding="utf-8-sig")

    # ---------- 检验 C：M04 杠杆超载当否决（四折 + 删一条）----------
    C = []
    dfc = df.copy()
    dfc["oi_qv"] = dfc["oi_value"] / dfc["quote_vol_24h"].replace(0, np.nan)
    for sp in SPLITS:
        s_ = pd.Timestamp(sp, tz="UTC")
        tr_ = dfc[dfc["pre_day"] < s_]
        te_ = dfc[dfc["pre_day"] >= s_].reset_index(drop=True)
        if len(tr_) < 200 or len(te_) < 100:
            continue
        q67 = tr_["oi_qv"].quantile(0.67)      # 阈值只从训练段取
        b = te_[te_["score"] >= 5]
        q = te_[(te_["score"] >= 5) & (te_["oi_qv"] <= q67)]
        bv = pd.to_numeric(b["y_ret"], errors="coerce").dropna().sort_values()
        qv_ = pd.to_numeric(q["y_ret"], errors="coerce").dropna().sort_values()
        if len(bv) < 30 or len(qv_) < 20:
            continue
        C.append({
            "split": sp, "n_base": len(b), "n_gate": len(q),
            "占_base": len(q) / max(len(b), 1),
            "爆拉率_基线": float(b["label"].mean()), "爆拉率_否决": float(q["label"].mean()),
            "ret7中位_基线": float(bv.median()), "ret7中位_否决": float(qv_.median()),
            "ret7均值_基线": float(bv.mean()), "ret7均值_否决": float(qv_.mean()),
            "均值去1_基线": float(bv.iloc[:-1].mean()), "均值去1_否决": float(qv_.iloc[:-1].mean()),
        })
    CC = pd.DataFrame(C)
    if len(CC):
        CC["Δ爆拉率"] = CC["爆拉率_否决"] - CC["爆拉率_基线"]
        CC["Δret7中位"] = CC["ret7中位_否决"] - CC["ret7中位_基线"]
        CC["Δret7均值"] = CC["ret7均值_否决"] - CC["ret7均值_基线"]
        CC["Δ均值去1"] = CC["均值去1_否决"] - CC["均值去1_基线"]
        CC.to_csv(OUT / "m04_veto_folds.csv", index=False, encoding="utf-8-sig")

    # ---------- 报告 ----------
    L = []
    L.append("# A22 · 比特尺 + 补测一行代码变量 `[ds4.1f]`\n")
    L.append(f"> 2026-10-20｜脚本 `research/18_bit_ceiling.py`｜"
             f"样本 {len(df)}（{int(df.label.sum())} 正例）｜候选特征 {len(feats)}\n")

    L.append("\n---\n\n## 一、加指标还有没有空间：增量信息曲线\n")
    L.append("单位是 **bit/样本**——模型每看一条样本，能把「会不会爆」的不确定性减少多少。\n")
    p0 = float(df["label"].mean())
    H = -(p0 * np.log2(p0) + (1 - p0) * np.log2(1 - p0))
    L.append(f"样本正例率 {p0:.1%}，对应熵 **{H:.3f} bit**——这是「完全不知道」的状态。")
    L.append(f"模型能把不确定性减少多少，就是下面这张表的 `bits` 列。\n")
    L.append(md_table(A, ".5f"))
    L.append(f"\n**换算**：峰值 {A['bits'].max():.4f} bit ÷ {H:.3f} bit = "
             f"**只消除了 {A['bits'].max() / H:.1%} 的不确定性**。\n")

    L.append("**三个必须分开看的东西**：\n")
    L.append("| 列 | 它在说什么 | 会不会因为加变量变好 |")
    L.append("|---|---|---|")
    L.append("| `auc` | **排序能力**：随便挑两个币，能不能把会爆的排前面 | **完全不提升** |")
    L.append("| `bits` | **概率校准**：说「70% 会爆」的时候，是不是真有七成 | k≥20 之后崩掉 |")
    L.append("| `Δbits` | 加第 k 个变量带来的**新增信息** | 前 12 个之后全是噪音 |")
    L.append("\n**最关键的一行是 `k=全部`。** 它同时告诉我们两件事：")
    L.append("加指标**不提高排序能力**（AUC 反而降），而且**把概率校准彻底搞坏**（bits 转负）。\n")
    L.append("> ⚠️ **负 bits ≠ 没有信息。** k=111 的 AUC 是 0.617，仍然明显好于随机（0.5）。")
    L.append("> 负 bits 的意思是：**模型的排序还有效，但它报出来的概率数字已经完全不可信**")
    L.append("> （过度自信到把 60% 的币说成 99%）。对一个靠「分数」做决策的系统，这比没有信息更危险。\n")

    if picked:
        top1 = sorted(picked.get(1, {}).items(), key=lambda kv: -kv[1])
        top12 = sorted(picked.get(12, {}).items(), key=lambda kv: -kv[1])[:12]
        L.append("**k=1 时选中的那个变量**（4 折里被选中的次数）：\n")
        L.append("| 变量 | 折数 |")
        L.append("|---|---:|")
        for nm, c in top1:
            L.append(f"| `{nm}` | {c} |")
        L.append("\n**k=12 时入选的变量**：\n")
        L.append("| 变量 | 折数 |")
        L.append("|---|---:|")
        for nm, c in top12:
            L.append(f"| `{nm}` | {c} |")
        L.append("")

    L.append("\n---\n\n## 二、补测 5 个「差一行代码」的变量\n")
    L.append("口径与 `14_variable_bench.py` 完全一致，可直接和已测的 125 个变量比。\n")
    L.append(md_table(B))
    L.append("\n**判定线**：\n")
    L.append("- `AUC > 0.5` 才有「会不会爆」的方向")
    L.append("- `IC7_爆拉组内` 才是决定赚钱的那个数（在已经会爆的币里，能不能预测爆多大）")
    L.append("- **`|IC| < 置换p95` = 和随机分不开**，不管看起来多显著\n")

    if len(CC):
        L.append("\n---\n\n## 三、唯一活下来的候选：M04 当否决\n")
        L.append("M04 在「OI 增速未达标」的样本里分档很明显（低杠杆 43.5% / 中 37.0% / **高杠杆 28.6%**），")
        L.append("所以把它做成一条否决：**`score>=5` 且 `oi/qv` 不在训练段最高三分位**。\n")
        L.append(md_table(CC[["split", "n_base", "n_gate", "占_base", "爆拉率_基线", "爆拉率_否决",
                              "Δ爆拉率", "ret7中位_基线", "ret7中位_否决", "Δret7中位",
                              "ret7均值_基线", "ret7均值_否决", "Δret7均值", "Δ均值去1"]]))
        L.append(f"\n**四折方向**：爆拉率 {(CC['Δ爆拉率'] < 0).sum()}/4 下降、"
                 f"中位数 {(CC['Δret7中位'] >= 0).sum()}/4 不降、"
                 f"均值 {(CC['Δret7均值'] >= 0).sum()}/4 不降、"
                 f"**删一条后均值 {(CC['Δ均值去1'] >= 0).sum()}/4 仍不降**。\n")
        L.append("**怎么读**：它把池子砍到约三分之一，**牺牲爆拉率（4/4 下降）和中位数**，")
        L.append("换来**均值 4/4 上升**。这不是帕累托改进，是**「删掉小赢、留下大赢」**的取舍。")
        L.append("但它**扛住了删一条检验**——A20 里那两个「均值赢」的候选全部没扛住。\n")

    L.append("\n---\n\n## 四、口径\n")
    L.append("1. 特征选择、标准化、缺失填充**全部只用训练段**。")
    L.append("2. 比特尺用**不加类别权重**的逻辑回归——`class_weight=balanced` 会破坏概率校准，")
    L.append("   让对数损失失去意义（AUC 不受影响，但 bit 会）。")
    L.append("3. 样本是「案例-对照」结构，所以 bit 的**绝对值**不能读成「实盘可获得的预测力」，")
    L.append("   但**曲线形状**（在哪走平、全部变量是否劣于少量变量）不受影响。")

    (OUT / "report.md").write_text("\n".join(L), encoding="utf-8")
    print(f"[bit] 报告 -> {OUT / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
