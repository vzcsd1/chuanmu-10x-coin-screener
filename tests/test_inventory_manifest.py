"""全量归档清单解析与分片校验的离线测试（tasks/glm53f_full_manifest.md 要求）。

覆盖：日/月周期、币名带连字符、币本位到期合约、完整日期、唯一键、周期匹配、
两个 URL 不共享一个状态（scope 独立 checkpoint）。不联网。
"""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, main

import pandas as pd

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import data_inventory_glm as inv  # noqa: E402


class ParseKeyTests(TestCase):
    def test_kline_monthly(self):
        p = inv.parse_archive_key(
            "data/spot/monthly/klines/BTCUSDT/1d/BTCUSDT-1d-2021-03.zip", 123)
        self.assertEqual((p["market"], p["dataset"], p["symbol"], p["interval"],
                          p["kind"], p["period"]),
                         ("spot", "klines", "BTCUSDT", "1d", "monthly", "2021-03"))

    def test_kline_daily(self):
        p = inv.parse_archive_key(
            "data/futures/um/daily/klines/ETHUSDT/5m/ETHUSDT-5m-2026-10-05.zip", 1)
        self.assertEqual(p["kind"], "daily")
        self.assertEqual(p["period"], "2026-10-05")
        self.assertEqual(p["market"], "futures-um")

    def test_symbol_with_dash_is_kept_via_directory(self):
        # 目录为真相：假设符号含连字符也不被文件名切坏
        p = inv.parse_archive_key(
            "data/spot/monthly/klines/AB-CD/1m/AB-CD-1m-2024-01.zip", 5)
        self.assertEqual(p["symbol"], "AB-CD")
        self.assertEqual(p["interval"], "1m")
        self.assertEqual(p["period"], "2024-01")

    def test_coinm_delivered_contract(self):
        # 币本位到期合约符号带下划线（如 BTCUSD_240329）
        p = inv.parse_archive_key(
            "data/futures/cm/monthly/klines/BTCUSD_240329/5m/"
            "BTCUSD_240329-5m-2024-01.zip", 9)
        self.assertEqual(p["market"], "futures-cm")
        self.assertEqual(p["symbol"], "BTCUSD_240329")
        self.assertEqual(p["interval"], "5m")

    def test_metrics_full_date_and_symbol(self):
        p = inv.parse_archive_key(
            "data/futures/um/daily/metrics/1000CATUSDT/"
            "1000CATUSDT-metrics-2024-02-29.zip", 7)
        self.assertEqual(p["dataset"], "metrics")
        self.assertEqual(p["symbol"], "1000CATUSDT")   # 不再为空
        self.assertEqual(p["period"], "2024-02-29")     # 完整日期，不是日号

    def test_funding_symbol_suffix_removed(self):
        p = inv.parse_archive_key(
            "data/futures/um/monthly/fundingRate/1000BTTCUSDT/"
            "1000BTTCUSDT-fundingRate-2023-07.zip", 3)
        self.assertEqual(p["dataset"], "fundingRate")
        self.assertEqual(p["symbol"], "1000BTTCUSDT")   # 去掉 -fundingRate 后缀

    def test_period_mismatch_is_rejected(self):
        # 文件名声称的 interval 与目录不一致 -> 拒绝
        self.assertIsNone(inv.parse_archive_key(
            "data/spot/monthly/klines/BTCUSDT/5m/BTCUSDT-1d-2024-01.zip", 1))
        # 周期格式错误（月文件给了日格式）
        self.assertIsNone(inv.parse_archive_key(
            "data/spot/monthly/klines/BTCUSDT/1d/BTCUSDT-1d-2024-01-15.zip", 1))
        # 不存在的日期
        self.assertIsNone(inv.parse_archive_key(
            "data/futures/um/daily/metrics/BTCUSDT/BTCUSDT-metrics-2024-02-30.zip", 1))
        # 非 zip
        self.assertIsNone(inv.parse_archive_key(
            "data/spot/monthly/klines/BTCUSDT/1d/BTCUSDT-1d-2024-01.CHECKSUM", 1))


class ValidateShardTests(TestCase):
    def _rows(self, **kw):
        row = {"source_url": "https://data.binance.vision/x.zip", "market": "spot",
               "dataset": "klines", "symbol": "BTCUSDT", "interval": "5m",
               "period_start": "2024-01-01", "period_end": "2024-01-02",
               "remote_size_bytes": 10, "checksum_url": "", "discovered_at_utc": ""}
        row.update(kw)
        return [row]

    def test_unique_url_required(self):
        rows = self._rows() + self._rows()
        passed, checks = inv.validate_shard(rows, "daily")
        self.assertFalse(passed)
        self.assertEqual(checks["dup_url"], 1)

    def test_interval_must_match_dataset(self):
        rows = self._rows(dataset="metrics", interval="")
        passed, _ = inv.validate_shard(rows, "daily")
        self.assertTrue(passed)
        rows = self._rows(dataset="metrics", interval="5m")
        passed, checks = inv.validate_shard(rows, "daily")
        self.assertFalse(passed)
        self.assertEqual(checks["bad_interval"], 1)

    def test_period_format_matches_kind(self):
        # r2 契约：period_start/end 一律完整日期；月度分片的 period_start 必须是月初
        rows = self._rows(period_start="2024-01-01", period_end="2024-01-31")
        passed, _ = inv.validate_shard(rows, "monthly")
        self.assertTrue(passed)
        rows = self._rows(period_start="2024-01-15", period_end="2024-01-15")
        passed, checks = inv.validate_shard(rows, "daily")
        self.assertTrue(passed)
        # 月初缺失（period_start 不是 -01）对月度分片不合格
        rows = self._rows(period_start="2024-01", period_end="2024-01-31")
        passed, checks = inv.validate_shard(rows, "monthly")
        self.assertFalse(passed)


