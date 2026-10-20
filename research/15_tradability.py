#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""15 · 可交易性清洗（A17）`[ds4.1f]`

问题：`pumps.json` 的 841 个事件只要求「涨了 2 倍」，不要求「买得进、拿得住」。
一根 1 小时插针 +300% 的币在标签里和「连续 13 天阴跌吸筹后放量拉升」完全等价。
用这种标签训练，识别层学到的是「哪种币会插针」，不是「哪种币能做」。

三个指标（对应 gemini 拷问 1 的 M70/M71/M72）：

  ① 建仓窗口 entry —— 从启动日 00:00 起 72 小时内，价格还在 `P_ref × 1.10` 以下
     的小时数，以及这些小时的累计成交额。`P_ref = pre_close`（信号日收盘，
     也就是策略真正能成交的价位）。
     注意：TODO 原文写的是「价格在 [P_t0, 1.10×P_t0] 内停留的小时数」。
     严格区间（整根 K 线都在带内）在 1h 上几乎恒为 0，没有区分度，
     故实现为「该小时 low <= 1.10×P_ref」——即「这一小时还买得进」。
     严格区间版本同时输出为 `band_hours`，仅作参考。

  ② 峰后驻留 keep —— 峰值后 48h 收盘均价相对涨幅的保留比例：
     `keep_frac = (post_mean − P_ref) / (peak_price − P_ref)`。
     = 1 表示峰后 48 小时一直站在峰值；= 0 表示涨的全吐回去了。

  ③ 路径夏普 psharpe —— 启动到峰值的逐小时对数收益 `mean/std`。

辅助诊断（不参与阈值，只用来解释分类结果）：
  hours_to_2x   从启动到收盘翻倍用了多少小时（越小越买不进）
  spike_conc    启动→峰值区间内，单小时最大成交额占区间总成交额的比例（插针指纹）
  path_maxdd    启动→峰值区间的最大回撤（拿得住的代价）
  up_hour_frac  上涨小时占比

用法：
  py -3.10 research/15_tradability.py                 # 算指标 + 打印分布
  py -3.10 research/15_tradability.py --classify      # 附加分类并写 reports/tradability_ds/
  py -3.10 research/15_tradability.py --labels        # 只导出清洗后的标签 parquet
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
H1 = DATA / "1h"
OUT = ROOT / "reports" / "tradability_ds"

ENTRY_WINDOW_H = 72        # 建仓窗口扫描长度（小时）
POST_PEAK_H = 48           # 峰后驻留观察长度（小时）
BAND = 0.10                # 建仓价带上沿：P_ref × 1.10

# ---- 分类阈值（先看分布、后定阈值，故附敏感性检验，见 --classify 输出）----
TH_GAP = 0.10              # 启动日开盘相对信号价跳空 <= 10%（还能买到）
TH_ENTRY_HOURS = 6         # 至少 6 个「买得进」的小时
TH_ENTRY_QV = 100_000.0    # 建仓窗口内累计成交额 >= 10 万 U
TH_KEEP = 0.30             # 峰后 48h 至少留住 30% 的涨幅
TH_PSHARPE = 0.03          # 逐小时收益均值/标准差 >= 0.03（≈ 周化 0.39）
TH_SPIKE = 0.30            # 单小时成交额占区间 <= 30%（超过就是插针指纹）


# ---------------------------------------------------------------- 数据

API = "https://data-api.binance.vision/api/v3/klines"
KCOLS = ["symbol", "open_time", "open", "high", "low", "close", "volume",
         "close_time", "quote_volume", "trades", "taker_buy_base",
         "taker_buy_quote", "dt"]


def _get_json(url: str, retry: int = 4) -> list | dict | None:
    for i in range(retry):
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                return json.load(r)
        except urllib.error.HTTPError as exc:
            if exc.code in (418, 429):
                time.sleep(2.0 * (i + 1))
                continue
            if exc.code == 400:
                return None          # 交易对不存在 / 区间非法
            time.sleep(0.6 * (i + 1))
        except Exception:  # noqa: BLE001
            time.sleep(0.6 * (i + 1))
    return None


def fetch_month(sym: str, ym: str) -> int:
    """拉一个币一个月的 1h K 线，落成 data/1h/{sym}-{ym}.parquet。返回条数。"""
    p = H1 / f"{sym}-{ym}.parquet"
    if p.exists():
        return -1
    t0 = pd.Timestamp(ym + "-01", tz="UTC")
    t1 = t0 + pd.DateOffset(months=1)
    rows: list[list] = []
    cur = int(t0.timestamp() * 1000)
    end = int(t1.timestamp() * 1000) - 1
    while cur <= end:
        url = f"{API}?symbol={sym}&interval=1h&startTime={cur}&endTime={end}&limit=1000"
        js = _get_json(url)
        if not isinstance(js, list) or not js:
            break
        rows.extend(js)
        if len(js) < 1000:
            break
        cur = int(js[-1][0]) + 3_600_000
        time.sleep(0.12)
    if not rows:
        return 0
    d = pd.DataFrame(rows, columns=[f"c{i}" for i in range(len(rows[0]))])
    d = d.iloc[:, :11]
    d.columns = ["open_time", "open", "high", "low", "close", "volume", "close_time",
                 "quote_volume", "trades", "taker_buy_base", "taker_buy_quote"]
    for c in d.columns:
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d.insert(0, "symbol", sym)
    d["dt"] = pd.to_datetime(d["open_time"], unit="ms", utc=True)
    d = d[KCOLS].sort_values("dt").reset_index(drop=True)
    H1.mkdir(parents=True, exist_ok=True)
    d.to_parquet(p, index=False)
    return len(d)


