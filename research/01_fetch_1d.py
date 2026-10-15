#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""01 · 抓取全量 USDT 现货日线（data.binance.vision 公开数据，含已下架交易对）

复用自「裸K盘感项目/scripts/fetch_data.py」的解析逻辑：
  - 2021 年及更早的 CSV 无表头，按首字符是否为数字判断
  - 2025 年起部分文件时间戳为微秒，按量级 >1e14 归一到毫秒
  - ZIP 内单个 CSV，命名 <SYM>-<INTERVAL>-<YYYY-MM>.csv

用法：
  py -3.10 research/01_fetch_1d.py plan           # 只列举，产出计划
  py -3.10 research/01_fetch_1d.py fetch --limit 30
  py -3.10 research/01_fetch_1d.py fetch
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
from xml.etree import ElementTree as ET

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RAW = DATA / "1d"
PLAN = DATA / "plan_1d.json"

S3 = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
CDN = "https://data.binance.vision"
KLINE = "data/spot/monthly/klines"
NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}

COLS = ["open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore"]

# 杠杆代币是每日再平衡的 3 倍产品，不是研究对象
LEVERAGED = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")
STABLE = {"USDCUSDT", "FDUSDUSDT", "TUSDUSDT", "BUSDUSDT", "DAIUSDT", "USDPUSDT",
          "EURUSDT", "AEURUSDT", "PYUSDUSDT", "USD1USDT", "USDEUSDT", "XUSDUSDT",
          "USDSUSDT", "EURIUSDT", "PAXGUSDT"}

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "chuanmu-research/0.1"})
try:
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    _ad = HTTPAdapter(max_retries=Retry(total=3, backoff_factor=0.5,
                                        status_forcelist=[429, 500, 502, 503, 504]),
                      pool_connections=48, pool_maxsize=48)
    SESSION.mount("https://", _ad)
except Exception:  # noqa: BLE001
    pass

START_MONTH = "2022-06"   # 预留 6 个月做指标预热（EMA50/箱体24 都需要前置数据）


def universe() -> list[str]:
    src = Path(r"C:/Users/Administrator/Desktop/裸K盘感项目/data/symbols_all.txt")
    syms = [l.strip() for l in src.read_text(encoding="utf-8").splitlines() if l.strip()]
    out = []
    for s in syms:
        if not s.endswith("USDT") or s == "USDTUSDT":
            continue
        if s.endswith(LEVERAGED) or s in STABLE:
            continue
        out.append(s)
    return sorted(set(out))


def months_of(sym: str, interval: str = "1d") -> tuple[str, list[str]]:
    p = {"list-type": "2", "prefix": f"{KLINE}/{sym}/{interval}/", "max-keys": "1000"}
    try:
        r = SESSION.get(S3, params=p, timeout=30)
        if r.status_code != 200:
            return sym, []
        root = ET.fromstring(r.text)
        keys = [c.find("s3:Key", NS).text for c in root.findall(".//s3:Contents", NS)]
        ms = []
        for k in keys:
            if not k.endswith(".zip"):
                continue
            parts = k.rsplit("-", 2)
            ms.append(parts[-2] + "-" + parts[-1].replace(".zip", ""))
        return sym, sorted(set(ms))
    except Exception:  # noqa: BLE001
        return sym, []


def cmd_plan(args):
    syms = universe()
    print(f"[universe] USDT 现货（已剔除杠杆代币/稳定币）: {len(syms)} 个")
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=24) as ex:
        res = dict(ex.map(months_of, syms))
    plan = {s: m for s, m in res.items() if m}
    plan = {s: [m for m in ms if m >= START_MONTH] for s, ms in plan.items()}
    plan = {s: ms for s, ms in plan.items() if ms}
    PLAN.parent.mkdir(parents=True, exist_ok=True)
    PLAN.write_text(json.dumps(plan, indent=1, ensure_ascii=False), encoding="utf-8")
    n_files = sum(len(m) for m in plan.values())
    print(f"[plan] {len(plan)}/{len(syms)} 个有数据（{START_MONTH} 起）"
          f" | 币·月文件 {n_files:,} | 列举用时 {time.time()-t0:.0f}s")
    print(f"       -> {PLAN}")
    return 0


def _parse_zip(content: bytes):
    z = zipfile.ZipFile(io.BytesIO(content))
    names = [n for n in z.namelist() if n.endswith(".csv")]
    if not names:
        return None
    with z.open(names[0]) as fh:
        raw = fh.read().decode("utf-8", "replace")
    lines = [l for l in raw.splitlines() if l.strip()]
    if lines and not lines[0][:1].isdigit():
        lines = lines[1:]
    if not lines:
        return None
    ncol = len(lines[0].split(","))
    if ncol < 11:
        return None
    use = COLS[: min(ncol, 12)]
    df = pd.read_csv(io.StringIO("\n".join(lines)), header=None, names=use,
                     on_bad_lines="skip", engine="c")
    df = df[[c for c in COLS[:11] if c in df.columns]].copy()
    for c in df.columns:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["open_time", "close"])
    if df.empty:
        return None
    if df["open_time"].iloc[0] > 1e14:
        df["open_time"] = df["open_time"] // 1000
        df["close_time"] = df["close_time"] // 1000
    df["open_time"] = df["open_time"].astype("int64")
    return df


