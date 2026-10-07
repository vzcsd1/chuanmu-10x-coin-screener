#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""离线测试：tools/archive_queue_ds.py（清单规范化、冲突阻断、排序轮转）

不联网。重点覆盖主代理审核报告点名的清单缺陷：
metrics 字段全空、fundingRate 币种带后缀、spot 清单混周期、来源身份不统一。

运行：
  py -3.10 -m unittest discover -s tests -p "test_archive_queue_ds.py" -v
"""
from __future__ import annotations

import contextlib
import csv
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import archive_download_ds as ad  # noqa: E402
import archive_queue_ds as aq  # noqa: E402

B = "https://data.binance.vision"


def write_part(path: Path, rows: list[dict]) -> Path:
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=ad.FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in ad.FIELDS})
    return path


class TestUrlParsing(unittest.TestCase):
    def test_klines_monthly(self):
        p = aq.parse_source_url(
            f"{B}/data/spot/monthly/klines/ADAUSDT/1h/ADAUSDT-1h-2024-01.zip")
        self.assertEqual((p.market, p.dataset, p.symbol, p.interval), ("spot", "klines", "ADAUSDT", "1h"))
        self.assertEqual((p.period_start, p.period_end), ("2024-01-01", "2024-01-31"))
        self.assertEqual(p.problem, "")

    def test_klines_daily_single_day(self):
        p = aq.parse_source_url(
            f"{B}/data/futures/um/daily/klines/ADAUSDT/1d/ADAUSDT-1d-2024-01-15.zip")
        self.assertEqual((p.period_start, p.period_end), ("2024-01-15", "2024-01-15"))
        self.assertEqual(p.market, "futures-um")

    def test_metrics_recovers_symbol_and_full_date(self):
        """metrics 清单里 symbol 全空、日期只剩日号 —— URL 必须能还原真实身份。"""
        p = aq.parse_source_url(
            f"{B}/data/futures/um/daily/metrics/0GUSDT/0GUSDT-metrics-2025-09-17.zip")
        self.assertEqual(p.symbol, "0GUSDT")
        self.assertEqual((p.period_start, p.period_end), ("2025-09-17", "2025-09-17"))
        self.assertEqual(p.dataset, "metrics")

    def test_funding_strips_category_suffix(self):
        """fundingRate 清单把 `-fundingRate` 当成币种的一部分 —— 必须去掉。"""
        p = aq.parse_source_url(
            f"{B}/data/futures/um/monthly/fundingRate/1000BTTCUSDT/"
            f"1000BTTCUSDT-fundingRate-2022-01.zip")
        self.assertEqual(p.symbol, "1000BTTCUSDT")
        self.assertEqual(p.period_start, "2022-01-01")
        self.assertEqual(p.period_end, "2022-01-31")

    def test_coin_margined_kept_separate(self):
        p = aq.parse_source_url(
            f"{B}/data/futures/cm/monthly/klines/BTCUSD_PERP/1h/BTCUSD_PERP-1h-2024-01.zip")
        self.assertEqual(p.market, "futures-cm")
        self.assertEqual(p.symbol, "BTCUSD_PERP")

    def test_unknown_shape_is_problem_not_guess(self):
        p = aq.parse_source_url("https://example.com/some/other/file.zip")
        self.assertNotEqual(p.problem, "")
        self.assertEqual(p.symbol, "")

    def test_path_symbol_mismatch_flagged(self):
        p = aq.parse_source_url(
            f"{B}/data/spot/monthly/klines/ADAUSDT/1h/BTCUSDT-1h-2024-01.zip")
        self.assertIn("不一致", p.problem)

    def test_interval_mismatch_flagged(self):
        p = aq.parse_source_url(
            f"{B}/data/spot/monthly/klines/ADAUSDT/1h/ADAUSDT-5m-2024-01.zip")
        self.assertIn("周期", p.problem)


class TestNormalization(unittest.TestCase):
    def _row(self, url: str, **kw) -> dict:
        row = {"source_url": url, "market": "", "dataset": "", "symbol": "",
               "interval": "", "period_start": "", "period_end": "",
               "remote_size_bytes": "100", "checksum_url": url + ".CHECKSUM",
               "discovered_at_utc": "2026-10-06T00:00:00Z"}
        row.update(kw)
        return row

    def test_market_identity_unified(self):
        """futures 与 futures-um 必须归一到同一身份，避免重复下载。"""
        a, _, _ = aq.normalize_entry(self._row(
            f"{B}/data/futures/um/daily/metrics/ADAUSDT/ADAUSDT-metrics-2024-01-15.zip",
            market="futures"))
        b, _, _ = aq.normalize_entry(self._row(
            f"{B}/data/futures/um/daily/metrics/ADAUSDT/ADAUSDT-metrics-2024-01-15.zip",
            market="futures-um"))
        self.assertEqual(a.market, b.market)
        self.assertEqual(a.key, b.key)
        self.assertEqual(a.relpath(), b.relpath())

    def test_field_mismatch_is_reported_not_silent(self):
        _, status, notes = aq.normalize_entry(self._row(
            f"{B}/data/futures/um/monthly/fundingRate/ADAUSDT/ADAUSDT-fundingRate-2024-01.zip",
            symbol="ADAUSDT-fundingRate"))
        self.assertEqual(status, "field_mismatch")
        self.assertTrue(any("symbol" in n for n in notes))

    def test_metrics_row_with_empty_fields_is_accepted(self):
        e, status, _ = aq.normalize_entry(self._row(
            f"{B}/data/futures/um/daily/metrics/ADAUSDT/ADAUSDT-metrics-2024-01-15.zip"))
        self.assertEqual(status, "ok")
        self.assertEqual(e.symbol, "ADAUSDT")
        self.assertEqual(e.period_start, "2024-01-15")


class TestBuildQueue(unittest.TestCase):
    def _build(self, rows: list[dict]) -> dict:
        tmp = Path(self.tmp.name)
        part = write_part(tmp / "part.csv", rows)
        # 重定向输出到临时目录
        old_dir, old_q, old_c, old_j = (aq.REPORT_DIR, aq.QUEUE_CSV,
                                        aq.CONFLICT_CSV, aq.BUILD_JSON)
        aq.REPORT_DIR = tmp
        aq.QUEUE_CSV = tmp / "queue.csv"
        aq.CONFLICT_CSV = tmp / "conflicts.csv"
        aq.BUILD_JSON = tmp / "build.json"
        try:
            return aq.build_queue([str(part)])
        finally:
            aq.REPORT_DIR, aq.QUEUE_CSV, aq.CONFLICT_CSV, aq.BUILD_JSON = (
                old_dir, old_q, old_c, old_j)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_spot_mixed_intervals_are_separated(self):
        """spot 5m 清单里混了非 USDT 的 1d 行 —— 必须按 URL 的真实周期归类。"""
        s = self._build([
            {"source_url": f"{B}/data/spot/monthly/klines/ADAUSDT/5m/ADAUSDT-5m-2024-01.zip"},
            {"source_url": f"{B}/data/spot/monthly/klines/XYZUSDT/1d/XYZUSDT-1d-2024-01.zip"},
        ])
        self.assertEqual(s["stats"]["problem"], 0)
        cats = s["by_category"]
        self.assertIn("spot/klines/5m", cats)
        self.assertIn("spot/klines/1d", cats)

    def test_conflicting_urls_block_shard(self):
        """两条不同 URL 映射到同一状态键 → 整组剔除并报告，不静默去重。"""
        s = self._build([
            {"source_url": f"{B}/data/spot/monthly/klines/ADAUSDT/1h/ADAUSDT-1h-2024-01.zip",
             "remote_size_bytes": "111"},
            {"source_url": f"{B}/data/spot/monthly/klines/ADAUSDT/1h/ADAUSDT-1h-2024-01.zip?v=2",
             "remote_size_bytes": "222"},
        ])
        self.assertGreaterEqual(s["conflicts"], 1)
        self.assertGreaterEqual(s["blocked_keys"], 1)
        self.assertEqual(s["queue_rows"], 0, "冲突分片不得进入队列")

    def test_identical_url_deduped_quietly(self):
        s = self._build([
            {"source_url": f"{B}/data/spot/monthly/klines/ADAUSDT/1h/ADAUSDT-1h-2024-01.zip"},
            {"source_url": f"{B}/data/spot/monthly/klines/ADAUSDT/1h/ADAUSDT-1h-2024-01.zip"},
        ])
        self.assertEqual(s["stats"]["duplicate_url"], 1)
        self.assertEqual(s["queue_rows"], 1)
        self.assertEqual(s["conflicts"], 0)

    def test_order_rotates_period_then_symbol(self):
        """排序必须按时间片轮转币种，避免只有字母表前几个币下载齐。"""
        rows = []
        for sym in ("AAAUSDT", "BBBUSDT"):
            for m in ("2024-01", "2024-02"):
                rows.append({"source_url":
                             f"{B}/data/spot/monthly/klines/{sym}/1h/{sym}-1h-{m}.zip"})
        self._build(rows)
        entries = ad.load_manifest(Path(self.tmp.name) / "queue.csv")
        got = [(e.period_start, e.symbol) for e in entries]
        self.assertEqual(got, [("2024-01-01", "AAAUSDT"), ("2024-01-01", "BBBUSDT"),
                               ("2024-02-01", "AAAUSDT"), ("2024-02-01", "BBBUSDT")])

    def test_funding_ranked_before_klines(self):
        rows = [
            {"source_url": f"{B}/data/spot/monthly/klines/AAAUSDT/1h/AAAUSDT-1h-2024-01.zip"},
            {"source_url": f"{B}/data/futures/um/monthly/fundingRate/BBBUSDT/BBBUSDT-fundingRate-2024-01.zip"},
        ]
        self._build(rows)
        entries = ad.load_manifest(Path(self.tmp.name) / "queue.csv")
        self.assertEqual(entries[0].dataset, "fundingRate")

    def test_unparseable_row_excluded_and_counted(self):
        s = self._build([{"source_url": "https://example.com/not/an/archive.zip"}])
        self.assertEqual(s["stats"]["problem"], 1)
        self.assertEqual(s["queue_rows"], 0)


class TestPendingAdvances(unittest.TestCase):
    """无人值守关键回归：确定性失败项必须被跳过。

    否则若某块首项永远失败，`todo[:chunk]` 每轮都取到同一批，采集永远推不到后面的币种。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)
        self.state = ad.State(self.out / ad.STATE_NAME)

    def tearDown(self):
        self.tmp.cleanup()

    def _entry(self, sym: str):
        e, status, _ = aq.normalize_entry({
            "source_url": f"{B}/data/spot/monthly/klines/{sym}/1h/{sym}-1h-2024-01.zip",
            "remote_size_bytes": "100"})
        self.assertEqual(status, "ok")
        return e

    def _place(self, e, payload: bytes = b"x"):
        p = self.out / e.relpath()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(payload)
        return p

    def test_all_pending_when_nothing_on_disk(self):
        es = [self._entry(s) for s in ("AAAUSDT", "BBBUSDT", "CCCUSDT")]
        self.assertEqual(len(aq._pending(es, set())), 3)

    def test_deferred_key_is_skipped_so_window_advances(self):
        es = [self._entry(s) for s in ("AAAUSDT", "BBBUSDT", "CCCUSDT")]
        got = aq._pending(es, set(), {es[0].key})
        self.assertEqual([e.symbol for e in got], ["BBBUSDT", "CCCUSDT"])

    def test_only_deterministic_failures_are_deferred(self):
        """网络类失败可能只是抖动，必须留在队列里自动重试，不能被搁置。"""
        self.assertIn(ad.S_NOT_FOUND, aq._DEFER_STATUSES)
        self.assertIn(ad.S_ANOMALY, aq._DEFER_STATUSES)
        self.assertNotIn(ad.S_TRANSIENT, aq._DEFER_STATUSES)

    # —— 任务第 6 条：完成判定必须区分四态，不能"存在即完成" ——

    def test_file_present_but_no_state_is_not_done(self):
        """**核心回归**：文件在磁盘上、但台账里根本没有记录 → 不算完成。"""
        e = self._entry("AAAUSDT")
        self._place(e)
        self.assertEqual(aq.classify_entry(e, self.out, self.state),
                         aq.C_NEEDS_VERIFY)
        self.assertEqual(aq.build_done_set([e], self.out, self.state), set())
        self.assertEqual([x.key for x in aq._pending([e], set())], [e.key])

    def test_size_mismatch_is_bad_and_pending(self):
        """台账字节数与磁盘不符（截断/损坏）→ 坏文件，需重下。"""
        e = self._entry("AAAUSDT")
        self._place(e, b"12345")
        self.state.set_record(e, status=ad.S_SUCCESS, bytes=9999,
                              checksum_status=ad.CHECK_VERIFIED)
        self.assertEqual(aq.classify_entry(e, self.out, self.state), aq.C_BAD)
        self.assertNotIn(e.key, aq.build_done_set([e], self.out, self.state))

    def test_mismatch_status_is_bad_not_done(self):
        """来源校验值不符 → 绝不能算完成。"""
        e = self._entry("AAAUSDT")
        self._place(e, b"12345")
        self.state.set_record(e, status=ad.S_ANOMALY, bytes=5,
                              checksum_status=ad.CHECK_MISMATCH)
        self.assertEqual(aq.classify_entry(e, self.out, self.state), aq.C_BAD)

    def test_present_file_with_failed_checksum_is_needs_verify(self):
        """文件已落盘但来源校验值取不到 → 待验证（只补校验，不重下）。"""
        e = self._entry("AAAUSDT")
        self._place(e, b"12345")
        self.state.set_record(e, status=ad.S_UNVERIFIED, bytes=5,
                              checksum_status=ad.CHECK_FETCH_FAILED)
        self.assertEqual(aq.classify_entry(e, self.out, self.state),
                         aq.C_NEEDS_VERIFY)
        self.assertEqual([x.key for x in aq._pending([e], set())], [e.key])

    def test_verified_with_matching_size_is_done(self):
        e = self._entry("AAAUSDT")
        self._place(e, b"12345")
        self.state.set_record(e, status=ad.S_SUCCESS, bytes=5,
                              checksum_status=ad.CHECK_VERIFIED)
        self.assertEqual(aq.classify_entry(e, self.out, self.state), aq.C_DONE)
        self.assertIn(e.key, aq.build_done_set([e], self.out, self.state))
        self.assertEqual(aq._pending([e], {e.key}), [])

    def test_no_checksum_source_counts_done(self):
        """来源确实没发布校验值文件（404）→ 可算完成（结论明确）。"""
        e = self._entry("AAAUSDT")
        self._place(e, b"12345")
        self.state.set_record(e, status=ad.S_SUCCESS, bytes=5,
                              checksum_status=ad.CHECK_NO_SOURCE)
        self.assertEqual(aq.classify_entry(e, self.out, self.state), aq.C_DONE)

    def test_missing_file_is_missing(self):
        e = self._entry("AAAUSDT")
        self.assertEqual(aq.classify_entry(e, self.out, self.state), aq.C_MISSING)

    def test_pending_bytes_are_needs_verify_not_missing(self):
        """**限流交错的关键回归**：正式文件不在、但 `.pending` 有待复核字节时，
        必须判「待验证」而不是「缺文件」——否则恢复后会把整份归档重下一次。

        `.pending` 一定与「校验值获取失败」的台账同时出现（写它时就会记这条），
        所以这里按真实配对来构造。
        """
        e = self._entry("AAAUSDT")
        pending = ad.pending_path_for(self.out, e)
        pending.parent.mkdir(parents=True, exist_ok=True)
        pending.write_bytes(b"12345")
        self.state.set_record(e, status=ad.S_PAUSED, bytes=5, path=str(pending),
                              checksum_status=ad.CHECK_FETCH_FAILED)
        self.assertEqual(aq.classify_entry(e, self.out, self.state),
                         aq.C_NEEDS_VERIFY)
        self.assertNotIn(e.key, aq.build_done_set([e], self.out, self.state))
        self.assertEqual([x.key for x in aq._pending([e], set())], [e.key])

    def test_pending_without_ledger_is_missing_by_design(self):
        """没有台账就只认"缺文件"：启动扫描要跑百万次，
        无条件多一次 stat 会让扫描时间翻倍，这里是有意的性能取舍。"""
        e = self._entry("AAAUSDT")
        pending = ad.pending_path_for(self.out, e)
        pending.parent.mkdir(parents=True, exist_ok=True)
        pending.write_bytes(b"12345")
        self.assertEqual(aq.classify_entry(e, self.out, self.state), aq.C_MISSING)

    def test_compact_threshold_scales_with_queue(self):
        """整理一次要写整库，阈值必须随队列增大，否则退化成平方级开销。"""
        self.assertGreaterEqual(aq.auto_compact_every(1_000_000), 50_000)
        self.assertGreaterEqual(aq.auto_compact_every(10), 5000)