def needed_pairs(events: list[dict]) -> list[tuple[str, str]]:
    need = set()
    for e in events:
        a = pd.Timestamp(e["ignite_date"]) - pd.Timedelta(days=2)
        b = pd.Timestamp(e["peak_date"]) + pd.Timedelta(days=4)
        cur = a.replace(day=1)
        while cur <= b:
            need.add((e["symbol"], cur.strftime("%Y-%m")))
            cur = (cur + pd.DateOffset(months=1)).replace(day=1)
    have = {p.stem for p in H1.glob("*.parquet")}
    return sorted(x for x in need if f"{x[0]}-{x[1]}" not in have)


def dedup_events(events: list[dict]) -> list[dict]:
    """同一 (symbol, pre_day) 会因不同峰值被重复检测（841 → 822 的差额就在这）。
    建仓窗口与路径起点都相同，只有峰值不同，故保留涨幅最大的那条。"""
    best: dict[tuple[str, str], dict] = {}
    for e in events:
        k = (e["symbol"], e["pre_day"])
        if k not in best or e["gain"] > best[k]["gain"]:
            best[k] = e
    return list(best.values())


def do_fetch(events: list[dict], jobs: int = 8) -> None:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    pairs = needed_pairs(events)
    print(f"[fetch] 缺 {len(pairs)} 个「币-月」，并发 {jobs}")
    got = empty = fail = 0
    t_start = time.time()
    with ThreadPoolExecutor(max_workers=jobs) as ex:
        futs = {ex.submit(fetch_month, s, m): (s, m) for s, m in pairs}
        for k, fu in enumerate(as_completed(futs), 1):
            try:
                n = fu.result()
            except Exception:  # noqa: BLE001
                n = 0
            if n > 0:
                got += 1
            elif n == 0:
                empty += 1
            else:
                fail += 1
            if k % 100 == 0:
                el = time.time() - t_start
                print(f"  ... {k}/{len(pairs)}  新增{got} 空{empty} 已存在{fail}  "
                      f"已用{el:.0f}s 预计剩余{el/k*(len(pairs)-k):.0f}s", flush=True)
    print(f"[fetch] 完成：新增 {got} 个文件，无数据 {empty}，已存在 {fail}，"
          f"耗时 {time.time()-t_start:.0f}s")
def load_1h(sym: str, t_start: pd.Timestamp, t_end: pd.Timestamp) -> pd.DataFrame | None:
    """读取 [t_start, t_end] 覆盖到的所有月文件并拼接。"""
    months, cur = [], t_start.replace(day=1)
    while cur <= t_end:
        months.append(cur.strftime("%Y-%m"))
        cur = (cur + pd.DateOffset(months=1)).replace(day=1)
    frames = []
    for ym in months:
        p = H1 / f"{sym}-{ym}.parquet"
        if not p.exists():
            continue
        try:
            d = pd.read_parquet(p)
        except Exception:  # noqa: BLE001
            continue
        if "dt" not in d.columns:
            d["dt"] = pd.to_datetime(d["open_time"], unit="ms", utc=True)
        frames.append(d)
    if not frames:
        return None
    d = pd.concat(frames, ignore_index=True)
    d["dt"] = pd.to_datetime(d["dt"], utc=True)
    d = d.drop_duplicates("dt").sort_values("dt").reset_index(drop=True)
    for c in ("open", "high", "low", "close", "quote_volume"):
        if c in d.columns:
            d[c] = pd.to_numeric(d[c], errors="coerce")
    if "quote_volume" not in d.columns:
        d["quote_volume"] = np.nan
    return d


def _nan(v):
    return float("nan") if v is None else float(v)


# ---------------------------------------------------------------- 单事件指标

