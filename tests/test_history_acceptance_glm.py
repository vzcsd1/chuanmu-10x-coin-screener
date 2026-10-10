"""历史双向研究 · glm 独立验收测试（2026-10-10）

归属：tasks/glm_history_acceptance_20261009.md 允许 glm 写的本文件。
对象：tools/history_research_ds.py（冻结版 SHA bbbaa689...，交接单 reports/history_research_ds/ready_for_glm.md）。

原则：全部期望值来自手算或独立实现（生产 binance_box_strategy = r2 已验收的独立尺），
不得把 ds 函数调两遍当验证。功能缺失显式失败，不用 skip 掩盖。

手算契约（读冻结源码 lines 315-606/907-936 钉住）：
- detect_events: m30[t] = max(high[t+1 .. t+720]) / close[t]（前瞻不含当根！）；
  W={t: m30>=2}；连续游程成段、间隔 <=48h 合并；EVENT_TIERS=(10,5,3,2.5,2) 降序取首个满足；
  mature_30d = 事件起点处窗口走满（n>720 的前 n-720 根）；min_periods=1 → 末端不足窗仍报已发生值。
- forward_path: seg = candles[ref+1 .. ref+win]（不含 ref 根）；[最高价倍数,最高收盘倍数,最低/参考-1,走满]；
  seg 空 → [None,None,None,0]（无后续=未知，不是 0 倍也不是失败）。
- evaluate: adopted=decision-1h 精确匹配；n_closed<BOX+3-1(26) → short；MIN_SCORE=5；
  has_futures=False 时合约侧=0 且 status=-1（metrics 不得冒充）；状态 0sel/1below/2short/3gap/4nodata。
- rank_matrix: argsort(-vals) stable → 并列按列序(symbol 升序)；不可评=32767；分母=total>=0。

运行：PYTHONPATH=. py -3.10 -B -m unittest tests.test_history_acceptance_glm -v
"""
import sys
import unittest
from pathlib import Path

import numpy as np

import tools.history_research_ds as H

