#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""离线边界测试：tools/b4_replay_ds.py

不联网。覆盖任务书第 5 节要求的边界，以及 R2 返修（D1–D5）新增的对抗性断言：
  倒数第二根时点 / 未来最后一根变化 / 时区·毫秒·微秒·字符串日期 / 小时分界 /
  历史不足 / 无合约 / 序列缺口 / 尾部不可用值 / 账户比不得冒充大户比 /
  标的身份未知与分母对账 / 评分入口不接受未来标签
  + D1 合约契约（接口窗口 / 有限数过滤 / >=25 个有效 OI 观测 / LS 合法零值）
  + D2 预热门槛按 indicators 行数（含占位行）对齐，边界前后一根
  + D3 未评分不得 selected，未知不得变成观察到的零
  + D4 延迟场景统一可得截止（lag=0 与 lag=1h）
  + D5 测试使用隔离输出目录，绝不写 r1 冻结交付

**所有会落盘的调用都显式传临时目录**，不依赖也不污染默认输出目录。

运行：
  py -3.10 -m unittest discover -s tests -p "test_b4_replay_ds.py" -v
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import b4_replay_ds as b4  # noqa: E402

HOUR = 3_600_000
BASE_MS = b4.parse_utc("2024-06-01T00:00:00Z")


def make_candles(n: int, start_ms: int = BASE_MS, step: int = HOUR) -> list[list[float]]:
    """构造 n 根平稳 1h K 线：价格恒定 100，量恒定 1000（闭合语义由调用方负责）。"""
    out = []
    for i in range(n):
        ts = start_ms + i * step
        out.append([float(ts), 100.0, 101.0, 99.0, 100.0, 1000.0])
    return out


def candles_ending_before(n: int, decision_ms: int) -> list[list[float]]:
    """n 根 K 线，最后一根 open = decision−1h（在 decision 时刻刚闭合）。"""
    return [[float(decision_ms - (n - i) * HOUR), 100.0, 101.0, 99.0, 100.0, 1000.0]
            for i in range(n)]


def make_metrics(days: int, start_ms: int = BASE_MS - 5 * 24 * HOUR,
                 oi_value=1_000_000.0, ls_top=1.2, step_min=5,
                 zero_tail=0, nan_ls_tail=0, oi_amount=1000.0,
                 ls_top_account=2.5, ls_global=1.8) -> pd.DataFrame:
    """构造 metrics 数据（含整点行），用于衍生侧口径测试。"""
    rows = []
    n = int(days * 24 * 60 / step_min)
    for i in range(n):
        dt = pd.Timestamp(start_ms, unit="ms", tz="UTC") + pd.Timedelta(minutes=i * step_min)
        rows.append({
            "create_time": dt.strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": "TESTUSDT",
            "sum_open_interest": oi_amount,
            "sum_open_interest_value": oi_value,
            "count_toptrader_long_short_ratio": ls_top_account,
            "sum_toptrader_long_short_ratio": ls_top,
            "count_long_short_ratio": ls_global,
            "sum_taker_long_short_vol_ratio": 1.0,
            "dt": dt,
        })
    df = pd.DataFrame(rows)
    for k in range(zero_tail):
        df.loc[df.index[-1 - k], "sum_open_interest_value"] = 0.0
    for k in range(nan_ls_tail):
        df.loc[df.index[-1 - k], "sum_toptrader_long_short_ratio"] = float("nan")
    return df


def append_hourly(df: pd.DataFrame, hours: int, start_ms: int,
                  oi_value=0.0, ls_top=float("nan")) -> pd.DataFrame:
    """在 df 之后追加整点行（用于模拟"比采用点更新但不可用"的尾部观测）。"""
    rows = []
    for i in range(hours):
        dt = pd.Timestamp(start_ms, unit="ms", tz="UTC") + pd.Timedelta(hours=i)
        rows.append({
            "create_time": dt.strftime("%Y-%m-%d %H:%M:%S"), "symbol": "TESTUSDT",
            "sum_open_interest": 1.0, "sum_open_interest_value": oi_value,
            "count_toptrader_long_short_ratio": 2.5,
            "sum_toptrader_long_short_ratio": ls_top,
            "count_long_short_ratio": 1.8, "sum_taker_long_short_vol_ratio": 1.0, "dt": dt,
        })
    out = pd.concat([df, pd.DataFrame(rows)], ignore_index=True)
    return out.sort_values("dt").drop_duplicates("dt", keep="last").reset_index(drop=True)