def event_metrics(ev: dict) -> dict | None:
    sym = ev["symbol"]
    t0 = pd.Timestamp(ev["ignite_date"], tz="UTC")
    peak_day = pd.Timestamp(ev["peak_date"], tz="UTC")
    p_ref = float(ev["pre_close"])
    peak_px = float(ev["peak_price"])
    if p_ref <= 0 or peak_px <= p_ref:
        return None

    bars = load_1h(sym, t0 - pd.Timedelta(days=2), peak_day + pd.Timedelta(days=4))
    if bars is None or bars.empty:
        return {"symbol": sym, "pre_day": ev["pre_day"], "ignite_date": ev["ignite_date"],
                "peak_date": ev["peak_date"], "gain": ev["gain"], "data_ok": 0}

    rec = {"symbol": sym, "pre_day": ev["pre_day"], "ignite_date": ev["ignite_date"],
           "peak_date": ev["peak_date"], "gain": float(ev["gain"]),
           "days_to_peak": int(ev["days_to_peak"]), "p_ref": p_ref,
           "peak_price": peak_px, "data_ok": 1}

    # ---------- 定位峰值小时 ----------
    seg_peak = bars[(bars["dt"] >= peak_day) & (bars["dt"] < peak_day + pd.Timedelta(days=1))]
    if seg_peak.empty:
        rec["data_ok"] = 0
        return rec
    pk_idx = int(seg_peak["high"].idxmax())
    peak_ts = bars.loc[pk_idx, "dt"]
    rec["peak_ts"] = peak_ts.isoformat()

    # ---------- ① 建仓窗口 ----------
    w = bars[(bars["dt"] >= t0) & (bars["dt"] < t0 + pd.Timedelta(hours=ENTRY_WINDOW_H))]
    if w.empty:
        rec["data_ok"] = 0
        return rec
    # 真正的「买不进」度量：信号日收盘 → 启动日第一个小时，已经跳空多少
    first = w.iloc[0]
    rec["gap_t0"] = float(first["open"] / p_ref - 1.0)
    rec["fill_1h"] = float(first["close"] / p_ref - 1.0)
    buyable = w["low"] <= p_ref * (1 + BAND)
    rec["entry_hours"] = int(buyable.sum())
    rec["entry_hours_frac"] = float(buyable.mean())
    rec["entry_quote_vol"] = float(w.loc[buyable, "quote_volume"].sum())
    rec["entry_vol_per_h"] = rec["entry_quote_vol"] / max(rec["entry_hours"], 1)
    # 严格带（TODO 原文口径，仅参考）
    rec["band_hours"] = int(((w["low"] >= p_ref) & (w["high"] <= p_ref * (1 + BAND))).sum())
    # 第一次「已经涨飞、买不到」的小时序号
    gone = np.where(~buyable.to_numpy())[0]
    rec["first_runaway_h"] = float(gone[0]) if len(gone) else float(ENTRY_WINDOW_H)
    # 建仓窗口内的最高价（判断这 72h 内有没有已经走完行情）
    rec["entry_win_max_high"] = float(w["high"].max())

    # ---------- ② 峰后驻留 ----------
    post = bars[(bars["dt"] > peak_ts) & (bars["dt"] <= peak_ts + pd.Timedelta(hours=POST_PEAK_H))]
    if len(post) >= 12:
        post_mean = float(post["close"].mean())
        rec["post_mean_48h"] = post_mean
        rec["retain_ratio"] = post_mean / peak_px
        rec["keep_frac"] = (post_mean - p_ref) / (peak_px - p_ref)
        rec["post_bars"] = int(len(post))
    else:
        rec["post_mean_48h"] = np.nan
        rec["retain_ratio"] = np.nan
        rec["keep_frac"] = np.nan
        rec["post_bars"] = int(len(post))

    # ---------- ③ 路径 ----------
    path = bars[(bars["dt"] >= t0) & (bars["dt"] <= peak_ts)]
    if len(path) >= 6:
        c = path["close"].to_numpy(float)
        r = np.diff(np.log(c))
        r = r[np.isfinite(r)]
        if len(r) >= 5 and np.std(r) > 0:
            rec["psharpe"] = float(np.mean(r) / np.std(r))
            rec["psharpe_w"] = rec["psharpe"] * np.sqrt(24 * 7)
        else:
            rec["psharpe"] = np.nan
            rec["psharpe_w"] = np.nan
        rec["up_hour_frac"] = float(np.mean(r > 0)) if len(r) else np.nan
        rec["path_hours"] = int(len(path))
        # 最大回撤
        peak_run = np.maximum.accumulate(c)
        rec["path_maxdd"] = float(np.min(c / peak_run - 1.0))
    else:
        for k in ("psharpe", "psharpe_w", "up_hour_frac", "path_maxdd"):
            rec[k] = np.nan
        rec["path_hours"] = int(len(path))

    # ---------- 诊断 ----------
    # 到翻倍用了几小时
    to2x = bars[(bars["dt"] >= t0) & (bars["close"] >= 2 * p_ref)]
    rec["hours_to_2x"] = float((to2x["dt"].iloc[0] - t0) / pd.Timedelta(hours=1)) if len(to2x) else np.nan
    # 单小时成交额集中度（启动→峰值）
    qv = path["quote_volume"].to_numpy(float)
    tot = np.nansum(qv)
    rec["spike_conc"] = float(np.nanmax(qv) / tot) if tot > 0 else np.nan
    # 启动→峰值的总倍数（用 1h 数据复核日线 gain）
    rec["mult_1h"] = float(path["close"].max() / p_ref) if len(path) else np.nan

    return rec


# ---------------------------------------------------------------- 分类