def parquet_months(dst: Path) -> set[str]:
    """本地 parquet 实际覆盖的月份集合（判「完整」的真依据，不靠文件是否存在）。"""
    if not dst.exists():
        return set()
    try:
        d = pd.read_parquet(dst, columns=["open_time"])
    except Exception:  # noqa: BLE001
        return set()
    if not len(d):
        return set()
    ts = pd.to_datetime(d["open_time"], unit="ms", utc=True)
    return set(ts.dt.strftime("%Y-%m").unique())


def missing_months(sym: str, months: list[str], dst: Path) -> list[str]:
    """按 `data/plan_1d.json` 的月份列表逐月校验，返回真正缺的月份。"""
    have = parquet_months(dst)
    return [m for m in months if m not in have]


def cmd_fetch(args):
    plan_path = Path(args.plan) if getattr(args, "plan", None) else PLAN
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    raw = Path(args.raw_dir) if getattr(args, "raw_dir", None) else RAW
    journal = Path(args.journal) if getattr(args, "journal", None) else (DATA / "state_1d.jsonl")
    dry = bool(getattr(args, "dry_run", False))
    raw.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    # ① 先按月校验完整性 → 挑出真正未完成的币（修复「先截 limit 再跳过」的空转）
    work = []
    complete = 0
    for sym in plan:
        dst = raw / f"{sym}.parquet"
        miss = missing_months(sym, plan[sym], dst)
        if not miss and dst.exists() and not args.force:
            complete += 1
            continue
        work.append((sym, miss if not args.force else list(plan[sym])))
    if args.limit:
        work = work[: args.limit]

    print(f"[fetch] 计划 {len(plan)} 币 | 已完整 {complete} | 本次待办 {len(work)}"
          f" | 缺月合计 {sum(len(m) for _, m in work):,}")
    if dry:
        for sym, miss in work[:50]:
            print(f"   would fetch {sym:<16} 缺 {len(miss)} 月: {','.join(miss[:6])}"
                  f"{'...' if len(miss) > 6 else ''}")
        return 0

    done = 0
    nfiles = 0
    failed_months = 0

    def one_month(sym, m):
        out = f"{CDN}/{KLINE}/{sym}/1d/{sym}-1d-{m}.zip"
        try:
            r = SESSION.get(out, timeout=90)
            if r.status_code != 200 or len(r.content) < 200:
                return m, None
            return m, _parse_zip(r.content)
        except Exception:  # noqa: BLE001
            return m, None

    for i, (sym, months) in enumerate(work, 1):
        dst = raw / f"{sym}.parquet"
        frames = []
        if dst.exists():
            try:                                  # 保留已有行，只补缺月（不覆盖原数据）
                frames.append(pd.read_parquet(dst))
            except Exception:  # noqa: BLE001
                pass
        got_months = []
        with ThreadPoolExecutor(max_workers=16) as ex:
            for m, df in ex.map(lambda mm: one_month(sym, mm), months):
                if df is not None and len(df):
                    frames.append(df)
                    got_months.append(m)
        with journal.open("a", encoding="utf-8") as fh:
            for m in months:
                fh.write(json.dumps({"symbol": sym, "month": m,
                                     "status": "ok" if m in got_months else "fail",
                                     "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
                                    ensure_ascii=False) + "\n")
        failed_months += len(months) - len(got_months)
        if not frames:
            continue
        out = pd.concat(frames, ignore_index=True)
        out = out.drop_duplicates(subset="open_time").sort_values("open_time").reset_index(drop=True)
        if "symbol" in out.columns:
            out = out.drop(columns=["symbol"])
        out.insert(0, "symbol", sym)
        out["dt"] = pd.to_datetime(out["open_time"], unit="ms", utc=True)
        tmp = dst.with_suffix(".parquet.tmp")     # 原子替换，避免写坏原文件
        out.to_parquet(tmp, index=False, compression="zstd")
        tmp.replace(dst)
        done += 1
        nfiles += len(got_months)
        if i % 25 == 0 or i <= 5:
            el = time.time() - t0
            print(f"  [{i}/{len(work)}] {sym:<16} +{len(got_months)} 月 | 累计 {len(out):>5} 根"
                  f" | {el:.0f}s")
    print(f"[fetch] 补齐 {done} 币 / 补下 {nfiles:,} 月 / 失败月 {failed_months} "
          f"| 已完整 {complete} | 用时 {time.time()-t0:.0f}s -> {raw}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("plan").set_defaults(func=cmd_plan)
    p = sub.add_parser("fetch")
    p.set_defaults(func=cmd_fetch)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--force", action="store_true")
    p.add_argument("--dry-run", action="store_true",
                   help="只列出本轮会补哪些币、缺哪些月，不发任何请求")
    p.add_argument("--raw-dir", default=None, help="覆盖本地日线目录（隔离测试用）")
    p.add_argument("--journal", default=None, help="覆盖逐月台账路径（隔离测试用）")
    p.add_argument("--plan", default=None, help="覆盖计划文件路径（隔离测试用）")
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
