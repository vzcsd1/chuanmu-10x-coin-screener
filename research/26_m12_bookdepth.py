#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""M12 盘口 ±5% 厚度不对称 —— 从 bookDepth 归档回补后第一次真正可测（2026-10-21）

背景
----
`变量清单_92机制可算性分级_20261020.md` 把 M12 标成「C（前向）、不可回补」。
2026-10-21 实测发现 **判断错了**：Binance `futures/um/daily/bookDepth` **仍在每日发布**，
内容正好是 M12 需要的——每 30 秒一个快照、±1~5% 分带、带 depth 与 notional。
所以 M12 从「等半年后才能测」变成「回补 0.3GB 就能测」。

数据口径（踩过的坑，别改）
--------------------------
1. **`notional` 是累计深度，不是单档。** 同一个快照里，同一侧随 |percentage| 增大而递增
   （实测 1000CATUSDT 2025-04-11：买 1%→5% 为 90k→255k，卖 1%→5% 为 65k→171k）。
   所以取 `percentage=±5` 的两行就够，**绝对不要把十档加总**（会重复计算 4 次）。
2. `percentage` 负数 = 买盘（mid 下方），正数 = 卖盘。
3. 时间戳是 UTC，一天 2,880 个快照（间隔 30 秒）。
4. **决策时点统一取 `pre_day` 22:55 UTC**，与 `05_deriv_features.py` 的 `ASOF_HHMM` 一致
   （M69 前视修正）。取「不超过 22:55 的最后一个快照」。

特征（全部用比值，避免被市值/流动性规模带跑）
----------------------------------------------
    bd_log5      末快照 log(买5% / 卖5%)          —— 主口径：±5% 厚度不对称
    bd_log1      末快照 log(买1% / 卖1%)          —— 只看贴身盘口
    bd_log5_day  全天 log 比值的均值              —— 剔掉「刚好那一刻」的噪音
    bd_log5_std  全天 log 比值的标准差            —— 挂单稳不稳
    bd_log5_chg  末 2 小时均值 − 之前均值         —— 托单是刚加的还是一直在
    bd_depth_usd 买5%+卖5% 的名义额（USDT）       —— 绝对厚度（流动性代理，做冗余检查用）

评价口径与 `21_death_audit.py` / `22_reopen_test.py` 完全一致，不另造一套：
    A. 四种环境 AUC（全样本 / 池内 score>=5 / 池外）+ 收益秩相关 + 换标签
    B. 十分位表（查非线性，U 型不会被 AUC 误判）
    C. 四折 walk-forward 排序键检验 + 随机键零分布（200 次）

用法
----
    py -3.10 research/26_m12_bookdepth.py build    # 抽特征 → data/bookdepth_m12.csv
    py -3.10 research/26_m12_bookdepth.py eval     # 评价（需先 build）
    py -3.10 research/26_m12_bookdepth.py all
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

DATA = Path("data")
BD = DATA / "bookdepth"
CACHE = DATA / "bookdepth_m12.csv"
OUT = Path("reports/m12_bookdepth_ds")

ASOF = "22:55:00"          # 决策时点（UTC），与 05_deriv_features.ASOF_HHMM 一致
MIN_SCORE = 5
SPLITS = ["2024-01-01", "2024-07-01", "2025-01-01", "2025-07-01"]
MIN_N = 40
TAKE = 0.50
N_NULL = 200
NDEC = 10


# ------------------------------------------------------------------ build

