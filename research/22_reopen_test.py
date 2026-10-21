"""A27 附：重开检验 —— 被「换皮/擦线」判死的变量，在收益口径下还能不能活。

背景：A27 复核发现判死口径有系统性问题——
    A22/A25 判死用的是「对爆拉标签有没有增量」，但 A16 早就证明
    「能不能爆」和「赚多少」是两个相反的轴。
    用「爆拉增量」当裁判，会系统性误杀「对收益有用、但对爆拉没增量」的变量。

本脚本对 3 个可疑对象做同折检验（与 A20 完全同一套 SPLITS，阈值只从训练段取）：
    作为排序键：池内按它取前 50%，看 7 日净收益是否每折不降
    作为否决：池内剔掉它的后 33%，同上

对照组：score（现行）、taker_4h（A16 已证有效）、随机键（负对照）

用法：py -3.10 research/22_reopen_test.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

DATA = Path("data")
OUT = Path("reports/death_audit_ds")
SPLITS = ["2024-01-01", "2024-07-01", "2025-01-01", "2025-07-01"]
MIN_SCORE = 5
MIN_N = 40          # 单折最少样本
TAKE = 0.50         # 排序键：取前 50%
VETO_Q = 0.33       # 否决：剔掉后 33%


def load() -> pd.DataFrame:
    h = pd.read_csv(DATA / "hit_rate_current.csv", parse_dates=["pre_day"])
    h["pre_day"] = pd.to_datetime(h["pre_day"], utc=True).dt.tz_convert(None).dt.normalize()
    v = pd.read_parquet(DATA / "vars_cache.parquet")
    v["pre_day"] = pd.to_datetime(v["pre_day"], utc=True).dt.tz_convert(None).dt.normalize()
    fr = pd.read_csv(DATA / "forward_returns.csv", parse_dates=["pre_day"])
    fr["pre_day"] = pd.to_datetime(fr["pre_day"], utc=True).dt.tz_convert(None).dt.normalize()

    d = h[["symbol", "pre_day", "score", "label", "taker"]].merge(
        v[["symbol", "pre_day", "mom_accel", "tr_ratio_4_24", "ats_ratio",
           "pos_range_168", "ema50_dist", "rv_ratio_24_168", "taker_ret_corr_7d"]],
        on=["symbol", "pre_day"], how="left")
    d = d.merge(fr[["symbol", "pre_day", "ret_net_7"]], on=["symbol", "pre_day"], how="left")
    d["taker_4h"] = pd.to_numeric(d["taker"], errors="coerce")
    # M09 成交分型：笔数比 / 均单比
    d["m09"] = d["tr_ratio_4_24"] / d["ats_ratio"].replace(0, np.nan)
    # 随机键（负对照）
    d["rand"] = np.random.default_rng(0).random(len(d))
    d["fold"] = np.searchsorted(
        np.array([pd.Timestamp(s).value for s in SPLITS]),
        d["pre_day"].astype("int64").to_numpy(), side="right")
    return d


KEYS = [
    ("score（现行，基线）", "score", "baseline"),
    ("taker_4h（A16 已证有效，正对照）", "taker_4h", "rank"),
    ("随机键（负对照）", "rand", "rank"),
    ("M30 动量加速度", "mom_accel", "rank"),
    ("M09 成交分型", "m09", "rank"),
    ("rv_ratio_24_168（活着，正对照）", "rv_ratio_24_168", "rank"),
    ("taker_ret_corr_7d（活着，正对照）", "taker_ret_corr_7d", "rank"),
    ("M30 当否决", "mom_accel", "veto"),
    ("M09 当否决", "m09", "veto"),
    ("pos_range_168 当否决（A20 已测）", "pos_range_168", "veto"),
]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    d = load()
    pool = d[d["score"] >= MIN_SCORE].copy()
    print(f"[reopen] 池内 {len(pool)} 条，{pool['fold'].value_counts().sort_index().to_dict()}")

    rows = []
    for name, col, mode in KEYS:
        x = pd.to_numeric(pool[col], errors="coerce")
        rec = {"键": name, "模式": {"baseline": "基线", "rank": "排序取前50%", "veto": "剔后33%"}[mode]}
        dif_med, dif_mean = [], []
        for f in range(len(SPLITS) + 1):
            tr = pool[pool["fold"] < f]
            te = pool[pool["fold"] == f]
            if len(te) < MIN_N or len(tr) < 200:
                continue
            base = te["ret_net_7"].median()
            if mode == "baseline":
                sel = te
            else:
                xt = pd.to_numeric(tr[col], errors="coerce").dropna()
                if len(xt) < 100:
                    continue
                thr = float(xt.quantile(1 - TAKE if mode == "rank" else VETO_Q))
                xe = pd.to_numeric(te[col], errors="coerce")
                sel = te[xe >= thr] if mode == "rank" else te[xe > thr]
            if len(sel) < MIN_N // 2:
                continue
            dif_med.append(float(sel["ret_net_7"].median()) - float(base))
            dif_mean.append(float(sel["ret_net_7"].mean()) - float(te["ret_net_7"].mean()))
        rec["可用折"] = len(dif_med)
        rec["中位差均值"] = float(np.mean(dif_med)) if dif_med else np.nan
        rec["中位不降折数"] = int(sum(1 for v in dif_med if v >= -1e-12))
        rec["均值差均值"] = float(np.mean(dif_mean)) if dif_mean else np.nan
        rec["均值不降折数"] = int(sum(1 for v in dif_mean if v >= -1e-12))
        rows.append(rec)

    R = pd.DataFrame(rows)
    R.to_csv(OUT / "reopen_test.csv", index=False, encoding="utf-8-sig")

    pd.set_option("display.width", 220)
    print(R.to_string(index=False))

    # ---- 零分布：随机键跑 N 次，看「不降折数」的分布 ----
    print("\n[reopen] 随机键零分布（N=200）…")
    rng = np.random.default_rng(7)
    null_med, null_mean = [], []
    for _ in range(200):
        pool["_rk"] = rng.random(len(pool))
        dm, dn = [], []
        for f in range(len(SPLITS) + 1):
            tr = pool[pool["fold"] < f]
            te = pool[pool["fold"] == f]
            if len(te) < MIN_N or len(tr) < 200:
                continue
            thr = float(tr["_rk"].quantile(1 - TAKE))
            sel = te[te["_rk"] >= thr]
            if len(sel) < MIN_N // 2:
                continue
            dm.append(float(sel["ret_net_7"].median()) - float(te["ret_net_7"].median()))
            dn.append(float(sel["ret_net_7"].mean()) - float(te["ret_net_7"].mean()))
        null_med.append(sum(1 for v in dm if v >= -1e-12))
        null_mean.append(sum(1 for v in dn if v >= -1e-12))
    nm = pd.Series(null_med)
    nn = pd.Series(null_mean)
    print(f"  中位不降折数：均值 {nm.mean():.2f}｜分布 {nm.value_counts().sort_index().to_dict()}")
    print(f"  均值不降折数：均值 {nn.mean():.2f}｜分布 {nn.value_counts().sort_index().to_dict()}")
    print(f"  随机键达到 4/4 的概率：中位 {(nm==4).mean():.1%}｜均值 {(nn==4).mean():.1%}")
    pd.DataFrame({"中位不降折数": nm, "均值不降折数": nn}).to_csv(
        OUT / "null_dist.csv", index=False, encoding="utf-8-sig")
    return 0


if __name__ == "__main__":
    sys.exit(main())