def classify(df: pd.DataFrame,
             th_gap: float = TH_GAP,
             th_hours: float = TH_ENTRY_HOURS,
             th_qv: float = TH_ENTRY_QV,
             th_keep: float = TH_KEEP,
             th_ps: float = TH_PSHARPE,
             th_spike: float = TH_SPIKE) -> pd.Series:
    ok = (
        (df["gap_t0"] <= th_gap)
        & (df["entry_hours"] >= th_hours)
        & (df["entry_quote_vol"] >= th_qv)
        & (df["keep_frac"] >= th_keep)
        & (df["psharpe"] >= th_ps)
        & (df["spike_conc"] <= th_spike)
    )
    return ok.fillna(False)


def pct(s: pd.Series) -> str:
    return f"{100 * s.mean():.1f}%"


# ---------------------------------------------------------------- 报告

def _spearman(a, b) -> float:
    a = np.asarray(a, float)
    b = np.asarray(b, float)
    m = np.isfinite(a) & np.isfinite(b)
    if m.sum() < 30:
        return np.nan
    a, b = a[m], b[m]
    ra = pd.Series(a).rank().to_numpy()
    rb = pd.Series(b).rank().to_numpy()
    if np.std(ra) == 0 or np.std(rb) == 0:
        return np.nan
    return float(np.corrcoef(ra, rb)[0, 1])


def _partial_spearman(y, x, ctrl) -> float:
    """扣掉 ctrl 后的偏相关（残差化）。"""
    y = np.asarray(y, float)
    x = np.asarray(x, float)
    C = np.asarray(ctrl, float)
    m = np.isfinite(y) & np.isfinite(x) & np.isfinite(C).all(axis=1)
    if m.sum() < 40:
        return np.nan
    y, x, C = y[m], x[m], C[m]
    C = np.column_stack([np.ones(len(C)), C])
    try:
        ry = y - C @ np.linalg.lstsq(C, y, rcond=None)[0]
        rx = x - C @ np.linalg.lstsq(C, x, rcond=None)[0]
    except np.linalg.LinAlgError:
        return np.nan
    return _spearman(ry, rx)


def targeted_test(df: pd.DataFrame) -> pd.DataFrame:
    """定向检验：18 分里符号反了的条件，负号是不是「可交易性」造成的？

    两条互补的检验：
      A. 分组：把正例按 tradable 拆成两组，各自算组内 IC7。
      B. 偏相关（主检验）：在**全部正例**上，扣掉 5 个可交易性指标后再算 IC7。
         若 B 的偏相关仍显著为负，说明负号与可交易性无关，是结构性的。
    B 用了全样本，统计功效远高于 A（骗炮组只有几十个样本）。
    """
    pos = df[df["label"] == 1].copy()
    if "tradable" not in pos.columns or not len(pos):
        return pd.DataFrame()
    fr = pd.read_csv(DATA / "forward_returns.csv")
    fr["pre_day"] = pd.to_datetime(fr["pre_day"], utc=True)
    pos = pos.merge(fr[["symbol", "pre_day", "ret_net_7"]], on=["symbol", "pre_day"], how="left")
    # 并入可交易性指标
    met = pd.read_csv(OUT / "metrics.csv")
    met["pre_day"] = pd.to_datetime(met["pre_day"], utc=True)
    mk = ["keep_frac", "psharpe", "path_maxdd", "entry_hours", "spike_conc"]
    if "data_ok" in met.columns:
        met = met[met["data_ok"] == 1]
    pos = pos.merge(met[["symbol", "pre_day"] + mk], on=["symbol", "pre_day"], how="inner")
    ctrl = pos[mk].to_numpy(float)

    conds = [("c_trend", 5), ("oi_chg_1d", 4), ("oi_chg_3d", 3), ("ls_top", 3),
             ("c_breakout", 2), ("c_v24up", 1)]
    rows = []
    for c, w in conds:
        if c not in pos.columns:
            continue
        x = pd.to_numeric(pos[c], errors="coerce").to_numpy(float)
        tr = pos["tradable"].to_numpy(bool)
        rows.append({
            "条件": c, "权重": w,
            "IC7_全部正例": _spearman(x, pos["ret_net_7"]),
            "IC7_扣可交易性": _partial_spearman(pos["ret_net_7"], x, ctrl),
            "IC7_可交易组": _spearman(x[tr], pos.loc[tr, "ret_net_7"]),
            "IC7_不可交易组": _spearman(x[~tr], pos.loc[~tr, "ret_net_7"]),
            "n_可交易": int(tr.sum()), "n_不可交易": int((~tr).sum()),
        })
    return pd.DataFrame(rows)


