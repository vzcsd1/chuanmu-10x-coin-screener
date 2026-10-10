#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""离线测试：tools/archive_download_ds.py

全部测试用注入式假取数器，**不联网**；不用真实限流实验制造封禁。
每个场景有独立预期（不是把实现复制成答案）。

运行：
  py -3.10 -m unittest discover -s tests -p "test_archive_download_ds.py" -v
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import archive_download_ds as ad  # noqa: E402


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------
FIXED_ZIP_TIME = (2024, 1, 1, 0, 0, 0)


def make_zip(member: str = "SYM-1h-2024-01.csv", rows: int = 3,
             header: bool = True, pad: int = 0) -> bytes:
    """构造一个结构合法的归档（12 列、毫秒时间戳）。pad 用于把体积撑大。

    固定成员时间戳，保证**同一参数两次调用字节完全相同**——否则 zip 头里的
    当前时间会让摘要每次都不一样，校验类测试无法比对。
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        lines = []
        if header:
            lines.append("open_time,open,high,low,close,volume,close_time,"
                         "quote_volume,trades,taker_buy_base,taker_buy_quote,ignore")
        for i in range(rows):
            lines.append(f"{1704067200000 + i * 3600000},1,2,0.5,1.5,10,0,10,1,5,5,0")
        z.writestr(zipfile.ZipInfo(member, date_time=FIXED_ZIP_TIME),
                   "\n".join(lines) + "\n")
        if pad:
            # ZIP_STORED：不压缩，体积可预期（否则 "xxxx" 会被压到几十字节）
            z.writestr(zipfile.ZipInfo("padding.bin", date_time=FIXED_ZIP_TIME),
                       b"x" * pad, compress_type=zipfile.ZIP_STORED)
    return buf.getvalue()


class FakeFetcher:
    """按 URL 路由返回内容；记录每一次调用，便于断言"发了几次请求"。"""

    def __init__(self, routes: dict[str, object]):
        self.routes = dict(routes)
        self.calls: list[str] = []

    def get(self, url: str, *, timeout: float = 60.0, max_bytes: int | None = None):
        self.calls.append(url)
        if url not in self.routes:
            raise ad.NotFound(f"HTTP 404: {url}")
        value = self.routes[url]
        if callable(value) and not isinstance(value, (bytes, bytearray)):
            value = value()
        if isinstance(value, Exception):
            raise value
        content = bytes(value)  # type: ignore[arg-type]
        if max_bytes is not None and len(content) > max_bytes:
            raise ad.ByteBudgetExceeded(f"超过字节预算（{len(content)} > {max_bytes}）")
        return ad.Resp(200, {}, content)


def entry(url: str, market: str = "futures", dataset: str = "metrics",
          symbol: str = "ABCUSDT", interval: str = "", period: str = "2024-01-01",
          checksum_url: str = "", size: int | None = None) -> ad.Entry:
    return ad.Entry(source_url=url, market=market, dataset=dataset, symbol=symbol,
                    interval=interval, period_start=period, period_end=period,
                    remote_size_bytes=size, checksum_url=checksum_url,
                    discovered_at_utc="2026-10-06T00:00:00Z")


def opts(tmp: Path, **kw) -> ad.Options:
    base = dict(out_dir=tmp / "out", state_path=tmp / "out" / "_state.json",
                status_csv=None, max_files=0, max_bytes=0, max_minutes=0,
                min_free_gib=0, interval_sec=0, timeout=5, retries=0)
    base.update(kw)
    return ad.Options(**base)


# ---------------------------------------------------------------------------
# 任务书点名的 7 个场景
# ---------------------------------------------------------------------------
class TestRequiredScenarios(unittest.TestCase):
    def test_continues_to_next_batch_after_previous_complete(self):
        """前批完成后，下一批必须真的前进（不是反复跳过原来的前 N 个）。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            urls = [f"https://example.com/f{i}.zip" for i in range(5)]
            ents = [entry(u, symbol=f"S{i}USDT") for i, u in enumerate(urls)]
            routes = {u: make_zip() for u in urls}

            f1 = FakeFetcher(routes)
            r1, _ = ad.run_fetch(ents, opts(tmp, max_files=2), fetcher=f1, sleeper=lambda s: None)
            self.assertEqual(f1.calls, urls[:2], "第一批应只下前两个")
            self.assertEqual([r["status"] for r in r1[:2]], [ad.S_SUCCESS] * 2)

            f2 = FakeFetcher(routes)
            r2, _ = ad.run_fetch(ents, opts(tmp, max_files=2), fetcher=f2, sleeper=lambda s: None)
            self.assertEqual(f2.calls, urls[2:4], "第二批必须换成第 3、4 个，而不是重下前两个")
            done = [r for r in r2 if r["status"] == ad.S_COMPLETE]
            self.assertEqual(len(done), 2, "前两个应被识别为本地完整")

            f3 = FakeFetcher(routes)
            r3, _ = ad.run_fetch(ents, opts(tmp, max_files=2), fetcher=f3, sleeper=lambda s: None)
            self.assertEqual(f3.calls, urls[4:], "第三批只剩最后一个")
            self.assertEqual(len([r for r in r3 if r["status"] == ad.S_SUCCESS]), 1)

    def test_resume_after_interrupt(self):
        """中断后重启，只补剩下的；已完成的不得重下，锁必须释放。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            urls = [f"https://example.com/r{i}.zip" for i in range(3)]
            ents = [entry(u, symbol=f"R{i}USDT") for i, u in enumerate(urls)]

            def boom():
                raise KeyboardInterrupt("模拟用户中断")

            f1 = FakeFetcher({urls[0]: make_zip(), urls[1]: make_zip(), urls[2]: boom})
            with self.assertRaises(KeyboardInterrupt):
                ad.run_fetch(ents, opts(tmp), fetcher=f1, sleeper=lambda s: None)
            self.assertFalse((tmp / "out" / ad.LOCK_NAME).exists(), "异常退出也必须释放锁")

            f2 = FakeFetcher({u: make_zip() for u in urls})
            r2, _ = ad.run_fetch(ents, opts(tmp), fetcher=f2, sleeper=lambda s: None)
            self.assertEqual(f2.calls, urls[2:], "重启只应补第 3 个")
            self.assertEqual(len([r for r in r2 if r["status"] == ad.S_COMPLETE]), 2)

    def test_corrupt_file_cannot_complete(self):
        """损坏文件不能算完成；下次仍会重试。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            url = "https://example.com/bad.zip"
            e = entry(url)
            f1 = FakeFetcher({url: b"this is not a zip at all"})
            r1, _ = ad.run_fetch([e], opts(tmp), fetcher=f1, sleeper=lambda s: None)
            self.assertEqual(r1[0]["status"], ad.S_ANOMALY)
            target = tmp / "out" / e.relpath()
            self.assertFalse(target.exists(), "异常内容不得落到正式路径")
            self.assertTrue(target.with_name(target.name + ".part").exists(),
                            "原始字节应留 .part 供排查")

            f2 = FakeFetcher({url: b"still broken"})
            r2, _ = ad.run_fetch([e], opts(tmp), fetcher=f2, sleeper=lambda s: None)
            self.assertEqual(f2.calls, [url], "未完成项必须重试")
            self.assertEqual(r2[0]["status"], ad.S_ANOMALY)

    def test_no_requests_after_ratelimit(self):
        """触发限流后本次不得再发任何请求，并登记暂停时间。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            urls = [f"https://example.com/q{i}.zip" for i in range(3)]
            ents = [entry(u, symbol=f"Q{i}USDT") for i, u in enumerate(urls)]
            until = ad.now_ms() + 3600_000
            f = FakeFetcher({urls[0]: ad.RateLimited(429, until, "HTTP 429")})
            r, _ = ad.run_fetch(ents, opts(tmp), fetcher=f, sleeper=lambda s: None)
            self.assertEqual(f.calls, urls[:1], "限流后必须立刻停手")
            self.assertEqual(r[0]["status"], ad.S_PAUSED)
            self.assertEqual([x["status"] for x in r[1:]], [ad.S_PAUSED] * 2)
            st = ad.State(tmp / "out" / "_state.json")
            self.assertEqual(st.paused_until("example.com"), until)

    def test_byte_cap(self):
        """总字节上限：超预算如实记录未完成，不把片段当完成。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            big = make_zip(pad=4000)
            url = "https://example.com/big.zip"
            e = entry(url, symbol="BIGUSDT")
            f = FakeFetcher({url: big})
            r, _ = ad.run_fetch([e], opts(tmp, max_bytes=1000), fetcher=f, sleeper=lambda s: None)
            self.assertEqual(r[0]["status"], ad.S_BUDGET)
            self.assertFalse((tmp / "out" / e.relpath()).exists())

            # 第二个文件超出剩余额度 → 也应停
            u1, u2 = "https://example.com/c1.zip", "https://example.com/c2.zip"
            e1, e2 = entry(u1, symbol="C1USDT"), entry(u2, symbol="C2USDT")
            blob = make_zip(pad=1500)
            f2 = FakeFetcher({u1: blob, u2: blob})
            r2, _ = ad.run_fetch([e1, e2], opts(tmp, max_bytes=len(blob) + 10),
                                 fetcher=f2, sleeper=lambda s: None)
            self.assertEqual(r2[0]["status"], ad.S_SUCCESS)
            self.assertEqual(r2[1]["status"], ad.S_BUDGET)

    def test_restart_preserves_pause(self):
        """暂停状态跨进程重启保留：重启后一个请求都不发。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            url = "https://example.com/p.zip"
            e = entry(url, symbol="PUSDT")
            f1 = FakeFetcher({url: ad.RateLimited(418, ad.now_ms() + 3600_000, "HTTP 418")})
            ad.run_fetch([e], opts(tmp), fetcher=f1, sleeper=lambda s: None)

            f2 = FakeFetcher({url: make_zip()})
            r2, msg = ad.run_fetch([e], opts(tmp), fetcher=f2, sleeper=lambda s: None)
            self.assertEqual(f2.calls, [], "暂停期内不得发请求")
            self.assertEqual(r2[0]["status"], ad.S_PAUSED)
            self.assertIn("暂停", msg)

    def test_same_symbol_different_market_no_collision(self):
        """不同市场的同名币不得互相覆盖。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            a = entry("https://example.com/a.zip", market="spot", dataset="klines",
                      symbol="ABCUSDT", interval="1h")
            b = entry("https://example.com/b.zip", market="futures", dataset="klines",
                      symbol="ABCUSDT", interval="1h")
            self.assertNotEqual(a.relpath(), b.relpath())
            f = FakeFetcher({a.source_url: make_zip("ABCUSDT-1h-2024-01.csv"),
                             b.source_url: make_zip("ABCUSDT-1h-2024-01.csv")})
            r, _ = ad.run_fetch([a, b], opts(tmp), fetcher=f, sleeper=lambda s: None)
            self.assertEqual([x["status"] for x in r], [ad.S_SUCCESS, ad.S_SUCCESS])
            self.assertTrue((tmp / "out" / a.relpath()).exists())
            self.assertTrue((tmp / "out" / b.relpath()).exists())


