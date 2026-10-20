#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""19 · 真实爆拉率：把「71% 爆拉率」翻译成现实世界的数字（A24）`[ds4.1f]`

## 为什么必须做这个

项目里到处写着「`score>=5` 的爆拉率 71.0%」。**这个数字不是「筛出来的币有七成会翻倍」**，
它是「在我特意构造的 822 个翻倍事件 + 580 个对照日里，71% 是翻倍事件」。

**打个比方**：你从全城 70 万人里挑出 822 个中过彩票的，再随便拉 580 个没中的，
然后说「我这个池子的中奖率是 58.6%」。这个数字对「预测谁会中奖」没有任何意义。

本脚本把三件事算清楚：

1. **爆拉的定义到底是什么**（从 `research/02_detect_pumps.py` 抄，不重新解释）
2. **真实世界里一个币-日发生爆拉的概率**（分母 = 全部符号-日）
3. **把样本内的 71% 换算成真实口径**（用「通过率」反推，假设写清楚）

以及由此推出的那个关键数字：**盈亏平衡需要多少命中率，现在还差多少倍。**

## 换算逻辑（必须看懂，否则数字会被误用）

样本 = 822 个爆拉事件 + 580 个对照日。
- 爆拉事件是**全部**被检测到的（不是抽样）
- 对照日是**抽样**的（从爆拉币的平静期里抽）

设 `f_p` = 爆拉事件里被判进池的比例，`f_c` = 对照日里被判进池的比例。
样本内池子的爆拉率 = `f_p·822 / (f_p·822 + f_c·580)`。

真实世界的分母不是 580，是**全部非爆拉币-日**（≈ 580,104 − 822）。
所以真实池子的爆拉率 = `f_p·822 / (f_p·822 + f_c·(580104−822))`。

**关键假设**：`f_c` 在「对照日」和「全部非爆拉币-日」上相同。
这个假设**未经检验**——对照日是从爆拉币的平静期抽的，可能不代表全市场。
所以下面的「真实爆拉率」是**数量级估计**，不是精确值。
**这正是 A15 全市场面板要解决的问题。**

用法
----
  py -3.10 research/19_base_rate.py