HOUR = 3_600_000
T0 = (1_700_000_000_000 // HOUR) * HOUR  # 整点锚 2023-11-14T22:00:00Z

# 独立尺：生产评分（r2 已验收与 ds 逐字段一致——这里作为"独立算法"来源之一）
import binance_box_strategy as B
CFG = B.Config()


def klines(closes, highs=None, lows=None, vols=None, t0=T0):
    n = len(closes)
    highs = highs or closes
    lows = lows or closes
    vols = vols or [100.0] * n
    return np.array([[t0 + i * HOUR, closes[i], highs[i], lows[i], closes[i], vols[i]]
                     for i in range(n)], dtype=float)


def flat(n, price=100.0):
    return klines([price] * n)


# ----------------------------------------------------------- 反例 1：盘中高不算 ----

class TestIntrabarHighNotFuture(unittest.TestCase):
    """任务书反例 1：当根盘中高 250、收盘 100，之后全 100 —— 不得生成"信号后 2.5x"。"""

    def test_forward_path_excludes_ref_candle_high(self):
        c = flat(90)
        c[70, 2] = 250.0  # 当根 high=250, close=100
        fp = H.forward_path(c, 70)
        self.assertEqual(fp["30d"][0], 1.0, "盘中高在 ref 根内 → 剩余最高价倍数必须=1.0")
        self.assertEqual(fp["30d"][3], 0, "窗口未走满 → 未到期标记")
        self.assertEqual(fp["ref_close"], 100.0)

    def test_intrabar_high_before_ref_is_real_future_wave(self):
        # 250 对之前的小时是真实未来高（30 天窗内真实到达）→ 事件成立是正确语义
        c = flat(90)
        c[70, 2] = 250.0
        ev = H.detect_events(c)
        self.assertEqual(len(ev), 1, "手算：inwave=t0..t69（m=2.5），游程连续 → 1 事件")
        self.assertEqual(ev[0]["start_ms"], int(c[0, 0]), "事件起点在盘中高之前（真未来高）")
        self.assertAlmostEqual(ev[0]["max_m30"], 2.5, places=4)
        self.assertEqual(ev[0]["tier"], 2.5)
        self.assertFalse(ev[0]["mature_30d"], "90 根 < 720 → 未到期，不算失败")

    def test_real_future_rise_to_250_is_detected(self):
        c = flat(90)
        c[75] = [c[75, 0], 250.0, 250.0, 250.0, 250.0, 100.0]  # 真涨：未来 5 小时 close=250
        ev = H.detect_events(c)
        self.assertEqual(len(ev), 1)
        self.assertAlmostEqual(ev[0]["max_m30"], 2.5, places=4, msg="真未来涨 2.5x 必须识别")

    def test_flat_series_no_events(self):
        self.assertEqual(H.detect_events(flat(90)), [])


# ----------------------------------------------------------- 反例 2：未来信息隔离 ----

class TestFutureIsolation(unittest.TestCase):
    """任务书反例 2：改截止后数据，截止时点输出不变；反向检测器可读未来。"""

    def _rows61(self):
        """61 根生产手算锚（idx58 突破+趋势，与生产 box_score 8 分锚一致）。"""
        rows = []
        for i in range(61):
            ts = T0 + i * HOUR
            if i == 58:
                rows.append([ts, 100.0, 110.0, 100.0, 110.0, 300.0])
            else:
                v = 200.0 if 34 <= i <= 57 else 100.0
                rows.append([ts, 100.0, 100.0, 100.0, 100.0, v])
        return np.array(rows, dtype=float)

    def test_tampering_after_decision_changes_nothing(self):
        rows = self._rows61()
        d = np.array([int(rows[59, 0])])  # decision = idx59 开盘 → 采用 idx58（手算 8 分）
        out_a = H.evaluate("X", rows, np.empty(0), np.empty(0), np.empty(0),
                           False, d)
        rows2 = rows.copy()
        rows2[60] = [rows2[60, 0], 500.0, 9999.0, 1.0, 9999.0, 1e9]  # decision 之后唯一网格根
        extra = np.array([[int(rows[-1, 0]) + (i + 1) * HOUR, 1.0, 1e6, 0.1, 1e6, 0.0]
                          for i in range(200)])
        rows2 = np.vstack([rows2, extra])
        out_b = H.evaluate("X", rows2, np.empty(0), np.empty(0), np.empty(0),
                           False, d)
        for key in out_a:
            np.testing.assert_array_equal(out_a[key], out_b[key],
                                          err_msg=f"改 decision 之后 K 线不得影响输出 [{key}]")
        self.assertEqual(int(out_a["score_kline"][0]), 8, "手算锚：采用 idx58 → 8 分")
        self.assertTrue(bool(out_a["selected"][0]), "8 >= 5 → selected")

    def test_metrics_after_decision_ignored(self):
        rows = self._rows61()
        d = np.array([int(rows[59, 0])])
        dt = np.array([T0 - k * HOUR for k in range(79, -1, -1)], dtype=np.int64)
        oi = np.array([150.0 if k <= 22 else 100.0 for k in range(79, -1, -1)])
        ls = np.array([0.8] * 80)
        out_a = H.evaluate("X", rows, dt, oi, ls, True, d)
        # 在 decision 之后追加 metrics 行 → 输出必须不变（appended 必须真在 decision 之后）
        dt2 = np.concatenate([dt, [int(rows[-1, 0]) + 2 * HOUR, int(rows[-1, 0]) + 3 * HOUR]])
        oi2 = np.concatenate([oi, [999.0, 1e9]])
        ls2 = np.concatenate([ls, [0.1, 5.0]])
        out_b = H.evaluate("X", rows, dt2, oi2, ls2, True, d)
        for key in ("score_deriv", "oi_chg_1d", "ls_top", "deriv_status"):
            np.testing.assert_array_equal(out_a[key], out_b[key],
                                          err_msg=f"decision 后 metrics 行不得影响 [{key}]")
        self.assertEqual(int(out_a["score_deriv"][0]), 10,
                         "手算：oi_chg=0.5(+4)+0.5(+3)+ls 0.8<1(+3)=10")
        self.assertEqual(int(out_a["score_total"][0]), 18, "8(K线) + 10(合约) = 18")

    def test_event_detector_reads_future_by_design(self):
        # 对照组（设计行为，非缺陷）：反向事件检测器读全序列（含未来）
        c1 = flat(90)
        c2 = flat(90)
        c2[80] = [c2[80, 0], 100.0, 260.0, 100.0, 260.0, 100.0]
        self.assertEqual(H.detect_events(c1), [])
        self.assertEqual(len(H.detect_events(c2)), 1,
                         "反向事件检测器读未来（全序列）是设计行为")


# ----------------------------------------------------------- 反例 3：剩余机会 ----

class TestRemainingOpportunity(unittest.TestCase):
    """任务书反例 3：半途信号剩余 2.6x 照实；过顶后不补记；无后续=未知。"""

    def setUp(self):
        # [100]*720 + [260]*780，n=1500：700 处收盘 100，之后 2.6x
        self.c = klines([100.0] * 720 + [260.0] * 780)

    def test_mid_signal_keeps_remaining(self):
        fp = H.forward_path(self.c, 700)
        self.assertAlmostEqual(fp["30d"][0], 2.6, places=4, msg="半途信号剩余 2.6x 照实记录")
        self.assertEqual(fp["30d"][3], 1, "ref+720=1420 <= 1499 → 窗口走满")
        self.assertAlmostEqual(fp["7d"][0], 2.6, places=4)

    def test_after_peak_no_backfilled_hit(self):
        fp = H.forward_path(self.c, 900)  # close 已到 260
        self.assertAlmostEqual(fp["30d"][0], 1.0, places=4, msg="峰值已成过去 → 不得补记命中")

    def test_no_future_data_is_unknown_not_zero(self):
        fp = H.forward_path(self.c, 1499)
        self.assertEqual(fp["30d"], [None, None, None, 0], "无后续=未知，不是 0 倍也不是失败")

    def test_90d_partial_window_reports_partial_maturity(self):
        fp = H.forward_path(self.c, 700)
        self.assertEqual(fp["90d"][3], 0, "700+2160=2860 > 1499 → 90d 未走满")
        self.assertAlmostEqual(fp["90d"][0], 2.6, places=4, msg="未走满仍报已发生值")


# ----------------------------------------------------------- 反例 4：事件关联 ----

class TestEventAssociation(unittest.TestCase):
    """任务书反例 4：重复/中断/接近/慢涨/不回踩，按冻结规则手工对账。"""

    def _mk(self, closes):
        return klines(closes)

    def test_two_waves_far_apart_stay_two(self):
        ev = H.detect_events(self._mk([100.0] * 720 + [260.0] * 24 + [180.0] * 936 + [400.0] * 824))
        # 手算（新语义窗口 [t+1..t+720]）：W1=[0..719] m=2.6；W2=[960..1679] m=2.22；间隔 241h>48h
        self.assertEqual(len(ev), 2)
        self.assertEqual(ev[0]["tier"], 2.5)
        self.assertEqual(ev[1]["tier"], 2.0)
        self.assertEqual(ev[0]["n_segments"], 1)

    def test_gap_within_48h_merges_with_n_segments(self):
        ev = H.detect_events(self._mk([100.0] * 720 + [260.0] * 24 + [180.0] * 736 + [400.0] * 1024))
        # 手算：W1=[0..719]，W2=[760..1479]，间隔 41h<=48h → 1 事件、2 个原始段
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["n_segments"], 2, "48h 合并须记录本事件内分段数")

    def test_slow_grind_single_event_immature_no_pullback(self):
        closes = [100.0 + 160.0 * i / 480 for i in range(480)]
        ev = H.detect_events(self._mk(closes))
        # 手算：W=[0..90]（close<=130），单一连续游程 → 1 事件（91 个 inwave 小时=重复提醒不加分）
        self.assertEqual(len(ev), 1, "重复提醒不得增加独立赢家数")
        self.assertEqual(ev[0]["tier"], 2.5)
        self.assertFalse(ev[0]["mature_30d"], "480<720 → 未到期不算失败")
        self.assertEqual(ev[0]["max_drawdown_to_peak"], 0.0, "不回踩直接涨 → 回撤 0")
        self.assertFalse(ev[0]["new_high_after_20pct_dd"])

    def test_repeat_signals_one_event_many_hours(self):
        # 同一波内重复提醒：事件数=1，但"选中小时/提醒数"是另一个口径（聚合层分开计）
        closes = [100.0 + 160.0 * i / 480 for i in range(480)]
        ev = H.detect_events(self._mk(closes))
        # 手算 span：W=[0..89]（t=90 处 max/close = 259.67/130 = 1.997 < 2）→ b-a+1 = 90
        self.assertEqual(ev[0]["span_hours"], 90)