# ---------------------------------------------------------------------------
# 额外边界
# ---------------------------------------------------------------------------
class TestGuardsAndChecks(unittest.TestCase):
    def test_zip_slip_rejected(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("../../evil.csv", "open_time,open\n1,2\n")
        ok, why = ad.inspect_content(entry("https://example.com/x.zip"), buf.getvalue())
        self.assertFalse(ok)
        self.assertIn("上跳", why)

    def test_checksum_mismatch_is_anomaly(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            url, ck = "https://example.com/s.zip", "https://example.com/s.zip.CHECKSUM"
            e = entry(url, symbol="SUSDT", checksum_url=ck)
            f = FakeFetcher({url: make_zip(), ck: ("0" * 64 + "  s.zip\n").encode()})
            r, _ = ad.run_fetch([e], opts(tmp), fetcher=f, sleeper=lambda s: None)
            self.assertEqual(r[0]["status"], ad.S_ANOMALY)
            self.assertEqual(r[0]["checksum_status"], ad.CHECK_MISMATCH)

    def test_checksum_verified_when_matching(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            blob = make_zip()
            url, ck = "https://example.com/v.zip", "https://example.com/v.zip.CHECKSUM"
            e = entry(url, symbol="VUSDT", checksum_url=ck)
            f = FakeFetcher({url: blob, ck: f"{ad.sha256_hex(blob)}  v.zip\n".encode()})
            r, _ = ad.run_fetch([e], opts(tmp), fetcher=f, sleeper=lambda s: None)
            self.assertEqual(r[0]["status"], ad.S_SUCCESS)
            self.assertEqual(r[0]["checksum_status"], ad.CHECK_VERIFIED)

    def test_no_checksum_source_not_claimed_verified(self):
        """来源没提供校验值时，不得声称已验证来源校验值。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            url = "https://example.com/n.zip"
            e = entry(url, symbol="NUSDT", checksum_url="")
            f = FakeFetcher({url: make_zip()})
            r, _ = ad.run_fetch([e], opts(tmp), fetcher=f, sleeper=lambda s: None)
            self.assertEqual(r[0]["status"], ad.S_SUCCESS)
            self.assertEqual(r[0]["checksum_status"], ad.CHECK_NO_SOURCE)
            self.assertIn("来源未提供校验值文件", r[0]["note"])

    def test_not_found_recorded_separately(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            e = entry("https://example.com/missing.zip", symbol="MUSDT")
            f = FakeFetcher({})
            r, _ = ad.run_fetch([e], opts(tmp), fetcher=f, sleeper=lambda s: None)
            self.assertEqual(r[0]["status"], ad.S_NOT_FOUND)
            self.assertIn("本次未找到", r[0]["note"])

    def test_transient_error_retried_then_recorded(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            url = "https://example.com/t.zip"
            e = entry(url, symbol="TUSDT")
            f = FakeFetcher({url: ad.FetchError("connection reset")})
            r, _ = ad.run_fetch([e], opts(tmp, retries=2), fetcher=f, sleeper=lambda s: None)
            self.assertEqual(len(f.calls), 3, "1 次 + 2 次重试")
            self.assertEqual(r[0]["status"], ad.S_TRANSIENT)

    def test_plan_mode_makes_no_requests(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            e = entry("https://example.com/plan.zip", symbol="PLUSDT")
            f = FakeFetcher({e.source_url: make_zip()})
            rows, msg = ad.run_fetch([e], opts(tmp, dry_run=True), fetcher=f, sleeper=lambda s: None)
            self.assertEqual(f.calls, [], "plan/dry-run 必须零请求")
            self.assertEqual(rows[0]["status"], ad.S_PENDING)
            self.assertIn("未发任何请求", msg)

    def test_concurrent_run_refused(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            out = tmp / "out"
            out.mkdir(parents=True, exist_ok=True)
            (out / ad.LOCK_NAME).write_text("{}", encoding="utf-8")
            e = entry("https://example.com/lock.zip", symbol="LUSDT")
            f = FakeFetcher({e.source_url: make_zip()})
            r, msg = ad.run_fetch([e], opts(tmp), fetcher=f, sleeper=lambda s: None)
            self.assertEqual(r, [])
            self.assertIn("另一份下载进程", msg)
            self.assertEqual(f.calls, [])

    def test_min_free_space_refuses(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            e = entry("https://example.com/free.zip", symbol="FUSDT")
            f = FakeFetcher({e.source_url: make_zip()})
            r, msg = ad.run_fetch([e], opts(tmp, min_free_gib=10 ** 6),
                                  fetcher=f, sleeper=lambda s: None)
            self.assertEqual(r, [])
            self.assertIn("未开始下载", msg)
            self.assertEqual(f.calls, [])

    def test_force_keeps_old_version(self):
        """同路径内容变化时保留旧原始资料，不静默覆盖。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            url = "https://example.com/ver.zip"
            e = entry(url, symbol="VERUSDT")
            ad.run_fetch([e], opts(tmp), fetcher=FakeFetcher({url: make_zip(pad=10)}),
                         sleeper=lambda s: None)
            ad.run_fetch([e], opts(tmp, force=True), fetcher=FakeFetcher({url: make_zip(pad=20)}),
                         sleeper=lambda s: None)
            target = tmp / "out" / e.relpath()
            stale = list(target.parent.glob(target.name + ".stale-*"))
            self.assertTrue(target.exists())
            self.assertEqual(len(stale), 1, "旧版本必须改名留存")

    def test_download_records_checksum_digest_in_state(self):
        """下载成功时摘要必须写进状态文件，否则累计状态表会丢校验信息。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            blob = make_zip()
            url, ck = "https://example.com/dg.zip", "https://example.com/dg.zip.CHECKSUM"
            e = entry(url, symbol="DGUSDT", checksum_url=ck)
            f = FakeFetcher({url: blob, ck: f"{ad.sha256_hex(blob)}  dg.zip\n".encode()})
            ad.run_fetch([e], opts(tmp), fetcher=f, sleeper=lambda s: None)
            st = ad.State(tmp / "out" / "_state.json")
            rec = st.records[e.key]
            self.assertEqual(rec["checksum_sha256"], ad.sha256_hex(blob))
            self.assertEqual(rec["checksum_status"], ad.CHECK_VERIFIED)
            self.assertEqual(rec["fetched_at_utc"] != "", True)

    def test_verify_backfills_checksum_without_download(self):
        """verify 只取 CHECKSUM，不重新下载归档；并补齐状态里的摘要。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            blob = make_zip()
            url, ck = "https://example.com/vf.zip", "https://example.com/vf.zip.CHECKSUM"
            e = entry(url, symbol="VFUSDT", checksum_url=ck)
            ad.run_fetch([e], opts(tmp), fetcher=FakeFetcher({url: blob}),
                         sleeper=lambda s: None)
            f = FakeFetcher({url: blob, ck: f"{ad.sha256_hex(blob)}  vf.zip\n".encode()})
            rows = ad.run_verify([e], opts(tmp), fetcher=f, sleeper=lambda s: None)
            self.assertEqual(f.calls, [ck], "复核不得重新下载归档")
            self.assertEqual(rows[0]["checksum_status"], ad.CHECK_VERIFIED)
            self.assertEqual(rows[0]["checksum_sha256"], ad.sha256_hex(blob))
            st = ad.State(tmp / "out" / "_state.json")
            self.assertEqual(st.records[e.key]["checksum_sha256"], ad.sha256_hex(blob))

    def test_accumulated_status_keeps_digest_after_skip_run(self):
        """后一轮 skipped_complete 不得抹掉前一轮的摘要与获取时间。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            blob = make_zip()
            url, ck = "https://example.com/ac.zip", "https://example.com/ac.zip.CHECKSUM"
            e = entry(url, symbol="ACUSDT", checksum_url=ck)
            routes = {url: blob, ck: f"{ad.sha256_hex(blob)}  ac.zip\n".encode()}
            ad.run_fetch([e], opts(tmp), fetcher=FakeFetcher(routes), sleeper=lambda s: None)
            ad.run_fetch([e], opts(tmp), fetcher=FakeFetcher(routes), sleeper=lambda s: None)
            st = ad.State(tmp / "out" / "_state.json")
            rows = ad.status_rows_from_state([e], st)
            self.assertEqual(rows[0]["checksum_sha256"], ad.sha256_hex(blob))
            self.assertEqual(rows[0]["checksum_status"], ad.CHECK_VERIFIED)

    def test_manifest_roundtrip_and_fields(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            mf = tmp / "m.csv"
            mf.write_text(
                "source_url,market,dataset,symbol,interval,period_start,period_end,"
                "remote_size_bytes,checksum_url,discovered_at_utc\n"
                "https://example.com/z.zip,spot,klines,ADAUSDT,1h,2024-01,2024-01,"
                "1234,https://example.com/z.zip.CHECKSUM,2026-10-06T00:00:00Z\n",
                encoding="utf-8")
            ents = ad.load_manifest(mf)
            self.assertEqual(len(ents), 1)
            self.assertEqual(ents[0].remote_size_bytes, 1234)
            self.assertEqual(ents[0].checksum_url, "https://example.com/z.zip.CHECKSUM")
            self.assertEqual(list(ad.FIELDS),
                             ["source_url", "market", "dataset", "symbol", "interval",
                              "period_start", "period_end", "remote_size_bytes",
                              "checksum_url", "discovered_at_utc"])

    def test_safe_seg_blocks_traversal(self):
        self.assertEqual(ad.safe_seg("../../etc/passwd"), "passwd")
        self.assertEqual(ad.safe_seg("C:\\Windows\\x"), "x")
        self.assertEqual(ad.safe_seg(".."), "_")
        self.assertEqual(ad.safe_seg(""), "_")

    def test_state_survives_corrupt_file(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            p = tmp / "s.json"
            p.write_text("{ not json", encoding="utf-8")
            st = ad.State(p)
            self.assertEqual(st.paused, {})
            self.assertEqual(st.records, {})


class TestLongTaskBlockers(unittest.TestCase):
    """长任务关键路径：主代理审核报告点名的 7 类问题，逐条独立预期。"""

    # —— ① 校验端点限流必须持久暂停、并与数据端点共用 ——
    def test_checksum_endpoint_ratelimit_pauses_and_stops(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            u1, u2 = "https://example.com/a1.zip", "https://example.com/a2.zip"
            c1 = u1 + ".CHECKSUM"
            e1 = entry(u1, symbol="A1USDT", checksum_url=c1)
            e2 = entry(u2, symbol="A2USDT", checksum_url=u2 + ".CHECKSUM")
            f = FakeFetcher({u1: make_zip(),
                             c1: ad.RateLimited(429, ad.now_ms() + 3600_000, "HTTP 429")})
            r, _ = ad.run_fetch([e1, e2], opts(tmp), fetcher=f, sleeper=lambda s: None)
            self.assertEqual(r[0]["status"], ad.S_PAUSED)
            self.assertEqual(r[1]["status"], ad.S_PAUSED)
            self.assertEqual(f.calls, [u1, c1], "校验端点限流后不得再发任何请求")
            st = ad.State(tmp / "out" / "_state.json")
            self.assertTrue(st.is_paused("example.com"), "限流必须持久化到状态")
            self.assertFalse((tmp / "out" / e1.relpath()).exists(),
                             "校验未确认前不得落正式路径")

    def test_checksum_ratelimit_pause_survives_restart(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            u = "https://example.com/r.zip"
            e = entry(u, symbol="RUSDT", checksum_url=u + ".CHECKSUM")
            ad.run_fetch([e], opts(tmp),
                         fetcher=FakeFetcher({u: make_zip(),
                                              u + ".CHECKSUM": ad.RateLimited(
                                                  418, ad.now_ms() + 3600_000, "418")}),
                         sleeper=lambda s: None)
            f2 = FakeFetcher({u: make_zip(),
                              u + ".CHECKSUM": b"0" * 64 + b"  r.zip\n"})
            r2, _ = ad.run_fetch([e], opts(tmp), fetcher=f2, sleeper=lambda s: None)
            self.assertEqual(f2.calls, [], "重启后暂停期内不得发请求")
            self.assertEqual(r2[0]["status"], ad.S_PAUSED)

    # —— ② 校验获取失败 ≠ 来源无校验，且不算完成 ——
    def test_checksum_fetch_failure_is_not_no_source_and_not_complete(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            blob = make_zip()
            u = "https://example.com/b.zip"
            c = u + ".CHECKSUM"
            e = entry(u, symbol="BUSDT", checksum_url=c)
            f = FakeFetcher({u: blob, c: ad.FetchError("connection reset")})
            r, _ = ad.run_fetch([e], opts(tmp), fetcher=f, sleeper=lambda s: None)
            self.assertEqual(r[0]["checksum_status"], ad.CHECK_FETCH_FAILED)
            self.assertNotEqual(r[0]["checksum_status"], ad.CHECK_NO_SOURCE,
                                "取不到校验值不等于来源没有校验值")
            self.assertEqual(r[0]["status"], ad.S_UNVERIFIED, "临时未验证不得算完成")
            self.assertTrue((tmp / "out" / e.relpath()).exists(), "内容有效的归档仍应保存")

            # 下一轮只补校验，不重下归档
            f2 = FakeFetcher({u: blob, c: f"{ad.sha256_hex(blob)}  b.zip\n".encode()})
            r2, _ = ad.run_fetch([e], opts(tmp), fetcher=f2, sleeper=lambda s: None)
            self.assertEqual(f2.calls, [c], "只补校验值，不得重新下载归档")
            self.assertEqual(r2[0]["status"], ad.S_COMPLETE)
            self.assertEqual(r2[0]["checksum_status"], ad.CHECK_VERIFIED)

    def test_404_checksum_is_no_source(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            u = "https://example.com/n404.zip"
            e = entry(u, symbol="N4USDT", checksum_url=u + ".CHECKSUM")
            f = FakeFetcher({u: make_zip()})  # CHECKSUM 未路由 → 404
            r, _ = ad.run_fetch([e], opts(tmp), fetcher=f, sleeper=lambda s: None)
            self.assertEqual(r[0]["checksum_status"], ad.CHECK_NO_SOURCE)
            self.assertEqual(r[0]["status"], ad.S_SUCCESS)

    # —— ③ 校验不符不得完成 ——
    def test_mismatch_never_completes(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            u = "https://example.com/mm.zip"
            e = entry(u, symbol="MMUSDT", checksum_url=u + ".CHECKSUM")
            f = FakeFetcher({u: make_zip(), u + ".CHECKSUM": b"0" * 64 + b"  mm.zip\n"})
            r, _ = ad.run_fetch([e], opts(tmp), fetcher=f, sleeper=lambda s: None)
            self.assertEqual(r[0]["status"], ad.S_ANOMALY)
            self.assertEqual(r[0]["checksum_status"], ad.CHECK_MISMATCH)
            self.assertFalse((tmp / "out" / e.relpath()).exists())
            st = ad.State(tmp / "out" / "_state.json")
            self.assertNotEqual((st.records.get(e.key) or {}).get("status"), ad.S_COMPLETE)

    # —— ④ 已存来源摘要用于本地复核（不解压、不联网） ——
    def test_saved_digest_local_check(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            blob = make_zip()
            p = tmp / "f.zip"
            p.write_bytes(blob)
            e = entry("https://example.com/f.zip", symbol="FUSDT")
            ok, why = ad.verify_local(p, e, ad.sha256_hex(blob))
            self.assertTrue(ok, why)
            self.assertIn("摘要", why)
            bad, why_bad = ad.verify_local(p, e, "0" * 64)
            self.assertFalse(bad)
            self.assertIn("摘要", why_bad)
            p.write_bytes(blob[:-5] + b"XXXXX")     # 篡改
            bad2, _ = ad.verify_local(p, e, ad.sha256_hex(blob))
            self.assertFalse(bad2, "被篡改的本地文件不得算完成")

    # —— ⑤ 锁：活进程不被抢占；心跳过期才可接管；不删他人锁 ——
    def test_lock_live_owner_not_taken_over(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out"
            out.mkdir(parents=True)
            (out / ad.LOCK_NAME).write_text(
                json.dumps({"token": "other", "pid": 999999,
                            "heartbeat_at": time.time()}), encoding="utf-8")
            lk = ad.DownloadLock(out)
            ok, msg = lk.acquire()
            self.assertFalse(ok, "活进程的锁不得被抢占")
            self.assertIn("另一份下载进程", msg)

    def test_lock_stale_owner_can_be_taken_over(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out"
            out.mkdir(parents=True)
            (out / ad.LOCK_NAME).write_text(
                json.dumps({"token": "dead", "pid": 999999,
                            "heartbeat_at": time.time() - 10_000}), encoding="utf-8")
            lk = ad.DownloadLock(out, stale_sec=300.0)
            ok, _ = lk.acquire()
            self.assertTrue(ok, "心跳过期的残留锁应可接管")

    def test_lock_release_keeps_other_token(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out"
            lk = ad.DownloadLock(out)
            self.assertTrue(lk.acquire()[0])
            (out / ad.LOCK_NAME).write_text(
                json.dumps({"token": "other", "heartbeat_at": time.time()}),
                encoding="utf-8")
            lk.release()
            self.assertTrue((out / ad.LOCK_NAME).exists(), "不得删掉别人的锁")

    def test_long_run_heartbeat_keeps_lock_fresh(self):
        """模拟长任务：反复 touch 后，锁始终不被判为过期。"""
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out"
            lk = ad.DownloadLock(out, stale_sec=0.3, refresh_sec=0.0)
            self.assertTrue(lk.acquire()[0])
            for _ in range(4):
                time.sleep(0.15)
                lk.touch(force=True)
                self.assertTrue(lk.owner_alive(), "活着的持有者不应被判过期")

    # —— ⑥ 状态增量保存，不重写整库 ——
    def test_state_is_incremental_not_rewrite(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            st = ad.State(tmp / "s.json", compact_every=10 ** 9)
            for i in range(40):
                st.set_record(entry(f"https://e/{i}.zip", symbol=f"S{i}USDT"),
                              status="x", bytes=i)
            self.assertFalse((tmp / "s.json").exists(), "未到阈值不得写整库快照")
            lines = (tmp / "s.journal.jsonl").read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 40, "每份记录只应追加一行")
            st2 = ad.State(tmp / "s.json", compact_every=10 ** 9)
            self.assertEqual(len(st2.records), 40, "重放日志应还原全部记录")
            st2.save(force=True)
            self.assertFalse((tmp / "s.journal.jsonl").exists())
            self.assertEqual(
                len(json.loads((tmp / "s.json").read_text(encoding="utf-8"))["records"]), 40)

    def test_state_pause_survives_journal_only(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            st = ad.State(tmp / "s.json", compact_every=10 ** 9)
            st.set_pause("h.example", ad.now_ms() + 60_000, "test")
            st2 = ad.State(tmp / "s.json", compact_every=10 ** 9)
            self.assertTrue(st2.is_paused("h.example"))

    # —— ⑦ 中断 / 字节预算 / 恢复 ——
    def test_interrupt_keeps_progress_and_releases_lock(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            urls = [f"https://example.com/i{k}.zip" for k in range(3)]
            ents = [entry(u, symbol=f"I{k}USDT") for k, u in enumerate(urls)]

            def boom():
                raise KeyboardInterrupt("模拟中断")

            f1 = FakeFetcher({urls[0]: make_zip(), urls[1]: make_zip(), urls[2]: boom})
            with self.assertRaises(KeyboardInterrupt):
                ad.run_fetch(ents, opts(tmp), fetcher=f1, sleeper=lambda s: None)
            self.assertFalse((tmp / "out" / ad.LOCK_NAME).exists())
            f2 = FakeFetcher({u: make_zip() for u in urls})
            r2, _ = ad.run_fetch(ents, opts(tmp), fetcher=f2, sleeper=lambda s: None)
            self.assertEqual(f2.calls, urls[2:], "只补未完成的那一份")

    def test_byte_budget_stops_but_keeps_records(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            blob = make_zip(pad=2000)
            urls = [f"https://example.com/z{k}.zip" for k in range(3)]
            ents = [entry(u, symbol=f"Z{k}USDT") for k, u in enumerate(urls)]
            f = FakeFetcher({u: blob for u in urls})
            r, _ = ad.run_fetch(ents, opts(tmp, max_bytes=len(blob) + 10),
                                fetcher=f, sleeper=lambda s: None)
            self.assertEqual(len(r), 3, "总数必须守恒")
            self.assertEqual(r[0]["status"], ad.S_SUCCESS)
            self.assertEqual(r[1]["status"], ad.S_BUDGET)
            self.assertEqual(r[2]["status"], ad.S_BUDGET)


class TestSafeSegIsInjective(unittest.TestCase):
    """路径片段必须单射，否则不同币种会写进同一文件互相覆盖。

    实测来源里真有中文币种目录（如 `牛来USDT`、`龙虾USDT`、`币安人生USDT`）。
    """

    def test_non_ascii_symbols_do_not_collide(self):
        syms = ["牛来USDT", "龙虾USDT", "币安人生USDT", "我踏马来了USDT",
                "哈基米USDT", "这是测试币456", "ADAUSDT"]
        segs = [ad.safe_seg(s) for s in syms]
        self.assertEqual(len(set(segs)), len(segs),
                         f"路径片段塌缩：{list(zip(syms, segs))}")

    def test_same_length_names_still_differ(self):
        """长度相同的两个中文名（旧实现必撞）必须仍然不同。"""
        self.assertNotEqual(ad.safe_seg("牛来USDT"), ad.safe_seg("龙虾USDT"))

    def test_ascii_unchanged(self):
        self.assertEqual(ad.safe_seg("ADAUSDT"), "ADAUSDT")

    def test_traversal_still_blocked(self):
        for bad in ("../../etc/passwd", "..\\..\\win.ini", "C:\\x\\y"):
            seg = ad.safe_seg(bad)
            self.assertNotIn("..", seg)
            self.assertNotIn("/", seg)
            self.assertNotIn("\\", seg)

    def test_distinct_entries_get_distinct_relpaths(self):
        a = entry("https://data.binance.vision/data/futures/um/monthly/fundingRate/"
                  "牛来USDT/牛来USDT-fundingRate-2026-08.zip", symbol="牛来USDT")
        b = entry("https://data.binance.vision/data/futures/um/monthly/fundingRate/"
                  "龙虾USDT/龙虾USDT-fundingRate-2026-08.zip", symbol="龙虾USDT")
        self.assertNotEqual(a.relpath(), b.relpath())


class TestConcurrency(unittest.TestCase):
    """多路并发正确性：限流、抢写、预算、状态不丢不坏。

    全部用假取数器，不联网；`workers=1` 必须与旧单路行为一致。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = Path(self.tmp.name)
        self.out = self.t / "out"

    def tearDown(self):
        self.tmp.cleanup()

    def _batch(self, n: int, z: bytes):
        es, routes = [], {}
        for i in range(n):
            sym = f"S{i:03d}USDT"
            url = f"https://data.binance.vision/data/spot/monthly/klines/{sym}/1h/{sym}-1h-2024-01.zip"
            es.append(entry(url, market="spot", dataset="klines", symbol=sym,
                            interval="1h", checksum_url=url + ".CHECKSUM"))
            routes[url] = z
            routes[url + ".CHECKSUM"] = ad.sha256_hex(z).encode() + b"  x.zip\n"
        return es, routes

    def test_multi_lane_all_files_land_and_state_complete(self):
        """4 路并发：文件全部落盘、台账一条不少、日志行行可解析（无抢写撕裂）。"""
        z = make_zip()
        es, routes = self._batch(8, z)
        f = FakeFetcher(routes)
        results, _ = ad.run_fetch(es, opts(self.t, workers=4), fetcher=f)
        self.assertEqual(len(results), 8)
        self.assertEqual(sum(1 for r in results if r["status"] == ad.S_SUCCESS), 8)
        for e in es:
            self.assertTrue((self.out / e.relpath()).exists(), e.symbol)
        st = ad.State(self.out / ad.STATE_NAME)
        self.assertEqual(len(st.records), 8)
        for e in es:
            self.assertEqual(st.records[e.key]["checksum_status"], ad.CHECK_VERIFIED)
        # 日志必须行行是合法 JSON（并发追加不得交错）
        jp = self.out / "_state.journal.jsonl"
        if jp.exists():
            for line in jp.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    json.loads(line)

    def test_workers_one_matches_single_lane(self):
        """`workers=1` 与旧单路一致：顺序处理、结果条数守恒。"""
        z = make_zip()
        es, routes = self._batch(3, z)
        results, _ = ad.run_fetch(es, opts(self.t, workers=1),
                                  fetcher=FakeFetcher(routes))
        self.assertEqual([r["key"] for r in results], [e.key for e in es])

    def test_multi_lane_ratelimit_stops_new_requests(self):
        """任一路触发 429/418 → 不再发新请求，且登记持久暂停。"""
        es, _ = self._batch(12, make_zip())
        f = FakeFetcher({e.source_url: ad.RateLimited(429, 0, "HTTP 429")
                         for e in es})
        results, _ = ad.run_fetch(es, opts(self.t, workers=4), fetcher=f)
        self.assertEqual(len(results), 12, "总数必须守恒")
        self.assertTrue(all(r["status"] == ad.S_PAUSED for r in results))
        # 已发出的请求可能最多 4 个（路数上限），绝不该把 12 个都发出去
        self.assertLessEqual(len(f.calls), 4)
        self.assertGreaterEqual(len(f.calls), 1)
        st = ad.State(self.out / ad.STATE_NAME)
        self.assertTrue(st.paused, "必须登记暂停，供重启后继续遵守")

    def test_multi_lane_shares_global_budget(self):
        """预算由所有路共享：超出后剩余项记为预算停止，而不是各算各的。"""
        z = make_zip()
        es, routes = self._batch(10, z)
        results, _ = ad.run_fetch(es, opts(self.t, workers=4, max_files=3),
                                  fetcher=FakeFetcher(routes))
        self.assertEqual(len(results), 10)
        ok = sum(1 for r in results if r["status"] == ad.S_SUCCESS)
        self.assertEqual(ok, 3, "共享预算只允许 3 份成功")
        self.assertEqual(sum(1 for r in results if r["status"] == ad.S_BUDGET), 7)

    def test_duplicate_and_corrupt_state_is_tolerated(self):
        """状态日志有损坏行 / 同一键重复写入 → 能读、后写覆盖先写。"""
        jp = self.out / "_state.journal.jsonl"
        jp.parent.mkdir(parents=True, exist_ok=True)
        jp.write_text(
            json.dumps({"t": "rec", "k": "K1", "v": {"status": "pending"}}) + "\n"
            + "{这不是合法 JSON\n"
            + json.dumps({"t": "rec", "k": "K1",
                          "v": {"status": "success"}}) + "\n",
            encoding="utf-8")
        st = ad.State(self.out / "_state.json")
        self.assertEqual(st.records["K1"]["status"], "success", "后写应覆盖先写")

    def test_interrupt_keeps_progress_and_releases_lock(self):
        """中断/异常退出后：进度保留、锁释放（多路同样成立）。"""
        z = make_zip()
        es, routes = self._batch(4, z)
        calls = {"n": 0}

        def boom():
            calls["n"] += 1
            raise KeyboardInterrupt()

        routes2 = dict(routes)
        routes2[es[2].source_url] = boom
        try:
            ad.run_fetch(es, opts(self.t, workers=2), fetcher=FakeFetcher(routes2))
        except KeyboardInterrupt:
            pass
        self.assertFalse((self.out / ad.LOCK_NAME).exists(), "锁必须被释放")


# ---------------------------------------------------------------------------
# 多路限流交错（任务 ds41f_rate_limit_race 第 4 条）
# 顺序全部由 Event 控制，**不用 sleep 碰运气**；不联网制造封禁。
# ---------------------------------------------------------------------------
RACE_URL = ("https://data.binance.vision/data/futures/um/daily/metrics/"
            "TESTUSDT/TESTUSDT-metrics-2026-01-01.zip")


class TestRateLimitInterleaving(unittest.TestCase):

    # —— ① 暂停检查必须在节奏等待**之后** ——
    def test_pause_registered_during_pace_wait_blocks_request(self):
        """节奏等待期间别的路登记了暂停 → 等完再查必须拦住，请求一个都不能发。"""
        state = ad.State(None)
        host = ad.host_of(RACE_URL)
        sent: list[str] = []

        class Fetcher:
            def get(self, url, *, timeout=60.0, max_bytes=None):
                sent.append(url)
                return ad.Resp(200, {}, b"x")

        def sleeper(_sec):        # 就在"等待节奏"的这一刻，另一路触发了限流
            state.set_pause(host, ad.now_ms() + 60_000, "another worker got 429")

        dl = ad.Downloader(Fetcher(), ad.Options(interval_sec=1.0), sleeper=sleeper)
        dl._last_request_at = time.time()           # 让本次真的进入"等待节奏"  # noqa: SLF001
        with self.assertRaises(ad.Paused):
            dl._get(RACE_URL, state=state)          # noqa: SLF001
        self.assertEqual(sent, [], "暂停检查必须在节奏等待之后，不能先发出去")

    # —— ② 另一工作者在归档返回前限流 → 不得再发校验请求 ——
    def test_other_worker_pause_before_archive_returns_stops_checksum(self):
        """A 的归档请求已发出，B 在它返回前登记暂停：
        A 返回后**不得**再发校验请求，且 B 的暂停**不得**被 A 抹掉。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            o = opts(tmp)
            e = entry(RACE_URL, symbol="TESTUSDT", checksum_url=RACE_URL + ".CHECKSUM")
            state = ad.State(None)
            host = ad.host_of(RACE_URL)
            blob = make_zip()
            archive_sent = threading.Event()
            release_archive = threading.Event()
            calls: list[str] = []

            class Fetcher:
                def get(self, url, *, timeout=60.0, max_bytes=None):
                    calls.append(url)
                    if url.endswith(".CHECKSUM"):
                        return ad.Resp(200, {}, b"")
                    archive_sent.set()
                    release_archive.wait(5)          # 卡在"归档在途"
                    return ad.Resp(200, {}, blob)

            dl = ad.Downloader(Fetcher(), o, sleeper=lambda s: None)
            with ThreadPoolExecutor(max_workers=1) as ex:
                fut = ex.submit(dl.fetch_one, e, ad.Budget(), state)
                self.assertTrue(archive_sent.wait(5), "归档请求应已发出")
                state.set_pause(host, ad.now_ms() + 60_000, "another worker got 429")
                release_archive.set()
                rec = fut.result(timeout=10)

            self.assertEqual(calls, [RACE_URL], "暂停后不得再发校验请求")
            self.assertEqual(rec["status"], ad.S_PAUSED)
            self.assertEqual(rec["checksum_status"], ad.CHECK_FETCH_FAILED)
            self.assertTrue(state.is_paused(host), "别人的未到期暂停不得被这次结果抹掉")
            self.assertFalse((o.out_dir / e.relpath()).exists(),
                             "校验未确认前不得落正式路径")
            pending = ad.pending_path_for(o.out_dir, e)
            self.assertTrue(pending.exists(), "归档字节必须保留为待复核")
            self.assertEqual(pending.read_bytes(), blob)

    # —— ③ 晚到的成功不得清掉别人刚登记的暂停（P1 本体）——
    def test_success_does_not_clear_pause_registered_mid_flight(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            o = opts(tmp)
            blob = make_zip()
            u = "https://example.com/late.zip"
            e = entry(u, symbol="LATEUSDT", checksum_url=u + ".CHECKSUM")
            state = ad.State(None)
            host = ad.host_of(u)

            class Fetcher:
                def get(self, url, *, timeout=60.0, max_bytes=None):
                    if url.endswith(".CHECKSUM"):
                        # 校验请求返回前，另一路登记了**新的未到期**暂停
                        state.set_pause(host, ad.now_ms() + 60_000, "another worker got 429")
                        return ad.Resp(200, {}, ad.sha256_hex(blob).encode() + b"  late.zip\n")
                    return ad.Resp(200, {}, blob)

            dl = ad.Downloader(Fetcher(), o, sleeper=lambda s: None)
            rec = dl.fetch_one(e, ad.Budget(), state)
            self.assertEqual(rec["status"], ad.S_SUCCESS, "本次归档本身是成功的")
            self.assertTrue(state.is_paused(host), "晚到的成功不得解除别人刚登记的暂停")

    # —— ④ 已到期才可解除，解除后请求恢复 ——
    def test_expired_pause_is_released_and_request_resumes(self):
        state = ad.State(None)
        host = "example.com"
        state.set_pause(host, ad.now_ms() + 1, "short pause")
        later = ad.now_ms() + 10_000
        self.assertFalse(state.is_paused(host, now=later), "到期后不再拦截")
        self.assertTrue(state.clear_pause_if_expired(host, now=later), "到期后可解除")
        self.assertFalse(state.is_paused(host, now=later))

        sent: list[str] = []

        class Fetcher:
            def get(self, url, *, timeout=60.0, max_bytes=None):
                sent.append(url)
                return ad.Resp(200, {}, b"x")

        dl = ad.Downloader(Fetcher(), ad.Options(interval_sec=0), sleeper=lambda s: None)
        dl._get("https://example.com/x.zip", state=state)   # noqa: SLF001
        self.assertEqual(sent, ["https://example.com/x.zip"], "到期后请求必须恢复")

    # —— ⑤ 重启后暂停保持，且待复核字节只补校验、不重下整份 ——
    def test_restart_keeps_pause_and_recovers_with_checksum_only(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            o = opts(tmp)
            blob = make_zip()
            u = "https://example.com/r2.zip"
            c = u + ".CHECKSUM"
            e = entry(u, symbol="R2USDT", checksum_url=c)

            f1 = FakeFetcher({u: blob, c: ad.RateLimited(418, ad.now_ms() + 3600_000, "418")})
            ad.run_fetch([e], o, fetcher=f1, sleeper=lambda s: None)
            self.assertEqual(f1.calls, [u, c])
            pending = ad.pending_path_for(o.out_dir, e)
            self.assertTrue(pending.exists(), "归档字节必须保留为待复核")

            # 模拟重启：全新 State 从磁盘读
            st = ad.State(o.state_path)
            self.assertTrue(st.is_paused("example.com"), "暂停必须跨重启保持")

            f2 = FakeFetcher({u: blob, c: ad.sha256_hex(blob).encode() + b"  r2.zip\n"})
            r2, _ = ad.run_fetch([e], o, fetcher=f2, sleeper=lambda s: None)
            self.assertEqual(f2.calls, [], "暂停未到期，重启后仍不得发请求")
            self.assertEqual(r2[0]["status"], ad.S_PAUSED)

            # 暂停到期 → 恢复：**只允许请求 CHECKSUM**，绝不重下整份归档
            ad.State(o.state_path).clear_pause("example.com")
            f3 = FakeFetcher({u: blob, c: ad.sha256_hex(blob).encode() + b"  r2.zip\n"})
            r3, _ = ad.run_fetch([e], o, fetcher=f3, sleeper=lambda s: None)
            self.assertEqual(f3.calls, [c], "恢复后只补校验，不得重下整份归档")
            self.assertEqual(r3[0]["status"], ad.S_COMPLETE)
            self.assertEqual(r3[0]["checksum_status"], ad.CHECK_VERIFIED)
            self.assertFalse(pending.exists(), "校验通过后待复核字节应转正")
            self.assertTrue((o.out_dir / e.relpath()).exists())

    # —— ⑥ 来源没有校验值时必须先验内容才敢"转正" ——
    def test_promote_requires_content_check_when_source_has_no_checksum(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            o = opts(tmp)
            u = "https://example.com/n.zip"
            e = entry(u, symbol="NUSDT", checksum_url=u + ".CHECKSUM")
            o.out_dir.mkdir(parents=True, exist_ok=True)
            pending = ad.pending_path_for(o.out_dir, e)
            pending.parent.mkdir(parents=True, exist_ok=True)
            pending.write_bytes(b"this is not a zip at all")

            st = ad.State(o.state_path)
            dl = ad.Downloader(FakeFetcher({u + ".CHECKSUM": ad.NotFound("HTTP 404")}),
                               o, sleeper=lambda s: None)
            rec = dl.recheck_checksum(e, st)
            self.assertEqual(rec["status"], ad.S_ANOMALY, "坏字节不得被转正")
            self.assertFalse((o.out_dir / e.relpath()).exists())
            self.assertTrue(pending.exists(), "坏字节仍保留待查")


# ---------------------------------------------------------------------------
# 暂停期限的"取最晚"语义（纯内存 / 临时目录，不联网、不 sleep）
#
# 缺陷原型：多路并发时两条在途请求会各收到一份限流头。若后到的那条更短，
# 无条件覆盖会把先到的长限制**提前解封**——第 61 秒就放行，等于没有限流保护。
# 正确语义：同一域名保留所有未到期期限中的**最晚者**（短不压长、长可延短），
# 不同域名各自独立。
# ---------------------------------------------------------------------------
class TestPauseDeadlineMerge(unittest.TestCase):

    # —— ① 长后短：短暂停不得缩短既有的长限制 ——
    def test_later_shorter_pause_does_not_shorten_longer(self):
        state = ad.State(None)
        n = ad.now_ms()
        host = ad.host_of(RACE_URL)         # 与下面真正发请求的域名保持一致
        state.set_pause(host, n + 3600_000, "first long pause")
        state.set_pause(host, n + 60_000, "later short pause")

        self.assertEqual(state.paused_until(host), n + 3600_000,
                         "后到的短暂停不得覆盖先到的长限制")
        self.assertTrue(state.is_paused(host, n + 61_000),
                        "第 61 秒必须仍处于暂停（缺陷版这里已经放行）")
        self.assertFalse(state.is_paused(host, n + 3600_001))
        info = state.paused[host]
        self.assertEqual(info["reason"], "first long pause",
                         "仍然生效的是长限制，原因应保留长限制那条")
        self.assertEqual(info["dropped_shorter_until_ms"], n + 60_000,
                         "被吸收的较短期限要留痕，便于事后审计")

        # 合并结果必须真的作用到请求路径上：一个请求都不许发出去
        sent: list[str] = []

        class Fetcher:
            def get(self, url, *, timeout=60.0, max_bytes=None):
                sent.append(url)
                return ad.Resp(200, {}, b"x")

        dl = ad.Downloader(Fetcher(), ad.Options(interval_sec=0), sleeper=lambda s: None)
        with self.assertRaises(ad.Paused):
            dl._get(RACE_URL, state=state)          # noqa: SLF001
        self.assertEqual(sent, [], "长限制未被缩短时，请求必须继续被拦住")

    # —— ② 短后长：长限制可以延长短限制 ——
    def test_later_longer_pause_extends_shorter(self):
        state = ad.State(None)
        n = ad.now_ms()
        state.set_pause("example.com", n + 60_000, "short first")
        state.set_pause("example.com", n + 3600_000, "longer later")

        self.assertEqual(state.paused_until("example.com"), n + 3600_000,
                         "长限制必须能把短限制延长")
        self.assertTrue(state.is_paused("example.com", n + 61_000))
        info = state.paused["example.com"]
        self.assertEqual(info["reason"], "longer later")
        self.assertNotIn("dropped_shorter_until_ms", info,
                         "没有发生缩短时不应留下吸收痕迹")

    # —— ③ 域名隔离：一个域名的期限不影响另一个 ——
    def test_deadlines_are_isolated_per_host(self):
        state = ad.State(None)
        n = ad.now_ms()
        state.set_pause("a.example.com", n + 3600_000, "long on a")
        state.set_pause("b.example.com", n + 60_000, "short on b")

        self.assertEqual(state.paused_until("a.example.com"), n + 3600_000)
        self.assertEqual(state.paused_until("b.example.com"), n + 60_000,
                         "b 的期限不得被 a 的长限制牵连延长")

        # a 的长限制也不得因为 b 又登记了一条更短的而变短
        state.set_pause("b.example.com", n + 30_000, "even shorter on b")
        self.assertEqual(state.paused_until("b.example.com"), n + 60_000)
        self.assertEqual(state.paused_until("a.example.com"), n + 3600_000)
        self.assertFalse(state.is_paused("c.example.com", n + 1))

    # —— ④ 重启保持：重放日志也必须取最晚 ——
    def test_restart_replay_keeps_longest_deadline(self):
        with tempfile.TemporaryDirectory() as td:
            sp = Path(td) / "_state.json"
            n = ad.now_ms()

            # (a) 只有日志（没有快照）：两条在途请求各写了一条 pause
            s1 = ad.State(sp)
            s1.set_pause("example.com", n + 3600_000, "long in flight")
            s1.set_pause("example.com", n + 60_000, "short in flight")
            self.assertTrue(ad.State(sp).is_paused("example.com", n + 61_000),
                            "重放日志时短暂停不得把长限制压短")

            # (b) 快照=长、日志=短：重启后仍应保留长
            s2 = ad.State(sp)
            s2.save(force=True)                     # 整理快照，清空日志
            s2.set_pause("example.com", n + 90_000, "short after snapshot")
            s3 = ad.State(sp)
            self.assertEqual(s3.paused_until("example.com"), n + 3600_000,
                             "快照里的长限制不得被日志里的短暂停覆盖")

            # (c) 快照=短、日志=长：重启后应延长
            s3.save(force=True)
            s3.set_pause("example.com", n + 7200_000, "long after snapshot")
            s4 = ad.State(sp)
            self.assertEqual(s4.paused_until("example.com"), n + 7200_000,
                             "日志里的长限制必须能延长快照里的短限制")

    # —— ⑤ 到期恢复：只在到期时解除，且已吸收的短期限不"复活" ——
    def test_expiry_releases_and_absorbed_deadline_does_not_revive(self):
        state = ad.State(None)
        n = ad.now_ms()
        state.set_pause("example.com", n + 3600_000, "long")
        state.set_pause("example.com", n + 60_000, "short")   # 被吸收

        self.assertFalse(state.clear_pause_if_expired("example.com", now=n + 60_001),
                         "长限制还没到期，成功收尾不得解除")
        self.assertTrue(state.is_paused("example.com", n + 60_001))
        self.assertTrue(state.clear_pause_if_expired("example.com", now=n + 3600_001))
        self.assertFalse(state.is_paused("example.com", n + 3600_001),
                         "长限制到期后应可恢复")
        self.assertEqual(state.paused_until("example.com"), 0)

        # 恢复之后新登记的短暂停完整生效，不被历史长限制残留影响
        state.set_pause("example.com", n + 3600_002, "fresh short")
        self.assertEqual(state.paused_until("example.com"), n + 3600_002)
        self.assertTrue(state.is_paused("example.com", n + 3600_001))


# ---------------------------------------------------------------------------
# 提速任务新增：在途字节预算 / 本地退避阶梯 / 请求计数与共享节奏
# ---------------------------------------------------------------------------
class TestInflightGate(unittest.TestCase):
    """并发上调后，**在途响应字节**不得突破声明的预算。"""

    def test_budget_never_exceeded(self):
        gate = ad.InflightGate(1000, default_bytes=100)
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()
            got = gate.reserve(300)
            time.sleep(0.03)
            gate.release(got)

        ts = [threading.Thread(target=worker) for _ in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertLessEqual(gate.peak, 1000, "在途预算被突破")
        self.assertEqual(gate.used, 0, "全部释放后占用应归零")
        self.assertGreater(gate.waits, 0, "预算不够时应等待而不是硬闯")

    def test_oversized_single_file_is_not_skipped(self):
        """预计就超预算的单份**不跳过**：夹到预算上限、等空场后独占跑完。"""
        gate = ad.InflightGate(500, default_bytes=100)
        got = gate.reserve(10_000)
        self.assertEqual(got, 500)
        self.assertEqual(gate.used, 500)
        gate.release(got)
        self.assertEqual(gate.used, 0)

    def test_unknown_size_uses_conservative_default(self):
        gate = ad.InflightGate(10_000, default_bytes=256)
        self.assertEqual(gate.reserve(None), 256)
        self.assertEqual(gate.reserve(0), 256)
        self.assertEqual(gate.reserve(100), 100)

    def test_zero_budget_disables_gate(self):
        gate = ad.InflightGate(0)
        self.assertEqual(gate.reserve(999), 0)
        self.assertEqual(gate.peak, 0)


class TestLocalBackoffLadder(unittest.TestCase):
    """来源没给恢复时间时才用本地阶梯；来源给了期限一律照办不截短。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = Path(self.tmp.name) / "_state.json"
        self.host = "data.binance.vision"

    def tearDown(self):
        self.tmp.cleanup()

    def test_ladder_escalates_and_caps_at_four_hours(self):
        st = ad.State(self.p, compact_every=10_000)
        got = []
        for _ in range(5):
            st.set_pause(self.host, 0, "429 但没有 Retry-After")
            got.append(st.paused_until(self.host) - ad.now_ms())
            self.assertEqual(st.pause_source(self.host), "local")
            st.clear_pause(self.host)
        # 30 分钟 → 1 小时 → 2 小时 → 4 小时 → 4 小时（封顶）
        for got_ms, want_ms in zip(got, ad.LOCAL_BACKOFF_LADDER_MS + (4 * 3600_000,)):
            self.assertAlmostEqual(got_ms / 60_000, want_ms / 60_000, delta=1.0)

    def test_source_deadline_wins_and_is_not_truncated(self):
        st = ad.State(self.p, compact_every=10_000)
        future = ad.now_ms() + 5 * 3600_000          # 来源给 5 小时，超过本地 4 小时封顶
        st.set_pause(self.host, future, "banned until ...")
        self.assertEqual(st.paused_until(self.host), future, "来源期限不得被截短")
        self.assertEqual(st.pause_source(self.host), "source")
        self.assertEqual(st.backoff_level(self.host), 0, "来源给了期限，本地档位不应推进")

    def test_ladder_survives_restart(self):
        st = ad.State(self.p, compact_every=10_000)
        st.set_pause(self.host, 0, "no retry-after")   # 档位 → 1
        st.clear_pause(self.host)
        st.set_pause(self.host, 0, "no retry-after")   # 档位 → 2
        st.clear_pause(self.host)

        st2 = ad.State(self.p, compact_every=10_000)   # 模拟重启
        self.assertEqual(st2.backoff_level(self.host), 2, "本地档位必须跨重启保持")
        st2.set_pause(self.host, 0, "no retry-after")  # 应取第 3 档 = 2 小时
        self.assertAlmostEqual((st2.paused_until(self.host) - ad.now_ms()) / 60_000,
                               120.0, delta=1.0)

    def test_success_resets_ladder(self):
        st = ad.State(self.p, compact_every=10_000)
        st.set_pause(self.host, 0, "no retry-after")
        self.assertEqual(st.backoff_level(self.host), 1)
        st.reset_backoff(self.host)
        self.assertEqual(st.backoff_level(self.host), 0)
        st2 = ad.State(self.p, compact_every=10_000)   # 归零也要跨重启保持
        self.assertEqual(st2.backoff_level(self.host), 0)


class TestRequestCountAndSharedPacing(unittest.TestCase):
    """请求统计必须覆盖 ZIP + CHECKSUM + 重试；所有请求共用同一张节奏发号。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_count_includes_zip_and_checksum(self):
        url = "https://example.com/a.zip"
        z = make_zip()
        f = FakeFetcher({url: z,
                         url + ".CHECKSUM": ad.sha256_hex(z).encode() + b"  a.zip\n"})
        o = opts(self.t, retries=0)
        e = entry(url, checksum_url=url + ".CHECKSUM")
        res, _ = ad.run_fetch([e], o, fetcher=f, sleeper=lambda s: None)
        self.assertEqual(res[0]["status"], ad.S_SUCCESS)
        self.assertEqual(o.last_request_count, 2, "ZIP 与 CHECKSUM 都要计数")
        self.assertEqual(len(f.calls), 2)

    def test_count_includes_retries(self):
        url = "https://example.com/retry.zip"
        z = make_zip()
        attempts = {"n": 0}

        def flaky():
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise ad.FetchError("连接被重置")
            return z

        f = FakeFetcher({url: flaky})
        o = opts(self.t, retries=1)
        e = entry(url, checksum_url="")
        res, _ = ad.run_fetch([e], o, fetcher=f, sleeper=lambda s: None)
        self.assertEqual(res[0]["status"], ad.S_SUCCESS)
        self.assertEqual(o.last_request_count, 2, "重试也必须计入请求数")

    def test_all_requests_share_one_pace(self):
        """3 份文件 = 6 个请求（ZIP+CHECKSUM）共用一张发号 → 总耗时 ≥ 5×间隔。"""
        gap = 0.04
        es, routes = [], {}
        for i in range(3):
            sym = f"P{i}USDT"
            url = f"https://example.com/{sym}.zip"
            z = make_zip(member=f"{sym}-1h-2024-01.csv")
            routes[url] = z
            routes[url + ".CHECKSUM"] = ad.sha256_hex(z).encode() + b"  x.zip\n"
            es.append(entry(url, symbol=sym, checksum_url=url + ".CHECKSUM"))
        o = opts(self.t, interval_sec=gap, retries=0)
        t0 = time.time()
        res, _ = ad.run_fetch(es, o, fetcher=FakeFetcher(routes), sleeper=time.sleep)
        elapsed = time.time() - t0
        self.assertEqual(sum(1 for r in res if r["status"] == ad.S_SUCCESS), 3)
        self.assertGreaterEqual(elapsed, gap * 5 * 0.8,
                                f"6 个请求未共用节奏（耗时 {elapsed:.3f}s 太短）")

    def test_counter_resets_per_call(self):
        url = "https://example.com/once.zip"
        z = make_zip()
        f = FakeFetcher({url: z})
        o = opts(self.t, retries=0)
        ad.run_fetch([entry(url, checksum_url="")], o, fetcher=f,
                     sleeper=lambda s: None)
        first = o.last_request_count
        # 第二块全部本地完整 → 不该发请求
        f2 = FakeFetcher({url: z})
        ad.run_fetch([entry(url, checksum_url="")], o, fetcher=f2,
                     sleeper=lambda s: None)
        self.assertEqual(first, 1)
        self.assertEqual(o.last_request_count, 0, "每块计数必须重置")


class TestReadPhaseErrorsAreWrapped(unittest.TestCase):
    """读取阶段（iter_content）异常必须和连接阶段一样包装成 FetchError。

    回归 2026-10-07 20:14 实测事故：data.binance.vision 读超时抛出的
    ConnectionError 未被包装，穿过 fetch_one 的 except 链，再经线程池
    ex.map() 冒到主线程，把整轮下载（第 9 块中途）打死。
    """

    def _fetcher_whose_body_raises(self, exc):
        f = ad.RequestsFetcher(pool_size=1)

        class _Resp:
            status_code = 200
            headers = {}

            def iter_content(self, n):
                raise exc

            def close(self):
                pass

        class _Sess:
            def get(self, url, timeout=None, stream=True):
                return _Resp()

        f.session = _Sess()
        return f

    def test_read_timeout_becomes_fetch_error(self):
        import requests
        f = self._fetcher_whose_body_raises(
            requests.exceptions.ConnectionError("Read timed out."))
        with self.assertRaises(ad.FetchError):
            f.get("https://example.com/a.zip")

    def test_body_budget_still_not_downgraded(self):
        """字节预算异常不能被降级成普通 FetchError（语义不同）。"""
        f = self._fetcher_whose_body_raises(ad.ByteBudgetExceeded("over"))
        with self.assertRaises(ad.ByteBudgetExceeded):
            f.get("https://example.com/a.zip")

    def test_read_phase_error_recorded_not_fatal(self):
        """端到端：读阶段异常记成「网络失败」，run_fetch 必须活着返回。"""
        import requests
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            e = entry("https://example.com/r.zip", symbol="RUSDT")
            f = self._fetcher_whose_body_raises(
                requests.exceptions.ConnectionError("Read timed out."))
            r, summary = ad.run_fetch([e], opts(tmp, retries=1), fetcher=f,
                                      sleeper=lambda s: None)
            self.assertEqual(r[0]["status"], ad.S_TRANSIENT)
            self.assertIn("网络失败 1", summary)


class TestPartReuse(unittest.TestCase):
    """`.part` 复用：把"留证字节"提升为正式文件，但**一步校验都不少**。

    背景：2,131 项 `content_anomaly` 的原始字节都已完好保存在 `.part` 里
    （全量核验 2131/2131 通过）。复用它们的唯一目的是**省掉重复下载**，
    绝不能顺带把校验放行。四类验收各自独立成测试：

    1. 命中 `.part` → **不发 ZIP 请求**（但 CHECKSUM 请求照发）；
    2. 复用后校验不过 → **仍是 `content_anomaly`**，不转正；
    3. 重跑 → 已转正的项变成"本地完整"，**不重复下载**；
    4. 原已完成项 → 状态**不回退**。
    """

    def _seed_part(self, tmp: Path, e: ad.Entry, content: bytes) -> Path:
        part = ad.part_path_for(tmp / "out", e)
        part.parent.mkdir(parents=True, exist_ok=True)
        part.write_bytes(content)
        return part

    def test_hit_part_skips_zip_download(self):
        """命中 `.part` 时，ZIP 的 URL 一次都不该被请求。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            content = make_zip()
            zip_url = "https://example.com/part/AUSDT.zip"
            ck_url = zip_url + ".CHECKSUM"
            digest = ad.sha256_hex(content)
            e = entry(zip_url, symbol="AUSDT", size=len(content),
                      checksum_url=ck_url)
            self._seed_part(tmp, e, content)
            # ZIP 路由**故意不给**：一旦真去下载就会 NotFound
            f = FakeFetcher({ck_url: f"{digest}  AUSDT.zip\n".encode()})

            r, summary = ad.run_fetch([e], opts(tmp, reuse_parts=True), fetcher=f,
                                      sleeper=lambda s: None)

            self.assertEqual(f.calls, [ck_url],
                             f"只应请求 CHECKSUM，实际请求了 {f.calls}")
            self.assertNotIn(zip_url, f.calls, "不得请求 ZIP")
            self.assertEqual(r[0]["status"], ad.S_SUCCESS)
            self.assertEqual(r[0]["checksum_status"], ad.CHECK_VERIFIED)
            official = tmp / "out" / e.relpath()
            self.assertTrue(official.exists(), "复用成功必须转正为正式文件")
            self.assertFalse(ad.part_path_for(tmp / "out", e).exists(),
                             "转正后 .part 应被消费（_write 内部 replace）")

    def test_reuse_disabled_by_default_still_downloads(self):
        """默认关闭时行为与旧版一致：照常发 ZIP 请求。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            content = make_zip()
            zip_url = "https://example.com/part/BUSDT.zip"
            e = entry(zip_url, symbol="BUSDT", size=len(content))
            self._seed_part(tmp, e, content)          # 即使 .part 在
            f = FakeFetcher({zip_url: content})       # 也应去下载

            r, _ = ad.run_fetch([e], opts(tmp), fetcher=f, sleeper=lambda s: None)

            self.assertIn(zip_url, f.calls, "未开开关就必须走正常下载")
            self.assertEqual(r[0]["status"], ad.S_SUCCESS)

    def test_reused_part_failing_checksum_stays_anomaly(self):
        """复用后校验不符 → 仍是 `content_anomaly`，**不得转正**。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            content = make_zip()
            zip_url = "https://example.com/part/CUSDT.zip"
            ck_url = zip_url + ".CHECKSUM"
            e = entry(zip_url, symbol="CUSDT", size=len(content),
                      checksum_url=ck_url)
            self._seed_part(tmp, e, content)
            other = "0" * 64                          # 与本地摘要必然不同
            f = FakeFetcher({ck_url: f"{other}  CUSDT.zip\n".encode()})

            r, _ = ad.run_fetch([e], opts(tmp, reuse_parts=True), fetcher=f,
                                sleeper=lambda s: None)

            self.assertEqual(r[0]["status"], ad.S_ANOMALY)
            self.assertEqual(r[0]["checksum_status"], ad.CHECK_MISMATCH)
            self.assertNotIn(zip_url, f.calls, "校验失败也不该回退去下载")
            self.assertFalse((tmp / "out" / e.relpath()).exists(),
                             "校验不过绝不能出现在正式路径")
            self.assertTrue(ad.part_path_for(tmp / "out", e).exists(),
                            "字节要留在 .part 继续留证")

    def test_reused_part_zip_structure_bad_stays_anomaly(self):
        """来源无校验值时，`.part` 还必须过结构检查才可能转正。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            bad = b"this is not a zip at all" * 40
            zip_url = "https://example.com/part/DUSDT.zip"
            e = entry(zip_url, symbol="DUSDT", size=None)   # 无清单大小 → 不看字节数
            self._seed_part(tmp, e, bad)
            f = FakeFetcher({})                             # 无 CHECKSUM → no_checksum_source

            r, _ = ad.run_fetch([e], opts(tmp, reuse_parts=True), fetcher=f,
                                sleeper=lambda s: None)

            self.assertEqual(r[0]["status"], ad.S_ANOMALY)
            self.assertIn("内容异常", r[0]["note"])
            self.assertFalse((tmp / "out" / e.relpath()).exists())

    def test_second_run_does_not_download_again(self):
        """复用转正后再跑一遍 → 记为「本地完整」，**零请求**（不重复下载）。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            content = make_zip()
            zip_url = "https://example.com/part/EUSDT.zip"
            ck_url = zip_url + ".CHECKSUM"
            digest = ad.sha256_hex(content)
            e = entry(zip_url, symbol="EUSDT", size=len(content),
                      checksum_url=ck_url)
            self._seed_part(tmp, e, content)
            f1 = FakeFetcher({ck_url: f"{digest}  EUSDT.zip\n".encode()})
            o1 = opts(tmp, reuse_parts=True)
            r1, _ = ad.run_fetch([e], o1, fetcher=f1, sleeper=lambda s: None)
            self.assertEqual(r1[0]["status"], ad.S_SUCCESS)
            self.assertEqual(f1.calls, [ck_url])

            f2 = FakeFetcher({})   # 第二轮**任何**请求都是意料之外
            o2 = opts(tmp, reuse_parts=True)
            o2.state_path = o1.state_path          # 续用同一台账
            r2, _ = ad.run_fetch([e], o2, fetcher=f2, sleeper=lambda s: None)

            self.assertEqual(f2.calls, [], f"第二轮不该发任何请求：{f2.calls}")
            self.assertEqual(r2[0]["status"], ad.S_COMPLETE)
            self.assertEqual(r2[0]["checksum_sha256"], digest,
                             "跳过时必须保留已确认的校验摘要，不能被空值抹掉")

    def test_already_done_item_not_regressed(self):
        """已完成项在开复用后**不回退**：仍是 `skipped_complete` + 原摘要。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            content = make_zip()
            zip_url = "https://example.com/part/FUSDT.zip"
            ck_url = zip_url + ".CHECKSUM"
            digest = ad.sha256_hex(content)
            e = entry(zip_url, symbol="FUSDT", size=len(content),
                      checksum_url=ck_url)
            # 第一轮：走正常下载完成
            f1 = FakeFetcher({zip_url: content, ck_url: f"{digest}  FUSDT.zip\n".encode()})
            o1 = opts(tmp)
            r1, _ = ad.run_fetch([e], o1, fetcher=f1, sleeper=lambda s: None)
            self.assertEqual(r1[0]["status"], ad.S_SUCCESS)

            # 第二轮：**打开复用**，也不该影响已完成项
            f2 = FakeFetcher({})
            o2 = opts(tmp, reuse_parts=True)
            o2.state_path = o1.state_path
            r2, _ = ad.run_fetch([e], o2, fetcher=f2, sleeper=lambda s: None)

            self.assertEqual(f2.calls, [])
            self.assertEqual(r2[0]["status"], ad.S_COMPLETE)
            self.assertEqual(r2[0]["checksum_status"], ad.CHECK_VERIFIED)
            self.assertEqual(r2[0]["checksum_sha256"], digest)
            self.assertEqual(r2[0]["bytes"], len(content))

    def test_reused_part_size_mismatch_with_manifest_stays_anomaly(self):
        """清单大小与实际字节不符时，复用**不能**绕过 `inspect_content` 的大小检查。

        这正是 2,131 项的真实处境：清单旧大小 ≠ 当前字节。若不复用清单修正
        就必须重下；这也说明"复用 + 旧清单"会空转（校验不过），
        所以纳管前必须先换成 v3 修正清单。
        """
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            content = make_zip()
            zip_url = "https://example.com/part/GUSDT.zip"
            ck_url = zip_url + ".CHECKSUM"
            digest = ad.sha256_hex(content)
            e = entry(zip_url, symbol="GUSDT",
                      size=len(content) + 329,          # 清单记的是旧大小
                      checksum_url=ck_url)
            self._seed_part(tmp, e, content)
            f = FakeFetcher({ck_url: f"{digest}  GUSDT.zip\n".encode()})

            r, _ = ad.run_fetch([e], opts(tmp, reuse_parts=True), fetcher=f,
                                sleeper=lambda s: None)

            self.assertEqual(r[0]["status"], ad.S_ANOMALY)
            self.assertIn("字节数与清单不符", r[0]["note"])
            self.assertNotIn(zip_url, f.calls)


if __name__ == "__main__":
    unittest.main(verbosity=2)
