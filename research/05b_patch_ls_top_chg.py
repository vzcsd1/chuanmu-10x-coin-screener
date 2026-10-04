#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""05b · 从 metrics 缓存补出「大户持仓多空比 3 日变化」

为什么单独一个脚本
    05_deriv_features.py 的 funding 部分需要串行下载数百个月文件，重跑一次
    要十几分钟；而本次只需要新增 ls_top_chg_3d 一列（把回测口径对齐实盘
    fetch_deriv_context 的 ls_chg_3d）。这里直接读 data/metrics_cache.parquet
    补列，秒级完成。
    05 脚本本身也已加入该列，未来全量重跑结果一致。

用法
    py -3.10 research/05b_patch_ls_top_chg.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def main() -> int:
    cache = pd.read_parquet(DATA / "metrics_cache.parquet")
    need = {"symbol", "day", "ls_top_last"}
    missing = need - set(cache.columns)
    if missing:
        print(f"[05b] 缓存缺少列：{sorted(missing)}")
        return 1
    lookup = {(str(r.symbol), str(r.day)): r.ls_top_last
              for r in cache[["symbol", "day", "ls_top_last"]].itertuples()}
    print(f"[05b] metrics 缓存条目 {len(lookup):,}")

    d = pd.read_csv(DATA / "features_deriv.csv")

    def chg(row) -> float:
        cur = lookup.get((str(row.symbol), str(row.pre_day)))
        prev_day = str((pd.Timestamp(row.pre_day) - pd.Timedelta(days=3)).date())
        prev = lookup.get((str(row.symbol), prev_day))
        if cur is None or prev is None or pd.isna(cur) or pd.isna(prev):
            return np.nan
        return float(cur) - float(prev)

    d["ls_top_chg_3d"] = d.apply(chg, axis=1)
    d.to_csv(DATA / "features_deriv.csv", index=False, encoding="utf-8-sig")

    ok = int(d["ls_top_chg_3d"].notna().sum())
    print(f"[05b] ls_top_chg_3d 覆盖 {ok}/{len(d)} ({ok / len(d) * 100:.1f}%)")

    # 立刻验一下这一列的区分度，决定 1 分权重是否站得住
    sub = d[d["ls_top_chg_3d"].notna()]
    pump, ctrl = sub[sub["label"] == 1], sub[sub["label"] == 0]
    hit_p = (pump["ls_top_chg_3d"] < 0).mean()
    hit_c = (ctrl["ls_top_chg_3d"] < 0).mean()
    prec = (pump["ls_top_chg_3d"] < 0).sum() / max(1, int((sub["ls_top_chg_3d"] < 0).sum()))
    base = len(pump) / len(sub)
    print(f"[05b] 大户多空比3日下降：爆拉命中 {hit_p * 100:.1f}% | 对照命中 {hit_c * 100:.1f}% "
          f"| 区分度 {(hit_p - hit_c) * 100:+.1f}pp | 提升 {prec / base:.2f}x")
    print(f"[05b] 爆拉组中位变化 {pump['ls_top_chg_3d'].median():+.3f} | "
          f"对照组中位 {ctrl['ls_top_chg_3d'].median():+.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
