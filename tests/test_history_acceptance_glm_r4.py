#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""glm r4 复验测试（closeout 版，2026-10-10）。

按 tasks/history_r4_closeout_20261010.md 第 3 节与主代理 r4 报告第 3、4 节：
- 持仓样例时间**升序**且最新点上涨（旧样例倒序是我方错误，已修）。
  正确预期：24 点无 OI 分；25 点跨 24h=4 分；跨 72h=7 分；LS 缺失不扣合法 OI 分。
- 合波样例先由朴素算法确认两个合格段，再断言合并/分立与 n_segments（C6）。
- 字段按 r4 真实接口断言：max_close_drawdown / max_dd_from_entry / elapsed /
  data_state / final_eligible / verdict / dd_before_2x(_worst/_uncertain)。
- C1–C6 最小正确反例接实际研究入口；r4 当前未修的项用 @expectedFailure 标注
  （红灯=ds 侧阻塞证据，修好后自动转为正式断言；unexpected success 会被发现）。

期望的唯一来源 = 本文件 _naive_* 参照实现（逐根循环 + 手算自检 IDxx）。
接入纪律：r4 冻结入口 = reports/history_research_ds/r4/closeout/ready_for_glm.md；
未冻结时产品断言由 GLM_R4_DIAG 显式开启（预接诊断，非验收）。
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

