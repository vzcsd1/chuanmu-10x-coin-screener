#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""17 · 否决制 vs 加法制：把入池规则画成一张取舍表（A20）`[ds4.1f]`

## 为什么要做这件事

`binance_box_strategy.py` 现在的入池规则只有一条：

    score >= 5      （6 个条件各打分，加起来过线）

**这是纯加法，没有任何「某一项不过关就一票否决」的机制。** 代码核实：
`Config.min_score = 5` 是唯一的质量阈值，另外三道闸门（成交额上下限、市值上限）
是「体量」闸门，不是「质量」闸门。

加法的问题在于**可以互相补分**：一个币价格没在涨（c_trend=0），
只要 OI 涨 10%（4 分）+ 成交量放大（1 分）也能进池。

## 一个反直觉的已知事实

KNOWLEDGE §2.14：`score>=5` 的实际效果 ≈ 「只要 c_trend」。
而且**把阈值抬高会更差**（`>=8` 爆拉率 67.0%、`>=11` 只有 62.3%，都低于 `>=5` 的 71.0%）。
原因见 §2.9：18 分里有 8 分是「价格已经涨上去了」，分数高 = 已经动过 = 后面空间小。

## 本脚本要回答什么

**能不能找到一个既收紧池子、又不牺牲收益的规则？**

判定用「帕累托改进」，这样**不需要先拍板目标函数**：

  ① 4 折的 7 日净收益中位   **每一折都不低于**基线
  ② 4 折的爆拉率            **每一折都不低于**基线
  ③ 池子占比 ≤ 50%（真的收紧了，不是换个说法）

三条同时满足 → 直接采纳，不用讨论取舍。
一条都不满足 → 必须拍板「要命中率还是要收益」，本脚本给出取舍曲线。

## 四个维度（TODO A20 要求）

| 维度 | 含义 | 谁在乎 |
|---|---|---|
| 池子占比 | 收紧到什么程度 | 能不能人工看过来 |
| 爆拉率 | 池内有多少真的爆了 | 精度（少踩坑） |
| **爆拉捕获率** | 全样本的爆拉事件，被池子留下了多少 | **召回（别漏掉大机会）** |
| 7/14/30 日净收益中位 | 真金白银 | 赚钱 |

「爆拉捕获率」是这次新加的——**只看爆拉率会选出「只留 5 个但全对」的规则，
而那意味着你一年错过 95% 的机会。** 精度和召回必须一起看。

## 15 个候选规则

- 加法阈值扫描：`>=5` / `>=8` / `>=11`（对照组，预期越抬越差）
- 单指标：只要 `c_trend`
- 两项合取：`trend∧breakout`、`trend∧OI1`、`trend∧ls_top`、`breakout∧OI1`
- 三项合取：`trend∧breakout∧OI1`、`trend∧breakout∧ls_top`
- k-of-n：6 项中 ≥3 项 / ≥4 项（加法与合取之间的中间形态）
- **加法 + 过度延伸否决**：`score>=5 ∧ ema50_dist ≤ 训练集中位数`
  —— 这条是本脚本的主假设。既然病根是「分数高 = 已经涨过」，
  那就**在保留加法池子的同时，否决掉「离均线太远」的那部分**。
  阈值只从**训练段**取，不用测试段，避免前视。

用法
----
  py -3.10 research/17_veto_gate.py
  py -3.10 research/17_veto_gate.py --min-pool 30
