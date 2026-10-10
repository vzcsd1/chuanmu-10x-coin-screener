"""B4 独立验收测试（glm · 2026-10-09，r2 准备轮更新）

归属：tasks/glm_b4_stage1_20261009.md 与 tasks/b4_stage1_r2_20261009.md 允许 glm 编辑的本文件。

Part A：生产契约手算边界（与 ds 无关，交接前后都必须通过）。
Part B：对 tools/b4_replay_ds.py 的接线与对抗断言。手算预期独立钉住（防两边同错），
功能缺失一律显式失败，不用 skip 掩盖。

版本基准（2026-10-09 记录；2026-10-09 验收轮更新）：
- r1 冻结版 SHA 7f6373f4...：主代理裁定 D1-D4 缺陷的行为证据见
  reports/b4_stage1_review/probe_results.json → 对 r1 本文件的 D1-D4 断言应当 FAIL。
- 2026-10-09 准备轮快照（已过时，仅追溯）：当时记录"r2 交接尚未出现"。
  r2 交接已于 2026-10-09 正式出现（reports/b4_stage1_ds/r2/ready_for_glm.md），
  目标版本：工具 e4c6e9af...、生产 d1cc20d8...；对目标版本跑出的绿即验收证据，
  详见 reports/b4_stage1_glm/r2/summary.md。

预期值全部手算 + 生产源码（binance_box_strategy.py）：
- box_score 读 df.iloc[-2]、量增窗口 iloc[:-1]，最后一行被完全忽略；
  只喂收盘 K 线会静默多退一根（需要"进行中"尾行）。
- deriv_score 阈值 OI_1D>=0.10、OI_3D>=0.20、ls_top<1.0；缺失/NaN/bool 不计分。
- OI 契约：有限数且>0 过滤、至少 25 个有效观测才计算增速、1d/3d 基准=采用点前
  <=24h/<=72h 最近可用值、接口窗口最近 200 根（DERIV_LOOKBACK）；LS 只认
  topLongShortPositionRatio，账户比不得冒充。

运行：PYTHONPATH=. py -3.10 -B -m unittest tests.test_b4_acceptance_glm -v
"""

import math
import sys
import unittest
from pathlib import Path

import pandas as pd

import binance_box_strategy as base

