#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""glm 独立验收聚合复算（只读 ds 交付产物 + 原始归档，不改任何 ds 产物）。

对账范围（对应任务书 8 类反例的聚合层）：
  A. 汇总自洽：integrity / baseline / reverse / events_summary / coverage
  B. attention 全量对账：从 event_best_rank（list[5932]）重算 covered_by_n
  C. 原始价格复算抽样：tier=10.0 主时段全部事件（≤27 个，逐一重读归档复算）
     + 缺资料事件抽样（验证「缺资料」去向属实、非静默删除）
  D. 已知契约缺口复核：F1（<25 OI 观测仍得分）、F2（随机排名挤占名次）、
     G1（reverse 无剩余机会字段）——只记录，不下结论。

输出：reports/history_research_glm/acceptance_checks.json + 控制台摘要。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import history_research_ds as H  # noqa: E402  冻结版（SHA bbbaa689...），只读调用

DS = ROOT / "reports" / "history_research_ds"
GLM = ROOT / "reports" / "history_research_glm"

CHECKS: list[dict] = []


def check(name: str, ok: bool, detail: str) -> None:
    CHECKS.append({"name": name, "ok": bool(ok), "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def load_json(name: str):
    return json.loads((DS / name).read_text(encoding="utf-8"))


# ------------------------------------------------------------------ A. 汇总自洽
def audit_summaries() -> None:
    cov = load_json("coverage.json")
    ig = load_json("integrity_lag0.json")
    bl = load_json("baseline_summary_lag0.json")
    rv = load_json("reverse_summary_lag0.json")
    ev = load_json("events_summary.json")

    n_sym, n_hours = ig["symbols"], ig["grid_hours"]
    check("A1 网格 = 符号 × 小时数", n_sym * n_hours == ig["total_cells"],
          f"{n_sym}x{n_hours}={n_sym * n_hours} vs {ig['total_cells']}")
    sc = ig["status_counts"]
    check("A2 状态和 = 总格数 (sum_check)", sum(sc.values()) == ig["total_cells"],
          f"{sum(sc.values())} vs {ig['total_cells']} (sum_check={ig['sum_check']})")
    check("A3 选中数一致 (integrity vs baseline)",
          sc["selected"] == bl["n_selected_hours"],
          f"{sc['selected']} vs {bl['n_selected_hours']}")
    check("A4 事件总数三处一致",
          ig["events_total"] == ev["total_events"] == rv["n_events"] == 5932,
          f"{ig['events_total']}/{ev['total_events']}/{rv['n_events']}")
    check("A5 分母完整性: not_observable 不冒充落选",
          sc["not_observable"] == 15383019 and "not_observable" in sc,
          f"not_observable={sc['not_observable']}（未观测≠未入选，分母保留）")
    disp = rv["disposition"]
    check("A6 去向和 = 事件总数", sum(disp.values()) == rv["n_events"],
          f"{disp} sum={sum(disp.values())}")
    bt_total = sum(sum(v.values()) for v in rv["by_tier"].values())
    check("A7 by_tier 和 = 事件总数", bt_total == rv["n_events"], f"{bt_total}")
    check("A8 coverage 主时段 = integrity 网格",
          cov["main_range"]["hours"] == n_hours and cov["counts"]["spot_usdt_symbols"] == n_sym,
          f"{cov['main_range']['hours']}h/{cov['counts']['spot_usdt_symbols']}sym")
    ps_sum = sum(p["n_events"] for p in ev["per_symbol"])
    check("A9 events per_symbol 和 = 总数", ps_sum == ev["total_events"],
          f"{ps_sum} vs {ev['total_events']} ({len(ev['per_symbol'])} symbols)")
    check("A10 无合约现货对象数", cov["counts"]["spot_without_futures"] == 282,
          f"{cov['counts']['spot_without_futures']} (282 预期)")


# ------------------------------------------------------- B. attention 全量对账
def audit_attention() -> None:
    att = load_json("attention_lag0.json")
    curve = [ln.split(",") for ln in
             (DS / "attention_curve_lag0.csv").read_text(encoding="utf-8").strip().splitlines()[1:]]
    for lane in ("score", "trend", "random_20261009", "random_20261010", "random_20261011"):
        ebr = att[lane]["event_best_rank"]
        assert len(ebr) == 5932, f"{lane}: ebr len {len(ebr)}"
        cbn = att[lane]["covered_by_n"]
        recomputed = {}
        covered_list = [r for r in ebr if r is not None]
        max_n = max(int(k) for k in cbn)
        for n in range(1, max_n + 1):
            recomputed[str(n)] = sum(1 for r in covered_list if r <= n)
        mism = [k for k in cbn if int(cbn[k]) != recomputed.get(k, -1)]
        check(f"B1 {lane}: covered_by_n 从 event_best_rank 全量重算",
              not mism,
              f"N=1..{max_n} 全对账{'一致' if not mism else f'，不符 N={mism[:5]}'}；"
              f"未覆盖事件={sum(1 for r in ebr if r is None)}")
    # 曲线文件抽 N=20 对账
    hdr = (DS / "attention_curve_lag0.csv").read_text(encoding="utf-8").splitlines()[0].split(",")
    row20 = next(r for r in curve if r[hdr.index("n")] == "20")
    sc20 = int(row20[hdr.index("score_covered")])
    check("B2 curve@N=20 vs attention json", sc20 == att["score"]["covered_by_n"]["20"],
          f"{sc20} vs {att['score']['covered_by_n']['20']}（候选卡 A 引用的每日去重前基线）")


# --------------------------------------------- C. 原始价格复算抽样（tier=10 全量）
MAIN_START_MS = 1640995200000          # 2022-01-01T00:00:00Z（主时段起点）


def m30_series(c: np.ndarray) -> np.ndarray:
    """与 ds detect_events 完全相同的 m30[t] = max(high[t+1..t+720])/close[t]（前瞻不含当根）。"""
    import pandas as pd
    high, close = c[:, 2], c[:, 4]
    n = len(c)
    A = pd.Series(high[::-1]).rolling(720, min_periods=1).max().to_numpy()[::-1]
    fwd = np.full(n, np.nan)
    if n >= 2:
        fwd[:n - 1] = A[1:]
    return fwd / close


def recompute_events() -> None:
    events = H.load_events()
    big = [e for e in events if e["tier"] >= 10.0 and e["start_ms"] >= MAIN_START_MS]
    out_of_main = sum(1 for e in events if e["tier"] >= 10.0) - len(big)
    rev = [json.loads(l) for l in
           (DS / "reverse_events_lag0.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    miss_ev = [e for e in rev if e.get("disposition") == "缺资料"]
    print(f"-- C: 主时段 tier>=10 事件 {len(big)} 个（主时段外 {out_of_main} 个不在复算范围）；"
          f"缺资料事件 {len(miss_ev)} 个，抽 3 个复核")

    ok_cnt = bad_cnt = skip_cnt = 0
    details = []
    for e in big:
        sym = e["symbol"]
        # 全量月份（复算须与 ds 同一数据范围：m30 为 720h 前瞻，且游程起点可能远早于事件）
        months = [f"{y:04d}-{m:02d}" for y in range(2017, 2027) for m in range(1, 13)]
        try:
            c = H.read_spot(sym, months)
        except Exception as ex:  # noqa: BLE001
            skip_cnt += 1
            details.append({"symbol": sym, "start": e["start_ms"], "error": str(ex)})
            continue
        if len(c) == 0:
            skip_cnt += 1
            details.append({"symbol": sym, "start": e["start_ms"], "error": "no archive rows"})
            continue
        ev2 = H.detect_events(c)
        match = [x for x in ev2 if x["start_ms"] == e["start_ms"]]
        if not match:
            bad_cnt += 1
            details.append({"symbol": sym, "start": e["start_ms"],
                            "error": f"event not reproduced ({len(ev2)} local events)"})
            continue
        m = match[0]
        a = int(np.searchsorted(c[:, 0], e["start_ms"]))
        b = int(np.searchsorted(c[:, 0], e["end_ms"]))
        m30 = m30_series(c)
        m30_max = float(np.nanmax(m30[a:b + 1])) if b >= a else float("nan")
        same = (m["tier"] == e["tier"] and m["n_segments"] == e["n_segments"]
                and abs(m30_max - e["max_m30"]) < 1e-3 * max(1.0, e["max_m30"])
                and bool(m["mature_30d"]) == bool(e["mature_30d"])
                and m["span_hours"] == e["span_hours"])
        fp = H.forward_path(c, a) if a < len(c) else None
        fp_m30 = fp["30d"][0] if fp else None
        # fp 从游程起点看：10× 爆拉通常发生在事件段更晚位置，起点 30d 窗内未必达到
        # tier → 只要求「起点有已发生的后续数据且不为补记值」（m30_max 对账已覆盖幅度）
        fp_ok = fp_m30 is not None and np.isfinite(fp_m30) and fp_m30 >= 1.0 - 1e-9
        ok_cnt += same and fp_ok
        if not (same and fp_ok):
            bad_cnt += 1
        details.append({"symbol": sym, "start": e["start_ms"], "reproduced": same,
                        "forward_path_ok": fp_ok, "fp_m30_at_start": fp_m30,
                        "m30_max_in_event": m30_max, "ds_m30": e["max_m30"]})
    check("C1 主时段 tier>=10 全量原始复算（detect_events + 事件内 m30 max + forward_path）",
          bad_cnt == 0 and skip_cnt == 0,
          f"ok={ok_cnt} bad={bad_cnt} skip(无归档)={skip_cnt} / {len(big)}")
    # 缺资料抽样：验证该币该时段归档确实无 K 线（去向属实、非删除失败）
    miss_ok = 0
    miss_details = []
    for e in miss_ev[:3]:
        sym = e["symbol"]
        ym = ms_to_ym(e["start_ms"]).replace("-", "")
        base = H.ARCHIVE / "spot" / "klines" / sym / "1h"
        has = (base / f"{sym}-1h-{ym}.zip").exists()
        n_rows = 0
        if has:
            c = H.read_spot(sym, [ym])
            n_rows = len(c)
        miss_details.append({"symbol": sym, "month": ym, "zip_exists": has, "rows": n_rows})
        miss_ok += (not has or n_rows == 0)
    check("C2 缺资料事件抽样 = 归档确无数据", miss_ok == min(3, len(miss_ev)),
          f"{miss_details}")
    (GLM / "_recompute_details.json").write_text(
        json.dumps({"tier10": details, "missing_sample": miss_details},
                   ensure_ascii=False, indent=1), encoding="utf-8")


def ms_to_ym(ms: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m")


# ------------------------------------------------- D. 契约缺口复核（只记录）
def audit_known_gaps() -> None:
    import tests.test_history_acceptance_glm as T  # noqa: PLC0415  复用测试锚
    # F1: 3 个 OI 点 <25 仍拿 10 分
    rows = T.TestFutureIsolation()._rows61()
    d59 = np.array([int(rows[59, 0])])
    dt = np.array([T.T0 - 72 * T.HOUR, T.T0 - 24 * T.HOUR, T.T0], dtype=np.int64)
    o = H.evaluate("X", rows, dt, np.array([100.0, 100.0, 150.0]),
                   np.array([0.8, 0.8, 0.8]), True, d59)
    check("D1/F1 <25 OI 观测仍得满分（生产应只给 LS 3 分）",
          int(o["score_deriv"][0]) == 10,
          "最小反例：3 点 → score_deriv=10（生产 deriv_as_of 契约应为 3）；"
          "status 仅降 partial，分数照加")
    # F2: 随机排名挤占名次
    total = np.array([[5, -1, 4]], dtype=np.int16)
    rk = H.random_rank_matrix(20261014, total)
    check("D2/F2 random_rank 不可评列挤占名次槽位", rk[0].tolist() == [2, 32767, 3],
          "最小反例 seed=20261014：rank1 消失 → random rank<=N 被系统性压低 → "
          "抬高 score 相对 lift（反保守）；rank_matrix 不受影响")
    # G1: reverse 无剩余机会字段
    rev1 = json.loads((DS / "reverse_events_lag0.jsonl").read_text(encoding="utf-8").splitlines()[0])
    has_remaining = any(k in rev1 for k in ("remaining_m30", "forward_after_mid", "max_m_after_mid"))
    check("D3/G1 reverse 中途发现无剩余机会字段", not has_remaining,
          "reverse_events 仅有 mid_selected_utc（时刻），无其后 forward_path/剩余倍数字段；"
          "任务书要求『中途发现要报告其后剩余机会，不能仅因晚于事后起点就归零』")
    # G2: "出现过晚" 死代码（cmd_reverse 内 first<=w1 恒真）
    src = (ROOT / "tools" / "history_research_ds.py").read_text(encoding="utf-8")
    check("D4/G2 cmd_reverse『出现过晚』分支死代码（静态）",
          "出现过晚" in src,
          "first 定义于 selwin=[w0,w1] 内 argmax → first<=w1 恒真；该分支永不触发（静态审查）")


def main() -> None:
    GLM.mkdir(parents=True, exist_ok=True)
    print("== glm 独立验收聚合复算 ==")
    audit_summaries()
    audit_attention()
    recompute_events()
    audit_known_gaps()
    n_fail = sum(1 for c in CHECKS if not c["ok"])
    out = {"checks": CHECKS, "n_pass": len(CHECKS) - n_fail, "n_fail": n_fail}
    (GLM / "acceptance_checks.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"== 完成: {len(CHECKS) - n_fail}/{len(CHECKS)} PASS, {n_fail} FAIL ==")
    print("输出: reports/history_research_glm/acceptance_checks.json")


if __name__ == "__main__":
    main()
