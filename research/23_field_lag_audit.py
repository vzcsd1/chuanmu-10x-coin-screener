#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""23 · M69 字段可得时点审计（发布延迟 + as-of 一致性 + 前视检查）

为什么要有这个脚本
------------------
M69 是「92 机制」里最便宜的地基：如果某个字段是**事后补的**，那所有用它算出来的结论
都用了「当时看不到的信息」，比「指标不好用」严重一个量级。

三个问题，分开回答
------------------
1. **发布延迟**：归档里 day D 的文件，什么时候才出现？（用 HTTP HEAD 的 Last-Modified 实测）
2. **as-of 一致性**：算特征时，用的是 day D 的**哪个时刻**的快照？
   —— `05_deriv_features.py:86` 用 `df[...].iloc[-1]`，但**日度 metrics 文件不按时间排序**，
   所以那是「文件里最后一行」，时刻是随机的。
3. **前视**：决策点是 `pre_day 23:00 UTC`（`03_build_features.py` 的 `iloc[-2]` = 22:00 那根收盘）。
   任何晚于 23:00 的取值都是前视。

命令
----
    py -3.10 research/23_field_lag_audit.py lag
    py -3.10 research/23_field_lag_audit.py asof   --n 300
    py -3.10 research/23_field_lag_audit.py impact --n 1402

产物
----
    data/m69_metrics_cache.parquet   每个 (symbol, day) 的多种 as-of 取值
    reports/m69_lag_ds/report.md     人话结论（由 --write 生成，默认只打印）
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
CACHE = DATA / "m69_metrics_cache.parquet"
CDN = "https://data.binance.vision"

UA = {"User-Agent": "chuanmu-research/0.1"}
NQ = 5


# ------------------------------------------------------------------ 通用

def _get(url: str, timeout: int = 60) -> bytes | None:
    try:
        req = urllib.request.Request(url, headers=UA)
        return urllib.request.urlopen(req, timeout=timeout).read()
    except Exception:  # noqa: BLE001
        return None


def _head(url: str) -> dict:
    """只取响应头，不下载正文。"""
    try:
        req = urllib.request.Request(url, headers=UA, method="HEAD")
        with urllib.request.urlopen(req, timeout=40) as r:
            return {"ok": True, "last_modified": r.headers.get("Last-Modified"),
                    "length": r.headers.get("Content-Length"), "code": r.status}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "err": str(exc)[:60]}


