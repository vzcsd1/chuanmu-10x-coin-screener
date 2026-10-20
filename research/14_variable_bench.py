#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""14 · 变量试验台（ds4.1f）

目的
----
给"再加一个变量有没有用"一把可复现的尺子。任何候选变量丢进来，输出四件事：

  1. 覆盖率        —— 有多少样本算得出来（不够就不许下结论）
  2. 区分度 AUC    —— 能不能分出"会翻倍" vs "不会"
  3. 前瞻 IC       —— 和**未来**收益的相关（label=1 / label=0 分开算）
  4. 混杂剔除 IC   —— 扣掉 ret_1d / ret_7d / 现有 score 之后还剩多少

第 4 项是核心。项目已知：现有 score 度量的是"已经涨了多少"，不是"还要涨多少"
（见 research_committee/前瞻收益_首次结果与含义_20261020.md）。
所以任何一个新变量，如果和 ret_1d 高度相关，它就不是新信息，只是动量换皮。

判定纪律
--------
- **零分布校准**：每个指标都用打乱后的变量重算 N 次，给出 5%/50%/95% 分位。
  真实值落在零分布带内 = 没有信息，无论它看起来多好。
- **分组内 IC**：爆拉组和对照组分开算。混在一起算会被 58.6% 的正例比例污染。
- **样本量门槛**：有效样本 < 200 的变量不进入结论区。

数据口径（与 13_forward_return.py 完全一致）
--------------------------------------------
  决策时点：pre_day 23:00:00（22:00 那根 K 线收盘）
  只用 dt < pre_day+23h 的已收盘 K 线
  入场价：pre_day 23:00 那根 K 线的 open
  成本：0.30% 双边（已在 ret_net_* 里扣掉）

用法
----
  py -3.10 research/14_variable_bench.py --perm 200
  py -3.10 research/14_variable_bench.py --only taker,v_ratio --perm 100
  py -3.10 research/14_variable_bench.py --limit 200          # 快速冒烟
  py -3.10 research/14_variable_bench.py --mod                # 条件效应模式
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
H1 = DATA / "1h"
D1 = DATA / "1d"
OUT_CSV = DATA / "variable_bench.csv"
REPORT_DIR = ROOT / "reports" / "variable_bench_ds"

TAG = "ds4.1f"
MIN_N = 200            # 进入结论区的最小有效样本
PRIMARY_H = 7          # 主判定期（n 最大且不含标签污染）
SECOND_H = 14

# ---------------------------------------------------------------- 基础工具