HOUR = 3_600_000
# 整点锚：2023-11-14T22:00:00Z。必须选整点——ds 的 deriv_as_of 按 minute==0 过滤
# metrics 行（r1 行为），非整点锚会让全部行被过滤、以无关原因"通过"或"失败"。
T0 = (1_700_000_000_000 // HOUR) * HOUR
CFG = base.Config()

TIME_CASES = [
    # (标签, 原始输入, 期望 UTC ms)——1_700_000_000 秒 == 2023-11-14T22:13:20Z（非整点，
    # 仅用于测 normalize_epoch_ms 的刻度换算，与 T0 锚无关）
    ("millis", 1_700_000_000_000, 1_700_000_000_000),
    ("seconds", 1_700_000_000, 1_700_000_000_000),
    ("micros", 1_700_000_000_000_000, 1_700_000_000_000),
    ("iso_z", "2023-11-14T22:13:20Z", 1_700_000_000_000),
    ("naive_utc", "2023-11-14 22:13:20", 1_700_000_000_000),  # 无时区字符串必须按 UTC
]
TIME_INVALID_CASES = [
    ("none", None), ("empty", ""), ("garbage", "abc"), ("zero", 0),
    ("too_small", 5e7), ("bool", True), ("inf", float("inf")),
]


def make_rows():
    """61 根 1h K 线，手算锚点：
    - idx0..33: ohlc=100, vol=100
    - idx34..57: ohlc=100, vol=200
    - idx58: o=100 h=110 l=100 c=110, vol=300  ← 突破+趋势锚
    - idx59, idx60: ohlc=100, vol=100         ← 非突破锚
    手算（EMA span=50, box=24；closed-only 喂入 len=n 时 box_score 评第 n-2 根）：
      n=59 → 评 idx57：仅 v24up（4800>2400）→ 1 分
      n=60 → 评 idx58：breakout+c_trend+v24up（4900>2500）→ 2+5+1=8 分
      n=61 → 评 idx59：100<box_high59(=110)、100<ema59、v24up(4800>2600) → 1 分
    """
    rows = []
    for i in range(61):
        ts = T0 + i * HOUR
        if i == 58:
            rows.append([ts, 100.0, 110.0, 100.0, 110.0, 300.0])
        else:
            vol = 200.0 if 34 <= i <= 57 else 100.0
            rows.append([ts, 100.0, 100.0, 100.0, 100.0, vol])
    return rows


def make_rows_26():
    """26 根闭合 K 线：idx25 突破+趋势。手算：生产 indicators(26 闭合+占位行)=27 行
    ≥ box_period+3，允许评分；box_high25=100、close25=110 → breakout(2)+trend(5)=7 分；
    量增窗口仅 26 行 <48 → v24up 不可能。这是 D2 的边界预期。"""
    rows = []
    for i in range(25):
        rows.append([T0 + i * HOUR, 100.0, 100.0, 100.0, 100.0, 100.0])
    rows.append([T0 + 25 * HOUR, 100.0, 110.0, 100.0, 110.0, 100.0])
    return rows


def oi_row(ts, value):
    return {"sumOpenInterestValue": value, "timestamp": ts}


def ls_row(ts, ratio):
    return {"longShortRatio": ratio, "timestamp": ts}


class FakeFutures:
    """离线假合约接口：只实现 _fetch_deriv_context_live 触及的方法，零网络。"""

    def __init__(self, oi_rows=None, ls_rows=None, fail_position=False,
                 account_rows=None):
        self._oi = oi_rows or []
        self._ls = ls_rows or []
        self._account = account_rows or []
        self._fail_position = fail_position

    def market(self, symbol):
        return {"id": "XUSDT"}

    def fapiDataGetOpenInterestHist(self, params):
        return [dict(r) for r in self._oi]

    def fapiDataGetTopLongShortPositionRatio(self, params):
        if self._fail_position:
            raise RuntimeError("position endpoint down (test)")
        return [dict(r) for r in self._ls]

    def fetch_long_short_ratio_history(self, *args, **kwargs):
        return [dict(r) for r in self._account]


OI_FIELD = "sum_open_interest_value"
LS_FIELD = "sum_toptrader_long_short_ratio"


def metrics_df(rows):
    """rows: (epoch_ms, oi_value, oi_amount, ls_top, ls_account, ls_global)。"""
    data = {
        "dt": [pd.Timestamp(ms, unit="ms", tz="UTC") for ms, *_ in rows],
        OI_FIELD: [r[1] for r in rows],
        "sum_open_interest": [r[2] for r in rows],
        LS_FIELD: [r[3] for r in rows],
        "count_toptrader_long_short_ratio": [r[4] for r in rows],
        "count_long_short_ratio": [r[5] for r in rows],
    }
    return pd.DataFrame(data)


def metric_row(k_hours_ago, oi_value=100.0, ls_top=0.8):
    ms = T0 - k_hours_ago * HOUR
    return (ms, oi_value, oi_value * 0.001, ls_top, 1.5, 1.8)


def strong_deriv_metrics():
    """≥25 个有效 OI 观测 + 正常 LS：手算 oi_chg_1d=150/100-1=0.5（命中 +4）、
    oi_chg_3d=0.5（命中 +3）、ls_top=0.8<1（命中 +3）→ deriv_score=10。"""
    rows = [metric_row(k, 150.0 if k <= 22 else 100.0) for k in range(79, -1, -1)]
    return metrics_df(rows)


# ----------------------------------------------------------- Part A ----

class TestCandleAdoptionContract(unittest.TestCase):
    """Part A-1：box_score 采用点契约（多退一根 / 尾行忽略）。"""

    def test_closed_only_feed_scores_second_to_last(self):
        rows = make_rows()
        expected = {59: 1, 60: 8, 61: 1}
        for n, want in expected.items():
            score, details = base.box_score(base.indicators(rows[:n], CFG), CFG)
            self.assertEqual(score, want, f"len={n} 应评第 {n-2} 根，得 {want} 分")
            if want == 1:
                self.assertNotIn("c_breakout", details)
                self.assertNotIn("c_trend", details)
                self.assertIn("c_v24up", details)
            if want == 8:
                self.assertIn("c_breakout", details)
                self.assertIn("c_trend", details)
                self.assertIn("c_v24up", details)

    def test_trailing_row_content_ignored(self):
        """尾行（进行中 K 线）无论填什么值都不改变评分：未来最终值偷不进来。"""
        rows = make_rows()
        base_rows = rows[:59]
        dummy = [T0 + 59 * HOUR, 1e9, 1e9, 1.0, 1e9, 0.0]
        partial = [T0 + 59 * HOUR, 100.0, 105.0, 100.0, 105.0, 123.0]
        final = [T0 + 59 * HOUR, 100.0, 130.0, 95.0, 130.0, 9999.0]
        scores = [base.box_score(base.indicators(base_rows + [tail], CFG), CFG)[0]
                  for tail in (dummy, partial, final)]
        self.assertEqual(scores, [8, 8, 8], "尾行内容不得影响评分")

    def test_one_extra_row_shifts_scored_candle(self):
        rows = make_rows()
        dummy = [T0 + 61 * HOUR, 100.0, 100.0, 100.0, 100.0, 100.0]
        score_a, _ = base.box_score(base.indicators(rows[:59] + [dummy], CFG), CFG)
        score_b, _ = base.box_score(base.indicators(rows[:60] + [dummy], CFG), CFG)
        self.assertEqual((score_a, score_b), (8, 1))


class TestDerivScoreContract(unittest.TestCase):
    """Part A-2：合约侧计分的缺失语义与阈值方向。"""

    def test_missing_is_not_zero(self):
        self.assertEqual(base.deriv_score(), (0, {}))
        score, details = base.deriv_score(oi_chg_1d=None, oi_chg_3d=None,
                                          ls_top=None, ls_chg_3d=None)
        self.assertEqual(score, 0)
        self.assertEqual(details, {})

    def test_thresholds_direction_and_boundaries(self):
        score, details = base.deriv_score(oi_chg_1d=0.15, ls_top=0.8, ls_chg_3d=0.5)
        self.assertEqual(score, 7)
        self.assertIn("hit_oi_1d", details)
        self.assertIn("hit_ls_top", details)
        self.assertNotIn("oi_chg_3d", details)
        self.assertIn("ls_chg_3d", details)
        self.assertEqual(base.deriv_score(oi_chg_1d=0.10)[0], 4)
        self.assertEqual(base.deriv_score(oi_chg_1d=0.0999)[0], 0)
        self.assertEqual(base.deriv_score(oi_chg_3d=0.20)[0], 3)
        self.assertEqual(base.deriv_score(oi_chg_3d=0.19)[0], 0)
        self.assertEqual(base.deriv_score(ls_top=1.0)[0], 0)
        self.assertEqual(base.deriv_score(ls_top=0.99)[0], 3)

    def test_nan_and_bool_excluded(self):
        self.assertEqual(base.deriv_score(oi_chg_1d=float("nan"))[0], 0)
        self.assertEqual(base.deriv_score(oi_chg_1d=True)[0], 0)
        self.assertEqual(base.deriv_score(ls_top=float("inf"))[0], 0)


class TestDerivContextContract(unittest.TestCase):
    """Part A-3：生产合约契约（OI 基准/≥25 有效/stale_tail/LS 身份，假接口零网络）。"""

    def test_oi_basis_uses_last_value_at_or_before_cutoff(self):
        vals = []
        for k in range(75, -1, -1):
            if k in (23, 24):
                continue
            v = 150.0 if k <= 22 else 100.0
            vals.append(oi_row(T0 - k * HOUR, str(v)))
        vals += [oi_row(T0 + HOUR, "0"), oi_row(T0 + 2 * HOUR, "")]
        ls = [ls_row(T0 - k * HOUR, "0.8") for k in range(0, 80)]
        out = base._fetch_deriv_context_live(FakeFutures(oi_rows=vals, ls_rows=ls),
                                             "X/USDT:USDT", CFG)
        self.assertAlmostEqual(out["oi_chg_1d"], 0.5, places=9)
        self.assertAlmostEqual(out["oi_chg_3d"], 0.5, places=9)
        self.assertEqual(out["oi_as_of"], T0)
        self.assertEqual(out["oi_stale_tail"], 2)
        self.assertEqual(out["ls_source"], "topLongShortPositionRatio")
        self.assertEqual(out["status"], "ok")

    def test_short_or_sparse_oi_history_yields_no_gain(self):
        # 3 个有效 OI 观测：生产要求 ≥25 个有效观测才计算增速
        vals = [oi_row(T0 - 72 * HOUR, "100.0"), oi_row(T0 - 24 * HOUR, "100.0"),
                oi_row(T0, "150.0")]
        ls = [ls_row(T0 - k * HOUR, "0.8") for k in range(0, 80)]
        out = base._fetch_deriv_context_live(FakeFutures(oi_rows=vals, ls_rows=ls),
                                             "X/USDT:USDT", CFG)
        self.assertIsNone(out["oi_chg_1d"], "有效观测 <25 时生产不计算 OI 增速")
        self.assertIsNone(out["oi_chg_3d"])
        self.assertNotEqual(out["status"], "ok")

    def test_short_history_is_not_fake_complete(self):
        vals = [oi_row(T0 - k * HOUR, "150.0" if k <= 22 else "100.0")
                for k in range(29, -1, -1)]
        ls = [ls_row(T0 - k * HOUR, "0.8") for k in range(0, 80)]
        out = base._fetch_deriv_context_live(FakeFutures(oi_rows=vals, ls_rows=ls),
                                             "X/USDT:USDT", CFG)
        self.assertIsNotNone(out["oi_chg_1d"])
        self.assertIsNone(out["oi_chg_3d"], "3 日历史不足不得填值")
        self.assertEqual(out["status"], "partial")

    def test_account_ratio_never_impersonates_top_position_ratio(self):
        vals = [oi_row(T0 - k * HOUR, "150.0" if k <= 22 else "100.0")
                for k in range(0, 76)]
        account = [ls_row(T0 - k * HOUR, "2.5") for k in range(0, 80)]
        out = base._fetch_deriv_context_live(
            FakeFutures(oi_rows=vals, fail_position=True, account_rows=account),
            "X/USDT:USDT", CFG)
        self.assertIsNone(out["ls_top"])
        self.assertEqual(out["ls_global"], 2.5)
        self.assertIn("globalLongShortAccountRatio", out["ls_source"] or "")
        self.assertNotEqual(out["status"], "ok")


# ------------------------------------------- 事件关联规则演示（卡片冻结语义）----

TIER_STEPS = (2.0, 2.5, 3.0, 5.0, 10.0)


def forward_mult_series(closes, highs, horizon_h=24 * 30):
    """m(t) = max(high[t+1 .. t+horizon]) / close_t（2026-10-09 修正，冻结语义）：
    参考时刻 = 第 t 根的收盘时刻；本根盘中高点发生在收盘**前**，不得计入未来
    （修前反例：close=100、本根 high=250、其后全 100 → 旧实现得 2.5，正确为 1.0）。
    未来区间 = 紧随收盘后的 horizon_h 根：索引 t+1 .. t+horizon_h（闭区间）。
    - 窗口走满（t+horizon_h < n）→ mature=True；
    - 有部分未来数据 → 按可得部分照实算，mature=False（未到期，不算失败）；
    - 完全无未来数据（t 为最后一根）→ mult=None（未知），不用 0 或 1 冒充观察结果。"""
    n = len(closes)
    mults, mature = [], []
    for t in range(n):
        end = min(t + horizon_h + 1, n)
        if end <= t + 1:          # 收盘后一根后续数据都没有 → 未知
            mults.append(None)
        else:
            peak = max(highs[t + 1:end])
            mults.append(peak / closes[t] if closes[t] else None)
        mature.append((t + horizon_h) < n)
    return mults, mature


def associate_waves(mults, mature, threshold=2.0, max_gap_h=48):
    """冻结的事件关联规则（实验卡 7.2，2026-10-09 修正版；演示用参考实现）：
    本批冻结口径 = **符合条件起点的连续段 + 间隔合并**：
    1) in-wave 集合 W = {t : m(t) >= threshold}（m=None 即未知，不入选也不算断档失败）；
    2) 连续 W 游程成段；两段间隔 <=max_gap_h → 并入同一事件（48h 为待验证研究设置，
       敏感性=不设间隔，结论翻转须如实报告）；
    3) 事件档位 = max(m(t))，t∈事件，所处梯度（2/2.5/3/5/10；不设入池硬下限）；
    4) 成熟度单独记录：事件内任一小时 30 天窗口未走满 → "未到期"，不算失败。
    注意①：回撤段只有在它本身构成 >=2x 起点时才留在事件内（见演示用例）。
    注意②：这是与实验卡旧文字"重叠 30 天观察窗合并"**不同的算法**——本批演示以
    本实现为准；"重叠窗口合并"如保留只作敏感性对照，两者分母不得混用。"""
    runs, cur = [], []
    for t, m in enumerate(mults):
        if m is not None and m >= threshold:
            cur.append(t)
        elif cur:
            runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)
    events = []
    for run in runs:
        if events and run[0] - events[-1]["end"] <= max_gap_h:
            events[-1]["end"] = run[-1]
            events[-1]["runs"].append(run)
        else:
            events.append({"start": run[0], "end": run[-1], "runs": [run]})
    for ev in events:
        peak_mult = max(mults[t] for r in ev["runs"] for t in r)
        ev["tier"] = max((s for s in TIER_STEPS if peak_mult >= s), default=None)
        ev["mature"] = all(mature[t] for r in ev["runs"] for t in r)
    return events