class TestRunQueueProgress(unittest.TestCase):
    """回归：`progress_*.json` 的「完成字节」必须含**本块刚下**的文件。

    旧实现每块从「本块开始时加载的 state」重算 done_bytes，本块新落的文件
    不在里面 → 永远少算最后一块（实盘偏差约 12%）。改为启动时算一次、之后增量累加。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.out = self.root / "out"
        self.out.mkdir()
        self.report = self.root / "rep"
        self.report.mkdir()
        self._saved = (aq.OUT_DIR, aq.paths_for, aq._old_queue_total, aq._setup_logging)
        aq.OUT_DIR = self.out
        aq.paths_for = lambda v: {
            "queue": self.report / "queue.csv",
            "conflicts": self.report / "conflicts.csv",
            "build": self.report / "build.json",
            "progress": self.report / "progress.json",
            "log": self.report / "run.log",
            "pid": self.report / "pid.txt",
            "ramp": self.report / "ramp.json",
            "blocks": self.report / "blocks.jsonl"}
        aq._old_queue_total = lambda: 251951
        aq._setup_logging = lambda p: None
        self._real_run_fetch = ad.run_fetch
        ad.run_fetch = self._fake_fetch

    def tearDown(self):
        ad.run_fetch = self._real_run_fetch
        (aq.OUT_DIR, aq.paths_for, aq._old_queue_total, aq._setup_logging) = self._saved
        self.tmp.cleanup()

    def _fake_fetch(self, entries, opts, fetcher=None, sleeper=None):
        """离线替身：真落盘 100 字节 + 写台账，返回与真下载器同形的结果。"""
        state = ad.State(opts.state_path)
        results = []
        for e in entries:
            p = opts.out_dir / e.relpath()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"y" * 100)
            state.set_record(e, status=ad.S_SUCCESS, bytes=100, path=str(p),
                             checksum_status=ad.CHECK_VERIFIED)
            results.append({"key": e.key, "status": ad.S_SUCCESS, "bytes": 100})
        return results, "共 %d 项 | 新下成功 %d | 成功字节 %d" % (
            len(entries), len(entries), 100 * len(entries))

    def test_completed_bytes_includes_current_chunk(self):
        rows = []
        for sym in ("AAAUSDT", "BBBUSDT", "CCCUSDT"):
            e, status, _ = aq.normalize_entry({
                "source_url": f"{B}/data/spot/monthly/klines/{sym}/1h/{sym}-1h-2024-01.zip",
                "remote_size_bytes": "100"})
            self.assertEqual(status, "ok")
            rows.append({f: getattr(e, f) for f in ad.FIELDS})
        write_part(self.report / "queue.csv", rows)

        rc = aq.run_queue(chunk=3, max_minutes=0, min_free_gib=0.0,
                          interval_sec=0.0, max_chunks=1, version="vtest")
        self.assertEqual(rc, 0)
        prog = json.loads((self.report / "progress.json").read_text(encoding="utf-8"))
        self.assertEqual(prog["queue_total"], 3)          # 新分母
        self.assertEqual(prog["old_queue_total"], 251951)  # 旧分母对照
        self.assertEqual(prog["completed"], 3)
        self.assertEqual(prog["completed_bytes"], 300)     # 旧实现这里会是 0
        self.assertEqual(prog["run_new_files"], 3)
        self.assertEqual(prog["run_new_bytes"], 300)


class TestStatusReadOrder(unittest.TestCase):
    """回归：`status` 必须先读队列、后读状态。

    反了的话，读 280 MB 队列的约 30 秒里**新落盘**的文件会被扫盘看见、
    却不在已加载的快照里 → 被误报成 `needs_verify`（实测假阳性数百条）。
    这里用一个"读队列时顺便落一个文件+写一条台账"的替身来区分两种顺序。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.out = self.root / "out"
        self.out.mkdir()
        self.report = self.root / "rep"
        self.report.mkdir()
        self._saved = (aq.OUT_DIR, aq.paths_for, aq._old_queue_total, aq.load_queue)
        aq.OUT_DIR = self.out
        aq.paths_for = lambda v: {
            "queue": self.report / "queue.csv",
            "conflicts": self.report / "conflicts.csv",
            "build": self.report / "build.json",
            "progress": self.report / "progress.json",
            "log": self.report / "run.log",
            "pid": self.report / "pid.txt"}
        aq._old_queue_total = lambda: 251951

    def tearDown(self):
        (aq.OUT_DIR, aq.paths_for, aq._old_queue_total, aq.load_queue) = self._saved
        self.tmp.cleanup()

    def _entry(self, sym: str = "AAAUSDT"):
        e, status, _ = aq.normalize_entry({
            "source_url": f"{B}/data/spot/monthly/klines/{sym}/1h/{sym}-1h-2024-01.zip",
            "remote_size_bytes": "5"})
        self.assertEqual(status, "ok")
        return e

    def test_state_loaded_after_queue(self):
        e = self._entry()
        (self.report / "queue.csv").write_text("", encoding="utf-8")  # status 先判存在

        def fake_load_queue(_path):
            # 模拟"读队列这 30 秒里下载器落了盘并写了台账"
            p = self.out / e.relpath()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"12345")
            ad.State(self.out / ad.STATE_NAME).set_record(
                e, status=ad.S_SUCCESS, bytes=5, path=str(p),
                checksum_status=ad.CHECK_VERIFIED)
            return [e]

        aq.load_queue = fake_load_queue
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = aq.status("vtest")
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("已完成 1", out)              # 台账在状态之后加载 → 认得出
        self.assertIn("'done': 1", out)
        self.assertNotIn("needs_verify", out)


