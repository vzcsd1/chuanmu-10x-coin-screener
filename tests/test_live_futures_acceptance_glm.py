"""glm 实时合约完整性独立验收（tasks/glm_live_futures_acceptance.md）。

用**实际生产函数**（public_selection / 川沐十倍币筛选.scan / ui.app.ScanManager）
配**假取数对象**离线复现八类情形，审计"返回卡片字段齐全"能否掩盖整轮数据缺失。

边界：全程不联网（okx_exchange 与真实请求一律被替身拦下）、不写真实 results/
（csv_dir / 限流状态 / last_result 全部落在 TemporaryDirectory）、不改生产源码。

断言的是**当前真实行为**——包括"会掩盖缺失"的行为本身；掩盖点是否构成缺陷
由报告按证据裁决，测试只负责把行为钉住、可复跑。
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, main
from unittest.mock import patch

import ccxt

import binance_box_strategy as base

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HOUR = 3_600_000
T0 = 1_700_000_000_000
FAR_FUTURE = 4_000_000_000_000  # 远期封禁时刻（ms），测试内不等待


def load_module(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, PROJECT_ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def rising_klines(n=240):
    # 缓涨 + 量增：趋势(5) + 24h量增(1) = 6 分
    return [[T0 + i * HOUR, 100 + i / 10, 101 + i / 10, 99 + i / 10,
             100 + i / 10, 10 + i * 0.05] for i in range(n)]


def flat_klines(n=240):
    # 阴跌 + 量平：K 线侧 0 分
    return [[T0 + i * HOUR, 120 - i / 10, 121 - i / 10, 119 - i / 10,
             120 - i / 10, 10.0] for i in range(n)]


def oi_hist(n=200, value=1_000_000.0, growth=0.0, tail_zeros=0):
    out = []
    for i in range(n):
        v = value * ((1.0 + growth) ** i if growth else 1.0)
        out.append({"sumOpenInterestValue": v, "timestamp": T0 + i * HOUR})
    for item in out[len(out) - tail_zeros:]:
        item["sumOpenInterestValue"] = 0
    return out


def ls_hist(n=200, ratio=0.9):
    return [{"longShortRatio": ratio, "timestamp": T0 + i * HOUR} for i in range(n)]


class FakeSpot:
    """现货域替身：只实现 public_selection 用到的方法。"""

    def __init__(self, kmap, ohlcv_fail=None):
        self.kmap = dict(kmap)
        self.ohlcv_fail = ohlcv_fail or {}
        self.tickers_calls = 0
        self.ohlcv_calls: dict[str, int] = {}

    def market(self, symbol):
        return {"base": symbol.split("/")[0], "active": True, "spot": True}

    def fetch_tickers(self):
        self.tickers_calls += 1
        return {s: {"quoteVolume": 5_000_000, "percentage": 1.0} for s in self.kmap}

    def fetch_ohlcv(self, symbol, timeframe, limit=None):
        self.ohlcv_calls[symbol] = self.ohlcv_calls.get(symbol, 0) + 1
        if symbol in self.ohlcv_fail:
            raise self.ohlcv_fail[symbol]
        ks = self.kmap[symbol]
        return ks[-limit:] if limit else ks


class FakeFutures:
    """合约域替身：可注入限流/故障/回退，并统计方法调用次数。"""

    def __init__(self, symbols=("DOGE/USDT:USDT", "BBB/USDT:USDT"),
                 oi=None, ls=None, ls_fallback=None,
                 oi_fail_on_call=None, oi_fail_exc=None, ls_fail=False):
        self.markets = {s: {"base": s.split("/")[0], "swap": True, "linear": True,
                            "quote": "USDT", "active": True} for s in symbols}
        self.oi = oi if oi is not None else oi_hist(growth=0.005)
        self.ls = ls if ls is not None else ls_hist()
        self.ls_fallback = ls_fallback
        self.oi_fail_on_call = oi_fail_on_call
        if oi_fail_exc is not None:
            self.oi_fail_exc = oi_fail_exc
        else:
            self.oi_fail_exc = ccxt.RateLimitExceeded(
                f"binance 418 IP banned until {FAR_FUTURE}. Please use the websocket.")
        self.ls_fail = ls_fail
        self.oi_calls = 0
        self.ls_calls = 0
        self.fallback_calls = 0
        self.snapshot_calls = 0
        self.funding_calls = 0

    def market(self, symbol):
        return {"id": symbol.split(":")[0].replace("/", "")}

    def fetch_tickers(self):
        return {s: {"quoteVolume": 6_000_000, "last": 1.0} for s in self.markets}

    def fapiDataGetOpenInterestHist(self, params):
        self.oi_calls += 1
        if self.oi_fail_on_call and self.oi_calls >= self.oi_fail_on_call:
            raise self.oi_fail_exc
        return self.oi

    def fapiDataGetTopLongShortPositionRatio(self, params):
        self.ls_calls += 1
        if self.ls_fail:
            raise RuntimeError("模拟 topLongShortPositionRatio 故障")
        return self.ls

    def fetch_long_short_ratio_history(self, symbol, timeframe, limit=None):
        self.fallback_calls += 1
        if self.ls_fallback is None:
            raise RuntimeError("模拟回退口径也不可用")
        return self.ls_fallback

    def fetch_open_interest(self, symbol):
        self.snapshot_calls += 1
        return {"openInterestValue": 1_500_000}

    def fetch_funding_rate(self, symbol):
        self.funding_calls += 1
        return {"fundingRate": 0.0001}


def make_cfg(tmp: Path, **kw):
    # 状态文件名与 default_state_path 的 csv_dir 兜底一致，UI read_pauses 才能读到同一份
    defaults = dict(csv_dir=str(tmp), rate_limit_state=str(tmp / "rate_limit_state.json"),
                    use_coingecko=False, deriv_cache=False, proxy=None,
                    min_score=5)
    defaults.update(kw)
    return base.Config(**defaults)


def seed_spot_pause(cfg):
    state = base.RateLimitState.load(cfg.rate_limit_state)
    state.record_pause(f"spot|{cfg.proxy or 'direct'}", FAR_FUTURE, "418 测试预置", now=T0)


SCAN_MOD = None


def scan_module():
    global SCAN_MOD
    if SCAN_MOD is None:
        SCAN_MOD = load_module("chuanmu_scan_glm", "川沐十倍币筛选.py")
    return SCAN_MOD


def run_scan(spot, futures, cfg, okx=None, round_stats=None):
    """走真实 川沐十倍币筛选.scan()：只 patch 连接与 OKX 两个入口，不复制筛选逻辑。"""
    mod = scan_module()
    with patch.object(base, "connect_exchanges", return_value=(spot, futures, cfg)), \
            patch.object(base, "okx_exchange", side_effect=RuntimeError("测试离线")):
        rows = mod.scan(cfg, round_stats=round_stats)
    return rows


def ui_snapshot(scan_fn, tmp: Path):
    """UI 层快照：patch 环境使 read_pauses/env_config 读 tmp，而非真实 results/。"""
    ui_app = load_module("ui_app_glm", os.path.join("ui", "app.py"))
    env = {"CSV_DIR": str(tmp), "BINANCE_NO_PROXY": "1"}
    with patch.dict(os.environ, env):
        manager = ui_app.ScanManager(scan_fn, tmp / "last_result.json")
        manager._run()  # 直接同步执行，避免线程时序
        return manager.snapshot()


def ui_scan(spot, fut, cfg, stats):
    """模拟 ui.real_scan 完整链路：scan() 填 stats，返回 {"rows","round"} 载荷。"""
    rows = run_scan(spot, fut, cfg, round_stats=stats)
    return {"rows": rows, "round": stats}


class LiveFuturesAcceptanceTests(TestCase):
    # ---------- S1 合约初始化失败 ----------
    def test_s1_futures_init_failure_masks_round(self):
        with TemporaryDirectory() as td:
            tmp = Path(td)
            spot = FakeSpot({"DOGE/USDT": rising_klines(), "BBB/USDT": flat_klines()})
            fut = FakeFutures()
            healthy = run_scan(spot, fut, make_cfg(tmp))  # 对照：合约可用
            self.assertIn("BBB/USDT", [r["symbol"] for r in healthy])
            bbb = next(r for r in healthy if r["symbol"] == "BBB/USDT")
            self.assertEqual(bbb["deriv_status"], "ok")
            self.assertEqual(bbb["score"], 10)  # 平K线 0 + OI 4+3 + 多空 3

            # 合约域初始化失败（connect_exchanges 降级返回 None 的真实形态）
            spot2 = FakeSpot({"DOGE/USDT": rising_klines(), "BBB/USDT": flat_klines()})
            broken = run_scan(spot2, None, make_cfg(tmp / "s1b"))
            self.assertEqual([r["symbol"] for r in broken], ["DOGE/USDT"])  # BBB 落选不可见
            doge = broken[0]
            # 关键混淆：DOGE 明明存在合约市场，却被标成「无合约」
            self.assertEqual(doge["deriv_status"], "no_futures")
            self.assertFalse(doge["has_futures"])
            self.assertEqual(doge["market_scope"], "仅现货")
            self.assertIsNone(doge["score_deriv"])
            # 卡片自身字段齐全（能通过"字段完整性"检查）——掩盖整轮缺失
            for field in ("symbol", "score", "deriv_status", "deriv_as_of",
                          "market_scope", "has_futures", "conditions"):
                self.assertIn(field, doge)

            # UI 层：状态 success、无暂停提示——页面没有任何整轮异常信号
            spot3 = FakeSpot({"DOGE/USDT": rising_klines()})
            snap = ui_snapshot(lambda: run_scan(spot3, None, make_cfg(tmp / "s1c")),
                               tmp / "s1c")
            self.assertEqual(snap["status"], "success")
            self.assertTrue(snap["rows"])
            self.assertEqual(snap["paused"], [])
            self.assertIsNone(snap["error"])

    # ---------- S2 扫描中途被限流 ----------
    def test_s2_mid_scan_futures_pause(self):
        with TemporaryDirectory() as td:
            tmp = Path(td)
            spot = FakeSpot({"A/USDT": rising_klines(), "B/USDT": rising_klines(),
                             "C/USDT": flat_klines()})
            fut = FakeFutures(symbols=("A/USDT:USDT", "B/USDT:USDT", "C/USDT:USDT"),
                              oi_fail_on_call=2)  # 第 2 次 OI 请求触发 418
            cfg = make_cfg(tmp)
            chosen = run_scan(spot, fut, cfg)
            by = {r["symbol"]: r for r in chosen}
            self.assertEqual(by["A/USDT"]["deriv_status"], "ok")      # 暂停前取到
            self.assertEqual(by["B/USDT"]["deriv_status"], "paused")  # 暂停后缺失
            self.assertEqual(by["B/USDT"]["score"], 6)                # 仅 K 线分
            # C 平 K 线 0 分，因合约中断拿不到 10 分 → 落选，不在返回里
            self.assertNotIn("C/USDT", by)
            # 限流状态已登记并持久化
            state = base.RateLimitState.load(cfg.rate_limit_state)
            self.assertFalse(state.should_attempt(f"futures|{cfg.proxy or 'direct'}"))
            # 请求计数：A 取满 2 次；B 的 OI 触发限流 1 次、LS 被 guard 拦下 0 次
            self.assertEqual((fut.oi_calls, fut.ls_calls), (2, 1))

            # UI：一轮之内 ok 与 paused 混杂，但整体仍是 success
            spot2 = FakeSpot({"A/USDT": rising_klines(), "B/USDT": rising_klines()})
            fut2 = FakeFutures(symbols=("A/USDT:USDT", "B/USDT:USDT"), oi_fail_on_call=2)
            snap = ui_snapshot(lambda: run_scan(spot2, fut2, make_cfg(tmp / "ui")),
                               tmp / "ui")
            self.assertEqual(snap["status"], "success")
            self.assertEqual({r["deriv_status"] for r in snap["rows"]}, {"ok", "paused"})
            self.assertTrue(snap["paused"])  # 快照能读到暂停——但 rows 不因此降级

    # ---------- S3 一项字段缺失（两个口径都不可用） ----------
    def test_s3_field_missing_partial(self):
        with TemporaryDirectory() as td:
            tmp = Path(td)
            spot = FakeSpot({"DOGE/USDT": rising_klines()})
            fut = FakeFutures(ls_fail=True, ls_fallback=None)
            row = run_scan(spot, fut, make_cfg(tmp))[0]
            self.assertEqual(row["deriv_status"], "partial")
            # 缺失字段的契约形态不一致：ls_top 整键缺席，ls_source 键在值 null
            self.assertNotIn("ls_top", row)
            self.assertIsNone(row.get("ls_source"))
            # LS 缺失不补分（缺失≠0 分证据）：K 线 6 + OI 4+3 = 13
            self.assertEqual(row["score_deriv"], 7)
            self.assertEqual(row["score"], 13)

    # ---------- S3b 回退口径（R2 修复后的回归钉，2026-10-07 更新） ----------
    def test_s3b_fallback_caliber_scores(self):
        """R2 前此测试钉住的是**缺陷行为**（回退值偷拿 ls_top 3 分）；修复后改为
        钉住正确行为：回退只展示、不加分。详见 R2FallbackScoringTests 与
        reports/live_futures_glm/r2/。"""
        with TemporaryDirectory() as td:
            tmp = Path(td)
            spot = FakeSpot({"DOGE/USDT": rising_klines()})
            fut = FakeFutures(ls_fail=True, ls_fallback=ls_hist(ratio=0.9))
            row = run_scan(spot, fut, make_cfg(tmp))[0]
            self.assertEqual(row["ls_source"], "globalLongShortAccountRatio(回退,仅展示)")
            self.assertEqual(row["deriv_status"], "partial")
            self.assertNotIn("ls_top", row)     # 不再冒充大户持仓比
            self.assertEqual(row["score_deriv"], 7)  # 无回退 3 分
            self.assertEqual(row["score"], 13)

    # ---------- S4 真实无合约 vs 断供：标签相同 ----------
    def test_s4_no_futures_label_identical_to_outage(self):
        with TemporaryDirectory() as td:
            tmp = Path(td)
            # 真无合约：BAR 不在合约市场，合约客户端健康
            fut = FakeFutures(symbols=("DOGE/USDT:USDT",))
            spot = FakeSpot({"DOGE/USDT": rising_klines(), "BAR/USDT": rising_klines()})
            bar = next(r for r in run_scan(spot, fut, make_cfg(tmp))
                       if r["symbol"] == "BAR/USDT")
            self.assertEqual(bar["deriv_status"], "no_futures")
            # 断供：同一 DOGE（有合约），域不可用
            spot2 = FakeSpot({"DOGE/USDT": rising_klines()})
            doge = run_scan(spot2, None, make_cfg(tmp / "b"))[0]
            self.assertEqual((bar["deriv_status"], bar["has_futures"], bar["market_scope"]),
                             (doge["deriv_status"], doge["has_futures"], doge["market_scope"]))

    # ---------- S5 上市历史不足 ----------
    def test_s5_short_history_silently_dropped(self):
        with TemporaryDirectory() as td:
            tmp = Path(td)
            spot = FakeSpot({"DOGE/USDT": rising_klines(),
                             "NEW/USDT": rising_klines(20)})  # 20 根 < box_period+3
            fut = FakeFutures(symbols=("DOGE/USDT:USDT",))
            cfg = make_cfg(tmp)
            with patch.object(base, "connect_exchanges", return_value=(spot, fut, cfg)), \
                    patch.object(base, "okx_exchange", side_effect=RuntimeError("离线")):
                ranked = base.public_selection(spot, fut, cfg, None)
            self.assertNotIn("NEW/USDT", [r["symbol"] for r in ranked])
            # 40 根：能算分（趋势可用），但 24h 量增窗口不足、交易层级缺失
            spot2 = FakeSpot({"MID/USDT": rising_klines(40)})
            fut2 = FakeFutures(symbols=("MID/USDT:USDT",))
            cfg2 = make_cfg(tmp / "b")
            with patch.object(base, "connect_exchanges", return_value=(spot2, fut2, cfg2)), \
                    patch.object(base, "okx_exchange", side_effect=RuntimeError("离线")):
                ranked2 = base.public_selection(spot2, fut2, cfg2, None)
            mid = ranked2[0]
            self.assertNotIn("c_v24up", mid)   # 量增条件缺席（窗口不足）
            self.assertIsNone(mid["entry"])    # 交易层级无法计算
            # 返回结构中没有任何"整轮跳过了多少对象"的痕迹
            self.assertFalse(any("skip" in k or "dropped" in k for k in mid))

    # ---------- S6 合法数值为零 ----------
    def test_s6_legit_zero_values(self):
        with TemporaryDirectory() as td:
            tmp = Path(td)
            # (a) OI 序列末尾 30 小时合法 0 值：被 v>0 静默过滤后仍标 ok，
            #     as_of 落后 30 小时（用较短 LS 序列让 as_of 只反映 OI 的陈旧）
            spot = FakeSpot({"DOGE/USDT": rising_klines()})
            fut = FakeFutures(oi=oi_hist(growth=0.005, tail_zeros=30), ls=ls_hist(n=100))
            row = run_scan(spot, fut, make_cfg(tmp))[0]
            self.assertEqual(row["deriv_status"], "ok")
            self.assertEqual(row["deriv_as_of"], T0 + (200 - 30 - 1) * HOUR)
            # (b) OI 全零（合法极端）与 OI 接口故障：二者同样 partial/无值，无字段可区分
            fut_allzero = FakeFutures(oi=oi_hist(value=0.0))
            row_zero = run_scan(FakeSpot({"DOGE/USDT": rising_klines()}), fut_allzero,
                                make_cfg(tmp / "b"))[0]
            fut_broken = FakeFutures(oi_fail_on_call=1,
                                     oi_fail_exc=RuntimeError("模拟 OI 接口故障"))
            row_broken = run_scan(FakeSpot({"DOGE/USDT": rising_klines()}), fut_broken,
                                  make_cfg(tmp / "c"))[0]
            self.assertEqual(row_zero["deriv_status"], "partial")
            self.assertEqual(row_broken["deriv_status"], "partial")
            self.assertNotIn("oi_chg_1d", row_zero)    # 整键缺席，非 null
            self.assertNotIn("oi_chg_1d", row_broken)
            # (c) 资金费率 0.0 是合法值：保留为 0 而非缺失
            fut_ok = FakeFutures()
            with patch.object(fut_ok, "fetch_funding_rate",
                              return_value={"fundingRate": 0.0}):
                row3 = run_scan(FakeSpot({"DOGE/USDT": rising_klines()}), fut_ok,
                                make_cfg(tmp / "d"))[0]
            self.assertEqual(row3["funding_rate"], 0.0)
            # (d) OI 快照返回 0（合法）：oi_cap_ratio 归 None，不冒充有值
            fut_ok2 = FakeFutures()
            with patch.object(fut_ok2, "fetch_open_interest",
                              return_value={"openInterestValue": 0}):
                row4 = run_scan(FakeSpot({"DOGE/USDT": rising_klines()}), fut_ok2,
                                make_cfg(tmp / "e"))[0]
            self.assertEqual(row4["open_interest_value"], 0.0)
            self.assertIsNone(row4["oi_cap_ratio"])

    # ---------- S7 旧缓存：断供时缓存币仍标 ok ----------
    def test_s7_cache_serves_ok_during_outage(self):
        with TemporaryDirectory() as td:
            tmp = Path(td)
            fixed_now = T0 + 300 * HOUR
            fut = FakeFutures()
            cfg = make_cfg(tmp, deriv_cache=True)
            base.clear_deriv_cache()
            try:
                with patch.object(base, "_now_ms", return_value=fixed_now):
                    run_scan(FakeSpot({"DOGE/USDT": rising_klines()}), fut, cfg)
                    self.assertEqual(fut.oi_calls, 1)
                    # 同一小时第二輪：缓存命中，0 新请求
                    run_scan(FakeSpot({"DOGE/USDT": rising_klines()}), fut, cfg)
                    self.assertEqual(fut.oi_calls, 1)
                    # 缓存未失效前域被封：缓存币仍标 ok（数据最多滞后到上一整点）
                    fut.oi_fail_on_call = 1
                    rows = run_scan(FakeSpot({"DOGE/USDT": rising_klines()}), fut, cfg)
                    self.assertEqual(rows[0]["deriv_status"], "ok")
                    self.assertEqual(rows[0]["deriv_as_of"], T0 + 199 * HOUR)
                    # 缓存清掉后再扫：同一段封禁期 → paused
                    base.clear_deriv_cache()
                    rows2 = run_scan(FakeSpot({"DOGE/USDT": rising_klines()}), fut, cfg)
                    self.assertEqual(rows2[0]["deriv_status"], "paused")
            finally:
                base.clear_deriv_cache()

    # ---------- S7b UI 旧结果：失败轮保留 last，带原时间 ----------
    def test_s7b_ui_last_result_is_old_round(self):
        with TemporaryDirectory() as td:
            tmp = Path(td)
            calls = {"n": 0}

            def scan_fn():
                calls["n"] += 1
                if calls["n"] == 1:
                    return [{"symbol": "DOGE/USDT", "score": 6}]
                raise base.DomainPaused("futures|direct 限流暂停中")

            snap1 = ui_snapshot(scan_fn, tmp)
            self.assertEqual(snap1["status"], "success")
            snap2 = ui_snapshot(scan_fn, tmp)
            # 失败轮：rows=None、错误如实呈现；上一轮完整结果保留在 last
            self.assertEqual(snap2["status"], "failed")
            self.assertIsNone(snap2["rows"])
            self.assertIn("限流暂停", snap2["error"])
            self.assertEqual(snap2["last"]["rows"][0]["symbol"], "DOGE/USDT")
            self.assertLess(snap2["last"]["finished_at"], snap2["server_now"])
            # 磁盘上的旧结果带原 finished_at，可辨认"这不是本轮成功"
            data = json.loads((tmp / "last_result.json").read_text(encoding="utf-8"))
            self.assertLess(data["finished_at"], snap2["server_now"])

    # ---------- S8 空结果 vs 全军覆没 ----------
    def test_s8_empty_vs_total_failure_indistinguishable(self):
        with TemporaryDirectory() as td:
            tmp = Path(td)
            # (a) 真空结果：平盘 + 合约也无任何命中（OI 零增速、多空比 1.5）
            spot = FakeSpot({"DOGE/USDT": flat_klines()})
            fut_flat = FakeFutures(oi=oi_hist(growth=0.0), ls=ls_hist(ratio=1.5))
            snap_a = ui_snapshot(lambda: run_scan(spot, fut_flat, make_cfg(tmp)), tmp)
            self.assertEqual(snap_a["status"], "empty")
            self.assertEqual(snap_a["rows"], [])
            self.assertIsNone(snap_a["error"])
            # (b) 逐币全部失败：每根 K 线请求都抛普通异常 → 行数 0 → 同样 empty
            spot2 = FakeSpot({"DOGE/USDT": rising_klines()},
                             ohlcv_fail={"DOGE/USDT": RuntimeError("模拟网络故障")})
            snap_b = ui_snapshot(
                lambda: run_scan(spot2, FakeFutures(), make_cfg(tmp / "b")), tmp / "b")
            self.assertEqual(snap_b["status"], "empty")
            self.assertEqual(snap_b["rows"], [])
            self.assertIsNone(snap_b["error"])
            # 两种情形 UI 三元组完全一致——总失败被当成"无候选"
            self.assertEqual((snap_a["status"], snap_a["error"], snap_a["rows"]),
                             (snap_b["status"], snap_b["error"], snap_b["rows"]))

    # ---------- 正面对照：现货域暂停应整轮失败、如实呈现 ----------
    def test_spot_pause_fails_honestly(self):
        with TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = make_cfg(tmp / "ui")
            seed_spot_pause(cfg)
            spot = FakeSpot({"DOGE/USDT": rising_klines()})
            with self.assertRaises(base.DomainPaused):
                base.public_selection(spot, FakeFutures(), cfg, None)
            snap = ui_snapshot(lambda: run_scan(spot, FakeFutures(), cfg), tmp / "ui")
            self.assertEqual(snap["status"], "failed")
            self.assertIsNone(snap["rows"])
            self.assertIn("暂停", snap["error"])
            self.assertTrue(snap["paused"])  # 快照能读出暂停域

    # ---------- 请求记账基线（供 ds 预算核对，方法调用口径） ----------
    def test_request_accounting_no_cache(self):
        with TemporaryDirectory() as td:
            tmp = Path(td)
            symbols = [f"C{i}/USDT" for i in range(3)]
            spot = FakeSpot({s: rising_klines() for s in symbols})
            fut = FakeFutures(symbols=tuple(f"{s}:USDT" for s in symbols))
            cfg = make_cfg(tmp)
            chosen = run_scan(spot, fut, cfg)
            self.assertEqual(len(chosen), 3)  # 全部 6+10=16 分过门槛
            self.assertEqual(spot.tickers_calls, 1)                # 现货行情 1 次
            self.assertEqual(sum(spot.ohlcv_calls.values()), 3)    # 逐币 K 线 N 次
            self.assertEqual(fut.oi_calls, 3)                      # 逐币 OI 历史 N 次
            self.assertEqual(fut.ls_calls, 3)                      # 逐币大户多空 N 次
            # 第二遍补取只对过门槛行：OI 快照 + 资金费率 × 3 币
            self.assertEqual(fut.snapshot_calls, 3)
            self.assertEqual(fut.funding_calls, 3)


class R2FallbackScoringTests(TestCase):
    """tasks/live_futures_r2.md glm-1：回退口径只能展示，不占 ls_top 计分。"""

    def test_r2_fallback_display_only_not_scored(self):
        with TemporaryDirectory() as td:
            tmp = Path(td)
            spot = FakeSpot({"DOGE/USDT": rising_klines()})
            fut = FakeFutures(ls_fail=True, ls_fallback=ls_hist(ratio=0.9))
            row = run_scan(spot, fut, make_cfg(tmp))[0]
            self.assertEqual(row["deriv_status"], "partial")
            # ls_top 键必须缺席——回退值不得冒充大户持仓比
            self.assertNotIn("ls_top", row)
            # 回退值保留为展示字段，来源可解释
            self.assertEqual(row.get("ls_global"), 0.9)
            self.assertEqual(row["ls_source"], "globalLongShortAccountRatio(回退,仅展示)")
            # 不加分：K 线 6 + OI 4+3 = 13（修复前回退偷拿 3 分=16）
            self.assertEqual(row["score_deriv"], 7)
            self.assertEqual(row["score"], 13)

    def test_r2_real_top_ratio_still_scores(self):
        with TemporaryDirectory() as td:
            tmp = Path(td)
            spot = FakeSpot({"DOGE/USDT": rising_klines()})
            fut = FakeFutures()  # 真实大户持仓口径 0.9
            row = run_scan(spot, fut, make_cfg(tmp))[0]
            self.assertEqual(row["ls_top"], 0.9)
            self.assertEqual(row["ls_source"], "topLongShortPositionRatio")
            self.assertEqual(row["score_deriv"], 10)  # OI 4+3 + 大户持仓多空比 3
            # ls_global 键恒在：真实口径下为 null（与 ls_source 同一形态，前端好处理）
            self.assertIsNone(row.get("ls_global"))


class R2RoundSummaryTests(TestCase):
    """tasks/live_futures_r2.md glm-2/3/4：整轮摘要、三态表达、as_of_min。"""

    def _stats(self):
        return {"kind": "round_summary"}

    def test_r2_summary_complete_round(self):
        with TemporaryDirectory() as td:
            tmp = Path(td)
            spot = FakeSpot({"A/USDT": rising_klines(), "B/USDT": rising_klines()})
            fut = FakeFutures(symbols=("A/USDT:USDT", "B/USDT:USDT"))
            stats = self._stats()
            run_scan(spot, fut, make_cfg(tmp), round_stats=stats)
            self.assertTrue(stats["task_completed"])
            self.assertEqual(stats["scope_total"], 2)
            self.assertEqual(stats["futures_expected"], 2)
            self.assertEqual(stats["futures_fetched_ok"], 2)
            self.assertEqual(stats["deriv_counts"]["ok"], 2)
            self.assertEqual(stats["deriv_domain_state"], "ok")
            self.assertTrue(stats["data_complete"])
            self.assertTrue(stats["has_passing"])
            self.assertEqual(stats["chosen_count"], 2)
            self.assertEqual(stats["errors"], 0)
            self.assertEqual(stats["skipped_short_history"], 0)
            self.assertEqual(stats["as_of_min"], T0 + 199 * HOUR)

    def test_r2_summary_no_client_expected_unknown(self):
        with TemporaryDirectory() as td:
            tmp = Path(td)
            spot = FakeSpot({"DOGE/USDT": rising_klines()})
            stats = self._stats()
            run_scan(spot, None, make_cfg(tmp), round_stats=stats)
            # 合约市场列表拿不到 → 应有合约数未知，不能填 0 并称齐全
            self.assertIsNone(stats["futures_expected"])
            self.assertEqual(stats["deriv_domain_state"], "no_client")
            self.assertFalse(stats["data_complete"])
            self.assertEqual(stats["deriv_counts"]["no_futures"], 1)

    def test_r2_summary_mid_scan_pause(self):
        with TemporaryDirectory() as td:
            tmp = Path(td)
            spot = FakeSpot({"A/USDT": rising_klines(), "B/USDT": rising_klines(),
                             "C/USDT": flat_klines()})
            fut = FakeFutures(symbols=("A/USDT:USDT", "B/USDT:USDT", "C/USDT:USDT"),
                              oi_fail_on_call=2)
            stats = self._stats()
            run_scan(spot, fut, make_cfg(tmp), round_stats=stats)
            self.assertEqual(stats["scope_total"], 3)
            self.assertEqual(stats["futures_expected"], 3)
            self.assertEqual(stats["futures_fetched_ok"], 1)
            self.assertEqual(stats["deriv_domain_state"], "paused")
            self.assertFalse(stats["data_complete"])
            self.assertTrue(stats["has_passing"])

    def test_r2_complete_data_no_candidates_is_normal(self):
        with TemporaryDirectory() as td:
            tmp = Path(td)
            spot = FakeSpot({"DOGE/USDT": flat_klines()})
            fut = FakeFutures(oi=oi_hist(growth=0.0), ls=ls_hist(ratio=1.5))
            stats = self._stats()
            rows = run_scan(spot, fut, make_cfg(tmp), round_stats=stats)
            self.assertEqual(rows, [])
            self.assertTrue(stats["data_complete"])
            self.assertFalse(stats["has_passing"])
            self.assertEqual(stats["chosen_count"], 0)

    def test_r2_cards_look_complete_but_dropped_coin_hidden(self):
        """用户核心场景：可见卡片字段全齐，落选币的缺数据只有摘要能暴露。"""
        with TemporaryDirectory() as td:
            tmp = Path(td)
            spot = FakeSpot({"A/USDT": rising_klines(), "B/USDT": rising_klines(),
                             "C/USDT": flat_klines()})
            # 故障点在第 3 次 OI 请求（C 的）：A/B 两个可见卡全 ok，C 被暂停后落选
            fut = FakeFutures(symbols=("A/USDT:USDT", "B/USDT:USDT", "C/USDT:USDT"),
                              oi_fail_on_call=3)
            stats = {"kind": "round_summary"}
            snap = ui_snapshot(lambda: ui_scan(spot, fut, make_cfg(tmp), stats), tmp)
            # 页面上的卡片确实"字段齐全"且全是 ok——单看卡片发现不了问题
            self.assertEqual(snap["status"], "success")
            for r in snap["rows"]:
                self.assertEqual(r["deriv_status"], "ok")
            # 摘要揭示被隐藏的落选缺数据
            self.assertEqual(stats["futures_fetched_ok"], 2)
            self.assertEqual(stats["futures_expected"], 3)
            self.assertFalse(stats["data_complete"])
            self.assertEqual(snap.get("round_quality"), "incomplete")
            self.assertEqual(snap["round"]["deriv_counts"]["paused"], 1)

    def test_r2_all_coins_failed_is_incomplete_not_empty(self):
        with TemporaryDirectory() as td:
            tmp = Path(td)
            spot = FakeSpot({"DOGE/USDT": rising_klines()},
                            ohlcv_fail={"DOGE/USDT": RuntimeError("模拟网络故障")})
            stats = {"kind": "round_summary"}
            snap = ui_snapshot(lambda: ui_scan(spot, FakeFutures(), make_cfg(tmp), stats),
                               tmp)
            self.assertEqual(snap["status"], "empty")  # 行数 0 的既有状态机
            self.assertGreaterEqual(stats["errors"], 1)
            self.assertFalse(stats["data_complete"])
            self.assertEqual(snap.get("round_quality"), "incomplete")

    def test_r2_as_of_min_not_max(self):
        """R2 原钉：as_of 不得掩盖陈旧 OI。R3 细化为**逐字段**时间——
        as_of_min 取所有字段实际采用时间的最小值（本夹具 LS 只有 100 根，
        比 OI 的 169h 更旧，故最小值是 99h）；字段级时间由 oi/ls_as_of_min 表达。"""
        with TemporaryDirectory() as td:
            tmp = Path(td)
            # A 的 OI 尾部 30h 全零（实际止于 169h）；B 数据新鲜
            spot = FakeSpot({"A/USDT": rising_klines(), "B/USDT": rising_klines()})
            fut = FakeFutures(
                symbols=("A/USDT:USDT", "B/USDT:USDT"),
                oi=oi_hist(growth=0.005, tail_zeros=30), ls=ls_hist(n=100))
            stats = {"kind": "round_summary"}
            run_scan(spot, fut, make_cfg(tmp), round_stats=stats)
            self.assertEqual(stats["oi_as_of_min"], T0 + (200 - 30 - 1) * HOUR)
            self.assertEqual(stats["ls_as_of_min"], T0 + 99 * HOUR)
            self.assertEqual(stats["as_of_min"], T0 + 99 * HOUR)  # 跨字段最小，不被掩盖

    def test_r2_backward_compat_without_stats(self):
        with TemporaryDirectory() as td:
            tmp = Path(td)
            spot = FakeSpot({"DOGE/USDT": rising_klines()})
            fut = FakeFutures()
            cfg = make_cfg(tmp)
            # 不传 round_stats：public_selection / scan 返回约定完全不变
            ranked = base.public_selection(spot, fut, cfg, None)
            self.assertIsInstance(ranked, list)
            rows = run_scan(spot, fut, make_cfg(tmp / "b"))
            self.assertIsInstance(rows, list)
            # UI 旧载荷（list）仍兼容
            snap = ui_snapshot(lambda: rows, tmp / "c")
            self.assertEqual(snap["status"], "success")
            self.assertIsNone(snap.get("round"))


class R2UiPayloadTests(TestCase):
    """glm-3：ScanManager 接受 {rows, round} 载荷；round_quality 三态。"""

    def test_ui_dict_payload_with_round(self):
        with TemporaryDirectory() as td:
            tmp = Path(td)
            payload = {"rows": [{"symbol": "DOGE/USDT", "score": 6}],
                       "round": {"task_completed": True, "data_complete": True,
                                 "has_passing": True, "futures_expected": 1,
                                 "futures_fetched_ok": 1, "errors": 0}}
            snap = ui_snapshot(lambda: payload, tmp)
            self.assertEqual(snap["status"], "success")
            self.assertEqual(snap["rows"][0]["symbol"], "DOGE/USDT")
            self.assertEqual(snap["round_quality"], "complete")
            self.assertEqual(snap["round"]["futures_expected"], 1)

    def test_ui_dict_payload_incomplete(self):
        with TemporaryDirectory() as td:
            tmp = Path(td)
            payload = {"rows": [{"symbol": "DOGE/USDT", "score": 6}],
                       "round": {"task_completed": True, "data_complete": False,
                                 "has_passing": True, "futures_expected": 3,
                                 "futures_fetched_ok": 1, "errors": 0}}
            snap = ui_snapshot(lambda: payload, tmp)
            self.assertEqual(snap["status"], "success")
            self.assertEqual(snap["round_quality"], "incomplete")

    def test_ui_old_list_payload_round_unknown(self):
        with TemporaryDirectory() as td:
            tmp = Path(td)
            snap = ui_snapshot(lambda: [{"symbol": "X/USDT", "score": 1}], tmp)
            self.assertEqual(snap["round_quality"], "unknown")


class R3FieldCompletenessTests(TestCase):
    """R3（reports/live_futures_r2_final_review.md）：完整性必须核对每个评分所需
    字段是否可计算及其**实际采用的数据时间**，不能只数 status=ok，
    也不能对取过最大值的时间再取最小值。"""

    def test_r3_short_oi_history_is_not_ok(self):
        """反例1：OI 只有 40 根小时数据 → 算不出 3 日变化，不得 ok/不得宣称完整。"""
        with TemporaryDirectory() as td:
            tmp = Path(td)
            spot = FakeSpot({"DOGE/USDT": rising_klines()})
            fut = FakeFutures(oi=oi_hist(n=40), ls=ls_hist())
            stats = {"kind": "round_summary"}
            rows = run_scan(spot, fut, make_cfg(tmp), round_stats=stats)
            row = rows[0]
            self.assertNotIn("oi_chg_3d", row)          # 三日变化确实不可计算
            self.assertEqual(row["deriv_status"], "partial")  # 不得 ok（1d 可算、3d 不可算）
            self.assertFalse(stats["data_complete"])     # 整轮不得宣称完整
            self.assertEqual(stats["futures_fetched_ok"], 0)

    def test_r3_trailing_zero_oi_not_masked_by_fresh_ls(self):
        """反例2：OI 尾部 30 小时合法零值被过滤 → 实际用 169h 前的旧数据；
        较新的 LS 时间不得掩盖它，整轮不得显示完整。"""
        with TemporaryDirectory() as td:
            tmp = Path(td)
            spot = FakeSpot({"DOGE/USDT": rising_klines()})
            fut = FakeFutures(oi=oi_hist(growth=0.005, tail_zeros=30), ls=ls_hist())
            stats = {"kind": "round_summary"}
            rows = run_scan(spot, fut, make_cfg(tmp), round_stats=stats)
            row = rows[0]
            # 字段都可计算（合法零值≠失败），但必须保留各自实际采用的数据时间
            self.assertEqual(row.get("oi_as_of"), T0 + (200 - 30 - 1) * HOUR)
            self.assertEqual(row.get("ls_as_of"), T0 + 199 * HOUR)
            self.assertFalse(stats["data_complete"])     # 最新观测不可用作基点 → 标不完整
            # 摘要的时间取“各字段实际采用时间”的最小值，不得被较新 LS 掩盖
            self.assertEqual(stats["as_of_min"], T0 + (200 - 30 - 1) * HOUR)
            self.assertEqual(stats["oi_as_of_min"], T0 + (200 - 30 - 1) * HOUR)
            self.assertEqual(stats["ls_as_of_min"], T0 + 199 * HOUR)
            self.assertGreaterEqual(stats.get("stale_rows", 0), 1)

    def test_r3_normal_complete_sample_unchanged(self):
        """正常完整样本：评分、排序、入选集合保持不变（回退误计分修复除外，已另行钉住）。"""
        with TemporaryDirectory() as td:
            tmp = Path(td)
            spot = FakeSpot({"A/USDT": rising_klines(), "B/USDT": rising_klines()})
            fut = FakeFutures(symbols=("A/USDT:USDT", "B/USDT:USDT"))
            stats = {"kind": "round_summary"}
            rows = run_scan(spot, fut, make_cfg(tmp), round_stats=stats)
            self.assertEqual([r["symbol"] for r in rows], ["A/USDT", "B/USDT"])
            self.assertTrue(all(r["deriv_status"] == "ok" for r in rows))
            self.assertTrue(all(r["score"] == 16 for r in rows))
            self.assertTrue(stats["data_complete"])
            self.assertEqual(stats["as_of_min"], T0 + 199 * HOUR)
            self.assertEqual(stats["stale_rows"], 0)

    def test_r3_inner_zeros_do_not_break_completeness(self):
        """序列中部的合法零值被过滤不影响完整性——计算基点仍是最新的可用观测。"""
        with TemporaryDirectory() as td:
            tmp = Path(td)
            spot = FakeSpot({"DOGE/USDT": rising_klines()})
            oi = oi_hist(growth=0.005)
            for item in oi[100:110]:  # 中部 10 根零值
                item["sumOpenInterestValue"] = 0
            fut = FakeFutures(oi=oi, ls=ls_hist())
            stats = {"kind": "round_summary"}
            run_scan(spot, fut, make_cfg(tmp), round_stats=stats)
            self.assertTrue(stats["data_complete"])
            self.assertEqual(stats["stale_rows"], 0)
            self.assertEqual(stats["oi_as_of_min"], T0 + 199 * HOUR)


if __name__ == "__main__":
    main()