def _one_file(sym: str, day: str) -> dict | None:
    """把一个「合约-日」压成一行特征。"""
    p = BD / sym / f"{sym}-{day}.parquet"
    if not p.exists():
        return None
    try:
        df = pd.read_parquet(p, columns=["timestamp", "percentage", "notional"])
    except Exception:  # noqa: BLE001
        return None
    if df.empty:
        return None
    # 只留 ±5 与 ±1 四行，其余不参与计算（累计口径，加总会重复计算）
    df = df[df["percentage"].isin([-5, -1, 1, 5])]
    if df.empty:
        return None
    w = df.pivot_table(index="timestamp", columns="percentage",
                       values="notional", aggfunc="last")
    for c in (-5, -1, 1, 5):
        if c not in w.columns:
            return None
    w = w.dropna(subset=[-5, 5])
    if w.empty:
        return None
    w.index = pd.to_datetime(w.index)

    eps = 1.0  # 1 USDT，避免 log(0)
    lr5 = np.log((w[-5] + eps) / (w[5] + eps))
    lr1 = np.log((w[-1] + eps) / (w[1] + eps))

    cut = pd.Timestamp(f"{day} {ASOF}")
    before = w.index <= cut
    if before.sum() < 100:            # 快照太少（半天缺数据）直接弃用
        return None
    lr5b, lr1b = lr5[before], lr1[before]
    last5 = float(lr5b.iloc[-1])
    last1 = float(lr1b.iloc[-1])

    # 末 2 小时 vs 之前
    t2 = lr5b.index >= (cut - pd.Timedelta(hours=2))
    tail = float(lr5b[t2].mean()) if t2.sum() >= 20 else np.nan
    head = float(lr5b[~t2].mean()) if (~t2).sum() >= 20 else np.nan

    return {
        "symbol": sym, "pre_day": day,
        "bd_log5": last5,
        "bd_log1": last1,
        "bd_log5_day": float(lr5b.mean()),
        "bd_log5_std": float(lr5b.std()),
        "bd_log5_chg": tail - head if (tail == tail and head == head) else np.nan,
        "bd_depth_usd": float((w.loc[w.index <= cut, -5].iloc[-1] + w.loc[w.index <= cut, 5].iloc[-1])),
        "bd_n_snap": int(before.sum()),
    }


def cmd_build() -> int:
    h = pd.read_csv(DATA / "hit_rate_current.csv", usecols=["symbol", "pre_day", "label"])
    pairs = h[["symbol", "pre_day"]].drop_duplicates()
    print(f"[build] 样本 {len(h)}｜唯一 合约-日 {len(pairs)}")
    rows: list[dict] = []
    t0 = time.time()
    for i, r in enumerate(pairs.itertuples(index=False), 1):
        d = _one_file(r.symbol, str(r.pre_day)[:10])
        if d is not None:
            rows.append(d)
        if i % 100 == 0:
            print(f"  {i}/{len(pairs)}  已抽 {len(rows)}  {time.time()-t0:.0f}s", flush=True)
    F = pd.DataFrame(rows)
    F.to_csv(CACHE, index=False, encoding="utf-8-sig")
    print(f"[build] 完成 {len(F)}/{len(pairs)}（{len(F)/len(pairs):.1%}）→ {CACHE}｜{time.time()-t0:.0f}s")
    return 0


# ------------------------------------------------------------------ 工具（与 21 一致）

