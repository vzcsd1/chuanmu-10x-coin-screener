#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""A14 验收脚本：数据集可复现性 + 时点一致性 + 30 天口径覆盖。

为什么要有验收脚本
------------------
A14 的四件事都是「小改动、影响所有历史结论」，靠人眼看文档判断不了有没有真修好。
这个脚本把验收标准变成可执行的四条，跑一遍就知道过没过。

四条验收（对应 TODO A14）
------------------------
1. **可复现**：`01_fetch_1d.py` 不再引用仓库外的绝对路径；`data/symbols_all.txt` 存在且非空。
2. **1h 覆盖**：全池每个月的币数不再塌陷（2026-07 之后不能只剩个位数）。
3. **时点一致**：`05_deriv_features.py` 有排序 + 22:55 截断；`03_build_features.py` 用 `iloc[-2]`。
4. **30 天口径**：`data/forward_returns.csv` 的 `ret_net_30` 覆盖 ≥85%。

    py -3.10 tools/verify_a14.py
    py -3.10 tools/verify_a14.py --json
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def check_reproducible() -> dict:
    src = (ROOT / "research" / "01_fetch_1d.py").read_text(encoding="utf-8")
    # 找 `Path(r"C:/...")` 这类仓库外绝对路径（排除 ROOT 推导出来的）
    bad = re.findall(r'Path\(\s*r?["\']([A-Za-z]:[/\\\\][^"\']+)["\']\s*\)', src)
    bad = [b for b in bad if "川沐十倍币筛选" not in b and str(ROOT) not in b]
    sym = DATA / "symbols_all.txt"
    n = len([x for x in sym.read_text(encoding="utf-8").splitlines() if x.strip()]) if sym.exists() else 0
    # 清单必须真的进仓库。曾经 `data/` 整个被 .gitignore 忽略，文件在本地存在、
    # 校验也能过，但换一台机器就没了——这正是 A14 要修的那类失效，所以单独查一次。
    # git 不可用时只降级为「未知」，不算失败（研究环境不一定有 git）。
    tracked = None
    try:
        r = subprocess.run(["git", "ls-files", "--error-unmatch", "data/symbols_all.txt"],
                           cwd=ROOT, capture_output=True, text=True, timeout=15)
        tracked = r.returncode == 0
    except Exception:  # noqa: BLE001
        tracked = None
    return {"外部绝对路径": bad, "symbols_all.txt": n,
            "清单已入库": tracked,
            "ok": (not bad) and n > 0 and tracked is not False}


def check_1h() -> dict:
    files = list((DATA / "1h").glob("*.parquet"))
    per_month: Counter = Counter()
    per_sym: Counter = Counter()
    for p in files:
        parts = p.stem.rsplit("-", 2)
        if len(parts) != 3:
            continue
        sym, y, m = parts[0], parts[1], parts[2]
        per_month[f"{y}-{m}"] += 1
        per_sym[sym] += 1
    months = sorted(per_month)
    tail = {m: per_month[m] for m in months[-6:]}
    thin = [m for m in months if m >= "2026-07" and per_month[m] < 300]
    return {"币月总数": len(files), "币数": len(per_sym), "月份数": len(months),
            "最近 6 个月每月币数": tail, "仍然偏薄的月份": thin,
            "ok": not thin}


def check_asof() -> dict:
    d5 = (ROOT / "research" / "05_deriv_features.py").read_text(encoding="utf-8")
    d3 = (ROOT / "research" / "03_build_features.py").read_text(encoding="utf-8")
    has_sort = 'sort_values("create_time")' in d5
    has_asof = "ASOF_HHMM" in d5 and '"22:55"' in d5
    iloc2 = "iloc[-2]" in d3
    return {"05 有排序": has_sort, "05 截到 22:55": has_asof,
            "03 用 iloc[-2]（最后一根已收盘）": iloc2,
            "ok": has_sort and has_asof and iloc2}


def check_ret30() -> dict:
    p = DATA / "forward_returns.csv"
    if not p.exists():
        return {"ok": False, "err": "缺 forward_returns.csv"}
    fr = pd.read_csv(p)
    col = "ret_net_30"
    if col not in fr:
        return {"ok": False, "err": f"缺 {col} 列"}
    cov = fr[col].notna().mean()
    return {"覆盖": round(float(cov), 4), "样本": len(fr), "ok": cov >= 0.85}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    res = {
        "1_可复现": check_reproducible(),
        "2_1h覆盖": check_1h(),
        "3_时点一致": check_asof(),
        "4_30天口径": check_ret30(),
    }
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if all(v.get("ok") for v in res.values()) else 1

    print("=" * 70)
    print("A14 验收")
    print("=" * 70)
    for name, r in res.items():
        flag = "✅" if r.get("ok") else "❌"
        print(f"\n{flag} {name}")
        for k, v in r.items():
            if k == "ok":
                continue
            print(f"     {k}: {v}")
    ok = all(v.get("ok") for v in res.values())
    print("\n" + "=" * 70)
    print("总结：" + ("全部通过" if ok else "**未通过**，见上面 ❌ 项"))
    print("=" * 70)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