# ----------------------------------------------------------- 反例 5：分母与状态 ----

class TestDenominatorStatuses(unittest.TestCase):
    """任务书反例 5：未入选/跳过/缺资料不丢；无合约不冒充合约分。"""

    def _rows61(self):
        return TestFutureIsolation()._rows61()

    def test_status_mapping_all_five(self):
        rows = self._rows61()
        d59 = np.array([int(rows[59, 0])])
        # selected
        o = H.evaluate("X", rows, np.empty(0), np.empty(0), np.empty(0), False, d59)
        self.assertEqual(int(o["status"][0]), 0)
        # scored_below：全平 61 根 → 0 分
        flat61 = flat(61)
        o2 = H.evaluate("X", flat61, np.empty(0), np.empty(0), np.empty(0), False, d59)
        self.assertEqual(int(o2["status"][0]), 1)
        self.assertEqual(int(o2["score_kline"][0]), 0, "全平 → 手算 0 分（无突破/趋势/量增）")
        # short：20 根闭合
        d19 = np.array([int(rows[19, 0])])
        o3 = H.evaluate("X", rows[:20], np.empty(0), np.empty(0), np.empty(0), False, d19)
        self.assertEqual(int(o3["status"][0]), 2, "n_closed=19 < 26 → 预热不足")
        self.assertEqual(int(o3["score_kline"][0]), -1, "未评分= -1，不得冒充 0 分")
        # gap：删除被采用根
        rows_nogap = np.delete(rows, 58, axis=0)
        o4 = H.evaluate("X", rows_nogap, np.empty(0), np.empty(0), np.empty(0), False, d59)
        self.assertEqual(int(o4["status"][0]), 3, "精确匹配失败但有更早行 → gap")
        # nodata：decision 早于全部
        d_early = np.array([T0 - 10 * HOUR])
        o5 = H.evaluate("X", rows, np.empty(0), np.empty(0), np.empty(0), False, d_early)
        self.assertEqual(int(o5["status"][0]), 4)

    def test_no_futures_ignores_metrics(self):
        rows = self._rows61()
        d59 = np.array([int(rows[59, 0])])
        dt = np.array([T0 - k * HOUR for k in range(79, -1, -1)], dtype=np.int64)
        oi = np.array([150.0 if k <= 22 else 100.0 for k in range(79, -1, -1)])
        ls = np.array([0.8] * 80)
        o = H.evaluate("X", rows, dt, oi, ls, False, d59)
        self.assertEqual(int(o["score_deriv"][0]), 0, "无合约市场 → 合约分=0")
        self.assertEqual(int(o["deriv_status"][0]), -1, "metrics 给了也不得冒充（status=-1）")
        self.assertEqual(int(o["score_total"][0]), 8, "total=K线分，不混入合约")

    def test_missing_oi_history_not_fake_complete(self):
        rows = self._rows61()
        d59 = np.array([int(rows[59, 0])])
        dt = np.array([T0 - 72 * HOUR, T0 - 24 * HOUR, T0], dtype=np.int64)  # 3 个点
        oi = np.array([100.0, 100.0, 150.0])
        ls = np.array([0.8, 0.8, 0.8])
        o = H.evaluate("X", rows, dt, oi, ls, True, d59)
        # ⚠️ 契约缺口 F1（钉住实际行为，交主代理裁定）：
        #   生产 deriv_as_of 要求 oi_valid >= 25 才算 oi_chg（不足=不可算，不给分）；
        #   ds deriv_scores 的 pts 只看 isfinite(oi_chg)，<25 观测照样拿满 OI +7 分，
        #   仅把 status 降为 partial(1)。下面 3 个点的最小反例实测 score_deriv=10（非 3）。
        self.assertEqual(int(o["score_deriv"][0]), 10,
                         "F1 缺口锚：3 个 OI 点 <25 仍拿 oi 4+3 + ls 3 = 10（生产应为 3）")
        self.assertEqual(int(o["deriv_status"][0]), 1, "status=partial（缺口只作用于 status）")
        self.assertFalse(np.isnan(o["oi_chg_1d"][0]), "缺口证据：oi_chg 在 <25 观测下仍有值")