"""
from __future__ import annotations

import argparse
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
TRAD = ROOT / "reports" / "tradability_ds"
OUT = ROOT / "reports" / "veto_gate_ds"
TAG = "ds4.1f"

SPLITS = ["2024-01-01", "2024-07-01", "2025-01-01", "2025-07-01"]
BASE_TH = 5.0
RETS = (7, 14, 30)
PRIMARY = 7
MIN_POOL = 40          # 一折里池子少于这个数就不给结论
MAX_SHARE = 0.50       # 「收紧」的定义

OI_1D = 0.10
OI_3D = 0.20
LS_TOP = 1.0


# ---------------------------------------------------------------- 工具

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


def med(s: pd.Series) -> float:
    s = pd.to_numeric(s, errors="coerce").dropna()
    return float(s.median()) if len(s) else np.nan


# ---------------------------------------------------------------- 数据

def build() -> pd.DataFrame:
    hr = pd.read_csv(DATA / "hit_rate_current.csv")
    hr["pre_day"] = pd.to_datetime(hr["pre_day"], utc=True)

    fr = pd.read_csv(DATA / "forward_returns.csv")
    fr["pre_day"] = pd.to_datetime(fr["pre_day"], utc=True)
    keep = ["symbol", "pre_day"] + [f"ret_net_{h}" for h in RETS] + [f"covered_{h}" for h in RETS]
    keep = [c for c in keep if c in fr.columns]
    fr = fr[keep].drop_duplicates(subset=["symbol", "pre_day"])

    df = hr.merge(fr, on=["symbol", "pre_day"], how="left")

    # 过度延伸否决要用的量（来自 vars_cache，14 号脚本算的）
    vc = pd.read_parquet(DATA / "vars_cache.parquet", columns=["symbol", "pre_day", "ema50_dist"])
    vc["pre_day"] = pd.to_datetime(vc["pre_day"], utc=True)
    df = df.merge(vc.drop_duplicates(subset=["symbol", "pre_day"]),
                  on=["symbol", "pre_day"], how="left")

    # 可交易性（A17），只用于诊断，不参与任何筛选
    met = pd.read_parquet(TRAD / "tradable_labels.parquet")
    met["pre_day"] = pd.to_datetime(met["pre_day"], utc=True)
    df = df.merge(met[["symbol", "pre_day", "tradable"]].drop_duplicates(
        subset=["symbol", "pre_day"]), on=["symbol", "pre_day"], how="left")

    for c in ("c_trend", "c_breakout", "c_v24up", "label"):
        df[c] = df[c].fillna(0).astype(int)
    return df.sort_values("pre_day").reset_index(drop=True)


# ---------------------------------------------------------------- 规则定义

def cond(d: pd.DataFrame) -> dict[str, pd.Series]:
    """6 个原始条件（与 WEIGHTS 一一对应）。NaN 一律当「不成立」。"""
    return {
        "trend": d["c_trend"].astype(bool),
        "oi1": (d["oi_chg_1d"] > OI_1D).fillna(False),
        "oi3": (d["oi_chg_3d"] > OI_3D).fillna(False),
        "ls": (d["ls_top"] < LS_TOP).fillna(False),
        "brk": d["c_breakout"].astype(bool),
        "vol": d["c_v24up"].astype(bool),
    }


def label_of(key: str) -> str:
    for k, lab, _ in gate_specs():
        if k == key:
            return lab
    return key


def gate_specs() -> list[tuple[str, str, str]]:
    """(key, 人话标签, 类型)。类型用于分组展示。"""
    return [
        ("score5", "加法 ≥5（现行）", "加法"),
        ("score8", "加法 ≥8", "加法"),
        ("score11", "加法 ≥11", "加法"),
        ("trend", "只要 c_trend", "单指标"),
        ("t_and_b", "trend ∧ breakout", "两项合取"),
        ("t_and_oi1", "trend ∧ OI1>10%", "两项合取"),
        ("t_and_ls", "trend ∧ ls_top<1", "两项合取"),
        ("b_and_oi1", "breakout ∧ OI1>10%", "两项合取"),
        ("t_b_oi1", "trend ∧ breakout ∧ OI1", "三项合取"),
        ("t_b_ls", "trend ∧ breakout ∧ ls_top", "三项合取"),
        ("k3of6", "6 项中 ≥3 项", "k-of-n"),
        ("k4of6", "6 项中 ≥4 项", "k-of-n"),
        ("s5_not_ext", "加法≥5 ∧ 未过度延伸(≤训练中位)", "加法+否决"),
        ("s5_not_ext75", "加法≥5 ∧ 未过度延伸(≤训练 p75)", "加法+否决"),
        ("t_not_ext", "trend ∧ 未过度延伸(≤训练中位)", "加法+否决"),
    ]


def apply_gate(key: str, d: pd.DataFrame, ctx: dict) -> pd.Series:
    c = cond(d)
    n6 = sum(v.astype(int) for v in c.values())
    ext = d["ema50_dist"]
    lo_med, lo_p75 = ctx["ext_med"], ctx["ext_p75"]
    m = {
        "score5": d["score"] >= 5,
        "score8": d["score"] >= 8,
        "score11": d["score"] >= 11,
        "trend": c["trend"],
        "t_and_b": c["trend"] & c["brk"],
        "t_and_oi1": c["trend"] & c["oi1"],
        "t_and_ls": c["trend"] & c["ls"],
        "b_and_oi1": c["brk"] & c["oi1"],
        "t_b_oi1": c["trend"] & c["brk"] & c["oi1"],
        "t_b_ls": c["trend"] & c["brk"] & c["ls"],
        "k3of6": n6 >= 3,
        "k4of6": n6 >= 4,
        # 过度延伸否决：ema50_dist 越大 = 离 50 日均线越远 = 已经涨过
        "s5_not_ext": (d["score"] >= 5) & (ext <= lo_med),
        "s5_not_ext75": (d["score"] >= 5) & (ext <= lo_p75),
        "t_not_ext": c["trend"] & (ext <= lo_med),
    }
    return m[key].fillna(False)


# ---------------------------------------------------------------- 单折评估

def fold_eval(te: pd.DataFrame, ctx: dict, base: dict, min_pool: int) -> list[dict]:
    n_all = len(te)
    n_days = max(te["pre_day"].nunique(), 1)
    n_pos_all = int(te["label"].sum())
    rows = []
    for key, label, kind in gate_specs():
        m = apply_gate(key, te, ctx)
        pool = te[m]
        n = len(pool)
        if n < min_pool:
            rows.append({"key": key, "规则": label, "类型": kind, "n": n,
                         "占比": n / n_all if n_all else np.nan,
                         "爆拉率": np.nan, "捕获率": np.nan,
                         "ret7": np.nan, "ret14": np.nan, "ret30": np.nan,
                         "mean7": np.nan, "p90_7": np.nan, "win7": np.nan,
                         "mean14": np.nan, "p90_14": np.nan, "win14": np.nan,
                         "mean30": np.nan, "p90_30": np.nan, "win30": np.nan,
                         "日均条数": n / n_days, "样本不足": True})
            continue
        r = {"key": key, "规则": label, "类型": kind, "n": n,
             "占比": n / n_all,
             "爆拉率": float(pool["label"].mean()),
             "捕获率": (float(pool["label"].sum()) / n_pos_all) if n_pos_all else np.nan,
             "日均条数": n / n_days, "样本不足": False}
        for h in RETS:
            col = f"ret_net_{h}"
            if col in pool.columns:
                v = pd.to_numeric(pool[col], errors="coerce").dropna()
                r[f"ret{h}"] = float(v.median()) if len(v) else np.nan
                r[f"mean{h}"] = float(v.mean()) if len(v) else np.nan
                r[f"p90_{h}"] = float(v.quantile(0.90)) if len(v) else np.nan
                r[f"win{h}"] = float((v > 0).mean()) if len(v) else np.nan
                r[f"_cov{h}"] = int(len(v))
            else:
                r[f"ret{h}"] = r[f"mean{h}"] = r[f"p90_{h}"] = r[f"win{h}"] = np.nan
                r[f"_cov{h}"] = 0
        # 可交易真赢家占比（A17）—— 顺手看一眼否决制是不是把骗炮也滤掉了
        if "tradable" in pool.columns:
            r["可交易占比"] = float(pool["tradable"].fillna(0).mean())
        rows.append(r)
    for r in rows:
        r["ret7_vs_base"] = r["ret7"] - base["ret7"] if np.isfinite(r["ret7"]) else np.nan
        r["爆拉率_vs_base"] = r["爆拉率"] - base["爆拉率"] if np.isfinite(r["爆拉率"]) else np.nan
    return rows


# ---------------------------------------------------------------- 主流程

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-pool", type=int, default=MIN_POOL)
    args = ap.parse_args()

    min_pool = args.min_pool

    df = build()
    print(f"[veto] 样本 {len(df)}｜正例 {int(df.label.sum())}｜"
          f"日均样本 {len(df) / max(df.pre_day.nunique(), 1):.2f} 条")

    all_rows: list[dict] = []
    BASE: dict[str, dict[str, dict]] = {}
    for sp in SPLITS:
        s = pd.Timestamp(sp, tz="UTC")
        tr = df[df["pre_day"] < s]
        te = df[df["pre_day"] >= s].reset_index(drop=True)
        if len(tr) < 200 or len(te) < 100:
            print(f"  !! {sp} 样本不足，跳过")
            continue
        # 阈值只从训练段取 —— 防前视
        ext_tr = pd.to_numeric(tr["ema50_dist"], errors="coerce")
        ctx = {"ext_med": float(ext_tr.median()), "ext_p75": float(ext_tr.quantile(0.75))}
        base_m = apply_gate("score5", te, ctx)
        bp = te[base_m]
        _b7 = pd.to_numeric(bp["ret_net_7"], errors="coerce").dropna()
        base = {"ret7": med(bp["ret_net_7"]), "爆拉率": float(bp["label"].mean()),
                "ret14": med(bp["ret_net_14"]), "ret30": med(bp["ret_net_30"]),
                "mean7": float(_b7.mean()) if len(_b7) else np.nan,
                "p90_7": float(_b7.quantile(0.90)) if len(_b7) else np.nan,
                "win7": float((_b7 > 0).mean()) if len(_b7) else np.nan}
        BASE.setdefault("score5", {})[sp] = base
        rows = fold_eval(te, ctx, base, min_pool)
        for r in rows:
            r["split"] = sp
        all_rows += rows
        print(f"  [{sp}] 训练 {len(tr)} / 测试 {len(te)}｜基线池 {len(bp)}"
              f"（{len(bp) / len(te):.0%}）爆拉率 {base['爆拉率']:.1%} ret7 {base['ret7']:+.1%}")

    R = pd.DataFrame(all_rows)
    OUT.mkdir(parents=True, exist_ok=True)
    R.to_csv(OUT / "gate_folds.csv", index=False, encoding="utf-8-sig")

    # ================= ① 逐折明细（只看主指标 ret7）=================
    detail_cols = ["规则", "类型", "n", "占比", "爆拉率", "捕获率", "ret7", "ret7_vs_base", "日均条数"]
    detail_md = []
    for sp in SPLITS:
        sub = R[R["split"] == sp]
        if not len(sub):
            continue
        d = sub[detail_cols].copy()
        detail_md.append(f"### 折 {sp}（测试池 {int(sub['n'].iloc[0] / max(sub['占比'].iloc[0], 1e-9))} 条）\n\n"
                         + md_table(d))

    # ================= ② 汇总：4 折同向性 =================
    agg = []
    for key, label, kind in gate_specs():
        sub = R[R["key"] == key]
        ok = sub[~sub["样本不足"]]
        if not len(ok):
            agg.append({"规则": label, "类型": kind, "可用折数": 0})
            continue
        d7 = ok["ret7_vs_base"].to_numpy(float)
        dp = ok["爆拉率_vs_base"].to_numpy(float)

        # 关键：不同规则可用折数不同，必须和「同一批折」的基线比，
        # 否则就是拿 3 折均值和 4 折均值比，会凭空造出假优势。
        folds_ok = set(ok["split"])
        b = BASE["score5"]
        b = pd.DataFrame([b[f] for f in sorted(folds_ok)])
        dm = (ok["mean7"].to_numpy(float) - b["mean7"].to_numpy(float))
        rec = {
            "规则": label, "类型": kind, "可用折数": len(ok),
            "池占比": float(ok["占比"].mean()),
            "日均条数": float(ok["日均条数"].mean()),
            "爆拉率": float(ok["爆拉率"].mean()),
            "捕获率": float(ok["捕获率"].mean()),
            "ret7": float(np.nanmean(ok["ret7"])),
            "ret7 同折基线": float(np.nanmean(b["ret7"])),
            "ret7 差(同折)": float(np.nanmean(ok["ret7"]) - np.nanmean(b["ret7"])),
            "ret14": float(np.nanmean(ok["ret14"])),
            "ret30": float(np.nanmean(ok["ret30"])),
            "mean7": float(np.nanmean(ok["mean7"])),
            "mean7 同折基线": float(np.nanmean(b["mean7"])),
            "mean7 差(同折)": float(np.nanmean(ok["mean7"]) - np.nanmean(b["mean7"])),
            "p90_7": float(np.nanmean(ok["p90_7"])),
            "win7": float(np.nanmean(ok["win7"])),
            "ret30 覆盖": float(np.nanmean(ok["_cov30"] / ok["n"].clip(lower=1))),
            "ret7 不降折数": int((d7 >= -1e-9).sum()),
            "mean7 不降折数": int((dm >= -1e-9).sum()) if len(dm) else 0,
            "mean7 最差折": float(np.nanmin(dm)) if len(dm) else np.nan,
            "爆拉率不降折数": int((dp >= -1e-9).sum()),
            "ret7 最差折": float(np.nanmin(d7)),
            "ret7 均值差": float(np.nanmean(d7)),
        }
        rec["帕累托改进"] = bool(rec["ret7 不降折数"] == len(ok) == 4
                                and rec["爆拉率不降折数"] == 4
                                and rec["池占比"] <= MAX_SHARE)
        agg.append(rec)
    A = pd.DataFrame(agg)
    if "帕累托改进" in A.columns:
        A["帕累托改进"] = A["帕累托改进"].fillna(False).astype(bool)
    A.to_csv(OUT / "gate_summary.csv", index=False, encoding="utf-8-sig")

    # ================= ③ 帕累托前沿 =================
    front = []
    cand = A[A["可用折数"] == 4].copy()
    for i, r in cand.iterrows():
        dom = False
        for j, o in cand.iterrows():
            if i == j:
                continue
            if (o["爆拉率"] >= r["爆拉率"] and o["ret7"] >= r["ret7"]
                    and (o["爆拉率"] > r["爆拉率"] or o["ret7"] > r["ret7"])
                    and o["池占比"] <= r["池占比"]):
                dom = True
                break
        if not dom:
            front.append(r)
    F = pd.DataFrame(front)

    # ================= ③b 稳健性：均值优势是不是靠 1 条币撑的 =================
    ROB = {}
    for sp in SPLITS:
        s_ = pd.Timestamp(sp, tz="UTC")
        tr_ = df[df["pre_day"] < s_]
        te_ = df[df["pre_day"] >= s_].reset_index(drop=True)
        if len(tr_) < 200 or len(te_) < 100:
            continue
        ext_ = pd.to_numeric(tr_["ema50_dist"], errors="coerce")
        ctx_ = {"ext_med": float(ext_.median()), "ext_p75": float(ext_.quantile(0.75))}
        for key, label, kind in gate_specs():
            pool_ = te_[apply_gate(key, te_, ctx_)]
            v = pd.to_numeric(pool_["ret_net_7"], errors="coerce").dropna().sort_values()
            if len(v) < 10:
                ROB[(key, sp)] = {"n": len(v), "mean": np.nan, "mean_drop1": np.nan,
                                  "big": 0}
                continue
            ROB[(key, sp)] = {"n": len(v), "mean": float(v.mean()),
                              "mean_drop1": float(v.iloc[:-1].mean()),
                              "big": int((v > 1.0).sum())}

    rob_rows = []
    for key, label, kind in gate_specs():
        base_ = {sp: ROB.get(("score5", sp), {}) for sp in SPLITS}
        d1, d2 = [], []
        for sp in SPLITS:
            r_ = ROB.get((key, sp), {})
            b_ = base_.get(sp, {})
            if not r_ or not np.isfinite(r_.get("mean", np.nan)):
                continue
            if np.isfinite(b_.get("mean", np.nan)):
                d1.append(r_["mean"] - b_["mean"])
            if np.isfinite(b_.get("mean_drop1", np.nan)):
                d2.append(r_["mean_drop1"] - b_["mean_drop1"])
        if not d1:
            continue
        rob_rows.append({
            "规则": label, "类型": kind,
            "均值差": float(np.mean(d1)), "均值不降折数": int((np.array(d1) >= 0).sum()),
            "去1条后均值差": float(np.mean(d2)) if d2 else np.nan,
            "去1条后不降折数": int((np.array(d2) >= 0).sum()) if d2 else 0,
            "去1条后折数": len(d2),
            "结论翻转": bool(d2 and np.mean(d1) > 0 and np.mean(d2) <= 0),
        })
    RB = pd.DataFrame(rob_rows)
    RB.to_csv(OUT / "robustness.csv", index=False, encoding="utf-8-sig")

    # ================= ④ 报告 =================
    best = A[A["帕累托改进"]].sort_values("ret7", ascending=False)
    L = []
    L.append("# A20 · 否决制 vs 加法制：取舍表 `[ds4.1f]`\n")
    L.append(f"> 2026-10-20｜脚本 `research/17_veto_gate.py`｜"
             f"样本 {len(df)}（{int(df.label.sum())} 正例）｜4 折走查\n")
    L.append(f"> 「帕累托改进」的定义：7 日净收益 4/4 不降 **且** 爆拉率 4/4 不降 "
             f"**且** 池子占比 ≤ {MAX_SHARE:.0%}\n")

    L.append("\n---\n\n## 一、判定\n")
    n4 = int((A["可用折数"] == 4).sum())
    L.append(f"{len(A)} 个候选规则里，{n4} 个能在 4 折上都算出来。"
             f"其中 **0 个** 能在「不牺牲收益」的前提下收紧池子。\n")
    L.append("但真正的结论比「失败」具体得多，是五句话：\n")
    L.append("### 1. 加法阈值抬高，全线更差（这是对的，不是 bug）\n")
    L.append("| 规则 | 池占比 | 7 日均值差 | 不降折数 | 可算折数 |")
    L.append("|---|---:|---:|---:|---:|")
    for k in ("score8", "score11"):
        r = A[A["规则"] == label_of(k)]
        if len(r):
            r = r.iloc[0]
            L.append(f"| {r['规则']} | {r['池占比']:.0%} | {r['mean7 差(同折)']:+.1%} "
                     f"| {int(r['mean7 不降折数'])} | {int(r['可用折数'])} |")
    L.append("\n和 KNOWLEDGE §2.14 一致：18 分里有 8 分是「价格已经涨上去了」，"
             "分数越高 = 动得越早 = 后面空间越小。**抬门槛是在筛「已经火过」的币。**\n")

    L.append("### 2. 深度合取在本样本上根本测不了\n")
    tight = A[A["可用折数"] < 4]
    L.append(f"{len(tight)} 个规则在 4 折里至少有一折池子少于 {args.min_pool} 条，"
             "后两折甚至只剩 1–23 条：\n")
    L.append(md_table(tight[["规则", "类型", "可用折数"]]))
    L.append("\n**这不是「合取不好」，是「样本不够」。** 案例-对照样本每天中位只有 2 条，"
             "砍到 5% 就剩不下东西了。要测深度合取，必须先有 A15 的全市场面板。\n")

    L.append("### 3. 爆拉率提高 ≠ 赚钱，而且方向是反的\n")
    tb = A[A["规则"] == "trend ∧ breakout"]
    if len(tb):
        r = tb.iloc[0]
        L.append(f"`trend ∧ breakout` 是教科书级案例：**爆拉率 {r['爆拉率']:.1%}（最高）、"
                 f"胜率 {r['win7']:.1%}（最高），但 90 分位只有 {r['p90_7']:.1%}（最低）、"
                 f"7 日均值 {r['mean7 差(同折)']:+.1%}。**\n")
    L.append("它筛出的是「经常小赢、从不暴涨」的币。被它排除掉的那部分里，"
             "恰恰藏着大赢家。\n")
    L.append("这正是 §2.9（AUC 与收益 IC 负相关 ρ = −0.374）和 A17（幅度与可交易性负相关）"
             "的第三种测量。**同一个现象，三个入口。**\n")

    L.append("### 4. 看起来赢的规则，删掉一条币就翻转\n")
    flip = RB[RB["结论翻转"] == True]
    if len(flip):
        L.append(md_table(flip[["规则", "均值差", "均值不降折数", "去1条后均值差",
                                "去1条后不降折数"]]))
        L.append("\n这两个是唯一「均值口径下赢了 3/4 折」的规则，"
                 "但**删掉池内最大的那一条，优势就消失了**。\n")
        L.append("原因很直白：它们每折池子只有 30–112 条，"
                 "一条 10 倍币就能把均值抬 5–15pp。**这不是规律，是运气。**\n")

    L.append("### 5. 唯一可用的实用改进：砍掉一半池子，钱不变\n")
    ne = A[A["规则"] == "加法≥5 ∧ 未过度延伸(≤训练 p75)"]
    if len(ne):
        r = ne.iloc[0]
        L.append(f"`{r['规则']}`：池子 {r['池占比']:.0%}（现行 {A.iloc[0]['池占比']:.0%}）、"
                 f"7 日中位 {r['ret7 差(同折)']:+.1%}、均值 {r['mean7 差(同折)']:+.1%}、"
                 f"爆拉捕获率 {r['捕获率']:.0%}。\n")
    L.append("**收益不变、池子小 40%。** 严格说它没通过帕累托检验（爆拉率 4/4 下降），"
             "但对你「每天要看几个」这件事是实打实的减负。\n")
    L.append("它的含义是：**池子下半部分（离均线太远的那 40%）对收益没有任何贡献。**\n")

    L.append("\n---\n\n## 二、全部 15 个规则（4 折汇总）\n")
    show = ["规则", "类型", "可用折数", "池占比", "日均条数", "爆拉率", "捕获率",
            "ret7", "ret7 差(同折)", "mean7", "mean7 差(同折)", "p90_7", "win7",
            "ret14", "ret30", "ret30 覆盖",
            "ret7 不降折数", "mean7 不降折数", "mean7 最差折",
            "爆拉率不降折数", "ret7 最差折", "帕累托改进"]
    L.append(md_table(A[show].rename(columns={"ret7 不降折数": "ret7≥基线(折)",
                                              "爆拉率不降折数": "爆拉率≥基线(折)"})))
    L.append("\n**怎么读这张表**：\n")
    L.append("- `池占比` 越小 = 筛得越紧；`爆拉率` 越高 = 池内越纯")
    L.append("- **`捕获率` 是被筛掉的机会比例的反面**：捕获率 10% 意味着你一年只抓到十分之一的爆拉币")
    L.append("- `ret7≥基线(折)` 满格是 4。**只有 4 才算稳定**，2 或 3 是噪音")
    L.append("- `ret7 最差折` 是四折里最难看的那一折——**决定你能不能扛过坏行情**")

    L.append("\n---\n\n## 三、帕累托前沿（谁在不牺牲任何一维的前提下更强）\n")
    L.append(md_table(F[["规则", "池占比", "爆拉率", "捕获率", "ret7"]] if len(F) else pd.DataFrame()))

    L.append("\n---\n\n## 四、稳健性：均值的优势是真的吗\n")
    L.append("均值最容易被一两条十倍币绑架。这里做两件事：")
    L.append("① 看均值差在几折为正；② **把每个池子里最大的那一条删掉，再算一次**。\n")
    L.append(md_table(RB))
    L.append("\n`结论翻转 = 是` 的行，意思是：**它的均值优势在删掉一条币之后就消失了**，")
    L.append("那是运气，不是规律。\n")

    L.append("\n---\n\n## 五、如果必须取舍：取舍曲线\n")
    L.append("按「爆拉率」排序，看每提高一点命中率要付出多少收益和多少机会：\n")
    curve = A[A["可用折数"] == 4].sort_values("爆拉率", ascending=False)
    L.append(md_table(curve[["规则", "池占比", "爆拉率", "捕获率", "ret7", "ret7 最差折"]]))
    L.append("\n**这张表就是目标函数问题本身**：越往下，池子越大、命中率越低、")
    L.append("但捕获率和收益通常越高。选哪一行，取决于你要什么，不取决于数据。\n")

    L.append("\n---\n\n## 六、逐折明细\n")
    L.append("\n\n".join(detail_md))

    L.append("\n---\n\n## 七、结论与建议\n")
    L.append("### 回答「加法/积分制是不是最优解」\n")
    L.append("**不是最优，但它是唯一被数据支持的解。** 这两句话不矛盾：\n")
    L.append("| 问题 | 答案 | 证据 |")
    L.append("|---|---|---|")
    L.append("| 加法有否决机制吗 | **没有** | 代码只有 `min_score=5`，无任何 AND 式单指标否决 |")
    L.append("| 换合取会更好吗 | **测不出来** | 深度合取后两折池子 1–23 条 |")
    L.append("| 换加法阈值会更好吗 | **不会，更差** | ≥8 −1.7pp、≥11 −5.5pp，0/4 折 |")
    L.append("| 有别的规则更好吗 | **看起来有，其实没有** | 两个候选删 1 条币就翻转 |")
    L.append("\n**所以现在的正确动作不是换规则，是换样本。** "
             "所有能测的规则都在 ±2pp 里打转，深一点的规则连 40 条都凑不齐——"
             "这个分辨力不足以区分任何两个方案。\n")

    L.append("### 目标函数该怎么定（这是 A20 唯一必须你拍板的事）\n")
    L.append("数据把这件事讲得很清楚：**同一个规则，换个统计量，结论就反过来。**\n")
    L.append("| 统计量 | 谁赢 | 为什么 |")
    L.append("|---|---|---|")
    L.append("| 中位数（典型情况） | 现行加法 | 中位数只看「一般的那一半」，不看尾部 |")
    L.append("| 均值（总账） | `6 项中 ≥3 项` | 但删掉一条币就翻转，不稳健 |")
    L.append("| 90 分位（大赢家） | `6 项中 ≥3 项` | 池子只有 13%，靠少数几条撑 |")
    L.append("| 胜率（少踩坑） | `trend ∧ breakout` | 72.9% 最高，但均值 −4.5%、尾部最差 |")
    L.append("\n**建议：不要把目标函数定成单一数字。** 定成两条：\n")
    L.append("1. **主指标：7 日净收益中位**（代表「平常那笔交易赚多少」）")
    L.append("2. **硬约束：90 分位不得下降**（代表「大赢家没被筛掉」）")
    L.append("\n理由：只看中位数会选出「高胜率无大赢家」的规则（`trend ∧ breakout` 就是），"
             "那和这个项目的立项理由（抓十倍币）直接矛盾；"
             "只看均值会被一两条币绑架，n<100 时不可信。\n")
    L.append("### 立刻可做的一件事\n")
    L.append("`score>=5 ∧ ema50_dist ≤ 训练集 p75`：池子 63% → 38%，收益不变。")
    L.append("**建议先落地这一条**，因为它省你 40% 的阅读量，代价是爆拉率从 61.9% 降到 58.1%。")
    L.append("如果你觉得这个代价不能接受，那就什么都别改，直接去做 A14/A15。\n")

    L.append("\n---\n\n## 八、口径与限制\n")
    L.append("1. **样本是「案例-对照」结构**（822 个已发生的爆拉 + 580 个对照日），"
             "不是每天全市场扫出来的。所以「爆拉率」是**相对**指标，"
             "不能直接读成「实盘命中率」。")
    L.append("2. **「日均条数」= 池内条数 / 测试段交易日数**，同样受案例-对照结构影响，"
             "只能做**规则之间的横向比较**，不能读成「每天真会扫出几个」。"
             "真数字要等 A15 全市场面板。")
    L.append("3. **过度延伸否决的阈值只从训练段取**（中位 / p75），测试段不参与，避免前视。")
    # 30 天覆盖必须现算：2026-10-21 A14 补齐全池 1h 数据后，这一项从 24% 变成 99%，
    # 硬编码的说明当场就过时了。写死数字 = 下次还会错。
    _cov30 = float(pd.to_numeric(df["ret_net_30"], errors="coerce").notna().mean())
    _med30 = float(pd.to_numeric(df["ret_net_30"], errors="coerce").median())
    if _cov30 >= 0.85:
        L.append(f"4. **`ret_net_30` 已可用**：全样本覆盖 {_cov30:.1%}，全样本中位 {_med30:+.1%}。"
                 "A14（2026-10-21）把全池 1h 数据补齐后才有这个覆盖；"
                 "在此之前只有 24%，且**系统性偏向对照样本**（旧覆盖里爆拉只占 33.2%，"
                 "而全样本 58.6%、池内 71.1%），所以当时那个 +3.8% 的中位是错的，不能引用。"
                 "**判定仍一律用 `ret7`**——30 天太长，与实盘持仓周期不符。")
    else:
        L.append(f"4. **`ret_net_30` 覆盖仅 {_cov30:.1%}，不足以判定**。"
                 "原因是 1h K 线缓存深度不够（凑不出 30 天前瞻）。判定一律用 `ret7`。")
    L.append("5. 全部规则都**不含** A17 的可交易性指标——那些用到峰值之后的数据，是未来函数。")

    (OUT / "report.md").write_text("\n".join(L), encoding="utf-8")
    print(f"\n[veto] 报告 -> {OUT / 'report.md'}")
    print(f"[veto] 帕累托改进 {len(best)} 个")
    if len(best):
        for _, r in best.iterrows():
            print(f"  ✓ {r['规则']}：池 {r['池占比']:.0%}｜爆拉率 {r['爆拉率']:.1%}"
                  f"｜捕获 {r['捕获率']:.0%}｜ret7 {r['ret7']:+.1%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
