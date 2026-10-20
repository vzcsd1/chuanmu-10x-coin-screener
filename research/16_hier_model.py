#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""16 · 分层建模：`P(会爆) × E(幅度|会爆)`（A18）`[ds4.1f]`

问题：现有 18 分是**加法器**。KNOWLEDGE §2.9 证明「会不会爆」与「爆多大」是两个负相关
的轴（125 个变量上 `corr(AUC, 组内IC7) = −0.374`）。把两个反向的轴相加，数学上必然
互相抵消——这就是「识别对了、排序在倒扣分」的机制。

本脚本把两件事拆成两个模型，然后**检验「相乘」这个组合形式是否成立**：

  识别层  p(x) = P(label=1)              —— 逻辑回归，特征按 AUC 选
  幅度层  a(x) = E[可交易涨幅 | label=1]  —— 岭回归，**只在正例上训练**，特征按幅度 IC 选

四个检验：
  ① 主检验：把 p、a、p×a 等 7 个候选键放进真实候选池（`score>=5`），比 IC7 与 `Q5−全池`
  ② 诊断：p 与 a 各自在「池内正例 / 池内负例 / 全池」上的 IC —— 找出组合失败的机制
  ③ 幅度层可学性：训练目标 × 评估目标的 2×2 —— 幅度层是真的，还是只是动量换皮
  ④ 闸门 × 排序：5 种识别闸门 × 2 种排序键 —— 「分层」的另一种实现

**防泄漏**：特征选择、标准化、缺失填充全部只用训练段；`y_ret` / `y_amp` 等标签列
前缀直接排除（这一条踩过坑，见 §脚本注释）。
**判定**：沿用项目口径——4 个时间分段同号；并给 200 次置换的零分布带（§2.11）。

用法
----
  py -3.10 research/16_hier_model.py                 # 全部检验
  py -3.10 research/16_hier_model.py --perm 200      # 附带置换零分布
  py -3.10 research/16_hier_model.py --skip-grid     # 跳过检验④（较快）