# ----------------------------------------------------------- 反例 6：排名与对照 ----

class TestRankingAndAttention(unittest.TestCase):
    """任务书反例 6：并列/随机对照/全范围分母/关注曲线手算。"""

    def test_ties_break_by_symbol_column_order(self):
        total = np.array([[5, 5], [3, -1]], dtype=np.int16)
        rk = H.rank_matrix(total)
        self.assertEqual(rk[0].tolist(), [1, 2], "并列按列序（symbol 升序）")
        self.assertEqual(rk[1].tolist(), [1, 32767], "不可评=32767，不挤占名次")

    def test_random_rank_deterministic_and_permutation(self):
        total = np.array([[5, 3, -1, 4, 2, 0, 7, 1]], dtype=np.int16)
        r1 = H.random_rank_matrix(20261009, total)
        r2 = H.random_rank_matrix(20261009, total)
        np.testing.assert_array_equal(r1, r2, "同 seed 必须逐格相同")
        self.assertEqual(int(r1[0][2]), 32767, "不可评=32767")
        # ⚠️ 契约缺口 F2（钉住实际行为）：random 给【所有】列掷随机数后再排名，
        #   不可评列的随机名次槽位被 32767 覆盖 → 可评分位出现空洞（本例缺 7、含 8）。
        #   影响：随机对照 rank<=N 计数被系统性压低 → 抬高 score 相对 lift（反保守）。
        scoreable = total[0] >= 0
        vals = sorted(int(r1[0][i]) for i in range(8) if scoreable[i])
        self.assertEqual(vals, [1, 2, 3, 4, 5, 6, 8],
                         "F2 缺口锚：可评分位={1..8}\\{被不可评列占走的槽}，非稠密 1..7")

    def test_random_rank_squeeze_minimal_counterexample(self):
        # F2 最小反例：3 列、1 列不可评。seed=20261014 时随机名次 -1 列占 rank1 →
        # 覆写 32767 后可评分位={2,3}，rank1 凭空消失（5 分对象被挤到第 2）。
        total = np.array([[5, -1, 4]], dtype=np.int16)
        rk = H.random_rank_matrix(20261014, total)
        self.assertEqual(rk[0].tolist(), [2, 32767, 3],
                         "F2：不可评列挤占 rank1 → 可评对象名次整体后移")
        # 对照：rank_matrix（按 total 排序）不挤占——-1 恒排最后
        rk2 = H.rank_matrix(total)
        self.assertEqual(rk2[0].tolist(), [1, 32767, 2], "正式排名不受影响（-1 恒最后）")

    def test_rank_full_range_denominator(self):
        # 3 小时 × 2 对象：手算 rank 矩阵；前 N 名单规模按小时并集去重
        total = np.array([[5, 3], [9, 0], [1, 2]], dtype=np.int16)
        rk = H.rank_matrix(total)
        self.assertEqual(rk.tolist(), [[1, 2], [1, 2], [2, 1]])
        n1 = len({j for h in range(3) for j in range(2) if rk[h][j] <= 1})
        self.assertEqual(n1, 2, "N=1 去重名单={A,B}=2（手算）")

    def test_screened_in_vs_ranked_distinct(self):
        # 过门槛入选（total>=5）与全范围排名是两套统计：-1 不可评仍保留在状态里
        total = np.array([[8, 3], [-1, 6]], dtype=np.int16)
        screened = (total >= 5)
        self.assertEqual(screened.sum(), 2, "门槛入选：8 和 6")
        rk = H.rank_matrix(total)
        self.assertEqual(int(rk[0].min()), 1, "全范围排名包含 3 分对象（不先筛后排）")
        self.assertEqual(int(rk[1][0]), 32767, "不可评格不参与排名但状态保留")