# ---------------------------------------------------------------------------
# 提速任务新增：逐档换档 / 停止哨兵 / 暂停不误退 / 每块指标
# ---------------------------------------------------------------------------
class TestRampController(unittest.TestCase):
    """档位判定的纯逻辑（不碰磁盘、不联网）。"""

    def _rc(self, **kw):
        return aq.RampController(**kw)

    def test_first_clean_block_records_baseline_then_steps_up(self):
        rc = self._rc()
        self.assertEqual(rc.tier["name"], "A")
        act = rc.observe(10.0, "spot/klines/1h", ok=True)
        self.assertEqual(act["action"], "up")
        self.assertEqual(rc.tier["name"], "B")
        self.assertEqual(rc.baseline, 10.0)

    def test_improvement_keeps_stepping_up(self):
        rc = self._rc()
        rc.observe(10.0, "c", ok=True)          # A→B，基线 10
        act = rc.observe(13.0, "c", ok=True)    # +30% ≥ 10% → 接受并上探 C
        self.assertEqual(act["action"], "up")
        self.assertEqual(rc.tier["name"], "C")

    def test_two_flat_windows_revert_to_lower_tier_and_lock(self):
        rc = self._rc()
        rc.observe(10.0, "c", ok=True)          # A→B，基线 10
        first = rc.observe(10.2, "c", ok=True)  # +2% < 10% → 第 1 窗
        self.assertEqual(first["action"], "hold")
        self.assertEqual(rc.tier["name"], "B")
        second = rc.observe(10.1, "c", ok=True)  # 第 2 窗 → 平台期
        self.assertEqual(second["action"], "revert")
        self.assertEqual(rc.tier["name"], "A", "平台期应退回较低档")
        self.assertTrue(rc.locked, "平台期后必须锁定，不再加线程")
        self.assertEqual(rc.observe(99.0, "c", ok=True)["action"], "hold")

    def test_dirty_block_does_not_participate(self):
        rc = self._rc()
        act = rc.observe(0.0, "c", ok=False)     # 有错误/暂停的块
        self.assertEqual(act["action"], "hold")
        self.assertEqual(rc.tier["name"], "A")
        self.assertIsNone(rc.baseline)

    def test_category_change_resets_to_lowest_tier(self):
        rc = self._rc()
        rc.observe(10.0, "futures/metrics/-", ok=True)     # → B
        rc.observe(20.0, "futures/metrics/-", ok=True)     # → C
        self.assertEqual(rc.tier["name"], "C")
        act = rc.observe(30.0, "spot/klines/5m", ok=True)  # 资料类别变了
        self.assertEqual(act["action"], "reset")
        self.assertEqual(rc.tier["name"], "A", "类别变化要保守重判回最低档")
        self.assertFalse(rc.locked)

    def test_cannot_exceed_top_tier(self):
        rc = self._rc()
        for i in range(6):
            rc.observe(10.0 * (i + 1), "c", ok=True)
        self.assertEqual(rc.tier["name"], "D", "不得越过最高档")

    def test_persist_round_trip(self):
        rc = self._rc()
        rc.observe(10.0, "c", ok=True)
        rc.observe(15.0, "c", ok=True)
        raw = json.loads(json.dumps(rc.to_dict()))
        again = aq.RampController.from_dict(raw)
        self.assertEqual(again.level, rc.level)
        self.assertEqual(again.baseline, rc.baseline)
        self.assertEqual(again.category, "c")


