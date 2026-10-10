"""历史双向研究 · 有限返修 r4 测试（ds，2026-10-10）。

把主代理验收报告 `reports/history_research_review/summary.md` 与
`reports/history_research_review/evidence_20261010.json` 给出的
**F1 / F2 缺陷**与 **P1–P10 反例**固化为回归测试。

覆盖：
  · F1：有效 OI 观测 < 25 时不得产生涨幅与得分；24/25 边界；LS 独立计分；lag0/lag1
  · F2：排序前先排除不可评分列，有效名次连续 1..k；总分/趋势/固定随机种子都查
  · R3：7/30/90 天窗口与 48h 合波按真实时刻；`peak_ms` 指向真实未来高点
  · R2：参考根同根高低价计入未来；窗口按真实时刻截止；四维分开；回撤不给正值；
        达标前下跌含入场点；上下界；资料不全/迟到参考价
  · R4：分数组按**首个信号当时**的分数；事后最高分只作描述
  · C1：特征时钟对齐决策小时；未来根不得改变过去特征；回看窗口缺口给 NaN
  · C2：30/90 天按真实结果窗口分组，跨界项从探索统计中真正排除
  · C3：直接后果标签（从当时参考价起的真实 30 天）与旁证标签分名分列；
        未到期/缺口/无参考价不判为负例；排序与分母统一
  · C4：人工负担按小时底账统计，日内重入不漏；2/3/6h 节奏由同一底账派生
  · C6：合并前真实连续段数（两波间隔 <48h → 1 事件 2 段）
  · C7：复用前核对评分核指纹（`scoring_core_fingerprint`）
  · C8：主范围 `2022-01-01 → 2026-09-30 23:00 UTC`、41,616 小时
  · 输出隔离：只写 reports/history_research_ds/r4/（本批新计算写 r4/closeout/）
  · 复用版本守卫：被复用的 v3 模块哈希不符即拒绝运行

运行：
  py -3.10 -B -m unittest discover -s tests -p "test_history_research_ds_r4.py" -v
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import b4_replay_ds as R           # noqa: E402
import history_research_ds_r4 as X  # noqa: E402

HR = X.INTERVAL_MS
T0 = X.MAIN_START_MS


def candles(prices, hours=None):
    """与主代理复核脚本同构造：high=low=close=price, open=price, vol=100。"""
    if hours is None:
        hours = np.arange(len(prices))
    return np.asarray([[T0 + h * HR, p, p, p, p, 100.0]
                       for p, h in zip(prices, hours)], dtype=float)


def _obs(d, ks, values):
    dt = np.asarray([d - int(k * HR) for k in ks], dtype=np.int64)
    order = np.argsort(dt)
    return dt[order], np.asarray(values, dtype=float)[order]


class TestF1DerivContract(unittest.TestCase):
    """F1：合约侧评分必须复现 `R.deriv_as_of` 的逐字段契约。"""

    def _run(self, n_obs, with_ls=True, lag=0):
        d = T0 + 100 * HR
        # 保证 d-72h、d-24h 附近与 d 都有观测，再用小时观测补足到 n_obs
        ks = [72.0] + [float(k) for k in range(22, 0, -1)] + [0.0]
        extra = 0.5
        while len(ks) < n_obs:
            ks.append(extra)
            extra += 1.0
        ks = ks[:n_obs]
        vals = [100.0] * len(ks)
        vals[ks.index(0.0)] = 150.0
        dt, oi = _obs(d, ks, vals)
        ls = np.full(len(ks), 0.8) if with_ls else np.full(len(ks), np.nan)
        return X.deriv_scores_r4(dt, oi, ls, np.array([d]), lag_hours=lag)

    def test_sparse_oi_three_observations(self):
        """主代理反例 F1_sparse_oi：只有 3 个观测时只给大户持仓比那 3 分。"""
        d = T0 + 100 * HR
        dt = np.array([d - 72 * HR, d - 24 * HR, d], dtype=np.int64)
        oi = np.array([100.0, 100.0, 150.0])
        ls = np.full(3, 0.8)
        out = X.deriv_scores_r4(dt, oi, ls, np.array([d]))
        self.assertEqual(int(out["points"][0]), 3)
        self.assertEqual(X.DERIV_STATUS_NAMES[int(out["status"][0])], "partial")
        self.assertTrue(bool(out["oi_gated"][0]))
        self.assertTrue(np.isnan(out["oi_chg_1d"][0]))
        self.assertTrue(np.isnan(out["oi_chg_3d"][0]))

    def test_boundary_24_vs_25(self):
        """24 个有效观测 → 不产生 OI 分；25 个 → 正常产生。"""
        a = self._run(24)
        self.assertEqual(int(a["oi_cnt"][0]), 24)
        self.assertEqual(int(a["points"][0]), 3)          # 只剩 LS 的 3 分
        self.assertTrue(bool(a["oi_gated"][0]))
        b = self._run(25)
        self.assertEqual(int(b["oi_cnt"][0]), 25)
        self.assertEqual(int(b["points"][0]), 10)         # 4 + 3 + 3
        self.assertFalse(bool(b["oi_gated"][0]))

    def test_ls_scored_independently(self):
        """只有 LS、没有 OI：LS 仍独立计分（不是「partial 全清零」）。"""
        out = self._run(25, with_ls=True)
        self.assertTrue(np.isfinite(out["ls_top"][0]))
        # 只有 LS 可用（构造 OI 不足）时，LS 分照给
        a = self._run(24, with_ls=True)
        self.assertEqual(int(a["points"][0]), 3)
        self.assertTrue(np.isfinite(a["ls_top"][0]))

    def test_missing_other_field_only(self):
        """观测够 25 但缺 LS：只算 OI 两项，状态 partial，不把 OI 分也清零。"""
        out = self._run(25, with_ls=False)
        self.assertEqual(int(out["points"][0]), 7)
        self.assertEqual(X.DERIV_STATUS_NAMES[int(out["status"][0])], "partial")

    def test_lag_hours(self):
        """lag=1 时可得截止提前 1 小时，参考观测不得晚于 cutoff。"""
        out0 = self._run(25, lag=0)
        out1 = self._run(25, lag=1)
        d = T0 + 100 * HR
        self.assertLessEqual(int(out0["oi_as_of"][0]), d)
        self.assertLessEqual(int(out1["oi_as_of"][0]), d - HR)


class TestF2Rank(unittest.TestCase):
    """F2：排序必须先排除不可评分列。"""

    def setUp(self):
        self.total = np.array([[0, -1, 0]], dtype=np.int16)

    def test_expected_valid_ranks(self):
        """evidence.F2_rank_masks：有效列名次应为连续的 1、2。"""
        trend = X._rank_rows_r4(np.zeros_like(self.total), self.total >= 0)
        rand = X.random_rank_matrix_r4(20261014, self.total)
        score = X.rank_matrix_r4(self.total)
        for rk in (trend, rand, score):
            valid = sorted(int(v) for v in rk[0] if v != 32767)
            self.assertEqual(valid, [1, 2])
            self.assertEqual(int(rk[0][1]), 32767)

    def test_ties_break_by_column_order(self):
        """并列按列序（symbol 字典序升序）：三列同分时名次应为 1、2、3。"""
        total = np.array([[5, 5, 5]], dtype=np.int16)
        rk = X.rank_matrix_r4(total)
        self.assertEqual([int(v) for v in rk[0]], [1, 2, 3])

    def test_random_seed_reproducible(self):
        total = np.array([[3, -1, 3, 3]], dtype=np.int16)
        a = X.random_rank_matrix_r4(20261009, total)
        b = X.random_rank_matrix_r4(20261009, total)
        self.assertTrue(np.array_equal(a, b))
        valid = sorted(int(v) for v in a[0] if v != 32767)
        self.assertEqual(valid, [1, 2, 3])


class TestEventTimeR4(unittest.TestCase):
    """R3：事件窗口按真实时刻，`peak_ms` 指向真实未来高点。"""

    def test_p8_window_is_time_not_index(self):
        """P8：1,540 小时后的高点不能算进 30 天窗。"""
        c = candles([100.0] * 900)
        c[50:, 0] += 60 * 24 * HR          # 中间空一大段
        c[100, 2] = 250
        ev = X.detect_events_r4(c)
        self.assertEqual(len(ev), 1)
        e = ev[0]
        self.assertGreater(e["start_ms"], c[0, 0] + 60 * 24 * HR)
        self.assertTrue(e["mature_30d"])
        self.assertTrue(e["complete_30d"])
        self.assertEqual(e["missing_bars_30d"], 0)

    def test_p9_peak_is_real_high_bar(self):
        """P9：`peak_ms` 必须指向真实高点那根，而不是参考根。"""
        c = candles([100.0] * 1500)
        c[1000, 2] = 250
        e = X.detect_events_r4(c)[0]
        self.assertEqual(int(e["peak_ms"]), int(c[1000, 0]))
        self.assertNotEqual(int(e["peak_ms"]), int(e["start_ms"]))

    def test_merge_gap_by_time(self):
        """合波按真实时间：间隔恰好 48 小时不拆、超过 48 小时才拆。"""
        def run(p2):
            c = candles([100.0] * 2000)
            c[1000, 2] = 250
            c[p2, 2] = 250
            return X.detect_events_r4(c)
        # 两波相隔恰好 48h → 合并为 1 个事件
        self.assertEqual(len(run(1767)), 1)
        # 再远 1 小时（49h）→ 拆成 2 个事件
        self.assertEqual(len(run(1768)), 2)


class TestForwardR4(unittest.TestCase):
    """R2：参考价、真实时刻截止、四维分开、回撤与路径上下界。"""

    def test_p1_reference_bar_touch(self):
        """P1：参考根**同根**盘中高点计入未来 → 2.5。"""
        c = candles([100.0, 100.0, 100.0], hours=[0, 1 / 12, 2 / 12])
        c[0, 2] = 250
        self.assertEqual(X.forward_from_series_r4(c, T0, HR)["30d"]["max_touch"], 2.5)

    def test_p2_drawdown(self):
        """P2：收盘回撤 -0.25；从入场点算的最低跌幅不得为正。"""
        c = candles([100.0, 200.0, 150.0], hours=[0, 1 / 12, 2 / 12])
        w = X.forward_from_series_r4(c, T0, HR)["30d"]
        self.assertEqual(w["max_close_drawdown"], -0.25)
        self.assertEqual(w["max_dd_from_entry"], 0.0)

    def test_p3_entry_included(self):
        """P3：达标前下跌必须含入场点 → -0.2。"""
        c = candles([100.0, 80.0, 200.0], hours=[0, 1 / 12, 2 / 12])
        self.assertEqual(X.forward_from_series_r4(c, T0, HR)["dd_before_2x"], -0.2)

    def test_p4_uncertain_bar(self):
        """P4：首次达标根内部先后不明 → 确定部分 0、保守下界 -0.4444、标记 uncertain。"""
        c = candles([100.0, 180.0, 100.0], hours=[0, 1 / 12, 2 / 12])
        c[2, 1] = 180
        c[2, 2] = 220
        fp = X.forward_from_series_r4(c, T0, HR)
        self.assertEqual(fp["dd_before_2x"], 0.0)
        self.assertEqual(fp["dd_before_2x_worst"], -0.4444)
        self.assertTrue(fp["dd_before_2x_uncertain"])

    def test_p5_internal_gap_is_partial(self):
        """P5：内部缺口 → data_state = partial，missing_bars 如实记录。"""
        c = candles([100.0] * 4, hours=[0, 1, 719, 720])
        w = X.forward_from_series_r4(c, T0, HR)["30d"]
        self.assertEqual(w["data_state"], "partial")
        self.assertEqual(w["missing_bars"], 717)

    def test_p6_delayed_reference(self):
        """P6：参考价迟到 6,000 分钟必须显式标记，不能当正常。"""
        c = candles([100.0] * 3, hours=[100, 101, 821])
        fp = X.forward_from_series_r4(c, T0, HR)
        self.assertEqual(fp["ref_lag_min"], 6000)
        self.assertEqual(fp["ref_skipped_bars"], 100)
        self.assertTrue(fp["ref_delayed"])
        self.assertEqual(fp["30d"]["data_state"], "partial")

    def test_p7_immature_not_failure(self):
        """P7：未到期未达标 → undetermined，不是「30 天失败」。"""
        c = candles([100.0, 100.0], hours=[0, 1 / 12])
        dim = X.forward_from_series_r4(c, T0, HR)["dim"]
        self.assertEqual(dim["verdict"], X.VERDICT_UNDETERMINED)
        self.assertFalse(dim["elapsed"])
        self.assertFalse(dim["final_eligible"])

    def test_p10_horizon_end_bar_excluded(self):
        """P10：截止时刻才开盘的一根不计入 → 1.0。"""
        c = candles([100.0, 100.0, 100.0], hours=[0, 719, 720])
        c[-1, 2] = 250
        self.assertEqual(X.forward_from_series_r4(c, T0, HR)["30d"]["max_touch"], 1.0)

    def test_reached_2x_kept_when_immature(self):
        """已达 2× 是已发生事实，与是否到期无关，必须保留。"""
        c = candles([100.0, 210.0], hours=[0, 1 / 12])
        fp = X.forward_from_series_r4(c, T0, HR)
        self.assertEqual(fp["dim"]["verdict"], "reach_2x")
        self.assertFalse(fp["dim"]["final_eligible"])

    def test_no_reference(self):
        """参考根之后归档再无 K 线 → 返回 None（片段记 no_reference）。"""
        c = candles([100.0, 100.0], hours=[0, 1 / 12])
        self.assertIsNone(X.forward_from_series_r4(c, T0 + 500 * HR, HR))


class TestR4Grouping(unittest.TestCase):
    """R4：分数组按首个信号当时的分数；事后最高分只作描述。"""

    def _row(self, first_score, max_score):
        return {
            "symbol": "AAAUSDT", "start_ms": T0 + 100 * HR, "granularity": "5m",
            "n_hours": 3, "first_score": first_score, "score_total_max": max_score,
            "deriv_status": "ok", "ref_price": 1.0, "ref_open_utc": "2022-01-05T04:00:00Z",
            "ref_lag_min": 0, "ref_skipped_bars": 0, "ref_delayed": False,
            "dd_before_2x": None, "dd_before_2x_worst": None, "dd_before_2x_uncertain": False,
            "waits": {"wait_hours_to_1.5x": None, "wait_hours_to_2.0x": None},
            "dim": {"data_state": "complete", "elapsed": True, "final_eligible": True,
                    "gain_observed": "below_1_5x", "verdict": "below_1_5x",
                    "path": "no_2x_shallow"},
            "7d": {}, "30d": {}, "90d": {},
        }

    def test_grouping_uses_first_score(self):
        rows = [self._row(6, 14), self._row(12, 12)]
        s = X.summarize_forward_r4(rows)
        self.assertEqual(s["by_first_score"]["first_score>=5"]["n"], 2)
        self.assertEqual(s["by_first_score"]["first_score>=10"]["n"], 1)
        # 事后最高分分组：两条都 ≥12
        self.assertEqual(s["by_max_score_expost_only"]["score_total_max>=12"]["n"], 2)
        self.assertEqual(s["n_episodes"], 2)

    def test_undetermined_excluded_from_failure_ratio(self):
        rows = [self._row(6, 6), self._row(6, 6)]
        rows[1]["dim"].update({"verdict": X.VERDICT_UNDETERMINED, "final_eligible": False,
                               "gain_observed": "below_1_5x", "elapsed": False})
        s = X.summarize_forward_r4(rows)
        self.assertEqual(s["final_eligible"], 1)
        self.assertEqual(s["final_ratio_denominator"], 1)


class TestC1FeatureClock(unittest.TestCase):
    """C1：特征只能用「决策小时上一根已收盘」的资料；未来根不得改变过去特征。

    反例来自 `reports/history_research_review/r4/evidence_20261010.json`
    → `feature_future_mutation`：第 1500 小时未收盘的根曾把 `ret30d` 从 0.0 改成 0.5。
    """

    def _run(self, c, sym="C1CLOCKUSDT"):
        old_m, old_r = X.H.spot_months, X.H.read_spot
        X.H.spot_months = lambda s: ["2022-01"]
        X.H.read_spot = lambda s, m: c
        self.addCleanup(lambda: (X.features_dir(0) / f"{sym}.npz").unlink(missing_ok=True))
        try:
            X._features_worker_r4((sym, 0))
        finally:
            X.H.spot_months, X.H.read_spot = old_m, old_r
        with np.load(X.features_dir(0) / f"{sym}.npz") as z:
            return dict(zip(z["hour_idx"].tolist(), z["ret30d"].tolist()))

    def test_future_bar_cannot_mutate_past_feature(self):
        base = self._run(candles([100.0] * 1800))
        c = candles([100.0] * 1800)
        c[1500, 2] = 150.0
        c[1500, 3] = 150.0
        c[1500, 4] = 150.0
        mut = self._run(c)
        # 决策小时 1500 只能用第 1499 根 → 不受第 1500 根影响
        self.assertEqual(base.get(1500), 0.0)
        self.assertEqual(mut.get(1500), base.get(1500))
        # 第 1500 根**确实**影响决策小时 1501（证明改动可达，不是把特征整体弄丢）
        self.assertEqual(base.get(1501), 0.0)
        self.assertAlmostEqual(mut.get(1501), 0.5, places=5)

    def test_gap_in_lookback_is_nan_not_substituted(self):
        hours = [h for h in range(1800) if h != 1000]
        d = self._run(candles([100.0] * len(hours), hours=hours))
        # 决策小时 1721 需要第 1000 根 → 缺口必须声明 NaN，不静默用更少根数顶替
        self.assertIn(1721, d)
        self.assertTrue(np.isnan(d[1721]))
        # 与缺口无关的小时照常可用
        self.assertEqual(d.get(1500), 0.0)


class TestC2ForwardSplit(unittest.TestCase):
    """C2：30/90 天按真实结果窗口分组，跨界项从探索统计中真正排除。"""

    def _row(self, start_ms):
        return {
            "symbol": "AAAUSDT", "start_ms": start_ms, "granularity": "5m",
            "n_hours": 3, "first_score": 6, "score_total_max": 6,
            "deriv_status": "ok", "ref_price": 1.0,
            "ref_open_utc": R.ms_to_iso(start_ms), "ref_lag_min": 0,
            "ref_skipped_bars": 0, "ref_delayed": False,
            "dd_before_2x": None, "dd_before_2x_worst": None, "dd_before_2x_uncertain": False,
            "waits": {"wait_hours_to_1.5x": None, "wait_hours_to_2.0x": None},
            "dim": {"data_state": "complete", "elapsed": True, "final_eligible": True,
                    "gain_observed": "below_1_5x", "verdict": "below_1_5x",
                    "path": "no_2x_shallow"},
            "7d": {}, "30d": {}, "90d": {},
        }

    def test_cross_boundary_excluded_from_explore(self):
        """evidence.cross_boundary：起点在切点前 20 天、30 天窗跨切点 → 不进探索组。"""
        split = R.parse_utc(X.REVIEW_START)
        start = split - 20 * 24 * HR
        s = X.summarize_forward_r4([self._row(start)])
        self.assertEqual(s["crossover_excluded"]["cross_30d"], 1)
        self.assertEqual(s["crossover_excluded"]["cross_90d"], 1)
        self.assertEqual(s["crossover_excluded"]["explore_30d_clean"], 0)
        self.assertEqual(s["by_segment"]["explore_2022_2024"]["n"], 0)
        # 跨界项只留在专表，且总数仍可对账
        self.assertEqual(s["by_segment"]["cross_30d_only"]["n"], 1)
        self.assertEqual(s["n_episodes"], 1)

    def test_clean_explore_kept(self):
        """起点早于切点 200 天 → 30/90 天窗都干净，进探索组。"""
        split = R.parse_utc(X.REVIEW_START)
        start = split - 200 * 24 * HR
        s = X.summarize_forward_r4([self._row(start)])
        self.assertEqual(s["crossover_excluded"]["cross_30d"], 0)
        self.assertEqual(s["by_segment"]["explore_2022_2024"]["n"], 1)


class TestC3DirectOutcome(unittest.TestCase):
    """C3：直接后果标签（从当时参考价起的真实 30 天）与旁证标签分名分列。"""

    def _run(self, c, sym="C3OUTCOMEUSDT"):
        old_m, old_r = X.H.spot_months, X.H.read_spot
        X.H.spot_months = lambda s: ["2022-01"]
        X.H.read_spot = lambda s, m: c
        self.addCleanup(lambda: (X.outcome_dir(0) / f"{sym}.npz").unlink(missing_ok=True))
        try:
            X._outcome_worker_r4((sym, 0))
        finally:
            X.H.spot_months, X.H.read_spot = old_m, old_r
        with np.load(X.outcome_dir(0) / f"{sym}.npz") as z:
            return {int(h): (int(s), int(v), float(t)) for h, s, v, t in
                    zip(z["hour_idx"], z["state"], z["verdict"], z["max_touch"])}

    def test_reach_2x_from_reference_price(self):
        c = candles([100.0] * 2000)
        c[150, 2] = 250.0
        d = self._run(c)
        st, vd, touch = d[101]
        self.assertEqual(X.OUTCOME_STATES[st], "complete")
        self.assertEqual(X.OUTCOME_VERDICTS[vd], "reach_2x")
        self.assertAlmostEqual(touch, 2.5, places=4)

    def test_range_tail_is_not_a_negative(self):
        """主范围末端未到期 → partial_tail / undetermined，**不判为负例**。"""
        d = self._run(candles([100.0] * 2000))
        st, vd, _ = d[1999]
        self.assertEqual(X.OUTCOME_STATES[st], "partial_tail")
        self.assertEqual(X.OUTCOME_VERDICTS[vd], "undetermined")

    def test_price_gap_is_not_a_negative(self):
        hours = [h for h in range(2000) if h != 500]
        d = self._run(candles([100.0] * len(hours), hours=hours))
        st, vd, _ = d[101]
        self.assertEqual(X.OUTCOME_STATES[st], "gap")
        self.assertEqual(X.OUTCOME_VERDICTS[vd], "undetermined")

    def test_proxy_label_named_as_proxy(self):
        defs = X._label_definitions()
        self.assertEqual(defs["event_start_proxy"]["kind"], "proxy")
        self.assertEqual(defs["direct_outcome_reach_2x"]["kind"], "direct")
        self.assertIn("旁证", defs["event_start_proxy"]["caveat"])

    def test_unified_denominator_excludes_ineligible(self):
        """排序与分母统一：不可判定格不进分母（旧口径会给足 N）。"""
        E = np.array([[True, False], [True, True]])
        elig = np.array([[True, False], [True, True]])
        rank = np.array([[1, 32767], [1, 2]], dtype=np.int16)
        p = X._precision_by_n(rank, E, elig, n_max=2)
        # h0 分母 = min(1,1)=1；h1 分母 = min(2,2)=2 → 分子 3 / 分母 3
        self.assertEqual(p[2], 1.0)


class TestC4Workload(unittest.TestCase):
    """C4：人工负担按小时底账统计，日内重入不漏。"""

    def test_same_day_reentry_detected(self):
        """evidence.same_day_reentry：第 0 小时进、第 1 小时出、第 2 小时重入 → 重入 1 次。"""
        old = X.N_HOURS
        X.N_HOURS = 48
        try:
            rank = np.full((48, 2), 32767, dtype=np.int16)
            rank[0, 0] = 1
            rank[2, 0] = 1
            w = X._workload(rank, 10, 2, cadence=1)
            self.assertEqual(w["first_added_total"], 1)
            self.assertEqual(w["re_added_total"], 1)
            self.assertEqual(w["reminders_total"], 2)
        finally:
            X.N_HOURS = old

    def test_cadence_derived_from_same_ledger(self):
        """2 小时节奏下「第 2 小时重入」被跳过（查询点只落在偶数小时）。"""
        old = X.N_HOURS
        X.N_HOURS = 48
        try:
            rank = np.full((48, 2), 32767, dtype=np.int16)
            rank[0, 0] = 1
            rank[2, 0] = 1
            w2 = X._workload(rank, 10, 2, cadence=2)
            self.assertEqual(w2["cadence_hours"], 2)
            self.assertEqual(w2["first_added_total"], 1)
            self.assertEqual(w2["re_added_total"], 0)
        finally:
            X.N_HOURS = old


class TestC6EventSegments(unittest.TestCase):
    """C6：合并前真实连续段数；间隔 <48h 的两波应记 1 事件 2 段。"""

    def test_two_waves_one_event_two_segments(self):
        """evidence.real_merged_segments：独立算法找到两段、间隔约 41h。"""
        c = candles([100.0] * 720 + [260.0] * 24 + [180.0] * 736 + [400.0] * 1024)
        ev = X.detect_events_r4(c)
        self.assertEqual(len(ev), 1)
        e = ev[0]
        self.assertEqual(int(e["n_segments"]), 2)
        self.assertEqual([s["n_hours"] for s in e["segments"]], [720, 720])
        self.assertEqual(len(e["merge_gaps_hours"]), 1)
        self.assertAlmostEqual(float(e["merge_gaps_hours"][0]), 41.0, places=3)

    def test_single_wave_is_one_segment(self):
        c = candles([100.0] * 1500)
        c[1000, 2] = 250.0
        e = X.detect_events_r4(c)[0]
        self.assertEqual(int(e["n_segments"]), 1)
        self.assertEqual(len(e["segments"]), 1)
        self.assertEqual(e["merge_gaps_hours"], [])


class TestC7ReuseFingerprint(unittest.TestCase):
    """C7：复用前必须核对评分核指纹。"""

    def test_fingerprint_is_stable_and_cached(self):
        a = X.scoring_core_fingerprint()
        b = X.scoring_core_fingerprint()
        self.assertEqual(a, b)
        self.assertEqual(len(a), 64)

    def test_fingerprint_covers_scoring_core_only(self):
        for n in X.SCORING_CORE_FUNCS:
            self.assertTrue(hasattr(X, n), f"评分核函数 {n} 缺失")

    def test_legacy_reuse_requires_matching_decision_file(self):
        """决定文件缺失或指纹不符 → 不允许复用旧分片（返回 None）。"""
        p = X.OUT5 / "reuse_decision.json"
        self.assertTrue(p.exists(), "closeout/reuse_decision.json 必须存在（C7 证据）")
        doc = json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual(doc["scoring_core_fingerprint"], X.scoring_core_fingerprint())
        self.assertTrue(doc["scoring_core_identical_to_snapshot"])
        self.assertEqual(X._legacy_reuse_fingerprint(), X.scoring_core_fingerprint())


class TestC8RangeCorrection(unittest.TestCase):
    """C8：主范围与格数口径订正。"""

    def test_main_range_is_2026_09_30(self):
        self.assertEqual(X.MAIN_END, "2026-09-30T23:00:00Z")
        self.assertEqual(X.MAIN_START, "2022-01-01T00:00:00Z")
        self.assertEqual(int(X.N_HOURS), 41616)

    def test_supplementary_labels_declare_proxy(self):
        """补充结果的标签必须声明为旁证，且给出标签定义。"""
        p = X.OUT5 / "supplementary_decision_curve_lag0.json"
        if not p.exists():
            self.skipTest("补充结果尚未重算")
        d = json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual(d["label_kind"], "event_start_proxy")
        self.assertTrue(d["is_proxy_label"])
        self.assertIn("event_start_proxy", d["label_definitions"])


class TestOutputIsolationR4(unittest.TestCase):
    """输出隔离：r4 只写 reports/history_research_ds/r4/。"""

    def test_paths_under_r4(self):
        self.assertEqual(X.OUT4.name, "r4")
        self.assertEqual(X.OUT4.parent, X.V3_OUT)
        for d in (X.SHARDS4, X.SIG4, X.EV4, X.FWD4):
            self.assertEqual(d.parent, X.OUT4, f"{d} 不在 r4 目录下")

    def test_closeout_subdirs(self):
        """本批新计算一律落在 closeout/ 下，不覆盖 r4 原产物。"""
        self.assertEqual(X.OUT5.parent, X.OUT4)
        self.assertEqual(X.OUT5.name, "closeout")
        for d in (X.EV5, X.FWD5, X.OUTCOME5, X.FEATURES_DIR):
            self.assertEqual(d.parent, X.OUT5, f"{d} 不在 closeout 目录下")

    def test_lag_subdirs(self):
        self.assertEqual(X.shards_dir(1).name, "lag1")
        self.assertEqual(X.shards_dir(0).parent, X.SHARDS4)


class TestReuseGuard(unittest.TestCase):
    """复用版本守卫：v3 源码哈希不符即拒绝运行。"""

    def test_match_now(self):
        rep = X.verify_reuse()
        self.assertTrue(rep["match"], "被复用的 v3 模块已变更，r4 复用清单失效")

    def test_mismatch_raises(self):
        old = X.REUSE_V3_SHA256
        try:
            X.REUSE_V3_SHA256 = "0" * 64
            with self.assertRaises(RuntimeError):
                X.verify_reuse()
        finally:
            X.REUSE_V3_SHA256 = old


class TestReconcileR4(unittest.TestCase):
    """对账：r4 产物内部一致（无产物则跳过）。"""

    def _load(self, name):
        p = X.OUT4 / name
        if not p.exists():
            raise unittest.SkipTest(f"缺少 {name}（尚未全量重算）")
        return json.loads(p.read_text(encoding="utf-8"))

    def test_forward_reconciles(self):
        s = self._load("forward_summary_lag0.json")
        self.assertTrue(s["verdict_reconciles"])
        self.assertEqual(sum(s["verdict"].values()), s["n_episodes"])

    def test_integrity_sum_check(self):
        s = self._load("integrity_lag0.json")
        self.assertTrue(s["sum_check"])

    def test_reverse_reconciles(self):
        s = self._load("reverse_summary_lag0.json")
        self.assertTrue(s["disposition_reconciles"])

    def test_c5_late_observations_have_remaining_opportunity(self):
        """C5：之后观察必须带剩余机会证据（22 条全有，且不再缺）。"""
        p = X.OUT5 / "reverse_summary_lag0.json"
        if not p.exists():
            raise unittest.SkipTest("closeout 反向结果尚未重算")
        s = json.loads(p.read_text(encoding="utf-8"))
        lo = s["late_observations"]
        self.assertEqual(lo["n"], 22)
        self.assertEqual(lo["n_with_remaining_computed"], 22)
        # 「缺失数」= 总数 − 已算出数；冻结版用 `n_with_remaining_computed` 表达，不另设字段
        self.assertEqual(lo["n"] - lo["n_with_remaining_computed"], 0)
        self.assertIn("remaining_30d_touch_by_kind", s)
        self.assertEqual(s["remaining_30d_touch_by_kind"]["late"]["n"], 22)

    def test_c6_events_report_true_segment_counts(self):
        """C6：事件汇总必须给出真实段数分布（不是恒定 1）。"""
        p = X.OUT5 / "events_summary.json"
        if not p.exists():
            raise unittest.SkipTest("closeout 事件表尚未重算")
        s = json.loads(p.read_text(encoding="utf-8"))
        self.assertIn("n_segments", s)
        self.assertGreater(int(s["n_segments"]["max"]), 1)

    def test_c2_forward_summary_excludes_crossover(self):
        """C2：forward 汇总必须给出跨界排除数与干净探索段计数。"""
        p = X.OUT5 / "forward_summary_lag0.json"
        if not p.exists():
            raise unittest.SkipTest("closeout forward 汇总尚未重算")
        s = json.loads(p.read_text(encoding="utf-8"))
        ce = s["crossover_excluded"]
        self.assertEqual(ce["cross_30d"], 13432)
        self.assertEqual(ce["cross_90d"], 37656)
        self.assertNotIn("explore_count", ce)

    def test_c3_outcome_unified_denominator(self):
        """C3：直接后果评价必须用统一分母，并声明 eligible 格数。"""
        p = X.OUT5 / "outcome_lift_lag0.json"
        if not p.exists():
            raise unittest.SkipTest("closeout 直接后果尚未重算")
        s = json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual(s["label_kind"], "direct_outcome")
        self.assertFalse(s["is_proxy_label"])
        # `n_eligible_cells` 是 (对象, 小时) 格数，上界 = 网格小时 × 对象数
        self.assertGreater(int(s["n_eligible_cells"]), 0)
        self.assertLess(int(s["n_eligible_cells"]),
                        int(X.N_HOURS) * len(X.H.usdt_spot_symbols()))
        self.assertGreater(int(s["hours_with_eligible"]), 0)
        # 「统一分母」= 各排序共用同一 eligible 基准
        bases = {v["reach_2x"]["base"] for v in s["by_ranking"].values()}
        self.assertEqual(len(bases), 1)
        self.assertGreater(int(s["time_split"]["cross_boundary_hours_excluded_from_train"]), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