# ----------------------------------------------------------- 反例 7：时间切分与成熟 ----

class TestTimeSplitMaturity(unittest.TestCase):
    """任务书反例 7：窗口跨界分别处理；未走满=未到期；30/90 天不混。"""

    def test_windows_independent_maturity(self):
        c = klines([100.0] * 720 + [260.0] * 780)  # n=1500
        fp = H.forward_path(c, 700)
        self.assertEqual((fp["7d"][3], fp["30d"][3], fp["90d"][3]), (1, 1, 0),
                         "7d/30d 走满、90d 未走满 —— 三窗分别记录")

    def test_event_maturity_at_start_not_max(self):
        # 事件起点 a 的窗口走满性决定 mature_30d（不是段内最大倍数处）
        c = klines([100.0] * 720 + [260.0] * 24 + [100.0] * 80)  # n=824 < 起点0+720? 824>721
        ev = H.detect_events(c)
        self.assertEqual(len(ev), 1)
        # 起点 t=0：窗口 [1..720] 走满（824>720）→ mature=True；max_m30=2.6
        self.assertTrue(ev[0]["mature_30d"])
        self.assertAlmostEqual(ev[0]["max_m30"], 2.6, places=4)

    def test_partial_window_event_reports_immature(self):
        c = klines([100.0] * 100 + [260.0] * 24)  # n=124：全部窗口未走满
        ev = H.detect_events(c)
        self.assertEqual(len(ev), 1)
        self.assertFalse(ev[0]["mature_30d"], "窗口未走满 → 未到期，不得当失败")


