#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""00 · 统一并行下载器（data.binance.vision 公开数据）

分批并行：每批 24 个币，批内所有「币·月」文件用 48 线程并发下载，
下完立刻落 parquet 并释放内存。断点续跑：已存在则跳过。

子命令：
  klines1d      现货日线（全量 USDT 池）
  klines1h      现货 1h（只下指定币的指定月份，见 --symbols/--months）
  metrics       合约 metrics（OI + 多空比，按天）
  funding       合约资金费率（按月）
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CDN = "https://data.binance.vision"

SPOT_COLS = ["open_time", "open", "high", "low", "close", "volume",
             "close_time", "quote_volume", "trades",
             "taker_buy_base", "taker_buy_quote", "ignore"]

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "chuanmu-research/0.1"})
try:
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    _ad = HTTPAdapter(max_retries=Retry(total=3, backoff_factor=0.4,
                                        status_forcelist=[429, 500, 502, 503, 504]),
                      pool_connections=64, pool_maxsize=64)
    SESSION.mount("https://", _ad)
except Exception:  # noqa: BLE001
    pass


def _get(url: str, timeout: int = 90) -> bytes | None:
    try:
        r = SESSION.get(url, timeout=timeout)
        if r.status_code != 200 or len(r.content) < 100:
            return None
        return r.content
    except Exception:  # noqa: BLE001
        return None


def _parse_kline_zip(content: bytes, cols: list[str]) -> pd.DataFrame | None:
    try:
        z = zipfile.ZipFile(io.BytesIO(content))
        names = [n for n in z.namelist() if n.endswith(".csv")]
        if not names:
            return None
        with z.open(names[0]) as fh:
            raw = fh.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return None
    lines = [l for l in raw.splitlines() if l.strip()]
    if lines and not lines[0][:1].isdigit():
        lines = lines[1:]
    if not lines:
        return None
    ncol = len(lines[0].split(","))
    if ncol < 11:
        return None
    use = cols[: min(ncol, 12)]
    df = pd.read_csv(io.StringIO("\n".join(lines)), header=None, names=use,
                     on_bad_lines="skip", engine="c")
    df = df[[c for c in cols[:11] if c in df.columns]].copy()
    for c in df.columns:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["open_time", "close"])
    if df.empty:
        return None
    if df["open_time"].iloc[0] > 1e14:      # 2025 起部分文件为微秒
        df["open_time"] = df["open_time"] // 1000
    df["open_time"] = df["open_time"].astype("int64")
    return df