class TestEventAssociationSemantics(unittest.TestCase):
    """卡片 7.2 事件规则的合成演示（明示输入，只演示机制，不验证策略）。

    fixture 全部用"平台+跳变"台阶价，手算窗口可达性：
    30 天前瞻窗口=修正后为收盘后 720h：m(t)=max(highs[t+1..t+720])/close_t。
    某点在 W（in-wave）当且仅当其收盘后的窗口内最高价/自身收盘 >=2。
    序列必须足够长（跳变段之间 >720h 隔离），否则早期窗口会"看到"远期峰值，
    与真实回放语义不符（这正是本组 fixture 第一版踩过的坑）。"""

    def test_two_waves_with_shallow_pullback_stay_two_events(self):
        # 平台1: 100 x 720h；跳变: 260 x 24h；回撤平台: 180 x 936h；波2 平台: 400 x 824h
        closes = [100.0] * 720 + [260.0] * 24 + [180.0] * 936 + [400.0] * 824
        mults, mature = forward_mult_series(closes, list(closes))
        # 手算（修正后窗口=[t+1..t+720]，400 在 idx1680）：
        #        t=0..719: close=100，窗口含 260 → 2.6；窗口末端 <=1439 <1680 不含 400
        #        t=720..743: close=260, m=1 → 断档
        #        t=744..959: close=180, 窗口峰值=260 → 1.44 <2 → 断档延续
        #        t=960..1679: close=180, 窗口含 400（t+720>=1680 ⇔ t>=960）→ 2.22 → W2
        #        （远期峰值可达性与修前相同：窗口最大索引都是 t+720；修正只屏蔽本根自身高点）
        #        t>=1680: close=400 → m=1 → 断
        events = associate_waves(mults, mature)
        self.assertEqual(len(events), 2, "浅回撤+间隔>48h 应为两个事件")
        self.assertEqual(events[0]["tier"], 2.5, "波1 峰值倍数 2.6 → 2.5 档")
        self.assertEqual(events[1]["tier"], 2.0, "波2 谷底起算 400/180≈2.22 → 2.0 档")
        gap = events[1]["start"] - events[0]["end"]
        self.assertEqual(gap, 960 - 719, "两事件 W 游程间隔=241h（手算锚）")
        self.assertGreater(gap, 48, "间隔 >48h 才分立（48h 为待验证研究设置）")
        self.assertTrue(events[0]["mature"] and events[1]["mature"])

    def test_gap_rule_sensitivity_merges_when_within_48h(self):
        # 同结构，波2 平台前移使 W2 始于 t=760（窗口 t+720>=1480 含 400）：
        # 平台1: 100 x 720h；跳变: 260 x 24h；回撤平台: 180 x 736h；波2 平台: 400 x 1024h
        closes = [100.0] * 720 + [260.0] * 24 + [180.0] * 736 + [400.0] * 1024
        mults, mature = forward_mult_series(closes, list(closes))
        # 手算：W1=[0..719]（m=2.6）；t=744..759 窗口峰值 260 → 1.44 断档；
        # t=760..1479 close=180 窗口含 400 → 2.22 → W2=[760..1479]；间隔=760-719=41h
        events = associate_waves(mults, mature)
        self.assertEqual(len(events), 1, "间隔 41h <=48h → 并入同一事件（敏感性口径）")
        self.assertEqual(events[0]["tier"], 2.5, "合并后峰值倍数 2.6 → 2.5 档")

    def test_deep_pullback_connects_through_wave_window(self):
        # 深回撤: 平台1 100 x 720h → 跳变 260 x 24h → 回撤 110 x 706h → 波2 300 x 1054h
        # 波2 顶放在 index 1450：>1439（W1 末段 t=719 的窗口最大 index，保证 W1 的 m 干净=2.6）
        # 且 <=1464（回撤低点 t=744 的窗口 [745..1464] 可见，保证低点 m=300/110≈2.73 >=2）
        closes = [100.0] * 720 + [260.0] * 24 + [110.0] * 706 + [300.0] * 1054
        mults, mature = forward_mult_series(closes, list(closes))
        # 手算：W1=[0..719]（m=2.6）；t=720..743 close=260 → m=1 断档 24h；
        # t=744..1449 close=110 窗口含 300 → m=2.73 → W2=[744..1449]；
        # 间隔=744-719=25h <=48h → 全部并入 1 个事件（深回撤不切断事件）
        events = associate_waves(mults, mature)
        self.assertEqual(len(events), 1, "回撤低点本身构成 >=2x 起点时应连成一个事件")
        self.assertEqual(events[0]["tier"], 2.5, "峰值倍数 max(2.6, 2.73)=2.73 → 2.5 档")

    def test_slow_grind_single_event_and_immature_tail(self):
        # 慢涨：20 天内 100→260 线性（480h）。t 的收盘后窗口含末端 479（对 t<=478）
        # → m=260/close_t；t=479 无后续数据 → m=None（未知，不冒充 1.0）。
        # m>=2 当 close_t<=130 → t<=90 → W=[0..90] 单一连续事件；序列 <30 天
        # → 事件内所有时点窗口未走满 → 未到期（不算失败）。
        closes = [100.0 + 160.0 * i / 480 for i in range(480)]
        mults, mature = forward_mult_series(closes, list(closes))
        self.assertIsNone(mults[479], "最后一根无后续数据 → 未知，不得冒充观察值")
        events = associate_waves(mults, mature)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["tier"], 2.5, "峰值倍数 2.6 → 2.5 档")
        self.assertFalse(events[0]["mature"], "窗口未走满 → 未到期，不算失败")
        # 对照：补平线到总长 811h（事件末端 90h + 720h 窗口需 n>=811）后窗口走满
        # → 同一事件转成熟。W 不变：t=91..478 close>130 → m<2；t>=480 close=260 → m=1。
        full = closes + [260.0] * 331
        mults2, mature2 = forward_mult_series(full, list(full))
        events2 = associate_waves(mults2, mature2)
        self.assertEqual(len(events2), 1)
        self.assertTrue(events2[0]["mature"], "补足 30 天窗口后同一事件应转成熟")

    def test_late_mid_event_signal_keeps_remaining_opportunity(self):
        # 事件: 100→150→180→240→400 顶，随后回撤 320→300 并横盘。
        # mults: t=0:4.0, t=1:2.67, t=2:2.22, t=3:1.67, t=4:1.0, t=5:1.25, t=6:1.33
        # → W=[0..2] 单事件（tier 4.0 → 3.0 档）。
        closes = [100.0, 150.0, 180.0, 240.0, 400.0, 320.0, 300.0] + [300.0] * 800
        highs = list(closes)
        mults, mature = forward_mult_series(closes, highs)
        events = associate_waves(mults, mature)
        self.assertEqual(len(events), 1)
        sig_t = 2
        self.assertGreater(mults[sig_t], 2.0, "信号时点前方 30 天 >2x → 仍在事件内")
        self.assertEqual(events[0]["start"], 0)
        self.assertEqual(events[0]["end"], 2, "手算 W=[0..2]")
        self.assertLess(closes[sig_t] / closes[events[0]["start"]], 2.0,
                        "信号价 180/100=1.8 尚未到 2x → 属于中途/偏晚信号，不是事件起点")
        # 剩余机会按同一时间口径：从信号收盘之后（t+1 起）的最高价算起
        remaining = max(highs[sig_t + 1:]) / closes[sig_t]
        self.assertAlmostEqual(remaining, 400.0 / 180.0, places=9,
                               msg="事件内中途信号：剩余机会 400/180≈2.22x 照实记录，不得清零")
        # 事件外信号（t=3，m=400/240≈1.67<2）：上涨中途但收盘后 30 天前瞻不足 2x → 不在 W，
        # 不落入事件区间；其剩余机会同样照实记录（不得清零）。
        out_t = 3
        self.assertAlmostEqual(mults[out_t], 400.0 / 240.0, places=9,
                               msg="t=3 的 30 天前瞻 400/240≈1.67 <2 → 不在 W")
        self.assertGreater(out_t, events[0]["end"], "事件外信号不落入事件区间")
        remaining_out = max(highs[out_t + 1:]) / closes[out_t]
        self.assertAlmostEqual(remaining_out, 400.0 / 240.0, places=9,
                               msg="事件外信号的剩余机会 1.67x 同样照实记录")


