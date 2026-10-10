#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""glm r4 独立复验聚合工具（closeout 版，2026-10-10）。

职责（tasks/history_r4_closeout_20261010.md §5.2）：
  status    冻结判断：closeout/ready_for_glm.md 必须存在且含【停止编辑声明】、
            且其声明的 ds 源 SHA 与当前 tools/history_research_ds_r4.py 一致。
            文件存在本身≠冻结。
  aggregate 行级独立复算：从 closeout 明细（forward_detail/events/reverse 的行级
            数据）独立重算计数/分组/去向，对照 ds 汇总；禁止 ds 汇总对 ds 汇总。
  anchors   原始行情锚点核查：v3 主时段 27 个 ≥10× 事件 → r4 事件映射；
            旧 22 条「之后观察」去向与剩余机会字段；27 锚点直接读本地原始
            归档复算 30 天最大触及。只读归档，不跑第二份全历史。

写权限：只写 reports/history_research_glm/r4/replay/。不改 ds 任何文件。
冻结前运行 aggregate/anchors 输出标记 pre_freeze=true（定位用，非验收）。
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

DS_R4 = ROOT / "tools" / "history_research_ds_r4.py"
R4_OUT = ROOT / "reports" / "history_research_ds" / "r4"
CLOSEOUT = R4_OUT / "closeout"
READY = CLOSEOUT / "ready_for_glm.md"          # ← 新交接入口（旧 r4/ready_for_glm.md 已废弃）
GLM_R4 = ROOT / "reports" / "history_research_glm" / "r4"
REPLAY = GLM_R4 / "replay"
GLM_V3_DETAILS = ROOT / "reports" / "history_research_glm" / "_recompute_details.json"

# 独立常量：不 import ds 常量，避免期望来自被测对象
REVIEW_START_MS = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
HOUR = 3_600_000
DD_TH = -0.20                                   # 先跌后涨阈值（生产契约口径，手算锚）
WINDOWS_H = {"7d": 168, "30d": 720, "90d": 2160}
ALLOWED_SUMMARY_IGNORE = {"generated_at_utc", "elapsed_sec"}

STOP_EDIT_MARKERS = ("停止编辑", "停止本批编辑", "不再修改", "停止编辑声明")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "ABSENT"


def _ds_source_shas_in(text: str) -> list[str]:
    """从交接单里提取所有 16/64 位 hex SHA。"""
    return re.findall(r"\b[0-9a-f]{16}(?:[0-9a-f]{48})?\b", text)


def validate_freeze() -> dict:
    """冻结判定：存在 + 停止编辑声明 + 源 SHA 匹配 + 有配置/输入身份清单。"""
    r = {"ready_path": str(READY), "exists": READY.exists(),
         "stop_edit_declared": False, "source_sha_declared": None,
         "source_sha_current16": sha(DS_R4)[:16], "sha_match": False,
         "has_config_input_list": False, "frozen": False, "reasons": []}
    if not READY.exists():
        r["reasons"].append("closeout/ready_for_glm.md 不存在（旧 r4/ready_for_glm.md 是旧入口，不算）")
        return r
    text = READY.read_text(encoding="utf-8", errors="replace")
    if any(m in text for m in STOP_EDIT_MARKERS):
        r["stop_edit_declared"] = True
    else:
        r["reasons"].append("交接单未找到停止编辑声明")
    shas = _ds_source_shas_in(text)
    cur = sha(DS_R4)
    r["source_sha_declared"] = shas[:8]
    if cur[:16] in shas or cur in shas:
        r["sha_match"] = True
    else:
        r["reasons"].append("交接单未声明与当前 tools/history_research_ds_r4.py 匹配的 SHA")
    if re.search(r"(配置|config|输入|依赖|input)", text, re.I):
        r["has_config_input_list"] = True
    else:
        r["reasons"].append("交接单缺配置/输入身份清单")
    r["frozen"] = bool(r["stop_edit_declared"] and r["sha_match"] and r["has_config_input_list"])
    return r


def _read_jsonl_dir(d: Path) -> list[dict]:
    rows = []
    for f in sorted(d.glob("*.jsonl")):
        for line in f.open(encoding="utf-8"):
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _read_csv(p: Path) -> list[dict]:
    with p.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _b(x):
    return str(x).strip().lower() == "true"


