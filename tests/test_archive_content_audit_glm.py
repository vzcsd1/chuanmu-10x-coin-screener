"""内容审计器的纯内存小样例测试（tasks/glm53f_archive_content_audit.md 验收）。

覆盖：8192 字节之后的错误仍被发现、无表头/有表头、毫秒/微秒混用、
合法负费率不误报、日期错配、重复时间、预算中止不算通过。
不照抄实现：直接构造 ZIP 字节，断言 anomalies/file_checks 的行为。
"""
import csv
import io
import sys
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, main

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import archive_content_audit_glm as aud  # noqa: E402

MS = 1_700_000_000_000          # 2023-11-14 起每小时
US = 1_700_000_000_000 * 1000   # 同刻的微秒


def kline_row(ms, unit="ms", o=100.0, h=101.0, l=99.0, c=100.0, vol=1.0):
    t = ms * (1000 if unit == "us" else 1)
    return [str(t), o, h, l, c, vol, ms + 3_599_999, vol * 100, 5, vol * 0.5,
            vol * 0.5]


def make_zip(path, lines, header=None):
    buf = io.StringIO()
    w = csv.writer(buf)
    if header:
        w.writerow(header)
    w.writerows(lines)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("data.csv", buf.getvalue())


class AuditorTests(TestCase):
    def _run_one(self, tmp, lines, header=None, dataset="klines",
                 period="2023-11"):
        """直接驱动 cmd_audit 的内部单文件路径（经 state 假文件）。"""
        state = {"records": {
            f"spot|{dataset}|TESTUSDT||{period}|{period}": {
                "status": "ok", "path": "", "bytes": 0}}}
        # 构造最小状态与 zip，然后把 cmd_audit 指向它们
        (Path(tmp) / "_state.json").write_text(
            __import__("json").dumps(state), encoding="utf-8")
        zip_path = Path(tmp) / "TESTUSDT-spot.zip"
        make_zip(zip_path, lines, header)
        # 重写 state 的 path
        state["records"][f"spot|{dataset}|TESTUSDT||{period}|{period}"]["path"] = \
            str(zip_path)
        (Path(tmp) / "_state.json").write_text(
            __import__("json").dumps(state), encoding="utf-8")
        old_state, old_out = aud.STATE, aud.OUT
        aud.STATE = Path(tmp) / "_state.json"
        aud.OUT = Path(tmp) / "out"
        try:
            aud.cmd_sample(None)
            aud.cmd_audit(None)
        finally:
            aud.STATE, aud.OUT = old_state, old_out
        checks = list(csv.DictReader(
            (aud.OUT if False else Path(tmp) / "out" / "file_checks.csv")
            .open(encoding="utf-8-sig")))
        anoms = list(csv.DictReader(
            (Path(tmp) / "out" / "anomalies.csv").open(encoding="utf-8-sig")))
        return checks, anoms

    def test_error_after_8192_bytes_is_found(self):
        """ds 的 inspect 只看前 8KB；审计器必须读到 8KB 之后的坏行。"""
        with TemporaryDirectory() as tmp:
            # 前 300 行正常（>8KB），最后一行 high < close
            lines = [kline_row(MS + i * 3_600_000) for i in range(300)]
            lines.append(kline_row(MS + 301 * 3_600_000, h=50.0, c=100.0))
            checks, anoms = self._run_one(tmp, lines)
            self.assertEqual(checks[0]["status"], "ok")
            kinds = {a["kind"] for a in anoms}
            self.assertIn("ohlc_relation", kinds)

    def test_no_header_and_header_both_parse(self):
        with TemporaryDirectory() as tmp:
            lines = [kline_row(MS + i * 3_600_000) for i in range(10)]
            checks, _ = self._run_one(tmp, lines, header=None)
            self.assertEqual(checks[0]["header_present"], "no")
            self.assertEqual(int(checks[0]["rows_read"]), 10)
            checks, _ = self._run_one(tmp, lines, header=[
                "open_time", "open", "high", "low", "close", "volume",
                "close_time", "quote_volume", "trades",
                "taker_buy_base", "taker_buy_quote"])
            self.assertEqual(checks[0]["header_present"], "yes")
            self.assertEqual(int(checks[0]["rows_read"]), 10)

    def test_ms_us_mixing_is_flagged(self):
        with TemporaryDirectory() as tmp:
            lines = [kline_row(MS + i * 3_600_000) for i in range(50)]
            lines += [kline_row(US + i * 3_600_000, unit="us") for i in range(10)]
            checks, anoms = self._run_one(tmp, lines)
            self.assertGreater(int(checks[0]["unit_mix_rows"]), 0)
            self.assertIn("time_unit_mix", {a["kind"] for a in anoms})

    def test_negative_funding_is_not_anomaly(self):
        with TemporaryDirectory() as tmp:
            # funding 无表头：calc_time, funding_interval_hours, last_funding_rate
            lines = [[str(MS + i * 8 * 3_600_000 * 1000 // 1000), "8", "-0.000125"]
                     for i in range(10)]
            checks, anoms = self._run_one(tmp, lines, dataset="fundingRate",
                                          period="2023-11")
            kinds = {a["kind"] for a in anoms}
            self.assertNotIn("negative_funding", kinds)
            self.assertEqual(int(checks[0]["neg_funding_count"]), 10)

    def test_date_mismatch_flagged(self):
        with TemporaryDirectory() as tmp:
            # 文件名声称 2023-11，内容却是 2024-01（毫秒错档）
            lines = [kline_row(MS + 50 * 24 * 3_600_000 + i * 3_600_000)
                     for i in range(10)]
            checks, anoms = self._run_one(tmp, lines)
            self.assertEqual(checks[0]["date_match"], "no")
            self.assertIn("date_mismatch", {a["kind"] for a in anoms})

    def test_duplicate_times_flagged(self):
        with TemporaryDirectory() as tmp:
            lines = [kline_row(MS + i * 3_600_000) for i in range(10)]
            lines.append(kline_row(MS + 5 * 3_600_000))  # 与第 6 行重复
            checks, anoms = self._run_one(tmp, lines)
            self.assertGreater(int(checks[0]["dup_time_count"]), 0)
            self.assertIn("duplicate_time", {a["kind"] for a in anoms})

    def test_legal_string_datetime_is_parsed_not_flagged(self):
        """合法字符串日期必须正确解析（不报 time_unparseable），日期核对通过。"""
        with TemporaryDirectory() as tmp:
            lines = [["2022-01-26 03:35:00", "TESTUSDT", "2023567.0", "4176.01"],
                     ["2022-01-26 03:40:00", "TESTUSDT", "2023568.0", "4176.02"]]
            checks, anoms = self._run_one(tmp, lines, header=[
                "create_time", "symbol", "sum_open_interest",
                "sum_open_interest_value"], dataset="metrics",
                period="2022-01-26")
            self.assertEqual(int(checks[0]["rows_read"]), 2)
            self.assertEqual(checks[0]["time_unit_dominant"], "strdate")
            self.assertEqual(checks[0]["date_match"], "yes")
            self.assertNotIn("time_unparseable", {a["kind"] for a in anoms})

    def test_illegal_string_time_is_flagged(self):
        with TemporaryDirectory() as tmp:
            lines = [["2022-01-26 03:35:00", "TESTUSDT", "1.0", "2.0"],
                     ["not-a-date", "TESTUSDT", "1.0", "2.0"],
                     ["2022-13-45 99:99:99", "TESTUSDT", "1.0", "2.0"]]
            checks, anoms = self._run_one(tmp, lines, header=[
                "create_time", "symbol", "sum_open_interest",
                "sum_open_interest_value"], dataset="metrics",
                period="2022-01-26")
            self.assertEqual(int(checks[0]["null_or_nonfinite"]), 2)
            self.assertEqual(sum(1 for a in anoms if a["kind"] == "time_unparseable"), 2)

    def test_string_date_and_ms_mixing_flagged(self):
        with TemporaryDirectory() as tmp:
            lines = [["2022-01-26 03:35:00", "TESTUSDT", "1.0", "2.0"]]
            lines += [[str(MS + i * 3_600_000), "TESTUSDT", "1.0", "2.0"]
                      for i in range(20)]
            checks, anoms = self._run_one(tmp, lines, header=[
                "create_time", "symbol", "sum_open_interest",
                "sum_open_interest_value"], dataset="metrics",
                period="2023-11")
            self.assertGreater(int(checks[0]["unit_mix_rows"]), 0)
            self.assertIn("time_unit_mix", {a["kind"] for a in anoms})

    def test_budget_abort_is_not_pass(self):
        with TemporaryDirectory() as tmp:
            old_cap = aud.MAX_FILE_BYTES
            aud.MAX_FILE_BYTES = 1024  # 强制单文件预算中止
            try:
                lines = [kline_row(MS + i * 3_600_000) for i in range(500)]
                checks, _ = self._run_one(tmp, lines)
            finally:
                aud.MAX_FILE_BYTES = old_cap
            self.assertEqual(checks[0]["status"], "partial_budget")
            self.assertNotEqual(checks[0]["status"], "ok")


if __name__ == "__main__":
    main()
