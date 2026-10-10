"""覆盖对账独立离线用例（tasks/success_followup_20261008.md，glm 部分）。

证明两件事（全程假接口、不联网、临时目录，不改生产行为）：
1. 正常跳过能对账：scope_total = 评分行数 + 各互斥跳过类 + 失败类，
   每个被跳过的对象都有类别和标的名，unclassified == 0。
2. 未知异常不消失：循环内抛出的未预期异常记入 failed 类并保留标的名，
   计入 classified_total，不会被静默吞掉。

背景：run_20261008_160701 当轮 346−285−1=60 个对象无逐项记录（停用/非现货
分支原先静默 continue）。本测试钉住补丁后的对账行为；历史当轮缺口不因此
视为已补回，留待下一次真实查询验证。
"""
from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, main
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import binance_box_strategy as base  # noqa: E402

HOUR = 3_600_000
T0 = 1_700_000_000_000
NORMAL_KLINES = [[T0 + i * HOUR, 100 + i / 10, 101 + i / 10, 99 + i / 10,
                  100 + i / 10, 10 + i * 0.05] for i in range(240)]
SHORT_KLINES = NORMAL_KLINES[:10]  # 少于 box_period(24)+3 → 历史不足
OI_HIST = [{"sumOpenInterestValue": 1_000_000.0 * (1.005 ** i),
            "timestamp": T0 + i * HOUR} for i in range(200)]
LS_HIST = [{"longShortRatio": 0.9, "timestamp": T0 + i * HOUR} for i in range(200)]

# symbol -> (active, spot)；缺省视为正常上市现货
MARKET_OVERRIDE = {
    "DEAD/USDT": (False, True),    # 已停用 → inactive_or_non_spot
    "NOTSPOT/USDT": (True, False),  # 非现货 → inactive_or_non_spot
}


class FakeSpot:
    def __init__(self, symbols, kmap, fail=None):
        self.symbols = list(symbols)
        self.kmap = dict(kmap)
        self.fail = dict(fail or {})

    def market(self, symbol):
        active, spot = MARKET_OVERRIDE.get(symbol, (True, True))
        return {"base": symbol.split("/")[0], "active": active, "spot": spot}

    def fetch_tickers(self):
        return {s: {"quoteVolume": 5_000_000, "percentage": 1.0}
                for s in self.symbols}

    def fetch_ohlcv(self, symbol, timeframe, limit=None):
        if symbol in self.fail:
            raise self.fail[symbol]
        ks = self.kmap[symbol]
        return ks[-limit:] if limit else ks


class FakeFutures:
    def __init__(self, bases=("DOGE", "BBB")):
        self.markets = {f"{b}/USDT:USDT": {"base": b, "swap": True, "linear": True,
                                           "quote": "USDT", "active": True}
                        for b in bases}

    def market(self, symbol):
        return {"id": symbol.split(":")[0].replace("/", "")}

    def fetch_tickers(self):
        return {s: {"quoteVolume": 6_000_000, "last": 1.0} for s in self.markets}

    def fapiDataGetOpenInterestHist(self, params):
        return OI_HIST

    def fapiDataGetTopLongShortPositionRatio(self, params):
        return LS_HIST

    def fetch_funding_rate(self, symbol):
        return {"fundingRate": 0.0001}

    def fetch_open_interest(self, symbol):
        return {"openInterestAmount": 1.0}

    def fetch_long_short_ratio_history(self, symbol, period, limit):
        return LS_HIST


def make_cfg(tmp: Path, **kw) -> "base.Config":
    defaults = dict(csv_dir=str(tmp),
                    rate_limit_state=str(tmp / "rate_limit_state.json"),
                    use_coingecko=False, deriv_cache=False, proxy=None,
                    min_score=5)
    defaults.update(kw)
    return base.Config(**defaults)


def patch_select_symbols(symbols):
    return mock.patch.object(
        base, "select_symbols", lambda tickers, cfg, caps=None: list(symbols))


class CoverageReconciliationTests(TestCase):
    def _run(self, tmp, spot, fut, cfg):
        stats: dict = {}
        ranked = base.public_selection(spot, fut, cfg, None, round_stats=stats)
        return ranked, stats

    def test_normal_skips_reconcile(self):
        """评分行 + 各互斥跳过类 = 原定对象；每个跳过对象有名有类。"""
        symbols = ["DOGE/USDT", "BBB/USDT", "DEAD/USDT", "NOTSPOT/USDT",
                   "SHORT/USDT", "MISS/USDT"]
        kmap = {"DOGE/USDT": NORMAL_KLINES, "BBB/USDT": NORMAL_KLINES,
                "SHORT/USDT": SHORT_KLINES}
        with TemporaryDirectory() as td:
            cfg = make_cfg(Path(td), require_futures=True)
            spot = FakeSpot(symbols, kmap)
            # SHORT 有合约（先过 require_futures 检查，再因历史不足被跳过）；
            # MISS 无合约 → require_futures_missing。
            fut = FakeFutures(bases=("DOGE", "BBB", "SHORT"))
            with patch_select_symbols(symbols):
                ranked, stats = self._run(Path(td), spot, fut, cfg)
        self.assertEqual(stats["scope_total"], len(symbols))
        self.assertEqual(stats["task_completed"], True)
        self.assertEqual(len(ranked), 2)  # DOGE、BBB 出评分行
        skips = stats["skip_reasons"]
        self.assertEqual(skips["inactive_or_non_spot"]["count"], 2)
        self.assertEqual(sorted(skips["inactive_or_non_spot"]["symbols"]),
                         ["DEAD/USDT", "NOTSPOT/USDT"])
        self.assertEqual(skips["require_futures_missing"]["count"], 1)
        self.assertEqual(skips["require_futures_missing"]["symbols"], ["MISS/USDT"])
        self.assertEqual(skips["short_history"]["count"], 1)
        self.assertEqual(skips["short_history"]["symbols"], ["SHORT/USDT"])
        self.assertEqual(skips["failed"]["count"], 0)
        self.assertEqual(stats["classified_total"], len(symbols))
        self.assertEqual(stats["unclassified"], 0)
        self.assertEqual(stats["symbols_scope"], symbols)

    def test_unexpected_failure_does_not_vanish(self):
        """未知异常（非限流、非 DomainPaused）记入 failed 并保留标的名。"""
        symbols = ["DOGE/USDT", "GHOST/USDT"]
        kmap = {"DOGE/USDT": NORMAL_KLINES}
        boom = RuntimeError("模拟：循环内未预期异常（未知原因）")
        with TemporaryDirectory() as td:
            cfg = make_cfg(Path(td), require_futures=False)
            spot = FakeSpot(symbols, kmap, fail={"GHOST/USDT": boom})
            fut = FakeFutures(bases=("DOGE",))
            with patch_select_symbols(symbols):
                ranked, stats = self._run(Path(td), spot, fut, cfg)
        self.assertEqual(stats["scope_total"], 2)
        self.assertEqual(len(ranked), 1)
        failed = stats["skip_reasons"]["failed"]
        self.assertEqual(failed["count"], 1)
        self.assertEqual(failed["symbols"], ["GHOST/USDT"])
        self.assertEqual(stats["errors"], 1)
        self.assertEqual(stats["classified_total"], 2)
        self.assertEqual(stats["unclassified"], 0)


if __name__ == "__main__":
    main()