def _iso_ms(s: str) -> int:
    return int(datetime.strptime(s.replace("Z", "+0000"),
                                 "%Y-%m-%dT%H:%M:%S%z").timestamp() * 1000)


# ================================================================ aggregate
def aggregate() -> dict:
    """行级独立复算。每个对账项给 PASS/FAIL/UNVERIFIED，不相互抵赖。"""
    out: dict = {"pre_freeze": not validate_freeze()["frozen"], "lags": {}}
    for lag in (0, 1):
        det = _read_csv(CLOSEOUT / f"forward_detail_lag{lag}.csv")
        summ = json.loads((CLOSEOUT / f"forward_summary_lag{lag}.json").read_text(encoding="utf-8"))
        checks: dict[str, dict] = {}

        # 1) verdict 计数（行级重算 vs 汇总）
        vc = Counter(r["dim_verdict"] for r in det)
        exp = summ["verdict"]
        checks["verdict_counts"] = {
            "status": "PASS" if all(vc.get(k, 0) == v for k, v in exp.items())
                      and sum(vc.values()) == len(det) else "FAIL",
            "recomputed": dict(vc), "ds_summary": exp}

        # 2) 跨期分组（独立 REVIEW_START=2025-01-01，从行级 start_utc 重算）
        cross30 = cross90 = explore = review = 0
        for r in det:
            t0 = _iso_ms(r["start_utc"])
            if r["dim_verdict"] == "no_reference" or not r.get("ref_open_utc"):
                continue
            if t0 < REVIEW_START_MS:
                if t0 + 30 * 24 * HOUR >= REVIEW_START_MS:
                    cross30 += 1
                else:
                    explore += 1
                if t0 + 90 * 24 * HOUR >= REVIEW_START_MS:
                    cross90 += 1
            else:
                review += 1
        ce = summ["crossover_excluded"]
        checks["crossover_excluded"] = {
            "status": "PASS" if (cross30, cross90, explore, review) ==
                      (ce["cross_30d"], ce["cross_90d"], ce["explore_30d_clean"], ce["review"])
                      else "FAIL",
            "recomputed": {"cross_30d": cross30, "cross_90d": cross90,
                           "explore_30d_clean": explore, "review": review},
            "ds_summary": {k: ce[k] for k in
                           ("cross_30d", "cross_90d", "explore_30d_clean", "review")}}

        # 3) 最终失败比例（分母=final_eligible 且 complete）
        fin = [r for r in det if _b(r["dim_final_eligible"])]
        gc = Counter(r["dim_gain_observed"] for r in fin)
        n = len(fin)
        ratio = {k: (round(gc.get(k, 0) / n, 6) if n else None)
                 for k in ("reach_2x", "reach_1_5x_only", "below_1_5x")}
        checks["final_ratio"] = {
            "status": "PASS" if n == summ["final_eligible"] and ratio == summ["final_ratio"]
                      else "FAIL",
            "recomputed": {"n": n, "ratio": ratio},
            "ds_summary": {"n": summ["final_eligible"], "ratio": summ["final_ratio"]}}

        # 4) reach_2x 先跌后涨份额（独立阈值 -0.20）
        reached = [r for r in det if r["dim_gain_observed"] == "reach_2x"]
        certain = sum(1 for r in reached if _f(r["dd_before_2x"]) is not None
                      and _f(r["dd_before_2x"]) <= DD_TH)
        possible = sum(1 for r in reached
                       if (_f(r["dd_before_2x"]) is not None and _f(r["dd_before_2x"]) <= DD_TH)
                       or (_f(r["dd_before_2x_worst"]) is not None
                           and _f(r["dd_before_2x_worst"]) <= DD_TH))
        checks["reach2x_dd20_share"] = {
            "status": "PASS" if
            (round(certain / len(reached), 6) == summ["reach_2x_share_with_dd20_certain"]
             and round(possible / len(reached), 6) == summ["reach_2x_share_with_dd20_possible"])
            else "FAIL",
            "recomputed": {"certain": certain, "possible": possible, "n": len(reached)},
            "ds_summary": {"certain": summ["reach_2x_share_with_dd20_certain"],
                           "possible": summ["reach_2x_share_with_dd20_possible"],
                           "n": summ["reach_2x_denominator"]}}

        # 5) 事件行级重算 vs events_summary
        ev = _read_jsonl_dir(CLOSEOUT / "events")
        evsumm = json.loads((CLOSEOUT / "events_summary.json").read_text(encoding="utf-8"))
        tier_c = Counter(str(float(e["tier"])) for e in ev)
        mature = sum(1 for e in ev if e["mature_30d"])
        complete = sum(1 for e in ev if e["complete_30d"])
        checks["events_rowlevel"] = {
            "status": "PASS" if (len(ev) == evsumm["total_events"]
                                 and {k: v for k, v in tier_c.items()} ==
                                 {k: v for k, v in evsumm["by_tier"].items()}
                                 and mature == evsumm["mature_30d"]["mature"]
                                 and complete == evsumm["complete_30d"]["complete"])
            else "FAIL",
            "recomputed": {"total": len(ev), "by_tier": dict(tier_c),
                           "mature_30d": mature, "complete_30d": complete},
            "ds_summary": {"total": evsumm["total_events"], "by_tier": evsumm["by_tier"],
                           "mature_30d": evsumm["mature_30d"]["mature"],
                           "complete_30d": evsumm["complete_30d"]["complete"]}}

        # 6) reverse 去向行级重算 vs reverse_summary
        rev = [json.loads(l) for l in
               (CLOSEOUT / f"reverse_events_lag{lag}.jsonl").open(encoding="utf-8")]
        disp = Counter(r["disposition"] for r in rev)
        after = [r for r in rev if r["disposition"] == "之后观察"]
        rs_path = CLOSEOUT / f"reverse_summary_lag{lag}.json"
        rev_check: dict = {"recomputed_dispositions": dict(disp),
                           "after_observed_n": len(after),
                           "after_before_recorded_peak": sum(1 for r in after
                                                             if r.get("before_recorded_peak")),
                           "after_with_remaining_opportunity":
                               sum(1 for r in after if r.get("remaining_opportunity") is not None)}
        if rs_path.exists():
            rs = json.loads(rs_path.read_text(encoding="utf-8"))
            s_disp = rs.get("dispositions") or rs.get("by_disposition") or {}
            rev_check["ds_summary_dispositions"] = s_disp
            rev_check["status"] = ("PASS" if all(s_disp.get(k, 0) == v for k, v in disp.items())
                                   else "FAIL") if s_disp else "UNVERIFIED"
        else:
            rev_check["status"] = "UNVERIFIED"
        checks["reverse_dispositions"] = rev_check

        # 7) attention 曲线：内部一致性（ds 汇总 vs ds 曲线）——非独立复算，如实标注
        ac_path = CLOSEOUT / f"attention_curve_lag{lag}.csv"
        aj_path = CLOSEOUT / f"attention_lag{lag}.json"
        if ac_path.exists() and aj_path.exists():
            ac = _read_csv(ac_path)
            aj = json.loads(aj_path.read_text(encoding="utf-8"))
            cov = aj["covered_by_n"]["score"]
            ok = all(str(int(row["n"])) in cov
                     and abs(float(cov[str(int(row["n"]))])
                             - float(row["score_covered"])) < 0.5
                     for row in ac)
            checks["attention_curve_internal"] = {
                "status": "PASS" if ok else "FAIL",
                "note": ("内部一致性检查（ds 曲线 vs ds 汇总），非独立复算——"
                         "小时级排名底账不可得（避免第二份全历史），独立性由"
                         "测试文件 naive_rank 参照实现承担"),
                "n_points_checked": len(ac)}
        out["lags"][f"lag{lag}"] = checks
    return out