def _s(x):
    """安全除法，除零/NaN 统一返回 NaN。"""
    x = np.asarray(x, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        return x


def _div(a, b):
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    out = np.full_like(a, np.nan, dtype=float)
    ok = np.isfinite(a) & np.isfinite(b) & (b != 0)
    out[ok] = a[ok] / b[ok]
    return out


def _spearman(a, b) -> float:
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    m = np.isfinite(a) & np.isfinite(b)
    if m.sum() < 30:
        return np.nan
    ra = pd.Series(a[m]).rank().values
    rb = pd.Series(b[m]).rank().values
    if ra.std() == 0 or rb.std() == 0:
        return np.nan
    return float(np.corrcoef(ra, rb)[0, 1])


def _auc(y, x) -> float:
    """Mann-Whitney AUC：P(x_pos > x_neg)。"""
    y = np.asarray(y, dtype=float)
    x = np.asarray(x, dtype=float)
    m = np.isfinite(x) & np.isfinite(y)
    if m.sum() < 30:
        return np.nan
    yy, xx = y[m], x[m]
    n1 = float((yy == 1).sum())
    n0 = float((yy == 0).sum())
    if n1 == 0 or n0 == 0:
        return np.nan
    r = pd.Series(xx).rank().values
    return float((r[yy == 1].sum() - n1 * (n1 + 1) / 2.0) / (n1 * n0))


def _resid(y, X) -> np.ndarray:
    """把 y 对 X（含常数项）回归后的残差。"""
    y = np.asarray(y, dtype=float)
    X = np.asarray(X, dtype=float)
    m = np.isfinite(y) & np.isfinite(X).all(axis=1)
    out = np.full(len(y), np.nan)
    if m.sum() < 50:
        return out
    A = np.column_stack([np.ones(m.sum()), X[m]])
    beta, *_ = np.linalg.lstsq(A, y[m], rcond=None)
    out[m] = y[m] - A @ beta
    return out


def _partial_spearman(y, x, ctrl) -> float:
    m = np.isfinite(y) & np.isfinite(x) & np.isfinite(ctrl).all(axis=1)
    if m.sum() < 60:
        return np.nan
    ry = _resid(y[m], ctrl[m])
    rx = _resid(x[m], ctrl[m])
    return _spearman(ry, rx)


def _ewm(arr, span):
    return pd.Series(arr).ewm(span=span, adjust=False).mean().values


def _slope(y) -> float:
    y = np.asarray(y, dtype=float)
    y = y[np.isfinite(y)]
    if len(y) < 10:
        return np.nan
    x = np.arange(len(y), dtype=float)
    return float(np.polyfit(x, y, 1)[0])


# ---------------------------------------------------------------- 数据加载

_BD = pd.Timestamp("2022-04-01", tz="UTC")


def months_before(pre_day: pd.Timestamp, days: int = 130):
    start = (pre_day - pd.Timedelta(days=days)).replace(day=1)
    out, cur = [], start
    while cur <= pre_day:
        out.append(cur.strftime("%Y-%m"))
        cur = (cur + pd.DateOffset(months=1)).replace(day=1)
    return out


def read_month(sym: str, ym: str) -> pd.DataFrame | None:
    p = H1 / f"{sym}-{ym}.parquet"
    if not p.exists():
        return None
    try:
        d = pd.read_parquet(p)
    except Exception:  # noqa: BLE001
        return None
    if "dt" not in d.columns:
        d["dt"] = pd.to_datetime(d["open_time"], unit="ms", utc=True)
    return d.sort_values("dt").reset_index(drop=True)


class Bench:
    def __init__(self, btc: pd.DataFrame):
        self.btc = btc

    # ---------- 单个样本的 K 线特征 ----------
    def bar_features(self, b: pd.DataFrame) -> dict:
        n = len(b)
        f: dict[str, float] = {}
        c = b["close"].values.astype(float)
        o = b["open"].values.astype(float)
        h = b["high"].values.astype(float)
        lo = b["low"].values.astype(float)
        v = b["volume"].values.astype(float)
        qv = b["quote_volume"].values.astype(float) if "quote_volume" in b else v
        tr = b["trades"].values.astype(float) if "trades" in b else np.full(n, np.nan)
        tbq = b["taker_buy_quote"].values.astype(float) if "taker_buy_quote" in b else np.full(n, np.nan)
        r = np.diff(np.log(c), prepend=np.nan)

        px = c[-1]
        if not np.isfinite(px) or px <= 0:
            return f

        def tail(a, k):
            return a[-k:] if n >= k else a

        # ============ A. 量能 ============
        v4, v4p = tail(v, 4).sum(), tail(v, 8)[:4].sum()
        v24, v24p = tail(v, 24).sum(), tail(v, 48)[:24].sum()
        f["v_ratio_4_4"] = _div([v4], [v4p])[0]
        f["v_ratio_24_24"] = _div([v24], [v24p])[0]
        f["v_ratio_4_24"] = _div([v4 / 4.0], [v24 / 24.0])[0]
        v24a = tail(v, 24)
        if np.isfinite(v24a).all() and np.nanstd(v24a) > 0:
            f["v_z_24"] = float((v[-1] - np.nanmean(v24a)) / np.nanstd(v24a))
            f["v_z_24_max4"] = float(np.nanmax((tail(v, 4) - np.nanmean(v24a)) / np.nanstd(v24a)))
            sv = np.nansum(v24a)
            f["v_hhi_24"] = float(np.nansum((v24a / sv) ** 2)) if sv > 0 else np.nan
        v72 = tail(v, 72)
        f["v_slope_72"] = _slope(np.log1p(v72))
        v168 = tail(v, 168)
        f["v_cv_7d"] = _div([np.nanstd(v168)], [np.nanmean(v168)])[0]
        f["qv_24_log"] = float(np.log10(max(tail(qv, 24).sum(), 1.0)))
        f["qv_24"] = float(tail(qv, 24).sum())
        f["tr_ratio_4_24"] = _div([tail(tr, 4).sum() / 4.0], [tail(tr, 24).sum() / 24.0])[0]
        ats4 = _div(tail(qv, 4), tail(tr, 4))
        ats24 = _div(tail(qv, 24), tail(tr, 24))
        f["ats_4"] = float(np.nanmean(ats4)) if np.isfinite(ats4).any() else np.nan
        f["ats_ratio"] = _div([np.nanmean(ats4)], [np.nanmean(ats24)])[0]
        a_now = _div([qv[-1]], [tr[-1]])[0]
        if np.isfinite(ats24).all() and np.nanstd(ats24) > 0:
            f["ats_z_24"] = float((a_now - np.nanmean(ats24)) / np.nanstd(ats24))

        # 主动买盘（H03 CVD 吸收）
        tk4 = _div([tail(tbq, 4).sum()], [tail(qv, 4).sum()])[0]
        tk24 = _div([tail(tbq, 24).sum()], [tail(qv, 24).sum()])[0]
        tk168 = _div([tail(tbq, 168).sum()], [tail(qv, 168).sum()])[0]
        f["taker_4"] = tk4
        f["taker_24"] = tk24
        f["taker_168"] = tk168
        f["taker_delta"] = (tk4 - tk24) if np.isfinite(tk4) and np.isfinite(tk24) else np.nan
        f["taker_delta_7d"] = (tk24 - tk168) if np.isfinite(tk24) and np.isfinite(tk168) else np.nan
        # 小时级 taker 比序列
        tk_hr = _div(tbq, qv)
        t24 = tail(tk_hr, 24)
        t168 = tail(tk_hr, 168)
        if np.isfinite(t168).sum() > 30:
            f["taker_slope_24"] = _slope(t24)
            f["taker_slope_168"] = _slope(t168)
            f["taker_z_7d"] = _div([np.nanmean(t24) - np.nanmean(t168)], [np.nanstd(t168)])[0]
        # CVD 与价格的背离：累计净主动买的变动 vs 价格变动
        if np.isfinite(tk_hr).sum() > 30:
            net = tbq - (qv - tbq)
            cvd = np.cumsum(np.nan_to_num(net))
            f["cvd_price_corr_7d"] = _spearman(tail(cvd, 168), tail(c, 168))
            f["cvd_div_7d"] = _div([tail(cvd, 168)[-1] - tail(cvd, 168)[0]],
                                   [abs(tail(cvd, 168)).sum()])[0]
        f["vol_absret_corr_7d"] = _spearman(tail(v, 168), np.abs(tail(r, 168)))
        f["taker_ret_corr_7d"] = _spearman(tail(tk_hr, 168), tail(r, 168))

        # ============ B. 波动 ============
        def rv(k):
            a = tail(r, k)
            a = a[np.isfinite(a)]
            return float(np.std(a) * np.sqrt(len(a))) if len(a) >= 3 else np.nan

        f["rv_4"] = rv(4)
        f["rv_24"] = rv(24)
        f["rv_168"] = rv(168)
        f["rv_720"] = rv(720)
        f["rv_ratio_24_168"] = _div([f["rv_24"]], [f["rv_168"]])[0]
        f["rv_ratio_4_24"] = _div([f["rv_4"]], [f["rv_24"]])[0]
        f["rv_ratio_168_720"] = _div([f["rv_168"]], [f["rv_720"]])[0]
        # ATR14
        trs = np.maximum(h[1:] - lo[1:], np.maximum(np.abs(h[1:] - c[:-1]), np.abs(lo[1:] - c[:-1])))
        f["atr_ratio"] = _div([np.nanmean(tail(trs, 14))], [px])[0]
        f["range_24"] = float(np.nanmean(_div(tail(h, 24) - tail(lo, 24), tail(c, 24))))
        f["range_168"] = float(np.nanmean(_div(tail(h, 168) - tail(lo, 168), tail(c, 168))))
        f["range_ratio_24_168"] = _div([f["range_24"]], [f["range_168"]])[0]
        # 箱体宽度（与项目 c_box12 同定义）
        bw24 = _div([tail(h, 24).max() - tail(lo, 24).min()], [px])[0]
        f["box_width_24"] = bw24
        f["box_width_168"] = _div([tail(h, 168).max() - tail(lo, 168).min()], [px])[0]
        # 回撤
        c168 = tail(c, 168)
        if len(c168) > 10:
            peak = np.maximum.accumulate(c168)
            f["dd_7d"] = float(np.nanmin(_div(c168 - peak, peak)))
        neg = tail(r, 168)
        neg = neg[np.isfinite(neg) & (neg < 0)]
        f["downside_vol_7d"] = float(np.std(neg)) if len(neg) > 10 else np.nan
        f["skew_7d"] = float(pd.Series(tail(r, 168)).skew())
        f["kurt_7d"] = float(pd.Series(tail(r, 168)).kurt())
        # vol of vol
        rs = pd.Series(r).rolling(24).std()
        f["vol_of_vol"] = _div([rs.tail(168).std()], [rs.tail(168).mean()])[0]
        # 影线/实体
        rng = tail(h, 24) - tail(lo, 24)
        body = np.abs(tail(c, 24) - tail(o, 24))
        upw = tail(h, 24) - np.maximum(tail(c, 24), tail(o, 24))
        dnw = np.minimum(tail(c, 24), tail(o, 24)) - tail(lo, 24)
        f["body_ratio_24"] = float(np.nanmean(_div(body, rng)))
        f["wick_up_24"] = float(np.nanmean(_div(upw, rng)))
        f["wick_dn_24"] = float(np.nanmean(_div(dnw, rng)))
        f["wick_asym_24"] = f["wick_dn_24"] - f["wick_up_24"]
        # 排列熵（H55）：24 根 K 线的收益符号 + 幅度分箱
        r24 = tail(r, 24)
        r24 = r24[np.isfinite(r24)]
        if len(r24) > 12:
            bins = pd.qcut(pd.Series(r24).rank(method="first"), 3, labels=False)
            p = np.bincount(bins.values, minlength=3) / len(bins)
            p = p[p > 0]
            f["perm_entropy_24"] = float(-(p * np.log(p)).sum() / np.log(3))

        # ============ C. 动量 ============
        for k, nm in ((4, "4"), (12, "12"), (24, "24"), (72, "72"), (168, "168"), (720, "720")):
            if n > k:
                f[f"ret_{nm}"] = float(px / c[-1 - k] - 1.0)
        f["mom_accel"] = (f.get("ret_24", np.nan) - _div([f.get("ret_168", np.nan)], [7.0])[0]) \
            if np.isfinite(f.get("ret_24", np.nan)) else np.nan
        f["mom_accel2"] = (f.get("ret_4", np.nan) * 6 - f.get("ret_24", np.nan)) \
            if np.isfinite(f.get("ret_4", np.nan)) else np.nan
        # RSI
        def rsi(k):
            a = tail(np.nan_to_num(np.diff(tail(c, k + 1))), k)
            if len(a) < k // 2:
                return np.nan
            up = a[a > 0].sum()
            dn = -a[a < 0].sum()
            return float(100 * up / (up + dn)) if (up + dn) > 0 else np.nan

        f["rsi_24"] = rsi(24)
        f["rsi_168"] = rsi(168)
        # 连续阳线
        ups = (tail(c, 24) > tail(o, 24)).astype(int)
        run = 0
        for x in ups[::-1]:
            if x:
                run += 1
            else:
                break
        f["consec_up"] = run
        f["up_ratio_24"] = float(np.nanmean(ups))
        # 距离高点 / 区间位置
        for k, nm in ((24, "24"), (168, "168"), (720, "720")):
            hi, lw = tail(h, k).max(), tail(lo, k).min()
            f[f"dist_high_{nm}"] = _div([hi - px], [px])[0]
            f[f"dist_low_{nm}"] = _div([px - lw], [px])[0]
            f[f"pos_range_{nm}"] = _div([px - lw], [hi - lw])[0]
            f[f"new_high_{nm}"] = 1.0 if px >= hi * 0.999 else 0.0
        # EMA50（H: 10x 币启动前跌破 EMA50 假说）
        ema50 = _ewm(c, 50)
        f["ema50_dist"] = _div([px - ema50[-1]], [ema50[-1]])[0]
        f["below_ema50"] = 1.0 if px < ema50[-1] else 0.0
        f["ema50_slope_24"] = _div([ema50[-1] - ema50[-25]], [ema50[-25]])[0] if n > 60 else np.nan
        # 最近一次上穿 EMA50 距今天数
        above = c > ema50
        cross = np.where(np.diff(above.astype(int)) == 1)[0]
        f["bars_since_cross_up"] = float(n - 1 - cross[-1]) if len(cross) else np.nan
        # 收益质量
        r168 = tail(r, 168)
        f["ret_skew_168"] = _div([np.nanmean(r168)], [np.nanstd(r168)])[0]

        # ============ D. 结构 ============
        # 箱体持续小时数（复刻项目 c_box12 口径）
        age = 0
        for j in range(n - 1, max(n - 720, 24), -1):
            w = _div([h[j - 23:j + 1].max() - lo[j - 23:j + 1].min()], [c[j]])[0]
            if np.isfinite(w) and w <= 0.12:
                age += 1
            else:
                break
        f["box_age_h_bar"] = float(age)
        # 箱体上沿被触碰次数
        box_hi = tail(h, 168).max()
        f["box_touches"] = float((tail(h, 168) >= box_hi * 0.98).sum())
        # VCP 收缩（H33）：三段波幅递减
        seg = [np.nanstd(tail(r, k)) for k in (168, 72, 24)]
        if all(np.isfinite(seg)) and seg[0] > 0:
            f["vcp_score"] = float((seg[0] - seg[1]) / seg[0] * 0.5 + (seg[1] - seg[2]) / max(seg[1], 1e-9) * 0.5)
        # 缩量程度
        f["vol_dryup"] = _div([np.nanmean(tail(v, 24))], [np.nanmean(tail(v, 720))])[0]
        # 沉寂天数：最近 168h 里 24h 滚动涨幅绝对值 < 3% 的窗口占比
        if n > 200:
            roll24 = pd.Series(c).pct_change(24)
            quiet = (roll24.tail(168).abs() < 0.03).mean()
            f["quiet_ratio_7d"] = float(quiet)
        # 静默期长度：距上一次单小时涨幅 > 8% 有多少小时
        big = np.where(tail(r, 720) > 0.08)[0]
        f["bars_since_big_up"] = float(len(tail(r, 720)) - 1 - big[-1]) if len(big) else np.nan

        # ============ E. 基准相对（H17/H18） ============
        try:
            bt = self.btc
            t0 = b["dt"].iloc[-1]
            sub = bt[bt.dt <= t0]
            if len(sub) > 200:
                bc = sub["close"].values
                br = np.diff(np.log(bc), prepend=np.nan)
                for k, nm in ((24, "24"), (168, "168")):
                    if len(bc) > k:
                        f[f"btc_ret_{nm}"] = float(bc[-1] / bc[-1 - k] - 1.0)
                        cr = tail(r, k)
                        mr = tail(br, k)
                        m = np.isfinite(cr) & np.isfinite(mr)
                        if m.sum() > 20 and np.std(mr[m]) > 0:
                            beta = float(np.cov(cr[m], mr[m])[0, 1] / np.var(mr[m]))
                            f[f"beta_{nm}"] = beta
                            f[f"alpha_{nm}"] = f.get(f"ret_{nm}", np.nan) - beta * f[f"btc_ret_{nm}"]
                            f[f"rel_strength_{nm}"] = f.get(f"ret_{nm}", np.nan) - f[f"btc_ret_{nm}"]
                # BTC 急跌时该币的抗跌度（H17）
                br168 = tail(br, 168)
                cr168 = tail(r, 168)
                mm = np.isfinite(br168) & np.isfinite(cr168) & (br168 < -0.02)
                f["btc_drop_resilience"] = float(np.nanmean(cr168[mm])) if mm.sum() >= 3 else np.nan
                f["btc_drop_n"] = float(mm.sum())
        except Exception:  # noqa: BLE001
            pass

        # ============ F. 时间 ============
        t0 = b["dt"].iloc[-1]
        f["dow"] = float(t0.dayofweek)
        f["hour"] = float(t0.hour)
        f["days_since_2022"] = float((t0 - _BD).days)
        return f


# ---------------------------------------------------------------- 变量注册表
# 每条：id -> (中文名, 对应研究假设编号, 计算函数名)
# 计算函数 = bar_features 输出的列名；None 表示直接取 hit_rate_current 已有列。
REGISTRY: list[tuple[str, str, str, str | None]] = [
    # ---- 量能 ----
    ("v_ratio_4_4", "4h 量能 / 前 4h", "H07", None),
    ("v_ratio_24_24", "24h 量能 / 前 24h", "H07", None),
    ("v_ratio_4_24", "4h 量能相对 24h 强度", "H07", None),
    ("v_z_24", "单根量能 24h z 值", "H07", None),
    ("v_z_24_max4", "近 4h 量能最大 z 值", "H07", None),
    ("v_hhi_24", "24h 量能集中度 HHI", "H20", None),
    ("v_slope_72", "72h 量能斜率", "H15", None),
    ("v_cv_7d", "7 日量能变异系数", "H15", None),
    ("qv_24_log", "24h 成交额（log10）", "H46", None),
    ("tr_ratio_4_24", "4h 成交笔数比", "H20", None),
    ("ats_4", "单笔平均成交额（4h）", "H02", None),
    ("ats_ratio", "单笔成交额 4h/24h", "H02", None),
    ("ats_z_24", "单笔成交额 24h z 值", "H02", None),
    ("taker_4", "主动买占比（4h）", "H03", None),
    ("taker_24", "主动买占比（24h）", "H03", None),
    ("taker_168", "主动买占比（7d）", "H03", None),
    ("taker_delta", "主动买占比 4h−24h", "H03", None),
    ("taker_delta_7d", "主动买占比 24h−7d", "H03", None),
    ("taker_slope_24", "主动买占比 24h 斜率", "H03", None),
    ("taker_slope_168", "主动买占比 7d 斜率", "H03", None),
    ("taker_z_7d", "主动买占比相对 7d 的 z 值", "H03", None),
    ("cvd_price_corr_7d", "CVD 与价格相关性", "H03", None),
    ("cvd_div_7d", "CVD 净流入强度", "H03", None),
    ("vol_absret_corr_7d", "量能与绝对收益相关", "H20", None),
    ("taker_ret_corr_7d", "主动买与收益相关（吸收效率）", "H03", None),
    # ---- 波动 ----
    ("rv_4", "4h 已实现波动", "H20", None),
    ("rv_24", "24h 已实现波动", "H20", None),
    ("rv_168", "7d 已实现波动", "H20", None),
    ("rv_720", "30d 已实现波动", "H20", None),
    ("rv_ratio_24_168", "波动压缩比 24h/7d", "H33", None),
    ("rv_ratio_4_24", "波动扩张比 4h/24h", "H20", None),
    ("rv_ratio_168_720", "波动压缩比 7d/30d", "H33", None),
    ("atr_ratio", "ATR14 / 价格", "H38", None),
    ("range_24", "24h 平均振幅", "H20", None),
    ("range_168", "7d 平均振幅", "H20", None),
    ("range_ratio_24_168", "振幅压缩比", "H33", None),
    ("box_width_24", "24h 箱体宽度", "现有c_box12", None),
    ("box_width_168", "7d 箱体宽度", "现有c_box12", None),
    ("dd_7d", "7 日最大回撤", "H26", None),
    ("downside_vol_7d", "下行波动", "H26", None),
    ("skew_7d", "收益偏度", "H20", None),
    ("kurt_7d", "收益峰度", "H20", None),
    ("vol_of_vol", "波动率的波动率", "H20", None),
    ("body_ratio_24", "实体占比", "H21", None),
    ("wick_up_24", "上影线占比", "H21", None),
    ("wick_dn_24", "下影线占比", "H21", None),
    ("wick_asym_24", "下影线−上影线（吸筹）", "H21", None),
    ("perm_entropy_24", "K 线排列熵", "H55", None),
    # ---- 动量 ----
    ("ret_4", "4h 涨幅", "现有c_v24up", None),
    ("ret_12", "12h 涨幅", "现有c_v24up", None),
    ("ret_24", "24h 涨幅", "现有c_v24up", None),
    ("ret_72", "3 日涨幅", "新", None),
    ("ret_168", "7 日涨幅", "新", None),
    ("ret_720", "30 日涨幅", "新", None),
    ("mom_accel", "动量加速度（1d vs 7d/7）", "H19", None),
    ("mom_accel2", "动量加速度（4h×6 vs 1d）", "H19", None),
    ("rsi_24", "RSI(24h)", "新", None),
    ("rsi_168", "RSI(7d)", "新", None),
    ("consec_up", "连续阳线根数", "H20", None),
    ("up_ratio_24", "24h 阳线占比", "H20", None),
    ("dist_high_24", "距 24h 高点距离", "现有c_breakout", None),
    ("dist_high_168", "距 7d 高点距离", "H34", None),
    ("dist_high_720", "距 30d 高点距离", "H34", None),
    ("dist_low_24", "距 24h 低点距离", "H15", None),
    ("dist_low_168", "距 7d 低点距离", "H15", None),
    ("pos_range_24", "24h 区间位置", "新", None),
    ("pos_range_168", "7d 区间位置", "新", None),
    ("pos_range_720", "30d 区间位置", "H34", None),
    ("new_high_24", "创 24h 新高", "现有c_breakout", None),
    ("new_high_168", "创 7d 新高", "H37", None),
    ("new_high_720", "创 30d 新高", "H37", None),
    ("ema50_dist", "距 EMA50 偏离度", "H: 假说α/β", None),
    ("below_ema50", "是否跌破 EMA50", "H: 假说α", None),
    ("ema50_slope_24", "EMA50 24h 斜率", "H: 假说β", None),
    ("bars_since_cross_up", "距上次上穿 EMA50 小时数", "H: 假说α", None),
    ("ret_skew_168", "7d 收益夏普", "H20", None),
    # ---- 结构 ----
    ("box_age_h_bar", "箱体持续小时数（复算）", "现有box_age_h", None),
    ("box_touches", "箱体上沿触碰次数", "H33", None),
    ("vcp_score", "VCP 波动收缩得分", "H33", None),
    ("vol_dryup", "缩量程度（24h/30d）", "H15", None),
    ("quiet_ratio_7d", "沉寂窗口占比", "H15", None),
    ("bars_since_big_up", "距上次单小时暴涨小时数", "H15", None),
    # ---- 基准 ----
    ("btc_ret_24", "BTC 24h 涨幅", "H58", None),
    ("btc_ret_168", "BTC 7d 涨幅", "H58", None),
    ("beta_24", "24h 对 BTC beta", "H18", None),
    ("beta_168", "7d 对 BTC beta", "H18", None),
    ("alpha_24", "24h 纯 Alpha 残差", "H18", None),
    ("alpha_168", "7d 纯 Alpha 残差", "H18", None),
    ("rel_strength_24", "24h 相对强度", "H18", None),
    ("rel_strength_168", "7d 相对强度", "H18", None),
    ("btc_drop_resilience", "BTC 急跌时抗跌度", "H17", None),
    # ---- 时间 ----
    ("dow", "星期几", "KX-4", None),
    ("is_month_end", "是否月末", "KX-4", None),
    ("days_since_2022", "距 2022-04 天数（时间漂移）", "H49", None),
    ("days_since_listing", "币龄（距上市天数）", "KX-5", None),
    ("days_since_last_pump", "距上次爆拉天数", "新", None),
    ("n_past_pumps_365", "近一年爆拉次数", "新", None),
    # ---- 现有衍生品字段（作为基线一起测）----
    ("oi_chg_1d", "OI 1 日变化", "现有权重4", "raw"),
    ("oi_chg_3d", "OI 3 日变化", "现有权重3", "raw"),
    ("oi_chg_7d", "OI 7 日变化", "新", "raw"),
    ("ls_top", "大户多空比", "现有权重3", "raw"),
    ("ls_top_chg_3d", "大户多空比 3 日变化", "H05", "raw"),
    ("ls_count_chg_3d", "全账户多空比 3 日变化", "H05", "raw"),
    ("taker", "合约 taker 买卖比", "现有", "raw"),
    ("funding_last", "最新资金费率", "H06", "raw"),
    ("funding_mean_7d", "7 日平均费率", "H06", "raw"),
    ("funding_neg_ratio_7d", "7 日负费率占比", "H06", "raw"),
    ("funding_min_7d", "7 日最低费率", "H06", "raw"),
    ("oi_value", "OI 名义值", "H04", "raw"),
    # ---- 现有 K 线字段（基线）----
    ("c_trend", "项目 c_trend", "现有权重5", "raw"),
    ("c_breakout", "项目 c_breakout", "现有权重2", "raw"),
    ("c_vol15", "项目 c_vol15", "现有", "raw"),
    ("c_stairs", "项目 c_stairs", "现有", "raw"),
    ("c_v4up", "项目 c_v4up", "现有", "raw"),
    ("c_v24up", "项目 c_v24up", "现有权重1", "raw"),
    ("box_width_pct", "项目 box_width_pct", "现有", "raw"),
    ("signal_fired", "项目 signal_fired", "现有", "raw"),
    ("score", "项目 18 分总分", "现有", "raw"),
    ("ret_1d", "前 1 日涨幅", "混杂源", "raw"),
    ("ret_7d", "前 7 日涨幅", "混杂源", "raw"),
    ("ret_30d", "前 30 日涨幅", "混杂源", "raw"),
    ("dd_from_60d_high", "距 60 日高点回撤", "H34", "raw"),
    ("box_age_h", "项目自带 box_age_h", "现有", "raw"),
    ("rank_1d", "当日涨幅排名", "H12", "raw"),
    ("top10_gainer", "当日 Top10 涨幅榜", "H12", "raw"),
]


# ---------------------------------------------------------------- 评估


def load_substrate(limit: int | None = None) -> pd.DataFrame:
    hr = pd.read_csv(DATA / "hit_rate_current.csv")
    hr["pre_day"] = pd.to_datetime(hr["pre_day"], utc=True)
    fr = pd.read_csv(DATA / "forward_returns.csv")
    fr["pre_day"] = pd.to_datetime(fr["pre_day"], utc=True)
    keep = ["symbol", "pre_day", "entry_price"] + \
           [f"ret_net_{h}" for h in (1, 3, 5, 7, 14, 30)] + \
           [f"covered_{h}" for h in (7, 14, 30)] + ["mfe_7", "mae_7", "mfe_14", "mae_14"]
    keep = [c for c in keep if c in fr.columns]
    df = hr.merge(fr[keep], on=["symbol", "pre_day"], how="left", suffixes=("", "_fr"))
    df = df.sort_values("pre_day").reset_index(drop=True)
    if limit:
        df = df.tail(limit).reset_index(drop=True)
    return df


def context_vars(df: pd.DataFrame) -> pd.DataFrame:
    """不依赖 K 线的样本级上下文变量（币龄 / 距上次爆拉 / 日历）。"""
    pumps = json.loads((DATA / "pumps.json").read_text(encoding="utf-8"))
    ign: dict[str, list[pd.Timestamp]] = {}
    for p in pumps:
        ign.setdefault(p["symbol"], []).append(pd.Timestamp(p["ignite_date"], tz="UTC"))
    listing: dict[str, pd.Timestamp] = {}
    for p in D1.glob("*.parquet"):
        try:
            d = pd.read_parquet(p, columns=["open_time"])
            listing[p.stem] = pd.to_datetime(int(d["open_time"].min()), unit="ms", utc=True)
        except Exception:  # noqa: BLE001
            continue
    rows = []
    for _, r in df.iterrows():
        sym, d0 = r["symbol"], r["pre_day"]
        rec = {"symbol": sym, "pre_day": d0}
        lst = listing.get(sym)
        rec["days_since_listing"] = float((d0 - lst).days) if lst is not None else np.nan
        past = [t for t in ign.get(sym, []) if t < d0]
        rec["days_since_last_pump"] = float((d0 - max(past)).days) if past else np.nan
        rec["n_past_pumps_365"] = float(sum(1 for t in past if (d0 - t).days <= 365))
        rec["is_month_end"] = 1.0 if (d0 + pd.Timedelta(days=3)).month != d0.month else 0.0
        rows.append(rec)
    return pd.DataFrame(rows)


def load_btc() -> pd.DataFrame:
    frames = []
    for m in sorted(D1.glob("BTCUSDT.parquet")):
        d = pd.read_parquet(m)
        if "dt" not in d.columns:
            d["dt"] = pd.to_datetime(d["open_time"], unit="ms", utc=True)
        frames.append(d)
    # 日线做基准太粗，优先用 1h
    hf = []
    for p in sorted(H1.glob("BTCUSDT-*.parquet")):
        d = pd.read_parquet(p)
        if "dt" not in d.columns:
            d["dt"] = pd.to_datetime(d["open_time"], unit="ms", utc=True)
        hf.append(d[["dt", "close"]])
    if hf:
        out = pd.concat(hf).drop_duplicates("dt").sort_values("dt").reset_index(drop=True)
        return out
    out = pd.concat(frames).drop_duplicates("dt").sort_values("dt").reset_index(drop=True)
    return out[["dt", "close"]]


def compute_vars(df: pd.DataFrame, btc: pd.DataFrame, verbose: bool = True) -> pd.DataFrame:
    bench = Bench(btc)
    rows = []
    n_sym = df["symbol"].nunique()
    for i, (sym, g) in enumerate(df.groupby("symbol"), 1):
        if verbose and (i % 25 == 0 or i == n_sym):
            print(f"  [{i}/{n_sym}] {sym}", flush=True)
        months = set()
        for d in g["pre_day"]:
            months.update(months_before(d, 130))
        frames = [read_month(sym, m) for m in sorted(months)]
        frames = [f for f in frames if f is not None and len(f)]
        if not frames:
            continue
        big = pd.concat(frames, ignore_index=True).drop_duplicates("dt").sort_values("dt").reset_index(drop=True)
        for _, r in g.iterrows():
            cutoff = r["pre_day"] + pd.Timedelta(hours=23)
            b = big[big["dt"] < cutoff]
            if len(b) < 200:
                continue
            try:
                f = bench.bar_features(b)
            except Exception as e:  # noqa: BLE001
                print(f"    ! {sym} {r['pre_day'].date()}: {e}", flush=True)
                continue
            f["symbol"] = sym
            f["pre_day"] = r["pre_day"]
            rows.append(f)
    out = pd.DataFrame(rows)
    return out


def _stat_block(df, x, label_col="label"):
    """一次算出所有评估指标。"""
    y = df[label_col].values.astype(float)
    x = np.asarray(x, dtype=float)
    res = {"n": int(np.isfinite(x).sum()), "cov": float(np.isfinite(x).mean())}
    res["auc"] = _auc(y, x)
    res["r_ret1d"] = _spearman(x, df["ret_1d"].values)
    res["r_ret7d"] = _spearman(x, df["ret_7d"].values)
    res["r_score"] = _spearman(x, df["score"].values)

    ctrl = np.column_stack([
        df["ret_1d"].values.astype(float),
        df["ret_7d"].values.astype(float),
        df["score"].values.astype(float),
    ])
    for h in (PRIMARY_H, SECOND_H):
        col = f"ret_net_{h}"
        if col not in df.columns:
            continue
        yh = df[col].values.astype(float)
        res[f"ic{h}_all"] = _spearman(x, yh)
        res[f"pIC{h}_all"] = _partial_spearman(yh, x, ctrl)
        for lab in (0, 1):
            m = (y == lab)
            res[f"ic{h}_l{lab}"] = _spearman(x[m], yh[m])
            res[f"n{h}_l{lab}"] = int((m & np.isfinite(x) & np.isfinite(yh)).sum())
        # 五分位价差（爆拉组内）
        m1 = (y == 1) & np.isfinite(x) & np.isfinite(yh)
        if m1.sum() >= 100:
            q = pd.qcut(pd.Series(x[m1]).rank(method="first"), 5, labels=False)
            g = pd.Series(yh[m1]).groupby(q).median()
            res[f"q5spread{h}"] = float(g.iloc[-1] - g.iloc[0]) if len(g) == 5 else np.nan
            res[f"q5top{h}"] = float(g.iloc[-1]) if len(g) == 5 else np.nan
            res[f"q5bot{h}"] = float(g.iloc[0]) if len(g) == 5 else np.nan
    return res


def permutation_band(df, x, n_perm: int, seed: int = 42) -> dict:
    """打乱变量后重算，给出零分布的 5/50/95 分位。"""
    if n_perm <= 0:
        return {}
    rng = np.random.default_rng(seed)
    x = np.asarray(x, dtype=float)
    ok = np.isfinite(x)
    y = df["label"].values.astype(float)
    y7 = df["ret_net_7"].values.astype(float)
    m1 = (y == 1)
    ctrl = np.column_stack([
        df["ret_1d"].values.astype(float),
        df["ret_7d"].values.astype(float),
        df["score"].values.astype(float),
    ])
    a_auc, a_ic, a_pic = [], [], []
    base = x.copy()
    for _ in range(n_perm):
        xs = base.copy()
        vals = xs[ok].copy()
        rng.shuffle(vals)
        xs[ok] = vals
        a_auc.append(_auc(y, xs))
        a_ic.append(_spearman(xs, y7))
        a_pic.append(_partial_spearman(y7, xs, ctrl))
    out = {}
    for nm, arr in (("auc", a_auc), ("ic7_all", a_ic), ("pIC7_all", a_pic)):
        arr = np.array([v for v in arr if np.isfinite(v)])
        if len(arr) >= 20:
            out[f"{nm}_p05"] = float(np.percentile(arr, 5))
            out[f"{nm}_p50"] = float(np.percentile(arr, 50))
            out[f"{nm}_p95"] = float(np.percentile(arr, 95))
            out[f"{nm}_nullsd"] = float(np.std(arr))
    return out


def _col_from_vardf(df: pd.DataFrame, vardf: pd.DataFrame, vid: str) -> np.ndarray:
    """从变量表取一列对齐到 df 的行序。用临时列名避开与 df 的同名冲突。"""
    tmp = df[["symbol", "pre_day"]].merge(
        vardf[["symbol", "pre_day", vid]].rename(columns={vid: "__v"}),
        on=["symbol", "pre_day"], how="left")
    return tmp["__v"].values.astype(float)


def _col_for(df: pd.DataFrame, vardf: pd.DataFrame, vid: str, src: str | None) -> np.ndarray | None:
    if src == "raw":
        return df[vid].values.astype(float) if vid in df.columns else None
    if vid not in vardf.columns:
        return None
    return _col_from_vardf(df, vardf, vid)


def run_main(df: pd.DataFrame, vardf: pd.DataFrame, only: list[str] | None,
             n_perm: int) -> pd.DataFrame:
    rows = []
    todo = REGISTRY if not only else [r for r in REGISTRY if any(k in r[0] for k in only)]
    for vid, cname, hid, src in todo:
        x = _col_for(df, vardf, vid, src)
        if x is None:
            continue
        st = _stat_block(df, x)
        st.update({"var": vid, "name": cname, "hid": hid, "src": src or "bar"})
        if st["n"] >= MIN_N and n_perm > 0:
            st.update(permutation_band(df, x, n_perm))
        rows.append(st)
        print(f"  {vid:24s} n={st['n']:5d} auc={st.get('auc', float('nan')):.3f} "
              f"ic7={st.get('ic7_all', float('nan')):+.3f} "
              f"pIC7={st.get('pIC7_all', float('nan')):+.3f}", flush=True)
    return pd.DataFrame(rows)


def run_mod(df: pd.DataFrame, vardf: pd.DataFrame, only: list[str] | None,
            n_bins: int = 3) -> pd.DataFrame:
    """条件效应：按变量分箱后，看现有 score 的前瞻 IC 是否翻正。

    这是"加变量能不能救活策略"的直接检验——不是问新变量自己有没有 alpha，
    而是问它能不能把 score 变成有用的排序器。
    """
    rows = []
    todo = REGISTRY if not only else [r for r in REGISTRY if any(k in r[0] for k in only)]
    for vid, cname, hid, src in todo:
        x = _col_for(df, vardf, vid, src)
        if x is None:
            continue
        ok = np.isfinite(x)
        if ok.sum() < MIN_N * 2:
            continue
        try:
            bins = pd.qcut(pd.Series(x).rank(method="first"), n_bins, labels=False, duplicates="drop")
        except ValueError:
            continue
        y7 = df["ret_net_7"].values.astype(float)
        sc = df["score"].values.astype(float)
        lab = df["label"].values.astype(float)
        rec = {"var": vid, "name": cname, "hid": hid, "n": int(ok.sum())}
        for b in range(n_bins):
            m = ok & (bins.values == b)
            rec[f"n_b{b}"] = int(m.sum())
            rec[f"score_ic7_b{b}"] = _spearman(sc[m], y7[m])
            rec[f"score_auc_b{b}"] = _auc(lab[m], sc[m])
            # 分组内 IC：只有这两个数才是干净的条件效应
            for L in (0, 1):
                mm = m & (lab == L)
                rec[f"ic7_l{L}_b{b}"] = _spearman(sc[mm], y7[mm])
                rec[f"n_l{L}_b{b}"] = int(mm.sum())
            rec[f"pump_share_b{b}"] = float(np.nanmean(lab[m]))
        vals = [rec.get(f"score_ic7_b{b}") for b in range(n_bins)]
        vals = [v for v in vals if v is not None and np.isfinite(v)]
        if len(vals) >= 2:
            rec["ic7_spread"] = float(max(vals) - min(vals))
            rec["ic7_max"] = float(max(vals))
        rows.append(rec)
        print(f"  {vid:24s} " + " ".join(
            f"b{b}={rec.get(f'score_ic7_b{b}', float('nan')):+.3f}" for b in range(n_bins)), flush=True)
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- 报告


def md_table(df: pd.DataFrame, floatfmt: str = ".3f") -> str:
    """不依赖 tabulate 的 markdown 表格。"""
    if df is None or not len(df):
        return "（无数据）"
    cols = list(df.columns)
    head = "| " + " | ".join(str(c) for c in cols) + " |"
    sep = "|" + "|".join(["---"] * len(cols)) + "|"
    lines = [head, sep]
    for _, r in df.iterrows():
        cells = []
        for c in cols:
            v = r[c]
            if isinstance(v, float) or isinstance(v, np.floating):
                cells.append("—" if not np.isfinite(v) else format(v, floatfmt))
            elif isinstance(v, (int, np.integer)):
                cells.append(str(int(v)))
            else:
                cells.append(str(v))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def run_flip(df: pd.DataFrame, split: str = "2025-01-01") -> pd.DataFrame:
    """符号修正实验：在训练段按「爆拉组内 IC 的符号」重设 6 个条件的加分方向，
    再到测试段验证。全程只用训练段的符号，测试段完全不参与定符号。

    这是对「18 分加法器符号加反了」这一判断的直接检验。
    """
    conds = [
        ("c_trend", 5), ("oi_chg_1d", 4), ("oi_chg_3d", 3),
        ("ls_top", 3), ("c_breakout", 2), ("c_v24up", 1),
    ]
    for c, _ in conds:
        if c not in df.columns:
            raise SystemExit(f"缺少字段 {c}")
    cut = pd.Timestamp(split, tz="UTC")
    tr = df[df["pre_day"] < cut]
    te = df[df["pre_day"] >= cut]
    print(f"训练段 {len(tr)} 条（<{split}）｜测试段 {len(te)} 条", flush=True)

    signs, rows = {}, []
    for c, w in conds:
        ic_tr = _spearman(tr[c].values.astype(float), tr["ret_net_7"].values.astype(float))
        m1 = tr["label"].values == 1
        ic_tr_l1 = _spearman(tr[c].values.astype(float)[m1], tr["ret_net_7"].values.astype(float)[m1])
        sign = 1.0 if ic_tr_l1 >= 0 else -1.0
        signs[c] = sign
        rows.append({"条件": c, "原权重": w, "训练段IC(全体)": ic_tr,
                     "训练段IC(爆拉组)": ic_tr_l1, "修正符号": int(sign)})
    audit = pd.DataFrame(rows)

    def build(d):
        s = np.zeros(len(d))
        for c, w in conds:
            v = d[c].values.astype(float)
            s += w * np.where(np.isfinite(v), v, 0.0) * signs[c]
        return s

    out = []
    for nm, d in (("训练段", tr), ("测试段", te)):
        if not len(d):
            continue
        for h in (7, 14):
            yh = d[f"ret_net_{h}"].values.astype(float)
            lab = d["label"].values.astype(float)
            for label, s in (("原 score（18 分）", d["score"].values.astype(float)),
                             ("修正 score_v2", build(d))):
                rec = {"段": nm, "持有期": f"{h}d", "评分": label,
                       "n": int(np.isfinite(s).sum()),
                       "auc": _auc(lab, s), f"ic{h}_全体": _spearman(s, yh)}
                for L in (0, 1):
                    m = lab == L
                    rec[f"ic{h}_l{L}"] = _spearman(s[m], yh[m])
                out.append(rec)

    # 决定性问题：在真实候选池（score >= 入池线）里，两套排序谁更差
    pool = []
    for nm, d in (("训练段", tr), ("测试段", te)):
        for th in (5, 8):
            s = d[d["score"] >= th]
            if len(s) < 40:
                continue
            y7 = s["ret_net_7"].values.astype(float)
            y14 = s["ret_net_14"].values.astype(float)
            sc = s["score"].values.astype(float)
            v2 = build(s)
            pool.append({"段": nm, "入池线": f"score>={th}", "n": len(s),
                         "原score_IC7": _spearman(sc, y7), "原score_IC14": _spearman(sc, y14),
                         "v2_IC7": _spearman(v2, y7), "v2_IC14": _spearman(v2, y14),
                         "中位净收益7d": float(np.nanmedian(y7))})
    return audit, pd.DataFrame(out), pd.DataFrame(pool)


def run_poolrank(df: pd.DataFrame, vardf: pd.DataFrame, only: list[str] | None,
                 split: str = "2025-01-01", th: float = 5.0) -> pd.DataFrame:
    """在真实候选池（score >= th）里，逐个变量看它能不能替代 score 做排序。

    这是「谁来排序」的直接竞标：训练段挑，测试段验。挑的规则是训练段 IC7 > 0，
    测试段只用来验证，不参与挑选。
    """
    cut = pd.Timestamp(split, tz="UTC")
    pool = df[df["score"] >= th].copy()
    tr = pool[pool["pre_day"] < cut]
    te = pool[pool["pre_day"] >= cut]
    print(f"候选池 n={len(pool)}｜训练 {len(tr)}｜测试 {len(te)}", flush=True)
    if len(tr) < 80 or len(te) < 80:
        print("  池太小，跳过", flush=True)
        return pd.DataFrame()

    todo = REGISTRY if not only else [r for r in REGISTRY if any(k in r[0] for k in only)]
    rows = []
    for vid, cname, hid, src in todo:
        x = _col_for(pool, vardf, vid, src)
        if x is None:
            continue
        xtr = _col_for(tr, vardf, vid, src)
        xte = _col_for(te, vardf, vid, src)
        ytr = tr["ret_net_7"].values.astype(float)
        yte = te["ret_net_7"].values.astype(float)
        if np.isfinite(xtr).sum() < 60 or np.isfinite(xte).sum() < 60:
            continue
        rows.append({
            "var": vid, "name": cname, "hid": hid,
            "n_tr": int(np.isfinite(xtr).sum()), "n_te": int(np.isfinite(xte).sum()),
            "IC7_训练": _spearman(xtr, ytr), "IC7_测试": _spearman(xte, yte),
        })
    out = pd.DataFrame(rows)
    if len(out):
        out["同号"] = np.sign(out["IC7_训练"]) == np.sign(out["IC7_测试"])
        out = out.sort_values("IC7_测试", ascending=False)
    return out


RANK_KEYS_DEFAULT = [
    "score", "score_inv", "quote_vol_24h",
    "vol_dryup", "ats_4", "rv_ratio_24_168", "oi_chg_1d", "taker_ret_corr_7d",
]


def _key_series(df: pd.DataFrame, vardf: pd.DataFrame, key: str) -> np.ndarray | None:
    if key == "score_inv":
        return -df["score"].values.astype(float)
    if key == "score":
        return df["score"].values.astype(float)
    if key in df.columns:
        return df[key].values.astype(float)
    if key in vardf.columns:
        return _col_from_vardf(df, vardf, key)
    return None


def run_rankkey(df: pd.DataFrame, vardf: pd.DataFrame, split: str = "2025-01-01",
                th: float = 5.0, keys: list[str] | None = None,
                nq: int = 5) -> pd.DataFrame:
    """排序键对比：在真实候选池里，把池子按某个键分五档，看最高档是否跑赢「全池等权」。

    这是 A16 的判定实验。排序只在「只买一部分」时才有意义，所以基准是**全池等权**：
      - 若 Q5（键的最高档）跑不赢全池中位数 → 这个排序键在扣分
      - 若 Q5 与 Q1 的价差为负 → 按这个键排序是反向的
    """
    keys = keys or RANK_KEYS_DEFAULT
    cut = pd.Timestamp(split, tz="UTC")
    pool = df[df["score"] >= th].copy()
    segs = [("训练段", pool[pool["pre_day"] < cut]), ("测试段", pool[pool["pre_day"] >= cut])]
    rows = []
    for seg_name, seg in segs:
        if len(seg) < 100:
            continue
        base = {}
        for h in (7, 14):
            y = seg[f"ret_net_{h}"].values.astype(float)
            base[h] = float(np.nanmedian(y)) if np.isfinite(y).any() else np.nan
        rows.append({"段": seg_name, "排序键": "（全池等权基准）", "n": len(seg),
                     "IC7": np.nan, "IC14": np.nan,
                     "Q5中位7d": base[7], "Q1中位7d": base[7],
                     "Q5−Q1": 0.0, "Q5−全池": 0.0,
                     "Q5中位14d": base[14]})
        for k in keys:
            x = _key_series(seg, vardf, k)
            if x is None or np.isfinite(x).sum() < 80:
                continue
            try:
                q = pd.qcut(pd.Series(x).rank(method="first"), nq, labels=False, duplicates="drop")
            except ValueError:
                continue
            if q.nunique() < nq:
                continue
            rec = {"段": seg_name, "排序键": k, "n": int(np.isfinite(x).sum())}
            for h in (7, 14):
                y = seg[f"ret_net_{h}"].values.astype(float)
                g = pd.Series(y).groupby(q.values).median()
                rec[f"Q5中位{h}d"] = float(g.iloc[-1])
                rec[f"Q1中位{h}d"] = float(g.iloc[0])
                rec[f"Q5−Q1_{h}d"] = float(g.iloc[-1] - g.iloc[0])
                rec[f"Q5−全池_{h}d"] = float(g.iloc[-1] - base[h])
                rec[f"IC{h}"] = _spearman(x, y)
            rows.append(rec)
    return pd.DataFrame(rows)


def write_report(main: pd.DataFrame, mod: pd.DataFrame | None, n_perm: int,
                 out_dir: Path | None = None, label_note: str = ""):
    rdir = out_dir or REPORT_DIR
    rdir.mkdir(parents=True, exist_ok=True)
    p = rdir / "report.md"
    L: list[str] = []
    A = L.append
    A("# 变量试验台结果（ds4.1f）")
    A("")
    if label_note:
        A(f"> **标签口径**：{label_note}")
        A("")
    A(f"- 生成脚本：`research/14_variable_bench.py`｜明细：`data/variable_bench.csv`")
    A(f"- 零分布校准：每个变量打乱 {n_perm} 次｜判定门槛：有效样本 ≥ {MIN_N}")
    A(f"- 主判定期：{PRIMARY_H} 天（n 最大且不含标签污染）｜次判定期：{SECOND_H} 天")
    A("")
    A("## 读法")
    A("")
    A("| 列 | 含义 | 怎么用 |")
    A("|---|---|---|")
    A("| `auc` | 区分「会翻倍」vs「不会」 | >0.55 且超出零分布 95% 才算有 |")
    A("| `ic7_all` | 与未来 7 天净收益的相关 | 正=方向对 |")
    A("| `pIC7_all` | 扣掉 ret_1d/ret_7d/score 后的偏相关 | **这才是新增信息** |")
    A("| `ic7_l1` | 爆拉组内部 IC | 避免被正例比例污染 |")
    A("| `r_ret1d` | 与「前一日涨幅」的相关 | >0.5 = 只是动量换皮 |")
    A("| `q5spread7` | 爆拉组内五分位收益价差 | 排序能力 |")
    A("")
    A("**零分布带**：`auc_p05/p95`、`pIC7_p05/p95`。真实值落在带内 = 没有信息。")
    A("")

    if len(main):
        v = main.copy()
        v = v[v["n"] >= MIN_N]
        A("## 一、按「新增信息」排序（pIC7 降序）")
        A("")
        cols = ["var", "name", "hid", "n", "auc", "auc_p95", "ic7_all", "pIC7_all",
                "pIC7_all_p95", "ic7_l0", "ic7_l1", "q5spread7", "r_ret1d"]
        cols = [c for c in cols if c in v.columns]
        s = v.sort_values("pIC7_all", ascending=False)[cols].head(30)
        A(md_table(s))
        A("")
        A("## 二、按「区分度」排序（AUC 降序）")
        A("")
        s = v.sort_values("auc", ascending=False)[cols].head(20)
        A(md_table(s))
        A("")
        A("## 三、按「动量换皮」嫌疑排序（|r_ret1d| 降序）")
        A("")
        s = v.reindex(v["r_ret1d"].abs().sort_values(ascending=False).index)[cols].head(20)
        A(md_table(s))
        A("")
        # 通过三重门的变量
        gate = v[(v["n"] >= MIN_N)]
        if "auc_p95" in gate.columns:
            g1 = gate[gate["auc"] > gate["auc_p95"]]
            A(f"## 四、通过检验的变量")
            A("")
            A(f"- 有效样本 ≥{MIN_N} 的变量：**{len(gate)}** 个")
            A(f"- AUC 超出零分布 95% 分位：**{len(g1)}** 个")
            if len(g1):
                A("")
                A("```")
                for _, r in g1.sort_values("auc", ascending=False).iterrows():
                    A(f"{r['var']:24s} auc={r['auc']:.3f} (零分布95%={r['auc_p95']:.3f}) "
                      f"pIC7={r.get('pIC7_all', float('nan')):+.3f}")
                A("```")
            A("")
            g2 = gate[(gate["pIC7_all"] > gate["pIC7_all_p95"]) & (gate["r_ret1d"].abs() < 0.4)] \
                if "pIC7_all_p95" in gate.columns else gate.iloc[0:0]
            A(f"- **新增信息**（pIC7 超零分布 95% 且 |r_ret1d|<0.4）：**{len(g2)}** 个")
            if len(g2):
                A("")
                A("```")
                for _, r in g2.sort_values("pIC7_all", ascending=False).iterrows():
                    A(f"{r['var']:24s} pIC7={r['pIC7_all']:+.3f} (零分布95%={r['pIC7_all_p95']:+.3f}) "
                      f"auc={r['auc']:.3f} r_ret1d={r['r_ret1d']:+.3f}")
                A("```")
            A("")

    if mod is not None and len(mod):
        A("## 五、条件效应：哪个变量能让 score 的 IC 翻正")
        A("")
        A("`score_ic7_b*` = 在该变量分位区间内，现有 18 分总分与未来 7 天净收益的 Spearman（合并样本）。")
        A("**但合并样本的 IC 会被「该箱里爆拉样本占比」污染**，所以必须看分组内的两列：")
        A("`ic7_l0_b*`（对照组内）与 `ic7_l1_b*`（爆拉组内）。只有这两列同向为正，条件效应才算真的。")
        A("")
        cols = ["var", "name", "hid", "n", "score_ic7_b0", "score_ic7_b1", "score_ic7_b2",
                "ic7_l1_b0", "ic7_l1_b1", "ic7_l1_b2", "ic7_l0_b0", "ic7_l0_b1", "ic7_l0_b2"]
        cols = [c for c in cols if c in mod.columns]
        s = mod.copy()
        if "ic7_l1_b0" in s.columns:
            s["_k"] = s[["ic7_l1_b0", "ic7_l1_b1", "ic7_l1_b2"]].max(axis=1)
        else:
            s["_k"] = s.get("ic7_max", 0)
        A(md_table(s.sort_values("_k", ascending=False)[cols].head(25)))
        A("")
        if "ic7_l1_b0" in mod.columns:
            clean = mod[(mod[["ic7_l1_b0", "ic7_l1_b1", "ic7_l1_b2"]].max(axis=1) > 0)
                        & (mod[["ic7_l0_b0", "ic7_l0_b1", "ic7_l0_b2"]].max(axis=1) > 0)]
            A(f"- 两箱同时翻正的变量：**{len(clean)}** / {len(mod)}")
            A("")

    # ---------- 六、现有权重逐项审计 ----------
    WEIGHT_AUDIT = [
        ("c_trend", 5, "收盘 > EMA50"),
        ("oi_chg_1d", 4, "OI 1 日变化 ≥ 10%"),
        ("oi_chg_3d", 3, "OI 3 日变化 ≥ 20%"),
        ("ls_top", 3, "大户多空比 ≥ 1.0"),
        ("c_breakout", 2, "突破 24h 箱体"),
        ("c_v24up", 1, "24h 量增"),
        ("score", 0, "合计"),
    ]
    if len(main):
        A("## 六、现有 18 分权重的逐项审计")
        A("")
        A("`ic7_l1` = 爆拉组内「该条件与未来 7 天净收益」的相关。")
        A("**权重方向若与 ic7_l1 反号，说明这一分加反了。**")
        A("")
        rows = []
        for vid, w, desc in WEIGHT_AUDIT:
            r = main[main["var"] == vid]
            if not len(r):
                continue
            r = r.iloc[0]
            rows.append({
                "条件": vid, "权重": w, "定义": desc, "n": int(r["n"]),
                "auc": r["auc"], "ic7_l0": r["ic7_l0"], "ic7_l1": r["ic7_l1"],
                "pIC7": r["pIC7_all"], "pIC7_零95%": r.get("pIC7_all_p95", np.nan),
                "方向": "一致" if (r["ic7_l1"] > 0) else "反向",
            })
        A(md_table(pd.DataFrame(rows)))
        A("")

    # ---------- 七、一致型新增信息 ----------
    if len(main) and "pIC7_all_p95" in main.columns:
        v = main[(main["n"] >= MIN_N) & (main["pIC7_all"] > main["pIC7_all_p95"])].copy()
        v = v[v["r_ret1d"].abs() < 0.4]
        cons = v[(v["ic7_l0"] > 0) & (v["ic7_l1"] > 0)]
        A("## 七、一致型新增信息（两组 IC 同时为正）")
        A("")
        A(f"- 通过「新增信息」检验：**{len(v)}** 个（含期望 {int(0.05 * len(main[main['n'] >= MIN_N]))} 个假阳性）")
        A(f"- 其中两组 IC 同时为正（方向稳定）：**{len(cons)}** 个")
        A("")
        if len(cons):
            cols = ["var", "name", "hid", "n", "auc", "ic7_l0", "ic7_l1",
                    "pIC7_all", "pIC7_all_p95", "r_ret1d"]
            cols = [c for c in cols if c in cons.columns]
            A(md_table(cons.sort_values("pIC7_all", ascending=False)[cols]))
            A("")

    p.write_text("\n".join(L), encoding="utf-8")
    print(f"\n报告已写入 {p}")
    return p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--perm", type=int, default=200, help="零分布打乱次数，0=关闭")
    ap.add_argument("--only", type=str, default="", help="只测匹配的变量名（逗号分隔）")
    ap.add_argument("--limit", type=int, default=0, help="只取最近 N 条样本（冒烟用）")
    ap.add_argument("--mod", action="store_true", help="条件效应模式")
    ap.add_argument("--flip", action="store_true", help="符号修正实验（训练/测试分段）")
    ap.add_argument("--poolrank", action="store_true", help="候选池内排序竞标")
    ap.add_argument("--rankkey", action="store_true", help="排序键对比（A16 判定实验）")
    ap.add_argument("--keys", type=str, default="", help="排序键候选，逗号分隔")
    ap.add_argument("--pool-th", type=float, default=5.0, help="候选池入池分数线")
    ap.add_argument("--split", type=str, default="2025-01-01", help="训练/测试分界日")
    ap.add_argument("--bins", type=int, default=3)
    ap.add_argument("--reuse", action="store_true", help="复用已缓存的 data/vars_cache.parquet")
    ap.add_argument("--label-clean", action="store_true",
                    help="用 A17 可交易性清洗后的标签替换 label（正例只留「买得进拿得住」）")
    ap.add_argument("--label-file", type=str, default="",
                    help="清洗标签 parquet，默认 reports/tradability_ds/tradable_labels.parquet")
    ap.add_argument("--tag", type=str, default="", help="输出文件后缀，用于区分不同标签口径")
    args = ap.parse_args()

    only = [s.strip() for s in args.only.split(",") if s.strip()] or None
    cache = DATA / "vars_cache.parquet"
    tag = args.tag or ("clean" if args.label_clean else "")
    sfx = f"_{tag}" if tag else ""
    out_csv = DATA / f"variable_bench{sfx}.csv"
    report_dir = REPORT_DIR if not tag else ROOT / "reports" / f"variable_bench_ds_{tag}"

    df = load_substrate(args.limit or None)
    print(f"样本 {len(df)} 条（label=1: {int(df.label.sum())}）", flush=True)

    if args.label_clean:
        lp = Path(args.label_file) if args.label_file else \
            ROOT / "reports" / "tradability_ds" / "tradable_labels.parquet"
        if not lp.exists():
            raise SystemExit(f"找不到清洗标签 {lp}；先跑 research/15_tradability.py --labels")
        lab = pd.read_parquet(lp)
        lab["pre_day"] = pd.to_datetime(lab["pre_day"], utc=True)
        df = df.merge(lab[["symbol", "pre_day", "tradable"]], on=["symbol", "pre_day"], how="left")
        # 正例里没有清洗结论的（1h 缺失）→ 剔除，不把「不知道」当成「不可交易」
        unknown = (df["label"] == 1) & df["tradable"].isna()
        print(f"正例中 1h 缺失、无法判定可交易性 → 剔除 {int(unknown.sum())} 条", flush=True)
        df = df[~unknown].reset_index(drop=True)
        df["label_raw"] = df["label"].astype(int)
        df["label"] = np.where(df["label"] == 1, df["tradable"].fillna(0).astype(int), 0).astype(int)
        n_raw, n_new = int(df["label_raw"].sum()), int(df["label"].sum())
        print(f"清洗后 label=1: {n_new}（原 {n_raw}，去掉 {n_raw - n_new} 个不可交易正例）", flush=True)

    if args.reuse and cache.exists() and not args.limit:
        vardf = pd.read_parquet(cache)
        vardf["pre_day"] = pd.to_datetime(vardf["pre_day"], utc=True)
        print(f"复用缓存 {len(vardf)} 条", flush=True)
    else:
        btc = load_btc()
        print(f"BTC 基准 {len(btc)} 根", flush=True)
        vardf = compute_vars(df, btc)
        # 统一成 float，避免 parquet 写出 object 列
        for c in vardf.columns:
            if c not in ("symbol", "pre_day"):
                vardf[c] = pd.to_numeric(vardf[c], errors="coerce")
        print(f"算出变量 {len(vardf)} 条 × {len(vardf.columns)} 列", flush=True)

    # 样本级上下文变量（币龄 / 距上次爆拉 / 日历）每次重算，成本极低
    ctx = context_vars(df)
    dup = [c for c in ctx.columns if c in vardf.columns and c not in ("symbol", "pre_day")]
    vardf = vardf.drop(columns=dup, errors="ignore")
    vardf = vardf.merge(ctx, on=["symbol", "pre_day"], how="left")
    # 清洗标签模式下行数会变少，不能覆盖全量缓存
    if not args.limit and not args.label_clean:
        vardf.to_parquet(cache, index=False)

    main_df = run_main(df, vardf, only, args.perm)
    main_df.to_csv(out_csv, index=False, encoding="utf-8-sig")
    print(f"\n明细已写入 {out_csv}")

    mod_df = run_mod(df, vardf, only, args.bins) if args.mod else None
    note = ""
    if args.label_clean:
        note = ("A17 可交易性清洗。正例只保留「买得进 + 拿得住」的真赢家，"
                "不可交易正例并入负例；1h 缺失、无法判定的正例已剔除。"
                f"清洗后 label=1 共 {int(df['label'].sum())} 条 / 全样本 {len(df)} 条"
                f"（清洗前正例 {int(df['label_raw'].sum())} 条）。")
    write_report(main_df, mod_df, args.perm, out_dir=report_dir, label_note=note)

    if args.flip:
        audit, res, pool = run_flip(df, args.split)
        print("\n=== 符号修正：训练段定符号 ===")
        print(md_table(audit))
        print("\n=== 训练段 / 测试段 对比 ===")
        print(md_table(res))
        print("\n=== 决定性问题：真实候选池内谁更差 ===")
        print(md_table(pool))
        ap_ = report_dir / f"flip_test_{args.split}.md"
        ap_.write_text(
            "# 符号修正实验（ds4.1f）\n\n"
            f"- 训练段：`pre_day` < {args.split}｜测试段：≥ {args.split}\n"
            "- 符号只在训练段决定（用爆拉组内 IC 的符号），测试段完全不参与\n\n"
            "## 一、训练段定出的符号\n\n" + md_table(audit) +
            "\n\n## 二、原 score vs 修正 score_v2（全体样本，按持有期）\n\n" + md_table(res) +
            "\n\n## 三、决定性问题：在真实候选池（`score >= 入池线`）里谁更差\n\n"
            "候选池才是策略实际会买的东西。若 v2 在池内比原版更差，"
            "说明「翻符号」是结构性事实，不是可交易的修正。\n\n" + md_table(pool) + "\n",
            encoding="utf-8")
        print(f"\n已写入 {ap_}")

    if args.poolrank:
        pr = run_poolrank(df, vardf, only, args.split, args.pool_th)
        if len(pr):
            print(f"\n=== 候选池内排序竞标（score>={args.pool_th}）===")
            print(md_table(pr.head(20)))
            bp = report_dir / f"poolrank_{args.split}.md"
            bp.write_text(
                "# 候选池内排序竞标（ds4.1f）\n\n"
                f"- 候选池：`score >= {args.pool_th}`（策略实际会买的东西）\n"
                f"- 训练段 < {args.split}｜测试段 ≥ {args.split}\n"
                "- `IC7_训练` / `IC7_测试`：该变量与未来 7 天净收益的 Spearman\n"
                "- `同号`：两段符号一致 = 方向稳定\n\n"
                "## 全部变量（按测试段 IC7 降序）\n\n" + md_table(pr) +
                f"\n\n## 汇总\n\n"
                f"- 变量数：{len(pr)}\n"
                f"- 两段同号且测试段 > 0：**{int(((pr['同号']) & (pr['IC7_测试'] > 0)).sum())}** 个\n"
                f"- 两段同号且测试段 < 0：**{int(((pr['同号']) & (pr['IC7_测试'] < 0)).sum())}** 个\n",
                encoding="utf-8")
            print(f"\n已写入 {bp}")

    if args.rankkey:
        keys = [s.strip() for s in args.keys.split(",") if s.strip()] or None
        rk = run_rankkey(df, vardf, args.split, args.pool_th, keys)
        if len(rk):
            print(f"\n=== 排序键对比（候选池 score>={args.pool_th}）===")
            cols = ["段", "排序键", "n", "IC7", "Q1中位7d", "Q5中位7d", "Q5−Q1_7d",
                    "Q5−全池_7d", "Q5−全池_14d"]
            cols = [c for c in cols if c in rk.columns]
            print(md_table(rk[cols]))
            bp = report_dir / f"rankkey_{args.split}{'_keys%d' % len(keys) if args.keys else ''}.md"
            bp.write_text(
                "# 排序键对比（ds4.1f · A16 判定实验）\n\n"
                f"- 候选池：`score >= {args.pool_th}`（策略实际会买的东西）\n"
                f"- 训练段 < {args.split}｜测试段 ≥ {args.split}\n"
                "- 每档 = 按该键把池子分五档，`Q5` 是键的最高档（会被优先买的那批）\n"
                "- **基准 = 全池等权中位数**。排序只在「只买一部分」时有意义\n\n"
                "## 判定规则\n\n"
                "| 现象 | 含义 |\n|---|---|\n"
                "| `Q5−全池` < 0 | 这个键把差的东西排到了前面，**在扣分** |\n"
                "| `Q5−Q1` < 0 | 排序方向是反的 |\n"
                "| `Q5−全池` > 0 且 `IC7` > 0 | 这个键值得当排序量 |\n\n"
                "## 结果\n\n" + md_table(rk[cols]) + "\n",
                encoding="utf-8")
            print(f"\n已写入 {bp}")


if __name__ == "__main__":
    main()
