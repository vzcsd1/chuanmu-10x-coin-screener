#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""05 · 合约侧特征（OI 增速 / 多空比 / 资金费率）—— 项目当前完全缺失的维度

数据源（全部免费、无需 API Key）：
  metrics   data/futures/um/daily/metrics/<SYM>/<SYM>-metrics-YYYY-MM-DD.zip
            5 分钟粒度，含 sum_open_interest / sum_open_interest_value /
            count_long_short_ratio / sum_toptrader_long_short_ratio / sum_taker_long_short_vol_ratio
  funding   data/futures/um/monthly/fundingRate/<SYM>/<SYM>-fundingRate-YYYY-MM.zip

为避免产生上万个临时文件，逐日下载后立即聚合为紧凑摘要，只保留最终特征表。
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

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CDN = "https://data.binance.vision"

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

MET_COLS = ["sum_open_interest", "sum_open_interest_value",
            "count_toptrader_long_short_ratio", "sum_toptrader_long_short_ratio",
            "count_long_short_ratio", "sum_taker_long_short_vol_ratio"]

WINDOW_BACK = 10      # 评估日前 N 天
WINDOW_FWD = 2


def _get(url: str) -> bytes | None:
    try:
        r = SESSION.get(url, timeout=60)
        if r.status_code != 200 or len(r.content) < 100:
            return None
        return r.content
    except Exception:  # noqa: BLE001
        return None


def _metrics_day(sym: str, day: str) -> dict | None:
    """取某一天的紧凑摘要。"""
    url = f"{CDN}/data/futures/um/daily/metrics/{sym}/{sym}-metrics-{day}.zip"
    blob = _get(url)
    if blob is None:
        return None
    try:
        z = zipfile.ZipFile(io.BytesIO(blob))
        nm = [n for n in z.namelist() if n.endswith(".csv")]
        if not nm:
            return None
        txt = z.read(nm[0]).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return None
    lines = [l for l in txt.splitlines() if l.strip()]
    if len(lines) < 2:
        return None
    df = pd.read_csv(io.StringIO("\n".join(lines)))
    for c in MET_COLS:
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    if "sum_open_interest_value" not in df or df["sum_open_interest_value"].dropna().empty:
        return None
    out = {"symbol": sym, "day": day}
    out["oi_val_last"] = float(df["sum_open_interest_value"].dropna().iloc[-1])
    out["oi_val_mean"] = float(df["sum_open_interest_value"].mean())
    out["oi_amt_last"] = float(df["sum_open_interest"].dropna().iloc[-1]) if "sum_open_interest" in df else np.nan
    for c, tag in (("count_long_short_ratio", "ls_count"),
                   ("sum_toptrader_long_short_ratio", "ls_top"),
                   ("sum_taker_long_short_vol_ratio", "taker")):
        if c in df and df[c].notna().any():
            out[f"{tag}_last"] = float(df[c].dropna().iloc[-1])
            out[f"{tag}_mean"] = float(df[c].mean())
        else:
            out[f"{tag}_last"] = np.nan
            out[f"{tag}_mean"] = np.nan
    return out


def _funding(sym: str, months: list[str]) -> pd.DataFrame | None:
    frames = []
    for m in months:
        url = f"{CDN}/data/futures/um/monthly/fundingRate/{sym}/{sym}-fundingRate-{m}.zip"
        blob = _get(url)
        if blob is None:
            continue
        try:
            z = zipfile.ZipFile(io.BytesIO(blob))
            nm = [n for n in z.namelist() if n.endswith(".csv")]
            txt = z.read(nm[0]).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            continue
        lines = [l for l in txt.splitlines() if l.strip()]
        if not lines:
            continue
        has_header = not lines[0][:1].isdigit()
        if has_header:
            frames.append(pd.read_csv(io.StringIO("\n".join(lines))))
        else:
            frames.append(pd.read_csv(io.StringIO("\n".join(lines)), header=None,
                                      names=["calc_time", "funding_interval_hours",
                                             "last_funding_rate"]))
    if not frames:
        return None
    df = pd.concat(frames, ignore_index=True)
    ct = pd.to_numeric(df["calc_time"], errors="coerce")
    if ct.dropna().iloc[0] > 1e11:                 # 毫秒时间戳
        df["dt"] = pd.to_datetime(ct, unit="ms", utc=True)
    else:
        df["dt"] = pd.to_datetime(df["calc_time"], utc=True)
    return df.sort_values("dt").reset_index(drop=True)


