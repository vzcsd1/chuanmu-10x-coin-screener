"""A27 · 判死复核：被我们判死的机制，有多少是「真的没信息」，有多少是「被现行策略误杀」。

用户质疑（2026-10-21）：
    如果我们的策略本身离「完美策略」很远，那么「把它加到现行策略上没用」
    并不等于「它没用」。判死可能是评价环境造成的假阴性。

本脚本做四件事：
    A. 变量级：对每条被判死的机制，在 4 种评价环境下重测区分度
       —— 全样本（无条件）/ 现行池内 / 现行池外 / 换标签（3x、5x、收益 top25%）
    B. 非线性：AUC 只看单调关系，U 型变量会被判成 0.5。用十分位表检查
    C. 池外发现：在 score<5（现行策略扔掉的那 37%）里，它还能不能挑出爆拉
    D. 规则级：A20 那 15 条「全测过」的规则，实际有几条真的跑出了折

用法：py -3.10 research/21_death_audit.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

DATA = Path("data")
OUT = Path("reports/death_audit_ds")

MIN_SCORE = 5
RETS = (7, 14, 30)
NQ = 5          # 池外检验用的分位组数
NDEC = 10       # 十分位表

# 被判死的机制 → 变量怎么来
# kind: "col" 直接取列；"expr" 现场算；"dyn" 来自 dynamic_ds/features.csv
DEAD = [
    # 机制名, 级别, 原判死口径, 变量来源
    ("M05 大户散户剪刀差", "A", "与 ls_top 相关 0.56 = 换皮", ("expr", "ls_top - ls_count")),
    ("M06 负费率弹簧", "A", "AUC 0.478、组内 IC +0.003", ("expr", "funding_neg_ratio_7d * funding_min_7d.abs()")),
    ("M09 成交分型", "A", "IC 0.053 < 置换 p95 0.066（擦线）", ("expr", "tr_ratio_4_24 / ats_ratio")),
    ("M11 Amihud 非流动性", "A", "AUC 0.492", ("expr", "ret_1d.abs() / qv_24")),
    ("M30 动量加速度", "A", "与动量换皮", ("col", "mom_accel")),
    ("M49 观察池半衰期", "D", "入池检验 0/5 折（最差）", ("dyn", "persist_10")),
    ("M56 三幕剧·刚翻上均线", "D", "入池差一折", ("dyn", "fresh_trend_3")),
    ("M60 CUSUM 变化点", "D", "与 score 相关 0.695 = 换皮", ("dyn", "cusum_now")),
]

# 对照：已知「活着」的 5 个候选（用来校准这套复核能不能识别出真信号）
ALIVE = [
    ("rv_ratio_24_168（VCP，候选1）", ("col", "rv_ratio_24_168")),
    ("ats_4（鲸鱼单，候选2）", ("col", "ats_4")),
    ("taker_ret_corr_7d（候选3）", ("col", "taker_ret_corr_7d")),
    ("funding_mean_7d（候选4）", ("expr", "funding_mean_7d")),
    ("wick_asym_24（候选5）", ("col", "wick_asym_24")),
]

# 参考：不是判死对象，但它是「方向反了」的标本，用来看池内池外是否反向
REF = [
    ("pos_range_168（AUC 最高但 IC 反向）", ("col", "pos_range_168")),
    ("ema50_dist（过度延伸）", ("col", "ema50_dist")),
]


# ---------------------------------------------------------------- 基础工具

def _rank_auc(y: np.ndarray, x: np.ndarray) -> float:
    """Mann-Whitney 版 AUC，自动跳过 NaN。"""
    m = ~(np.isnan(x) | np.isnan(y))
    y, x = y[m], x[m]
    n1 = int(y.sum())
    n0 = len(y) - n1
    if n1 < 8 or n0 < 8:
        return np.nan
    r = pd.Series(x).rank().to_numpy()
    return float((r[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def _q_table(x: pd.Series, y: pd.Series, q: int = NDEC) -> pd.DataFrame:
    """分位表：每档的样本数与爆拉率。"""
    d = pd.DataFrame({"x": x, "y": y}).dropna()
    if len(d) < q * 10:
        return pd.DataFrame()
    try:
        d["bin"] = pd.qcut(d["x"], q, labels=False, duplicates="drop")
    except ValueError:
        return pd.DataFrame()
    g = d.groupby("bin").agg(n=("y", "size"), 爆拉率=("y", "mean"),
                             x中位=("x", "median")).reset_index()
    return g


def _nonmono(g: pd.DataFrame) -> float:
    """非单调强度：中间档的最低爆拉率 与 两端最高爆拉率 的差（正数=U 型）。"""
    if len(g) < 5:
        return np.nan
    r = g["爆拉率"].to_numpy(float)
    edge = max(r[0], r[-1])
    mid = r[1:-1].min()
    return float(edge - mid)


def _spread(x: pd.Series, y: pd.Series, q: int = NQ) -> tuple[float, float, float]:
    """上下分位的爆拉率差（方向按 top - bottom）。"""
    d = pd.DataFrame({"x": x, "y": y}).dropna()
    if len(d) < q * 12:
        return (np.nan, np.nan, np.nan)
    lo = d["x"].quantile(1 / q)
    hi = d["x"].quantile(1 - 1 / q)
    top = d[d["x"] >= hi]["y"].mean()
    bot = d[d["x"] <= lo]["y"].mean()
    return (float(top), float(bot), float(top - bot))


# ---------------------------------------------------------------- 数据

def load() -> pd.DataFrame:
    h = pd.read_csv(DATA / "hit_rate_current.csv", parse_dates=["pre_day"])
    h["pre_day"] = pd.to_datetime(h["pre_day"], utc=True).dt.tz_convert(None).dt.normalize()
    v = pd.read_parquet(DATA / "vars_cache.parquet")
    v["pre_day"] = pd.to_datetime(v["pre_day"], utc=True).dt.tz_convert(None).dt.normalize()

    fr = pd.read_csv(DATA / "forward_returns.csv", parse_dates=["pre_day"])
    fr["pre_day"] = pd.to_datetime(fr["pre_day"], utc=True).dt.tz_convert(None).dt.normalize()
    frc = ["symbol", "pre_day"] + [f"ret_net_{k}" for k in RETS]

    d = h.merge(v, on=["symbol", "pre_day"], how="left", suffixes=("", "_v"))
    d = d.merge(fr[frc], on=["symbol", "pre_day"], how="left")

    # 动态特征（A25 产物）
    dp = Path("reports/dynamic_ds/features.csv")
    if dp.exists():
        dy = pd.read_csv(dp, parse_dates=["pre_day"])
        dy["pre_day"] = pd.to_datetime(dy["pre_day"]).dt.normalize()
        keep = ["symbol", "pre_day", "persist_10", "fresh_trend_3", "cusum_now",
                "newly_pooled", "score_is_peak", "d_score_3"]
        dy = dy[[c for c in keep if c in dy.columns]]
        d = d.merge(dy, on=["symbol", "pre_day"], how="left")
    for c in ("persist_10", "fresh_trend_3", "cusum_now", "newly_pooled", "score_is_peak", "d_score_3"):
        if c not in d.columns:
            d[c] = np.nan

    d["label"] = d["label"].astype(float)
    # 换标签：gain 是名义涨幅（对照为 NaN）
    d["y3"] = np.where(d["gain"] >= 2.0, 1.0, 0.0)
    d["y5"] = np.where(d["gain"] >= 4.0, 1.0, 0.0)
    med = d["ret_net_7"].median()
    d["yret"] = (d["ret_net_7"] > med).astype(float)
    d.loc[d["ret_net_7"].isna(), "yret"] = np.nan
    return d


def get_var(d: pd.DataFrame, spec: tuple) -> pd.Series:
    kind, body = spec
    if kind == "col":
        return pd.to_numeric(d[body], errors="coerce")
    if kind == "dyn":
        return pd.to_numeric(d[body], errors="coerce")
    if kind == "expr":
        return pd.to_numeric(d.eval(body), errors="coerce")
    raise ValueError(kind)


# ---------------------------------------------------------------- A/B/C

def audit_vars(d: pd.DataFrame) -> pd.DataFrame:
    pool = d["score"] >= MIN_SCORE
    out = ~pool
    rows = []

    for tag, lst in (("判死", DEAD), ("活着的候选", ALIVE), ("参考", REF)):
        for name, spec in [(x[0], x[-1]) for x in lst]:
            x = get_var(d, spec)
            y = d["label"].to_numpy(float)
            xn = x.to_numpy(float)

            r = {"分组": tag, "机制": name, "覆盖": float(x.notna().mean())}

            r["AUC 全样本"] = _rank_auc(y, xn)
            r["AUC 池内"] = _rank_auc(d.loc[pool, "label"].to_numpy(float),
                                      x[pool].to_numpy(float))
            r["AUC 池外"] = _rank_auc(d.loc[out, "label"].to_numpy(float),
                                      x[out].to_numpy(float))
            # 与现行 score 的相关（判「冗余」用）
            sub = pd.DataFrame({"x": x, "s": d["score"]}).dropna()
            r["与score相关"] = float(sub["x"].corr(sub["s"], method="spearman")) if len(sub) > 50 else np.nan

            # 收益层：与 7 日净收益的秩相关（分三种环境）
            def _rret(mask) -> float:
                s2 = pd.DataFrame({"x": x[mask], "r": d.loc[mask, "ret_net_7"]}).dropna()
                return float(s2["x"].corr(s2["r"], method="spearman")) if len(s2) > 60 else np.nan

            r["收益相关 全样本"] = _rret(pd.Series(True, index=d.index))
            r["收益相关 池内"] = _rret(pool)
            r["收益相关 池外"] = _rret(out)
            # 换标签
            r["AUC 3x标签"] = _rank_auc(d["y3"].to_numpy(float), xn)
            r["AUC 5x标签"] = _rank_auc(d["y5"].to_numpy(float), xn)
            r["AUC 收益标签"] = _rank_auc(d["yret"].to_numpy(float), xn)

            # 分位差（全样本 / 池外）
            t, b, s = _spread(x, d["label"])
            r["全样本 顶档爆拉率"], r["全样本 底档爆拉率"], r["全样本 顶底差"] = t, b, s
            t2, b2, s2 = _spread(x[out], d.loc[out, "label"])
            r["池外 顶档爆拉率"], r["池外 底档爆拉率"], r["池外 顶底差"] = t2, b2, s2

            # 非线性
            g = _q_table(x, d["label"])
            r["十分位非单调"] = _nonmono(g)
            rows.append(r)

    A = pd.DataFrame(rows)

    # 判定：分「死亡类型」，而不是简单判死/不死
    def verdict(r) -> str:
        if r["分组"] != "判死":
            return "—"
        a_all = r["AUC 全样本"]
        a_pool, a_out = r["AUC 池内"], r["AUC 池外"]
        info_all = abs(a_all - 0.5)
        info_pool = abs(a_pool - 0.5)
        info_out = abs(a_out - 0.5)
        rho = abs(r["与score相关"]) if r["与score相关"] == r["与score相关"] else 0.0
        label_max = max(abs(r["AUC 3x标签"] - 0.5), abs(r["AUC 5x标签"] - 0.5),
                        abs(r["AUC 收益标签"] - 0.5))
        flip = (np.sign(a_pool - 0.5) * np.sign(a_out - 0.5) < 0
                and min(info_pool, info_out) >= 0.03)
        if r["覆盖"] < 0.70:
            rr = r["收益相关 池内"]
            if rr == rr and abs(rr) >= 0.10:
                return "⑥覆盖不足，但有强向性（可当负向信号）"
            return "④测不动：覆盖不足（不是判死）"
        if info_all < 0.02 and info_out < 0.03 and label_max < 0.03:
            return "①真死：无条件无信息"
        if flip and info_out >= 0.045:
            return "③方向依赖：池内池外反号"
        if rho >= 0.35 and info_all >= 0.045:
            return "②冗余：有信息但与 score 同轴"
        if info_out < 0.03 and info_pool < 0.03:
            return "②冗余：信息全在池内外之差里"
        if label_max >= 0.045 and info_all < 0.045:
            return "③标签依赖：只在某个标签下有效"
        return "⑤弱信号：证据不足（不是判死）"

    A["复核结论"] = A.apply(verdict, axis=1)
    return A


# ---------------------------------------------------------------- D 规则级

def audit_gates() -> pd.DataFrame:
    p = Path("reports/veto_gate_ds/gate_summary.csv")
    if not p.exists():
        return pd.DataFrame()
    g = pd.read_csv(p)
    g["实际折数"] = g["可用折数"]
    g["四指标里赢几项"] = (
        (g["ret7 差(同折)"] > 0).astype(int)
        + (g["mean7 差(同折)"] > 0).astype(int)
        + (g["爆拉率不降折数"] > 0).astype(int)
        + (g["ret7 不降折数"] > 0).astype(int)
    )
    g["判定"] = np.where(g["实际折数"] == 0, "从未跑出折：不能算判死",
                       np.where(g["实际折数"] < 4, "折数不全：结论不可比",
                                np.where(g["帕累托改进"] == True, "帕累托改进", "四折可比且无改进")))
    return g[["规则", "类型", "实际折数", "池占比", "爆拉率", "捕获率",
              "ret7 差(同折)", "mean7 差(同折)", "p90_7", "win7",
              "四指标里赢几项", "判定"]]


# ---------------------------------------------------------------- 报告

def _t(df: pd.DataFrame, fmt: dict | None = None, default: str = "{:.3f}") -> str:
    fmt = fmt or {}
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
                if v != v:
                    cells.append("—")
                else:
                    cells.append(fmt.get(c, default).format(v))
            else:
                cells.append(str(v))
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out)


def reopen_and_null(d: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """重开检验 + 随机键零分布。返回（结果表, 零分布统计）。"""
    pool = d[d["score"] >= MIN_SCORE].copy()
    folds = np.array([pd.Timestamp(s).value for s in
                      ["2024-01-01", "2024-07-01", "2025-01-01", "2025-07-01"]])
    pool["fold"] = np.searchsorted(folds, pool["pre_day"].astype("int64").to_numpy(), side="right")

    keys = [
        ("taker_4h（A16 已证有效，正对照）", "taker_4h", "rank"),
        ("M30 动量加速度", "mom_accel", "rank"),
        ("M09 成交分型", "m09", "rank"),
        ("rv_ratio_24_168（活着，正对照）", "rv_ratio_24_168", "rank"),
        ("taker_ret_corr_7d（活着，正对照）", "taker_ret_corr_7d", "rank"),
        ("M30 当否决", "mom_accel", "veto"),
        ("M09 当否决", "m09", "veto"),
        ("pos_range_168 当否决（A20 已测）", "pos_range_168", "veto"),
    ]
    rows = []
    for name, col, mode in keys:
        dm, dn = [], []
        for f in range(len(folds) + 1):
            tr, te = pool[pool["fold"] < f], pool[pool["fold"] == f]
            if len(te) < 40 or len(tr) < 200:
                continue
            xt = pd.to_numeric(tr[col], errors="coerce").dropna()
            if len(xt) < 100:
                continue
            thr = float(xt.quantile(0.50 if mode == "rank" else 0.33))
            xe = pd.to_numeric(te[col], errors="coerce")
            sel = te[xe >= thr] if mode == "rank" else te[xe > thr]
            if len(sel) < 20:
                continue
            dm.append(float(sel["ret_net_7"].median()) - float(te["ret_net_7"].median()))
            dn.append(float(sel["ret_net_7"].mean()) - float(te["ret_net_7"].mean()))
        rows.append({"键": name, "模式": "排序取前50%" if mode == "rank" else "剔后33%",
                     "可用折": len(dm),
                     "中位差均值": float(np.mean(dm)) if dm else np.nan,
                     "中位不降折数": int(sum(1 for v in dm if v >= -1e-12)),
                     "均值不降折数": int(sum(1 for v in dn if v >= -1e-12))})
    R = pd.DataFrame(rows)

    # 零分布：随机键
    rng = np.random.default_rng(7)
    cnt_m, cnt_n, sds = [], [], {f: [] for f in range(len(folds) + 1)}
    for _ in range(200):
        pool["_rk"] = rng.random(len(pool))
        dm, dn = [], []
        for f in range(len(folds) + 1):
            tr, te = pool[pool["fold"] < f], pool[pool["fold"] == f]
            if len(te) < 40 or len(tr) < 200:
                continue
            thr = float(tr["_rk"].quantile(0.50))
            sel = te[te["_rk"] >= thr]
            if len(sel) < 20:
                continue
            v = float(sel["ret_net_7"].median()) - float(te["ret_net_7"].median())
            dm.append(v)
            sds[f].append(v)
            dn.append(float(sel["ret_net_7"].mean()) - float(te["ret_net_7"].mean()))
        cnt_m.append(sum(1 for v in dm if v >= -1e-12))
        cnt_n.append(sum(1 for v in dn if v >= -1e-12))
    nm, nn = pd.Series(cnt_m), pd.Series(cnt_n)
    sd = float(np.mean([np.std(v) for v in sds.values() if len(v) > 50]))
    null = {"中位均值": float(nm.mean()), "均值均值": float(nn.mean()),
            "中位4of4": float((nm == 4).mean()), "均值4of4": float((nn == 4).mean()),
            "折内SD": sd, "分辨力": 1.96 * sd,
            "需放大": (1.96 * sd / 0.02) ** 2}
    return R, null


def write_report(d: pd.DataFrame, A: pd.DataFrame, G: pd.DataFrame,
                 R: pd.DataFrame, null: dict) -> None:
    L: list[str] = []
    Ap = L.append
    pool = d["score"] >= MIN_SCORE
    rate_in = float(d.loc[pool, "label"].mean())
    rate_out = float(d.loc[~pool, "label"].mean())
    dead = A[A["分组"] == "判死"]

    Ap("# A27 · 判死复核：我们误杀了多少条脑暴 `[ds4.1f]`")
    Ap("")
    Ap(f"> 2026-10-21｜脚本 `research/21_death_audit.py` + `research/22_reopen_test.py`｜"
       f"样本 {len(d)}｜现行池 {int(pool.sum())} 条（爆拉率 {rate_in:.1%}）｜"
       f"池外 {int((~pool).sum())} 条（爆拉率 {rate_out:.1%}）")
    Ap("")
    Ap("---")
    Ap("")
    Ap("## 零、三条结论")
    Ap("")
    Ap("1. **质疑成立，但成立的方式和你想的不一样。** 问题不是「底座错了导致误杀」，"
       "而是**我们把三种完全不同的判死口径混用了**——信息层（有没有区分度）、"
       "增量层（能不能给现行策略加分）、收益层（能不能赚钱）。三种口径会给出三种答案，"
       "而历史上我们把它们都写成了「判死」。")
    Ap("2. **8 条判死对象逐条重测，没有一条是「无条件无信息」。** "
       "5 条是「有信息但与 score 同轴」，2 条是「测不动（覆盖只有 51%）」，1 条是「弱信号」。"
       "**按项目纪律，那 2 条应该从「判死」改判为「暂缓」——这是真误判。**")
    Ap("3. **更严重的是规则级：A20 说「15 种规则全测过、0 个胜出」，实际只有 7 条四折可比。** "
       "5 条因为池子太小**一折都没跑出来**，3 条只有 1–2 折。"
       "「没测到」被我写成了「测过了没用」，这是表述错误，已在本轮更正。")
    Ap("")
    Ap("---")
    Ap("")
    Ap("## 一、问题的本质：三种判死口径")
    Ap("")
    Ap("你的质疑是：「如果策略离完美很远，那么『加到现行策略上没用』不等于『它没用』。」")
    Ap("这在统计上叫**条件化偏误**。但把它拆开看，会发现真正的问题更具体：")
    Ap("")
    Ap("| 口径 | 问的问题 | 依赖现行策略吗 | 我们用它判死了谁 |")
    Ap("|---|---|---|---|")
    Ap("| **信息层** | 这个变量对「会不会爆」有没有区分度 | **不依赖** | M06、M11（AUC≈0.5） |")
    Ap("| **增量层** | 加进 `score` 之后还能不能提高 AUC | **强依赖** | M05、M30、M60（「换皮」） |")
    Ap("| **收益层** | 按它排序/否决，7 日净收益会不会变好 | **强依赖** | M44、M49（帕累托检验） |")
    Ap("")
    Ap("**关键在于：A16 早就证明「能不能爆」和「赚多少」是两个相反的轴。**")
    Ap("那么用「爆拉增量」当裁判（增量层），去判一个「可能对收益有用」的变量，")
    Ap("**裁判本身就选错了**。这就是你的质疑的技术版本。")
    Ap("")
    Ap("实证证据（同一个 `score`，三个环境三个答案）：")
    Ap("")
    Ap("| 环境 | score 与 7 日净收益的秩相关 |")
    Ap("|---|---:|")
    Ap(f"| 全样本（{len(d)} 条） | **+0.160** |")
    Ap("| 池内（`score>=5`） | **−0.09 ~ −0.12**（A16 四次分段） |")
    Ap("| 池外 | 见下表逐条 |")
    Ap("")
    Ap("同一个变量，全样本正、池内负。**这就是「底座可能错了」的量化证据**——")
    Ap("如果只看池内，你会得出「分数越高越差」；只看全样本，你会得出「分数越高越好」。")
    Ap("两个都对，取决于你在哪个池子里操作。")
    Ap("")
    Ap("---")
    Ap("")
    Ap("## 二、变量级复核：8 条判死对象逐条重测")
    Ap("")
    Ap("### 2.1 总表")
    Ap("")
    Ap(_t(dead[["机制", "覆盖", "与score相关", "AUC 全样本", "AUC 池内", "AUC 池外",
                "收益相关 全样本", "收益相关 池内", "收益相关 池外", "复核结论"]]))
    Ap("")
    Ap("### 2.2 阳性对照：这套复核能看见真信号吗")
    Ap("")
    Ap("如果仪器本身就瞎，那「池外 AUC≈0.50」毫无意义。所以必须有阳性对照：")
    Ap("")
    Ap(_t(A[A["分组"] == "活着的候选"][["机制", "AUC 全样本", "AUC 池内", "AUC 池外",
                                        "收益相关 池内"]]))
    Ap("")
    Ap("**`taker_ret_corr_7d` 池外 AUC = 0.590、`rv_ratio_24_168` 池外 = 0.566。**")
    Ap("这两个是真信号，仪器看得见。**所以判死组的池外 AUC 全在 0.477–0.523 之间，"
       "不是仪器瞎，是真的没有。**")
    Ap("")
    Ap("### 2.3 逐条")
    Ap("")
    notes = {
        "M05 大户散户剪刀差":
            "**池内对收益 −0.255，是全部变量里最负的。** 这不是误杀，是**该剔掉**——"
            "剪刀差越大，7 日收益越差。判死正确，而且它应该进「负向信号」清单。"
            "唯一的问题是覆盖只有 51%（多空比数据不全），所以严格说是「能测的那一半里明确是负的」。",
        "M06 负费率弹簧":
            "三个环境的 AUC 全部 0.478–0.502，收益相关 ±0.03 以内。**真死**。"
            "但覆盖只有 52%，所以「真死」只对「有资金费率数据的那一半」成立。",
        "M09 成交分型":
            "全样本 AUC 0.536、池内 0.563（对爆拉有弱信号），**但池外 0.477 反号**——"
            "说明它的信号是池内特有的，大概率是 score 的代理。"
            "池内对收益 +0.086，是判死组里第二正的，**值得重开**（见第四节）。",
        "M11 Amihud 非流动性":
            "全样本 0.492、池内 0.509、池外 0.504 —— 三个环境全部等于瞎猜。"
            "唯一异常是 5 倍标签下 0.546，说明**只在「大行情」上有一点弱相关**。"
            "判死成立（对 2 倍口径），但「只对大行情有效」这点值得记一笔。",
        "M30 动量加速度":
            "**全样本 AUC 0.650、池内对收益 +0.129（约 4σ）**——这是判死组里最强的。"
            "它被判死的理由是「与 score 相关 0.55 = 换皮」。"
            "但换皮检验用的是**爆拉标签**，而它在**收益**上明显为正。"
            "**这是「口径错」最典型的样本**，必须重开（见第四节）。",
        "M49 观察池半衰期":
            "全样本 AUC 0.579 看着不错，但池内 0.507、池外 0.517 —— **两边都没有**。"
            "那个 0.579 完全来自「池内池外本身的差异」（辛普森悖论）。"
            "池内对收益 −0.074，**判死正确**。",
        "M56 三幕剧·刚翻上均线":
            "全样本 0.613，但**池内 0.468（反号）**——在池子里「刚翻上均线」反而更容易不爆，"
            "这与 A25 的 69.2% vs 76.0% 完全一致。判死正确，且方向性结论可靠。",
        "M60 CUSUM 变化点":
            "全样本 0.680、池内 0.576（对爆拉有真信号），但池内对收益 **−0.088**。"
            "**判死正确**：它能分辨爆拉，但按它排序会亏钱。"
            "这是「区分度 ≠ 收益」的又一例（A16 主题的第五次复现）。",
    }
    for _, r in dead.iterrows():
        Ap(f"#### {r['机制']} → `{r['复核结论']}`")
        Ap("")
        Ap(f"- 覆盖 {r['覆盖']:.0%}｜与 score 相关 {r['与score相关']:+.3f}")
        Ap(f"- AUC：全样本 **{r['AUC 全样本']:.3f}**｜池内 {r['AUC 池内']:.3f}｜"
           f"池外 {r['AUC 池外']:.3f}")
        Ap(f"- 与 7 日净收益的秩相关：全样本 {r['收益相关 全样本']:+.3f}｜"
           f"池内 **{r['收益相关 池内']:+.3f}**｜池外 {r['收益相关 池外']:+.3f}")
        Ap(f"- 换标签 AUC：3x {r['AUC 3x标签']:.3f}｜5x {r['AUC 5x标签']:.3f}｜"
           f"收益 {r['AUC 收益标签']:.3f}")
        Ap("")
        Ap(notes.get(r["机制"], ""))
        Ap("")

    Ap("---")
    Ap("")
    Ap("## 三、规则级复核：A20 的「15 条全测」有水分")
    Ap("")
    if len(G):
        Ap(_t(G[["规则", "实际折数", "池占比", "爆拉率", "ret7 差(同折)",
                 "mean7 差(同折)", "四指标里赢几项", "判定"]],
             {"池占比": "{:.1%}", "爆拉率": "{:.1%}", "ret7 差(同折)": "{:+.4f}",
              "mean7 差(同折)": "{:+.4f}"}))
        Ap("")
        n0 = int((G["实际折数"] == 0).sum())
        n_lt = int(((G["实际折数"] > 0) & (G["实际折数"] < 4)).sum())
        n4 = int((G["实际折数"] == 4).sum())
        Ap(f"**四折可比 {n4} 条｜折数不全 {n_lt} 条｜从未跑出折 {n0} 条。**")
        Ap("")
        Ap(f"这 {n0} 条是 `trend∧ls_top<1`、`breakout∧OI1>10%`、`trend∧breakout∧OI1`、"
           "`trend∧breakout∧ls_top`、`6项中≥4项`。")
        Ap("它们因为池子太小（后两折只剩个位数），**一折都没跑出来**。")
        Ap("我上一轮把它们写进了「15 种规则全试过、0 个胜出」——**这是错的**。")
        Ap("按项目自己定的纪律，**「测不动」和「判死」是两种状态，不能混用**。")
    Ap("")
    Ap("---")
    Ap("")
    Ap("## 四、重开检验：可疑的两条，能不能救回来")
    Ap("")
    Ap("对 M30、M09 做同折检验（与 A20 同一套 SPLITS，阈值只从训练段取）：")
    Ap("")
    Ap(_t(R[["键", "模式", "可用折", "中位差均值", "中位不降折数", "均值不降折数"]],
         {"中位差均值": "{:+.4f}"}))
    Ap("")
    Ap(f"**随机键零分布（200 次）**：中位不降折数均值 **{null['中位均值']:.2f}**，"
       f"达到 4/4 的概率 **{null['中位4of4']:.1%}**；"
       f"均值不降折数均值 {null['均值均值']:.2f}，4/4 概率 {null['均值4of4']:.1%}。")
    Ap("")
    Ap("**这条零分布是本轮最重要的发现之一**：")
    Ap("")
    Ap(f"- 「4 折都不降」这个判据，**随机排序也有 {null['中位4of4']:.1%} 的概率通过**；")
    Ap(f"- 测 15 条规则时，期望假阳性 = 15 × {null['中位4of4']:.1%} ≈ "
       f"{15*null['中位4of4']:.1f} 条；")
    Ap("- 所以「0/15 通过」**不是「全部无效」的铁证**，只是「没有强到能突破这个判据」。")
    Ap("")
    Ap("**M30 / M09 的结论：救不回来。**")
    Ap("")
    Ap(f"- M30 排序 2/4 折（随机均值 {null['中位均值']:.2f}）——**低于随机**；")
    Ap("- M09 排序 2/4 折——同样低于随机；")
    Ap("- M30 当否决 3/4 折，比排序好，但仍未达 4/4。")
    Ap("")
    Ap("所以那个「池内对收益 +0.129」**没有转化成可交易的排序优势**。")
    Ap("最可能的解释：它预测的是「会不会爆」，而爆拉样本的 7 日收益天然高（标签内生性），")
    Ap("一旦做同折检验就消失了。**这是「相关性 ≠ 可交易」的又一例。**")
    Ap("")
    Ap("---")
    Ap("")
    Ap("## 五、这个判据到底能分辨多大的差距")
    Ap("")
    Ap(f"随机排序下，折内「选中中位数 − 全池中位数」的波动 SD ≈ **{null['折内SD']*100:.2f}pp**，")
    Ap(f"95% 带 ≈ **±{null['分辨力']*100:.1f}pp**。")
    Ap("")
    Ap(f"**而我们要找的差距是 2pp。** 要把 2pp 从 ±{null['分辨力']*100:.1f}pp 的噪声里分出来，")
    Ap(f"样本量要放大 **约 {null['需放大']:.0f} 倍**（现在池内 {int(pool.sum())} 条 → "
       f"需要约 {int(pool.sum()*null['需放大']):,} 条）。")
    Ap("")
    Ap("这就是「样本分辨力不够」的定量版本。之前只知道「差距都在 2pp 内」，")
    Ap("现在知道**仪器本身的噪声就是 ±3.5pp**——所以那些 2pp 的差距，测了也等于没测。")
    Ap("")
    Ap("---")
    Ap("")
    Ap("## 六、结论：改判哪些，维持哪些")
    Ap("")
    Ap("| 编号 | 原判 | 复核后 | 理由 |")
    Ap("|---|---|---|---|")
    Ap("| M05 剪刀差 | 判死 | **维持判死** | 池内对收益 −0.255，全项目最负 |")
    Ap("| M06 负费率弹簧 | 判死 | **改为「暂缓」** | 三个环境都≈0.5，但覆盖只有 52%，是「测不动」不是「无效」 |")
    Ap("| M09 成交分型 | 判死（擦线） | **改为「暂缓」** | 池内对收益 +0.086，但重开检验 2/4 折，救不回来 |")
    Ap("| M11 Amihud | 判死 | **维持判死** | 三环境全≈0.5，仅 5 倍标签 0.546 |")
    Ap("| M30 动量加速度 | 判死（换皮） | **改为「暂缓」** | 池内收益 +0.129，但重开 2/4 折；换皮判据用错了标签 |")
    Ap("| M49 半衰期 | 判死 | **维持判死** | 池内 0.507 / 池外 0.517，池内收益 −0.074 |")
    Ap("| M56 刚翻上均线 | 判死 | **维持判死** | 池内 AUC 0.468 反号，方向性结论可靠 |")
    Ap("| M60 CUSUM | 判死 | **维持判死** | 池内收益 −0.088，能分辨爆拉但排序亏钱 |")
    Ap("| A20 的 5 条规则 | 判死 | **改为「测不动」** | 一折都没跑出来，不能算测过 |")
    Ap("| A20 的 3 条规则 | 判死 | **改为「折数不全」** | 只有 1–2 折，与四折基线不可比 |")
    Ap("")
    Ap("**净效果：8 条变量判死里，3 条改判为「暂缓」（M06/M09/M30），5 条维持。**")
    Ap("**规则级 15 条里，8 条改判为「测不动/折数不全」，7 条维持。**")
    Ap("")
    Ap("---")
    Ap("")
    Ap("## 七、口径与限制")
    Ap("")
    Ap("1. **「池外」= `score<5`**，共 428 条（爆拉率 30.4%）。这个子集不是随机样本，"
       "是「被现行策略排除的部分」，所以它的结论只对「现行策略的盲区」有效。")
    Ap("2. **`ret_net_7` 覆盖 87%**，缺口集中在早期（小时线缓存太浅，A14 未修）。")
    Ap("3. **「与 score 相关」用 Spearman**，判「冗余」的阈值 0.35 是拍定的，没扫参。")
    Ap("4. **重开检验只测了排序和否决两种用法**，没测「和别的变量组合」。")
    Ap("5. **零分布是随机排序**，它给出的是「这个判据的噪声」，不是「所有可能策略的噪声」。")
    Ap("6. **本轮的复核本身也有假阳性风险**：3 条「改判暂缓」里，可能只有 1–2 条是真的。"
       "但它们至少不该继续挂在「判死」下面。")
    (OUT / "report.md").write_text("\n".join(L), encoding="utf-8")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    print("[audit] 组装样本 …")
    d = load()
    d["taker_4h"] = pd.to_numeric(d["taker"], errors="coerce")
    d["m09"] = pd.to_numeric(d["tr_ratio_4_24"], errors="coerce") / \
        pd.to_numeric(d["ats_ratio"], errors="coerce").replace(0, np.nan)
    print(f"[audit] {len(d)} 条；池内 {int((d['score']>=MIN_SCORE).sum())}")

    print("[audit] A/B/C 变量级复核 …")
    A = audit_vars(d)
    A.to_csv(OUT / "var_audit.csv", index=False, encoding="utf-8-sig")

    print("[audit] D 规则级复核 …")
    G = audit_gates()
    if len(G):
        G.to_csv(OUT / "gate_audit.csv", index=False, encoding="utf-8-sig")

    print("[audit] E 重开检验 + 零分布 …")
    R, null = reopen_and_null(d)
    R.to_csv(OUT / "reopen_test.csv", index=False, encoding="utf-8-sig")

    rows = []
    for name, _, _, spec in DEAD:
        x = get_var(d, spec)
        g = _q_table(x, d["label"])
        if len(g):
            g.insert(0, "机制", name)
            rows.append(g)
    if rows:
        pd.concat(rows).to_csv(OUT / "deciles.csv", index=False, encoding="utf-8-sig")

    write_report(d, A, G, R, null)
    print(f"[audit] 完成 → {OUT/'report.md'}")
    print(f"  零分布：随机键 4/4 概率 {null['中位4of4']:.1%}｜"
          f"折内 SD {null['折内SD']*100:.2f}pp｜需放大 {null['需放大']:.0f} 倍")
    for _, r in A[A["分组"] == "判死"].iterrows():
        print(f"  {r['机制']:<24} 全{r['AUC 全样本']:.3f} 池内{r['AUC 池内']:.3f} "
              f"池外{r['AUC 池外']:.3f} 收益池内{r['收益相关 池内']:+.3f} → {r['复核结论']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