def _batch_download(tasks: list[tuple[str, str, str]], out_dir: Path,
                    workers: int = 48, cols: list[str] | None = None) -> tuple[int, int]:
    """tasks: [(symbol, month_or_day, url)]。返回 (ok, fail)。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    cols = cols or SPOT_COLS

    def one(t):
        sym, tag, url = t
        dst = out_dir / f"{sym}.parquet"
        if dst.exists():
            return "skip", sym, tag, None
        blob = _get(url)
        if blob is None:
            return "fail", sym, tag, None
        df = _parse_kline_zip(blob, cols)
        if df is None or df.empty:
            return "fail", sym, tag, None
        return "ok", sym, tag, df

    ok = fail = 0
    buf: dict[str, list[pd.DataFrame]] = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for st, sym, tag, df in ex.map(one, tasks):
            if st == "skip":
                continue
            if st == "fail":
                fail += 1
                continue
            ok += 1
            buf.setdefault(sym, []).append(df)
    for sym, frames in buf.items():
        out = pd.concat(frames, ignore_index=True)
        out = out.drop_duplicates(subset="open_time").sort_values("open_time").reset_index(drop=True)
        out.insert(0, "symbol", sym)
        out["dt"] = pd.to_datetime(out["open_time"], unit="ms", utc=True)
        out.to_parquet(out_dir / f"{sym}.parquet", index=False, compression="zstd")
    return ok, fail


def cmd_klines1d(args):
    plan = json.loads((DATA / "plan_1d.json").read_text(encoding="utf-8"))
    out_dir = DATA / "1d"
    todo = [s for s in plan if not (out_dir / f"{s}.parquet").exists()]
    print(f"[1d] 待处理 {len(todo)}/{len(plan)} 个币")
    t0, done = time.time(), 0
    BATCH = 24
    for i in range(0, len(todo), BATCH):
        grp = todo[i:i + BATCH]
        tasks = [(s, m, f"{CDN}/data/spot/monthly/klines/{s}/1d/{s}-1d-{m}.zip")
                 for s in grp for m in plan[s]]
        ok, fail = _batch_download(tasks, out_dir, args.workers)
        done += len(grp)
        print(f"  [{done}/{len(todo)}] 批 {i//BATCH+1} | 文件 ok={ok} fail={fail} | {time.time()-t0:.0f}s")
    print(f"[1d] 完成 -> {out_dir} | 用时 {time.time()-t0:.0f}s")
    return 0


def cmd_klines1h(args):
    """现货 1h：按「币·月」单独落盘，便于按窗口精确读取。"""
    jobs = json.loads(Path(args.jobs).read_text(encoding="utf-8"))
    out_dir = DATA / "1h"
    out_dir.mkdir(parents=True, exist_ok=True)
    tasks = [(j["symbol"], j["month"],
              f"{CDN}/data/spot/monthly/klines/{j['symbol']}/1h/{j['symbol']}-1h-{j['month']}.zip")
             for j in jobs]
    t0 = time.time()

    def one(t):
        sym, mon, url = t
        dst = out_dir / f"{sym}-{mon}.parquet"
        if dst.exists():
            return "skip"
        blob = _get(url)
        if blob is None:
            return "fail"
        df = _parse_kline_zip(blob, SPOT_COLS)
        if df is None or df.empty:
            return "fail"
        df.insert(0, "symbol", sym)
        df["dt"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
        df.to_parquet(dst, index=False, compression="zstd")
        return "ok"

    ok = fail = skip = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for i, st in enumerate(ex.map(one, tasks), 1):
            if st == "ok":
                ok += 1
            elif st == "fail":
                fail += 1
            else:
                skip += 1
            if i % 500 == 0:
                print(f"   {i}/{len(tasks)} ok={ok} fail={fail} skip={skip}")
    print(f"[1h] 任务 {len(tasks)} | ok={ok} fail={fail} skip={skip} | {time.time()-t0:.0f}s -> {out_dir}")
    return 0


def cmd_metrics(args):
    """合约 metrics：OI + 多空比，5 分钟粒度，按天下载。"""
    jobs = json.loads(Path(args.jobs).read_text(encoding="utf-8"))
    out_dir = DATA / "metrics"
    out_dir.mkdir(parents=True, exist_ok=True)
    tasks = []
    for j in jobs:
        s, d = j["symbol"], j["day"]
        tasks.append((s, d, f"{CDN}/data/futures/um/daily/metrics/{s}/{s}-metrics-{d}.zip"))
    t0 = time.time()

    def one(t):
        sym, day, url = t
        dst = out_dir / f"{sym}-{day}.parquet"
        if dst.exists():
            return "skip", None
        blob = _get(url)
        if blob is None:
            return "fail", None
        try:
            z = zipfile.ZipFile(io.BytesIO(blob))
            nm = [n for n in z.namelist() if n.endswith(".csv")]
            if not nm:
                return "fail", None
            txt = z.read(nm[0]).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return "fail", None
        lines = [l for l in txt.splitlines() if l.strip()]
        if not lines:
            return "fail", None
        df = pd.read_csv(io.StringIO("\n".join(lines)))
        if df.empty:
            return "fail", None
        for c in df.columns:
            if c not in ("create_time", "symbol"):
                df[c] = pd.to_numeric(df[c], errors="coerce")
        df["dt"] = pd.to_datetime(df["create_time"], utc=True)
        df.to_parquet(dst, index=False, compression="zstd")
        return "ok", None

    ok = fail = skip = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for i, (st, _) in enumerate(ex.map(one, tasks), 1):
            if st == "ok":
                ok += 1
            elif st == "fail":
                fail += 1
            else:
                skip += 1
            if i % 500 == 0:
                print(f"   {i}/{len(tasks)} ok={ok} fail={fail} skip={skip}")
    print(f"[metrics] 任务 {len(tasks)} | ok={ok} fail={fail} skip={skip} | {time.time()-t0:.0f}s")
    return 0


def cmd_funding(args):
    jobs = json.loads(Path(args.jobs).read_text(encoding="utf-8"))
    out_dir = DATA / "funding"
    out_dir.mkdir(parents=True, exist_ok=True)
    tasks = [(j["symbol"], j["month"],
              f"{CDN}/data/futures/um/monthly/fundingRate/{j['symbol']}/{j['symbol']}-fundingRate-{j['month']}.zip")
             for j in jobs]
    t0 = time.time()

    def one(t):
        sym, mon, url = t
        dst = out_dir / f"{sym}.parquet"
        if dst.exists():
            return "skip", sym, None
        blob = _get(url)
        if blob is None:
            return "fail", sym, None
        try:
            z = zipfile.ZipFile(io.BytesIO(blob))
            nm = [n for n in z.namelist() if n.endswith(".csv")]
            txt = z.read(nm[0]).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return "fail", sym, None
        lines = [l for l in txt.splitlines() if l.strip()]
        if lines and not lines[0][:1].isdigit():
            lines = lines[1:]
        if not lines:
            return "fail", sym, None
        df = pd.read_csv(io.StringIO("\n".join(lines)))
        return "ok", sym, df

    ok = fail = 0
    buf: dict[str, list[pd.DataFrame]] = {}
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for st, sym, df in ex.map(one, tasks):
            if st == "ok":
                ok += 1
                buf.setdefault(sym, []).append(df)
            elif st == "fail":
                fail += 1
    for sym, frames in buf.items():
        out = pd.concat(frames, ignore_index=True)
        out.to_parquet(out_dir / f"{sym}.parquet", index=False, compression="zstd")
    print(f"[funding] 任务 {len(tasks)} | ok={ok} fail={fail} | {time.time()-t0:.0f}s -> {out_dir}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=48)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("klines1d").set_defaults(func=cmd_klines1d)
    for name, fn in (("klines1h", cmd_klines1h), ("metrics", cmd_metrics), ("funding", cmd_funding)):
        p = sub.add_parser(name)
        p.set_defaults(func=fn)
        p.add_argument("--jobs", required=True)
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
