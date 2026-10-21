#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""M12 盘口 ±5% 厚度不对称：从归档回补（2026-10-21）

为什么不用前向采集
------------------
脑暴文档把 M12 标成「只能从今天开始抓，历史买不到」。**这个判断是错的。**
实测 `data/futures/um/daily/bookDepth/` **仍在每日发布**（2026-10-20 的数据在 10-21 07:07 UTC 上架），
而且内容正好是 M12 要的东西——每 30 秒一个快照、±1~5% 十个分带：

    timestamp,percentage,depth,notional
    2026-10-20 00:00:01,-5.00,10053.98500000,808006890.87950000
    2026-10-20 00:00:01,-1.00,2949.36100000,240327446.67850000
    2026-10-20 00:00:01,3.00,7197.79500000,604148278.91290000

`percentage` 负数 = 买盘（mid 下方），正数 = 卖盘；`depth` 是基币数量，`notional` 是计价币金额。
一天 2,880 个快照 × 10 档 = 28,800 行（BTCUSDT 实测 34,561 行含边界）。

所以 M12 变成**回补任务**，不用等半年。

只补需要的日子，不整库下载
--------------------------
全库是 1,009 个合约 × 约 700 天 ≈ 70 万份文件、上百 GB，没有必要。
第一批只补 **822 个爆拉事件的 `pre_day`**——这是所有研究样本的定义日，一份就够算特征。

用法
----
    py -3.10 tools/backfill_bookdepth_ds.py plan     # 只统计，不下载（零请求）
    py -3.10 tools/backfill_bookdepth_ds.py fetch    # 下载
    py -3.10 tools/backfill_bookdepth_ds.py verify   # 复核本地
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUT = DATA / "bookdepth"
MANIFEST = DATA / "bookdepth_manifest.json"

S3 = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
PREFIX = "data/futures/um/daily/bookDepth"


def proxy() -> str | None:
    """系统代理优先。环境变量里那个 9390 是沙箱内部代理，对公网是死路（A14 踩过）。"""
    forced = (os.getenv("BINANCE_PROXY") or "").strip()
    if forced:
        return forced
    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
            ) as key:
                if winreg.QueryValueEx(key, "ProxyEnable")[0]:
                    s = str(winreg.QueryValueEx(key, "ProxyServer")[0]).strip()
                    if s:
                        return s if "://" in s else f"http://{s}"
        except Exception:  # noqa: BLE001
            pass
    return None


def session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    p = proxy()
    if p:
        s.proxies = {"http": p, "https": p}
    return s


def tasks(labels: str = "1") -> list[dict]:
    """从研究样本里取 (合约, 日期)。

    样本源用 `data/hit_rate_current.csv`（1,402 条 = 822 爆拉 + 580 对照），
    因为它已经是「币 + pre_day + 标签」的标准表，且 A14 之后口径统一。

    `labels` 默认只取 `1`（爆拉日）。**只补爆拉日算不出区分度**——
    区分度 = 爆拉组命中率 − 对照组命中率，没有对照组就没有分母。
    所以要做完整检验得 `--labels 1,0`（1,402 份，约 0.3GB）。
    """
    p = DATA / "hit_rate_current.csv"
    if not p.exists():
        raise SystemExit(f"缺 {p}；先跑 research/11_hit_rate_current.py")
    want = {int(x) for x in str(labels).replace("，", ",").split(",") if x.strip() != ""}
    df = pd.read_csv(p, usecols=["symbol", "pre_day", "label"])
    df = df[df["label"].isin(want)].drop_duplicates(subset=["symbol", "pre_day"])
    out = []
    for r in df.itertuples(index=False):
        day = str(r.pre_day)[:10]
        out.append({"symbol": r.symbol, "day": day, "label": int(r.label),
                    "key": f"{PREFIX}/{r.symbol}/{r.symbol}-bookDepth-{day}.zip",
                    "dest": f"{r.symbol}/{r.symbol}-{day}.parquet"})
    return sorted(out, key=lambda x: (x["symbol"], x["day"]))


