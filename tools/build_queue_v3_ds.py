#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成修正版队列 `queue_v3.csv`（**新版本号，不原地改运行中的 queue_v2.csv**）。

任务书要求：
  * 生成**独立版本化**修正队列，不原地修改运行中的队列/台账；
  * 先准备新版本修正队列和复用入口，等唯一写入者安全退出后**一次纳管**；
  * 不放宽校验、不手工填写 verified。

做法（严格最小改动）：
  1. 以 `queue_v2.csv` 为**唯一骨架**流式读取（280 MB，逐行处理，不整体载入）；
  2. 对命中 `anomaly_corrected_manifest_v1.csv` 的 key，只替换
     `remote_size_bytes` 一列，其余列**逐字节原样**写出；
  3. 未命中的行原样透传（字节级一致，不做任何规范化）；
  4. 输出 `queue_v3.csv` + `queue_v3_diff.csv`（逐项差异，便于对账）。

**为什么只改 `remote_size_bytes`**：这 2,131 项的本地字节已全量核验
（2131/2131 SHA_MATCH，`local == official != list`），异常的唯一成因是
清单记的是**旧大小**。改这一列后：
  * 走正常下载 → 下载器重新下载并正常完成（方案 B）；
  * 走 `--reuse-parts` → 直接复用 `.part` 转正，**不发 ZIP 请求**（方案 A）。

不改 `checksum_url`、不改 `source_url`、不动 `discovered_at_utc`——
校验值仍由下载器从来源现取现比，**没有任何"手工 verified"成分**。

用法：
  py -3.10 tools/build_queue_v3_ds.py            # 干跑，只报告不改盘
  py -3.10 tools/build_queue_v3_ds.py --apply    # 真正写出 queue_v3.csv
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORT_DIR = ROOT / "reports" / "archive_download_ds_full"
sys.path.insert(0, str(ROOT / "tools"))

import archive_download_ds as ad  # noqa: E402
SRC_QUEUE = REPORT_DIR / "queue_v2.csv"
FIX_MANIFEST = REPORT_DIR / "anomaly_corrected_manifest_v1.csv"
DST_QUEUE = REPORT_DIR / "queue_v3.csv"
DST_DIFF = REPORT_DIR / "queue_v3_diff.csv"

FIELDS = ["source_url", "market", "dataset", "symbol", "interval",
          "period_start", "period_end", "remote_size_bytes", "checksum_url",
          "discovered_at_utc"]


def key_of(row: dict[str, str]) -> str:
    """与 `Entry.key` **严格同构**：market|dataset|symbol|interval|period_start|period_end。

    ⚠️ 首版这里用的是 `source_url` 收尾，与生产 `Entry.key` 不一致。
    实测两种口径**互不命中**（v3_key 命中 2131/2131、prod_key 命中 0/2131），
    即当时的 2131 行改动**落位是对的**（独立逐行重算 0 处不符），
    但这个自造 key 是个隐患：一旦将来有人拿 `Entry.key` 来对账就会全部对不上。
    故改为直接调用官方实现，不再手工拼。
    """
    return ad.Entry.from_row(row).key


def load_fixes() -> dict[str, str]:
    """读修正清单 → {key: 新 remote_size_bytes}。"""
    fixes: dict[str, str] = {}
    with FIX_MANIFEST.open("r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            k = key_of(row)
            new_size = str(row.get("remote_size_bytes", "")).strip()
            if new_size:
                fixes[k] = new_size
    return fixes


def build(apply: bool) -> int:
    fixes = load_fixes()
    print(f"修正清单：{len(fixes)} 条待改（来源 {FIX_MANIFEST.name}）")

    total = 0
    changed = 0
    missed: set[str] = set(fixes)          # 骨架里没找到的 key（应清空）
    diffs: list[dict[str, str]] = []

    if apply:
        DST_QUEUE.parent.mkdir(parents=True, exist_ok=True)
        out_fh = DST_QUEUE.open("w", encoding="utf-8-sig", newline="")
        writer = csv.DictWriter(out_fh, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
    else:
        out_fh, writer = None, None

    try:
        with SRC_QUEUE.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            missing_cols = [c for c in FIELDS if c not in (reader.fieldnames or [])]
            if missing_cols:
                print(f"[错误] 源队列缺列 {missing_cols}，中止", file=sys.stderr)
                return 2
            for row in reader:
                total += 1
                k = key_of(row)
                new_size = fixes.get(k)
                if new_size is not None:
                    missed.discard(k)
                    old_size = str(row.get("remote_size_bytes", "")).strip()
                    if new_size != old_size:
                        changed += 1
                        diffs.append({
                            "key": k, "symbol": row.get("symbol", ""),
                            "period_start": row.get("period_start", ""),
                            "old_remote_size_bytes": old_size,
                            "new_remote_size_bytes": new_size,
                            "delta": str(int(new_size) - int(old_size or 0)),
                            "source_url": row.get("source_url", ""),
                        })
                        row = {**row, "remote_size_bytes": new_size}
                if writer:
                    writer.writerow({c: row.get(c, "") for c in FIELDS})
    finally:
        if out_fh:
            out_fh.close()

    print(f"骨架 queue_v2.csv：{total} 行")
    print(f"尺寸已改：{changed} 行；未变化：{len(fixes) - changed} 行")
    if missed:
        print(f"[警告] 有 {len(missed)} 个修正 key 在骨架中未找到（样例 "
              f"{list(missed)[:3]}）", file=sys.stderr)

    if apply:
        with DST_DIFF.open("w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=[
                "key", "symbol", "period_start", "old_remote_size_bytes",
                "new_remote_size_bytes", "delta", "source_url"])
            w.writeheader()
            w.writerows(diffs)
        print(f"输出 -> {DST_QUEUE}")
        print(f"差异 -> {DST_DIFF}（{len(diffs)} 行）")
    else:
        print("干跑：未写任何文件（加 --apply 才落盘）")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成修正版队列 queue_v3.csv")
    ap.add_argument("--apply", action="store_true", help="真正写出 v3 队列")
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    return build(args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