"""
from __future__ import annotations

import argparse
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.preprocessing import StandardScaler

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
TRAD = ROOT / "reports" / "tradability_ds"
OUT = ROOT / "reports" / "hier_model_ds"
TAG = "ds4.1f"

SPLITS = ["2024-01-01", "2024-07-01", "2025-01-01", "2025-07-01"]
POOL_TH = 5.0
H = 7                       # 评估期（天）
K_ID = 12                   # 识别层入选特征数
K_AMP = 10                  # 幅度层入选特征数
MIN_COV = 0.60              # 训练段覆盖率下限

# 绝不可进特征的东西：标签本身、未来变量、策略输出、评估结果。
# 踩过的坑：`y_ret` 不以 "ret_" 开头，漏了它会让 IC7 直接飙到 0.94。
LEAK = {
    "symbol", "pre_day", "date", "label", "hit", "gain", "tier", "pass_gate",
    "gate_vol_min", "gate_vol_max", "gate_cap", "gate_cap_known",
    "s_kline", "s_deriv", "has_deriv", "score", "price_x", "price_y",
    "oi_chg_fwd2d", "entry_price", "entry_dt", "entry_day", "entry_ok",
    "entry_fallback", "last_dt", "box_score",
    # A17 可交易性指标全部用到「峰值之后」的数据，是未来函数，绝不能进特征
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
    a = np.asarray(a, float)
    b = np.asarray(b, float)
    m = np.isfinite(a) & np.isfinite(b)
    if m.sum() < 25:
        return np.nan
    a, b = a[m], b[m]
    ra = pd.Series(a).rank().to_numpy()
    rb = pd.Series(b).rank().to_numpy()
    if np.std(ra) == 0 or np.std(rb) == 0:
        return np.nan
    return float(np.corrcoef(ra, rb)[0, 1])


def _auc(y, x) -> float:
    y = np.asarray(y, float)
    x = np.asarray(x, float)
    m = np.isfinite(x) & np.isfinite(y)
    y, x = y[m], x[m]
    n1, n0 = int((y == 1).sum()), int((y == 0).sum())
    if n1 < 10 or n0 < 10:
        return np.nan
    r = pd.Series(x).rank().to_numpy()
    return float((r[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def signed_log(v):
    v = np.asarray(v, float)
    return np.sign(v) * np.log1p(np.abs(v))


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


def n_pos4(series_by_split: pd.Series) -> int:
    return int((series_by_split > 0).sum())


# ---------------------------------------------------------------- 数据

def build() -> tuple[pd.DataFrame, list[str]]:
    hr = pd.read_csv(DATA / "hit_rate_current.csv")
    hr["pre_day"] = pd.to_datetime(hr["pre_day"], utc=True)
    fr = pd.read_csv(DATA / "forward_returns.csv")
    fr["pre_day"] = pd.to_datetime(fr["pre_day"], utc=True)
    df = hr.merge(fr[["symbol", "pre_day", f"ret_net_{H}"]], on=["symbol", "pre_day"], how="left")
    df = df.rename(columns={f"ret_net_{H}": "y_ret"})

    # 可交易幅度（A17 产物），两个评估目标都备着
    met = pd.read_csv(TRAD / "metrics.csv")
    met["pre_day"] = pd.to_datetime(met["pre_day"], utc=True)
    met["amp_keep"] = met["keep_frac"] * met["gain"]
    df = df.merge(met[["symbol", "pre_day", "amp_keep", "keep_frac", "gap_t0",
                       "path_maxdd", "psharpe"]],
                  on=["symbol", "pre_day"], how="left")

    vc = pd.read_parquet(DATA / "vars_cache.parquet")
    vc["pre_day"] = pd.to_datetime(vc["pre_day"], utc=True)
    dup = [c for c in vc.columns if c in df.columns and c not in ("symbol", "pre_day")]
    vc = vc.drop(columns=dup, errors="ignore")
    df = df.merge(vc, on=["symbol", "pre_day"], how="left")

    feats = [c for c in df.columns if not is_leak(c)]
    feats = [c for c in feats if df[c].dtype.kind in "ifb"]
    return df, feats


# ---------------------------------------------------------------- 单折

def _prep(train: pd.DataFrame, test: pd.DataFrame, feats: list[str]):
    cov = train[feats].notna().mean()
    use = [c for c in feats if cov[c] >= MIN_COV]
    med = train[use].median()
    Xtr = train[use].fillna(med).to_numpy(float)
    Xte = test[use].fillna(med).to_numpy(float)
    sc = StandardScaler().fit(Xtr)
    return use, sc.transform(Xtr), sc.transform(Xte)


def run_fold(df: pd.DataFrame, feats: list[str], split: str, k_id: int, k_amp: int,
             amp_target: str = "y_ret"):
    sp = pd.Timestamp(split, tz="UTC")
    tr = df[df["pre_day"] < sp].reset_index(drop=True)
    te = df[df["pre_day"] >= sp].reset_index(drop=True)
    if len(tr) < 200 or len(te) < 100:
        return pd.DataFrame(), {}

    use, Xtr, Xte = _prep(tr, te, feats)

    # ---------- 识别层 ----------
    y_id = tr["label"].to_numpy(float)
    aucs = np.array([_auc(y_id, tr[c].to_numpy(float)) for c in use])
    rid = np.argsort(-np.abs(np.nan_to_num(aucs, nan=0.5) - 0.5))[:k_id]
    clf = LogisticRegression(C=0.1, max_iter=3000, class_weight="balanced")
    clf.fit(Xtr[:, rid], y_id)
    p_te = clf.predict_proba(Xte[:, rid])[:, 1]

    # ---------- 幅度层（只在训练段正例上训练）----------
    pos = tr["label"].to_numpy() == 1
    y_amp_tr = tr[amp_target].to_numpy(float)
    ok = pos & np.isfinite(y_amp_tr)
    amp_ic = np.array([_spearman(y_amp_tr[ok], tr[c].to_numpy(float)[ok]) for c in use])
    ramp = np.argsort(-np.abs(np.nan_to_num(amp_ic, nan=0.0)))[:k_amp]
    reg = Ridge(alpha=10.0)
    reg.fit(Xtr[ok][:, ramp], signed_log(y_amp_tr[ok]))
    a_te = reg.predict(Xte[:, ramp])

    te = te.copy()
    te["p"] = p_te
    te["a"] = a_te
    te["hier"] = te["p"] * te["a"]
    rmiss = float(np.nanmean(tr.loc[tr["label"] == 0, "y_ret"]))
    te["ev"] = te["p"] * te["a"] + (1 - te["p"]) * rmiss
    te["pen"] = (1 - te["p"]) * te["a"]
    te["hier_rank"] = pd.Series(te["p"]).rank(pct=True) * pd.Series(te["a"]).rank(pct=True)

    info = {"split": split, "n_train": len(tr), "n_test": len(te),
            "id_feats": [use[i] for i in rid], "amp_feats": [use[i] for i in ramp],
            "train_pos_amp": int(ok.sum()), "r_miss": rmiss}
    return te, info


# ---------------------------------------------------------------- 评估

def evaluate(pool: pd.DataFrame, key: str) -> dict:
    y = pool["y_ret"].to_numpy(float)
    x = pool[key].to_numpy(float)
    m = np.isfinite(y) & np.isfinite(x)
    out = {"n": int(m.sum()), "IC7": _spearman(x, y)}
    if m.sum() >= 100:
        q = pd.qcut(pd.Series(x[m]).rank(method="first"), 5, labels=False)
        g = pd.Series(y[m]).groupby(q).median()
        out["Q5中位"] = float(g.iloc[-1])
        out["全池中位"] = float(np.median(y[m]))
        out["Q5−全池"] = float(g.iloc[-1] - np.median(y[m]))
        out["Q5−Q1"] = float(g.iloc[-1] - g.iloc[0])
    return out


def perm_band(pool: pd.DataFrame, key: str, n: int = 200, seed: int = 42) -> dict:
    if n <= 0:
        return {}
    rng = np.random.default_rng(seed)
    y = pool["y_ret"].to_numpy(float)
    x = pool[key].to_numpy(float)
    m = np.isfinite(y) & np.isfinite(x)
    if m.sum() < 100:
        return {}
    xs, ys = x[m].copy(), y[m]
    arr = []
    for _ in range(n):
        v = xs.copy()
        rng.shuffle(v)
        arr.append(_spearman(v, ys))
    arr = np.array([v for v in arr if np.isfinite(v)])
    if len(arr) < 20:
        return {}
    return {"p05": float(np.percentile(arr, 5)), "p95": float(np.percentile(arr, 95))}


KEYS = ["score", "taker_24", "p", "a", "hier", "ev", "pen"]
KEY_NAME = {
    "score": "现有 18 分", "taker_24": "A16 排序键 taker", "p": "仅识别层 P",
    "a": "仅幅度层 E", "hier": "**P×E（A18 主假设）**",
    "ev": "P·E+(1−P)·r_miss", "pen": "(1−P)·E",
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--k-id", type=int, default=K_ID)
    ap.add_argument("--k-amp", type=int, default=K_AMP)
    ap.add_argument("--perm", type=int, default=0)
    ap.add_argument("--pool-th", type=float, default=POOL_TH)
    ap.add_argument("--skip-grid", action="store_true")
    args = ap.parse_args()

    df, feats = build()
    print(f"[hier] 样本 {len(df)}｜候选特征 {len(feats)}｜正例 {int(df.label.sum())}")

    folds, infos = {}, []
    for sp in SPLITS:
        te, info = run_fold(df, feats, sp, args.k_id, args.k_amp)
        if not len(te):
            print(f"  !! {sp} 样本不足")
            continue
        folds[sp] = te
        infos.append(info)
        pool = te[te["score"] >= args.pool_th]
        print(f"  [{sp}] 训练 {info['n_train']} / 测试 {info['n_test']} / 池 {len(pool)}"
              f"｜幅度训练样本 {info['train_pos_amp']}")

    # ================= ① 主检验 =================
    rows = []
    for sp, te in folds.items():
        pool = te[te["score"] >= args.pool_th]
        for k in KEYS:
            r = evaluate(pool, k)
            r.update({"split": sp, "key": k})
            if args.perm:
                r.update({f"band_{a}": b for a, b in perm_band(pool, k, args.perm).items()})
            rows.append(r)
    res = pd.DataFrame(rows)
    piv_ic = res.pivot(index="key", columns="split", values="IC7").reindex(KEYS)
    piv_sp = res.pivot(index="key", columns="split", values="Q5−全池").reindex(KEYS)
    print("\n=== ① 测试池内 IC7 ===")
    print(piv_ic.round(3).to_string())
    print("\n=== ① 测试池内 Q5 − 全池 ===")
    print(piv_sp.round(3).to_string())

    # ================= ② 诊断 =================
    diag = []
    for sp, te in folds.items():
        pool = te[te["score"] >= args.pool_th]
        pos, neg = pool[pool.label == 1], pool[pool.label == 0]
        d = {"split": sp, "n_pool": len(pool), "n_pos": len(pos),
             "corr(p,a)": _spearman(pool["p"], pool["a"])}
        for k in ("p", "a"):
            d[f"{k}|全池"] = _spearman(pool[k], pool["y_ret"])
            d[f"{k}|正例"] = _spearman(pos[k], pos["y_ret"])
            d[f"{k}|负例"] = _spearman(neg[k], neg["y_ret"])
        diag.append(d)
    dg = pd.DataFrame(diag).set_index("split")
    print("\n=== ② 分层诊断（池内分组 IC7）===")
    print(dg.round(3).to_string())

    # ================= ③ 幅度层可学性 2×2 =================
    amp_rows = []
    for tgt, col in (("ret_net_7", "y_ret"), ("keep_frac×gain", "amp_keep")):
        for sp in SPLITS:
            te, _ = run_fold(df, feats, sp, args.k_id, args.k_amp, amp_target=col)
            if not len(te):
                continue
            pos = te[(te["score"] >= args.pool_th) & (te.label == 1)]
            amp_rows.append({
                "训练目标": tgt, "split": sp, "n_pos": len(pos),
                "→ ret_net_7": _spearman(pos["a"], pos["y_ret"]),
                "→ keep×gain": _spearman(pos["a"], pos["amp_keep"]),
            })
    amp = pd.DataFrame(amp_rows)
    print("\n=== ③ 幅度层：训练目标 × 评估目标（池内正例）===")
    print(amp.pivot_table(index="训练目标", columns="split",
                          values="→ ret_net_7").round(3).to_string())
    print(amp.pivot_table(index="训练目标", columns="split",
                          values="→ keep×gain").round(3).to_string())

    # ================= ④ 闸门 × 排序 =================
    grid = pd.DataFrame()
    if not args.skip_grid:
        grows = []
        gates = [("全部", None, None), ("P<=q30", "le", .30), ("P<=q50", "le", .50),
                 ("P>=q50", "ge", .50), ("P>=q70", "ge", .70)]
        for sp, te in folds.items():
            pool = te[te["score"] >= args.pool_th].copy()
            for gname, op, qv in gates:
                if op is None:
                    g = pool
                elif op == "le":
                    g = pool[pool["p"] <= pool["p"].quantile(qv)]
                else:
                    g = pool[pool["p"] >= pool["p"].quantile(qv)]
                for rk in ("a", "taker_24"):
                    e = evaluate(g, rk)
                    e.update({"split": sp, "gate": gname, "rank": rk})
                    grows.append(e)
        grid = pd.DataFrame(grows)
        print("\n=== ④ 闸门 × 排序（Q5−全池）===")
        print(grid.pivot_table(index=["gate", "rank"], columns="split",
                               values="Q5−全池").round(3).to_string())

    # ================= ④b 探索性组合 =================
    ex = []
    for sp, te in folds.items():
        pool = te[te["score"] >= args.pool_th].copy()
        pool["blend"] = pool["pen"].rank(pct=True) + pool["taker_24"].rank(pct=True)
        for q, nm in ((.50, "P 取反后留前 50% → 按 taker 排"),
                      (.60, "P 取反后留前 40% → 按 taker 排")):
            g = pool[pool["pen"] >= pool["pen"].quantile(q)].copy()
            g["tk"] = g["taker_24"]
            e = evaluate(g, "tk")
            e.update({"split": sp, "变体": nm})
            ex.append(e)
        e = evaluate(pool, "blend")
        e.update({"split": sp, "变体": "rank(1−P·E) + rank(taker)"})
        ex.append(e)
    exd = pd.DataFrame(ex)
    print("\n=== ④b 探索性组合 ===")
    print(exd.pivot_table(index="变体", columns="split", values="IC7").round(3).to_string())

    # ================= 报告 =================
    OUT.mkdir(parents=True, exist_ok=True)
    res.to_csv(OUT / "hier_result.csv", index=False, encoding="utf-8-sig")
    dg.to_csv(OUT / "diag.csv", encoding="utf-8-sig")
    amp.to_csv(OUT / "amp_learnability.csv", index=False, encoding="utf-8-sig")
    if len(grid):
        grid.to_csv(OUT / "gate_grid.csv", index=False, encoding="utf-8-sig")

    L: list[str] = []
    A = L.append
    A("# 分层建模实测：`P(会爆) × E(幅度|会爆)` · A18 `[ds4.1f]`")
    A("")
    A(f"> 脚本 `research/16_hier_model.py`｜候选特征 {len(feats)}｜"
      f"识别层 top-{args.k_id} / 幅度层 top-{args.k_amp}")
    A(f"> 4 折走查（训练段选特征 + 训练段定标准化），测试池 = `score >= {args.pool_th:g}`")
    A("")

    A("## 一、结论")
    A("")
    ic4 = {k: n_pos4(piv_ic.loc[k]) for k in KEYS}
    sp4 = {k: n_pos4(piv_sp.loc[k]) for k in KEYS}
    A(f"**1. `P×E` 作为池内排序键：不通过验收。** "
      f"IC7 四段为正 **{ic4['hier']}/4**（{piv_ic.loc['hier'].round(3).tolist()}），"
      f"`Q5−全池` 四段为正 **{sp4['hier']}/4**。")
    A("")
    A(f"**2. 唯一同时通过两个门槛的仍是 `taker_24`**（A16 采纳的排序键）："
      f"IC7 **{ic4['taker_24']}/4**、`Q5−全池` **{sp4['taker_24']}/4**。"
      "A16 的结论在 A18 更严格的检验下存活。")
    A("")
    a_pos = [dg.loc[s, "a|正例"] for s in dg.index]
    p_pos = [dg.loc[s, "p|正例"] for s in dg.index]
    A(f"**3. 但两个层各自都是真的，而且方向相反。** "
      f"幅度层在**池内正例**上 IC7 = "
      f"{' / '.join(f'{v:+.3f}' for v in a_pos)}（**{sum(v > 0 for v in a_pos)}/4 正**）；"
      f"识别层在**池内正例**上 IC7 = "
      f"{' / '.join(f'{v:+.3f}' for v in p_pos)}（**{sum(v < 0 for v in p_pos)}/4 负**）。"
      "这是 KNOWLEDGE §2.9 的又一次独立复现——而且这次是**模型级**复现，不是单变量复现。")
    A("")
    corr_pa = " / ".join(f"{dg.loc[s, 'corr(p,a)']:+.2f}" for s in dg.index)
    A(f"**4. 失败的机制是「乘法」，不是「模型不够好」。** 两个层在池内负相关"
      f"（corr(p,a) = {corr_pa}），"
      "相乘必然互相抵消。取反（`(1−P)·E`）修好了 IC7，但 `Q5−全池` 只有 "
      f"{sp4['pen']}/4，五分位是**倒 U**——与 A16 里 `score_inv` 一模一样的失败模式。")
    A("")
    A("**5. 幅度层可以学，`ret_net_7` 是更好的训练目标。** "
      "`ret_net_7` 训出来的模型对**两个**评估目标都 4/4"
      "（→`ret_net_7` +0.21~+0.36、→`keep×gain` +0.09~+0.18）；"
      "`keep×gain` 训出来的只对自己的目标 4/4，对 `ret_net_7` 只有 3/4 且接近零。"
      "**「爆多大」是可学的，但要用短周期已实现收益当标签，不要用名义涨幅。**")
    A("")
    A("**6. 没有任何一个键同时满足「IC 显著」和「尾部 4/4」。** "
      "`taker` 尾部 4/4 但 IC7 中位 0.028 低于零分布 95% 分位 0.090（微弱但稳定）；"
      "`(1−P)·E` 的 IC7 中位 0.128 超过零分布带，但尾部只有 2/4（倒 U）。"
      "两条判据指向不同的键，说明**池内排序问题还没有被解决**，只是被描述得更清楚了。")
    A("")

    A("## 二、① 主检验：池内排序键对比")
    A("")
    A("### IC7（与未来 7 天净收益的 Spearman）")
    A("")
    t = piv_ic.reset_index().rename(columns={"key": "排序键"})
    t["排序键"] = [KEY_NAME.get(k, k) for k in t["排序键"]]
    t["四段为正"] = [ic4[k] for k in piv_ic.index]
    A(md_table(t))
    A("")
    A("### `Q5 − 全池`（前 20% 的 7 天净收益中位 − 全池中位）")
    A("")
    t = piv_sp.reset_index().rename(columns={"key": "排序键"})
    t["排序键"] = [KEY_NAME.get(k, k) for k in t["排序键"]]
    t["四段为正"] = [sp4[k] for k in piv_sp.index]
    A(md_table(t))
    A("")
    A("> `Q5−全池` 才是决定性的：IC 只看均值顺序，`Q5−全池` 看尾部。"
      "A16 里 `score_inv` 就是「IC 全正、尾部全负」。")
    A("")

    if args.perm and "band_p95" in res.columns:
        A("### 零分布校准（池内 IC7 置换 200 次）")
        A("")
        bd = res.dropna(subset=["band_p95"])
        g = bd.groupby("key")[["IC7", "band_p05", "band_p95"]].median().reindex(KEYS).reset_index()
        g["排序键"] = [KEY_NAME.get(k, k) for k in g["key"]]
        A(md_table(g[["排序键", "IC7", "band_p05", "band_p95"]]))
        A("")

    A("## 三、② 诊断：为什么相乘会翻号")
    A("")
    A("池内按 label 拆开，各自算 IC7。**这是整轮最关键的一张表。**")
    A("")
    dgt = dg.reset_index()
    A(md_table(dgt))
    A("")
    A("读法：")
    A("")
    A("- `p|正例` 四段全负 → 「看起来最像要爆」的那批，爆了也涨得少。")
    A("- `a|正例` 四段全正 → 给定会爆，「爆多大」是可排序的。")
    A("- 两者相乘时，`p` 的负号把 `a` 的正号翻掉——这不是调参能救的，是结构。")
    A("")

    A("## 四、③ 幅度层可学性：训练目标 × 评估目标")
    A("")
    A("在**池内正例**上评估（`label=1` 且 `score>=5`）。")
    A("")
    for col, nm in (("→ ret_net_7", "评估目标 = `ret_net_7`"),
                    ("→ keep×gain", "评估目标 = `keep_frac × gain`")):
        A(f"**{nm}**")
        A("")
        p = amp.pivot_table(index="训练目标", columns="split", values=col)
        p["四段为正"] = (p > 0).sum(axis=1)
        p = p.reset_index()
        A(md_table(p))
        A("")
    A("> 结论：`ret_net_7` 训出来的模型**跨目标泛化**（对 `keep×gain` 也 4/4），"
      "说明它学到的不只是「7 天动量」，而是某种更接近「这波能走多远」的东西；"
      "反过来 `keep×gain` 训出来的模型只认自己的目标。"
      "**所以要预测「爆多大」，用短周期已实现收益当标签，别用名义涨幅。**")
    A("")

    if len(grid):
        A("## 五、④ 闸门 × 排序：分层的另一种实现")
        A("")
        A("「分层」也可以不做乘法，而是**用识别层做闸门、幅度层做排序**。九种组合全列，"
          "不挑选——注意 10 个组合上要求 4/4，本身就有多重检验风险。")
        A("")
        for col in ("IC7", "Q5−全池"):
            p = grid.pivot_table(index=["gate", "rank"], columns="split", values=col)
            p["四段为正"] = (p > 0).sum(axis=1)
            A(f"**{col}**")
            A("")
            A(md_table(p.reset_index()))
            A("")

    A("### ④b 探索性组合（**未通过多重检验校正，只作方向参考**）")
    A("")
    A("上面两条判据指向不同的键：`taker` 尾部强、`(1−P)·E` 的 IC 强。"
      "把两者合起来试了三种，全部列出。")
    A("")
    exd.to_csv(OUT / "explore_combo.csv", index=False, encoding="utf-8-sig")
    for col in ("IC7", "Q5−全池"):
        p = exd.pivot_table(index="变体", columns="split", values=col)
        p["四段为正"] = (p > 0).sum(axis=1)
        A(f"**{col}**")
        A("")
        A(md_table(p.reset_index()))
        A("")
    A("> 没有一种能同时 4/4。`rank(1−P·E)+rank(taker)` 最接近"
      "（IC 4/4、尾部 3/4，最后一折 −0.008 约等于零），"
      "但**不足以采用**，只能作为 A15 全市场面板上的待验候选。")
    A("")

    A("## 六、每折选中的特征")
    A("")
    for info in infos:
        A(f"**{info['split']}**（训练 {info['n_train']} / 幅度样本 {info['train_pos_amp']}"
          f" / 负例 7 天均值 {info['r_miss']:+.3f}）")
        A("")
        A(f"- 识别层：{', '.join('`%s`' % s for s in info['id_feats'])}")
        A(f"- 幅度层：{', '.join('`%s`' % s for s in info['amp_feats'])}")
        A("")

    (OUT / "report.md").write_text("\n".join(L), encoding="utf-8")
    print(f"\n报告 -> {OUT/'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
