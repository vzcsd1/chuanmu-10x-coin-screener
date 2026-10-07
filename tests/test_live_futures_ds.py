"""实时合约取数可靠性：请求预算、出口回退、暂停期限合并的离线取证（ds4.1f）。

对应任务书 tasks/ds41f_live_futures_reliability.md。

边界：全程不联网（所有取数对象为替身）、不写真实 results/（csv_dir 与限流状态
全部落在 TemporaryDirectory）、不改评分/权重/门槛/币池。

本文件分两部分：
  · RequestBudgetTests  —— 用真实 public_selection 调用链统计方法调用数（供权重换算）
  · GovernorFixTests    —— 三个"先用失败用例证明、修复后转绿"的行为钉死
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, main
from unittest.mock import Mock, patch

import ccxt

import binance_box_strategy as base

T0 = 1_700_000_000_000


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


if __name__ == "__main__":
    main()