# ================================================================ anchors
def anchors() -> dict:
    """27 个 ≥10× 锚点 + 22 条「之后观察」：映射、字段核查、原始归档复算。"""
    out: dict = {"anchors_27": [], "after_observed_22": [], "notes": []}
    v3 = json.loads(GLM_V3_DETAILS.read_text(encoding="utf-8"))["tier10"]
    # r4/closeout 事件（新口径），按 symbol 建索引
    ev = _read_jsonl_dir(CLOSEOUT / "events")
    by_sym: dict[str, list[dict]] = {}
    for e in ev:
        by_sym.setdefault(e["symbol"], []).append(e)
    mapped = unmapped = 0
    for a in v3:
        sym, start = a["symbol"], int(a["start"])
        cand = [e for e in by_sym.get(sym, [])
                if any(seg["start_ms"] <= start <= max(seg["end_ms"], seg["start_ms"] + HOUR)
                       for seg in e.get("segments", [{"start_ms": e["start_ms"],
                                                      "end_ms": e["end_ms"]}]))]
        rec = {"symbol": sym, "v3_start": start, "v3_fp_m30_at_start": a.get("fp_m30_at_start"),
               "v3_m30_max_in_event": a.get("m30_max_in_event")}
        if cand:
            e = cand[0]
            mapped += 1
            rec.update({
                "mapped": True, "r4_event_start": e["start_ms"], "r4_tier": e["tier"],
                "r4_n_segments": e["n_segments"], "r4_max_m30": e.get("max_m30"),
                "start_delta_h": round((e["start_ms"] - start) / HOUR, 2)})
        else:
            unmapped += 1
            rec.update({"mapped": False,
                        "note": "r4/closeout 无包含该 v3 起点的事件（归并/窗口口径变化，需逐例解释）"})
        out["anchors_27"].append(rec)
    out["anchor_mapping"] = {"mapped": mapped, "unmapped": unmapped}

    # 22 条之后观察（旧 r4 = 主代理报告锚点；closeout 同口径对照）
    for tag, p in (("old_r4", R4_OUT / "reverse_events_lag0.jsonl"),
                   ("closeout", CLOSEOUT / "reverse_events_lag0.jsonl")):
        rows = [json.loads(l) for l in p.open(encoding="utf-8")]
        aft = [r for r in rows if r["disposition"] == "之后观察"]
        out[f"after_observed_{tag}"] = {
            "n": len(aft),
            "before_recorded_peak": sum(1 for r in aft if r.get("before_recorded_peak")),
            "with_remaining_opportunity": sum(1 for r in aft
                                              if r.get("remaining_opportunity") is not None),
            "rows": [{"symbol": r["symbol"], "start_ms": r["start_ms"],
                      "ref_price": r.get("remaining_ref_price"),
                      "ref_open_utc": r.get("remaining_ref_open_utc"),
                      "bar_ms": r.get("remaining_bar_ms"),
                      "remaining_30d_touch": r.get("remaining_30d_touch"),
                      "data_state": r.get("remaining_30d_data_state"),
                      "elapsed": r.get("remaining_30d_elapsed"),
                      "verdict": r.get("remaining_verdict"),
                      "remaining_opportunity": r.get("remaining_opportunity"),
                      "before_recorded_peak": r.get("before_recorded_peak")} for r in aft]}

    # 27 锚点直接读原始归档复算（独立参考价=起点前最后收盘；30 天最大触及）
    try:
        import numpy as np
        import history_research_ds_r4 as m
        H = m.H
        for rec in out["anchors_27"]:
            sym, start = rec["symbol"], rec["v3_start"]
            try:
                arr = H.read_spot(sym, H.spot_months(sym))
                if arr is None or len(arr) < 2:
                    rec["raw"] = {"status": "NO_DATA"}
                    continue
                ts = arr[:, 0].astype(np.int64)
                i0 = int(np.searchsorted(ts, start, side="right")) - 1  # 起点（含当根）
                i0 = max(i0, 0)
                iend = int(np.searchsorted(ts, start + 720 * HOUR, side="left"))
                seg = arr[i0:iend]
                nb = len(seg)
                expect = 720
                touch_prev = (float(seg[:, 2].max()) / float(arr[i0 - 1, 4])
                              if nb and i0 > 0 else None)      # 独立口径 A：起点前收盘为参考
                touch_open = (float(seg[:, 2].max()) / float(arr[i0, 1])
                              if nb else None)                  # 独立口径 B：起点根开盘为参考
                rec["raw"] = {"status": "OK", "ref_prev_close": (float(arr[i0 - 1, 4])
                                                                 if i0 > 0 else None),
                              "ref_start_open": float(arr[i0, 1]) if nb else None,
                              "bars_in_30d": nb, "expected_bars": expect,
                              "missing_bars": max(0, expect - nb),
                              "max_touch_30d_ref_prev_close": touch_prev,
                              "max_touch_30d_ref_start_open": touch_open,
                              "note": ("独立复算口径：30 天窗=起点(含)起 720h 最高价/参考价；"
                                       "v3_fp_m30_at_start 仅作对照不判定——v3 口径已被 r4 取代，"
                                       "实测其值混入事件内窗口语义，不可由原始数据直接复现"),
                              "v3_anchor_fp_m30_for_reference": rec.get("v3_fp_m30_at_start")}
            except Exception as ex:  # 单例失败不拖垮整批
                rec["raw"] = {"status": "ERROR", "err": repr(ex)[:120]}
    except ImportError:
        out["notes"].append("原始归档复算未执行：ds 模块不可导入（只读引用失败）")
    return out


