"""历史双向研究工具测试（ds，2026-10-09）。

重点：
  · 向量化评估器与已验收接入 `b4_replay_ds.score_timepoint` **逐字段等价**
    （含 kline 状态/分、deriv 状态/分、总分、selected、screening_status；lag=0/1）
  · 事件库不依赖评分、未来窗口不含本根盘中高点、未成熟不判失败
  · 分母/未知：not_observable ≠ 未入选；未评分记 unknown 而非 0
  · 输出隔离：只写 reports/history_research_ds/

运行：
  py -3.10 -B -m unittest discover -s tests -p "test_history_research_ds.py" -v
"""
from __future__ import annotations

import json
import random
import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import b4_replay_ds as R          # noqa: E402
import history_research_ds as H   # noqa: E402


class TestKlineEquivalence(unittest.TestCase):
    """K 线侧：向量化 vs 验收函数。"""

    @classmethod
    def setUpClass(cls):
        cls.symbol = "AAVEUSDT"
        cls.months = [f"2024-{m:02d}" for m in range(1, 7)]
        cls.candles = R.read_spot_1h(cls.symbol, cls.months)
        cls.candles_arr = H.read_spot(cls.symbol, cls.months)
        if len(cls.candles) < 500:
            raise unittest.SkipTest("本地缺少 AAVE 2024 上半年 1h 归档")

    def _compare(self, decision_ms_list, lag=0, mdf=None, has_fut=False):
        candles = self.candles_arr
        d = np.asarray(decision_ms_list, dtype=np.int64)
        fast = H.evaluate(self.symbol, candles,
                          np.empty(0, dtype=np.int64), np.empty(0), np.empty(0),
                          has_fut, d, lag_hours=lag) if mdf is None else H.evaluate(
            self.symbol, candles, *self._metric_arrays(mdf), has_fut, d, lag_hours=lag)
        bad = []
        for k, ms in enumerate(d):
            rec = R.score_timepoint(self.symbol, int(ms), self.candles, mdf, has_fut,
                                    deriv_lag_hours=lag)
            ks = int(fast["score_kline"][k])
            rs = rec["score_kline"] if rec["score_kline"] is not None else -1
            ts = int(fast["score_total"][k])
            rt = rec["score_total"] if rec["score_total"] is not None else -1
            if (ks != rs or ts != rt or bool(fast["selected"][k]) != rec["selected"]
                    or int(fast["score_deriv"][k]) != rec["score_deriv"]
                    or H.STATUS_NAMES[int(fast["status"][k])] != rec["screening_status"]):
                bad.append((rec["decision_time_utc"], ks, rs, ts, rt,
                            H.STATUS_NAMES[int(fast["status"][k])], rec["screening_status"]))
        return bad

    @staticmethod
    def _metric_arrays(mdf):
        dt = mdf["dt"].to_numpy().astype("datetime64[ns]").view("int64") // 10 ** 6
        keep = (mdf["dt"].dt.minute == 0).to_numpy()
        oi = mdf["sum_open_interest_value"].to_numpy(dtype=float)
        ls = mdf["sum_toptrader_long_short_ratio"].to_numpy(dtype=float)
        return dt[keep], oi[keep], ls[keep]

    def test_kline_only_matches_verifier(self):
        start = R.parse_utc("2024-04-01T00:00:00Z")
        d = [start + k * R.INTERVAL_MS for k in range(0, 240)]
        bad = self._compare(d)
        self.assertEqual(bad, [], f"K 线侧出现 {len(bad)} 处不一致")

    def test_kline_random_hours_match(self):
        lo = int(self.candles_arr[300, 0]); hi = int(self.candles_arr[-1, 0])
        random.seed(20261009)
        d = sorted(random.sample(range(lo, hi, R.INTERVAL_MS), 150))
        bad = self._compare(d)
        self.assertEqual(bad, [], f"随机时点出现 {len(bad)} 处不一致")

    def test_deriv_and_total_match_verifier(self):
        days = [str(x.date()) for x in
                __import__("pandas").date_range("2024-01-01", "2024-06-30", freq="D")]
        mdf = R.read_metrics(self.symbol, days)
        if mdf is None:
            self.skipTest("本地缺少 AAVE metrics")
        start = R.parse_utc("2024-04-01T00:00:00Z")
        d = [start + k * R.INTERVAL_MS for k in range(0, 200)]
        bad = self._compare(d, mdf=mdf, has_fut=True)
        self.assertEqual(bad, [], f"含合约侧出现 {len(bad)} 处不一致")

    def test_deriv_lag1_matches_verifier(self):
        days = [str(x.date()) for x in
                __import__("pandas").date_range("2024-01-01", "2024-06-30", freq="D")]
        mdf = R.read_metrics(self.symbol, days)
        if mdf is None:
            self.skipTest("本地缺少 AAVE metrics")
        start = R.parse_utc("2024-04-01T00:00:00Z")
        d = [start + k * R.INTERVAL_MS for k in range(0, 200)]
        bad = self._compare(d, lag=1, mdf=mdf, has_fut=True)
        self.assertEqual(bad, [], f"lag=1 出现 {len(bad)} 处不一致")


