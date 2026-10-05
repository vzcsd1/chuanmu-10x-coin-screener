"""请求治理（P0 请求经济 / P1 限流退让 / P2 通路拆分）的离线行为检查；不联网。

覆盖场景（对应 2026-10-05 方案验收清单）：
  · 封禁恢复时间解析（banned until 毫秒/秒级、Retry-After、无信息）
  · 暂停状态跨"重启"持久化；状态文件损坏按 fail-open 处理
  · 暂停期不触碰网络；到期后只发一次恢复探测，失败进入冷却
  · 合约域被封时现货照常出池，合约数据标注 paused 而不是伪装成正常
  · 启动时合约被封：connect_exchanges 降级为 (spot, None)，且不重试轰炸
  · 1h 粒度合约数据按小时缓存：同一小时内第二轮不再请求
  · OI 快照/费率只对展示行补取，其余行明确标注 extras_deferred
  · fetchMarkets 按域收窄；市值缓存同日复用
"""
from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
from tempfile import TemporaryDirectory
from unittest import TestCase, main
from unittest.mock import Mock, patch

import ccxt

import binance_box_strategy as base

T0 = 1_700_000_000_000


def rising_klines(n=240):
    # 缓涨 + 量能缓增：趋势(5) + 24h量增(1) = 6 分
    return [[T0 + i * 3_600_000, 100 + i / 10, 101 + i / 10, 99 + i / 10,
             100 + i / 10, 10 + i * 0.05] for i in range(n)]


def flat_klines(n=240):
    # 一路阴跌、量能持平：趋势/突破/量增全不成立 → K 线分 0
    return [[T0 + i * 3_600_000, 120 - i / 10, 121 - i / 10, 119 - i / 10,
             120 - i / 10, 10.0] for i in range(n)]


