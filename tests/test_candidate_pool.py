"""Offline integration checks for wider candidate selection; no network or files."""
from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
from unittest import TestCase, main
from unittest.mock import Mock, patch

import binance_box_strategy as base


class CandidatePoolTests(TestCase):
    def setUp(self):
        self.cfg = replace(base.Config(), use_coingecko=False, csv_dir=None)
        spot_markets = {
            name + "/USDT": {"base": name, "spot": True, "active": name != "OLD"}
            for name in ["SPOT", "SWAP", "OLD", "USDC"]
        }
        self.spot = Mock()
        self.spot.market.side_effect = spot_markets.__getitem__
        self.spot.fetch_tickers.return_value = {
            "SPOT/USDT": {"quoteVolume": 1_200_000},
            "SWAP/USDT": {"quoteVolume": 10_000_000},
            "OLD/USDT": {"quoteVolume": 8_000_000},
            "USDC/USDT": {"quoteVolume": 100_000_000},
        }
        # Rising price with equal volume, no breakout: trend alone contributes 5.
        self.spot.fetch_ohlcv.return_value = [
            [1_700_000_000_000 + i * 3_600_000,
             100 + i / 10, 101 + i / 10, 99 + i / 10, 100 + i / 10, 10]
            for i in range(240)
        ]
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
        self.deriv = patch.object(base, "fetch_deriv_context", return_value={
            "oi_chg_1d": 0.15, "oi_chg_3d": 0.30, "ls_top": 1.2,
            "ls_source": "topLongShortPositionRatio",
        }).start()
        self.addCleanup(patch.stopall)

    def test_spot_only_trend_is_selected_without_contract_requests(self):
        with patch.object(base, "okx_exchange", return_value=None), redirect_stdout(StringIO()) as output:
            chosen = base.run_once(self.spot, self.futures, self.cfg)
        by_symbol = {row["symbol"]: row for row in chosen}
        self.assertEqual(set(by_symbol), {"SPOT/USDT", "SWAP/USDT"})
        spot = by_symbol["SPOT/USDT"]
        self.assertEqual(spot["score"], 5)
        self.assertEqual(spot["score_kline"], 5)
        self.assertIsNone(spot["score_deriv"])
        self.assertFalse(spot["has_futures"])
        self.assertIsNone(spot["open_interest_value"])
        self.assertEqual(spot["market_scope"], "仅现货")
        self.assertIn("仅现货", output.getvalue())
        self.assertEqual(by_symbol["SWAP/USDT"]["score"], 12)
        self.deriv.assert_called_once_with(self.futures, "SWAP/USDT:USDT", self.cfg)
        self.futures.fetch_open_interest.assert_called_once_with("SWAP/USDT:USDT")
        self.futures.fetch_funding_rate.assert_called_once_with("SWAP/USDT:USDT")
        fetched = [call.args[0] for call in self.spot.fetch_ohlcv.call_args_list]
        self.assertCountEqual(fetched, ["SPOT/USDT", "SWAP/USDT"])

    def test_contract_requirement_can_restore_old_universe(self):
        cfg = replace(self.cfg, require_futures=True)
        rows = base.public_selection(self.spot, self.futures, cfg)
        self.assertEqual([row["symbol"] for row in rows], ["SWAP/USDT"])
        self.spot.fetch_ohlcv.assert_called_once_with("SWAP/USDT", cfg.timeframe, limit=cfg.lookback)

    def test_no_active_contracts_still_returns_spot_observations(self):
        self.futures.markets["SWAP/USDT:USDT"]["active"] = False
        rows = base.public_selection(self.spot, self.futures, self.cfg)
        self.assertEqual({row["symbol"] for row in rows}, {"SPOT/USDT", "SWAP/USDT"})
        self.assertTrue(all(not row["has_futures"] for row in rows))
        self.deriv.assert_not_called()
        self.futures.fetch_open_interest.assert_not_called()
        self.futures.fetch_funding_rate.assert_not_called()


if __name__ == "__main__":
    main()
