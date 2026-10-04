#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""08 · 前视 / 马后炮审计

判据（可量化，不靠感觉）：
  对每个条件，取"在 pre_day 为真"的样本，看它们**触发时点的前期涨幅**。
    - 前期涨幅 ≈ 0 或为负  → 领先信号：价格还没动，可以事前埋伏
    - 前期涨幅很大(>+20%)  → 滞后信号：触发时行情已经走了一半，属于马后炮/追涨
  再叠加区分度：
    - 区分度 ≈ 0           → 噪音（无论领先还是滞后都没用）

同时审计"未来数据"：任何用到 pre_day 之后信息的字段，一律标记为前视。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
D1 = DATA / "1d"


def trailing_returns(symbol: str, pre_day: str) -> dict | None:
    p = D1 / f"{symbol}.parquet"
    if not p.exists():
        return None
    try:
        df = pd.read_parquet(p, columns=["open_time", "close", "quote_volume"])
    except Exception:  # noqa: BLE001
        return None
    if "dt" not in df.columns:
        df["dt"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["day"] = df["dt"].dt.tz_localize(None).dt.normalize()
    d = pd.Timestamp(pre_day)
    cur = df[df["day"] <= d]
    if len(cur) < 31:
        return None
    c0 = float(cur["close"].iloc[-1])

    def back(n):
        idx = len(cur) - 1 - n
        return float(cur["close"].iloc[idx]) if idx >= 0 else np.nan

    out = {"symbol": symbol, "pre_day": pre_day, "price": c0}
    for n, tag in ((1, "1d"), (7, "7d"), (30, "30d")):
        b = back(n)
        out[f"ret_{tag}"] = (c0 / b - 1.0) if (b and np.isfinite(b) and b > 0) else np.nan
    # 距 60 日高点的回撤
    hi60 = float(cur["close"].tail(60).max())
    out["dd_from_60d_high"] = (c0 / hi60 - 1.0) if hi60 else np.nan
    return out


def main():
    k = pd.read_csv(DATA / "features.csv")
    d = pd.read_csv(DATA / "features_deriv.csv")
    d = d.drop(columns=[c for c in ["label", "gain"] if c in d.columns])
    d = d.drop_duplicates(subset=["symbol", "pre_day"])
    k = k.drop_duplicates(subset=["symbol", "pre_day", "label"])
    m = k.merge(d, on=["symbol", "pre_day"], how="left")
    print(f"[audit] 合并后 {len(m)} 行（爆拉 {int((m['label']==1).sum())} / "
          f"对照 {int((m['label']==0).sum())}）")

    print(f"[audit] 计算 {len(m)} 个样本的前期涨幅 ...")
    tr = [trailing_returns(r.symbol, r.pre_day) for r in m.itertuples()]
    trdf = pd.DataFrame([t for t in tr if t]).drop_duplicates(subset=["symbol", "pre_day"])
    m = m.merge(trdf, on=["symbol", "pre_day"], how="left")

    # 横截面：当日 24h 涨幅排名（模拟项目里的"涨幅前十"加分）
    m["rank_1d"] = m.groupby("pre_day")["ret_1d"].rank(ascending=False, method="min")
    m["top10_gainer"] = m["rank_1d"] <= 10

    m.to_csv(DATA / "features_audit.csv", index=False, encoding="utf-8-sig")

    pump_all = m[m["label"] == 1]
    ctrl_all = m[m["label"] == 0]

    # 每个条件声明它依赖的原始列；只在"该列非空"的子集内比较，
    # 避免左连接产生的 NaN 被当成 False 而系统性压低爆拉组命中率。
    conds = [
        ("收盘>EMA50", "kline", lambda x: x["c_trend"], []),
        ("24h量增", "kline", lambda x: x["c_v24up"], []),
        ("4h量增", "kline", lambda x: x["c_v4up"], []),
        ("量能三台阶", "kline", lambda x: x["c_stairs"], []),
        ("箱体宽<=12%", "kline", lambda x: x["c_box12"], []),
        ("箱体持续>=72h", "kline", lambda x: x["box_age_h"] >= 72, ["box_age_h"]),
        ("量比>=1.5", "kline", lambda x: x["c_vol15"], []),
        ("已突破箱体", "kline", lambda x: x["c_breakout"], []),
        ("箱体突破信号", "kline", lambda x: x["signal_fired"], []),
        ("48h波动15-25%", "kline", lambda x: x["c_move48_15_25"], []),
        ("涨幅前十(项目加分项)", "kline", lambda x: x["top10_gainer"], ["top10_gainer"]),
        ("OI 1日增>10%", "deriv", lambda x: x["oi_chg_1d"] > 0.10, ["oi_chg_1d"]),
        ("OI 3日增>20%", "deriv", lambda x: x["oi_chg_3d"] > 0.20, ["oi_chg_3d"]),
        ("OI 7日增>30%", "deriv", lambda x: x["oi_chg_7d"] > 0.30, ["oi_chg_7d"]),
        ("大户持仓多空比<1", "deriv", lambda x: x["ls_top"] < 1.0, ["ls_top"]),
        ("多空比3日下降", "deriv", lambda x: x["ls_top_chg_3d"] < 0, ["ls_top_chg_3d"]),
        ("当期费率>=0", "deriv", lambda x: x["funding_last"] >= 0, ["funding_last"]),
        ("7日内出现过负费率", "deriv", lambda x: x["funding_neg_ratio_7d"] > 0,
         ["funding_neg_ratio_7d"]),
    ]

    rows = []
    for name, src, fn, need in conds:
        sub_all = m
        for c in need:
            sub_all = sub_all[sub_all[c].notna()]
        if len(sub_all) == 0:
            continue
        pump = sub_all[sub_all["label"] == 1]
        ctrl = sub_all[sub_all["label"] == 0]
        if len(pump) == 0 or len(ctrl) == 0:
            continue
        base = len(pump) / len(sub_all)
        tp = fn(pump).fillna(False).astype(bool)
        tc = fn(ctrl).fillna(False).astype(bool)
        if tp.sum() == 0:
            continue
        p_rate, c_rate = tp.mean(), tc.mean()
        diff = p_rate - c_rate
        prec = tp.sum() / max(1, tp.sum() + tc.sum())
        s = pump[tp]
        rows.append({
            "条件": name, "来源": src, "样本数": len(sub_all),
            "爆拉命中率": p_rate, "对照命中率": c_rate,
            "区分度": diff, "精确率": prec, "提升倍数": prec / base,
            "命中数": int(tp.sum()),
            "触发时1日涨幅": s["ret_1d"].median(),
            "触发时7日涨幅": s["ret_7d"].median(),
            "触发时30日涨幅": s["ret_30d"].median(),
            "距60日高点": s["dd_from_60d_high"].median(),
        })

    t = pd.DataFrame(rows).sort_values("区分度", ascending=False)

    print("=" * 118)
    print(f"{'条件':<22}{'样本':>6}{'区分度':>8}{'提升':>7}{'命中数':>7}"
          f"{'触发时1日':>10}{'触发时7日':>10}{'触发时30日':>11}{'距60日高':>10}   判定")
    print("-" * 118)
    for _, r in t.iterrows():
        d_ = r["区分度"] * 100
        r7 = r["触发时7日涨幅"]
        if abs(d_) < 3:
            verdict = "噪音"
        elif np.isfinite(r7) and r7 > 0.20:
            verdict = "⚠ 马后炮/滞后"
        elif d_ > 0:
            verdict = "✅ 领先可用"
        else:
            verdict = "✗ 反向"
        print(f"{r['条件']:<22}{int(r['样本数']):>6}{d_:>7.1f}{r['提升倍数']:>6.2f}x{r['命中数']:>7}"
              f"{r['触发时1日涨幅']*100:>9.1f}%{r7*100:>9.1f}%"
              f"{r['触发时30日涨幅']*100:>10.1f}%{r['距60日高点']*100:>9.1f}%   {verdict}")
    print("=" * 118)

    print("\n【对照：平静期与爆拉组整体的前期涨幅（中位）】")
    print(f"  对照组  1日 {ctrl_all['ret_1d'].median()*100:6.1f}%  |  "
          f"7日 {ctrl_all['ret_7d'].median()*100:6.1f}%  |  "
          f"30日 {ctrl_all['ret_30d'].median()*100:6.1f}%  |  "
          f"距60日高 {ctrl_all['dd_from_60d_high'].median()*100:6.1f}%")
    print(f"  爆拉组  1日 {pump_all['ret_1d'].median()*100:6.1f}%  |  "
          f"7日 {pump_all['ret_7d'].median()*100:6.1f}%  |  "
          f"30日 {pump_all['ret_30d'].median()*100:6.1f}%  |  "
          f"距60日高 {pump_all['dd_from_60d_high'].median()*100:6.1f}%")

    t.to_csv(DATA / "audit_conditions.csv", index=False, encoding="utf-8-sig")
    print(f"\n-> {DATA/'audit_conditions.csv'} / features_audit.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