class GovernorUnitTests(TestCase):
    def test_parse_ban_until_variants(self):
        self.assertEqual(base.parse_ban_until("IP banned until 1699999999999."),
                         1699999999999)
        self.assertEqual(base.parse_ban_until("banned until 1699999999"),
                         1699999999000)  # 秒级时间戳归一到毫秒
        later = base.parse_ban_until("xxx Retry-After: 120")
        self.assertGreaterEqual(later, base._now_ms() + 119_000)
        self.assertEqual(base.parse_ban_until("no info"), 0)

    def test_state_persists_across_restart_and_fails_open(self):
        with TemporaryDirectory() as tmp:
            path = base.Path(tmp) / "state.json"
            first = base.RateLimitState.load(path)
            first.record_pause("futures|direct", 10**13, "test", now=1_000)
            second = base.RateLimitState.load(path)  # 模拟重启后重新读盘
            self.assertFalse(second.should_attempt("futures|direct", now=2_000))
            self.assertEqual(second.until("futures|direct"), 10**13)
            self.assertTrue(second.should_attempt("futures|direct", now=10**13 + 1))
            # 状态文件损坏 → fail-open，按无暂停处理
            path.write_text("{not json", encoding="utf-8")
            broken = base.RateLimitState.load(path)
            self.assertEqual(broken.domains, {})
            self.assertTrue(broken.should_attempt("futures|direct"))

    def test_single_probe_per_pause_window(self):
        state = base.RateLimitState.load(None)  # 仅内存
        key = "futures|direct"
        clock = {"now": 1_000_000}
        fn = Mock(side_effect=[
            ccxt.RateLimitExceeded("Retry-After: 60"),  # 探测失败 → 按服务端时间重新登记
            ccxt.RequestTimeout("net"),                  # 下次探测遇到网络错误（无新暂停信息）
            "ok",                                        # 冷却结束后恢复成功
        ])
        with patch.object(base, "_now_ms", lambda: clock["now"]):
            # 种子：一个还剩 500ms 到期的暂停（直接写入，绕过 record_pause 的钳制）
            state.domains[key] = {"banned_until_ms": 1_000_500, "probe_at_ms": 0,
                                  "paused_since_ms": 900_000, "reason": "seed"}
            guard = base.RequestGuard(state, replace(base.Config()))
            with self.assertRaises(base.DomainPaused):
                guard.run("futures", fn)               # 暂停期内：直接拒绝，不碰网络
            clock["now"] = 1_000_600                   # 暂停到期
            with self.assertRaises(ccxt.RateLimitExceeded):
                guard.run("futures", fn)               # 到期后的第一次调用 = 恢复探测
            with self.assertRaises(base.DomainPaused):
                guard.run("futures", fn)               # 新暂停期内：拒绝
            state.domains[key]["banned_until_ms"] = 1  # 人为让新暂停到期
            with self.assertRaises(ccxt.RequestTimeout):
                guard.run("futures", fn)               # 新探测：这次遇到网络错误
            with self.assertRaises(base.DomainPaused):
                guard.run("futures", fn)               # 探测无新暂停信息 → 进入冷却，仍拒绝
            clock["now"] = 1_000_600 + base.PROBE_COOLDOWN_MS  # 冷却结束
            self.assertEqual(guard.run("futures", fn), "ok")
        self.assertNotIn(key, state.domains)           # 恢复成功 → 清除暂停
        self.assertEqual(fn.call_count, 3)             # 每个暂停窗口只发一次探测

    def test_connect_degrades_when_futures_banned(self):
        with TemporaryDirectory() as tmp:
            cfg = replace(base.Config(), proxy=None, csv_dir=tmp,
                          rate_limit_state=str(base.Path(tmp) / "state.json"))
            spot, futures = Mock(), Mock()
            futures.load_markets.side_effect = ccxt.DDoSProtection(
                "IP banned until 9999999999999.")

            def fake_build(_cfg, dtype):
                return {"spot": spot, "future": futures}[dtype]

            with patch.object(base, "build_exchange", side_effect=fake_build):
                got_spot, got_futures, _ = base.connect_exchanges(cfg)
            self.assertIs(got_spot, spot)
            self.assertIsNone(got_futures)          # 合约降级，现货照常
            self.assertEqual(futures.load_markets.call_count, 1)  # 封禁后不重试轰炸
            persisted = base.RateLimitState.load(base.Path(cfg.rate_limit_state))
            self.assertTrue(any(k.startswith("futures|") for k in persisted.domains))

    def test_connect_raises_when_spot_banned(self):
        spot = Mock()
        spot.load_markets.side_effect = ccxt.DDoSProtection("IP banned until 9999999999999.")
        cfg = replace(base.Config(), proxy=None, csv_dir=None)
        with patch.object(base, "build_exchange", return_value=spot):
            with self.assertRaises(ccxt.DDoSProtection):
                base.connect_exchanges(cfg)


