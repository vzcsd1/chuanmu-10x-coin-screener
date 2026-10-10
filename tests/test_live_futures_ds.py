"""实时合约取数可靠性：请求预算、出口回退、暂停期限合并的离线取证（ds4.1f）。

对应任务书 tasks/ds41f_live_futures_reliability.md。

边界：全程不联网（所有取数对象为替身）、不写真实 results/（csv_dir 与限流状态
全部落在 TemporaryDirectory）、不改评分/权重/门槛/币池。

本文件分两部分：
  · RequestBudgetTests  —— 用真实 public_selection 调用链统计方法调用数（供权重换算）
  · GovernorFixTests    —— 三个"先用失败用例证明、修复后转绿"的行为钉死
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, main
from unittest.mock import Mock, patch

import ccxt

import binance_box_strategy as base

T0 = 1_700_000_000_000
EGRESS = "futures|direct"


class FakeClock:
    """假时钟：让"等待 5 分钟"在测试里瞬间完成，且时间可控可跳变。"""

    def __init__(self, start=T0):
        self.t = start
        self.waits = []

    def now_ms(self):
        return self.t

    def slept(self, seconds, step=1.0):
        self.waits.append(seconds)
        self.t += int(seconds * 1000) + 1  # 睡完时钟前进，避免死循环

    def advance_ms(self, ms):
        self.t += ms



def rising_klines(n=240):
    """缓涨 + 量能缓增：趋势(5) + 24h量增(1) = 6 分。"""
    return [[T0 + i * 3_600_000, 100 + i / 10, 101 + i / 10, 99 + i / 10,
             100 + i / 10, 10 + i * 0.05] for i in range(n)]


class CountingSpot:
    """现货替身：只计数，不发网络请求。"""

    def __init__(self, symbols):
        self.symbols = list(symbols)
        self._markets = {s: {"base": s.split("/")[0], "spot": True, "active": True}
                         for s in self.symbols}
        self.tickers_calls = 0
        self.ohlcv_calls = 0

    def market(self, symbol):
        return self._markets[symbol]

    def fetch_tickers(self):
        self.tickers_calls += 1
        return {s: {"quoteVolume": 2_000_000} for s in self.symbols}

    def fetch_ohlcv(self, symbol, timeframe, limit=None):
        self.ohlcv_calls += 1
        return rising_klines()


class CountingFutures:
    """合约替身：分端点计数。"""

    def __init__(self, symbols, ls_top_fails=False):
        # symbols 形如 ("AAA/USDT:USDT", ...)
        self.symbols = list(symbols)
        self.markets = {s: {"base": s.split("/")[0], "quote": "USDT", "swap": True,
                            "linear": True, "active": True} for s in self.symbols}
        self.ls_top_fails = ls_top_fails
        self.tickers_calls = 0
        self.oi_hist_calls = 0
        self.ls_top_calls = 0
        self.ls_fallback_calls = 0
        self.oi_snapshot_calls = 0
        self.funding_calls = 0

    def market(self, symbol):
        return {"id": symbol.replace("/", "").replace(":", "")}

    def fetch_tickers(self):
        self.tickers_calls += 1
        return {s: {"quoteVolume": 20_000_000, "last": 124} for s in self.symbols}

    def fapiDataGetOpenInterestHist(self, params):
        self.oi_hist_calls += 1
        return [{"sumOpenInterestValue": 100 + i,
                 "timestamp": T0 - (200 - i) * 3_600_000} for i in range(200)]

    def fapiDataGetTopLongShortPositionRatio(self, params):
        self.ls_top_calls += 1
        if self.ls_top_fails:
            raise ccxt.ExchangeError("endpoint gone")
        return [{"longShortRatio": 1.5,
                 "timestamp": T0 - (200 - i) * 3_600_000} for i in range(200)]

    def fetch_long_short_ratio_history(self, symbol, timeframe, limit=None):
        self.ls_fallback_calls += 1
        return [{"longShortRatio": 1.1, "timestamp": T0}]

    def fetch_open_interest(self, symbol):
        self.oi_snapshot_calls += 1
        return {"openInterestValue": 500_000}

    def fetch_funding_rate(self, symbol):
        self.funding_calls += 1
        return {"fundingRate": 0.0001}


def make_cfg(tmp, **kw):
    opts = dict(use_coingecko=False, csv_dir=tmp, proxy=None,
                rate_limit_state=str(Path(tmp) / "state.json"))
    opts.update(kw)
    return replace(base.Config(), **opts)


class OIFailingFutures(CountingFutures):
    """OI 历史接口抛普通网络错误（用于验证失败请求也计数、不回退）。"""

    def fapiDataGetOpenInterestHist(self, params):
        self.oi_hist_calls += 1
        raise ccxt.NetworkError("boom")


class OILimitFutures(CountingFutures):
    """OI 历史接口抛 418/429（用于验证不再继续后面的取数分支）。"""

    def __init__(self, symbols, exc=None):
        super().__init__(symbols)
        self._exc = exc or ccxt.DDoSProtection(
            "IP banned until 9999999999999. code=-1003")

    def fapiDataGetOpenInterestHist(self, params):
        self.oi_hist_calls += 1
        raise self._exc



class RequestBudgetTests(TestCase):
    """统计一轮真实 public_selection 的方法调用数（不联网）。"""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base.clear_deriv_cache()

    def _scan(self, n, cfg=None):
        cfg = cfg or make_cfg(self.tmp.name)
        spot = CountingSpot([f"C{i}/USDT" for i in range(n)])
        fut = CountingFutures([f"C{i}/USDT:USDT" for i in range(n)])
        with patch.object(base, "okx_exchange", return_value=None):
            rows = base.public_selection(spot, fut, cfg)
        return spot, fut, rows

    def test_cold_cache_request_counts_scale_linearly(self):
        """冷缓存：K线 N 次；OI 历史 N 次；大户多空 N 次。"""
        spot, fut, rows = self._scan(6)
        self.assertEqual(spot.tickers_calls, 1)
        self.assertEqual(spot.ohlcv_calls, 6)
        self.assertEqual(fut.tickers_calls, 1)
        self.assertEqual(fut.oi_hist_calls, 6)
        self.assertEqual(fut.ls_top_calls, 6)
        self.assertEqual(fut.ls_fallback_calls, 0)
        self.assertEqual(len(rows), 6)

    def test_warm_cache_within_same_hour_skips_deriv(self):
        """同一小时内第二轮：合约历史接口 0 次（只缓存 ok）。"""
        cfg = make_cfg(self.tmp.name)
        spot = CountingSpot(["AAA/USDT"])
        fut = CountingFutures(["AAA/USDT:USDT"])
        with patch.object(base, "okx_exchange", return_value=None), \
                patch.object(base, "_now_ms", return_value=T0 + 1_000):
            base.public_selection(spot, fut, cfg)
            first = (fut.oi_hist_calls, fut.ls_top_calls)
            base.public_selection(spot, fut, cfg)
            second = (fut.oi_hist_calls, fut.ls_top_calls)
        self.assertEqual(first, (1, 1))
        self.assertEqual(second, (1, 1))          # 未增加

    def test_cache_lost_across_restart_refetches(self):
        """模拟重启：清空进程内缓存后，同小时也要重新取。"""
        cfg = make_cfg(self.tmp.name)
        spot = CountingSpot(["AAA/USDT"])
        fut = CountingFutures(["AAA/USDT:USDT"])
        with patch.object(base, "okx_exchange", return_value=None), \
                patch.object(base, "_now_ms", return_value=T0 + 1_000):
            base.public_selection(spot, fut, cfg)
            base.clear_deriv_cache()               # 进程重启 = 内存缓存清零
            base.public_selection(spot, fut, cfg)
        self.assertEqual(fut.oi_hist_calls, 2)
        self.assertEqual(fut.ls_top_calls, 2)

    def test_fallback_caliber_adds_one_request_per_symbol(self):
        """大户接口失败走回退口径：每个币多 1 次请求（N → 3N）。"""
        cfg = make_cfg(self.tmp.name)
        spot = CountingSpot(["AAA/USDT"])
        fut = CountingFutures(["AAA/USDT:USDT"], ls_top_fails=True)
        with patch.object(base, "okx_exchange", return_value=None):
            base.public_selection(spot, fut, cfg)
        self.assertEqual(fut.oi_hist_calls, 1)
        self.assertEqual(fut.ls_top_calls, 1)
        self.assertEqual(fut.ls_fallback_calls, 1)

    def test_extras_only_for_shown_rows(self):
        """OI 快照/费率只对展示行补取：不随 N 线性增长。"""
        n = 20
        cfg = make_cfg(self.tmp.name)
        spot = CountingSpot([f"C{i}/USDT" for i in range(n)])
        fut = CountingFutures([f"C{i}/USDT:USDT" for i in range(n)])
        with patch.object(base, "okx_exchange", return_value=None):
            rows = base.public_selection(spot, fut, cfg)
        chosen = [r for r in rows if r["score"] >= cfg.min_score]
        shown = len(chosen) if chosen else min(10, len(rows))
        self.assertEqual(fut.oi_snapshot_calls, shown)
        self.assertEqual(fut.funding_calls, shown)

    def test_futures_pause_stops_deriv_but_keeps_spot(self):
        """合约域暂停：合约历史 0 次，现货 K 线照常 N 次。"""
        cfg = make_cfg(self.tmp.name)
        state = base.RateLimitState.load(base.default_state_path(cfg))
        state.record_pause("futures|direct", base._now_ms() + 3_600_000, "seed")
        spot = CountingSpot(["AAA/USDT", "BBB/USDT"])
        fut = CountingFutures(["AAA/USDT:USDT", "BBB/USDT:USDT"])
        with patch.object(base, "okx_exchange", return_value=None):
            base.public_selection(spot, fut, cfg)
        self.assertEqual(spot.ohlcv_calls, 2)
        self.assertEqual(fut.oi_hist_calls, 0)
        self.assertEqual(fut.ls_top_calls, 0)


class GovernorFixTests(TestCase):
    """先用失败用例钉死缺陷，修复后转绿。"""

    def test_shorter_pause_must_not_override_longer(self):
        """同域短暂停不得覆盖长暂停（期限取最晚）。"""
        state = base.RateLimitState.load(None)
        state.record_pause("futures|direct", 10**13, "long ban", now=1_000)
        state.record_pause("futures|direct", 2_000, "short ban", now=1_500)
        self.assertEqual(state.until("futures|direct"), 10**13)

    def test_longer_pause_does_override_shorter(self):
        """更长的期限仍应生效（避免只取最晚导致无法延长）。"""
        state = base.RateLimitState.load(None)
        state.record_pause("futures|direct", 2_000, "short", now=1_000)
        state.record_pause("futures|direct", 10**13, "long", now=1_500)
        self.assertEqual(state.until("futures|direct"), 10**13)

    def test_limit_ban_does_not_fall_back_to_direct_egress(self):
        """代理出口被 418 封禁后，不得自动改用直连出口继续请求。"""
        with TemporaryDirectory() as tmp:
            cfg = replace(base.Config(), proxy="http://127.0.0.1:7897",
                          csv_dir=tmp,
                          rate_limit_state=str(Path(tmp) / "state.json"))
            via_proxy = Mock()
            via_proxy.load_markets.side_effect = ccxt.DDoSProtection(
                "IP(203.10.99.42) banned until 9999999999999.")
            via_direct = Mock()

            def fake_build(c, dtype):
                if dtype == "spot":
                    return Mock()
                return via_proxy if c.proxy else via_direct

            with patch.object(base, "build_exchange", side_effect=fake_build):
                base.connect_exchanges(cfg)
            self.assertEqual(via_direct.load_markets.call_count, 0)

    def test_network_error_may_still_fall_back(self):
        """普通网络错误（代理不可用）仍允许回退直连——不要误伤可用性。"""
        with TemporaryDirectory() as tmp:
            cfg = replace(base.Config(), proxy="http://127.0.0.1:7897",
                          csv_dir=tmp,
                          rate_limit_state=str(Path(tmp) / "state.json"))
            via_proxy = Mock()
            via_proxy.load_markets.side_effect = ccxt.NetworkError("proxy down")
            via_direct = Mock()

            def fake_build(c, dtype):
                if dtype == "spot":
                    return Mock()
                return via_proxy if c.proxy else via_direct

            with patch.object(base, "build_exchange", side_effect=fake_build):
                base.connect_exchanges(cfg)
            self.assertEqual(via_direct.load_markets.call_count, 1)

    def test_spot_ticker_ban_is_recorded(self):
        """现货 fetch_tickers 收到 418 必须登记暂停（该请求当前未走 guard）。"""
        with TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            spot = CountingSpot(["AAA/USDT"])
            spot.fetch_tickers = Mock(side_effect=ccxt.DDoSProtection(
                "IP banned until 9999999999999."))
            fut = CountingFutures(["AAA/USDT:USDT"])
            with patch.object(base, "okx_exchange", return_value=None):
                with self.assertRaises(ccxt.DDoSProtection):
                    base.public_selection(spot, fut, cfg)
            state = base.RateLimitState.load(base.default_state_path(cfg))
            self.assertTrue(any(k.startswith("spot|") for k in state.domains),
                            f"现货封禁未登记：{state.domains}")


class DataQuotaTests(TestCase):
    """/futures/data/* 的独立额度（官方 1000 requests/5min）自限（滑动窗口 + 跨进程）。"""

    EGRESS = "futures|direct"

    def test_quota_counts_deriv_requests_and_persists(self):
        with TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            base.clear_deriv_cache()
            n = 5
            spot = CountingSpot([f"C{i}/USDT" for i in range(n)])
            fut = CountingFutures([f"C{i}/USDT:USDT" for i in range(n)])
            with patch.object(base, "okx_exchange", return_value=None):
                base.public_selection(spot, fut, cfg)
            state = base.RateLimitState.load(base.default_state_path(cfg))
            # 每个币 OI 历史 + 大户多空 = 2 次，且已落盘（重启后仍在）
            self.assertEqual(len(state.data_quota[self.EGRESS]), 2 * n)

    def test_delay_only_after_limit(self):
        state = base.RateLimitState.load(None)
        now = 1_000_000
        for _ in range(base.DATA_QUOTA_LIMIT - 1):
            state.note_data_request(self.EGRESS, now)
        self.assertEqual(state.data_quota_delay_ms(self.EGRESS, now), 0)
        state.note_data_request(self.EGRESS, now)
        self.assertGreater(state.data_quota_delay_ms(self.EGRESS, now), 0)

    def test_window_slides_not_fixed(self):
        """滑动窗口：满额后不到 5 分钟不释放，超过 5 分钟才释放。"""
        state = base.RateLimitState.load(None)
        now = 1_000_000
        for _ in range(base.DATA_QUOTA_LIMIT):
            state.note_data_request(self.EGRESS, now)
        self.assertGreater(state.data_quota_delay_ms(self.EGRESS, now + 1), 0)
        self.assertEqual(state.data_quota_delay_ms(self.EGRESS, now + base.DATA_QUOTA_WINDOW_MS + 1), 0)

    def test_quota_persists_across_restart(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            first = base.RateLimitState.load(path)
            for _ in range(50):
                first.note_data_request(self.EGRESS, 1_000_000)
            first.save()
            second = base.RateLimitState.load(path)  # 模拟重启
            self.assertEqual(len(second.data_quota[self.EGRESS]), 50)

    def test_scan_waits_instead_of_hammering_when_quota_exhausted(self):
        """额度用满：先等待到窗口重置再发，不硬打（用假时钟，不真等 5 分钟）。"""
        with TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            base.clear_deriv_cache()
            clock = FakeClock(1_700_000_000_000)
            seed = base.RateLimitState.load(base.default_state_path(cfg))
            for _ in range(base.DATA_QUOTA_LIMIT):
                seed.note_data_request(self.EGRESS, clock.now_ms())
            seed.save()
            spot = CountingSpot(["AAA/USDT"])
            fut = CountingFutures(["AAA/USDT:USDT"])
            with patch.object(base, "okx_exchange", return_value=None), \
                    patch.object(base, "_now_ms", side_effect=clock.now_ms), \
                    patch.object(base, "_sleep_interruptible", side_effect=clock.slept):
                base.public_selection(spot, fut, cfg)
            self.assertTrue(clock.waits, "额度用满时应先等待，而不是继续发请求")
            self.assertGreaterEqual(fut.oi_hist_calls, 1, "等待结束后应正常取数")


class R2QuotaSemanticsTests(TestCase):
    """R2：计数落盘、失败不回退、边界与时间跳变、等待中暂停、429 不续分支。"""

    def _guard(self, state, cfg):
        return base.RequestGuard(state, cfg)

    def test_reservation_is_persisted_immediately(self):
        """预约后立刻落盘：另一个进程（新 load）也能看到这条计数。"""
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            state = base.RateLimitState.load(path)
            with state.locked():
                state.reload()
                delay = state.reserve_data_request(EGRESS, now=T0)
            self.assertEqual(delay, 0)
            self.assertTrue(path.exists(), "预约后应立即落盘，而不是只留在内存")
            other = base.RateLimitState.load(path)  # 模拟另一个进程
            self.assertEqual(len(other.data_quota[EGRESS]), 1)

    def test_reload_syncs_quota_not_only_pauses(self):
        """reload 必须同时同步额度窗口，不能只同步暂停表。"""
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            writer = base.RateLimitState.load(path)
            for _ in range(3):
                writer.note_data_request(EGRESS, T0)
            writer.save()
            reader = base.RateLimitState.load(path)
            reader.reload()
            self.assertEqual(len(reader.data_quota[EGRESS]), 3)

    def test_failed_request_count_does_not_roll_back(self):
        """请求失败（网络错误）不得把已预约的计数退回。"""
        with TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            base.clear_deriv_cache()
            fut = OIFailingFutures(["AAA/USDT:USDT"])
            spot = CountingSpot(["AAA/USDT"])
            with patch.object(base, "okx_exchange", return_value=None):
                base.public_selection(spot, fut, cfg)
            state = base.RateLimitState.load(base.default_state_path(cfg))
            # OI 失败 1 次 + 大户多空成功 1 次 = 2；失败的那次仍计数
            self.assertEqual(fut.oi_hist_calls, 1)
            self.assertEqual(len(state.data_quota[EGRESS]), 2)

    def test_boundary_does_not_release_one_ms_early(self):
        """满额后，第 5 分钟差 1 毫秒仍不放行，跨过才放行。"""
        state = base.RateLimitState.load(None)
        for _ in range(base.DATA_QUOTA_LIMIT):
            state.note_data_request(EGRESS, T0)
        self.assertGreater(state.data_quota_delay_ms(EGRESS, T0 + base.DATA_QUOTA_WINDOW_MS - 1), 0)
        self.assertEqual(state.data_quota_delay_ms(EGRESS, T0 + base.DATA_QUOTA_WINDOW_MS), 0)

    def test_clock_jump_backward_does_not_release_early(self):
        """时钟回跳（now 变小）不得把窗口内计数当成过期而提前放行。"""
        state = base.RateLimitState.load(None)
        for _ in range(base.DATA_QUOTA_LIMIT):
            state.note_data_request(EGRESS, T0)
        jumped_back = T0 - 10 * base.DATA_QUOTA_WINDOW_MS
        self.assertGreater(state.data_quota_delay_ms(EGRESS, jumped_back), 0)

    def test_pause_during_wait_blocks_the_request(self):
        """等待额度期间若被登记暂停，等待结束后必须重新检查、不得发出请求。"""
        with TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            base.clear_deriv_cache()
            clock = FakeClock(T0)
            seed = base.RateLimitState.load(base.default_state_path(cfg))
            for _ in range(base.DATA_QUOTA_LIMIT):
                seed.note_data_request(EGRESS, clock.now_ms())
            seed.save()
            state = base.RateLimitState.load(base.default_state_path(cfg))
            guard = self._guard(state, cfg)
            fut = CountingFutures(["AAA/USDT:USDT"])

            def sleep_then_pause(seconds, step=1.0):
                clock.slept(seconds, step)
                state.record_pause(EGRESS, clock.now_ms() + 3_600_000, "injected", now=clock.now_ms())

            with patch.object(base, "_now_ms", side_effect=clock.now_ms), \
                    patch.object(base, "_sleep_interruptible", side_effect=sleep_then_pause):
                out = base._fetch_deriv_context_live(fut, "AAA/USDT:USDT", cfg, guard)
            self.assertEqual(out["status"], "paused")
            self.assertEqual(fut.oi_hist_calls, 0, "等待期间被暂停后不得再发请求")
            self.assertEqual(fut.ls_top_calls, 0)

    def test_418_does_not_continue_to_next_branch(self):
        """OI 端点收到 418：登记暂停、立刻返回，不得接着请求大户多空。"""
        with TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            base.clear_deriv_cache()
            state = base.RateLimitState.load(base.default_state_path(cfg))
            guard = self._guard(state, cfg)
            fut = OILimitFutures(["AAA/USDT:USDT"])
            out = base._fetch_deriv_context_live(fut, "AAA/USDT:USDT", cfg, guard)
            self.assertEqual(out["status"], "paused")
            self.assertEqual(fut.oi_hist_calls, 1)
            self.assertEqual(fut.ls_top_calls, 0, "429/418 后不得继续另一取数分支")
            self.assertGreater(state.until(EGRESS), base._now_ms())

    def test_late_success_does_not_clear_new_pause(self):
        """晚到的成功不能抹掉等待期间/其它请求登记的新暂停（只清已到期的）。"""
        state = base.RateLimitState.load(None)
        state.record_pause(EGRESS, T0 + 3_600_000, "new ban", now=T0)
        self.assertFalse(state.clear_if_expired(EGRESS, now=T0 + 60_000))
        self.assertEqual(state.until(EGRESS), T0 + 3_600_000)
        self.assertTrue(state.clear_if_expired(EGRESS, now=T0 + 3_600_001))
        self.assertEqual(state.until(EGRESS), 0)

    def test_raw_whole_table_write_loses_update(self):
        """反例：绕过语义方法、各自整表写盘 → 后写覆盖前写（丢失更新）。

        这是 R2 遗留缺陷的原始形态，保留作对照，说明为什么必须走语义方法。
        """
        with TemporaryDirectory() as tmp:
            p = Path(tmp) / "raw.json"
            base.RateLimitState.load(p).save()          # 建空文件
            a = base.RateLimitState.load(p)
            b = base.RateLimitState.load(p)             # 与 a 同一份快照
            a.data_quota[EGRESS] = [T0]                 # 各自改内存
            a.save()
            b.data_quota[EGRESS] = [T0]                 # b 仍基于旧快照
            b.save()
            self.assertEqual(len(base.RateLimitState.load(p).data_quota[EGRESS]), 1,
                             "两次预约只剩一条 → 少算一次")

    def test_public_api_never_loses_update(self):
        """修复：走 reserve_data_request，两个实例各自读到的都是磁盘最新，不丢计数。"""
        with TemporaryDirectory() as tmp:
            p = Path(tmp) / "safe.json"
            base.RateLimitState.load(p).save()
            a, b = base.RateLimitState.load(p), base.RateLimitState.load(p)
            self.assertEqual(a.reserve_data_request(EGRESS, T0), 0)
            self.assertEqual(b.reserve_data_request(EGRESS, T0), 0)
            self.assertEqual(len(base.RateLimitState.load(p).data_quota[EGRESS]), 2,
                             "两次预约都应保留")

    def test_normal_scan_scope_unchanged(self):
        """额度充足时，正常扫描的币池与请求数不变（本轮不改筛选范围）。"""
        with TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            base.clear_deriv_cache()
            n = 7
            spot = CountingSpot([f"C{i}/USDT" for i in range(n)])
            fut = CountingFutures([f"C{i}/USDT:USDT" for i in range(n)])
            with patch.object(base, "okx_exchange", return_value=None):
                rows = base.public_selection(spot, fut, cfg)
            self.assertEqual(len(rows), n)
            self.assertEqual(fut.oi_hist_calls, n)
            self.assertEqual(fut.ls_top_calls, n)


class R3CrossInstanceTests(TestCase):
    """R3：同一状态文件的**全部**修改路径都要「锁内重读 → 合并/条件判断 → 保存」。

    对应主代理 `reports/live_futures_r2_final_review.md` 的 ds 阻塞：
    跨实例登记的新暂停，不得被旧请求成功后的保存覆盖。用**两个独立实例**
    （而非同一实例）复现与验证。
    """

    def _seed_expired(self, path, now):
        """播种一条**已过期**的暂停，模拟"A 读到旧暂停、开始恢复请求"。"""
        st = base.RateLimitState.load(path)
        st.domains[EGRESS] = {"banned_until_ms": now - 1,
                              "paused_since_ms": now - 10_000,
                              "probe_at_ms": 0, "reason": "old"}
        st.save()
        return st

    def test_new_pause_survives_late_success_via_request_guard(self):
        """主代理报告的最小复现：A 成功返回后，B 登记的新暂停必须还在。"""
        with TemporaryDirectory() as tmp:
            p = Path(tmp) / "state.json"
            now = base._now_ms()
            a = self._seed_expired(p, now)
            guard = base.RequestGuard(a, make_cfg(tmp))

            def late_success():
                other = base.RateLimitState.load(p)          # 另一个实例
                other.record_pause(EGRESS, now + 3_600_000, "new pause", now=now)
                return "ok"

            self.assertEqual(guard.run(base.FUTURES_DOMAIN, late_success), "ok")
            self.assertEqual(base.RateLimitState.load(p).until(EGRESS), now + 3_600_000,
                             "跨实例登记的新暂停被旧成功清除了")

    def test_new_quota_survives_late_success_via_request_guard(self):
        """同一路径下，等待期间由另一实例登记的额度也不得被覆盖。"""
        with TemporaryDirectory() as tmp:
            p = Path(tmp) / "state.json"
            now = base._now_ms()
            a = self._seed_expired(p, now)
            guard = base.RequestGuard(a, make_cfg(tmp))

            def late_success():
                other = base.RateLimitState.load(p)
                other.record_pause(EGRESS, now + 3_600_000, "new pause", now=now)
                other.reserve_data_request(EGRESS, now=now)
                return "ok"

            guard.run(base.FUTURES_DOMAIN, late_success)
            final = base.RateLimitState.load(p)
            self.assertEqual(final.until(EGRESS), now + 3_600_000, "新暂停必须保留")
            self.assertEqual(len(final.data_quota[EGRESS]), 1, "新登记的额度必须保留")

    def test_record_pause_merges_disk_max_not_stale_copy(self):
        """B 用旧副本写短暂停，不得把磁盘上更晚的暂停缩短。"""
        with TemporaryDirectory() as tmp:
            p = Path(tmp) / "state.json"
            base.RateLimitState.load(p).save()
            a, b = base.RateLimitState.load(p), base.RateLimitState.load(p)
            a.record_pause(EGRESS, T0 + 3_600_000, "long", now=T0)
            b.record_pause(EGRESS, T0 + 60_000, "short", now=T0)
            self.assertEqual(base.RateLimitState.load(p).until(EGRESS), T0 + 3_600_000)

    def test_mark_probe_keeps_new_pause(self):
        """A 的 mark_probe 不得抹掉 B 刚登记的新暂停（只应更新探测字段）。"""
        with TemporaryDirectory() as tmp:
            p = Path(tmp) / "state.json"
            base.RateLimitState.load(p).save()
            a, b = base.RateLimitState.load(p), base.RateLimitState.load(p)
            a.record_pause(EGRESS, T0 + 1_000, "old", now=T0)
            b.record_pause(EGRESS, T0 + 3_600_000, "new", now=T0)
            a.mark_probe(EGRESS, now=T0 + 2_000)
            final = base.RateLimitState.load(p)
            self.assertEqual(final.until(EGRESS), T0 + 3_600_000)
            self.assertEqual(final.domains[EGRESS]["probe_at_ms"], T0 + 2_000)

    def test_clear_if_expired_judges_on_disk_not_local_copy(self):
        """条件清除必须看磁盘最新：本地旧副本已过期，但磁盘上是新暂停 → 不清。"""
        with TemporaryDirectory() as tmp:
            p = Path(tmp) / "state.json"
            a = self._seed_expired(p, T0)
            b = base.RateLimitState.load(p)
            b.record_pause(EGRESS, T0 + 3_600_000, "new", now=T0)
            self.assertFalse(a.clear_if_expired(EGRESS, now=T0),
                             "磁盘上是未到期的新暂停，不得清除")
            self.assertEqual(base.RateLimitState.load(p).until(EGRESS), T0 + 3_600_000)
            # 磁盘上确已过期时仍应正常清除
            self.assertTrue(a.clear_if_expired(EGRESS, now=T0 + 3_600_001))
            self.assertEqual(base.RateLimitState.load(p).until(EGRESS), 0)

    def test_nested_locked_does_not_deadlock(self):
        """锁内再调会加锁的方法不得自锁死（同实例重入）。"""
        with TemporaryDirectory() as tmp:
            p = Path(tmp) / "state.json"
            st = base.RateLimitState.load(p)
            out = []

            def work():
                with st.locked():
                    st.record_pause(EGRESS, T0 + 60_000, "x", now=T0)
                    with st.locked():                       # 嵌套
                        st.mark_probe(EGRESS, now=T0)
                        out.append(st.reserve_data_request(EGRESS, now=T0))
                    st.clear_if_expired(EGRESS, now=T0 + 60_001)
                out.append("done")

            th = threading.Thread(target=work, daemon=True)
            th.start()
            th.join(timeout=15)
            self.assertFalse(th.is_alive(), "嵌套加锁自锁死了")
            self.assertEqual(out, [0, "done"])

    def test_two_threads_same_process_only_one_reserves(self):
        """同进程两个线程、各用独立实例，争抢最后一个名额只允许一个通过。"""
        with TemporaryDirectory() as tmp:
            p = Path(tmp) / "state.json"
            seed = base.RateLimitState.load(p)
            for _ in range(base.DATA_QUOTA_LIMIT - 1):
                seed.note_data_request(EGRESS, T0)
            seed.save()
            barrier = threading.Barrier(2)
            out: dict[str, int] = {}

            def worker(name):
                st = base.RateLimitState.load(p)
                barrier.wait()
                out[name] = st.reserve_data_request(EGRESS, T0)

            threads = [threading.Thread(target=worker, args=(n,), daemon=True)
                       for n in ("a", "b")]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=20)
            self.assertTrue(all(not t.is_alive() for t in threads), "线程间争抢死锁了")
            vals = sorted(out.values())
            self.assertEqual(vals[0], 0)
            self.assertGreater(vals[1], 0, "第二个线程必须被挡住")
            self.assertEqual(len(base.RateLimitState.load(p).data_quota[EGRESS]),
                             base.DATA_QUOTA_LIMIT, "并发不得多算/少算")


_RACE_CHILD = r'''
import os, sys, time
sys.path.insert(0, sys.argv[1])
import binance_box_strategy as b
state_path, egress, go_file = sys.argv[2], sys.argv[3], sys.argv[4]
state = b.RateLimitState.load(state_path)
deadline = time.time() + 30
while not os.path.exists(go_file):
    if time.time() > deadline:
        break
    time.sleep(0.005)
with state.locked():
    state.reload()
    delay = state.reserve_data_request(egress)
sys.stdout.write(str(delay))
'''


class R2CrossProcessTests(TestCase):
    """R2：真实双进程争抢最后一个额度名额，只允许一个通过。"""

    def test_two_processes_race_last_slot_only_one_passes(self):
        if not sys.executable:
            self.skipTest("no python executable")
        root = str(Path(base.__file__).resolve().parent)
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            seed = base.RateLimitState.load(path)
            now = base._now_ms()
            for _ in range(base.DATA_QUOTA_LIMIT - 1):   # 只剩 1 个名额
                seed.note_data_request(EGRESS, now)
            seed.save()
            go = Path(tmp) / "go"
            procs = [subprocess.Popen(
                [sys.executable, "-c", _RACE_CHILD, root, str(path), EGRESS, str(go)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                for _ in range(2)]
            time.sleep(0.4)          # 让两个子进程都进入自旋等待
            go.write_text("go")
            results = []
            for proc in procs:
                out, err = proc.communicate(timeout=60)
                self.assertEqual(proc.returncode, 0, err)
                results.append(int(out.strip()))
            results.sort()
            self.assertEqual(results[0], 0, "应恰好有一个进程拿到名额")
            self.assertGreater(results[1], 0, "另一个进程必须被挡住并返回等待")
            final = base.RateLimitState.load(path)
            self.assertEqual(len(final.data_quota[EGRESS]), base.DATA_QUOTA_LIMIT,
                             "总计数不得因并发而多算/少算")


if __name__ == "__main__":
    main()