# ----------------------------------------------------------- 反例 8：保留率 vs 绝对覆盖 ----

def retention_vs_coverage(orig_winners, kept_from_orig, new_winners, market_winners):
    """任务书反例 8 手算样例（glm 独立实现，供聚合报告复用）。
    返回 dict：保留率与市场绝对覆盖分开；新增/新漏分开列。"""
    retained = kept_from_orig
    dropped_orig = orig_winners - kept_from_orig
    total_caught = kept_from_orig + new_winners
    missed_total = market_winners - total_caught
    return {
        "retention_of_orig": retained / orig_winners,
        "absolute_market_coverage": total_caught / market_winners,
        "new_winners": new_winners,
        "dropped_orig_winners": dropped_orig,
        "missed_total": missed_total,
    }


class TestRetentionVsAbsoluteCoverage(unittest.TestCase):
    """任务书反例 8："原版抓 10、新版保留 9、市场共 50" 的样例。"""

    def test_example_from_task_card(self):
        r = retention_vs_coverage(orig_winners=10, kept_from_orig=9,
                                  new_winners=3, market_winners=50)
        self.assertAlmostEqual(r["retention_of_orig"], 0.9, places=9)
        self.assertAlmostEqual(r["absolute_market_coverage"], 12 / 50, places=9)
        self.assertEqual(r["new_winners"], 3)
        self.assertEqual(r["dropped_orig_winners"], 1)
        self.assertEqual(r["missed_total"], 38, "市场漏掉 50-12=38（原版漏 40，新版少漏 2）")
        # 两个比例不得混报：保留率 90% 远高于绝对覆盖 24%
        self.assertGreater(r["retention_of_orig"], r["absolute_market_coverage"])


if __name__ == "__main__":
    unittest.main()
