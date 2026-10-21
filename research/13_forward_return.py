"""13 · 前瞻收益：按 pre_day 买入，持有 h 天的真实结果。

这是本项目第一次计算「买了会怎样」。

背景
----
全库此前的收益字段（`ret_1d` / `ret_7d` / `ret_30d` / `dd_from_60d_high`）全部是
**回看值**（见 `12_optimize_gate.py` 的列名「前30日涨幅」），唯一的前瞻字段
`oi_chg_fwd2d` 在 `07_oos_validate.py` 里被主动排除。因此 58.6% 基准率、72.3% 精确率、
+35.3pp 区分度都只是分类质量，从未被翻译成收益。

口径（写死在这里，不要在别处另立一套）
------------------------------------
**唯一决策时刻 = `pre_day` 23:00 UTC。** 三处取数都对齐到它：

| 用什么 | 取到哪一根 | 为什么 |
|---|---|---|
| K 线特征 | `pre_day` 22:00 那根（22:00–23:00，**在 23:00 收盘**） | `03_build_features.py` 的 `iloc[-2]` |
| 合约快照（OI/多空比/taker） | `pre_day` 22:55 那根 5 分钟快照 | `05_deriv_features.py` 的 `ASOF_HHMM`（M69 前视修正） |
| 入场价 | `pre_day` 23:00 那根的 **open**（= 22:00 收盘价） | 决策当下可成交的价格 |

- 出场价：入场时刻 + h 天，取「不晚于该时刻的最后一根已收盘 K 线」的 close。
- `mfe_h` / `mae_h`：区间 (入场, 入场+h] 内的最高价 / 最低价 相对入场价的偏移。
- 成本：默认 0.3% 双边（手续费 0.1%×2 + 滑点 0.1%），`ret_net = (1+ret)*(1-COST)-1`。
- 数据不全的样本**不填补、不丢弃成 0**：`covered_h=False` 单独统计。

**样本性质的边界（必须随结果一起报）**
`hit_rate_current.csv` 的 1,402 条 = 822 个「事后确认会翻倍」的爆拉样本 + 580 个同币平静期对照，
爆拉占 58.6%。真实候选池里这个比例远低于此。所以：
- 绝对收益是**上限**，不是策略收益；
- **跨分数组的差值**才是可信信号（同分布内的比较）。

**覆盖变更史（引用旧数字前必读）**
2026-10-21 A14 把全池 1h 数据从 3,926 币月补到 19,796 币月，各持有期覆盖变成
1/3/5/7 天 100%、14 天 99.7%、30 天 99.6%。在此之前 30 天只有 **26.4%**，
而且**不是随机缺失**——旧覆盖里对照样本占 67%，池子本身只占 41%，
所以当时报的「30 天中位 +3.83%」是**错的**（看起来「30 天比 7 天差」，纯属构成效应）。
正确值 +35.4%。在共同子集上新旧数值**逐位相同**（最大差 0.0000pp、秩相关 1.000），
说明计算逻辑没变，变的只是样本。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
H1 = DATA / "1h"
OUT_CSV = DATA / "forward_returns.csv"
OUT_DIR = ROOT / "reports" / "forward_return_ds"

DEFAULT_HORIZONS = (1, 3, 5, 7, 14, 30)
DEFAULT_COST = 0.003
# 出场时刻允许的取数容差：目标时刻前后 2 小时内必须有 K 线，否则视为未覆盖
TOLERANCE = pd.Timedelta(hours=2)


def _month_key(dt: pd.Timestamp) -> str:
    return f"{dt.year:04d}-{dt.month:02d}"


def _month_range(a: pd.Timestamp, b: pd.Timestamp) -> list[str]:
    out, y, m = [], a.year, a.month
    while (y, m) <= (b.year, b.month):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


class Loader:
    """按 (symbol, month) 缓存 1h 数据，避免重复读盘。"""

    def __init__(self, root: Path = H1) -> None:
        self.root = root
        self._cache: dict[tuple[str, str], pd.DataFrame | None] = {}

    def month(self, symbol: str, ym: str) -> pd.DataFrame | None:
        key = (symbol, ym)
        if key in self._cache:
            return self._cache[key]
        path = self.root / f"{symbol}-{ym}.parquet"
        if not path.exists():
            self._cache[key] = None
            return None
        try:
            df = pd.read_parquet(path, columns=["open_time", "open", "high", "low", "close"])
        except Exception:  # noqa: BLE001
            self._cache[key] = None
            return None
        if df.empty:
            self._cache[key] = None
            return None
        df = df.copy()
        df["dt"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
        df = df.sort_values("dt").drop_duplicates(subset="dt").reset_index(drop=True)
        self._cache[key] = df
        return df

    def window(self, symbol: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame | None:
        frames = [self.month(symbol, ym) for ym in _month_range(start, end)]
        frames = [f for f in frames if f is not None]
        if not frames:
            return None
        df = pd.concat(frames, ignore_index=True)
        df = df.drop_duplicates(subset="dt").sort_values("dt").reset_index(drop=True)
        return df[(df["dt"] >= start) & (df["dt"] <= end)].reset_index(drop=True)


def evaluate(loader: Loader, symbol: str, pre_day: pd.Timestamp, horizons, cost: float) -> dict:
    entry_dt = pre_day + pd.Timedelta(hours=23)          # 23:00 那根 K 线的开盘时刻
    max_h = max(horizons)
    raw = loader.window(symbol, entry_dt - pd.Timedelta(hours=1),
                        entry_dt + pd.Timedelta(days=max_h, hours=6))
    row: dict = {"entry_dt": entry_dt}
    if raw is None or raw.empty:
        row["entry_ok"] = False
        return row

    entry_row = raw[raw["dt"] == entry_dt]
    if entry_row.empty:
        # 23:00 那根缺失时退回 22:00 收盘（同一价格），仍算可入场
        prev = raw[raw["dt"] == entry_dt - pd.Timedelta(hours=1)]
        if prev.empty:
            row["entry_ok"] = False
            return row
        entry_price = float(prev["close"].iloc[0])
        row["entry_fallback"] = True
    else:
        entry_price = float(entry_row["open"].iloc[0])
        row["entry_fallback"] = False

    if not np.isfinite(entry_price) or entry_price <= 0:
        row["entry_ok"] = False
        return row
    row["entry_ok"] = True
    row["entry_price"] = entry_price

    after = raw[raw["dt"] > entry_dt]
    last_dt = raw["dt"].max()
    row["last_dt"] = last_dt

    for h in horizons:
        target = entry_dt + pd.Timedelta(days=h)
        seg = after[after["dt"] <= target]
        if seg.empty:
            row[f"covered_{h}"] = False
            continue
        exit_row = seg.iloc[-1]
        # 目标时刻附近必须有数据，否则视为该持有期未覆盖（断更/退市）
        if target - exit_row["dt"] > TOLERANCE:
            row[f"covered_{h}"] = False
            continue
        exit_price = float(exit_row["close"])
        ret = exit_price / entry_price - 1.0
        row[f"covered_{h}"] = True
        row[f"exit_dt_{h}"] = exit_row["dt"]
        row[f"exit_price_{h}"] = exit_price
        row[f"ret_{h}"] = ret
        row[f"ret_net_{h}"] = (1.0 + ret) * (1.0 - cost) - 1.0
        row[f"mfe_{h}"] = float(seg["high"].max()) / entry_price - 1.0
        row[f"mae_{h}"] = float(seg["low"].min()) / entry_price - 1.0
    return row


def _stats(s: pd.Series) -> dict:
    s = s.dropna()
    if s.empty:
        return {"n": 0}
    win = s > 0
    gains = s[win].sum()
    losses = -s[~win].sum()
    return {
        "n": int(len(s)),
        "median": float(s.median()),
        "mean": float(s.mean()),
        "win_rate": float(win.mean()),
        "p25": float(s.quantile(0.25)),
        "p75": float(s.quantile(0.75)),
        # 亏损样本少于 10 个时盈亏比没有意义（除以接近 0 会炸成 1e4 量级），置空
        "payoff": float(gains / losses) if (losses > 1e-9 and int((~win).sum()) >= 10) else np.nan,
        "p_gt_50": float((s > 0.5).mean()),
        "p_lt_-20": float((s < -0.2).mean()),
    }


SCORE_BINS = [(0, 4, "0-4"), (5, 7, "5-7"), (8, 10, "8-10"), (11, 18, "11-18")]


def _build_summary(out: pd.DataFrame, horizons, args, n_samples: int) -> list[str]:
    lines: list[str] = []
    P = lines.append
    off = args.entry_offset_days

    P("# 前瞻收益结果（13_forward_return）")
    P("")
    P(f"- 样本：`data/hit_rate_current.csv` 共 {n_samples} 条")
    P(f"- 成本假设：{args.cost:.2%} 双边（手续费 + 滑点）")
    if off == 0:
        P("- 入场：`pre_day` 23:00 K 线开盘价（= 22:00 收盘，决策当下可成交价）")
        P("- **口径：启动日前一天入场** —— 回答「能不能提前发现」")
    else:
        P(f"- 入场：`pre_day + {off} 天` 23:00 K 线开盘价")
        P("- **口径：启动日收盘入场** —— 回答「实盘当天扫到再买会怎样」")
    P("")
    P("> **两条必须随数字一起读的边界**")
    P(">")
    P("> 1. 1,402 条里 822 条是「事后确认会翻倍」的爆拉样本（58.6%），真实候选池远低于此。"
      "**池化后的绝对收益是上限，不是策略收益。**")
    P("> 2. 爆拉组的 `pre_day` 是启动日前一天，**下一根 K 线就是那根 +15% 的启动 K 线**。"
      "所以「1 天」这一列在爆拉组里等于直接吃标签，**不可用**。对照组没有这个问题。")
    P("")

    P("## 一、覆盖率")
    P("")
    P("| 持有期 | 可用样本 | 占比 |")
    P("|---|---:|---:|")
    entry_ok = int(out["entry_ok"].fillna(False).sum())
    P(f"| 入场成功 | {entry_ok} | {entry_ok / len(out):.1%} |")
    for h in horizons:
        c = int(out.get(f"covered_{h}", pd.Series(dtype=bool)).fillna(False).sum())
        P(f"| {h} 天 | {c} | {c / len(out):.1%} |")
    P("")

    def table(sub: pd.DataFrame, title: str, note: str = "") -> None:
        P(f"## {title}")
        P("")
        if note:
            P(f"> {note}")
            P("")
        P("| 持有期 | n | 净收益中位 | 均值 | 胜率 | p25 | p75 | 盈亏比 | >+50% | <−20% |")
        P("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        for h in horizons:
            col = f"ret_net_{h}"
            if col not in sub:
                continue
            st = _stats(sub[col])
            if not st["n"]:
                continue
            flag = " ⚠️" if (h == 1 and off == 0 and title.startswith("二")) else ""
            po = st["payoff"]
            # payoff 可能是 nan（亏损样本不足）；也可能极大（几乎全胜），>50 一律显示为 >50
            po_txt = "—" if po != po else (">50" if po > 50 else f"{po:.2f}")
            P(f"| {h} 天{flag} | {st['n']} | {st['median']:+.2%} | {st['mean']:+.2%} | "
              f"{st['win_rate']:.1%} | {st['p25']:+.2%} | {st['p75']:+.2%} | "
              f"{po_txt} | "
              f"{st['p_gt_50']:.1%} | {st['p_lt_-20']:.1%} |")
        P("")

    out = out.copy()
    sc = pd.to_numeric(out["score"], errors="coerce")
    out["_grp"] = pd.cut(sc, bins=[-0.5, 4, 7, 10, 18], labels=["0-4", "5-7", "8-10", "11-18"])
    pump = out[out["label"] == 1]
    ctrl = out[out["label"] == 0]

    table(pump, "二、爆拉组（label=1，n=%d）" % len(pump),
          "⚠️ 本组 1 天列等于吃启动 K 线，**跳过 1 天看后面的**。"
          "本组的用途是「如果当时选中了会怎样」，不是策略收益。")
    table(ctrl, "三、对照组（label=0，n=%d）" % len(ctrl),
          "本组是同币随机平静日，**没有启动标签污染**，是这批数据里最接近无偏的基准。"
          "但它仍只覆盖「历史上曾爆拉过的币」，带幸存者偏差。")

    P("## 四、分数梯度（本节是最可信的信号）")
    P("")
    P("分数若真有信息，应该在**同一组内部**也表现为单调：分越高、收益越好。")
    P("跨组比会被标签比例污染，组内比不会。")
    P("")
    for label_name, sub in (("对照组（label=0）", ctrl), ("爆拉组（label=1）", pump)):
        if sub.empty:
            continue
        P(f"### {label_name}")
        P("")
        for h in horizons:
            col = f"ret_net_{h}"
            if col not in out:
                continue
            cells = []
            for g in ["0-4", "5-7", "8-10", "11-18"]:
                st = _stats(sub.loc[sub["_grp"] == g, col])
                cells.append(f"{st['median']:+.2%} (n={st['n']})" if st["n"] >= 20 else "—")
            P(f"- **{h} 天**：0-4 {cells[0]} ｜ 5-7 {cells[1]} ｜ 8-10 {cells[2]} ｜ 11-18 {cells[3]}")
        P("")

    P("## 五、最大不利偏移（定止损用）")
    P("")
    P("| 持有期 | MAE 中位 | MAE p25 | MAE p10 | 对照组 MAE 中位 |")
    P("|---|---:|---:|---:|---:|")
    for h in horizons:
        col = f"mae_{h}"
        if col not in out:
            continue
        s = out[col].dropna()
        if s.empty:
            continue
        c = ctrl[col].dropna()
        cm = f"{c.median():+.2%}" if len(c) else "—"
        P(f"| {h} 天 | {s.median():+.2%} | {s.quantile(0.25):+.2%} | {s.quantile(0.10):+.2%} | {cm} |")
    P("")
    return lines



def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cost", type=float, default=DEFAULT_COST)
    ap.add_argument("--horizons", type=str, default=",".join(map(str, DEFAULT_HORIZONS)))
    ap.add_argument("--limit", type=int, default=0, help="仅用于调试，0 表示全量")
    ap.add_argument("--entry-offset-days", type=int, default=0,
                    help="0=pre_day 23:00（研究口径，启动前一天）；1=启动日 23:00（实盘口径）")
    ap.add_argument("--tag", type=str, default="", help="输出文件名后缀")
    args = ap.parse_args()

    horizons = [int(x) for x in args.horizons.split(",") if x.strip()]
    src = DATA / "hit_rate_current.csv"
    if not src.exists():
        print(f"[fatal] 缺少 {src}", file=sys.stderr)
        return 1

    samples = pd.read_csv(src)
    samples["pre_day"] = pd.to_datetime(samples["pre_day"])
    samples = samples.dropna(subset=["pre_day", "symbol"]).reset_index(drop=True)
    # 与 1h parquet 的 dt（UTC aware）对齐
    if samples["pre_day"].dt.tz is None:
        samples["pre_day"] = samples["pre_day"].dt.tz_localize("UTC")
    else:
        samples["pre_day"] = samples["pre_day"].dt.tz_convert("UTC")
    if args.limit:
        samples = samples.head(args.limit)
    print(f"[13] 样本 {len(samples)} 条，持有期 {horizons}，成本 {args.cost:.3%}")

    loader = Loader()
    rows = []
    for k, r in enumerate(samples.itertuples(index=False), 1):
        entry_day = r.pre_day + pd.Timedelta(days=args.entry_offset_days)
        rec = evaluate(loader, r.symbol, entry_day, horizons, args.cost)
        rec["symbol"] = r.symbol
        rec["pre_day"] = r.pre_day
        rec["entry_day"] = entry_day
        rec["label"] = getattr(r, "label", np.nan)
        rec["score"] = getattr(r, "score", np.nan)
        rec["pass_gate"] = getattr(r, "pass_gate", np.nan)
        rec["tier"] = getattr(r, "tier", np.nan)
        rows.append(rec)
        if k % 200 == 0:
            print(f"  ... {k}/{len(samples)}")

    out = pd.DataFrame(rows)
    tag = f"_{args.tag}" if args.tag else ""
    out_csv = DATA / f"forward_returns{tag}.csv"
    out.to_csv(out_csv, index=False, encoding="utf-8-sig")
    print(f"[13] 明细写入 {out_csv.relative_to(ROOT)}（{len(out)} 行）")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    lines = _build_summary(out, horizons, args, len(samples))
    summary = OUT_DIR / f"summary{tag}.md"
    summary.write_text("\n".join(lines), encoding="utf-8")
    print(f"[13] 汇总写入 {summary.relative_to(ROOT)}")

    meta = {
        "samples": int(len(samples)),
        "horizons": horizons,
        "cost": args.cost,
        "entry_offset_days": args.entry_offset_days,
        "entry_rule": ("pre_day 23:00 bar open (== 22:00 close)" if args.entry_offset_days == 0
                       else f"pre_day+{args.entry_offset_days}d 23:00 bar open"),
        "coverage": {str(h): int(out.get(f'covered_{h}', pd.Series(dtype=bool)).fillna(False).sum())
                     for h in horizons},
    }
    (OUT_DIR / f"meta{tag}.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