def _rank_auc(y: np.ndarray, x: np.ndarray) -> float:
    m = ~(np.isnan(x) | np.isnan(y))
    y, x = y[m], x[m]
    n1 = int(y.sum())
    n0 = len(y) - n1
    if n1 < 8 or n0 < 8:
        return np.nan
    r = pd.Series(x).rank().to_numpy()
    return float((r[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def _spread(x: pd.Series, y: pd.Series, q: int = 5) -> tuple[float, float, float]:
    d = pd.DataFrame({"x": x, "y": y}).dropna()
    if len(d) < q * 12:
        return (np.nan, np.nan, np.nan)
    lo = d["x"].quantile(1 / q)
    hi = d["x"].quantile(1 - 1 / q)
    top = d[d["x"] >= hi]["y"].mean()
    bot = d[d["x"] <= lo]["y"].mean()
    return (float(top), float(bot), float(top - bot))


def _boot_auc(y: np.ndarray, x: np.ndarray, n: int = 500, seed: int = 11) -> tuple[float, float]:
    rng = np.random.default_rng(seed)
    m = ~(np.isnan(x) | np.isnan(y))
    y, x = y[m], x[m]
    if len(y) < 40:
        return (np.nan, np.nan)
    vals = []
    for _ in range(n):
        idx = rng.integers(0, len(y), len(y))
        vals.append(_rank_auc(y[idx], x[idx]))
    v = np.array([a for a in vals if a == a])
    return (float(np.percentile(v, 2.5)), float(np.percentile(v, 97.5)))


def load() -> pd.DataFrame:
    h = pd.read_csv(DATA / "hit_rate_current.csv", parse_dates=["pre_day"])
    h["pre_day"] = pd.to_datetime(h["pre_day"], utc=True).dt.tz_convert(None).dt.normalize()
    f = pd.read_csv(CACHE, parse_dates=["pre_day"])
    f["pre_day"] = pd.to_datetime(f["pre_day"], utc=True).dt.tz_convert(None).dt.normalize()
    fr = pd.read_csv(DATA / "forward_returns.csv", parse_dates=["pre_day"])
    fr["pre_day"] = pd.to_datetime(fr["pre_day"], utc=True).dt.tz_convert(None).dt.normalize()

    d = h.merge(f, on=["symbol", "pre_day"], how="left")
    d = d.merge(fr[["symbol", "pre_day", "ret_net_7", "ret_net_14", "ret_net_30"]],
                on=["symbol", "pre_day"], how="left")
    d["label"] = d["label"].astype(float)
    d["y3"] = np.where(d["gain"] >= 2.0, 1.0, 0.0)
    d["y5"] = np.where(d["gain"] >= 4.0, 1.0, 0.0)
    med = d["ret_net_7"].median()
    d["yret"] = (d["ret_net_7"] > med).astype(float)
    d.loc[d["ret_net_7"].isna(), "yret"] = np.nan
    d["fold"] = np.searchsorted(
        np.array([pd.Timestamp(s).value for s in SPLITS]),
        d["pre_day"].astype("int64").to_numpy(), side="right")
    return d


# ------------------------------------------------------------------ eval

FEATS = ["bd_log5", "bd_log1", "bd_log5_day", "bd_log5_chg", "bd_log5_std", "bd_depth_usd"]


def cmd_eval() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    d = load()
    pool = d["score"] >= MIN_SCORE
    out = ~pool
    have = d["bd_log5"].notna()

    # ---------- 0. 覆盖偏差体检（A14 的教训：覆盖不全时先查是否与标签相关） ----------
    print("[eval] 0) 覆盖体检")
    cov = pd.DataFrame({
        "全样本": [have.mean(), int(have.sum())],
        "爆拉组": [have[d["label"] == 1].mean(), int(have[d["label"] == 1].sum())],
        "对照组": [have[d["label"] == 0].mean(), int(have[d["label"] == 0].sum())],
        "池内": [have[pool].mean(), int(have[pool].sum())],
        "池外": [have[out].mean(), int(have[out].sum())],
    }, index=["覆盖率", "条数"])
    print(cov.to_string())
    cov.to_csv(OUT / "coverage.csv", encoding="utf-8-sig")

    yr = d.assign(y=d["pre_day"].dt.year, have=have).groupby(["y", "label"])["have"].agg(["mean", "size"])
    yr.columns = ["覆盖率", "样本数"]
    print("\n  按年 × 标签：")
    print(yr.to_string())
    yr.to_csv(OUT / "coverage_by_year.csv", encoding="utf-8-sig")

    # 覆盖率差异的卡方检验（爆拉 vs 对照）
    from scipy.stats import chi2_contingency
    tab = pd.crosstab(d["label"], have)
    chi2, p, _, _ = chi2_contingency(tab)
    print(f"\n  覆盖 × 标签 卡方 p = {p:.4f}（{'有' if p < 0.05 else '无'}显著关联）")

    # ---------- 1. 变量级：四种环境 ----------
    print("\n[eval] 1) 变量级")
    rows = []
    for c in FEATS:
        x = pd.to_numeric(d[c], errors="coerce")
        y = d["label"].to_numpy(float)
        xn = x.to_numpy(float)
        r = {"特征": c, "覆盖": float(x.notna().mean())}
        r["AUC 全样本"] = _rank_auc(y, xn)
        r["AUC 池内"] = _rank_auc(d.loc[pool, "label"].to_numpy(float), x[pool].to_numpy(float))
        r["AUC 池外"] = _rank_auc(d.loc[out, "label"].to_numpy(float), x[out].to_numpy(float))
        lo, hi = _boot_auc(y, xn)
        r["AUC 95%CI 下"], r["AUC 95%CI 上"] = lo, hi
        sub = pd.DataFrame({"x": x, "s": d["score"]}).dropna()
        r["与score相关"] = float(sub["x"].corr(sub["s"], method="spearman")) if len(sub) > 50 else np.nan
        sub2 = pd.DataFrame({"x": x, "q": np.log(d["quote_vol_24h"].clip(lower=1))}).dropna()
        r["与成交额相关"] = float(sub2["x"].corr(sub2["q"], method="spearman")) if len(sub2) > 50 else np.nan

        def _rret(mask) -> float:
            s2 = pd.DataFrame({"x": x[mask], "r": d.loc[mask, "ret_net_7"]}).dropna()
            return float(s2["x"].corr(s2["r"], method="spearman")) if len(s2) > 60 else np.nan

        r["收益相关 全样本"] = _rret(pd.Series(True, index=d.index))
        r["收益相关 池内"] = _rret(pool)
        r["AUC 3x标签"] = _rank_auc(d["y3"].to_numpy(float), xn)
        r["AUC 5x标签"] = _rank_auc(d["y5"].to_numpy(float), xn)
        r["AUC 收益标签"] = _rank_auc(d["yret"].to_numpy(float), xn)
        t, b, s = _spread(x, d["label"])
        r["顶档爆拉率"], r["底档爆拉率"], r["顶底差"] = t, b, s
        rows.append(r)
    A = pd.DataFrame(rows)
    A.to_csv(OUT / "var_level.csv", index=False, encoding="utf-8-sig")
    pd.set_option("display.width", 250)
    print(A.round(4).to_string(index=False))

    # ---------- 2. 十分位表（主口径） ----------
    print("\n[eval] 2) 十分位（bd_log5）")
    x = pd.to_numeric(d["bd_log5"], errors="coerce")
    g = pd.DataFrame({"x": x, "y": d["label"], "r7": d["ret_net_7"]}).dropna(subset=["x"])
    g["bin"] = pd.qcut(g["x"], NDEC, labels=False, duplicates="drop")
    t = g.groupby("bin").agg(n=("y", "size"), x中位=("x", "median"),
                             爆拉率=("y", "mean"), 收益7中位=("r7", "median")).reset_index()
    print(t.round(4).to_string(index=False))
    t.to_csv(OUT / "decile.csv", index=False, encoding="utf-8-sig")

    # ---------- 3. 四折 walk-forward（与 22 同一套） ----------
    print("\n[eval] 3) 四折排序键检验（池内，取前 50%）")
    p = d[pool & have].copy()
    print(f"  可用池内样本 {len(p)}｜{p['fold'].value_counts().sort_index().to_dict()}")
    folds = []
    for c in ("bd_log5", "bd_log5_day", "bd_log1"):
        xc = pd.to_numeric(p[c], errors="coerce")
        dm, dn, used = [], [], 0
        for f in range(len(SPLITS) + 1):
            tr, te = p[p["fold"] < f], p[p["fold"] == f]
            if len(te) < MIN_N or len(tr) < 200:
                continue
            xt = pd.to_numeric(tr[c], errors="coerce").dropna()
            if len(xt) < 100:
                continue
            thr = float(xt.quantile(1 - TAKE))
            xe = pd.to_numeric(te[c], errors="coerce")
            sel = te[xe >= thr]
            if len(sel) < MIN_N // 2:
                continue
            dm.append(float(sel["ret_net_7"].median()) - float(te["ret_net_7"].median()))
            dn.append(float(sel["ret_net_7"].mean()) - float(te["ret_net_7"].mean()))
            used += 1
        folds.append({"键": c, "可用折": used,
                      "中位差均值": float(np.mean(dm)) if dm else np.nan,
                      "中位不降折数": int(sum(1 for v in dm if v >= -1e-12)),
                      "均值不降折数": int(sum(1 for v in dn if v >= -1e-12)),
                      "逐折中位差": [round(v, 4) for v in dm]})
    R = pd.DataFrame(folds)
    print(R.to_string(index=False))
    R.to_csv(OUT / "folds.csv", index=False, encoding="utf-8-sig")

    # 随机键零分布：在同样「可用折」的样本上跑
    rng = np.random.default_rng(7)
    null = []
    q = p.copy()
    for _ in range(N_NULL):
        q["_rk"] = rng.random(len(q))
        dm = []
        for f in range(len(SPLITS) + 1):
            tr, te = q[q["fold"] < f], q[q["fold"] == f]
            if len(te) < MIN_N or len(tr) < 200:
                continue
            thr = float(tr["_rk"].quantile(1 - TAKE))
            sel = te[te["_rk"] >= thr]
            if len(sel) < MIN_N // 2:
                continue
            dm.append(float(sel["ret_net_7"].median()) - float(te["ret_net_7"].median()))
        null.append(sum(1 for v in dm if v >= -1e-12))
    nm = pd.Series(null)
    n_use = int(R.loc[R["键"] == "bd_log5", "可用折"].iloc[0])
    print(f"  随机键：均值 {nm.mean():.2f} 折｜达到 {n_use}/{n_use} 的概率 "
          f"{(nm >= n_use).mean():.1%}")
    pd.DataFrame({"null_folds": nm}).to_csv(OUT / "null_dist.csv", index=False, encoding="utf-8-sig")
    return 0


# ------------------------------------------------------------------ robust

def cmd_robust() -> int:
    """稳健性复核：覆盖偏差 + 分年稳定 + 换皮检查。

    为什么必须做这三项（都是本项目踩过的坑）：
      ① 2022 年覆盖 0%、2023 年约 55%，池化会把「数据有没有」当成「机制行不行」；
      ② 覆盖率和标签显著相关（p=0.011），直接池化就是 A14 那个辛普森悖论的翻版；
      ③ `bd_log5_std`（挂单不稳定度）一看就像波动率的换皮，不查清不能当新发现。
    """
    OUT.mkdir(parents=True, exist_ok=True)
    d = load()
    v = pd.read_parquet(DATA / "vars_cache.parquet")
    v["pre_day"] = pd.to_datetime(v["pre_day"], utc=True).dt.tz_convert(None).dt.normalize()
    keep = ["symbol", "pre_day", "rv_24", "vol_of_vol", "range_24", "v_z_24",
            "rv_ratio_24_168", "qv_24_log"]
    d = d.merge(v[[c for c in keep if c in v.columns]], on=["symbol", "pre_day"], how="left")
    d["year"] = d["pre_day"].dt.year
    pool = d["score"] >= MIN_SCORE

    print("[robust] 1) 分年 AUC（每年内部比较，避免年份构成效应）")
    rows = []
    for y, sub in d.groupby("year"):
        for c in ("bd_log5", "bd_log5_std"):
            x = pd.to_numeric(sub[c], errors="coerce").to_numpy(float)
            rows.append({"年": y, "特征": c, "n": int(sub[c].notna().sum()),
                         "AUC": _rank_auc(sub["label"].to_numpy(float), x)})
    Y = pd.DataFrame(rows).pivot(index="年", columns="特征", values="AUC")
    N = d.dropna(subset=["bd_log5"]).groupby("year").size().rename("可用n")
    print(pd.concat([Y.round(4), N], axis=1).to_string())
    Y.to_csv(OUT / "auc_by_year.csv", encoding="utf-8-sig")

    print("\n[robust] 2) 高覆盖期（2025 起，覆盖 87~97%）单独看")
    hi = d[d["year"] >= 2025]
    hip = hi["score"] >= MIN_SCORE
    for c in ("bd_log5", "bd_log5_std"):
        x = pd.to_numeric(hi[c], errors="coerce")
        a_all = _rank_auc(hi["label"].to_numpy(float), x.to_numpy(float))
        a_pool = _rank_auc(hi.loc[hip, "label"].to_numpy(float), x[hip].to_numpy(float))
        lo, hi95 = _boot_auc(hi["label"].to_numpy(float), x.to_numpy(float))
        print(f"  {c:<12} n={int(x.notna().sum()):<5} AUC 全样本 {a_all:.4f} "
              f"[{lo:.4f}, {hi95:.4f}]｜池内 {a_pool:.4f}")

    print("\n[robust] 3) bd_log5_std 是不是波动率换皮（池内秩相关）")
    for c in ("rv_24", "vol_of_vol", "range_24", "v_z_24", "rv_ratio_24_168", "score"):
        s = pd.DataFrame({"a": pd.to_numeric(d.loc[pool, "bd_log5_std"], errors="coerce"),
                          "b": pd.to_numeric(d.loc[pool, c], errors="coerce")}).dropna()
        print(f"  vs {c:<16} ρ={s['a'].corr(s['b'], method='spearman'):+.3f}  (n={len(s)})")

    # 剔除 vol_of_vol 后的残差还有没有信息（排秩后线性回归取残差）
    print("\n[robust] 4) 剔除 vol_of_vol 后的增量（全样本，秩残差）")
    m = d[["bd_log5_std", "vol_of_vol", "label"]].dropna()
    ra = m["bd_log5_std"].rank()
    rb = m["vol_of_vol"].rank()
    beta = np.polyfit(rb, ra, 1)
    resid = ra - np.polyval(beta, rb)
    print(f"  n={len(m)}｜bd_log5_std AUC {_rank_auc(m['label'].to_numpy(float), m['bd_log5_std'].to_numpy(float)):.4f}"
          f"｜vol_of_vol AUC {_rank_auc(m['label'].to_numpy(float), m['vol_of_vol'].to_numpy(float)):.4f}"
          f"｜残差 AUC {_rank_auc(m['label'].to_numpy(float), resid.to_numpy(float)):.4f}")

    print("\n[robust] 5) 四折排序键（池内，含 bd_log5_std 与反向键）")
    p = d[pool].copy()
    p["bd_inv5"] = -pd.to_numeric(p["bd_log5"], errors="coerce")

    def _fold_diff(frame: pd.DataFrame, col: str) -> list[float]:
        """同折检验：基线必须与选择用**同一批非缺失样本**，否则基数不同会造出假差。"""
        out_ = []
        for f in range(len(SPLITS) + 1):
            te = frame[frame["fold"] == f].dropna(subset=[col, "ret_net_7"])
            tr = frame[(frame["fold"] < f)].dropna(subset=[col])
            if len(te) < MIN_N or len(tr) < 200:
                continue
            thr = float(tr[col].quantile(1 - TAKE))
            sel = te[te[col] >= thr]
            if len(sel) < MIN_N // 2:
                continue
            out_.append(float(sel["ret_net_7"].median()) - float(te["ret_net_7"].median()))
        return out_

    for c in ("bd_log5_std", "bd_log5", "bd_inv5"):
        dm = _fold_diff(p, c)
        # 零分布：同一缺失掩码下的随机键，200 次
        rng = np.random.default_rng(7)
        mask = p[c].notna()
        q = p[mask].copy()
        null = []
        for _ in range(N_NULL):
            q["_rk"] = rng.random(len(q))
            null.append(sum(1 for x in _fold_diff(q, "_rk") if x >= -1e-12))
        ns = pd.Series(null)
        k = sum(1 for x in dm if x >= -1e-12)
        print(f"  {c:<12} 可用折 {len(dm)}｜中位差均值 {np.mean(dm):+.4f}｜不降 {k}/{len(dm)}"
              f"｜逐折 {[round(x, 4) for x in dm]}")
        print(f"  {'':12} 随机键零分布：均值 {ns.mean():.2f}｜≥{k} 折的概率 {(ns >= k).mean():.1%}")

    print("\n[robust] 6) 分层检验：剔掉「规模」与「年份」构成后还在不在")
    s = d.dropna(subset=["bd_log5"]).copy()
    s["qv_bin"] = pd.qcut(np.log(s["quote_vol_24h"].clip(lower=1)), 5,
                          labels=False, duplicates="drop")
    rows = []
    for (y, q), sub in s.groupby(["year", "qv_bin"]):
        if len(sub) < 60:
            continue
        for c in ("bd_log5", "bd_log5_std"):
            x = pd.to_numeric(sub[c], errors="coerce").to_numpy(float)
            rows.append({"年": y, "规模档": q, "n": int(sub[c].notna().sum()), "特征": c,
                         "AUC": _rank_auc(sub["label"].to_numpy(float), x)})
    S = pd.DataFrame(rows)
    piv = S.pivot_table(index=["年", "规模档"], columns="特征", values="AUC")
    print(piv.round(4).to_string())
    for c in ("bd_log5", "bd_log5_std"):
        n = S[S["特征"] == c]["n"].sum()
        w = (S[S["特征"] == c].dropna(subset=["AUC"]))
        avg = float((w["AUC"] * w["n"]).sum() / w["n"].sum()) if len(w) else np.nan
        pos = int((w["AUC"] > 0.5).sum())
        print(f"  {c:<12} 加权平均 AUC {avg:.4f}｜{len(w)} 个分层里 {pos} 个 > 0.5")
    S.to_csv(OUT / "auc_stratified.csv", index=False, encoding="utf-8-sig")
    return 0


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    if cmd in ("build", "all"):
        cmd_build()
    if cmd in ("eval", "all"):
        cmd_eval()
    if cmd in ("robust", "all"):
        cmd_robust()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