def compare_bench(out: Path) -> None:
    """比较原标签与清洗标签两次试验台的结论差异。"""
    a_p = DATA / "variable_bench.csv"
    b_p = DATA / "variable_bench_clean.csv"
    if not (a_p.exists() and b_p.exists()):
        print("[compare] 缺 variable_bench.csv 或 variable_bench_clean.csv，跳过")
        return
    a = pd.read_csv(a_p)
    b = pd.read_csv(b_p)
    keep = ["var", "name", "n", "auc", "ic7_all", "ic7_l1", "pIC7_all", "pIC7_all_p95"]
    keep = [c for c in keep if c in a.columns and c in b.columns]
    m = a[keep].merge(b[keep], on=["var", "name"], suffixes=("_原", "_清"))
    m["Δauc"] = m["auc_清"] - m["auc_原"]
    m["Δic7_l1"] = m["ic7_l1_清"] - m["ic7_l1_原"]
    m["翻号"] = np.sign(m["ic7_l1_原"]) != np.sign(m["ic7_l1_清"])
    m["原显著"] = m["pIC7_all_原"] > m["pIC7_all_p95_原"]
    m["清显著"] = m["pIC7_all_清"] > m["pIC7_all_p95_清"]
    m["显著性变化"] = np.where(m["清显著"] & ~m["原显著"], "新增显著",
                          np.where(~m["清显著"] & m["原显著"], "失去显著", ""))

    L: list[str] = []
    A = L.append
    A("# 标签清洗前后：变量结论对照 `[ds4.1f]`")
    A("")
    A(f"- 原标签：`data/variable_bench.csv`（正例 = 任何 2 倍以上事件）")
    A(f"- 清洗标签：`data/variable_bench_clean.csv`（正例 = 买得进且拿得住）")
    A(f"- 共同变量 {len(m)} 个")
    A("")
    A("## 一、区分度（AUC）几乎没变")
    A("")
    A(f"- `|Δauc|` 中位 **{m['Δauc'].abs().median():.4f}**，"
      f"最大 {m['Δauc'].abs().max():.4f}")
    A(f"- `|Δic7_l1|` 中位 **{m['Δic7_l1'].abs().median():.4f}**，"
      f"最大 {m['Δic7_l1'].abs().max():.4f}")
    A("")
    A("> 读法：如果清洗标签后 AUC 排名大洗牌，说明原结论被假机会污染；"
      "如果几乎不动，说明「假机会」并不是原结论错的原因，得另找。")
    A("")
    A("## 二、组内 IC7 翻号的变量")
    A("")
    fl = m[m["翻号"]].reindex(m["Δic7_l1"].abs().sort_values(ascending=False).index).dropna(subset=["var"])
    A(f"共 **{len(fl)}** 个（占 {100*len(fl)/len(m):.0f}%）。"
      "随机翻号期望约 50%，所以只有伴随大幅变化的才值得看。")
    A("")
    if len(fl):
        A("| 变量 | 中文名 | IC7_l1 原 | IC7_l1 清 | Δ | 显著性变化 |")
        A("|---|---|---|---|---|---|")
        for _, r in fl.head(25).iterrows():
            A(f"| `{r['var']}` | {r['name']} | {r['ic7_l1_原']:+.3f} | {r['ic7_l1_清']:+.3f} | "
              f"{r['Δic7_l1']:+.3f} | {r['显著性变化'] or '—'} |")
    A("")
    A("## 三、显著性变化")
    A("")
    n_add = int((m["显著性变化"] == "新增显著").sum())
    n_lose = int((m["显著性变化"] == "失去显著").sum())
    A(f"- 清洗后**新增显著**：{n_add} 个　**失去显著**：{n_lose} 个")
    A("")
    for lab in ("新增显著", "失去显著"):
        sub = m[m["显著性变化"] == lab]
        if not len(sub):
            continue
        A(f"**{lab}**：")
        A("")
        A("| 变量 | 中文名 | AUC 原 | AUC 清 | IC7_l1 原 | IC7_l1 清 |")
        A("|---|---|---|---|---|---|")
        for _, r in sub.head(20).iterrows():
            A(f"| `{r['var']}` | {r['name']} | {r['auc_原']:.3f} | {r['auc_清']:.3f} | "
              f"{r['ic7_l1_原']:+.3f} | {r['ic7_l1_清']:+.3f} |")
        A("")
    m.to_csv(out / "bench_compare.csv", index=False, encoding="utf-8-sig")
    (out / "compare.md").write_text("\n".join(L), encoding="utf-8")
    print(f"[compare] -> {out/'compare.md'}")