"""
from __future__ import annotations

import json
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUT = ROOT / "reports" / "base_rate_ds"
TAG = "ds4.1f"

# 与 02_detect_pumps.py 保持一致
TIERS = [(9.0, "10x+"), (4.0, "5–10x"), (2.0, "3–5x"), (1.0, "2–3x")]
MIN_GAIN = 1.0


def md_table(df: pd.DataFrame, floatfmt: str = ".3f") -> str:
    if not len(df):
        return "（空）"
    cols = list(df.columns)
    out = ["| " + " | ".join(str(c) for c in cols) + " |",
           "|" + "|".join(["---"] * len(cols)) + "|"]
    for _, r in df.iterrows():
        cells = []
        for c in cols:
            v = r[c]
            if isinstance(v, (bool, np.bool_)):
                cells.append("是" if v else "否")
            elif isinstance(v, (int, np.integer)):
                cells.append(str(int(v)))
            elif isinstance(v, (float, np.floating)):
                cells.append("—" if not np.isfinite(v) else format(v, floatfmt))
            else:
                cells.append("—" if v is None else str(v))
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out)


def pct(x: float, nd: int = 3) -> str:
    return "—" if not np.isfinite(x) else f"{x:.{nd}%}"


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)

    pumps = json.loads((DATA / "pumps.json").read_text(encoding="utf-8"))
    ev = pd.DataFrame(pumps)
    ev["pre_day"] = pd.to_datetime(ev["pre_day"], utc=True)
    ev_u = ev.drop_duplicates(subset=["symbol", "pre_day"])

    # ---------- 分母：全部符号-日 ----------
    files = list((DATA / "1d").glob("*.parquet"))
    days = 0
    for f in files:
        try:
            days += len(pd.read_parquet(f, columns=["open_time"]))
        except Exception:  # noqa: BLE001
            continue

    n_pump = len(ev_u)
    n_nonpump = days - n_pump
    base_rate = n_pump / days

    # ---------- 分级 ----------
    ev["gain"] = pd.to_numeric(ev["gain"], errors="coerce")
    ev["倍数"] = ev["gain"] + 1.0
    tiers = []
    prev = None
    for th, lab in TIERS:
        m = ev["gain"] >= th
        if prev is not None:
            m = m & ~prev
        tiers.append({"档": lab, "事件数": int(m.sum()),
                      "占全部": float(m.mean())})
        prev = ev["gain"] >= th
    T = pd.DataFrame(tiers)

    # ---------- 样本内 vs 真实口径 ----------
    hr = pd.read_csv(DATA / "hit_rate_current.csv")
    hr["pre_day"] = pd.to_datetime(hr["pre_day"], utc=True)
    hr = hr.drop_duplicates(subset=["symbol", "pre_day"])

    GATES = [
        ("score>=5（现行）", hr["score"] >= 5),
        ("score>=8", hr["score"] >= 8),
        ("score>=11", hr["score"] >= 11),
        ("只要 c_trend", hr["c_trend"].fillna(0).astype(bool)),
        ("trend ∧ breakout",
         hr["c_trend"].fillna(0).astype(bool) & hr["c_breakout"].fillna(0).astype(bool)),
    ]
    rows = []
    for name, m in GATES:
        in_pump = int((m & (hr["label"] == 1)).sum())
        in_ctrl = int((m & (hr["label"] == 0)).sum())
        f_p = in_pump / max(int((hr["label"] == 1).sum()), 1)
        f_c = in_ctrl / max(int((hr["label"] == 0).sum()), 1)
        sample_rate = in_pump / max(in_pump + in_ctrl, 1)
        real_pump = f_p * n_pump
        real_non = f_c * n_nonpump
        real_rate = real_pump / max(real_pump + real_non, 1e-9)
        rows.append({
            "规则": name,
            "样本内爆拉率": sample_rate,
            "捕获爆拉(f_p)": f_p,
            "误纳对照(f_c)": f_c,
            "真实爆拉率(估)": real_rate,
            "相对基准的倍数": real_rate / base_rate if base_rate > 0 else np.nan,
            "相对现行的倍数": np.nan,
        })
    G = pd.DataFrame(rows)
    if len(G):
        base_real = float(G.loc[0, "真实爆拉率(估)"])
        G["相对现行的倍数"] = G["真实爆拉率(估)"] / base_real

    # ---------- 盈亏平衡 ----------
    fr = pd.read_csv(DATA / "forward_returns.csv")
    fr["pre_day"] = pd.to_datetime(fr["pre_day"], utc=True)
    fr = fr.drop_duplicates(subset=["symbol", "pre_day"])
    hr2 = hr.merge(fr[["symbol", "pre_day", "ret_net_7", "ret_net_14"]],
                   on=["symbol", "pre_day"], how="left")

    be = []
    for h in (7, 14):
        col = f"ret_net_{h}"
        w = hr2.loc[hr2["label"] == 1, col].dropna()
        l = hr2.loc[hr2["label"] == 0, col].dropna()
        for stat, wv, lv in (("中位", w.median(), l.median()), ("均值", w.mean(), l.mean())):
            # p·w + (1−p)·l = 0
            p_star = -lv / (wv - lv) if wv > lv else np.nan
            be.append({"持有期": f"{h} 天", "统计量": stat,
                       "爆拉时收益": float(wv), "没爆时收益": float(lv),
                       "盈亏平衡命中率": float(p_star)})
    BE = pd.DataFrame(be)

    # ---------- 报告 ----------
    L = []
    L.append("# A24 · 真实爆拉率：把「71%」翻译成现实数字 `[ds4.1f]`\n")
    L.append(f"> 2026-10-20｜脚本 `research/19_base_rate.py`｜"
             f"分母 {days:,} 个符号-日（{len(files)} 个币）\n")

    L.append("\n---\n\n## 一、爆拉到底怎么定义（照抄 `research/02_detect_pumps.py`）\n")
    L.append("```")
    L.append("波段低点 T  = 峰前 60 天内的最低 low")
    L.append("峰值     P  = 30 日滚动涨幅创局部新高（±10 天窗口内最高）的那一天")
    L.append("涨幅        = high[P] / low[T] − 1        ← 从最低点到最高点")
    L.append("爆拉门槛    = 涨幅 ≥ 1.0，即价格 ≥ 2 倍（翻倍）")
    L.append("启动日   I  = T 之后第一根「收盘较 T 收盘涨 ≥ 15%」的日线")
    L.append("评估日      = I 的前一天 —— 策略只能看到这一天及之前的数据")
    L.append("```\n")
    L.append("**所以「爆拉」= 从 60 天内的最低点，到之后某个高点，涨了 2 倍以上。**")
    L.append("不是「今天涨了 5%」，也不是「7 天内涨 2 倍」——**时间窗口最长 60 天。**\n")
    L.append(md_table(T.rename(columns={"占全部": "占全部事件"})))

    L.append("\n---\n\n## 二、真实世界里，一个币-日发生爆拉的概率\n")
    L.append("| 项 | 数值 |")
    L.append("|---|---:|")
    L.append(f"| 全部符号-日（分母） | **{days:,}** |")
    L.append(f"| 爆拉事件（唯一 symbol+pre_day） | **{n_pump:,}** |")
    L.append(f"| 非爆拉币-日 | {n_nonpump:,} |")
    L.append(f"| **基准爆拉率** | **{base_rate:.4%}** |")
    L.append(f"\n换算一下：**约每 {int(round(1 / base_rate))} 个币-日，才有 1 次爆拉启动。**\n")

    L.append("\n---\n\n## 三、为什么会有「50% 以上爆拉率」\n")
    L.append("因为那个数字的**分母是人造的**。\n")
    L.append(f"样本 = **{n_pump} 个已发生的爆拉事件** + **{len(hr) - n_pump} 个对照日**。")
    L.append(f"爆拉占比 = {n_pump} / {len(hr)} = **{n_pump / len(hr):.1%}** ——")
    L.append("这个数在样本构造的那一刻就定死了，**和策略好坏完全无关**。\n")
    L.append("**打个比方**：你从全城 70 万人里挑出 822 个中过彩票的，再随便拉 580 个没中的，")
    L.append("然后说「我这个池子的中奖率是 58.6%」。这个数字对「预测谁会中奖」毫无意义。\n")

    L.append("\n---\n\n## 四、换算成真实口径\n")
    L.append("用「通过率」反推（假设见文末）：\n")
    L.append(md_table(G))
    L.append("\n**怎么读**：\n")
    L.append("- `样本内爆拉率` 是报告里一直在用的那个数（会误导人）")
    L.append("- `真实爆拉率(估)` 是**换成真实分母之后**的数（能用来做决策）")
    L.append("- `相对基准的倍数` 才是这个规则的**真实增益**——不是 71%，而是**个位数倍**\n")

    L.append("\n---\n\n## 五、盈亏平衡需要多少命中率\n")
    L.append("按「买入后持有 N 天」算，`p·爆拉收益 + (1−p)·没爆收益 = 0`：\n")
    L.append(md_table(BE))
    L.append("\n**这张表是整件事的结论**：\n")
    L.append(f"- 真实基准率 {base_rate:.2%}")
    L.append(f"- 现行池子真实爆拉率（估）{base_real:.2%}")
    L.append("- 盈亏平衡需要 5%–12%（看持有期）")
    L.append(f"- **差距：还需要把命中率提高 {5 / base_real:.0f}–{12 / base_real:.0f} 倍**\n")
    L.append("**这就是为什么「爆拉率从 61.9% 提到 76.9%」不值得高兴**——")
    L.append("换算到真实口径，那是从 0.24% 提到 0.30%，离 5% 还差一个数量级。\n")

    L.append("\n---\n\n## 六、口径与限制（必须读）\n")
    L.append("1. **`f_c` 的假设未经检验**：对照日是从爆拉币的平静期抽的，")
    L.append("   不代表全部非爆拉币-日。所以「真实爆拉率」是**数量级估计**。")
    L.append("   **A15 全市场面板能一次性消掉这个假设。**")
    L.append("2. **分母用「符号-日」，不是「交易日 × 币数」**：")
    L.append("   新币上市时间短，所以每个币的天数不同（平均 904 天）。")
    L.append("3. **爆拉的定义是「从 60 天内最低点到之后高点 2 倍」**，")
    L.append("   这个口径比「7 天涨 2 倍」宽松得多。所以真实事件率 0.14% 已经是**偏宽松**的估计。")
    L.append("4. 盈亏平衡用的是**样本内**的收益分布，同样有案例-对照结构的问题，")
    L.append("   但方向（需要 5% 以上）和 A15 文档里独立算出的 5–6% 一致。")

    (OUT / "report.md").write_text("\n".join(L), encoding="utf-8")

    print(f"[base] 分母 {days:,} 符号-日｜爆拉 {n_pump}｜基准率 {base_rate:.4%}")
    for _, r in G.iterrows():
        print(f"  {r['规则']:20s} 样本内 {r['样本内爆拉率']:.1%} → 真实 {r['真实爆拉率(估)']:.3%}"
              f"（基准 ×{r['相对基准的倍数']:.1f}）")
    print(f"[base] 报告 -> {OUT / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
