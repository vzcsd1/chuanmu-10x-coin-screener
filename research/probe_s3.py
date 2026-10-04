#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""探测 data.binance.vision 的列举与下载速度，决定研究样本规模。"""
from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from xml.etree import ElementTree as ET

import requests

S3 = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
CDN = "https://data.binance.vision"
KLINE = "data/spot/monthly/klines"
NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "chuanmu-research/0.1"})

src = Path(r"C:/Users/Administrator/Desktop/裸K盘感项目/data/symbols_all.txt")
syms = [l.strip() for l in src.read_text(encoding="utf-8").splitlines() if l.strip()]
usdt = [s for s in syms if s.endswith("USDT") and s != "USDTUSDT"]
print(f"symbols_all 总数 {len(syms)}，USDT 计价 {len(usdt)}")
print("样例:", ", ".join(usdt[:10]))


def months_of(sym: str, interval: str = "1d"):
    p = {"list-type": "2", "prefix": f"{KLINE}/{sym}/{interval}/", "max-keys": "1000"}
    try:
        r = SESSION.get(S3, params=p, timeout=30)
        if r.status_code != 200:
            return sym, []
        root = ET.fromstring(r.text)
        keys = [c.find("s3:Key", NS).text for c in root.findall(".//s3:Contents", NS)]
        return sym, sorted(k.rsplit("-", 1)[-1].replace(".zip", "") for k in keys if k.endswith(".zip"))
    except Exception:
        return sym, []


sample = usdt[:120]
t0 = time.time()
with ThreadPoolExecutor(max_workers=16) as ex:
    res = dict(ex.map(months_of, sample))
dt = time.time() - t0
print(f"\n[列举] {len(sample)} 个交易对用时 {dt:.1f}s  → 单币 {dt/len(sample)*1000:.0f} ms")
have = {s: m for s, m in res.items() if m}
print(f"[列举] 有 1d 数据的 {len(have)}/{len(sample)}")
tot_months = sum(len(m) for m in have.values())
print(f"[列举] 这 120 币合计 {tot_months} 个币·月文件")
print(f"[推算] 全量 {len(usdt)} 币列举约需 {dt/len(sample)*len(usdt)/60:.1f} 分钟")

for s, m in list(have.items())[:5]:
    print(f"   {s:<12} {len(m):>3} 月  {m[0]} ~ {m[-1]}")

# 下载测速
if have:
    sym = list(have)[0]
    mon = have[sym][-1]
    url = f"{CDN}/{KLINE}/{sym}/1d/{sym}-1d-{mon}.zip"
    t1 = time.time()
    r = SESSION.get(url, timeout=60)
    print(f"\n[下载] {url.rsplit('/',1)[-1]}  {r.status_code}  {len(r.content)/1024:.1f} KB  {time.time()-t1:.2f}s")