def _rank_auc(y: np.ndarray, x: np.ndarray) -> float:
    """Mann-Whitney AUC，自动跳过 NaN。"""
    m = np.isfinite(x) & np.isfinite(y)
    if m.sum() < 30:
        return float("nan")
    yy, xx = y[m], x[m]
    if yy.min() == yy.max():
        return float("nan")
    r = pd.Series(xx).rank().to_numpy()
    n1 = int((yy == 1).sum())
    n0 = int((yy == 0).sum())
    if n1 == 0 or n0 == 0:
        return float("nan")
    return float((r[yy == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def _spread(x: pd.Series, y: pd.Series) -> float:
    """上下 1/NQ 分位的爆拉率差（pp）。"""
    m = x.notna() & y.notna()
    if m.sum() < 50:
        return float("nan")
    xx, yy = x[m], y[m]
    try:
        q = pd.qcut(xx, NQ, labels=False, duplicates="drop")
    except ValueError:
        return float("nan")
    lo, hi = yy[q == 0].mean(), yy[q == q.max()].mean()
    return float((hi - lo) * 100)


# ------------------------------------------------------------------ 1. lag

def cmd_lag(rows_out: list | None = None) -> int:
    """各数据集 day D 的文件什么时候出现。"""
    # 最近 6 天 + 两个月
    days = ["2026-10-14", "2026-10-15", "2026-10-16", "2026-10-17",
            "2026-10-18", "2026-10-19", "2026-10-20"]
    kinds = [
        ("metrics 日度（OI/多空比/taker）",
         "data/futures/um/daily/metrics/BTCUSDT/BTCUSDT-metrics-{d}.zip", "daily"),
        ("K线 1d 日度", "data/futures/um/daily/klines/BTCUSDT/1d/BTCUSDT-1d-{d}.zip", "daily"),
        ("K线 1h 日度", "data/futures/um/daily/klines/BTCUSDT/1h/BTCUSDT-1h-{d}.zip", "daily"),
    ]
    monthly = [
        ("fundingRate 月度", "data/futures/um/monthly/fundingRate/BTCUSDT/BTCUSDT-fundingRate-{m}.zip"),
        ("K线 1h 月度", "data/futures/um/monthly/klines/BTCUSDT/1h/BTCUSDT-1h-{m}.zip"),
    ]

    print("=" * 78)
    print("1) 归档发布时刻（HTTP HEAD 的 Last-Modified，全部为 UTC）")
    print("=" * 78)
    print(f"{'数据集':<26} {'数据日':<12} {'发布时刻':<22} {'滞后':>10}")
    print("-" * 78)

    out = []
    for name, pat, _ in kinds:
        for d in days:
            h = _head(f"{CDN}/{pat.format(d=d)}")
            if not h.get("ok") or not h.get("last_modified"):
                print(f"{name:<26} {d:<12} {'—（文件不存在/未发布）':<22} {'':>10}")
                continue
            lm = datetime.strptime(h["last_modified"], "%a, %d %b %Y %H:%M:%S %Z")
            lm = lm.replace(tzinfo=timezone.utc)
            day_end = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            lag_h = (lm - day_end).total_seconds() / 3600
            print(f"{name:<26} {d:<12} {lm.strftime('%Y-%m-%d %H:%M'):<22} {lag_h:>9.1f}h")
            out.append({"dataset": name, "data_date": d,
                        "published_utc": lm.strftime("%Y-%m-%d %H:%M"), "lag_h": round(lag_h, 1)})
        print("-" * 78)

    for name, pat in monthly:
        for m in ["2026-08", "2026-09"]:
            h = _head(f"{CDN}/{pat.format(m=m)}")
            if not h.get("ok") or not h.get("last_modified"):
                print(f"{name:<26} {m:<12} {'—':<22}")
                continue
            lm = datetime.strptime(h["last_modified"], "%a, %d %b %Y %H:%M:%S %Z").replace(tzinfo=timezone.utc)
            month_end = (pd.Timestamp(m) + pd.offsets.MonthEnd(1)).to_pydatetime().replace(tzinfo=timezone.utc)
            lag_h = (lm - month_end).total_seconds() / 3600
            print(f"{name:<26} {m:<12} {lm.strftime('%Y-%m-%d %H:%M'):<22} {lag_h:>9.1f}h")
            out.append({"dataset": name, "data_date": m,
                        "published_utc": lm.strftime("%Y-%m-%d %H:%M"), "lag_h": round(lag_h, 1)})
    print()

    if rows_out is not None:
        rows_out.extend(out)
    return 0


# ------------------------------------------------------------------ 1b. live

def _proxy() -> str | None:
    """读 Windows 系统代理（本机 fapi.binance.com 直连不通，必须走代理）。"""
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        if winreg.QueryValueEx(k, "ProxyEnable")[0]:
            return "http://" + str(winreg.QueryValueEx(k, "ProxyServer")[0])
    except Exception:  # noqa: BLE001
        pass
    return None


def _jget(path: str, proxy: str | None) -> tuple[object, float]:
    """返回 (json, 本机发起时刻的 epoch 秒)。"""
    url = "https://fapi.binance.com" + path
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy}) if proxy
        else urllib.request.ProxyHandler({}))
    opener.addheaders = [("User-Agent", "chuanmu-research/0.1")]
    t0 = datetime.now(timezone.utc).timestamp()
    with opener.open(url, timeout=25) as r:
        return json.loads(r.read().decode()), t0


