"""川沐十倍币筛选 · 精简版（输出 JSON，便于二次处理）

评分口径与 binance_box_strategy.py 完全一致——本文件不再自带一套条件判断，
而是直接调用 base.public_selection，确保权重只有一处定义（base.WEIGHTS）。

本次修订要点（依据 841 个历史爆拉事件的实测 + 样本外验证）：
  · 删除「24h 涨幅前十」加分 —— 实测区分度 −19.3pp、提升 0.91x，比随机还差
  · 删除「48h 波动 15–25%」条件 —— 实测区分度 +1.0pp，纯噪音
  · 「箱体宽 <=12%」「量比 >=1.5」「4h 量增」「量能三台阶」「资金费率 >=0」
    不再计分，仅作展示（前两者样本外方向翻转）
  · 「收盘 > EMA50」权重提高到 5（唯一强条件，区分度 +35.3pp）
  · 新增合约侧维度：OI 1 日增 >10%、OI 3 日增 >20%、大户持仓多空比 <1、多空比 3 日下降
"""
from __future__ import annotations

import json
import logging
import sys

import binance_box_strategy as base


def scan(cfg):
    """跑一轮扫描，返回达到 min_score 的标的（已按分数降序）。"""
    spot, futures, cfg = base.connect_exchanges(cfg)
    okx = None
    try:
        okx = base.okx_exchange(cfg)
    except Exception as exc:  # noqa: BLE001
        logging.warning("OKX 客户端不可用: %s", exc)
    ranked = base.public_selection(spot, futures, cfg, okx)
    return [row for row in ranked if row["score"] >= cfg.min_score]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = base.env_config()
    rows = scan(cfg)
    print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
    sys.exit(0)