class ScanIntegrationTests(TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = replace(base.Config(), use_coingecko=False, csv_dir=self.tmp.name,
                           rate_limit_state=str(base.Path(self.tmp.name) / "state.json"))
        self.spot_markets = {
            name + "/USDT": {"base": name, "spot": True, "active": True}
            for name in ["SPOT", "SWAP"]
        }
        self.spot = Mock()
        self.spot.market.side_effect = self.spot_markets.__getitem__
        self.spot.fetch_tickers.return_value = {
            "SPOT/USDT": {"quoteVolume": 1_200_000},
            "SWAP/USDT": {"quoteVolume": 10_000_000},
        }
        self.spot.fetch_ohlcv.return_value = rising_klines()
        self.futures = Mock()
        self.futures.markets = {
            "SWAP/USDT:USDT": {"base": "SWAP", "quote": "USDT", "swap": True,
                               "linear": True, "active": True}
        }
        self.futures.fetch_tickers.return_value = {
            "SWAP/USDT:USDT": {"quoteVolume": 20_000_000, "last": 124}
        }
        self.futures.fetch_open_interest.return_value = {"openInterestValue": 500_000}
        self.futures.fetch_funding_rate.return_value = {"fundingRate": 0.0001}
        self.futures.fapiDataGetOpenInterestHist.return_value = []
        self.futures.fapiDataGetTopLongShortPositionRatio.return_value = []
        self.futures.market.return_value = {"id": "SWAPUSDT"}  # 真实 fetch_deriv_context 会取 market_id
        self.deriv_patcher = patch.object(base, "fetch_deriv_context", return_value={
            "oi_chg_1d": 0.15, "oi_chg_3d": 0.30, "ls_top": 1.2,
            "ls_source": "topLongShortPositionRatio",
        })
        self.deriv = self.deriv_patcher.start()
        self.addCleanup(patch.stopall)
        base.clear_deriv_cache()

    def stop_deriv_mock(self):
        """停用整函数 mock，让测试走真实的 fetch_deriv_context / 缓存层。"""
        self.deriv_patcher.stop()

    def test_futures_ban_midround_keeps_spot_output_and_marks_paused(self):
        self.futures.fetch_tickers.side_effect = ccxt.DDoSProtection(
            "IP banned until 9999999999999.")
        self.deriv.side_effect = base.DomainPaused("futures 暂停")  # 模拟 guard 拒绝
        with patch.object(base, "okx_exchange", return_value=None), \
                redirect_stdout(StringIO()) as output:
            chosen = base.run_once(self.spot, self.futures, self.cfg)
        by_symbol = {row["symbol"]: row for row in chosen}
        self.assertIn("SPOT/USDT", by_symbol)            # 现货照常出池
        swap = by_symbol["SWAP/USDT"]
        self.assertTrue(swap["has_futures"])             # 有合约 ≠ 取得到
        self.assertEqual(swap["deriv_status"], "paused") # 如实标注，不冒充正常
        self.assertIsNone(swap["deriv_as_of"])
        self.assertIsNone(swap["open_interest_value"])
        self.assertIsNone(swap["funding_rate"])
        persisted = base.RateLimitState.load(base.Path(self.cfg.rate_limit_state))
        self.assertTrue(any(k.startswith("futures|") for k in persisted.domains))
        self.assertIn("限流暂停", output.getvalue())

    def test_deriv_hourly_cache_skips_second_round(self):
        self.stop_deriv_mock()
        self.futures.fapiDataGetOpenInterestHist.return_value = [
            {"sumOpenInterestValue": 100 + i, "timestamp": T0 - (200 - i) * 3_600_000}
            for i in range(200)]
        self.futures.fapiDataGetTopLongShortPositionRatio.return_value = [
            {"longShortRatio": 1.2, "timestamp": T0 - (200 - i) * 3_600_000}
            for i in range(200)]
        base.clear_deriv_cache()
        with patch.object(base, "_now_ms", return_value=base._now_ms()):
            base.public_selection(self.spot, self.futures, self.cfg)
            base.public_selection(self.spot, self.futures, self.cfg)  # 同一小时内的第二轮
        self.assertEqual(self.futures.fapiDataGetOpenInterestHist.call_count, 1)
        self.assertEqual(self.futures.fapiDataGetTopLongShortPositionRatio.call_count, 1)

    def test_extras_fetched_only_for_shown_rows(self):
        self.stop_deriv_mock()
        names = [f"C{i}" for i in range(12)]
        self.spot_markets.update({
            name + "/USDT": {"base": name, "spot": True, "active": True} for name in names})
        self.spot.fetch_tickers.return_value = {
            **{name + "/USDT": {"quoteVolume": 2_000_000} for name in names},
            "SPOT/USDT": {"quoteVolume": 5_000_000},
            "SWAP/USDT": {"quoteVolume": 5_500_000},
        }
        self.spot.fetch_ohlcv.side_effect = lambda sym, *a, **k: (
            rising_klines() if sym in ("SPOT/USDT", "SWAP/USDT") else flat_klines())
        self.futures.markets.update({
            name + "/USDT:USDT": {"base": name, "quote": "USDT", "swap": True,
                                  "linear": True, "active": True} for name in names})
        self.futures.fetch_tickers.return_value = {
            **{n + "/USDT:USDT": {"quoteVolume": 3_000_000} for n in names},
            "SWAP/USDT:USDT": {"quoteVolume": 20_000_000, "last": 124},
        }
        rows = base.public_selection(self.spot, self.futures, self.cfg)
        chosen = [r for r in rows if r["score"] >= self.cfg.min_score]
        shown_symbols = {r["symbol"] for r in (chosen or rows[:10])}
        for row in rows:
            should_defer = row["has_futures"] and row["symbol"] not in shown_symbols
            self.assertEqual(row["extras_deferred"], should_defer, row["symbol"])
            if should_defer:
                self.assertIsNone(row["open_interest_value"])
        # 12 个 C 币全部低于门槛 → 全部延后；只有过门槛的 SWAP 补取 OI/费率
        self.assertEqual(len([r for r in rows if r["extras_deferred"]]), 12)
        self.assertEqual(self.futures.fetch_open_interest.call_count, 1)
        self.assertEqual(self.futures.fetch_funding_rate.call_count, 1)

    def test_deriv_status_ok_and_as_of_recorded(self):
        self.stop_deriv_mock()
        self.futures.fapiDataGetOpenInterestHist.return_value = [
            {"sumOpenInterestValue": 100 + i, "timestamp": T0 - (200 - i) * 3_600_000}
            for i in range(200)]
        self.futures.fapiDataGetTopLongShortPositionRatio.return_value = [
            {"longShortRatio": 1.5, "timestamp": T0 - (200 - i) * 3_600_000}
            for i in range(200)]
        rows = base.public_selection(self.spot, self.futures, self.cfg)
        swap = next(r for r in rows if r["symbol"] == "SWAP/USDT")
        self.assertEqual(swap["deriv_status"], "ok")
        self.assertEqual(swap["deriv_as_of"], T0 - 3_600_000)  # 序列最后一根是 T0-1h
        self.assertAlmostEqual(swap["oi_chg_1d"], 299 / 275 - 1, places=6)
        self.assertEqual(swap["ls_source"], "topLongShortPositionRatio")

    def test_fallback_ratio_is_marked_partial_not_ok(self):
        """大户接口失败、回退全市场口径时：数据可用但必须标 partial（口径不算完整取数）。"""
        self.stop_deriv_mock()
        self.futures.fapiDataGetOpenInterestHist.return_value = [
            {"sumOpenInterestValue": 100 + i, "timestamp": T0 - (200 - i) * 3_600_000}
            for i in range(200)]
        self.futures.fapiDataGetTopLongShortPositionRatio.side_effect = \
            ccxt.ExchangeError("endpoint gone")
        self.futures.fetch_long_short_ratio_history.return_value = [
            {"longShortRatio": 1.1, "timestamp": T0}]
        base.clear_deriv_cache()
        rows = base.public_selection(self.spot, self.futures, self.cfg)
        swap = next(r for r in rows if r["symbol"] == "SWAP/USDT")
        self.assertEqual(swap["deriv_status"], "partial")
        self.assertEqual(swap["ls_source"], "globalLongShortAccountRatio(回退口径)")

    def test_fetch_markets_scoped_per_domain(self):
        spot_client = base.build_exchange(replace(base.Config(), proxy=None), "spot")
        future_client = base.build_exchange(replace(base.Config(), proxy=None), "future")
        self.assertEqual(list(spot_client.options["fetchMarkets"]), ["spot"])
        self.assertEqual(list(future_client.options["fetchMarkets"]), ["linear"])


class CoinGeckoCacheTests(TestCase):
    def test_caps_cached_within_same_utc_day(self):
        with TemporaryDirectory() as tmp:
            path = base.Path(tmp) / "caps.json"
            cfg = replace(base.Config(), proxy=None, csv_dir=tmp)
            resp = Mock()
            resp.json.return_value = [{"symbol": "AAA", "market_cap": 123.0}]
            with patch.object(base.requests, "get", return_value=resp) as get:
                caps1 = base.coingecko_caps(cfg, cache_path=path)
                caps2 = base.coingecko_caps(cfg, cache_path=path)
            self.assertEqual(get.call_count, 4)     # 4 页只取了一次
            self.assertEqual(caps1, caps2)
            self.assertEqual(caps2.get("AAA"), 123.0)


if __name__ == "__main__":
    main()