class TestStatusSemantics(unittest.TestCase):
    """分母/未知/未评分语义。"""

    def test_not_observable_is_not_below_threshold(self):
        self.assertNotEqual(H.ST_NOT_OBSERVABLE, H.ST_BELOW)
        self.assertIn("not_observable", H.STATUS_NAMES[H.ST_NOT_OBSERVABLE])
        self.assertIn("skipped", H.STATUS_NAMES[H.ST_SKIP_NODATA])

    def test_unscored_is_minus_one_not_zero(self):
        # 空输入 → 全部不可评分（-1），不是 0 分
        res = H.evaluate("XUSDT", np.empty((0, 6)), np.empty(0, dtype=np.int64),
                         np.empty(0), np.empty(0), False,
                         np.asarray([H.MAIN_START_MS], dtype=np.int64))
        self.assertEqual(int(res["score_kline"][0]), -1)
        self.assertEqual(int(res["score_total"][0]), -1)
        self.assertFalse(bool(res["selected"][0]))
        self.assertEqual(int(res["status"][0]), H.ST_SKIP_NODATA)

    def test_short_history_marked(self):
        # 只有 10 根 K 线 → 预热不足，不评分
        c = np.asarray([[H.MAIN_START_MS - (10 - i) * R.INTERVAL_MS, 1, 1, 1, 1, 1]
                        for i in range(10)], dtype=np.float64)
        d = np.asarray([H.MAIN_START_MS], dtype=np.int64)
        res = H.evaluate("XUSDT", c, np.empty(0, dtype=np.int64), np.empty(0), np.empty(0),
                         False, d)
        self.assertEqual(int(res["status"][0]), H.ST_SKIP_SHORT)
        self.assertEqual(int(res["score_kline"][0]), -1)


class TestEventLibrary(unittest.TestCase):
    """事件库：独立于评分、未来窗口不含本根盘中高点、未成熟不判失败。"""

    @staticmethod
    def _mk(prices, start_ms=H.MAIN_START_MS):
        rows = []
        for i, p in enumerate(prices):
            rows.append([start_ms + i * R.INTERVAL_MS, p, p, p, p, 1.0])
        return np.asarray(rows, dtype=np.float64)

    def test_past_high_not_counted_as_future(self):
        # 第 0 根盘中 250，其余 100 → 不应识别为 2 倍事件
        c = self._mk([100.0] * 800)
        c[0, 2] = 250.0                       # 本根盘中高点
        evs = H.detect_events(c)
        self.assertEqual(evs, [], "本根盘中高点被误算成未来涨幅")

    def test_real_rise_is_detected(self):
        prices = [100.0] * 300 + [250.0] * 300
        c = self._mk(prices)
        evs = H.detect_events(c)
        self.assertGreaterEqual(len(evs), 1)
        self.assertGreaterEqual(evs[0]["max_m30"], 2.0)
        self.assertIn(evs[0]["tier"], (2.0, 2.5, 3.0, 5.0, 10.0))

    def test_immature_tail_not_failure(self):
        # 上涨发生在数据末端 → 30 天窗未走满 → mature_30d False，但仍识别
        prices = [100.0] * 300 + [250.0] * 200
        c = self._mk(prices)
        evs = H.detect_events(c)
        self.assertTrue(evs)
        self.assertFalse(evs[0]["mature_30d"])
        self.assertFalse(evs[0]["mature_90d"])

    def test_merge_gap_48h(self):
        # 两个「低位起点」相隔 10 小时（<=48h）→ 必须合并为一个事件
        prices = [100.0] * 100 + [300.0] * 10 + [100.0] * 10 + [300.0] * 300
        c = self._mk(prices)
        evs = H.detect_events(c)
        self.assertEqual(len(evs), 1, "间隔 <=48h 的两段未合并")
        # n_segments 必须是「本事件内」的分段数（2），不是该币全部分段数
        self.assertEqual(evs[0]["n_segments"], 2)
        self.assertEqual(evs[0]["span_hours"], 120)   # 0..119 合并后跨度

    def test_gap_over_48h_not_merged(self):
        # 两个低位起点相隔 200 小时（>48h）→ 保持为两个独立事件
        prices = [100.0] * 100 + [300.0] * 200 + [100.0] * 10 + [300.0] * 300
        c = self._mk(prices)
        evs = H.detect_events(c)
        self.assertEqual(len(evs), 2, "间隔 >48h 的两段不应合并")
        self.assertEqual([e["n_segments"] for e in evs], [1, 1])