LIVE_EP = [
    ("OI 历史 5m", "/futures/data/openInterestHist?symbol=BTCUSDT&period=5m&limit=3", "timestamp"),
    ("OI 即时", "/fapi/v1/openInterest?symbol=BTCUSDT", None),
    ("全账户多空比 5m", "/futures/data/globalLongShortAccountRatio?symbol=BTCUSDT&period=5m&limit=3", "timestamp"),
    ("大户账户多空比 5m", "/futures/data/topLongShortAccountRatio?symbol=BTCUSDT&period=5m&limit=3", "timestamp"),
    ("大户持仓多空比 5m", "/futures/data/topLongShortPositionRatio?symbol=BTCUSDT&period=5m&limit=3", "timestamp"),
    ("taker 买卖量比 5m", "/futures/data/takerlongshortRatio?symbol=BTCUSDT&period=5m&limit=3", "timestamp"),
    ("资金费率（即时）", "/fapi/v1/premiumIndex?symbol=BTCUSDT", None),
    ("K线 1h", "/fapi/v1/klines?symbol=BTCUSDT&interval=1h&limit=3", None),
]


def cmd_live(pause: int = 75) -> int:
    """实盘 API 的可得时点：最新桶是不是「已收盘」，以及同一个桶会不会被改写。"""
    proxy = _proxy()
    if not proxy:
        print("[live] 没读到系统代理；fapi.binance.com 直连在本机不通，跳过。")
        return 0
    print(f"[live] 代理 {proxy}")
    try:
        st, _ = _jget("/fapi/v1/time", proxy)
    except Exception as exc:  # noqa: BLE001
        print(f"[live] 连不上 fapi：{exc}")
        return 0
    print(f"[live] 服务器时间 {datetime.fromtimestamp(st['serverTime']/1000, timezone.utc):%Y-%m-%d %H:%M:%S} UTC\n")

    def snap() -> dict:
        out = {}
        for name, path, key in LIVE_EP:
            try:
                js, t0 = _jget(path, proxy)
            except Exception as exc:  # noqa: BLE001
                out[name] = {"err": str(exc)[:40]}
                continue
            rec = {"t_local": t0}
            if key and isinstance(js, list) and js:
                rec["last_bucket"] = int(js[-1][key]) / 1000
                rec["last_row"] = js[-1]
            elif isinstance(js, dict):
                rec["last_row"] = js
            out[name] = rec
        return out

    a = snap()
    print("=" * 84)
    print("6) 实盘 API：最新一根 5m 桶，在「现在」是已收盘还是进行中")
    print("=" * 84)
    print(f"{'接口':<20} {'最新桶时刻(UTC)':<18} {'桶龄':>8} {'状态':<12} {'本机取到时刻(UTC)'}")
    print("-" * 84)
    for name, _, key in LIVE_EP:
        r = a[name]
        if "err" in r:
            print(f"{name:<20} {'—':<18} {'':>8} {'失败':<12} {r['err']}")
            continue
        tl = r["t_local"]
        if key:
            age = tl - r["last_bucket"]
            state = "**进行中（未收盘）**" if age < 300 else f"已收盘 {age-300:.0f}s"
            print(f"{name:<20} {datetime.fromtimestamp(r['last_bucket'], timezone.utc):%H:%M} "
                  f"{'':<12} {age/60:>6.1f}m {state:<12} "
                  f"{datetime.fromtimestamp(tl, timezone.utc):%H:%M:%S}")
        else:
            print(f"{name:<20} {'即时值':<18} {'':>8} {'T+0':<12} "
                  f"{datetime.fromtimestamp(tl, timezone.utc):%H:%M:%S}")
    print()

    print(f"[live] 等 {pause} 秒，看同一个桶的数值会不会被改写……", flush=True)
    import time as _t
    _t.sleep(pause)
    b = snap()

    print()
    print("=" * 84)
    print("7) 同一个 5m 桶，隔 {}s 再取，值变没变（变 = 当时拿到的是半截数据）".format(pause))
    print("=" * 84)
    for name, _, key in LIVE_EP:
        if not key:
            continue
        ra, rb = a.get(name, {}), b.get(name, {})
        if "err" in ra or "err" in rb:
            continue
        if ra.get("last_bucket") != rb.get("last_bucket"):
            print(f"{name:<20} 桶已滚动（{datetime.fromtimestamp(ra['last_bucket'], timezone.utc):%H:%M}"
                  f" → {datetime.fromtimestamp(rb['last_bucket'], timezone.utc):%H:%M}），本项无法比较")
            continue
        va, vb = ra["last_row"], rb["last_row"]
        num = [k for k in va if isinstance(va[k], str) and _isnum(va[k])]
        diffs = [(k, abs(float(va[k]) - float(vb[k])) / max(abs(float(va[k])), 1e-12))
                 for k in num if k != "timestamp"]
        worst = max(diffs, key=lambda x: x[1]) if diffs else ("—", 0.0)
        print(f"{name:<20} 最大改动 {worst[1]*100:>6.2f}%（{worst[0]}）")
    print()
    return 0


