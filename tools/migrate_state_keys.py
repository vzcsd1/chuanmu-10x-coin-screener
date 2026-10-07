#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""台账键迁移：把旧清单口径的状态记录对到新清单的身份上。

**为什么需要**：状态键 = `市场|类别|币种|周期|起|止`。首批清单把月度写成
`period_start=2024-01`，新清单写成 `2024-01-01`，于是**同一个磁盘文件**在新队列里
换了键 → 被当成"有文件无台账"，白跑一次复核。这里按**磁盘相对路径**把二者接上。

安全边界：
- **只新增、不删除**：旧记录原样保留，新键写一条等价记录。
- 只迁移**磁盘上确实存在**且**路径能在新队列里唯一对应**的记录；其余如实报告。
- 默认干跑，`--apply` 才写盘。

用法：
  py -3.10 tools/migrate_state_keys.py --from-queue reports/archive_download_ds_full/queue.csv \
      --to-queue reports/archive_download_ds_full/queue_v2.csv
  py -3.10 tools/migrate_state_keys.py ... --apply
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import archive_download_ds as ad  # noqa: E402

OUT_DIR = ROOT / "data" / "archive_raw"


def migrate(from_queue: Path, to_queue: Path, out_dir: Path, apply: bool) -> dict:
    """**以台账里记录的磁盘路径为桥**，把旧键对到新队列的身份上。

    不能靠"旧队列的键"来找：首批是直接按 glm 旧清单（月份写法）写的台账，
    而队列文件里的键早已是 URL 重解析出的完整日期——两边键对不上，
    只有磁盘路径是共同的。故以 `rec['path']` 为准。
    """
    del from_queue  # 保留参数仅为接口对称；实际以台账 path 为准
    new_entries = ad.load_manifest(to_queue)
    new_keys = {e.key for e in new_entries}
    by_relpath: dict[str, list[ad.Entry]] = {}
    for e in new_entries:
        by_relpath.setdefault(str(e.relpath()).replace("\\", "/"), []).append(e)

    out_dir = Path(out_dir).resolve()
    state = ad.State(out_dir / ad.STATE_NAME)
    moved, ambiguous, orphan = [], [], []
    for key, rec in list(state.records.items()):
        if key in new_keys:
            continue                      # 键没变，无需迁移
        raw = str(rec.get("path") or "")
        if not raw:
            orphan.append((key, "台账无 path"))
            continue
        try:
            rel = str(Path(raw).resolve().relative_to(out_dir)).replace("\\", "/")
        except ValueError:
            orphan.append((key, "path 不在库目录内"))
            continue
        cands = by_relpath.get(rel) or []
        if len(cands) == 1:
            moved.append((key, cands[0]))
        elif len(cands) > 1:
            ambiguous.append((key, [c.key for c in cands]))
        else:
            orphan.append((key, f"新队列无此路径：{rel}"))

    if apply:
        for old_key, new_e in moved:
            rec = dict(state.records.get(old_key) or {})
            rec.pop("key", None)
            rec["note"] = (f"键迁移：{old_key} → {new_e.key}；"
                           + str(rec.get("note", "")))[:200]
            state.set_record(new_e, **rec)
        state.save(force=True)

    return {"moved": len(moved), "ambiguous": len(ambiguous),
            "orphan": len(orphan), "applied": bool(apply),
            "samples": [f"{o} -> {n.key}" for o, n in moved[:5]],
            "ambiguous_samples": [o for o, _ in ambiguous[:5]],
            "orphan_samples": [f"{o} | {why}" for o, why in orphan[:5]]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="按磁盘路径迁移台账键（只增不删）")
    ap.add_argument("--from-queue", required=True)
    ap.add_argument("--to-queue", required=True)
    ap.add_argument("--out-dir", default=str(OUT_DIR))
    ap.add_argument("--apply", action="store_true", help="不加则只干跑")
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    rep = migrate(Path(args.from_queue), Path(args.to_queue),
                  Path(args.out_dir), args.apply)
    print(json.dumps(rep, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