def make_hourly_at(offsets_hours, oi_value=1_000_000.0, ls_top=1.2,
                   ls_top_account=2.5, ls_global=1.8) -> pd.DataFrame:
    """在**指定小时偏移**上构造整点 metrics 行（用于稀疏/边界场景）。"""
    rows = []
    for off in offsets_hours:
        dt = pd.Timestamp(BASE_MS + off * HOUR, unit="ms", tz="UTC")
        rows.append({
            "create_time": dt.strftime("%Y-%m-%d %H:%M:%S"), "symbol": "TESTUSDT",
            "sum_open_interest": 1000.0, "sum_open_interest_value": oi_value,
            "count_toptrader_long_short_ratio": ls_top_account,
            "sum_toptrader_long_short_ratio": ls_top,
            "count_long_short_ratio": ls_global, "sum_taker_long_short_vol_ratio": 1.0, "dt": dt,
        })
    return pd.DataFrame(rows)


class FakeFutures:
    """生产 `_fetch_deriv_context_live` 用到的两个 /futures/data/* 端点（不联网）。"""

    def __init__(self, frame: pd.DataFrame) -> None:
        self.frame = frame

    def market(self, symbol: str) -> dict:
        return {"id": "TESTUSDT"}

    def fapiDataGetOpenInterestHist(self, params: dict) -> list[dict]:
        return [{"timestamp": int(row["dt"].value // 1_000_000),
                 "sumOpenInterestValue": row[b4.OI_VALUE_FIELD]}
                for _, row in self.frame.iterrows()][-params["limit"]:]

    def fapiDataGetTopLongShortPositionRatio(self, params: dict) -> list[dict]:
        return [{"timestamp": int(row["dt"].value // 1_000_000),
                 "longShortRatio": row[b4.LS_TOP_FIELD]}
                for _, row in self.frame.iterrows()][-params["limit"]:]


def replay_vs_live(frame: pd.DataFrame, decision_ms: int = BASE_MS) -> tuple[dict, dict]:
    """同一批输入：回放 deriv_as_of vs 生产假接口。返回 (replay, live)。"""
    return (b4.deriv_as_of(frame, decision_ms),
            b4.B._fetch_deriv_context_live(FakeFutures(frame), "TEST/USDT:USDT", b4.CFG))


class TestTimeParsing(unittest.TestCase):
    """时区 / 毫秒 / 微秒 / 字符串日期；非法或缺失不填零。"""

    def test_millisecond(self):
        self.assertEqual(b4.normalize_epoch_ms(1730160000000), 1730160000000)

    def test_microsecond_normalized_to_ms(self):
        self.assertEqual(b4.normalize_epoch_ms(1730160000000000), 1730160000000)

    def test_seconds(self):
        self.assertEqual(b4.normalize_epoch_ms(1730160000), 1730160000000)

    def test_string_seconds_and_iso(self):
        self.assertEqual(b4.normalize_epoch_ms("1730160000"), 1730160000000)
        self.assertEqual(b4.normalize_epoch_ms("2024-10-29T00:00:00Z"), 1730160000000)
        self.assertEqual(b4.normalize_epoch_ms("2024-10-29 00:00:00"), 1730160000000)

    def test_string_date_is_utc(self):
        # 同一 UTC 瞬间，两种写法必须相等（证明按 UTC 解析，不按本地时区）
        self.assertEqual(b4.normalize_epoch_ms("2024-06-01 12:00:00"),
                         b4.normalize_epoch_ms("2024-06-01T12:00:00Z"))

    def test_invalid_returns_none_never_zero(self):
        for bad in ["", "   ", None, "abc", "NaN", float("nan"), 0, -1, 1e18]:
            self.assertIsNone(b4.normalize_epoch_ms(bad), msg=repr(bad))

    def test_mixed_units_in_one_series(self):
        vals = [b4.normalize_epoch_ms(x) for x in
                ("1730160000000", "1730246400000000", "2024-10-31 00:00:00")]
        self.assertEqual(vals, [1730160000000, 1730246400000, 1730332800000])


class TestKlineAdoptionContract(unittest.TestCase):
    """box_score 的 df.iloc[-2] 与量增 iloc[:-1] 契约。"""

    def setUp(self):
        # 400 根闭合 K 线，最后一根 open = T-1h（在 T 时刻刚闭合）
        self.candles = make_candles(400)
        self.decision = BASE_MS + 300 * HOUR      # 决策时刻

    def test_adopted_candle_is_last_closed(self):
        closed = b4.closed_candles(self.candles, self.decision)
        self.assertEqual(closed[-1][0], self.decision - HOUR)
        df = b4.kline_df_as_of(closed, self.decision)
        # iloc[-2] 必须正好是「决策时刻刚闭合的那根」
        self.assertEqual(int(df.iloc[-2]["ts"]), self.decision - HOUR)
        # 最后一根是占位（形成中）
        self.assertEqual(int(df.iloc[-1]["ts"]), self.decision)

    def test_naive_closed_only_steps_back_one_candle(self):
        """仅喂闭合 K 线（不加占位）会多退一根 —— 这是必须避免的取点错误。"""
        closed = b4.closed_candles(self.candles, self.decision)
        naive = b4.B.indicators(closed[-b4.CFG.lookback:], b4.CFG)
        self.assertEqual(int(naive.iloc[-2]["ts"]), self.decision - 2 * HOUR)

    def test_placeholder_values_are_ignored(self):
        closed = b4.closed_candles(self.candles, self.decision)
        df_a = b4.kline_df_as_of(closed, self.decision)
        win = closed[-(b4.CFG.lookback - 1):]
        garbage = [float(self.decision), 1.0, 1e9, 1e-9, 1.0, 1e12]
        df_b = b4.B.indicators(win + [garbage], b4.CFG)
        self.assertEqual(b4.B.box_score(df_a, b4.CFG), b4.B.box_score(df_b, b4.CFG))

    def test_window_length_matches_live_lookback(self):
        closed = b4.closed_candles(self.candles, self.decision)
        df = b4.kline_df_as_of(closed, self.decision)
        self.assertEqual(len(df), b4.CFG.lookback)

    def test_future_candles_do_not_change_score(self):
        """截止时间之后的数据不得改变当时评分（隔离未来）。"""
        rec_now = b4.score_timepoint("TESTUSDT", self.decision, self.candles, None, False)
        extended = self.candles + make_candles(50, start_ms=self.decision + HOUR)
        rec_future = b4.score_timepoint("TESTUSDT", self.decision, extended, None, False)
        self.assertEqual(rec_now["score_total"], rec_future["score_total"])
        self.assertEqual(rec_now["kline"]["details"], rec_future["kline"]["details"])
        self.assertEqual(rec_now["kline"]["adopted_candle_utc"],
                         rec_future["kline"]["adopted_candle_utc"])

    def test_hour_boundary_exact_close_counts_as_closed(self):
        """open+1h == decision 的那根算已闭合；差 1ms 则不算。"""
        ts = BASE_MS
        candle = [float(ts), 1.0, 1.0, 1.0, 1.0, 1.0]
        self.assertEqual(len(b4.closed_candles([candle], ts + HOUR)), 1)
        self.assertEqual(len(b4.closed_candles([candle], ts + HOUR - 1)), 0)


class TestKlineStatusPaths(unittest.TestCase):

    def test_short_history_not_scored(self):
        candles = candles_ending_before(10, BASE_MS)
        rec = b4.score_timepoint("TESTUSDT", BASE_MS, candles, None, False)
        self.assertEqual(rec["kline"]["status"], "kline_short_history")
        self.assertIsNone(rec["kline"]["score"])
        # 未知不得变成观察到的零（D3）
        self.assertIsNone(rec["score_kline"])
        self.assertIsNone(rec["score_total"])
        self.assertFalse(rec["selected"])
        self.assertEqual(rec["screening_status"], "skipped:kline_short_history")

    def test_no_kline_data(self):
        rec = b4.score_timepoint("TESTUSDT", BASE_MS, [], None, False)
        self.assertEqual(rec["kline"]["status"], "no_kline_data")
        self.assertIn("no_closed_candle_at_decision", rec["reasons"])

    def test_sequence_gap_flagged_not_scored(self):
        """最后一根闭合 K 线与决策时刻之间有空档 → kline_gap，不得用陈旧数据充数。"""
        candles = make_candles(60)                 # 只到 BASE_MS+59h
        decision = BASE_MS + 400 * HOUR            # 远在其后
        rec = b4.score_timepoint("TESTUSDT", decision, candles, None, False)
        self.assertEqual(rec["kline"]["status"], "kline_gap")
        self.assertGreater(rec["kline"]["gap_hours"], 1)
        self.assertIsNone(rec["kline"]["score"])
        self.assertFalse(rec["selected"])

    def test_no_futures_path(self):
        candles = make_candles(400)
        rec = b4.score_timepoint("TESTUSDT", BASE_MS + 300 * HOUR, candles, None, False)
        self.assertEqual(rec["deriv"]["status"], "no_futures")
        self.assertEqual(rec["score_deriv"], 0)
        self.assertIn("no_futures", json.dumps(rec["deriv"]))


class TestKlineWarmupAlignment(unittest.TestCase):
    """D2：预热门槛按真实输入经 indicators 后的行数判定（占位行计入）。"""

    REQUIRED = None

    def setUp(self):
        self.required = b4.CFG.box_period + 3

    def test_boundary_25_26_27_closed_candles(self):
        r25 = b4.score_timepoint("TESTUSDT", BASE_MS, candles_ending_before(25, BASE_MS), None, False)
        r26 = b4.score_timepoint("TESTUSDT", BASE_MS, candles_ending_before(26, BASE_MS), None, False)
        r27 = b4.score_timepoint("TESTUSDT", BASE_MS, candles_ending_before(27, BASE_MS), None, False)
        # 25 根闭合 + 1 占位 = 26 行 < 27 → 仍不足
        self.assertEqual(r25["kline"]["df_rows"], 26)
        self.assertEqual(r25["kline"]["status"], "kline_short_history")
        # 26 根闭合 + 1 占位 = 27 行 → 达标（修复前会误判为不足）
        self.assertEqual(r26["kline"]["df_rows"], 27)
        self.assertEqual(r26["kline"]["status"], "ok")
        self.assertIsNotNone(r26["score_kline"])
        self.assertEqual(r27["kline"]["status"], "ok")

    def test_warmup_matches_live_length_check(self):
        """回放判 ok 的那一档，生产用同样输入也必须通过长度闸门。"""
        for n in (25, 26, 27):
            candles = candles_ending_before(n, BASE_MS)
            closed = b4.closed_candles(candles, BASE_MS)
            live_input = b4.kline_df_as_of(closed, BASE_MS)
            rec = b4.score_timepoint("TESTUSDT", BASE_MS, candles, None, False)
            live_passes = len(live_input) >= self.required
            self.assertEqual(rec["kline"]["status"] == "ok", live_passes, msg=f"n={n}")

    def test_placeholder_row_is_counted(self):
        """占位行必须计入行数（线上 fetch_ohlcv 也含形成中那根）。"""
        closed = b4.closed_candles(candles_ending_before(26, BASE_MS), BASE_MS)
        self.assertEqual(len(closed), 26)
        self.assertEqual(len(b4.kline_df_as_of(closed, BASE_MS)), 27)


class TestDerivContract(unittest.TestCase):

    def test_uses_top_position_ratio_not_account_ratio(self):
        """ls_top 必须取 sum_toptrader_long_short_ratio（大户持仓比）。"""
        df = make_metrics(days=5, ls_top=0.8, ls_top_account=3.3, ls_global=2.1)
        d = b4.deriv_as_of(df, BASE_MS)
        self.assertEqual(d["ls_source_field"], b4.LS_TOP_FIELD)
        self.assertAlmostEqual(d["ls_top"], 0.8, places=6)
        self.assertAlmostEqual(d["alternatives"]["ls_top_account"], 3.3, places=6)
        self.assertAlmostEqual(d["alternatives"]["ls_global"], 2.1, places=6)

    def test_account_ratio_never_impersonates_top_position(self):
        """只有账户比可用时，ls_top 必须保持 None，且不得因此计分。"""
        df = make_metrics(days=5, ls_top=float("nan"))
        d = b4.deriv_as_of(df, BASE_MS)
        self.assertIsNone(d["ls_top"])
        self.assertIsNotNone(d["alternatives"]["ls_top_account"])
        score, details = b4.B.deriv_score(d["oi_chg_1d"], d["oi_chg_3d"],
                                          d["ls_top"], d["ls_chg_3d"])
        self.assertNotIn("hit_ls_top", details)

    def test_min_25_valid_oi_obs_required(self):
        """D1：有效 OI 观测 < 25 → 不得算 oi_chg；>= 25 才算（与生产同款门槛）。"""
        few = make_metrics(days=1, start_ms=BASE_MS - 24 * HOUR, step_min=60)
        d_few = b4.deriv_as_of(few, BASE_MS)
        self.assertEqual(d_few["oi_valid_obs"], 24)
        self.assertIsNone(d_few["oi_chg_1d"])
        self.assertIsNone(d_few["oi_chg_3d"])
        many = make_metrics(days=2, start_ms=BASE_MS - 48 * HOUR, step_min=60)
        d_many = b4.deriv_as_of(many, BASE_MS)
        self.assertGreaterEqual(d_many["oi_valid_obs"], 25)
        self.assertIsNotNone(d_many["oi_chg_1d"])

    def test_sparse_oi_replay_matches_live(self):
        """D1：只有 3 个 OI 观测时，回放必须与生产假接口一致（都不得算 oi_chg）。"""
        sparse = make_hourly_at([-72, -24, 0])
        replay, live = replay_vs_live(sparse)
        self.assertEqual(replay["oi_chg_1d"], live["oi_chg_1d"])
        self.assertIsNone(replay["oi_chg_1d"])
        self.assertEqual(replay["status"], live["status"])

    def test_endpoint_lookback_window_enforced(self):
        """D1：超出 DERIV_LOOKBACK 的旧观测不得被回放拿来当基准。"""
        df = make_metrics(days=13, start_ms=BASE_MS - 13 * 24 * HOUR, step_min=60)
        self.assertGreater(len(df), b4.DERIV_LOOKBACK)
        # 只有 200 小时窗口之外的旧观测是"好"的，窗口内全为 0
        oi = [1_000_000.0 if i < len(df) - b4.DERIV_LOOKBACK else 0.0 for i in range(len(df))]
        df[b4.OI_VALUE_FIELD] = oi
        d = b4.deriv_as_of(df, BASE_MS)
        self.assertEqual(d["window"]["window_rows"], b4.DERIV_LOOKBACK)
        self.assertEqual(d["oi_valid_obs"], 0)
        self.assertIsNone(d["oi_chg_1d"])
        replay, live = replay_vs_live(df)
        self.assertEqual(replay["oi_chg_1d"], live["oi_chg_1d"])
        self.assertEqual(replay["status"], live["status"])

    def test_ls_infinite_tail_filtered_like_live(self):
        """D1：LS 尾部为 inf 时，按「有限」过滤回退到前一有效点（与生产一致）。"""
        df = make_metrics(days=5, start_ms=BASE_MS - 5 * 24 * HOUR, step_min=60)
        df.loc[df.index[-1], b4.LS_TOP_FIELD] = float("inf")
        replay, live = replay_vs_live(df)
        self.assertEqual(replay["ls_top"], live["ls_top"])
        self.assertEqual(replay["ls_as_of"], live["ls_as_of"])
        self.assertEqual(replay["ls_stale_tail"], live["ls_stale_tail"])
        self.assertNotEqual(replay["ls_as_of"], BASE_MS)      # 回退到前一有效点

    def test_ls_legal_zero_is_kept(self):
        """D1：LS 的合法零值必须保留（不得自行改成必须正数）。"""
        df = make_metrics(days=5, start_ms=BASE_MS - 5 * 24 * HOUR, step_min=60, ls_top=0.0)
        d = b4.deriv_as_of(df, BASE_MS)
        self.assertEqual(d["ls_top"], 0.0)
        self.assertEqual(d["status"], "ok")
        replay, live = replay_vs_live(df)
        self.assertEqual(replay["ls_top"], live["ls_top"])
        self.assertEqual(replay["ls_top"], 0.0)
        self.assertEqual(replay["status"], live["status"])
        # 0 < LS_TOP_THRESHOLD(1.0) → 应计 ls_top 分（与生产一致）
        self.assertEqual(b4.B.deriv_score(None, None, replay["ls_top"], None)[0],
                         b4.WEIGHTS["ls_top"])

    def test_healthy_replay_matches_live_contract(self):
        """D1 总检：健康输入下，回放与生产假接口逐字段一致。"""
        df = make_metrics(days=5, start_ms=BASE_MS - 5 * 24 * HOUR, step_min=60)
        replay, live = replay_vs_live(df)
        for key in ("status", "oi_chg_1d", "oi_chg_3d", "ls_top", "oi_as_of", "ls_as_of",
                    "oi_stale_tail", "ls_stale_tail"):
            self.assertEqual(replay[key], live[key], msg=key)
        self.assertEqual(b4.B.deriv_score(replay["oi_chg_1d"], replay["oi_chg_3d"],
                                          replay["ls_top"], replay["ls_chg_3d"]),
                         b4.B.deriv_score(live["oi_chg_1d"], live["oi_chg_3d"],
                                          live["ls_top"], live["ls_chg_3d"]))

    def test_insufficient_history_partial(self):
        """OI 历史不足 → oi_chg 不可算；ls 可用 → partial。"""
        df = make_metrics(days=1, start_ms=BASE_MS - 24 * HOUR, step_min=60)
        d = b4.deriv_as_of(df, BASE_MS)
        self.assertIsNone(d["oi_chg_1d"])
        self.assertIsNone(d["oi_chg_3d"])
        self.assertEqual(d["status"], "partial")

    def test_zero_oi_value_not_usable(self):
        """合法零值不得当基数：OI 全为 0 → 不可用。"""
        df = make_metrics(days=5, oi_value=0.0)
        d = b4.deriv_as_of(df, BASE_MS)
        self.assertIsNone(d["oi_chg_1d"])
        self.assertFalse(d["status"] == "ok")

    def test_tail_zero_does_not_poison_adopted_point(self):
        """采用点之后出现"更新但不可用"的整点观测（OI=0 / 大户比 NaN）时：
        采用点必须回退到最后一根**可用**观测，并把尾部不可用数单独记录。"""
        df = make_metrics(days=5)                     # 结束于 BASE_MS-5min
        df = append_hourly(df, hours=3, start_ms=BASE_MS, oi_value=0.0, ls_top=float("nan"))
        decision = BASE_MS + 3 * HOUR                 # 尾部 3 根不可用观测在决策时刻之前
        d = b4.deriv_as_of(df, decision)
        self.assertIsNotNone(d["oi_chg_1d"])
        self.assertIsNotNone(d["ls_top"])
        self.assertEqual(d["oi_as_of"], BASE_MS - HOUR)     # 回退到最后一根可用
        self.assertEqual(d["oi_stale_tail"], 3)             # 尾部不可用被记数
        self.assertEqual(d["ls_stale_tail"], 3)

    def test_future_rows_are_not_counted_as_stale_tail(self):
        """决策时刻之后的行是未来，不计入 stale_tail。"""
        df = make_metrics(days=5)
        df = append_hourly(df, hours=5, start_ms=BASE_MS + HOUR, oi_value=0.0, ls_top=float("nan"))
        d = b4.deriv_as_of(df, BASE_MS)
        self.assertEqual(d["oi_stale_tail"], 0)
        self.assertEqual(d["ls_stale_tail"], 0)

    def test_future_metrics_do_not_change_deriv(self):
        """决策时刻之后的 metrics 行不得改变当时结果（隔离未来）。"""
        df = make_metrics(days=5)
        d_now = b4.deriv_as_of(df, BASE_MS)
        extra = make_metrics(days=3, start_ms=BASE_MS + HOUR)
        d_future = b4.deriv_as_of(pd.concat([df, extra], ignore_index=True), BASE_MS)
        self.assertEqual(d_now["oi_chg_1d"], d_future["oi_chg_1d"])
        self.assertEqual(d_now["oi_chg_3d"], d_future["oi_chg_3d"])
        self.assertEqual(d_now["ls_top"], d_future["ls_top"])
        self.assertEqual(d_now["oi_as_of"], d_future["oi_as_of"])

    def test_missing_metrics_files_reason(self):
        d = b4.deriv_as_of(None, BASE_MS)
        self.assertEqual(d["status"], "failed")
        self.assertEqual(d["reason"], "no_metrics_files")

    def test_no_observation_before_cutoff(self):
        df = make_metrics(days=1, start_ms=BASE_MS + 10 * HOUR)
        d = b4.deriv_as_of(df, BASE_MS)
        self.assertEqual(d["status"], "failed")
        self.assertEqual(d["reason"], "no_observation_at_or_before_cutoff")

    def test_deriv_score_thresholds(self):
        s, det = b4.B.deriv_score(0.12, 0.25, 0.9, 0.0)
        self.assertEqual(s, b4.WEIGHTS["oi_chg_1d"] + b4.WEIGHTS["oi_chg_3d"] + b4.WEIGHTS["ls_top"])
        s2, det2 = b4.B.deriv_score(None, None, None, None)
        self.assertEqual(s2, 0)


class TestDelayCutoff(unittest.TestCase):
    """D4：延迟场景下所有字段遵守同一个可得截止。"""

    def _frame_with_T_row(self) -> pd.DataFrame:
        df = make_metrics(days=5, start_ms=BASE_MS - 120 * HOUR, step_min=60)   # 到 BASE_MS-1h
        return append_hourly(df, hours=1, start_ms=BASE_MS,
                             oi_value=1_000_000.0, ls_top=1.2)                  # 加一行 T

    def test_unavailable_row_does_not_change_output(self):
        """lag=1h 时 T 行尚未可得：加载与否输出必须完全相同。"""
        df = self._frame_with_T_row()
        with_t = b4.deriv_as_of(df, BASE_MS, lag_hours=1)
        without_t = b4.deriv_as_of(df[df["dt"] < pd.Timestamp(BASE_MS, unit="ms", tz="UTC")],
                                   BASE_MS, lag_hours=1)
        self.assertEqual(with_t, without_t)
        self.assertEqual(with_t["oi_stale_tail"], 0)
        self.assertEqual(with_t["ls_stale_tail"], 0)

    def test_lag_zero_vs_one_hour_shifts_adoption(self):
        """lag 必须真正生效：lag=0 用 T 行，lag=1h 回退到 T−1h。"""
        df = self._frame_with_T_row()
        d0 = b4.deriv_as_of(df, BASE_MS, lag_hours=0)
        d1 = b4.deriv_as_of(df, BASE_MS, lag_hours=1)
        self.assertEqual(d0["oi_as_of"] - d1["oi_as_of"], HOUR)
        self.assertEqual(d0["ls_as_of"] - d1["ls_as_of"], HOUR)
        self.assertEqual(d0["oi_stale_tail"], 0)
        self.assertEqual(d1["oi_stale_tail"], 0)

    def test_availability_cutoff_recorded(self):
        rec = b4.score_timepoint("TESTUSDT", BASE_MS, candles_ending_before(60, BASE_MS),
                                 make_metrics(days=5, start_ms=BASE_MS - 120 * HOUR, step_min=60),
                                 True, deriv_lag_hours=1)
        self.assertEqual(rec["field_adoption"]["deriv_availability_cutoff_utc"],
                         b4.ms_to_iso(BASE_MS - HOUR))


class TestNoFutureLabelsAndAccounting(unittest.TestCase):

    def test_score_entry_has_no_label_parameters(self):
        import inspect
        params = set(inspect.signature(b4.score_timepoint).parameters)
        for forbidden in ("label", "gain", "peak_date", "days_to_peak", "future_return"):
            self.assertNotIn(forbidden, params)

    def test_record_contains_no_future_label_keys(self):
        candles = make_candles(400)
        rec = b4.score_timepoint("TESTUSDT", BASE_MS + 300 * HOUR, candles, None, False)
        for forbidden in ("label", "gain", "peak_date", "days_to_peak", "future_return"):
            self.assertNotIn(forbidden, rec)

    def test_every_planned_point_has_one_output(self):
        """分母对账：每个预定 (段, 时点) 恰好一条输出，跳过/未知也保留。"""
        man = b4.build_manifest()
        planned = sum(s["points"] for s in man["segments"])
        with tempfile.TemporaryDirectory() as tmp:
            res = b4.run_pilot(man, limit_points=2, out_dir=tmp)
        self.assertEqual(len(res["records"]), 2 * len(man["segments"]))
        for rec in res["records"]:
            self.assertIn("score_total", rec)
            self.assertIn("reasons", rec)
            self.assertIn("completeness", rec)
            self.assertIn("screening_status", rec)
        self.assertEqual(planned, 48 * len(man["segments"]))

    def test_manifest_within_declared_bounds(self):
        man = b4.build_manifest()
        self.assertLessEqual(len(man["segments"]), 12)
        for seg in man["segments"]:
            self.assertLessEqual(seg["points"], 48)
            self.assertTrue(seg["selection_reason"])

    def test_no_network_during_pilot(self):
        man = b4.build_manifest()
        with tempfile.TemporaryDirectory() as tmp:
            res = b4.run_pilot(man, limit_points=1, out_dir=tmp)
        self.assertEqual(res["cost"]["network_blocked_attempts"], 0)

    def test_coverage_levels_present(self):
        cov = b4.build_coverage(full=False)   # 轻量模式：跳过百万行逐份对账
        for key in ("queue", "local_census", "queue_vs_local_reconciliation",
                    "ledger_sample_check", "identity", "unknowns"):
            self.assertIn(key, cov)


class TestScreeningSemantics(unittest.TestCase):
    """D3：K 线未评分 → 不是入选信号，未知不得变成观察到的零。"""

    def test_unscored_kline_never_selected_even_with_full_deriv(self):
        metrics = make_metrics(days=5, start_ms=BASE_MS - 120 * HOUR, step_min=60, ls_top=0.5)
        rec = b4.score_timepoint("TESTUSDT", BASE_MS, candles_ending_before(20, BASE_MS),
                                 metrics, True)
        self.assertEqual(rec["kline"]["status"], "kline_short_history")
        self.assertGreater(rec["score_deriv"], 0)        # 合约侧确实有分
        self.assertIsNone(rec["score_kline"])            # 未知，不是 0
        self.assertIsNone(rec["score_total"])
        self.assertFalse(rec["selected"])
        self.assertFalse(rec["threshold_compare"]["is_selection_signal"])
        self.assertEqual(rec["screening_status"], "skipped:kline_short_history")
        self.assertTrue(rec["score_deriv_diagnostic_only"])

    def test_scored_below_threshold_status(self):
        rec = b4.score_timepoint("TESTUSDT", BASE_MS, candles_ending_before(60, BASE_MS),
                                 None, False)
        self.assertTrue(rec["kline"]["scored"])
        self.assertFalse(rec["selected"])
        self.assertEqual(rec["screening_status"], "scored_below_threshold")
        self.assertTrue(rec["threshold_compare"]["is_selection_signal"])

    def test_selected_implies_kline_scored(self):
        man = b4.build_manifest()
        with tempfile.TemporaryDirectory() as tmp:
            res = b4.run_pilot(man, limit_points=3, out_dir=tmp)
        for rec in res["records"]:
            if rec["selected"]:
                self.assertEqual(rec["kline"]["status"], "ok")
                self.assertIsNotNone(rec["score_total"])


class TestOutputIsolation(unittest.TestCase):
    """D5：默认输出不覆盖 r1；测试写临时目录。"""

    def test_default_out_dir_is_r2_not_r1(self):
        self.assertEqual(b4.OUT_DIR, b4.R1_DIR / "r2")
        self.assertNotEqual(b4.OUT_DIR, b4.R1_DIR)

    def test_set_out_dir_is_explicit(self):
        original = b4.OUT_DIR
        try:
            b4.set_out_dir("some/other/dir")
            self.assertEqual(b4.OUT_DIR, Path("some/other/dir"))
        finally:
            b4.set_out_dir(original)

    def test_run_pilot_does_not_touch_r1(self):
        r1_files = sorted(p for p in b4.R1_DIR.glob("*") if p.is_file())
        before = {p.name: b4._sha256(p) for p in r1_files}
        with tempfile.TemporaryDirectory() as tmp:
            b4.run_pilot(b4.build_manifest(), limit_points=1, out_dir=tmp)
        after = {p.name: b4._sha256(p) for p in r1_files}
        self.assertEqual(before, after)

    def test_pilot_writes_only_to_given_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            b4.run_pilot(b4.build_manifest(), limit_points=1, out_dir=tmp)
            produced = sorted(p.name for p in Path(tmp).iterdir())
        self.assertIn("scoring_output.jsonl", produced)
        self.assertIn("cost.json", produced)
        self.assertIn("segment_summary.json", produced)
        self.assertIn("raw_refs.json", produced)


if __name__ == "__main__":
    unittest.main(verbosity=2)