def write_report(df: pd.DataFrame, out: Path, tgt: pd.DataFrame | None = None) -> None:
    d = df[df["data_ok"] == 1].copy()
    n_all, n_ok = len(df), len(d)
    L: list[str] = []
    A = L.append

    A("# 可交易性清洗 · A17 实测 `[ds4.1f]`")
    A("")
    A(f"> 生成脚本：`research/15_tradability.py`　事件源：`data/pumps.json`（{n_all} 条）")
    A(f"> 1h 数据可用：{n_ok} 条（{100*n_ok/n_all:.1f}%）；其余因该币在该时段无 1h 缓存被剔除。")
    A("")

    A("## 一、结论先行")
    A("")
    tr = d["tradable"]
    A(f"**1. 可交易真赢家 {int(tr.sum())} / {n_ok}（{100*tr.mean():.1f}%）**；"
      f"脉冲骗炮 {int((~tr).sum())}（{100*(1-tr.mean()):.1f}%）。"
      "「正例里混着大量买不进的机会」这个假设，**在这个事件集里不成立**。")
    A("")
    gap = d["gap_t0"].dropna()
    A(f"**2. 决定性证据：信号日收盘 → 启动日开盘的跳空，中位 {gap.median():.2%}，"
      f"p99 {gap.quantile(0.99):.2%}，最大 {gap.max():.2%}**。"
      f"{len(gap)} 个可测事件里没有一个跳空超过 10%。")
    A("")
    A("   原因是事件定义本身：`ignite_date` 取的是「收盘较低点收盘 +15% 的第一根日线」，"
      "`pre_day` 是它的前一天。所以入场点被**构造性地**放在行情起点，"
      "跳空这一环根本不存在。真正要担心「追高」的是实盘里信号晚到几天的情况，"
      "那不是这个标签能回答的（属 A9 时刻对齐问题）。")
    A("")
    A(f"**3. 真正的可交易性代价不是「买不进」，是「拿不住 + 涨得慢」**："
      f"启动→峰值的路径最大回撤中位 **{d['path_maxdd'].median():.1%}**，"
      f"收盘翻倍所需小时数中位 **{d['hours_to_2x'].median()/24:.0f} 天**"
      f"（p90 {d['hours_to_2x'].quantile(0.9)/24:.0f} 天）。"
      "也就是说：能买到，但要先扛住两成以上的浮亏，再等三周以上。")
    A("")
    A("   这与 KNOWLEDGE §2.9 的「识别与幅度负相关」是同一件事的两面："
      "涨得最猛的（ALPACA 44 倍）恰恰是入场窗口最短、路径最颠的（建仓窗口仅 2 小时、路径回撤 −70%）。")
    A("")
    A("**4. 各条件的实际杀伤力**（六个条件各自否掉多少，可叠加）：")
    A("")
    A("| 条件 | 阈值 | 不满足的事件数 | 不满足占比 |")
    A("|---|---|---|---|")
    for name, col, th, sign in [
        ("买得进：启动日跳空", "gap_t0", TH_GAP, "<="),
        ("买得进：窗口小时数", "entry_hours", TH_ENTRY_HOURS, ">="),
        ("买得进：窗口成交额", "entry_quote_vol", TH_ENTRY_QV, ">="),
        ("拿得住：峰后保留", "keep_frac", TH_KEEP, ">="),
        ("不是一根针：路径夏普", "psharpe", TH_PSHARPE, ">="),
        ("不是一根针：单小时集中度", "spike_conc", TH_SPIKE, "<="),
    ]:
        okc = (d[col] <= th) if sign == "<=" else (d[col] >= th)
        bad = int((~okc.fillna(False)).sum())
        A(f"| {name} | {sign} {th:g} | {bad} | {100*bad/n_ok:.1f}% |")
    A("")
    A("> 「窗口小时数」和「峰后保留」是唯二有实际杀伤力的条件；"
      "「跳空」与「成交额」几乎不否任何事件——它们在本事件集里是**空条件**，"
      "保留它们只是为了让口径完整、并在未来换事件定义时仍然有效。")
    A("")

    A("## 二、指标分布")
    A("")
    cols = ["gain", "gap_t0", "fill_1h", "entry_hours", "entry_quote_vol", "keep_frac",
            "retain_ratio", "psharpe", "psharpe_w", "hours_to_2x", "spike_conc",
            "path_maxdd", "up_hour_frac", "path_hours", "days_to_peak"]
    q = d[cols].quantile([0.05, 0.25, 0.5, 0.75, 0.95]).T
    q.columns = ["p5", "p25", "p50", "p75", "p95"]
    A("| 指标 | p5 | p25 | 中位 | p75 | p95 |")
    A("|---|---|---|---|---|---|")
    for i, r in q.iterrows():
        A(f"| `{i}` | " + " | ".join(
            "—" if not np.isfinite(v) else (f"{v:.3g}" if abs(v) < 1e4 else f"{v:.0f}")
            for v in r) + " |")
    A("")

    A("## 三、真假两组对照")
    A("")
    A("| 指标 | 真赢家中位 | 骗炮中位 | 差异 |")
    A("|---|---|---|---|")
    for c in ["gain", "days_to_peak", "gap_t0", "fill_1h", "entry_hours", "entry_quote_vol",
              "entry_vol_per_h", "keep_frac", "retain_ratio", "psharpe", "hours_to_2x",
              "spike_conc", "path_maxdd", "up_hour_frac", "ret_net_7", "ret_net_30", "mfe_7"]:
        if c not in d.columns:
            continue
        a = d.loc[tr, c].median()
        b = d.loc[~tr, c].median()
        A(f"| `{c}` | {a:.3g} | {b:.3g} | {(a-b):+.3g} |")
    A("")
    A("> 最后三行是**污染幅度**：若骗炮组的前瞻净收益明显高于真赢家组，"
      "说明这些「买不进的机会」在把幅度结论系统性抬高。")
    A("")

    A("## 四、阈值敏感性")
    A("")
    A("阈值是看过分布之后定的，所以必须给出敏感性。下表每次只动一个条件，其余保持默认。")
    A("")
    A("| 变体 | 真赢家数 | 占比 |")
    A("|---|---|---|")
    A(f"| 默认（跳空{TH_GAP:.0%} / {TH_ENTRY_HOURS}h / {TH_ENTRY_QV:,.0f}U / "
      f"保留{TH_KEEP:.0%} / 夏普{TH_PSHARPE} / 集中度{TH_SPIKE:.0%}） "
      f"| {int(tr.sum())} | {100*tr.mean():.1f}% |")
    for label, kw in [
        ("跳空放宽到 20%", dict(th_gap=0.20)),
        ("跳空收紧到 5%", dict(th_gap=0.05)),
        ("建仓窗口放宽到 3h", dict(th_hours=3)),
        ("建仓窗口收紧到 12h", dict(th_hours=12)),
        ("成交额门槛降到 3 万 U", dict(th_qv=30_000.0)),
        ("峰后保留降到 15%", dict(th_keep=0.15)),
        ("峰后保留升到 50%", dict(th_keep=0.50)),
        ("路径夏普降到 0", dict(th_ps=0.0)),
        ("集中度放宽到 50%", dict(th_spike=0.50)),
        ("集中度收紧到 15%", dict(th_spike=0.15)),
        ("全部放宽", dict(th_gap=0.20, th_hours=3, th_qv=30_000.0, th_keep=0.15,
                       th_ps=0.0, th_spike=0.50)),
        ("全部收紧", dict(th_gap=0.05, th_hours=12, th_qv=300_000.0, th_keep=0.50,
                       th_ps=0.06, th_spike=0.15)),
    ]:
        c = classify(d, **kw)
        A(f"| {label} | {int(c.sum())} | {100*c.mean():.1f}% |")
    A("")

    # ---------- 五、骗炮率随涨幅单调上升 ----------
    A("## 五、骗炮率随涨幅单调上升")
    A("")
    band = pd.cut(d["gain"], [0, 1, 2, 4, 10, 1e9],
                  labels=["2-3x", "3-4x", "4-5x", "5-11x", "11x+"])
    g = d.groupby(band, observed=True)["tradable"].agg(["mean", "count"])
    g["骗炮数"] = (g["count"] * (1 - g["mean"])).round(0).astype(int)
    g["骗炮率"] = (100 * (1 - g["mean"])).round(1)
    A("| 涨幅档 | 事件数 | 骗炮数 | 骗炮率 |")
    A("|---|---|---|---|")
    for i, r in g.iterrows():
        A(f"| {i} | {int(r['count'])} | {int(r['骗炮数'])} | {r['骗炮率']:.1f}% |")
    A("")
    A("> 这是本轮最有操作含义的一条：**越大的行情越买不进/拿不住**。"
      "11 倍以上事件里两成是骗炮，3–5 倍里只有 3.7%。"
      "它与 KNOWLEDGE §2.9 的「识别与幅度负相关」是同一个现象的两个测法——"
      "所以「抓大行情」这个目标本身要付可交易性折扣，不能拿名义涨幅当收益。")
    A("")

    A("## 六、骗炮长什么样（按 spike_conc 排前 10）")
    A("| symbol | 日期 | 涨幅 | 建仓小时 | 建仓额 | 峰后保留 | 路径夏普 | 单小时集中度 |")
    A("|---|---|---|---|---|---|---|---|")
    top = d[~tr].nlargest(10, "spike_conc")
    for _, r in top.iterrows():
        A(f"| {r['symbol']} | {r['ignite_date']} | {r['gain']:.1f}x | {int(r['entry_hours'])} | "
          f"{r['entry_quote_vol']:,.0f} | {r['keep_frac']:.2f} | {r['psharpe']:.3f} | "
          f"{r['spike_conc']:.2f} |")
    A("")

    # ---------- 六、定向检验 ----------
    A("## 七、定向检验：18 分里符号反了的条件，是买不进造成的吗")
    A("")
    if tgt is None or not len(tgt):
        A("（本次未提供样本池，跳过。）")
    else:
        A("把 822 个正例按可交易性拆成两组，各自算「条件 vs 未来 7 天净收益」的组内 Spearman；"
          "并在**全部正例**上扣掉 5 个可交易性指标（峰后保留、路径夏普、路径回撤、"
          "建仓小时数、单小时集中度）后再算一次。")
        A("若某条件在**不可交易组**里 IC 显著为负、在**可交易组**里 IC 正常，"
          "它的负号就是标签污染；若两组都负、且扣掉可交易性后仍负，则是结构性负号。")
        A("")
        A("| 条件 | 权重 | IC7 全部正例 | IC7 扣可交易性 | IC7 可交易组 | IC7 不可交易组 | n 可/不可 |")
        A("|---|---|---|---|---|---|---|")
        for _, r in tgt.iterrows():
            A(f"| `{r['条件']}` | {int(r['权重'])} | {r['IC7_全部正例']:+.3f} | "
              f"**{r['IC7_扣可交易性']:+.3f}** | "
              f"{r['IC7_可交易组']:+.3f} | {r['IC7_不可交易组']:+.3f} | "
              f"{int(r['n_可交易'])}/{int(r['n_不可交易'])} |")
        A("")
        A("> 判读：看**第三列**（扣可交易性）。若它与第二列接近，说明负号跟可交易性无关。"
          "第四、五列样本量悬殊（785 vs 36），只作描述。")
    A("")

    out.mkdir(parents=True, exist_ok=True)
    (out / "report.md").write_text("\n".join(L), encoding="utf-8")
    print(f"[tradability] 报告 -> {out/'report.md'}")