class TestStopSentinel(unittest.TestCase):
    """停止哨兵：可打断等待、陈旧哨兵不误停、消费即删除。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_stale_sentinel_is_ignored(self):
        sp = aq.stop_path(self.out)
        sp.write_text("x", encoding="utf-8")
        old = sp.stat().st_mtime - 600
        os.utime(sp, (old, old))
        self.assertFalse(aq.stop_requested(self.out, since=time.time()),
                         "启动前的陈旧哨兵不得把本次启动打死")
        self.assertTrue(aq.stop_requested(self.out, since=None))

    def test_fresh_sentinel_is_honored_and_consumed(self):
        aq.stop_path(self.out).write_text("x", encoding="utf-8")
        self.assertTrue(aq.stop_requested(self.out, since=time.time() - 60))
        self.assertTrue(aq.clear_stop(self.out))
        self.assertFalse(aq.stop_path(self.out).exists())
        self.assertFalse(aq.clear_stop(self.out), "重复消费应返回 False")

    def test_wait_is_interruptible(self):
        since = time.time()
        threading.Timer(0.25, lambda: aq.stop_path(self.out).write_text("x", encoding="utf-8")).start()
        t0 = time.time()
        interrupted = aq.wait_with_stop(self.out, since, 30.0, step=0.05)
        self.assertTrue(interrupted, "等待必须能被停止哨兵打断")
        self.assertLess(time.time() - t0, 5.0, "不应真的等满 30 秒")

    def test_wait_returns_false_when_no_stop(self):
        self.assertFalse(aq.wait_with_stop(self.out, time.time(), 0.2, step=0.05))


class TestRunQueueRampAndStop(unittest.TestCase):
    """`run_queue` 集成：块边界换档、跨重启保持、暂停不误退、指标落盘。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.out = self.root / "out"
        self.out.mkdir()
        self.report = self.root / "rep"
        self.report.mkdir()
        self._saved = (aq.OUT_DIR, aq.paths_for, aq._old_queue_total, aq._setup_logging)
        aq.OUT_DIR = self.out
        aq.paths_for = lambda v: {
            "queue": self.report / "queue.csv",
            "conflicts": self.report / "conflicts.csv",
            "build": self.report / "build.json",
            "progress": self.report / "progress.json",
            "log": self.report / "run.log",
            "pid": self.report / "pid.txt",
            "ramp": self.report / "ramp.json",
            "blocks": self.report / "blocks.jsonl"}
        aq._old_queue_total = lambda: 0
        aq._setup_logging = lambda p: None
        self._real_run_fetch = ad.run_fetch

    def tearDown(self):
        ad.run_fetch = self._real_run_fetch
        (aq.OUT_DIR, aq.paths_for, aq._old_queue_total, aq._setup_logging) = self._saved
        self.tmp.cleanup()

    def _write_queue(self, n: int = 6) -> None:
        rows = []
        for i in range(n):
            sym = f"Q{i}USDT"
            e, status, _ = aq.normalize_entry({
                "source_url": f"{B}/data/spot/monthly/klines/{sym}/1h/{sym}-1h-2024-01.zip",
                "remote_size_bytes": "100"})
            self.assertEqual(status, "ok")
            rows.append({f: getattr(e, f) for f in ad.FIELDS})
        write_part(self.report / "queue.csv", rows)

    def _install_fake(self, *, status=ad.S_SUCCESS, summary_extra=""):
        calls: list[dict] = []

        def fake(entries, opts, fetcher=None, sleeper=None):
            calls.append({"workers": opts.workers, "interval": opts.interval_sec,
                          "n": len(entries)})
            opts.last_request_count = len(entries) * 2      # 每份 2 个请求（ZIP+CHECKSUM）
            results = [{"key": e.key, "status": status, "bytes": 100} for e in entries]
            n_ok = len(entries) if status == ad.S_SUCCESS else 0
            n_paused = len(entries) if status == ad.S_PAUSED else 0
            # **复刻真实汇总串**：无论是否限流，字面量「限流暂停」总在里面。
            # 判定必须看结果状态，不能对这个字符串做 in 判断。
            summary = (f"共 {len(entries)} 项 | 本地完整 0 | 新下成功 {n_ok} | "
                       f"待复核 0 | 未找到 0 | 限流暂停 {n_paused} | "
                       f"网络失败 0 | 内容异常 0 | 预算停止 0 | "
                       f"成功字节 {100 * n_ok}{summary_extra}")
            return results, summary

        ad.run_fetch = fake
        return calls

    def _blocks(self) -> list[dict]:
        p = self.report / "blocks.jsonl"
        if not p.exists():
            return []
        return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]

    def test_summary_wording_does_not_fake_a_pause(self):
        """回归：汇总串里永远含字面量「限流暂停 0」，不能被当成"本块被限流"。"""
        self._write_queue(6)
        calls = self._install_fake()          # 全部 S_SUCCESS，但汇总含「限流暂停 0」
        aq.run_queue(chunk=3, max_minutes=0, min_free_gib=0.0, interval_sec=0.0,
                     max_chunks=2, version="vtest")
        self.assertEqual(calls[1]["workers"], aq.RAMP_TIERS[1]["workers"],
                         "干净块必须能上探；把汇总字面量当限流会让逐档永久卡在 A")
        b = self._blocks()[0]
        self.assertFalse(b["paused"], "没有 paused_ratelimit 结果就不是被限流")

    def test_tier_changes_at_block_boundary(self):
        self._write_queue(6)
        calls = self._install_fake()
        rc = aq.run_queue(chunk=3, max_minutes=0, min_free_gib=0.0, interval_sec=0.0,
                          max_chunks=2, version="vtest")
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["workers"], aq.RAMP_TIERS[0]["workers"], "第 1 块应为 A 档")
        self.assertEqual(calls[1]["workers"], aq.RAMP_TIERS[1]["workers"],
                         "第 2 块应在块边界换到 B 档（不重启进程）")
        self.assertEqual(calls[1]["interval"], aq.RAMP_TIERS[1]["interval_sec"])

    def test_tier_persists_across_restart(self):
        self._write_queue(6)
        self._install_fake()
        aq.run_queue(chunk=3, max_minutes=0, min_free_gib=0.0, interval_sec=0.0,
                     max_chunks=1, version="vtest")
        self.assertEqual(json.loads((self.report / "ramp.json").read_text(encoding="utf-8"))["level"],
                         1, "首块后应已上探到 B 并落盘")

        calls2 = self._install_fake()      # 模拟重启：新进程读 ramp.json
        aq.run_queue(chunk=3, max_minutes=0, min_free_gib=0.0, interval_sec=0.0,
                     max_chunks=1, version="vtest")
        self.assertEqual(calls2[0]["workers"], aq.RAMP_TIERS[1]["workers"],
                         "重启后必须从上次档位继续，不能回到 A")

    def test_pause_does_not_trigger_no_progress_exit(self):
        self._write_queue(9)
        calls = self._install_fake(status=ad.S_PAUSED, summary_extra=" | 限流暂停 3")
        rc = aq.run_queue(chunk=3, max_minutes=0, min_free_gib=0.0, interval_sec=0.0,
                          max_chunks=5, version="vtest")
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 5,
                         "限流暂停不算「零进展」，不得触发连续 3 块退出")

    def test_stop_sentinel_exits_before_first_block(self):
        self._write_queue(6)
        aq.stop_path(self.out).write_text("x", encoding="utf-8")   # 刚创建 → 非陈旧
        calls = self._install_fake()
        rc = aq.run_queue(chunk=3, max_minutes=0, min_free_gib=0.0, interval_sec=0.0,
                          max_chunks=3, version="vtest")
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [], "命中停止哨兵后不得再开新块")
        self.assertFalse(aq.stop_path(self.out).exists(), "哨兵应被消费删除")

    def test_blocks_metrics_written(self):
        self._write_queue(3)
        self._install_fake()
        aq.run_queue(chunk=3, max_minutes=0, min_free_gib=0.0, interval_sec=0.0,
                     max_chunks=1, version="vtest")
        blocks = self._blocks()
        self.assertEqual(len(blocks), 1)
        b = blocks[0]
        for key in ("tier", "workers", "interval_sec", "category", "wall_sec",
                    "confirmed", "new_files", "requests", "rate_files_per_sec",
                    "rss_bytes", "errors", "paused", "ramp"):
            self.assertIn(key, b, f"每块指标缺少 {key}")
        self.assertEqual(b["requests"], 6, "请求数必须计入 ZIP+CHECKSUM")
        self.assertEqual(b["new_files"], 3)
        self.assertEqual(b["tier"], "A")

    def test_ramp_off_uses_fixed_parameters(self):
        self._write_queue(3)
        calls = self._install_fake()
        aq.run_queue(chunk=3, max_minutes=0, min_free_gib=0.0, interval_sec=0.75,
                     max_chunks=1, workers=3, version="vtest", ramp="off")
        self.assertEqual(calls[0]["workers"], 3)
        self.assertEqual(calls[0]["interval"], 0.75)

    def test_explicit_tier_pins_and_disables_autostep(self):
        self._write_queue(6)
        calls = self._install_fake()
        aq.run_queue(chunk=3, max_minutes=0, min_free_gib=0.0, interval_sec=0.0,
                     max_chunks=2, version="vtest", tier="C")
        self.assertEqual([c["workers"] for c in calls],
                         [aq.RAMP_TIERS[2]["workers"]] * 2, "钉档后两块都应停在 C")

    def test_unknown_tier_is_rejected(self):
        self._write_queue(3)
        self._install_fake()
        rc = aq.run_queue(chunk=3, max_minutes=0, min_free_gib=0.0, interval_sec=0.0,
                          max_chunks=1, version="vtest", tier="Z")
        self.assertEqual(rc, 2)