def _isnum(s: str) -> bool:
    try:
        float(s)
        return True
    except Exception:  # noqa: BLE001
        return False


# ------------------------------------------------------------------ 2. asof

def _metrics_day(sym: str, day: str) -> dict | None:
    """取一天，抽出多个 as-of 时刻的值 + 文件顺序最后一行（当前代码实际用的那个）。"""
    url = f"{CDN}/data/futures/um/daily/metrics/{sym}/{sym}-metrics-{day}.zip"
    blob = _get(url)
    if blob is None:
        return None
    try:
        z = zipfile.ZipFile(io.BytesIO(blob))
        nm = [n for n in z.namelist() if n.endswith(".csv")]
        if not nm:
            return None
        df = pd.read_csv(io.BytesIO(z.read(nm[0])))
    except Exception:  # noqa: BLE001
        return None
    if "create_time" not in df or df.empty:
        return None
    df["create_time"] = pd.to_datetime(df["create_time"], errors="coerce")
    df = df.dropna(subset=["create_time"])
    if df.empty:
        return None

    rec = {"symbol": sym, "day": day, "n_rows": len(df)}
    # 文件顺序最后一行 = 05_deriv_features.py 实际取到的那个
    last_file = df.iloc[-1]
    rec["t_filelast"] = last_file["create_time"].strftime("%H:%M")
    # 真正的时间末行
    s = df.sort_values("create_time")

    def pick(hhmm: str, col: str) -> float:
        m = s[s["create_time"].dt.strftime("%H:%M") == hhmm]
        if m.empty or col not in m:
            return np.nan
        return pd.to_numeric(m[col], errors="coerce").iloc[0]

    for col, tag in (("sum_open_interest_value", "oi"),
                     ("sum_toptrader_long_short_ratio", "ls_top"),
                     ("sum_taker_long_short_vol_ratio", "taker"),
                     ("count_long_short_ratio", "ls_count")):
        if col not in s:
            rec[f"{tag}_filelast"] = np.nan
            for t in ("2355", "2255", "2200"):
                rec[f"{tag}_{t}"] = np.nan
            continue
        rec[f"{tag}_filelast"] = float(pd.to_numeric(pd.Series([last_file[col]]), errors="coerce").iloc[0])
        for t in ("2355", "2255", "2200"):
            rec[f"{tag}_{t}"] = pick(f"{t[:2]}:{t[2:]}", col)
    return rec