class TestForwardPath(unittest.TestCase):
    def test_forward_path_uses_after_reference(self):
        prices = [100.0] * 100 + [400.0] * 800
        c = TestEventLibrary._mk(prices)
        fp = H.forward_path(c, 99)          # 参考根 = 100 的最后一根
        self.assertAlmostEqual(fp["30d"][0], 4.0, places=3)
        self.assertEqual(fp["30d"][3], 1)   # 参考根之后还有 800 根 → 30 天窗走满
        fp0 = H.forward_path(c, 100)        # 参考根本身已是 400
        self.assertAlmostEqual(fp0["30d"][0], 1.0, places=3)
        # 靠近数据末端 → 前向窗未走满，不得当作失败统计
        tail = H.forward_path(c, 899)
        self.assertEqual(tail["30d"][3], 0)
        self.assertIsNone(tail["30d"][0])
        # 参考根本身不计入前向窗口（第 100 根已涨到 400，但窗口从第 101 根起）
        self.assertAlmostEqual(fp["7d"][0], 4.0, places=3)

    def test_quote_volume_approx(self):
        c = TestEventLibrary._mk([2.0] * 30)
        c[:, 5] = 1000.0
        self.assertAlmostEqual(H.quote_volume_24h(c, 29), 2.0 * 1000.0 * 24, places=3)


class TestRankDenominator(unittest.TestCase):
    """名次分母：不可评分不占名次；并列按 symbol 字典序升序。"""

    def test_ties_break_by_symbol_ascending(self):
        total = np.asarray([[5, 5, 3]], dtype=np.int8)
        rk = H.rank_matrix(total)
        self.assertEqual(int(rk[0, 0]), 1)
        self.assertEqual(int(rk[0, 1]), 2)
        self.assertEqual(int(rk[0, 2]), 3)

    def test_unscored_excluded_from_denominator(self):
        total = np.asarray([[5, -1, 3]], dtype=np.int8)
        rk = H.rank_matrix(total)
        self.assertEqual(int(rk[0, 1]), 32767)   # 不可评分 → 不占名次
        self.assertEqual(int(rk[0, 0]), 1)
        self.assertEqual(int(rk[0, 2]), 2)

    def test_random_rank_is_fixed_seed(self):
        total = np.asarray([[5, -1, 3], [1, 2, 3]], dtype=np.int8)
        a = H.random_rank_matrix(20261009, total)
        b = H.random_rank_matrix(20261009, total)
        np.testing.assert_array_equal(a, b)
        # 不可评分列仍是 32767，不参与随机名次
        self.assertEqual(int(a[0, 1]), 32767)