def cmd_plan(args) -> int:
    ts = tasks(args.labels)
    print(f"[plan] 待补 {len(ts)} 个「合约-日」")
    have = sum(1 for t in ts if (OUT / t["dest"]).exists())
    print(f"[plan] 本地已有 {have}｜缺 {len(ts) - have}")
    print(f"[plan] 体积估算：按每份约 0.2MB → 约 {len(ts) * 0.2 / 1024:.1f}GB")
    syms = sorted({t["symbol"] for t in ts})
    days = sorted({t["day"] for t in ts})
    print(f"[plan] 涉及 {len(syms)} 个合约、{len(days)} 个不同日期（{days[0]} ~ {days[-1]}）")
    if args.write_manifest:
        MANIFEST.write_text(json.dumps(
            {"generated_at": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
             "n": len(ts), "tasks": ts}, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"[plan] 清单 → {MANIFEST.relative_to(ROOT)}")
    return 0


def _one(s: requests.Session, t: dict, timeout: int = 90) -> tuple[str, int, str]:
    dest = OUT / t["dest"]
    if dest.exists() and dest.stat().st_size > 0:
        return "skip", 0, ""
    url = f"{S3}/{t['key']}"
    for attempt in (1, 2, 3):
        try:
            r = s.get(url, timeout=timeout)
            if r.status_code == 404:
                return "missing", 0, "404"
            r.raise_for_status()
            z = zipfile.ZipFile(io.BytesIO(r.content))
            name = z.namelist()[0]
            df = pd.read_csv(io.BytesIO(z.read(name)))
            dest.parent.mkdir(parents=True, exist_ok=True)
            df.to_parquet(dest, index=False)
            return "ok", len(df), ""
        except Exception as e:  # noqa: BLE001
            if attempt == 3:
                return "fail", 0, f"{type(e).__name__}: {e}"[:150]
            time.sleep(1.5 * attempt)
    return "fail", 0, ""


def cmd_fetch(args) -> int:
    ts = tasks(args.labels)
    if args.limit:
        ts = ts[:args.limit]
    print(f"[fetch] 任务 {len(ts)}｜并发 {args.workers}")
    stat: dict[str, int] = {}
    rows = 0
    fails: list[str] = []
    t0 = time.time()
    s = session()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(_one, s, t): t for t in ts}
        for i, f in enumerate(as_completed(futs), 1):
            kind, n, err = f.result()
            stat[kind] = stat.get(kind, 0) + 1
            rows += n
            if kind == "fail":
                fails.append(f"{futs[f]['dest']} :: {err}")
            if i % 100 == 0:
                print(f"  {i}/{len(ts)}  {stat}  {time.time()-t0:.0f}s")
    print(f"[fetch] 完成 {stat}｜新增 {rows:,} 行｜{time.time()-t0:.0f}s")
    if fails:
        print(f"[fetch] 失败 {len(fails)} 个（前 5 条）：")
        for x in fails[:5]:
            print("   ", x)
    return 0 if not fails else 1


def cmd_verify(args) -> int:
    ts = tasks(args.labels)
    have = [t for t in ts if (OUT / t["dest"]).exists()]
    miss = [t for t in ts if not (OUT / t["dest"]).exists()]
    print(f"[verify] 应有 {len(ts)}｜已有 {len(have)}（{len(have)/len(ts):.1%}）｜缺 {len(miss)}")
    if have:
        sample = have[:3]
        for t in sample:
            df = pd.read_parquet(OUT / t["dest"])
            print(f"  {t['dest']}: {len(df):,} 行  列={list(df.columns)}  "
                  f"快照数={df['timestamp'].nunique()}")
    if miss:
        print(f"  缺失样例：{[t['dest'] for t in miss[:5]]}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="M12 bookDepth 回补")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan"); p.add_argument("--write-manifest", action="store_true")
    p.add_argument("--labels", default="1")
    p.set_defaults(func=cmd_plan)
    f = sub.add_parser("fetch")
    f.add_argument("--workers", type=int, default=8)
    f.add_argument("--limit", type=int, default=0)
    f.add_argument("--labels", default="1,0")
    f.set_defaults(func=cmd_fetch)
    v = sub.add_parser("verify"); v.add_argument("--labels", default="1,0")
    v.set_defaults(func=cmd_verify)
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