class TestRunQueueEndToEnd(unittest.TestCase):
    """用**真实** `run_fetch`（只把取数层换成假 fetcher）跑通
    「停止哨兵 → 保存退出 → 续传不重下」。这是切换/续传的端到端回归。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.out = self.root / "out"
        self.out.mkdir()
        self.report = self.root / "rep"
        self.report.mkdir()
        self._saved = (aq.OUT_DIR, aq.paths_for, aq._old_queue_total,
                       aq._setup_logging, ad.RequestsFetcher)
        aq.OUT_DIR = self.out
        aq.paths_for = lambda v: {
            "queue": self.report / "queue.csv",
            "conflicts": self.report / "conflicts.csv",
            "build": self.report / "build.json",
            "progress": self.report / "progress.json",
            "log": self.report / "run.log",
            "pid": self.report / "pid.txt",
            "ramp": self.report / "ramp.json",
            "blocks": self.report / "blocks.jsonl"}
        aq._old_queue_total = lambda: 0
        aq._setup_logging = lambda p: None

        self.urls, routes, sizes = [], {}, {}
        for i in range(6):
            sym = f"E{i}USDT"
            url = (f"{B}/data/spot/monthly/klines/{sym}/1h/{sym}-1h-2024-01.zip")
            z = _tiny_zip(sym)
            routes[url] = z
            routes[url + ".CHECKSUM"] = ad.sha256_hex(z).encode() + b"  x.zip\n"
            self.urls.append(url)
            sizes[url] = len(z)
        self.fake = _SharedFetcher(routes)
        ad.RequestsFetcher = lambda *a, **k: self.fake

        rows = []
        for url in self.urls:
            e, status, _ = aq.normalize_entry(
                {"source_url": url, "remote_size_bytes": str(sizes[url])})
            self.assertEqual(status, "ok")
            rows.append({f: getattr(e, f) for f in ad.FIELDS})
        write_part(self.report / "queue.csv", rows)

    def tearDown(self):
        (aq.OUT_DIR, aq.paths_for, aq._old_queue_total, aq._setup_logging,
         ad.RequestsFetcher) = self._saved
        self.tmp.cleanup()

    def _run(self, **kw):
        base = dict(chunk=3, max_minutes=0, min_free_gib=0.0, interval_sec=0.0,
                    version="vtest")
        base.update(kw)
        return aq.run_queue(**base)

    def test_stop_then_resume_reuses_completed(self):
        self.assertEqual(self._run(max_chunks=1, ramp="off", workers=2), 0)
        self.assertEqual(len(self.fake.calls), 6, "3 份 × (ZIP+CHECKSUM)")

        prog = json.loads((self.report / "progress.json").read_text(encoding="utf-8"))
        self.assertEqual(prog["completed"], 3)

        # 放停止哨兵 → 下一轮必须在开块前就保存退出
        aq.stop_path(self.out).write_text("x", encoding="utf-8")
        before = len(self.fake.calls)
        self.assertEqual(self._run(max_chunks=2, ramp="off", workers=2), 0)
        self.assertEqual(len(self.fake.calls), before, "命中哨兵后不得再发请求")
        self.assertFalse(aq.stop_path(self.out).exists(), "哨兵应被消费")
        self.assertFalse((self.out / ad.LOCK_NAME).exists(), "退出必须释放锁")

        # 续传：剩下的 3 份应被下完；已完成的 3 份**不重下**
        self.assertEqual(self._run(max_chunks=5, ramp="off", workers=2), 0)
        prog = json.loads((self.report / "progress.json").read_text(encoding="utf-8"))
        self.assertEqual(prog["completed"], 6)
        self.assertEqual(prog["run_new_files"], 3, "第二轮只应下缺的 3 份")
        # 第一轮的 3 个 URL 在第二轮不得被再次请求
        second_half = self.fake.calls[6:]
        for u in self.urls[:3]:
            self.assertNotIn(u, second_half, "已完成的文件不得重下")

    def test_keyboard_interrupt_releases_lock_and_keeps_progress(self):
        """模拟 Ctrl+C：中断发生在**下载途中**，锁必须释放、已落盘进度不丢。"""
        real = ad.RequestsFetcher
        shared = self.fake
        seen = {"n": 0}

        class _Boom:
            def get(self, url, timeout=60.0, max_bytes=None):
                seen["n"] += 1
                if seen["n"] > 3:            # 第 2 份文件开始前被中断
                    raise KeyboardInterrupt()
                return shared.get(url, timeout=timeout, max_bytes=max_bytes)

        ad.RequestsFetcher = lambda *a, **k: _Boom()
        try:
            rc = self._run(max_chunks=3, ramp="off", workers=1)
        finally:
            ad.RequestsFetcher = real
        self.assertEqual(rc, 0, "中断必须被吞掉并正常返回，不能抛出")
        self.assertFalse((self.out / ad.LOCK_NAME).exists(), "中断后必须释放锁")
        st = ad.State(self.out / ad.STATE_NAME)
        self.assertGreaterEqual(len(st.records), 1, "已落盘的进度不得丢失")


def _tiny_zip(sym: str) -> bytes:
    """构造一个结构合法的小归档（12 列 CSV）。"""
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(zipfile.ZipInfo(f"{sym}-1h-2024-01.csv", date_time=(2024, 1, 1, 0, 0, 0)),
                   "open_time,open,high,low,close,volume,close_time,quote_volume,"
                   "trades,taker_buy_base,taker_buy_quote,ignore\n"
                   "1704067200000,1,2,0.5,1.5,10,0,10,1,5,5,0\n")
    return buf.getvalue()


class _SharedFetcher:
    """假取数层：路由到本地字节；记录每一次请求。"""

    def __init__(self, routes: dict):
        self.routes = dict(routes)
        self.calls: list[str] = []

    def get(self, url, timeout=60.0, max_bytes=None):
        self.calls.append(url)
        if url not in self.routes:
            raise ad.NotFound(f"HTTP 404: {url}")
        content = bytes(self.routes[url])
        if max_bytes is not None and len(content) > max_bytes:
            raise ad.ByteBudgetExceeded("over budget")
        return ad.Resp(200, {}, content)


if __name__ == "__main__":
    unittest.main(verbosity=2)