class TestForwardMultTimeBoundary(unittest.TestCase):
    """结果标签的时间边界（实验卡 7.2，2026-10-09 修正；每例独立手算）。

    修前反例（主代理 2026-10-09，reports/b4_stage1_review/r2/check_results.json
    .past_high_counterexample；glm 修前实跑复现见
    reports/b4_stage1_glm/r2/past_high_fix_evidence.json）：close 全 100、
    本根 high=250、其后全 100 → 旧实现 m=2.5/mature/1 个事件，正确应为 1.0、无事件。
    冻结语义：参考时刻=第 t 根收盘时刻；未来区间=[t+1, t+horizon]；
    本根盘中高点发生在收盘前，不得计入未来。"""

    def test_past_high_counterexample_fixed(self):
        # 手算：close_t=100；收盘后未来 720 根 high 全 100 → m(0)=100/100=1.0。
        closes = [100.0] * 800
        highs = [250.0] + [100.0] * 799
        mults, mature = forward_mult_series(closes, highs)
        self.assertAlmostEqual(mults[0], 1.0, places=9,
                               msg="本根盘中高点不在未来区间 → m=1.0（修前为 2.5）")
        self.assertTrue(mature[0], "0+720<800 → 窗口走满")
        self.assertEqual(associate_waves(mults, mature), [],
                         "m 全部 <2 → 不得凭空制造事件（修前 1 个）")

    def test_no_future_data_is_unknown_not_fake_value(self):
        # 手算：只有 1 根，收盘后无任何数据 → 未知（None/未到期），不用 0 或 1 冒充。
        closes = [100.0]
        highs = [250.0]
        mults, mature = forward_mult_series(closes, highs)
        self.assertIsNone(mults[0], "无后续数据 → None（未知），不得冒充 0 或 1")
        self.assertFalse(mature[0], "窗口未走满 → 未到期")

    def test_exact_full_window_is_mature(self):
        # 手算：n=721，未来恰好 720 根（idx1..720）；250 在 idx720（未来第 720 根）
        # → m(0)=250/100=2.5，且 0+720=720 < 721 → 恰好走满、mature=True。
        closes = [100.0] * 721
        highs = [100.0] * 720 + [250.0]
        mults, mature = forward_mult_series(closes, highs)
        self.assertAlmostEqual(mults[0], 2.5, places=9)
        self.assertTrue(mature[0], "未来恰好走满 720 根 → 已到期")
        # 边界外一根：250 若挪到 idx721（未来第 721 根）就不可见 → m=1.0
        closes2 = [100.0] * 722
        highs2 = [100.0] * 721 + [250.0]
        mults2, mature2 = forward_mult_series(closes2, highs2)
        self.assertAlmostEqual(mults2[0], 1.0, places=9,
                               msg="窗口外（未来第 721 根）的高点不得计入")
        self.assertTrue(mature2[0])

    def test_partial_window_is_immature_but_still_observed(self):
        # 手算：n=720，未来只有 719 根（idx1..719）；250 在 idx719
        # → 按可得部分照实算 m(0)=2.5，但 0+720=720 不 <720 → 未到期。
        closes = [100.0] * 720
        highs = [100.0] * 719 + [250.0]
        mults, mature = forward_mult_series(closes, highs)
        self.assertAlmostEqual(mults[0], 2.5, places=9,
                               msg="部分窗口按可得数据照实记录，不清零")
        self.assertFalse(mature[0], "窗口未走满 → 未到期，不算失败")

    def test_real_later_rise_to_250_still_counts(self):
        # 手算：250 出现在未来第 1 根（idx1）→ m(0)=250/100=2.5；修正不得漏掉真实涨幅。
        closes = [100.0] * 800
        highs = [100.0, 250.0] + [100.0] * 798
        mults, mature = forward_mult_series(closes, highs)
        self.assertAlmostEqual(mults[0], 2.5, places=9)
        self.assertTrue(mature[0])
        events = associate_waves(mults, mature)
        self.assertEqual(len(events), 1, "真实 2.5x 涨幅构成事件（修正不丢真事件）")
        self.assertEqual(events[0]["tier"], 2.5)