HOUR = 3_600_000
T0 = (1_700_000_000_000 // HOUR) * HOUR  # 整点锚（=1699999200000，注意非 1700006400000）

FROZEN_MARK = ROOT / "reports" / "history_research_ds" / "r4" / "closeout" / "ready_for_glm.md"
_R4 = None


def _load_r4():
    global _R4
    if _R4 is None:
        if FROZEN_MARK.exists() or os.environ.get("GLM_R4_DIAG") == "1":
            try:
                import history_research_ds_r4 as m
                _R4 = m
            except ImportError:
                _R4 = False
        else:
            _R4 = False
    return _R4 if _R4 else None


class _RequireR4(unittest.TestCase):
    """r4 未冻结且未开 GLM_R4_DIAG → 产品断言整体跳过（朴素自检类不受影响）。"""

    @classmethod
    def setUpClass(cls):
        cls.H = _load_r4()
        if cls.H is None:
            raise unittest.SkipTest("r4 未冻结且未开 GLM_R4_DIAG → 产品断言跳过")


# ================================================================ 朴素参照实现
def naive_klines(closes, t0=T0, gaps=None):
    rows, ts = [], t0
    for i, c in enumerate(closes):
        rows.append([ts, c, c, c, c, 1.0])
        ts += HOUR * (1 + (gaps.get(i, 0) if gaps else 0))
    return np.array(rows, dtype=float)


def naive_w_segments(c, thr=2.0, win=720):
    """朴素 W 段：m30[t]=max(high[t+1..t+win])/close[t]>=thr 的连续下标段（独立标准）。"""
    n = len(c)
    m = np.zeros(n)
    for t in range(n - 1):
        seg = c[t + 1: t + 1 + win, 2]
        if len(seg):
            m[t] = seg.max() / c[t, 4]
    w = m >= thr
    segs, a = [], None
    for t in range(n):
        if w[t] and a is None:
            a = t
        elif not w[t] and a is not None:
            segs.append((a, t - 1)); a = None
    if a is not None:
        segs.append((a, n - 1))
    return segs


def naive_fwd_open_ref(c, ref_idx, win_h):
    """开盘参照：参考根开盘之后（含参考根自身根内涨幅）纳入；开盘>=截止的根排除。
    返回 (max_touch, max_close, max_dd_from_entry, max_close_drawdown, elapsed)。"""
    ref = float(c[ref_idx, 1])
    t_end = float(c[ref_idx, 0]) + win_h * HOUR
    seg = c[(c[:, 0] >= c[ref_idx, 0]) & (c[:, 0] < t_end)]
    if len(seg) == 0:
        return (None, None, None, None, bool(c[-1, 0] + HOUR >= t_end))
    cc = seg[:, 4]
    run = np.maximum.accumulate(np.concatenate(([ref], cc)))
    ddser = np.concatenate(([ref], cc)) / run - 1.0
    return (float(seg[:, 2].max()) / ref, float(cc.max()) / ref,
            min(0.0, float(seg[:, 3].min()) / ref - 1.0), float(ddser.min()),
            bool(c[-1, 0] + HOUR >= t_end))


def naive_rank(vals_row, valid_mask):
    idx = [i for i in range(len(vals_row)) if valid_mask[i]]
    order = sorted(idx, key=lambda i: -vals_row[i])
    rk = {i: r for r, i in enumerate(order, 1)}
    return [rk.get(i, 32767) for i in range(len(vals_row))]


# ============================================================ IDxx 期望自检
class TestNaiveReferenceSelfCheck(unittest.TestCase):
    """朴素参照 = 手算。始终运行，不计入产品通过。"""

    def test_id1_close_ref_path(self):
        c = naive_klines([100.0, 200.0, 150.0])
        touch, mx, dde, ddc, el = naive_fwd_open_ref(c, 0, 720)
        self.assertAlmostEqual(ddc, -0.25, msg="收盘路径自高点回落=-25%")
        self.assertEqual(dde, 0.0, "100→200→150 无入场下跌（不得与高点回撤混名）")

    def test_id2_entry_dd_includes_entry(self):
        c = naive_klines([100.0, 80.0, 200.0])
        _, _, dde, ddc, _ = naive_fwd_open_ref(c, 0, 720)
        self.assertAlmostEqual(dde, -0.20, msg="相对入场最低 80 → -20%")
        self.assertAlmostEqual(ddc, -0.20, msg="收盘路径同序列=-20%")

    def test_id3_w_segments_two_pulse(self):
        # 主代理 C6 fixture：段1=[0..719]，段2=[760..1479]，间隔 41h —— 两个合格段
        c = naive_klines([100.0] * 720 + [260.0] * 24 + [180.0] * 736 + [400.0] * 1024)
        segs = naive_w_segments(c)
        self.assertEqual(segs[0], (0, 719), "手算：段1=[0..719]")
        self.assertEqual(segs[1], (760, 1479), "手算：段2=[760..1479]（400/180=2.22 合格）")
        self.assertEqual(segs[1][0] - segs[0][1] - 1, 40, "实际间隔 40h（下标差 41）≤48h")

    def test_id4_w_segments_single_pulse(self):
        # 我方旧合波样例错误的自证：涨到 260 后回到 100，全程朴素算出恰 1 个合格段
        # （260 段自身 m30=1，回落后无第二个 ≥2× 前瞻）→ 不能作为"两波合并"证据。
        c = naive_klines([100.0] * 720 + [260.0] * 24 + [100.0] * 700)
        segs = [s for s in naive_w_segments(c)]
        self.assertEqual(len(segs), 1, "手算：单脉冲 → 恰 1 段 (0,719)")
        self.assertEqual(segs[0], (0, 719))

    def test_id5_naive_rank_continuous(self):
        self.assertEqual(naive_rank([5.0, -1.0, 4.0], [True, False, True]), [1, 32767, 2])

    def test_id6_daily_distinct_vs_per_round(self):
        rounds = [["A", "B"], ["C", "D"], ["E", "F"]]
        self.assertEqual(len({s for r in rounds for s in r}), 6,
                         "每轮前 2 全天轮换 → 每日不同币=6，不得写成 2")

    def test_id7_intraday_reentry_hand(self):
        # 手算：0h 进、1h 出、2h 重入（同一日）→ 日内重入=1（天级合并=0，C4 阻塞核心）
        hours_in = [True, False, True]
        seen, reentry = False, 0
        for h in hours_in:
            if h and seen:
                reentry += 1
            seen |= h
        self.assertEqual(reentry, 1)
        daily = any(hours_in)
        self.assertTrue(daily)
        # 天级口径会把同一日的进-出-进合并成"持续在榜" → 重入 0（漏）

    def test_id8_cross_window_hand(self):
        # 手算：start=2024-12-15，start+30d=2025-01-14 ≥ 2025-01-01 → 跨 30 天界
        from datetime import datetime, timezone
        start = datetime(2024, 12, 15, tzinfo=timezone.utc)
        review = datetime(2025, 1, 1, tzinfo=timezone.utc)
        self.assertTrue(start.toordinal() + 30 >= review.toordinal())
        self.assertLess(start.timestamp(), review.timestamp())


# ============================================================ 持仓评分（升序样例）
class TestR1DerivAscending(_RequireR4):

    def _rows61(self):
        import tests.test_history_acceptance_glm as T
        return T.TestFutureIsolation()._rows61()

    def _eval(self, oi, ls_vals, lag=0):
        """dt 升序（T0-79h … T0），decision=T0+59h → 全部 80 行可用。
        涨价放在**最新**一端（最新点确实上涨）。"""
        H = _load_r4()
        rows = self._rows61()
        d = np.array([int(rows[59, 0])])
        dt = np.array([T0 - (79 - k) * HOUR for k in range(80)], dtype=np.int64)  # 升序
        ls = np.full(80, np.nan) if ls_vals is None else np.full(80, ls_vals)
        return H.evaluate_r4("X", rows, dt, oi, ls, True, d, lag_hours=lag)

    def _sparse(self, n_pts):
        """n_pts 个有效点、每 3h 一个、末点最新（150）：1d 基准 100、3d 基准 100。"""
        oi = np.full(80, np.nan)
        idxs = [3 * k for k in range(n_pts)]
        for i in idxs[:-1]:
            oi[i] = 100.0
        oi[idxs[-1]] = 150.0
        return oi

    def test_24_points_no_oi_score(self):
        o = self._eval(self._sparse(24), 1.5)
        self.assertEqual(int(o["score_deriv"][0]), 0, "24 点（<25）→ 无 OI 分；LS 1.5 也不给")

    def test_25_points_span72h_is_7(self):
        o = self._eval(self._sparse(25), 1.5)
        self.assertEqual(int(o["score_deriv"][0]), 7,
                         "25 点跨 72h → 1d(+4)+3d(+3)=7（手算锚）")

    def test_25_consecutive_rows_span24h_is_4(self):
        oi = np.full(80, np.nan)
        oi[-25:] = 100.0
        oi[-1] = 150.0                       # 最新 25 小时，只有 24h 前基准
        o = self._eval(oi, 1.5)
        self.assertEqual(int(o["score_deriv"][0]), 4, "跨 24h：仅 1d 可得 → 4 分")
        self.assertTrue(np.isnan(o["oi_chg_3d"][0]), "3d 基准不在有效窗 → NaN")

    def test_25_points_ls_missing_keeps_oi(self):
        o = self._eval(self._sparse(25), None)
        self.assertEqual(int(o["score_deriv"][0]), 7,
                         "LS 全缺 → OI 7 分保留（partial 不得清零）")

    def test_three_points_ls_only_3(self):
        oi = np.full(80, np.nan)
        oi[[0, 40, 79]] = [100.0, 100.0, 150.0]
        o = self._eval(oi, 0.8)
        self.assertEqual(int(o["score_deriv"][0]), 3, "3 点例 → 仅 LS 0.8<1 得 3 分（F1 正确预期）")
        self.assertTrue(np.isnan(o["oi_chg_1d"][0]), "不足 25 观测 → oi_chg 不可算")


# ============================================================ 排名（F2 修复核对）
class TestR1bRanking(_RequireR4):

    def test_valid_ranks_continuous_all_lanes(self):
        H = _load_r4()
        self.assertIsNotNone(H, "r4 未冻结且未开 DIAG")
        total = np.array([[5, -1, 4, 5]], dtype=np.int16)
        rk = (getattr(H, "rank_matrix_r4", None) or H.rank_matrix)(total)
        self.assertEqual(rk[0].tolist(), [1, 32767, 3, 2], "总分：无效列不占名次，并列按列序")
        for seed in (20261009, 20261010, 20261011):
            rk = (getattr(H, "random_rank_matrix_r4", None) or H.random_rank_matrix)(seed, total)
            vals = sorted(int(rk[0][i]) for i in range(4) if total[0][i] >= 0)
            self.assertEqual(vals, [1, 2, 3], f"random seed={seed}：有效名次连续 1..3")
            self.assertEqual(int(rk[0][1]), 32767)


# ============================================================ R2 参考价与路径（真实字段）
class TestR2ForwardRealFields(_RequireR4):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.H = _load_r4()
        cls.fn = None
        if cls.H:
            # staticmethod 包装：避免 self.fn(...) 把 self 当首参传给模块函数
            cls.fn = staticmethod(getattr(cls.H, "forward_from_series_r4", None))

    def _f(self, c, ref_ms):
        self.assertIsNotNone(self.fn, "r4 forward 入口缺失")
        return self.fn(c, float(ref_ms), HOUR, windows={"30d": 720})

    def test_open_ref_same_bar_250_recorded(self):
        c = naive_klines([100.0] * 61)
        c = np.vstack([c, [[c[-1, 0] + HOUR, 100.0, 250.0, 100.0, 250.0, 1.0]]])
        f = self._f(c, c[61, 0])
        self.assertAlmostEqual(f["30d"]["max_touch"], 2.5, "开盘参考：同根真实涨到 250 应可记录")

    def test_close_ref_via_next_open_not_backfilled(self):
        c = naive_klines([100.0] * 162)
        c[61] = [c[61, 0], 100.0, 250.0, 100.0, 150.0, 1.0]
        f = self._f(c, c[61, 0] + HOUR)          # 参考收盘可用时刻 → 下一根开盘
        self.assertLess(f["30d"]["max_touch"], 2.5, "参考收盘之前盘中 250 不得冒充后续")

    def test_bar_opening_at_cutoff_excluded(self):
        closes = [100.0] * 781
        c = naive_klines(closes)
        c[780] = [c[780, 0], 100.0, 999.0, 100.0, 100.0, 1.0]  # 开盘恰=截止时刻
        f = self._f(c, c[60, 0])
        self.assertLess(f["30d"]["max_touch"], 9.99, "截止时刻才开盘一根的 999 不得计入已截止窗")

    def test_close_drawdown_minus25_and_entry_dd_named_separately(self):
        c = naive_klines([100.0, 200.0, 150.0])
        f = self._f(c, c[0, 0])
        self.assertAlmostEqual(f["30d"]["max_close_drawdown"], -0.25,
                               msg="收盘路径自高点回落=-25%（正值=错）")
        self.assertGreaterEqual(f["30d"]["max_dd_from_entry"], 0.0,
                                "100→200→150 无入场下跌；两字段不得混名")
        c2 = naive_klines([100.0, 80.0, 200.0])
        f2 = self._f(c2, c2[0, 0])
        self.assertAlmostEqual(f2["30d"]["max_dd_from_entry"], -0.20, msg="入场点不消失 → -20%")
        self.assertAlmostEqual(f2["30d"]["max_close_drawdown"], -0.20)

    def test_first_hit_bar_close_not_prepended(self):
        c = naive_klines([100.0, 180.0])
        # 首个 2× 根内 low=80 低于此前路径 → 根内先后不明，必须给不确定标志
        c[1] = [c[1, 0], 100.0, 220.0, 80.0, 100.0, 1.0]
        f = self._f(c, c[0, 0])
        self.assertTrue(f["dd_before_2x_uncertain"],
                        "同根内先后不明 → 必须给不确定标志（不得把达标后收盘归为达标前）")
        self.assertGreaterEqual(f["dd_before_2x"], 0.0, "确定部分：达标根之前无下跌 → 0")

    def test_immature_undetermined_not_failure(self):
        c = naive_klines([100.0] * 100)                      # 远不足 720h
        f = self._f(c, c[10, 0])
        self.assertFalse(f["30d"]["elapsed"], "时间未走满 → elapsed=False")
        self.assertNotEqual(f["30d"]["data_state"], "complete", "资料不完整 → 非 complete")
        self.assertFalse(f["dim"]["final_eligible"], "未到期 → 不进最终失败比例")
        self.assertEqual(f["dim"]["verdict"], "undetermined", "未到期未涨 ≠ 完整失败")

    def test_reach_2x_fact_independent_of_maturity(self):
        c = naive_klines([100.0] * 100)
        c[50] = [c[50, 0], 100.0, 260.0, 100.0, 260.0, 1.0]
        f = self._f(c, c[10, 0])
        self.assertEqual(f["dim"]["verdict"], "reach_2x",
                         "已发生的 2× 是事实，与是否到期无关")

    def test_late_reference_exposed(self):
        c = naive_klines([100.0] * 100, gaps={59: 24 * 30})   # 中段 30 天缺口
        ref_ms = c[59, 0] + HOUR                # 参考落在缺口内 → 下一根在 720h 后
        f = self._f(c, ref_ms)
        self.assertGreater(int(f["ref_lag_min"]), 60, "迟到参考必须暴露 ref_lag_min")
        self.assertTrue(bool(f["ref_delayed"]) or int(f["ref_skipped_bars"]) > 0,
                        "跳过的整根数必须可见（KEYUSDT 型）")

    def test_gap_in_window_not_complete(self):
        c = naive_klines([100.0] * 60 + [110.0] * 700, gaps={59: 48})
        f = self._f(c, c[0, 0])
        self.assertGreater(int(f["30d"]["missing_bars"]), 0, "缺口 → 实有根数 < 应有")
        self.assertNotEqual(f["30d"]["data_state"], "complete", "窗口内有缺口 → 不得 complete")


# ============================================================ R3 事件时间与合波（C6）
class TestR3Events(_RequireR4):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.H = _load_r4()
        f = getattr(cls.H, "detect_events_r4", None) if cls.H else None
        cls.fn = staticmethod(f)

    def test_two_pulses_40h_gap_merge_with_n_segments_2(self):
        """C6 正确预期：两合格段间隔 40h≤48h → 1 事件、n_segments=2。
        独立探针（2026-10-10）：当前 ds 工作副本已返回 n_segments=2，
        与主代理 r4 报告的「固定 1」不一致 → 转正式断言，closeout 冻结时须复核差异。"""
        c = naive_klines([100.0] * 720 + [260.0] * 24 + [180.0] * 736 + [400.0] * 1024)
        segs = naive_w_segments(c)
        self.assertEqual(len(segs), 2, "前置：朴素算法确认两个合格段")
        ev = self.fn(c)
        self.assertEqual(len(ev), 1, "间隔 ≤48h → 合并为 1 事件")
        self.assertEqual(int(ev[0]["n_segments"]), 2,
                         "合并事件必须保留原始段数（C6：不得固定 1）")

    def test_two_pulses_far_apart_stay_two(self):
        c = naive_klines([100.0] * 720 + [260.0] * 24 + [180.0] * 790 + [400.0] * 970)
        segs = naive_w_segments(c)
        self.assertEqual(len(segs), 2, "前置：两个合格段")
        self.assertGreater(segs[1][0] - segs[0][1] - 1, 48, "前置：间隔 >48h")
        ev = self.fn(c)
        self.assertEqual(len(ev), 2, "间隔 >48h → 2 事件")

    def test_peak_ms_true_high_and_gap_not_merged(self):
        c = naive_klines([100.0] * 100 + [260.0] + [100.0] * 619, gaps={101: 1440})
        c[719] = [c[719, 0], 100.0, 400.0, 100.0, 100.0, 1.0]
        ev = self.fn(c)
        self.assertGreaterEqual(len(ev), 1)
        e0 = ev[0]
        self.assertAlmostEqual(e0["max_m30"], 2.6, places=3, msg="缺口后 400 不得进入起点 30 天窗")
        self.assertEqual(int(e0["peak_ms"]), int(c[100, 0]), "peak_ms=真实高点（260 根）")
        self.assertFalse(bool(e0.get("has_internal_gap", False)), "缺口处断开游程，不跨缺合波")
        # 90d=2160h：400 根距起点 2159h 恰在窗内（手算）→ max_m90 可为 4.0
        self.assertLessEqual(e0["max_m90"], 4.0 + 1e-6)


# ============================================================ C2 跨期排除（实际入口）
class TestC2CrossPeriodExcluded(_RequireR4):

    def _row(self, start_ms):
        H = _load_r4()
        return {
            "symbol": "X", "start_ms": start_ms,
            "ref_open_utc": H.R.ms_to_iso(int(start_ms)),
            "granularity": "1h",
            "dim": {"verdict": "reach_2x", "gain_observed": "reach_2x",
                    "data_state": "complete", "final_eligible": True,
                    "path": "reach_2x_no_dd"},
            "waits": {"wait_hours_to_2.0x": 1.0, "wait_hours_to_1.5x": 1.0},
            "30d": {"max_touch": 2.5, "max_close": 2.4, "max_dd_from_entry": 0.0,
                    "max_close_drawdown": -0.1},
            "dd_before_2x": 0.0, "dd_before_2x_worst": 0.0, "dd_before_2x_uncertain": False,
            "first_score": 10, "score_total_max": 10,
        }

    def test_cross_30d_row_excluded_from_explore(self):
        """C2：跨界记录必须从探索组移除。
        独立探针（2026-10-10）：当前副本 summarize_forward_r4(:1084-1100) 已按
        结果窗分组、跨界项只留专表 → 转正式断言；主代理报告的「无条件 append」
        与当前工作副本不符，closeout 冻结时须复核差异来源（版本/快照）。"""
        H = _load_r4()
        s = int(H.R.parse_utc(H.REVIEW_START))
        rows = [self._row(s - 15 * 24 * HOUR)]      # start+30d 跨过切点
        out = H.summarize_forward_r4(rows)
        self.assertEqual(out["crossover_excluded"]["explore_30d_clean"], 0,
                         f"跨界行不得留在探索组（实际 {out['crossover_excluded']}）")
        self.assertEqual(out["crossover_excluded"]["cross_30d"], 1, "跨界行保留在专表")

    def test_non_cross_row_stays_in_explore(self):
        H = _load_r4()
        s = int(H.R.parse_utc(H.REVIEW_START))
        rows = [self._row(s - 200 * 24 * HOUR)]     # start+30d 仍在探索段
        out = H.summarize_forward_r4(rows)
        self.assertEqual(out["crossover_excluded"]["explore_30d_clean"], 1,
                         "非跨界行正常留在探索组")


# ============================================================ C4 人工量（实际入口）
class TestC4Workload(_RequireR4):

    def test_day_level_reentry_counted(self):
        H = _load_r4()
        # 4 天 × 2 币：币0 前 2 天在榜、第 3 天掉出、第 4 天回进 → 隔日重入=1
        rank = np.full((4 * 24, 2), 32767, dtype=np.int16)
        rank[:2 * 24, 0] = 1
        rank[3 * 24:, 0] = 1
        w = H._workload(rank, 1, 2)
        self.assertEqual(w["re_added_total"], 1, "手算：隔日回来=重入 1 次")
        self.assertEqual(w["first_added_total"], 1, "首次加入只有 1 次")

    def test_intraday_reentry_visible(self):
        """C4：日内 0h 进、1h 出、2h 重入 → 重入=1。
        独立探针（2026-10-10）：当前 _workload 已是「C4 修正版」（小时级、重入不漏，
        re_added_total=1），与主代理 r4 报告的「天级合并漏记」不一致 → 转正式断言。"""
        H = _load_r4()
        rank = np.full((24, 2), 32767, dtype=np.int16)
        rank[0, 0] = 1
        rank[2, 0] = 1                          # 同一日内重入
        w = H._workload(rank, 1, 2)
        self.assertGreaterEqual(w["re_added_total"], 1,
                                "日内重入不得被天级合并吞掉")


# ============================================================ C3 标签≠机会（实际入口）
class TestC3LabelVsOpportunity(_RequireR4):

    def test_label_true_but_no_2x_in_30d_window(self):
        """C3 最小反例：事件起点 s=700h → 标签覆盖 [0..699]；但第 0 小时的真实
        30 天窗 [0,720h] 内最高触及=1.0×。标签 true 不得冒充当前可得的 2× 机会。"""
        H = _load_r4()
        # 事件格子以 MAIN_START_MS 为 0 点（2022-01-01），不是 T0
        s = 700
        events = [{"symbol": "XUSDT",
                   "start_ms": H.MAIN_START_MS + s * HOUR, "tier": 2.5}]
        labels = H._build_labels(events, {"XUSDT": 0}, np.array([True]))
        E, n_in = labels["tier2"]
        self.assertEqual(n_in, 1)
        self.assertTrue(bool(E[0, 0]), "第 0 小时在事件起点前 720h 内 → 标签 true")
        # 同一序列的真实 forward：0h 参考、30 天窗（720h）内平盘
        c = naive_klines([100.0] * 760)          # 事件涨幅在 700h 后、30 天窗之外
        fn = getattr(H, "forward_from_series_r4", None)
        if fn is not None:
            f = fn(c, float(c[0, 0]), HOUR, windows={"30d": 720})
            self.assertAlmostEqual(f["30d"]["max_touch"], 1.0,
                                   msg="同一时点真实 30 天窗内无上涨 → 触及=1.0×")
            self.assertNotEqual(f["dim"]["gain_observed"], "reach_2x")


# ============================================================ C1 特征时间（期望自检）
class TestC1FeatureCausality(_RequireR4):

    def test_naive_ret30d_unchanged_by_future_bar(self):
        """C1 期望：改第 1500 根（未完成）K 线，此前所有小时格的 ret30d 不得变。
        （r4 当前把当根 close 放进当根开盘格 → 违反；接 closeout 修复后的特征产物
        转正式断言。）"""
        closes = [100.0] * 1400 + [200.0] * 100 + [150.0]
        base = np.array(closes, dtype=float)
        bumped = base.copy()
        bumped[1500] = 999.0                    # 只改最后一根
        def ret30d(a):
            out = np.full(len(a), np.nan)
            for t in range(720, len(a)):
                out[t] = a[t] / a[t - 720] - 1.0
            return out
        r0, r1 = ret30d(base), ret30d(bumped)
        np.testing.assert_array_equal(r0[:1500], r1[:1500],
                                      err_msg="改未完成根不得影响此前任何小时格的特征")
        self.assertNotAlmostEqual(float(r1[1500]), float(r0[1500]))
        # r4 修复后期望：第 1500 格（开盘时刻格）只放上一根已完成值 → 主代理探针
        # 「改 1500 根 close，ret30d@1500 必不变」将作为接入 closeout 特征产物的正式断言。


# ==================================================== C1 特征时间（实际生成路径）
class TestC1FeaturesRealEntry(_RequireR4):
    """5.2.2：调用**实际特征生成路径**（_features_worker_r4），输出写自有目录；
    naive 参照只用「已收盘」根逐根复算全量特征——若生成器把未完成根泄进决策格，
    全量对账必然失配（等价于主代理「改第 1500 根」探针的注入式证明）。
    原始归档只读；ds 模块的 FEATURES_DIR 指到 replay 目录后还原。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.m = cls.H
        cls.save_dir = getattr(cls.m, "FEATURES_DIR", None)
        out = ROOT / "reports" / "history_research_glm" / "r4" / "replay" / "features"
        cls.m.FEATURES_DIR = out                    # 只写自有目录
        cls.sym = cls._pick_symbol()
        if cls.sym is None:
            raise unittest.SkipTest("前 200 个对象均无可用归档 → C1 实路径整类跳过")
        cls.res0 = cls.m._features_worker_r4((cls.sym, 0))
        cls.res1 = cls.m._features_worker_r4((cls.sym, 1))
        cls.npz0 = np.load(out / "lag0" / f"{cls.sym}.npz")
        cls.npz1 = np.load(out / "lag1" / f"{cls.sym}.npz")
        cls.arr = cls.m.H.read_spot(cls.sym, cls.m.H.spot_months(cls.sym))

    @classmethod
    def tearDownClass(cls):
        if cls.save_dir is not None:
            cls.m.FEATURES_DIR = cls.save_dir       # 还原，不污染其他测试

    @classmethod
    def _pick_symbol(cls):
        """前 200 个对象里找一个有归档且小时网格有缺口的（缺口验证用）；
        全无缺口则退回首个有归档样本（缺口断言自行 skip 并如实记录）。"""
        fallback = None
        for sym in cls.m.H.usdt_spot_symbols()[:200]:
            months = cls.m.H.spot_months(sym)
            if not months:
                continue
            arr = cls.m.H.read_spot(sym, months)
            if arr is None or len(arr) < 1000:
                continue
            rel = ((arr[:, 0].astype(np.int64) - int(arr[0, 0])) // HOUR)
            if len(np.unique(rel)) < len(rel):      # 有缺口
                return sym
            if fallback is None:
                fallback = sym
        return fallback

    def test_01_worker_ran_with_real_archive(self):
        if self.sym is None:
            self.skipTest("前 200 个对象均无可用归档，C1 实路径测试无法取样")
        self.assertGreater(self.res0["rows"], 0, "实际特征生成路径必须产出行")
        self.assertFalse(self.res0["network_blocked"], "只读本地归档，不得触发网络")

    def test_02_hour_grid_is_decision_clock_plus1(self):
        """决策小时 = K 线开盘小时 + 1（只用已收盘根的时钟对齐）。"""
        ts = self.arr[:, 0].astype(np.int64)
        abs_hour = (ts - self.m.MAIN_START_MS) // HOUR
        keep = (abs_hour + 1 >= 0) & (abs_hour + 1 < int(self.m.N_HOURS))
        np.testing.assert_array_equal(self.npz0["hour_idx"], (abs_hour[keep] + 1).astype(np.int64))

    def test_03_naive_full_array_match_closed_bars_only(self):
        """naive 只用已收盘根逐根复算 ret30d/ret7d/dist60h → 全量一致。
        一致即证明：任何决策小时的特征都不含当根（未完成）信息。"""
        ts = self.arr[:, 0].astype(np.int64)
        rel = ((ts - int(ts[0] - (ts[0] % HOUR))) // HOUR).astype(np.int64)
        # 与 worker 同一网格起点：worker 用 start=int(ts[0])，rel=(ts-start)//HR
        start = int(ts[0])
        rel = ((ts - start) // HOUR).astype(np.int64)
        cmap = {}                                   # rel → close（只含实有根）
        hmap = {}                                   # rel → high
        for j, r in enumerate(rel):
            cmap[int(r)] = float(self.arr[j, 4])
            hmap[int(r)] = float(self.arr[j, 2])
        hour_idx = self.npz0["hour_idx"]
        abs_hour = hour_idx - 1                     # 决策小时对应的开盘小时（=MAIN 格）
        # worker 采样的是 rel[sel]（sel=keep 掩码），ret30d 在 rel 网格上算好后取样
        keep_rel = rel[( (ts - self.m.MAIN_START_MS) // HOUR + 1 >= 0)
                       & (((ts - self.m.MAIN_START_MS) // HOUR) + 1 < int(self.m.N_HOURS))]
        # naive ret30d / ret7d：c[r]/c[r-W]-1，任一端缺口 → NaN
        for key, win in (("ret30d", 720), ("ret7d", 168)):
            naive = np.full(len(keep_rel), np.nan)
            for k, r in enumerate(keep_rel):
                a, b = cmap.get(int(r)), cmap.get(int(r) - win)
                if a is not None and b is not None:
                    naive[k] = a / b - 1.0
            got = self.npz0[key]
            self.assertEqual(len(got), len(naive), f"{key} 行数")
            fin = np.isfinite(naive)
            np.testing.assert_allclose(got[fin], naive[fin], rtol=1e-4, atol=1e-6,
                                       err_msg=f"{key} 全量对账失配 → 存在未收盘信息泄漏")
            self.assertTrue(np.array_equal(np.isfinite(got), fin),
                            f"{key} 缺口处的 NaN 语义必须一致（min_periods）")
        # dist60h：c[r]/max(high[r-1439..r])-1，窗口内任一缺口 → NaN
        naive = np.full(len(keep_rel), np.nan)
        for k, r in enumerate(keep_rel):
            r = int(r)
            win = [hmap[i] for i in range(r - 1439, r + 1) if i in hmap]
            if len(win) == 1440 and r in cmap:
                naive[k] = cmap[r] / max(win) - 1.0
        got = self.npz0["dist60h"]
        fin = np.isfinite(naive)
        np.testing.assert_allclose(got[fin], naive[fin], rtol=1e-4, atol=1e-6,
                                   err_msg="dist60h 全量对账失配")
        self.assertTrue(np.array_equal(np.isfinite(got), fin),
                        "dist60h 缺口必须 NaN（不得用更少根数顶替）")

    def test_04_gap_hours_are_nan(self):
        """时间缺口（缺失小时）处特征必须 NaN，不得静默用更少根数顶替。"""
        ts = self.arr[:, 0].astype(np.int64)
        start = int(ts[0])
        rel = ((ts - start) // HOUR).astype(np.int64)
        n_grid = int(rel[-1]) + 1
        missing = sorted(set(range(n_grid)) - set(int(r) for r in rel))
        if not missing:
            self.skipTest("该样本无缺口（抽样保证已尽量找有缺口对象）")
        # 缺口之后第一个实有根：其 ret30d 依赖缺失端 → NaN 或由远离缺口的窗覆盖
        first_after = int(rel[rel > missing[0]][0]) if (rel > missing[0]).any() else None
        self.assertIsNotNone(first_after)
        # ret30d 窗 [r-720, r] 含缺口 → 该格必须 NaN（worker 用 shift+min_periods 保证）
        hour_idx = self.npz0["hour_idx"]
        abs_hour = (ts - self.m.MAIN_START_MS) // HOUR
        keep_mask = ((abs_hour + 1 >= 0) & (abs_hour + 1 < int(self.m.N_HOURS)))
        keep_rel = rel[keep_mask]
        pos = [k for k, r in enumerate(keep_rel)
               if first_after - 720 <= int(r) <= first_after + 720]
        hit = [k for k in pos if not np.isfinite(self.npz0["ret30d"][k])]
        self.assertTrue(hit, "跨缺口的 ret30d 窗至少一格必须 NaN")

    def test_05_lag0_equals_lag1_worker_output(self):
        """两个延迟情景：worker 计算与 lag 无关（lag 在下游时钟应用）→ 逐数组一致；
        若不一致说明为单特征另造了有利时钟。"""
        for key in self.npz0.files:
            np.testing.assert_array_equal(self.npz0[key], self.npz1[key],
                                          err_msg=f"lag0/lag1 的 {key} 不一致")


# ============================================ C3 事件起点标签 vs 直接未来结果
class TestC3LabelVsDirectForward(_RequireR4):
    """5.2.2：同一 fixture 下，事件起点标签（_build_labels）与从当时参考价出发的
    直接结果（forward_from_series_r4）必须可以分歧——分歧小时就是「标签≠机会」。"""

    def test_label_true_but_forward_window_cannot_reach_2x(self):
        """边界小时 s-720：标签窗口 [s-720, s-1] 恰含它 → 标签 true；
        但它自己的 30 天窗 [s-720, s) 不含事件起点 s → 真实触及 <2×。"""
        H = self.H
        s = 800
        closes = [100.0] * s + [260.0] * 200        # 事件起点 s 处 2.6×
        events = [{"symbol": "XUSDT",
                   "start_ms": H.MAIN_START_MS + s * HOUR, "tier": 2.5}]
        labels = H._build_labels(events, {"XUSDT": 0}, np.array([True]))
        E, n_in = labels["tier2"]
        self.assertEqual(n_in, 1)
        boundary = s - 720                          # = -20 → 越界，改用窗口内首格
        lo = max(0, s - 720)
        self.assertTrue(bool(E[lo, 0]), "标签窗口首格必须 true")
        # 直接未来结果：从 lo 小时的参考价出发，30 天窗 [lo, lo+720) 不含 s（s=lo+720 恰被排除）
        c = naive_klines(closes)
        fn = getattr(H, "forward_from_series_r4", None)
        f = fn(c, float(c[lo, 0]), HOUR, windows={"30d": 720})
        self.assertLess(f["30d"]["max_touch"], 2.0,
                        "标签 true 的小时，从当时价出发 30 天窗不得冒充 2× 机会")
        # 对照：窗口内后段小时（如 lo+100）其窗含事件起点 → 真实可达 2.6×
        f2 = fn(c, float(c[lo + 100, 0]), HOUR, windows={"30d": 720})
        self.assertGreaterEqual(f2["30d"]["max_touch"], 2.0,
                                "窗口含事件起点的小时是真实机会，不得被标签机制抹掉")


if __name__ == "__main__":
    unittest.main(verbosity=2)