def _months_around(d: pd.Timestamp, back: int, fwd: int) -> list[str]:
    a = (d - pd.Timedelta(days=back)).replace(day=1)
    b = d + pd.Timedelta(days=fwd)
    out, cur = [], a
    while cur <= b:
        out.append(f"{cur.year:04d}-{cur.month:02d}")
        cur = cur + pd.offsets.MonthBegin(1)
    return sorted(set(out))


def _days_around(d: pd.Timestamp, back: int, fwd: int) -> list[str]:
    return [(d + pd.Timedelta(days=k)).strftime("%Y-%m-%d") for k in range(-back, fwd + 1)]


def build_samples():
    pumps = json.loads((DATA / "pumps.json").read_text(encoding="utf-8"))
    pumps = [e for e in pumps if e["gain"] >= 2.0]        # 3x 以上，聚焦强样本
    controls = json.loads((DATA / "control_days.json").read_text(encoding="utf-8"))
    samples = [{"symbol": e["symbol"], "pre_day": e["pre_day"], "label": 1,
                "gain": e["gain"], "peak_date": e["peak_date"]} for e in pumps]
    samples += [{"symbol": c["symbol"], "pre_day": c["pre_day"], "label": 0,
                 "gain": 0.0, "peak_date": None} for c in controls]
    return samples


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=48)
    args = ap.parse_args()

    samples = build_samples()
    n_p = sum(1 for s in samples if s["label"] == 1)
    print(f"[deriv] 样本 {len(samples)}（爆拉 {n_p} / 对照 {len(samples)-n_p}）")

    # 1) metrics：全局任务池
    tasks = []
    for s in samples:
        d = pd.Timestamp(s["pre_day"])
        for day in _days_around(d, WINDOW_BACK, WINDOW_FWD):
            tasks.append((s["symbol"], day))
    tasks = sorted(set(tasks))
    print(f"[deriv] metrics 日文件任务 {len(tasks):,}")

    t0 = time.time()
    cache_path = DATA / "metrics_cache.parquet"
    cache: dict[tuple[str, str], dict | None] = {}
    if cache_path.exists():
        old = pd.read_parquet(cache_path)
        for rec in old.to_dict("records"):
            cache[(rec["symbol"], rec["day"])] = rec
        print(f"[deriv] 复用缓存 {len(cache):,} 条")
    pending = [t for t in tasks if t not in cache]
    print(f"[deriv] 需新下载 {len(pending):,}")
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for i, (key, res) in enumerate(
                zip(pending, ex.map(lambda t: _metrics_day(*t), pending)), 1):
            cache[key] = res
            if i % 2000 == 0:
                print(f"   metrics {i}/{len(pending)} | {time.time()-t0:.0f}s")
    ok = sum(1 for v in cache.values() if v)
    print(f"[deriv] metrics 成功 {ok}/{len(cache)} | {time.time()-t0:.0f}s")
    good = [v for v in cache.values() if v]
    if good:
        pd.DataFrame(good).to_parquet(cache_path, index=False, compression="zstd")

    # 2) funding：按月，量小 —— 加本地缓存，避免每次重跑都重新下载几百个文件
    fund_jobs = sorted({(s["symbol"], m) for s in samples
                        for m in _months_around(pd.Timestamp(s["pre_day"]), WINDOW_BACK, WINDOW_FWD)})
    print(f"[deriv] funding 月文件任务 {len(fund_jobs):,}")
    fund_cache: dict[str, pd.DataFrame] = {}
    fcache_path = DATA / "funding_cache.parquet"
    if fcache_path.exists():
        try:
            fc = pd.read_parquet(fcache_path)
            for sym, g in fc.groupby("symbol"):
                fund_cache[str(sym)] = g.drop(columns=["symbol"]).reset_index(drop=True)
            print(f"[deriv] funding 复用缓存 {len(fund_cache)} 个币")
        except Exception as exc:  # noqa: BLE001
            print(f"[deriv] funding 缓存读取失败，改为重新下载：{exc}")
    todo = [s for s in sorted({s["symbol"] for s in samples}) if s not in fund_cache]
    print(f"[deriv] funding 需新下载 {len(todo)} 个币")
    for sym in todo:
        ms = sorted({m for (sy, m) in fund_jobs if sy == sym})
        df = _funding(sym, ms)
        if df is not None:
            fund_cache[sym] = df
    print(f"[deriv] funding 覆盖 {len(fund_cache)} 个币")
    frames = [g.assign(symbol=s) for s, g in fund_cache.items() if len(g)]
    if frames:
        pd.concat(frames, ignore_index=True).to_parquet(fcache_path, index=False, compression="zstd")

    # 3) 聚合
    rows = []
    for s in samples:
        sym, d = s["symbol"], pd.Timestamp(s["pre_day"])
        days = _days_around(d, WINDOW_BACK, WINDOW_FWD)
        per = {day: cache.get((sym, day)) for day in days}
        cur = per.get(d.strftime("%Y-%m-%d"))
        if not cur:
            continue
        row = {"symbol": sym, "pre_day": s["pre_day"], "label": s["label"], "gain": s["gain"]}

        def oi(k):
            v = per.get(k)
            return v["oi_val_last"] if v else np.nan

        cur_oi = cur["oi_val_last"]
        row["oi_value"] = cur_oi
        for back, tag in ((1, "1d"), (3, "3d"), (7, "7d")):
            prev = oi((d - pd.Timedelta(days=back)).strftime("%Y-%m-%d"))
            row[f"oi_chg_{tag}"] = (cur_oi / prev - 1.0) if (prev and np.isfinite(prev) and prev > 0) else np.nan
        # 未来 2 天 OI 变化（看是否已在启动）
        fwd = oi((d + pd.Timedelta(days=2)).strftime("%Y-%m-%d"))
        row["oi_chg_fwd2d"] = (fwd / cur_oi - 1.0) if (fwd and np.isfinite(fwd) and cur_oi) else np.nan

        row["ls_count"] = cur.get("ls_count_last")
        row["ls_top"] = cur.get("ls_top_last")
        row["taker"] = cur.get("taker_last")
        prev3 = per.get((d - pd.Timedelta(days=3)).strftime("%Y-%m-%d"))
        row["ls_count_chg_3d"] = (cur.get("ls_count_last") - prev3.get("ls_count_last")) \
            if prev3 and cur.get("ls_count_last") is not None and prev3.get("ls_count_last") is not None else np.nan
        # 大户持仓多空比的 3 日变化 —— 与实盘 fetch_deriv_context 的 ls_chg_3d 同口径
        row["ls_top_chg_3d"] = (cur.get("ls_top_last") - prev3.get("ls_top_last")) \
            if prev3 and cur.get("ls_top_last") is not None and prev3.get("ls_top_last") is not None else np.nan

        # 资金费率
        fr = fund_cache.get(sym)
        if fr is not None:
            d_utc = pd.Timestamp(s["pre_day"], tz="UTC")
            hist = fr[fr["dt"] <= d_utc + pd.Timedelta(hours=23, minutes=59)]
            last7 = hist[hist["dt"] > d_utc - pd.Timedelta(days=7)]
            row["funding_last"] = float(hist["last_funding_rate"].iloc[-1]) if len(hist) else np.nan
            row["funding_mean_7d"] = float(last7["last_funding_rate"].mean()) if len(last7) else np.nan
            row["funding_neg_ratio_7d"] = float((last7["last_funding_rate"] < 0).mean()) if len(last7) else np.nan
            row["funding_min_7d"] = float(last7["last_funding_rate"].min()) if len(last7) else np.nan
        rows.append(row)

    out = pd.DataFrame(rows)
    out.to_csv(DATA / "features_deriv.csv", index=False, encoding="utf-8-sig")
    print(f"[deriv] 有效样本 {len(out)} -> {DATA/'features_deriv.csv'}")
    print(out["label"].value_counts().to_dict())
    return 0


if __name__ == "__main__":
    sys.exit(main())