# ----------------------------------------------------------- Part B ----

_DS_PATH = Path(__file__).resolve().parents[1] / "tools" / "b4_replay_ds.py"
_HAS_DS = _DS_PATH.exists()


def _load_ds():
    """按主代理 G1 裁定修复：先登记 sys.modules 再执行，dataclass 才能加载。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("b4_replay_ds", _DS_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("b4_replay_ds", module)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(_HAS_DS, "tools/b4_replay_ds.py 不存在（ds 未交付）")
class TestReplayContract(unittest.TestCase):
    """Part B：对接入版实际接口（normalize_epoch_ms / score_timepoint / deriv_as_of）
    的断言。当前冻结版按主代理裁定存在 D1–D4 缺陷 → 对应测试**应当 FAIL**；
    r2 修复并通过后才转绿。手算预期独立钉住，不与回放互为对错（防两边同错）。"""

    @classmethod
    def setUpClass(cls):
        cls.ds = _load_ds()

    # ---- 时间规范化 ----

    def test_time_normalization_table(self):
        for label, raw, want in TIME_CASES:
            got = self.ds.normalize_epoch_ms(raw)
            self.assertEqual(got, want,
                             f"[{label}] {raw!r} 应为 {want}（UTC ms），实得 {got}")

    def test_time_invalid_returns_none_not_zero(self):
        for label, raw in TIME_INVALID_CASES:
            got = self.ds.normalize_epoch_ms(raw)
            self.assertIsNone(got,
                              f"[{label}] 非法/缺失输入必须返回 None 不得填 0；实得 {got!r}")

    # ---- 取点对齐（生产契约一致性） ----

    def test_score_alignment_no_extra_step_back(self):
        rec = self.ds.score_timepoint("X/USDT", T0 + 59 * HOUR, make_rows(),
                                      None, has_futures=False)
        self.assertEqual(rec["kline"]["status"], "ok")
        self.assertEqual(rec["score_kline"], 8,
                         "H=idx58 收盘后应得 8 分（与生产 box_score 一致）")
        self.assertEqual(rec["kline"]["adopted_candle_utc"],
                         self.ds.ms_to_iso(T0 + 58 * HOUR),
                         "采用蜡烛应为 idx58，不得多退到 idx57")
        rec_early = self.ds.score_timepoint("X/USDT", T0 + 58 * HOUR, make_rows(),
                                            None, has_futures=False)
        self.assertEqual(rec_early["score_kline"], 1, "H=idx57 收盘后应得 1 分（仅 v24up）")

    def test_d2_warmup_counts_indicators_rows(self):
        """D2：26 根闭合+1 占位行，生产 indicators 输出 27 行=box_period+3，允许评分。
        手算 7 分（breakout+trend，量增不可能）。生产尺：直接调生产函数同输入=7。"""
        closed = make_rows_26()
        placeholder = [T0 + 26 * HOUR, 110.0, 110.0, 110.0, 110.0, 0.0]
        ruler, _ = base.box_score(base.indicators(closed + [placeholder], CFG), CFG)
        self.assertEqual(ruler, 7, "生产函数同输入应得 7 分（手算锚）")
        rec = self.ds.score_timepoint("X/USDT", T0 + 26 * HOUR, closed,
                                      None, has_futures=False)
        self.assertEqual(rec["kline"]["status"], "ok",
                         "26 闭合+占位行不得判历史不足（D2）")
        self.assertEqual(rec["score_kline"], 7, "接入版应与生产尺一致得 7 分（D2）")

    def test_future_data_cannot_change_past_score(self):
        rows = make_rows()
        rec_a = self.ds.score_timepoint("X/USDT", T0 + 59 * HOUR, rows,
                                        None, has_futures=False)
        tampered = [list(r) for r in rows]
        tampered[59] = [T0 + 59 * HOUR, 100.0, 500.0, 10.0, 500.0, 12345.0]
        tampered.append([T0 + 62 * HOUR, 1.0, 9999.0, 0.5, 9999.0, 0.0])
        rec_b = self.ds.score_timepoint("X/USDT", T0 + 59 * HOUR, tampered,
                                        None, has_futures=False)
        self.assertEqual(rec_a["score_total"], rec_b["score_total"])
        self.assertEqual(rec_a["kline"], rec_b["kline"],
                         "截止时刻之后的数据变化不得影响该时点任何 K 线输出")

    # ---- D1：合约契约（稀疏/inf/窗外旧点/≥25 有效） ----

    def test_d1_sparse_oi_below_25_valid_obs_not_scored(self):
        """只有 3 个有效 OI 观测：生产 >=25 有效才计算增速 → 手算锚=None；
        回放必须与生产一致，不得拿 3 个点算出 0.5 加分。"""
        sparse = metrics_df([metric_row(72, 100.0), metric_row(24, 100.0),
                             metric_row(0, 150.0)])
        rec = self.ds.score_timepoint("X/USDT", T0, make_rows()[:59],
                                      sparse, has_futures=True)
        self.assertIsNone(rec["deriv"]["oi_chg_1d"],
                          "有效 OI 观测 <25 不得计算增速（D1，生产契约）")
        self.assertNotEqual(rec["deriv"]["status"], "ok")
        # 生产尺（同一手算契约）：假接口同输入 → None
        prod = base._fetch_deriv_context_live(
            FakeFutures(oi_rows=[oi_row(T0 - 72 * HOUR, "100.0"),
                                 oi_row(T0 - 24 * HOUR, "100.0"),
                                 oi_row(T0, "150.0")],
                        ls_rows=[ls_row(T0 - k * HOUR, "0.8") for k in range(80)]),
            "X/USDT:USDT", CFG)
        self.assertIsNone(prod["oi_chg_1d"], "生产尺手算锚：稀疏 OI → None")

    def test_d1_ls_inf_tail_uses_previous_valid(self):
        """LS 尾行 inf：生产过滤非有限值后采用前一有效点；inf 是"有更新却不可用"
        → stale_tail=1。回放不得把 inf 当有效观测。"""
        rows = [metric_row(k, 100.0, ls_top=0.8) for k in range(79, 0, -1)]
        rows.append((T0, 100.0, 0.1, float("inf"), 1.5, 1.8))
        out = self.ds.deriv_as_of(metrics_df(rows), T0)
        self.assertAlmostEqual(out["ls_top"], 0.8, places=9,
                               msg="inf 尾行应被过滤，采用前一有效点 0.8（D1）")
        self.assertEqual(out["ls_stale_tail"], 1, "inf 尾行计入'有更新却不可用'")

    def test_d1_oi_outside_interface_window_ignored(self):
        """接口窗口=最近 200 根 1h。窗外旧点（T-300h）不得参与增速基准。
        仅 2 个有效点（窗外旧点+当点）→ 生产 <25 → None。"""
        rows = [metric_row(300, 50.0), metric_row(0, 150.0)]
        rec = self.ds.score_timepoint("X/USDT", T0, make_rows()[:59],
                                      metrics_df(rows), has_futures=True)
        self.assertIsNone(rec["deriv"]["oi_chg_1d"],
                          "窗外旧 OI 点不得参与计算（D1：接口窗口限制）")

    # ---- D3：未评分 K 线不得成为筛选信号 ----

    def test_d3_unscored_kline_never_selected(self):
        """20 根闭合（历史不足）+ 强合约分 10 分：生产会先 continue，
        不得标 selected；分项可算≠可作本次筛选信号（score_deriv 在 rec 顶层）。"""
        short = make_rows()[:20]
        rec = self.ds.score_timepoint("X/USDT", T0 + 20 * HOUR, short,
                                      strong_deriv_metrics(), has_futures=True)
        self.assertEqual(rec["kline"]["status"], "kline_short_history")
        self.assertFalse(rec["selected"],
                         "K 线历史不足时总分再高也不得 selected（D3）")
        self.assertGreaterEqual(rec["score_deriv"], 10,
                                "合约分项照常保留诊断（分开，不冒充入选）")
        self.assertTrue(any("short_history" in r for r in rec["reasons"]))

    # ---- D4：延迟情景同一可得截止 ----

    def test_d4_lag_scenario_single_availability_cutoff(self):
        """lag=1h：T0 整点观测尚未可得。采用点应为 T0-1h；
        不可得行不得计入 stale_tail（它不是'有更新却不可用'）；
        改动/删除不可得行不得改变任何输出。"""
        rows_with = [metric_row(k, 150.0 if k == 0 else 100.0) for k in range(79, -1, -1)]
        rows_without = [r for r in rows_with if r[0] != T0]
        out_with = self.ds.deriv_as_of(metrics_df(rows_with), T0, lag_hours=1)
        out_without = self.ds.deriv_as_of(metrics_df(rows_without), T0, lag_hours=1)
        self.assertEqual(out_with["oi_as_of"], T0 - HOUR,
                         "lag=1h 时采用点应为 T0-1h（D4）")
        self.assertEqual(out_with, out_without,
                         "改动尚未可得的数据不得改变输出（D4 同一可得截止）")
        self.assertEqual(out_with["oi_stale_tail"], 0,
                         "不可得行不是'有更新却不可用'（D4）")


if __name__ == "__main__":
    unittest.main()
