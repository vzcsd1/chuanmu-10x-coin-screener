#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""并发路数对照：单路 / 2 路 / 4 路真实采集同一量级的**互不相交**样本。

- 三组样本互不重叠，避免"后一组占了前一组的便宜"。
- 采到的资料**全部入库**（真实 out_dir + 真实状态），可被后续复用。
- 多路共享总节奏、预算与暂停；单一状态写入者（由下载器内部保证）。
- 只做测量与报告，不改策略、不动研究基线。

用法：
  py -3.10 tools/benchmark_lanes.py --queue reports/archive_download_ds_full/queue_v2.csv \
      --per-lane 120 --lanes 1,2,4 --interval-sec 0.02
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import archive_download_ds as ad  # noqa: E402
import archive_queue_ds as aq  # noqa: E402

OUT_DIR = ROOT / "data" / "archive_raw"
REPORT = ROOT / "reports" / "archive_download_ds_full" / "lanes_benchmark.json"


def pick_samples(entries, out_dir, state, per_lane: int, lanes: list[int]):
    """按队列顺序取**未完成**项，切成 len(lanes) 组互不相交的样本。"""
    missing = [e for e in entries
               if aq.classify_entry(e, out_dir, state) == aq.C_MISSING]
    need = per_lane * len(lanes)
    chosen = missing[:need]
    groups = [chosen[i * per_lane:(i + 1) * per_lane] for i in range(len(lanes))]
    return groups, len(missing)


def run_one(group, workers: int, interval_sec: float) -> dict:
    opts = ad.Options(out_dir=OUT_DIR, state_path=OUT_DIR / ad.STATE_NAME,
                      status_csv=None, max_files=0, max_bytes=0, max_minutes=0,
                      min_free_gib=20, interval_sec=interval_sec, retries=2,
                      workers=workers)
    t0 = time.time()
    results, summary = ad.run_fetch(group, opts)
    elapsed = time.time() - t0
    counts = Counter(r["status"] for r in results)
    ok = counts.get(ad.S_SUCCESS, 0) + counts.get(ad.S_COMPLETE, 0)
    verified = sum(1 for r in results
                   if r.get("checksum_status") in ad.CHECK_CONCLUSIVE)
    bad = sum(counts.get(s, 0) for s in
              (ad.S_ANOMALY, ad.S_TRANSIENT, ad.S_NOT_FOUND, ad.S_PAUSED))
    return {
        "workers": workers, "files": len(group), "elapsed_sec": round(elapsed, 1),
        "ok": ok, "verified": verified, "errors": bad,
        "files_per_sec": round(ok / elapsed, 2) if elapsed else 0,
        "bytes": sum(r["bytes"] for r in results if r["status"] == ad.S_SUCCESS),
        "status_counts": dict(counts), "summary": summary,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="并发路数对照实验")
    ap.add_argument("--queue", default=str(ROOT / "reports" / "archive_download_ds_full"
                                           / "queue_v2.csv"))
    ap.add_argument("--per-lane", type=int, default=120)
    ap.add_argument("--lanes", default="1,2,4")
    ap.add_argument("--interval-sec", type=float, default=0.02)
    ap.add_argument("--out", default=str(REPORT))
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    lanes = [int(x) for x in args.lanes.split(",") if x.strip()]
    entries = ad.load_manifest(Path(args.queue))
    state = ad.State(OUT_DIR / ad.STATE_NAME)
    groups, n_missing = pick_samples(entries, OUT_DIR, state, args.per_lane, lanes)
    print(f"队列 {len(entries):,}；未完成 {n_missing:,}；"
          f"取 {args.per_lane}×{len(lanes)} 组互不相交样本")

    rows = []
    for workers, group in zip(lanes, groups):
        print(f"\n--- {workers} 路：{len(group)} 份 ---")
        row = run_one(group, workers, args.interval_sec)
        rows.append(row)
        print(f"  耗时 {row['elapsed_sec']}s | 成功 {row['ok']} | "
              f"校验一致 {row['verified']} | 错误 {row['errors']} | "
              f"{row['files_per_sec']} 份/秒")

    base = rows[0]["files_per_sec"] if rows else 0
    for r in rows:
        r["speedup_vs_1"] = round(r["files_per_sec"] / base, 2) if base else 0
    out = {"per_lane": args.per_lane, "interval_sec": args.interval_sec,
           "queue": Path(args.queue).name, "rows": rows}
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=1),
                              encoding="utf-8")
    print(f"\n结果 -> {args.out}")
    for r in rows:
        print(f"  {r['workers']} 路：{r['files_per_sec']} 份/秒 "
              f"(×{r['speedup_vs_1']})，错误 {r['errors']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