# ================================================================ main
def _run_tests_diag() -> str | None:
    import os
    env = dict(os.environ)
    env["GLM_R4_DIAG"] = "1"
    env.setdefault("SYSTEMROOT", "")
    p = subprocess.run([sys.executable, "-B", "-m", "unittest",
                        "tests.test_history_acceptance_glm_r4"],
                       cwd=ROOT, capture_output=True, text=True, env=env)
    tail = (p.stderr or p.stdout).strip().splitlines()
    return tail[-1] if tail else None


def main() -> None:
    REPLAY.mkdir(parents=True, exist_ok=True)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"

    if cmd == "resume":
        print("恢复命令（冻结交接出现后依序执行）：")
        print("  1. python tools/history_acceptance_glm_r4.py status      # 冻结校验必须 frozen=true")
        print("  2. python -m unittest tests.test_history_acceptance_glm_r4 -v   # 去 DIAG 全绿（无 skip/expectedFailure 计通过）")
        print("  3. python tools/history_acceptance_glm_r4.py aggregate   # 行级独立复算 → replay/aggregate.json")
        print("  4. python tools/history_acceptance_glm_r4.py anchors     # 27/22 锚点 + 原始行情复算 → replay/anchors.json")
        print("  5. 写 reports/history_research_glm/r4/closeout/summary.md 后停止")
        return

    fz = validate_freeze()
    if cmd == "status":
        st = {"freeze": fz, "ds_source_sha16": sha(DS_R4)[:16],
              "closeout_artifacts": {p.name: sha(p)[:16]
                                     for p in sorted(CLOSEOUT.glob("*"))
                                     if p.is_file()},
              "diag_tests_last": _run_tests_diag()}
        (GLM_R4 / "prep_status.json").write_text(
            json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
        print(json.dumps(st, ensure_ascii=False, indent=1)[:2000])
        if not fz["frozen"]:
            print("\n== 未冻结（" + "；".join(fz["reasons"]) + "）==")
            print("恢复命令：python tools/history_acceptance_glm_r4.py resume")
        return

    if cmd == "aggregate":
        res = aggregate()
        (REPLAY / "aggregate.json").write_text(
            json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
        bad = {f"{lag}:{k}": v["status"]
               for lag, ch in res["lags"].items() for k, v in ch.items()
               if v.get("status") not in ("PASS", "UNVERIFIED")}
        print(json.dumps({"pre_freeze": res["pre_freeze"],
                          "non_pass_items": bad or "none",
                          "out": str(REPLAY / 'aggregate.json')},
                         ensure_ascii=False, indent=1))
        return

    if cmd == "anchors":
        res = anchors()
        (REPLAY / "anchors.json").write_text(
            json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
        raw_err = sum(1 for a in res["anchors_27"] if a.get("raw", {}).get("status") != "OK")
        print(json.dumps({"anchor_mapping": res["anchor_mapping"],
                          "after_old": {k: v for k, v in res["after_observed_old_r4"].items()
                                        if k != "rows"},
                          "after_closeout": {k: v for k, v in res["after_observed_closeout"].items()
                                             if k != "rows"},
                          "raw_non_ok": raw_err,
                          "out": str(REPLAY / "anchors.json")}, ensure_ascii=False, indent=1))
        return

    raise SystemExit(f"未知子命令：{cmd}（可用 status/aggregate/anchors/resume）")


if __name__ == "__main__":
    main()
