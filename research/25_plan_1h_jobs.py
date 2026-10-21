#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""25 · 生成「全池 1h」下载清单（A14 ② 的前置）

为什么需要
----------
**（2026-10-21 已执行完毕，本节保留作历史。现状：19,796 个币月 / 664 币，`ret_net_30` 覆盖 99.6%。）**

原状态：`data/1h` 只有 **396 个币 × 约 10 个月**（3,926 个币月），而且只覆盖爆拉样本附近的窗口。
后果有两层：
  ① **A15 面板做不出来**——面板要「全池 × 每个交易日」，缺的正是「扫到了但没涨」的样本；
  ② **30 天口径算不出来**——`ret_net_30` 只有 24% 覆盖，因为缓存只覆盖事件前后一个多月。

这个脚本只做「列清单」，不下载。下载交给 `research/00_fetch.py klines1h --jobs`。

清单怎么来
----------
对 `data/symbols_all.txt` 里每个币，向 S3 列出归档里**真实存在**的 1h 月份
（不靠猜，避免几万个 404），再减掉本地已有 parquet 覆盖的月份。

用法
----
    py -3.10 research/25_plan_1h_jobs.py            # 列清单并写 data/jobs_1h_full.json
    py -3.10 research/25_plan_1h_jobs.py --dry-run  # 只统计，不写

然后
----
    py -3.10 research/00_fetch.py klines1h --jobs data/jobs_1h_full.json --workers 32
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from xml.etree import ElementTree as ET

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SYMS = DATA / "symbols_all.txt"
OUT = DATA / "jobs_1h_full.json"
H1 = DATA / "1h"

S3 = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
PREFIX = "data/spot/monthly/klines"
NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
START_MONTH = "2022-06"


def _proxy() -> str | None:
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        if winreg.QueryValueEx(key, "ProxyEnable")[0]:
            return "http://" + str(winreg.QueryValueEx(key, "ProxyServer")[0])
    except Exception:  # noqa: BLE001
        pass
    for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        v = os.getenv(k)
        if v:
            return v
    return None


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": "chuanmu-research/0.1"})
    s.trust_env = False
    p = _proxy()
    if p:
        s.proxies = {"http": p, "https": p}
    return s


SESS = _session()


def months_of(sym: str) -> tuple[str, list[str]]:
    p = {"list-type": "2", "prefix": f"{PREFIX}/{sym}/1h/", "max-keys": "1000"}
    try:
        r = SESS.get(S3, params=p, timeout=30)
        if r.status_code != 200:
            return sym, []
        root = ET.fromstring(r.text)
        out = []
        for c in root.findall(".//s3:Contents", NS):
            k = c.find("s3:Key", NS).text
            if not k.endswith(".zip"):
                continue
            parts = k.rsplit("-", 2)
            out.append(parts[-2] + "-" + parts[-1].replace(".zip", ""))
        return sym, sorted({m for m in out if m >= START_MONTH})
    except Exception:  # noqa: BLE001
        return sym, []


def have_months() -> set[tuple[str, str]]:
    """本地已有 parquet 的 (币, 月)。文件存在即认为有效（00_fetch 只写完整解析成功的）。"""
    got = set()
    for p in H1.glob("*.parquet"):
        stem = p.stem                      # {SYM}-{YYYY}-{MM}
        parts = stem.rsplit("-", 2)
        if len(parts) == 3:
            got.add((parts[0], f"{parts[1]}-{parts[2]}"))
    return got


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--workers", type=int, default=24)
    args = ap.parse_args()

    if not SYMS.exists():
        print(f"[error] 缺 {SYMS}，先跑 py -3.10 tools/build_symbols_all.py")
        return 1
    syms = [x.strip() for x in SYMS.read_text(encoding="utf-8").splitlines() if x.strip()]
    print(f"[plan1h] universe {len(syms)} 个币")

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        res = dict(ex.map(months_of, syms))
    have = have_months()
    print(f"[plan1h] 本地已有 {len(have):,} 个币月｜列举用时 {time.time()-t0:.0f}s")

    jobs, per_sym = [], {}
    for sym in syms:
        ms = res.get(sym) or []
        per_sym[sym] = len(ms)
        for m in ms:
            if (sym, m) not in have:
                jobs.append({"symbol": sym, "month": m})
    jobs.sort(key=lambda j: (j["month"], j["symbol"]))

    total = sum(per_sym.values())
    print(f"[plan1h] 归档里共 {total:,} 个币月｜本地已有 {len(have):,}｜"
          f"**待下载 {len(jobs):,}**")
    if jobs:
        months = sorted({j["month"] for j in jobs})
        print(f"         月份跨度 {months[0]} ~ {months[-1]}（{len(months)} 个月）")
        mb = len(jobs) * 60 / 1024
        print(f"         预估体积 ≈ {mb:,.0f} MB（按 60KB/币月估）")
        print("         注意：月度归档里**没有当月**（2026-10 要等 11 月初才出），"
              "当月数据走日度文件或实盘扫描")
        # 最近 4 个月的缺口（2026 年结论薄样本的根因）
        recent = [m for m in months if m >= "2026-07"]
        if recent:
            n_recent = sum(1 for j in jobs if j["month"] >= "2026-07")
            print(f"         其中 2026-07 之后 {n_recent:,} 个（这是 2026 年结论薄样本的根因）")

    if args.dry_run:
        print("[plan1h] --dry-run，未写入")
        return 0
    OUT.write_text(json.dumps(jobs, ensure_ascii=False), encoding="utf-8")
    print(f"[plan1h] -> {OUT.relative_to(ROOT)}")
    print(f"         下一步： py -3.10 research/00_fetch.py klines1h "
          f"--jobs data/jobs_1h_full.json --workers 32")
    return 0


if __name__ == "__main__":
    sys.exit(main())