class TestForwardFromSeries(unittest.TestCase):
    """信号后果：参考价口径、未来隔离、成熟度、等待时间、路径事实。"""

    REF0 = H.MAIN_START_MS

    @staticmethod
    def _mk5(prices, start_ms, step_min=5, high_of=None):
        step = step_min * 60_000
        rows = []
        for i, p in enumerate(prices):
            hi = p if high_of is None else high_of[i]
            rows.append([start_ms + i * step, p, hi, p, p, 1.0])
        return np.asarray(rows, dtype=np.float64)

    def test_ref_price_first_open_at_or_after_signal(self):
        start = self.REF0
        c = self._mk5([100.0] * 100, start)
        sig = start + 2 * 5 * 60_000 + 30_000          # 落在第 2 根 5m 之内
        fp = H.forward_from_series(c, sig)
        self.assertEqual(fp["ref_price"], 100.0)
        self.assertEqual(fp["ref_open_utc"], R.ms_to_iso(start + 3 * 5 * 60_000))
        self.assertGreaterEqual(fp["ref_lag_min"], 0)
        self.assertLess(fp["ref_lag_min"], 5)          # 最多等到下一根 5m 开盘

    def test_exact_boundary_uses_that_bar(self):
        start = self.REF0
        c = self._mk5([100.0] * 100, start)
        sig = start + 3 * 5 * 60_000                    # 正好落在第 3 根开盘
        fp = H.forward_from_series(c, sig)
        self.assertEqual(fp["ref_lag_min"], 0)
        self.assertEqual(fp["ref_open_utc"], R.ms_to_iso(sig))

    def test_future_excludes_reference_bar_high(self):
        start = self.REF0
        high = [100.0] * 210
        high[10] = 250.0                                # 参考根本身盘中 250
        c = self._mk5([100.0] * 210, start, high_of=high)
        sig = start + 10 * 5 * 60_000
        fp = H.forward_from_series(c, sig)
        self.assertEqual(fp["ref_price"], 100.0)
        # 参考根自己的 250 不算未来 → 7 天窗内最高触及仍是 1.0
        self.assertAlmostEqual(fp["7d"]["max_touch"], 1.0, places=3)

    def test_no_lookahead_ref_price_independent_of_future(self):
        start = self.REF0
        c1 = self._mk5([100.0] * 50 + [1000.0] * 50, start)
        c2 = self._mk5([100.0] * 50 + [100.0] * 50, start)
        sig = start + 45 * 5 * 60_000                   # 落在两序列都还是 100 的区间
        self.assertEqual(H.forward_from_series(c1, sig)["ref_price"], 100.0)
        self.assertEqual(H.forward_from_series(c1, sig)["ref_price"],
                         H.forward_from_series(c2, sig)["ref_price"])
        # 未来确实不同 → 最高触及不同（证明未来只进「后果」，不进「参考价」）
        self.assertGreater(H.forward_from_series(c1, sig)["7d"]["max_touch"],
                           H.forward_from_series(c2, sig)["7d"]["max_touch"])

    def test_immature_tail_not_failure(self):
        start = self.REF0
        c = self._mk5([100.0] * 10 + [300.0] * 20, start)   # 序列很短
        sig = start + 10 * 5 * 60_000
        fp = H.forward_from_series(c, sig)
        self.assertFalse(fp["30d"]["mature"])
        self.assertEqual(fp["dim"]["maturity"], "immature")

    def test_wait_hours_to_2x(self):
        start = self.REF0
        prices = [100.0] * 10 + [100.0] * 12 + [200.0] * 100
        c = self._mk5(prices, start)
        sig = start + 10 * 5 * 60_000
        fp = H.forward_from_series(c, sig)
        self.assertAlmostEqual(fp["waits"]["wait_hours_to_2.0x"], 1.0, places=3)
        self.assertEqual(fp["dim"]["gain"], "reach_2x")

    def test_dd_before_2x(self):
        start = self.REF0
        prices = [100.0] * 10 + [100.0] * 6 + [50.0] * 6 + [200.0] * 200
        c = self._mk5(prices, start)
        sig = start + 10 * 5 * 60_000
        fp = H.forward_from_series(c, sig)
        self.assertIsNotNone(fp["dd_before_2x"])
        self.assertLessEqual(fp["dd_before_2x"], H.FWD_DD_THRESHOLD)
        self.assertEqual(fp["dim"]["path"], "reach_2x_after_dd")

    def test_empty_series_none(self):
        self.assertIsNone(H.forward_from_series(np.empty((0, 6)), self.REF0))

    def test_ref_beyond_series_none(self):
        start = self.REF0
        c = self._mk5([100.0] * 5, start)
        self.assertIsNone(H.forward_from_series(c, start + 10 * 5 * 60_000))

    def test_granularity_kept_separate(self):
        rows = [
            {"dim": {"gain": "reach_2x", "path": "reach_2x_no_dd", "data_state": "complete",
                     "maturity": "mature"},
             "granularity": "5m", "start_utc": "2023-01-01T00:00:00Z",
             "score_total_max": 8, "has_futures": True, "waits": {"wait_hours_to_2.0x": 5.0},
             "30d": {"max_touch": 2.5, "max_dd": -0.1}},
            {"dim": {"gain": "below_1_5x", "path": "no_2x_shallow", "data_state": "complete",
                     "maturity": "mature"},
             "granularity": "1h", "start_utc": "2023-01-01T00:00:00Z",
             "score_total_max": 6, "has_futures": False, "waits": {"wait_hours_to_2.0x": None},
             "30d": {"max_touch": 1.2, "max_dd": -0.05}},
        ]
        s = H.summarize_forward(rows)
        self.assertEqual(set(s["ref_granularity"]), {"5m", "1h"})
        self.assertEqual(s["by_granularity"]["5m"]["n"], 1)
        self.assertEqual(s["by_granularity"]["1h"]["n"], 1)
        self.assertEqual(s["outcome"]["reach_2x"], 1)
        self.assertEqual(s["outcome"]["below_1_5x"], 1)

    def test_no_reference_row_reconciles(self):
        # 片段之后归档再无 K 线 → no_reference，不静默丢弃，且各去向之和 = 片段总数
        row = {
            "dim": {"gain": "no_reference", "path": "no_reference",
                    "data_state": "no_reference", "maturity": "unknown"},
            "granularity": "none", "start_utc": "2023-01-01T00:00:00Z",
            "score_total_max": 7, "has_futures": False,
            "waits": {"wait_hours_to_1.5x": None, "wait_hours_to_2.0x": None},
            "30d": {"max_touch": None, "max_dd": None},
        }
        s = H.summarize_forward([row])
        self.assertTrue(s["outcome_reconciles"])
        self.assertEqual(s["outcome"]["no_reference"], 1)
        self.assertEqual(sum(s["outcome"].values()), s["n_episodes"])

    def test_forward_dir_isolated_by_lag(self):
        self.assertNotEqual(H.forward_dir(0).resolve(), H.forward_dir(1).resolve())
        self.assertTrue(str(H.forward_dir(1)).endswith("lag1"))
        self.assertTrue(str(H.forward_dir(0).resolve()).startswith(str(H.OUT.resolve())))