def cmd_asof(n: int) -> pd.DataFrame:
    """抽样，量「文件顺序最后一行」的时刻分布。"""
    samples = pd.read_csv(DATA / "features_deriv.csv")
    samples = samples.dropna(subset=["pre_day"])
    if len(samples) > n:
        samples = samples.sample(n, random_state=42)
    days = sorted({str(pd.Timestamp(d).date()) for d in samples["pre_day"]})
    syms = sorted(samples["symbol"].unique())
    # 只取样本真实出现的 (币, 日)，外加前一日（算 oi_chg_1d 要用）——不要做笛卡尔积
    jobs = set()
    for r in samples.to_dict("records"):
        dd = pd.Timestamp(r["pre_day"])
        jobs.add((r["symbol"], str(dd.date())))
        jobs.add((r["symbol"], str((dd - pd.Timedelta(days=1)).date())))
    jobs = sorted(jobs)
    print(f"[asof] 抽样 {len(samples)} 条样本 → {len(jobs)} 个 (币,日) 任务", flush=True)

    cache: dict[tuple[str, str], dict] = {}
    if CACHE.exists():
        old = pd.read_parquet(CACHE)
        for r in old.to_dict("records"):
            cache[(r["symbol"], r["day"])] = r
        print(f"[asof] 复用缓存 {len(cache):,} 条")
    pending = [t for t in jobs if t not in cache]
    print(f"[asof] 需下载 {len(pending):,}")
    if pending:
        with ThreadPoolExecutor(max_workers=32) as ex:
            for i, (k, r) in enumerate(zip(pending, ex.map(lambda t: _metrics_day(*t), pending)), 1):
                cache[k] = r
                if i % 400 == 0:
                    print(f"   {i}/{len(pending)}")
    good = [v for v in cache.values() if v]
    if good:
        pd.DataFrame(good).to_parquet(CACHE, index=False, compression="zstd")
    d = pd.DataFrame(good)
    print(f"[asof] 成功 {len(d):,} 个 (币,日)\n")

    # 文件顺序末行的时刻分布
    t = d["t_filelast"].value_counts().sort_index()
    print("2) 「文件顺序最后一行」实际落在什么时刻（应该全是 23:55）")
    print("-" * 60)
    print(f"  唯一时刻数 {d['t_filelast'].nunique()}｜众数 {t.index[0]}（{t.iloc[0]} 次）")
    hh = pd.to_numeric(d["t_filelast"].str[:2], errors="coerce")
    for lo, hi, lab in ((0, 12, "上午 00:00–11:59"), (12, 18, "下午 12:00–17:59"),
                        (18, 22, "傍晚 18:00–21:59"), (22, 23, "22:00–22:59"), (23, 24, "23:00–23:59")):
        k = ((hh >= lo) & (hh < hi)).sum()
        print(f"  {lab:<18} {k:>5} 次  {k/len(d):>6.1%}")
    print()

    # 数值偏差
    print("3) 「文件顺序末行」vs「真正 23:55」vs「决策安全 22:55」的 OI 差")
    print("-" * 60)
    for col, lab in (("oi", "OI 名义值"),):
        a, b, c = d[f"{col}_filelast"], d[f"{col}_2355"], d[f"{col}_2255"]
        for name, s in ((f"{lab} filelast vs 2355", (a / b - 1).abs() * 100),
                        (f"{lab} 2355 vs 2255", (b / c - 1).abs() * 100)):
            s = s.replace([np.inf, -np.inf], np.nan).dropna()
            if len(s) < 10:
                continue
            print(f"  {name:<26} 中位 {s.median():>6.2f}%  p90 {s.quantile(.9):>6.2f}%  最大 {s.max():>6.1f}%")
    print()
    return d


# ------------------------------------------------------------------ 3. impact