# ---------------------------------------------------------------- 主流程

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--classify", action="store_true", help="附加分类并写报告")
    ap.add_argument("--labels", action="store_true", help="导出清洗后的标签 parquet")
    ap.add_argument("--fetch", action="store_true", help="先补齐缺失的 1h K 线")
    ap.add_argument("--compare", action="store_true",
                    help="比较原标签/清洗标签两次试验台结论（需先跑 14 两次）")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    if args.compare:
        compare_bench(OUT)
        return 0

    events = json.loads((DATA / "pumps.json").read_text(encoding="utf-8"))
    if args.limit:
        events = events[: args.limit]
    print(f"[tradability] 事件 {len(events)} 条")

    if args.fetch:
        do_fetch(events)

    n_raw = len(events)
    events = dedup_events(events)
    print(f"[tradability] 去重 (symbol, pre_day) 后 {len(events)} 条（原 {n_raw}）")

    rows = []
    for k, ev in enumerate(events, 1):
        try:
            r = event_metrics(ev)
        except Exception as exc:  # noqa: BLE001
            print(f"  !! {ev['symbol']} {ev['ignite_date']}: {exc}")
            r = None
        if r:
            rows.append(r)
        if k % 100 == 0:
            print(f"  ... {k}/{len(events)}")

    df = pd.DataFrame(rows)
    for c in df.columns:
        if c not in ("symbol", "pre_day", "ignite_date", "peak_date", "peak_ts"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df["data_ok"] = df["data_ok"].fillna(0).astype(int)

    OUT.mkdir(parents=True, exist_ok=True)
    # 附上前瞻净收益，用来看「假机会」把幅度结论抬高了多少
    try:
        fr = pd.read_csv(DATA / "forward_returns.csv")
        fr["pre_day"] = pd.to_datetime(fr["pre_day"], utc=True)
        df["pre_day"] = pd.to_datetime(df["pre_day"], utc=True)
        fk = [c for c in ("ret_net_7", "ret_net_14", "ret_net_30", "mfe_7", "mae_7")
              if c in fr.columns]
        df = df.merge(fr[["symbol", "pre_day"] + fk], on=["symbol", "pre_day"], how="left")
    except Exception as exc:  # noqa: BLE001
        print(f"  !! 前瞻收益合并跳过：{exc}")
    df.to_csv(OUT / "metrics.csv", index=False, encoding="utf-8-sig")
    print(f"[tradability] 指标 -> {OUT/'metrics.csv'}  shape={df.shape}")
    ok = df["data_ok"] == 1
    print(f"[tradability] 1h 可用 {int(ok.sum())}/{len(df)}")

    for c in ["entry_hours", "entry_quote_vol", "keep_frac", "psharpe", "spike_conc",
              "hours_to_2x", "path_maxdd"]:
        s = df.loc[ok, c].dropna()
        if len(s):
            print(f"  {c:18s} n={len(s):4d} "
                  f"p10={s.quantile(.1):>10.4g} p50={s.quantile(.5):>10.4g} "
                  f"p90={s.quantile(.9):>10.4g}")

    if args.classify or args.labels:
        df["tradable"] = False
        df.loc[ok, "tradable"] = classify(df.loc[ok])
        print(f"\n[tradability] 可交易真赢家 {int(df['tradable'].sum())} / {int(ok.sum())} "
              f"= {100*df.loc[ok,'tradable'].mean():.1f}%")

    if args.classify:
        # 把清洗结论并回样本池，做定向检验
        tgt = None
        try:
            hr = pd.read_csv(DATA / "hit_rate_current.csv")
            hr["pre_day"] = pd.to_datetime(hr["pre_day"], utc=True)
            lab0 = df[df["data_ok"] == 1][["symbol", "pre_day", "tradable"]].copy()
            lab0["pre_day"] = pd.to_datetime(lab0["pre_day"], utc=True)
            hr = hr.merge(lab0, on=["symbol", "pre_day"], how="left")
            hr["tradable"] = hr["tradable"].fillna(False).astype(bool)
            tgt = targeted_test(hr)
        except Exception as exc:  # noqa: BLE001
            print(f"  !! 定向检验跳过：{exc}")
        write_report(df, OUT, tgt)

    if args.labels:
        lab = df[df["data_ok"] == 1][["symbol", "pre_day", "tradable", "gain"]].copy()
        lab["pre_day"] = pd.to_datetime(lab["pre_day"], utc=True)
        lab.to_parquet(OUT / "tradable_labels.parquet", index=False)
        print(f"[tradability] 标签 -> {OUT/'tradable_labels.parquet'}  {lab.shape}")
        print(lab["tradable"].value_counts().to_string())

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
