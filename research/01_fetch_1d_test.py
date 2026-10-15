# -*- coding: utf-8 -*-
"""B7 隔离验收：`research/01_fetch_1d.py` 的续传与完整性修复

全程离线：把 `SESSION.get` 换成假来源（内存里造 ZIP），在临时目录里跑。
验证四件事：
  ① 文件存在但缺月 → 不会被当成「完整」跳过；
  ② 补采只补缺的月，且**保留**原有行（不覆盖原数据）；
  ③ `--limit` 先挑未完成项，不再空转；
  ④ 全部完整时待办为 0。

用法：py -3.10 research/01_fetch_1d_test.py
"""
from __future__ import annotations

import importlib.util
import io
import json
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("fetch1d", ROOT / "research" / "01_fetch_1d.py")
F = importlib.util.module_from_spec(spec)
spec.loader.exec_module(F)


def make_zip(sym: str, month: str, n_days: int = 28) -> bytes:
    """造一份币安形状的日线月分片 ZIP。"""
    start = pd.Timestamp(month + "-01", tz="UTC")
    rows = []
    for k in range(n_days):
        t = start + pd.Timedelta(days=k)
        if t.strftime("%Y-%m") != month:
            break
        ms = int(t.value // 10 ** 6)
        rows.append(f"{ms},{k+1},{k+2},{k+0.5},{k+1.5},100,{ms+86399999},1000,5,10,20,0")
    csv = "\n".join(rows)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"{sym}-1d-{month}.csv", csv)
    return buf.getvalue()


def run_case():
    tmp = Path(tempfile.mkdtemp(prefix="b7_"))
    try:
        raw = tmp / "1d"
        raw.mkdir(parents=True)
        journal = tmp / "state_1d.jsonl"
        plan_path = tmp / "plan_1d.json"

        sym = "AAVEUSDT"
        months = ["2022-06", "2022-07", "2022-08"]
        plan_path.write_text(json.dumps({sym: months}), encoding="utf-8")

        # 预置一份「缺 2022-07」的本地文件（模拟下载失败留下的残缺文件）
        pre = pd.concat([F._parse_zip(make_zip(sym, m)) for m in ("2022-06", "2022-08")],
                        ignore_index=True)
        pre = pre.drop_duplicates(subset="open_time").sort_values("open_time").reset_index(drop=True)
        pre.insert(0, "symbol", sym)
        pre["dt"] = pd.to_datetime(pre["open_time"], unit="ms", utc=True)
        pre.to_parquet(raw / f"{sym}.parquet", index=False)
        pre_rows = len(pre)

        fetched = []

        class FakeResp:
            def __init__(self, content):
                self.status_code = 200
                self.content = content

        def fake_get(url, timeout=90, **kw):
            m = url.rsplit("/", 1)[-1].replace(f"{sym}-1d-", "").replace(".zip", "")
            fetched.append(m)
            if m in months:
                return FakeResp(make_zip(sym, m))
            return FakeResp(b"x" * 500)

        orig = F.SESSION.get
        F.SESSION.get = fake_get
        try:
            # ② 首次：应只补 2022-07
            args = SimpleNamespace(plan=str(plan_path), raw_dir=str(raw), journal=str(journal),
                                   limit=0, force=False, dry_run=False)
            F.cmd_fetch(args)
            first_fetched = list(fetched)

            # ③ 再跑一次：已完整 → 待办 0（不空转）
            fetched.clear()
            F.cmd_fetch(args)
            second_fetched = list(fetched)

            # --limit 1 且全部完整 → 仍然待办 0
            fetched.clear()
            F.cmd_fetch(SimpleNamespace(plan=str(plan_path), raw_dir=str(raw),
                                        journal=str(journal), limit=1, force=False,
                                        dry_run=False))
            third_fetched = list(fetched)

            # ④ 人为删掉一个月 → 应被重新识别为缺月
            d = pd.read_parquet(raw / f"{sym}.parquet")
            keep = d[d["dt"].dt.strftime("%Y-%m") != "2022-08"]
            keep.to_parquet(raw / f"{sym}.parquet", index=False)
            fetched.clear()
            F.cmd_fetch(SimpleNamespace(plan=str(plan_path), raw_dir=str(raw),
                                        journal=str(journal), limit=1, force=False,
                                        dry_run=False))
            fourth_fetched = list(fetched)
        finally:
            F.SESSION.get = orig

        after = pd.read_parquet(raw / f"{sym}.parquet")
        have = set(after["dt"].dt.strftime("%Y-%m").unique())
        checks = {
            "only_missing_month_requested": first_fetched == ["2022-07"],
            "existing_rows_preserved": pre["open_time"].isin(after["open_time"]).all()
                                       and len(after) > pre_rows,
            "months_now_complete": have == set(months),
            "second_run_no_work": second_fetched == [],
            "limit_does_not_spin": third_fetched == [],
            "detects_removed_month": fourth_fetched == ["2022-08"],
            "journal_written": journal.exists() and len(
                journal.read_text(encoding="utf-8").strip().splitlines()) > 0,
        }
        detail = {"first_fetched": first_fetched, "second_fetched": second_fetched,
                  "third_fetched": third_fetched, "fourth_fetched": fourth_fetched,
                  "pre_rows": pre_rows, "after_rows": len(after), "months": sorted(have)}
        return checks, detail
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    checks, detail = run_case()
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}")
    print("  detail:", json.dumps(detail, ensure_ascii=False))
    print("ALL:", all(checks.values()))
    sys.exit(0 if all(checks.values()) else 1)
