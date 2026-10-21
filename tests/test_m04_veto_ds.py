#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""M04 杠杆超载否决（A21）的单元测试。

覆盖四条，都是「错了会让实盘静默变差」的点：
1. 关闭时什么都不做（默认必须无副作用）
2. 超过阈值才否决，等于阈值不否决
3. OI 或成交额缺失时，pass 放行 / drop 否决
4. 分母为 0 或负数时不能炸，也不能算出 inf 去误杀
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import binance_box_strategy as base  # noqa: E402


def _row(sym: str, oi, qv) -> dict:
    return {"symbol": sym, "score": 6, "open_interest_value": oi, "spot_quote_volume": qv}


class TestM04Veto(unittest.TestCase):
    def setUp(self) -> None:
        self.on = base.Config(m04_veto=True, m04_veto_ref=3.2)
        self.off = base.Config(m04_veto=False, m04_veto_ref=3.2)

    def test_disabled_is_noop(self):
        rows = [_row("AAAUSDT", 1e8, 1e6)]     # 比值 100，远超阈值
        kept, vetoed = base.apply_m04_veto(rows, self.off)
        self.assertEqual(len(kept), 1)
        self.assertEqual(vetoed, [])
        self.assertNotIn("m04_oi_qv", kept[0])   # 关闭时不该写任何字段

    def test_threshold_is_strict_greater(self):
        hit, _ = base.m04_veto_decision(_row("A", 3.2, 1.0), self.on)
        self.assertFalse(hit, "等于阈值不应否决")
        hit, why = base.m04_veto_decision(_row("B", 3.21, 1.0), self.on)
        self.assertTrue(hit)
        self.assertIn("3.21", why)

    def test_missing_oi_passes_by_default(self):
        hit, _ = base.m04_veto_decision(_row("A", None, 1e6), self.on)
        self.assertFalse(hit, "默认 fail-open：无合约的币爆拉率最高，不能剔")
        drop = base.Config(m04_veto=True, m04_veto_missing="drop")
        hit, why = base.m04_veto_decision(_row("A", None, 1e6), drop)
        self.assertTrue(hit)
        self.assertIn("未知", why)

    def test_zero_and_negative_volume_do_not_crash(self):
        for qv in (0, -1.0, float("nan")):
            hit, _ = base.m04_veto_decision(_row("A", 1e8, qv), self.on)
            self.assertFalse(hit, f"qv={qv} 应放行而不是误杀")

    def test_ratio_recorded_when_evaluable(self):
        row = _row("A", 2.0, 1.0)
        base.m04_veto_decision(row, self.on)
        self.assertAlmostEqual(row["m04_oi_qv"], 2.0)

    def test_apply_keeps_order(self):
        rows = [_row("A", 1.0, 1.0), _row("B", 9.0, 1.0), _row("C", 2.0, 1.0)]
        kept, vetoed = base.apply_m04_veto(rows, self.on)
        self.assertEqual([r["symbol"] for r in kept], ["A", "C"])
        self.assertEqual([r["symbol"] for r in vetoed], ["B"])
        self.assertIn("veto_reason", vetoed[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