class DedupeDailyTests(TestCase):
    """月度覆盖去重必须含 symbol；市场/周期/币种任一不同都不得互相剔除。"""

    @staticmethod
    def _df(market, symbol, kind, period, interval="5m"):
        """monthly/daily 都用同一 dataset+interval（5m 月度覆盖 5m 日度才是同一口径）。"""
        return pd.DataFrame([{
            "source_url": f"u/{market}/{symbol}/{interval}/{period}", "market": market,
            "dataset": "klines", "symbol": symbol, "interval": interval,
            "period_start": period + ("-01" if kind == "monthly" else ""),
            "period_end": period + "-28" if kind == "monthly" else period,
            "remote_size_bytes": 10, "checksum_url": "", "discovered_at_utc": ""}])

    def test_symbol_a_monthly_does_not_drop_symbol_b_daily(self):
        monthly = self._df("spot", "AAAUSDT", "monthly", "2026-09")
        daily_b = self._df("spot", "BBBUSDT", "daily", "2026-09-15")
        kept, dropped = inv.dedupe_daily(daily_b, monthly)
        self.assertEqual(len(kept), 1)   # B 币日度必须保留
        self.assertEqual(len(dropped), 0)

    def test_same_symbol_monthly_covers_daily(self):
        monthly = self._df("spot", "AAAUSDT", "monthly", "2026-09")
        daily = self._df("spot", "AAAUSDT", "daily", "2026-09-15")
        kept, dropped = inv.dedupe_daily(daily, monthly)
        self.assertEqual(len(kept), 0)   # 同币同市场同周期月度覆盖才剔除
        self.assertEqual(len(dropped), 1)

    def test_market_and_kind_isolation(self):
        monthly = self._df("futures-um", "AAAUSDT", "monthly", "2026-09")
        daily_spot = self._df("spot", "AAAUSDT", "daily", "2026-09-15")
        kept, dropped = inv.dedupe_daily(daily_spot, monthly)
        self.assertEqual(len(kept), 1)   # 不同市场不互删
        self.assertEqual(len(dropped), 0)

    def test_month_outside_coverage_kept(self):
        monthly = self._df("spot", "AAAUSDT", "monthly", "2026-09")
        daily = self._df("spot", "AAAUSDT", "daily", "2026-10-15")
        kept, dropped = inv.dedupe_daily(daily, monthly)
        self.assertEqual(len(kept), 1)   # 月度未覆盖的月份保留
        self.assertEqual(len(dropped), 0)


class ScopeStateIsolationTests(TestCase):
    def test_two_urls_do_not_share_one_state(self):
        """不同 scope 的 checkpoint 状态互不串用（两个 URL 不共享一个状态）。"""
        with TemporaryDirectory() as tmp:
            old = inv.PROGRESS
            inv.PROGRESS = Path(tmp) / "progress.json"
            try:
                progress = inv.load_progress()
                a = progress["scopes"].setdefault("spot_klines_5m_daily", {})
                b = progress["scopes"].setdefault("fut_um_metrics_daily", {})
                a["next_index"] = 7
                b["next_index"] = 3
                inv.save_progress(progress)
                reloaded = inv.load_progress()
                self.assertEqual(
                    reloaded["scopes"]["spot_klines_5m_daily"]["next_index"], 7)
                self.assertEqual(
                    reloaded["scopes"]["fut_um_metrics_daily"]["next_index"], 3)
                self.assertIsNot(
                    reloaded["scopes"]["spot_klines_5m_daily"],
                    reloaded["scopes"]["fut_um_metrics_daily"])
            finally:
                inv.PROGRESS = old

    def test_stage_scope_of_parsed_routes_by_true_interval(self):
        """spot_5m 旧清单里混入的非 USDT 1d 行必须按真实周期路由，不冒充 5m。"""
        row = inv.parse_url(
            "https://data.binance.vision/data/spot/monthly/klines/ETHBTC/1d/"
            "ETHBTC-1d-2024-01.zip", 5)
        scope = inv.scope_of_parsed(row)
        self.assertEqual(scope, "spot_klines_1d_monthly")


if __name__ == "__main__":
    main()