def cmd_impact(n: int) -> int:
    """重算 oi_chg_1d 三种口径，看结论会不会变。"""
    if not CACHE.exists():
        print("[impact] 先跑 asof 建缓存")
        return 1
    cache = pd.read_parquet(CACHE)
    per = {(r["symbol"], r["day"]): r for r in cache.to_dict("records")}

    samples = pd.read_csv(DATA / "features_deriv.csv").dropna(subset=["pre_day"])
    if n and len(samples) > n:
        samples = samples.sample(n, random_state=42)

    rows = []
    for s in samples.to_dict("records"):
        sym, d = s["symbol"], pd.Timestamp(s["pre_day"])
        cur = per.get((sym, str(d.date())))
        prev = per.get((sym, str((d - pd.Timedelta(days=1)).date())))
        if not cur or not prev:
            continue
        row = {"symbol": sym, "pre_day": s["pre_day"], "label": int(s["label"]), "gain": s["gain"]}
        for tag in ("filelast", "2355", "2255"):
            a, b = cur.get(f"oi_{tag}"), prev.get(f"oi_{tag}")
            row[f"oi_chg_1d_{tag}"] = (a / b - 1.0) if (a and b and np.isfinite(a) and np.isfinite(b) and b > 0) else np.nan
        for tag in ("filelast", "2355", "2255"):
            row[f"ls_top_{tag}"] = cur.get(f"ls_top_{tag}")
        rows.append(row)
    d = pd.DataFrame(rows)
    print(f"[impact] 可用样本 {len(d)}（爆拉 {int(d['label'].sum())}）\n")

    print("4) 同一个 oi_chg_1d，三种取值口径的区分度")
    print("-" * 72)
    print(f"{'口径':<34} {'覆盖':>7} {'AUC':>8} {'上下分位差':>12} {'与 filelast 相关':>16}")
    print("-" * 72)
    base = d["oi_chg_1d_filelast"]
    for tag, lab in (("filelast", "① 现状：文件顺序末行（有 bug）"),
                     ("2355", "② 修正排序，取真末行 23:55（前视 55 分）"),
                     ("2255", "③ 决策安全：22:55（无前视）")):
        x = d[f"oi_chg_1d_{tag}"]
        auc = _rank_auc(d["label"].to_numpy(float), x.to_numpy(float))
        sp = _spread(x, d["label"])
        rho = base.corr(x, method="spearman") if tag != "filelast" else 1.0
        print(f"{lab:<34} {x.notna().mean():>6.1%} {auc:>8.3f} {sp:>11.2f}pp {rho:>15.3f}")
    print()
    print("   参考：AUC 0.5 = 没用；本项目阳性对照 0.566–0.649。")
    print("   分位差 = 该特征最高 1/5 与最低 1/5 的爆拉率差（pp）。")
    print()

    # 结论提示
    a1 = _rank_auc(d["label"].to_numpy(float), d["oi_chg_1d_filelast"].to_numpy(float))
    a3 = _rank_auc(d["label"].to_numpy(float), d["oi_chg_1d_2255"].to_numpy(float))
    rho = base.corr(d["oi_chg_1d_2255"], method="spearman")
    print("5) 判定")
    print("-" * 72)
    print(f"   现状口径与安全口径的秩相关 {rho:.3f}")
    if rho >= 0.95:
        print("   → 两者几乎同一个变量：bug 只是换了取样时刻，**不改变历史结论**。")
    elif rho >= 0.80:
        print("   → 中等偏差：结论方向可能不变，但「提升多少 pp」这类数字不可信。")
    else:
        print("   → 偏差很大：历史结论里的 OI 相关数字都要重算。")
    print(f"   AUC 现状 {a1:.3f} → 安全 {a3:.3f}（差 {a3-a1:+.3f}）")
    print()
    return 0


# ------------------------------------------------------------------ main

def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["lag", "live", "asof", "impact", "all"])
    ap.add_argument("--n", type=int, default=400)
    args = ap.parse_args(argv)

    if args.cmd in ("lag", "all"):
        cmd_lag()
    if args.cmd in ("live", "all"):
        cmd_live()
    if args.cmd in ("asof", "all"):
        cmd_asof(args.n)
    if args.cmd in ("impact", "all"):
        cmd_impact(args.n)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