class TestRetentionScale(unittest.TestCase):
    """保留刻度：分母反算、边界、够不到如实报告。"""

    def test_monotone_and_endpoints(self):
        covered = {n: min(n, 50) for n in range(1, 61)}
        scale = H.retention_scale(covered, base_n=50, max_rank=60)
        self.assertEqual(scale["100%"]["target_events"], 50)
        self.assertEqual(scale["100%"]["required_n"], 50)
        self.assertEqual(scale["80%"]["target_events"], 40)
        self.assertEqual(scale["80%"]["required_n"], 40)
        ns = [scale[k]["required_n"] for k in ("100%", "95%", "90%", "80%")]
        self.assertEqual(ns, sorted(ns, reverse=True))

    def test_unreachable_flagged(self):
        covered = {n: 10 for n in range(1, 21)}     # 无论取多少都只覆盖 10
        scale = H.retention_scale(covered, base_n=50, max_rank=20)
        self.assertTrue(scale["100%"]["unreachable_within_max_rank"])
        self.assertIsNone(scale["100%"]["required_n"])

    def test_zero_base_events_does_not_crash(self):
        scale = H.retention_scale({}, base_n=0, max_rank=10)
        self.assertEqual(scale["100%"]["target_events"], 0)

    def test_exact_threshold_boundary(self):
        covered = {n: (0 if n < 9 else 9) for n in range(1, 11)}
        scale = H.retention_scale(covered, base_n=10, max_rank=10, pcts=(90,))
        self.assertEqual(scale["90%"]["target_events"], 9)
        self.assertEqual(scale["90%"]["required_n"], 9)     # 恰好够到，不取 10


class TestOutputIsolation(unittest.TestCase):
    def test_outputs_confined(self):
        p = H.OUT.resolve()
        self.assertTrue(str(p).endswith("history_research_ds"), str(p))
        self.assertIn("reports", str(p))
        for d in (H.SHARDS, H.EV_DIR, H.SIG_DIR):
            self.assertTrue(str(d.resolve()).startswith(str(p)), str(d))

    def test_lag_shards_isolated(self):
        # lag=0 与 lag=1 必须落在不同目录，否则 lag=1 会静默复用 lag=0 结果
        self.assertNotEqual(H.shards_dir(0).resolve(), H.shards_dir(1).resolve())
        self.assertNotEqual(H.signals_dir(0).resolve(), H.signals_dir(1).resolve())
        self.assertTrue(str(H.shards_dir(1)).endswith("lag1"))
        self.assertTrue(str(H.shards_dir(0)).endswith("lag0"))
        for d in (H.shards_dir(0), H.shards_dir(1)):
            self.assertTrue(str(d.resolve()).startswith(str(H.OUT.resolve())))

    def test_main_range_and_grid(self):
        self.assertEqual(int(H.N_HOURS), 41616)
        self.assertEqual(H.MAIN_START, "2022-01-01T00:00:00Z")
        self.assertEqual(H.MAIN_END, "2026-09-30T23:00:00Z")


if __name__ == "__main__":
    unittest.main(verbosity=2)
