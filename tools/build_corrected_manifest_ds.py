#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""基于全量核验结果，生成**独立的版本化修正清单**与逐项差异报告。

硬约束（任务书要求）：
  - **不原地修改** queue_v2.csv、_state.json 或任何数据文件
  - 只产出新文件，供唯一写入者退出后由正常流程纳管
  - 不放宽校验、不批量重下、不手动搬 .part

输入：reports/archive_download_ds_full/anomaly_full_audit.csv（全量 2131 项）
输出：
  - anomaly_corrected_manifest_v1.csv   修正后的清单行（含新 remote_size_bytes）
  - anomaly_diff_report.csv             逐项差异（清单 vs 本地 vs 官方）
"""
from __future__ import annotations

import csv
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RD = ROOT / "reports" / "archive_download_ds_full"
AUDIT = RD / "anomaly_full_audit.csv"
QUEUE = RD / "queue_v2.csv"
OUT_MANIFEST = RD / "anomaly_corrected_manifest_v1.csv"
OUT_DIFF = RD / "anomaly_diff_report.csv"

# 与 queue_v2.csv 完全一致的表头，确保修正清单可直接替换行
QCOLS = ["source_url", "market", "dataset", "symbol", "interval",
         "period_start", "period_end", "remote_size_bytes", "checksum_url",
         "discovered_at_utc"]


def main() -> int:
    audit = list(csv.DictReader(AUDIT.open(encoding="utf-8-sig")))
    print(f"核验明细 {len(audit)} 项")

    # 清单全量索引（只读）
    qmap: dict[str, dict] = {}
    with QUEUE.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            qmap[row["source_url"]] = row
    print(f"清单 {len(qmap)} 行（只读，不修改）")

    manifest_rows: list[dict] = []
    diff_rows: list[dict] = []
    n_ok = n_skip = 0

    for a in audit:
        url = a["src_url"]
        q = qmap.get(url)
        if q is None:
            diff_rows.append({**a, "diff_reason": "清单中找不到该 URL"})
            n_skip += 1
            continue
        if a["verdict"] != "SHA_MATCH":
            diff_rows.append({**a, "diff_reason": f"判定非 SHA_MATCH：{a['verdict']}"})
            n_skip += 1
            continue

        local = int(a["local_bytes"])
        official = int(a["official_bytes"]) if a["official_bytes"] else None
        listed = int(q["remote_size_bytes"]) if q["remote_size_bytes"] else None

        # 修正值：优先用官方当前大小（== 本地实际大小，二者已验证一致）
        new_size = official if official is not None else local

        # 修正清单行：除 remote_size_bytes 外，其余字段原样保留
        mrow = dict(q)
        mrow["remote_size_bytes"] = str(new_size)
        manifest_rows.append(mrow)

        diff_rows.append({
            "key": a["key"], "market": a["market"], "dataset": a["dataset"],
            "symbol": a["symbol"], "period": a["period"],
            "listed_bytes": listed, "local_bytes": local,
            "official_bytes": official if official is not None else "",
            "delta_local_minus_listed": (local - listed) if listed is not None else "",
            "delta_official_minus_listed": (official - listed) if (official and listed) else "",
            "local_sha256": a["local_sha256"], "official_sha256": a["official_sha256"],
            "sha_equal": "yes" if a["local_sha256"] == a["official_sha256"] else "no",
            "verdict": a["verdict"], "diff_reason": "清单大小偏小，本地/官方一致",
            "src_url": url,
        })
        n_ok += 1

    # 输出修正清单（保持原表头，可直接替换 queue_v2.csv 对应行）
    with OUT_MANIFEST.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=QCOLS, extrasaction="ignore")
        w.writeheader()
        for r in manifest_rows:
            w.writerow(r)

    # 输出差异报告
    dcols = ["key", "market", "dataset", "symbol", "period",
             "listed_bytes", "local_bytes", "official_bytes",
             "delta_local_minus_listed", "delta_official_minus_listed",
             "local_sha256", "official_sha256", "sha_equal", "verdict",
             "diff_reason", "src_url"]
    with OUT_DIFF.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=dcols, extrasaction="ignore")
        w.writeheader()
        for r in diff_rows:
            w.writerow(r)

    print()
    print(f"修正清单行数 : {len(manifest_rows)}  -> {OUT_MANIFEST.name}")
    print(f"差异报告行数 : {len(diff_rows)}  -> {OUT_DIFF.name}")
    print(f"  （可修正 {n_ok}；跳过 {n_skip}）")
    print()
    print("⚠️  以上均为**新文件**；queue_v2.csv / _state.json 未被修改。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
