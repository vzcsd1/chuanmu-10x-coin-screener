#!/usr/bin/env python3
"""历史双向研究 · 有限返修 r4（ds，2026-10-10）。

依据：`tasks/ds_history_repair_20261010.md`；问题与可重跑证据唯一见
`reports/history_research_review/summary.md` 与 `reports/history_research_review/evidence_20261010.json`。

本批只做**计算订正**，不新增策略、不改生产、不联网。

硬边界（违反即失败）：
  · 不联网：运行期安装 socket 守卫（`b4_replay_ds.NetworkGuard`），任何外连直接抛错并计数
  · 不改生产评分：只 import `binance_box_strategy` 的阈值/权重/`deriv_score`（只读）
  · **只写 `reports/history_research_ds/r4/`**；v3 产物、glm 文件、公共文档、生产代码一律不写
  · 事件库独立于评分；评分入口接触不到事件表与未来价格

复用声明（重要）
----------------
r4 **复用** `tools/history_research_ds.py`（v3，冻结 SHA 见 `REUSE_V3_SHA256`）中**未受本批
修正影响**的函数：归档枚举与读取（`usdt_*_symbols` / `spot_months` / `metrics_days` /
`read_spot` / `read_spot_interval` / `read_metrics_hourly`）、K 线指标与 K 线侧评分
（`kline_frame` / `kline_scores`）、`quote_volume_24h`，以及全部常量。
调用旧函数**只读取**，绝不让它们写回 v3 的输出目录；r4 自己重写所有受影响的函数。
`verify_reuse()` 会校验 v3 源码哈希，不符即拒绝运行。

受影响并已在 r4 重写的函数（见各段注释）：
  R1/F1 `deriv_scores_r4`      —— 有效 OI 观测 < 25 时不得产生涨幅与得分
  R1/F2 `_rank_rows_r4`        —— 先排除不可评分列再排名
  R2    `forward_from_series_r4` —— 参考开盘、真实时刻截止、三分开、路径上下界
  R3    `detect_events_r4`     —— 7/30/90 天窗口与 48h 合并按真实时间；peak 指向真实高点
  R4    `by_score` 分组改用**首个信号当时**的分数
  R5    `cmd_attention_r4` / `cmd_retention_r4` —— 全范围曲线 + 真实人工负担
  R6    `cmd_supplementary_r4` —— 恢复可复跑的补充结果生成入口

closeout 收口（`tasks/history_r4_closeout_20261010.md` 的 ds 部分）：
  C1 `_features_worker_r4`  —— 特征时钟对齐决策小时 + 真实时间窗 + 缺口声明 NaN
  C2 `summarize_forward_r4` / 补充时间切分 —— 按真实结果窗口分组，跨界项**真正排除**
  C3 `_outcome_worker_r4` / `cmd_outcome_r4` —— 直接后果标签（从当时参考价起的真实 30 天）
  C4 `_workload`            —— 小时底账 + 固定 2/3/6h 节奏派生（可算日内重入）
  C5 `cmd_reverse_r4`       —— 之后观察的首次发现时刻 + 完整剩余机会证据
  C6 `detect_events_r4`     —— 合并前真实连续段数 `n_segments`
  C7 `_baseline_worker_r4`  —— 复用前核对评分核指纹（`scoring_core_fingerprint`）
  C8 报告订正见 `closeout/report_corrections.md`
本批新计算一律写 `reports/history_research_ds/r4/closeout/`，**不覆盖** r4 原产物。

运行（PowerShell，项目根目录）：
  py -3.10 -u tools/history_research_ds_r4.py all
  py -3.10 -u tools/history_research_ds_r4.py universe
  py -3.10 -u tools/history_research_ds_r4.py events
  py -3.10 -u tools/history_research_ds_r4.py baseline --lag-hours 0
  py -3.10 -u tools/history_research_ds_r4.py forward  --lag-hours 0
  py -3.10 -u tools/history_research_ds_r4.py reverse  --lag-hours 0
  py -3.10 -u tools/history_research_ds_r4.py attention
  py -3.10 -u tools/history_research_ds_r4.py retention
  py -3.10 -u tools/history_research_ds_r4.py features
  py -3.10 -u tools/history_research_ds_r4.py supplementary
  py -3.10 -u tools/history_research_ds_r4.py outcome
  py -3.10 -u tools/history_research_ds_r4.py diff
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
from collections import Counter, deque
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import b4_replay_ds as R            # noqa: E402  已验收接入：取点/缺失/延迟语义唯一真源
import binance_box_strategy as B    # noqa: E402  生产评分：只读引用
import history_research_ds as H     # noqa: E402  v3：**只复用未受本批修正影响的函数**

# ---------------------------------------------------------------------------
# 输出隔离
# ---------------------------------------------------------------------------
OUT4 = ROOT / "reports" / "history_research_ds" / "r4"
# 本批（closeout）新计算一律写 OUT5，**不覆盖** r4 原产物
OUT5 = OUT4 / "closeout"
SHARDS4 = OUT4 / "shards"          # 复用：baseline 评分未受 C1–C6 影响
SIG4 = OUT4 / "signals"            # 复用：片段定义未变
EV4 = OUT4 / "events"              # r4 原事件（保留只读，不再写）
FWD4 = OUT4 / "forward"            # 复用：逐片段结果未受影响，只重算汇总
EV5 = OUT5 / "events"              # C6：新事件（真实段数）
FWD5 = OUT5 / "forward"            # C2：新 forward 汇总
OUTCOME5 = OUT5 / "outcome"        # C3：新直接后果标签（从当时参考价起的真实 30 天）
V3_OUT = ROOT / "reports" / "history_research_ds"

REUSE_V3_SHA256 = "95e9056bba795673846958b80fbd21cd669cf3a2067b9523f653925177e25199"
REUSE_V3_BYTES = 74037

# ---------------------------------------------------------------------------
# 常量（全部来自 v3 冻结版 / 生产，r4 不另立一套）
# ---------------------------------------------------------------------------
CFG = H.CFG
INTERVAL_MS = H.INTERVAL_MS
HR = INTERVAL_MS
LOOKBACK = H.LOOKBACK
BOX = H.BOX
ATR_P = H.ATR_P
EMA_P = H.EMA_P
MIN_SCORE = H.MIN_SCORE
W = dict(H.W)
MAX_SCORE = H.MAX_SCORE
A_EMA = H.A_EMA
DERIV_LOOKBACK = H.DERIV_LOOKBACK
DERIV_MIN_OI_OBS = H.DERIV_MIN_OI_OBS
OI_1D_TH = H.OI_1D_TH
OI_3D_TH = H.OI_3D_TH
LS_TOP_TH = H.LS_TOP_TH
MAIN_START = H.MAIN_START
MAIN_END = H.MAIN_END
MAIN_START_MS = H.MAIN_START_MS
MAIN_END_MS = H.MAIN_END_MS
N_HOURS = H.N_HOURS
GRID_MS = H.GRID_MS
EXPLORE_END = H.EXPLORE_END
REVIEW_START = H.REVIEW_START
WINDOWS = dict(H.WINDOWS)
EVENT_MERGE_GAP_H = H.EVENT_MERGE_GAP_H
EVENT_TIERS = tuple(H.EVENT_TIERS)
STATUS_NAMES = dict(H.STATUS_NAMES)
ST_SELECTED = H.ST_SELECTED
ST_BELOW = H.ST_BELOW
ST_SKIP_SHORT = H.ST_SKIP_SHORT
ST_SKIP_GAP = H.ST_SKIP_GAP
ST_SKIP_NODATA = H.ST_SKIP_NODATA
ST_NOT_OBSERVABLE = H.ST_NOT_OBSERVABLE
DERIV_STATUS_NAMES = dict(H.DERIV_STATUS_NAMES)

FWD_WAIT_MULTS = (1.5, 2.0)
FWD_DD_THRESHOLD = -0.20
# 参考价迟到判据：跳过的整根数 > 0 即记为延迟（见 forward_from_series_r4）
REF_DELAY_TOLERANCE_BARS = 0
# 未到期/资料不全时的判定名
VERDICT_UNDETERMINED = "undetermined"
VERDICT_NO_REFERENCE = "no_reference"


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


def _guard() -> R.NetworkGuard:
    g = R.NetworkGuard()
    g.install()
    return g


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_reuse() -> dict:
    """被复用的 v3 模块必须是验收过的冻结版，否则拒绝运行（防止悄悄换口径）。"""
    p = ROOT / "tools" / "history_research_ds.py"
    got = sha256_file(p)
    ok = got == REUSE_V3_SHA256
    if not ok and os.environ.get("HR4_ALLOW_REUSE_MISMATCH") != "1":
        raise RuntimeError(
            "复用的 tools/history_research_ds.py 不是验收过的冻结版：\n"
            f"  期望 {REUSE_V3_SHA256}（{REUSE_V3_BYTES} 字节）\n"
            f"  实际 {got}（{p.stat().st_size} 字节）\n"
            "r4 的复用清单以该版本为前提，拒绝混用。确需继续请设 HR4_ALLOW_REUSE_MISMATCH=1。")
    return {"reused_module": "tools/history_research_ds.py",
            "expected_sha256": REUSE_V3_SHA256, "expected_bytes": REUSE_V3_BYTES,
            "actual_sha256": got, "actual_bytes": int(p.stat().st_size), "match": ok}


def ensure_dirs() -> None:
    for d in (OUT4, OUT5, SHARDS4, SIG4, EV4, EV5, FWD4, FWD5, OUTCOME5):
        d.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# C7：冻结与复用契约
# ---------------------------------------------------------------------------
# 只守住 v3 哈希不够：baseline 缓存必须能证明「由同一份 r4 评分核 + 同一配置」产生。
# 做法：把评分核（受影响的那些函数源码 + 常量）算成指纹，写进分片 meta 与 manifest；
# 复用前比对指纹，不符即拒绝复用并重算。

SCORING_CORE_FUNCS = ("deriv_scores_r4", "_rank_rows_r4", "evaluate_r4",
                      "rank_matrix_r4", "random_rank_matrix_r4")


_SCORING_FP_CACHE: str | None = None


def scoring_core_fingerprint() -> str:
    """评分核指纹：只覆盖影响「评分/入选」的代码与常量，不含事件/特征/汇总。

    C7：分片复用前必须与本指纹一致；不符即视为「评分核已变」，强制重算而不是读旧分片。
    """
    global _SCORING_FP_CACHE
    if _SCORING_FP_CACHE is not None:
        return _SCORING_FP_CACHE
    import inspect
    parts = []
    for name in SCORING_CORE_FUNCS:
        fn = globals().get(name)
        parts.append(f"# {name}\n{inspect.getsource(fn)}")
    consts = {
        "MIN_SCORE": MIN_SCORE, "W": dict(W), "MAX_SCORE": MAX_SCORE,
        "LOOKBACK": LOOKBACK, "BOX": BOX, "ATR_P": ATR_P, "EMA_P": EMA_P, "A_EMA": A_EMA,
        "DERIV_LOOKBACK": DERIV_LOOKBACK, "DERIV_MIN_OI_OBS": DERIV_MIN_OI_OBS,
        "OI_1D_TH": OI_1D_TH, "OI_3D_TH": OI_3D_TH, "LS_TOP_TH": LS_TOP_TH,
        "MAIN_START": MAIN_START, "MAIN_END": MAIN_END, "N_HOURS": int(N_HOURS),
        "V3_SHA256": REUSE_V3_SHA256,
    }
    parts.append("# consts\n" + json.dumps(consts, ensure_ascii=False, sort_keys=True))
    _SCORING_FP_CACHE = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
    return _SCORING_FP_CACHE


def config_fingerprint() -> dict:
    """本批实际配置（写进 manifest，供后续复用核对）。"""
    return {
        "main_start": MAIN_START, "main_end": MAIN_END, "grid_hours": int(N_HOURS),
        "interval_ms": INTERVAL_MS, "min_score": MIN_SCORE, "weights": dict(W),
        "windows_hours": dict(WINDOWS), "merge_gap_hours": EVENT_MERGE_GAP_H,
        "event_tiers": list(EVENT_TIERS), "deriv_min_oi_obs": DERIV_MIN_OI_OBS,
        "review_start": REVIEW_START, "split_hour": int((H.R.parse_utc(REVIEW_START)
                                                         - MAIN_START_MS) // HR),
        "scoring_core_fingerprint": scoring_core_fingerprint(),
        "reused_v3_sha256": REUSE_V3_SHA256,
    }


def _legacy_reuse_fingerprint() -> str | None:
    """C7：读 closeout 复用决定文件（scoring_core 组）。

    仅当该文件存在、且其 `scoring_core_fingerprint` 与当前一致时，才允许复用
    **缺少逐分片指纹字段**的 r4 原分片（这些分片由同一评分核产生，源码哈希已比对）。
    指纹不符 → 返回 None → 旧分片一律重算。绝不修改 r4 原产物。
    """
    return scoring_core_fingerprint() if _legacy_reuse_ok("scoring_core") else None


# 可复用产物 → 其正确性所依赖的源码函数组（C7）
REUSE_GROUPS = {
    # baseline 分片 / signals / 逐时点重放
    "scoring_core": SCORING_CORE_FUNCS,
    # 逐片段 forward 详情：只依赖 signals（未变）+ 原始价格 + 本函数
    "forward_detail": ("forward_from_series_r4",),
}


def reuse_group_fingerprint(group: str) -> str:
    """某一复用组的源码指纹（只覆盖该组实际依赖的函数）。"""
    import inspect
    names = REUSE_GROUPS[group]
    parts = [f"# {n}\n{inspect.getsource(globals()[n])}" for n in names]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _legacy_reuse_ok(group: str) -> bool:
    """该复用组是否被 closeout/reuse_decision.json 证明「与当前源码一致」。"""
    p = OUT5 / "reuse_decision.json"
    if not p.exists():
        return False
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return False
    rec = (d.get("groups") or {}).get(group)
    if not rec:
        return False
    cur = (scoring_core_fingerprint() if group == "scoring_core"
           else reuse_group_fingerprint(group))
    return rec.get("fingerprint") == cur


def shards_dir(lag_hours: int) -> Path:
    return SHARDS4 / f"lag{int(lag_hours)}"


def signals_dir(lag_hours: int) -> Path:
    return SIG4 / f"lag{int(lag_hours)}"


def forward_dir(lag_hours: int) -> Path:
    return FWD4 / f"lag{int(lag_hours)}"


# ===========================================================================
# R1 / F1：合约侧评分必须严格复现 R.deriv_as_of 的逐字段契约
# ===========================================================================
#
# 主代理反例（evidence_20261010.json → synthetic.F1_sparse_oi）：
#   只有 3 个有效持仓观测时，v3 仍算出 oi_chg_1d/3d 并给 4+3 分（合计 10 分）；
#   已验收的 `R.deriv_as_of` + 生产 `B.deriv_score` 只给 3 分（仅大户持仓比那一项）。
#
# 修正：**有效 OI 观测 < DERIV_MIN_OI_OBS(25) 时，oi_chg_1d / oi_chg_3d 记为不可算（NaN）**，
# 与 `deriv_as_of` 的 `if len(oi_valid) >= DERIV_MIN_OI_OBS:` 完全一致。
#
# 注意（主代理明确要求）：**不是**「把所有 partial 的持仓分一律清零」。
#   · 观测数 ≥ 25 但 24h 或 72h 基准缺失 → 只算得出的那一项计分（生产 deriv_score 语义）。
#   · 大户持仓比 LS **独立**按其自己的可用条件计分（只要求 finite，合法零值保留）。
#   · 状态仍分 ok / partial / failed，缺哪个字段与该字段能否评分是两件事。

def deriv_scores_r4(dt: np.ndarray, oi: np.ndarray, ls: np.ndarray,
                    decision_ms: np.ndarray, lag_hours: int = 0) -> dict:
    """合约侧批量评分（F1 修正版）。窗口/过滤/≥25/可得截止/合法零 全部对齐 R.deriv_as_of。"""
    d = decision_ms.astype(np.int64)
    N = len(d)
    cutoff = d - lag_hours * INTERVAL_MS
    oi_pos = np.flatnonzero(np.isfinite(oi) & (oi > 0)) if len(oi) else np.empty(0, dtype=np.int64)
    ls_pos = np.flatnonzero(np.isfinite(ls)) if len(ls) else np.empty(0, dtype=np.int64)
    oi_dt = dt[oi_pos] if len(oi_pos) else np.empty(0, dtype=np.int64)
    oi_vals = oi[oi_pos] if len(oi_pos) else np.empty(0)
    ls_vals = ls[ls_pos] if len(ls_pos) else np.empty(0)

    has_obs = np.zeros(N, dtype=bool)
    oi_cnt = np.zeros(N, dtype=np.int64)
    ls_cnt = np.zeros(N, dtype=np.int64)
    oi_chg_1d = np.full(N, np.nan)
    oi_chg_3d = np.full(N, np.nan)
    ls_top = np.full(N, np.nan)
    ok_oi = np.zeros(N, dtype=bool)
    ok_ls = np.zeros(N, dtype=bool)
    oi_as_of = np.full(N, -1, dtype=np.int64)
    ls_as_of = np.full(N, -1, dtype=np.int64)

    if len(dt):
        i = np.searchsorted(dt, cutoff, side="right") - 1
        has_obs = i >= 0
        w0 = np.maximum(0, i - (DERIV_LOOKBACK - 1))
        w1 = i
        if len(oi_pos):
            oi_cnt = np.where(has_obs, np.searchsorted(oi_pos, w1, side="right")
                              - np.searchsorted(oi_pos, w0, side="left"), 0)
            cpos = np.searchsorted(oi_pos, w1, side="right") - 1
            cps = np.clip(cpos, 0, len(oi_pos) - 1)
            cur = np.where(cpos >= 0, oi_pos[cps], -1)
            cur = np.where((cur >= w0) & (cur <= w1) & has_obs, cur, -1)
            cur_ts = np.where(cur >= 0, dt[np.where(cur >= 0, cur, 0)], -1)
            cur_oi = np.where(cur >= 0, oi_vals[np.where(cpos >= 0, cps, 0)], np.nan)
            for back_h, out in ((24, oi_chg_1d), (72, oi_chg_3d)):
                base_ts = cur_ts - back_h * INTERVAL_MS
                p = np.searchsorted(oi_dt, base_ts, side="right") - 1
                ps = np.clip(p, 0, len(oi_pos) - 1)
                pidx = np.where(p >= 0, oi_pos[ps], -1)
                got = (pidx >= w0) & (pidx >= 0)
                base_val = np.where(got, oi_vals[np.where(p >= 0, ps, 0)], np.nan)
                out[:] = np.where(got, cur_oi / base_val - 1.0, np.nan)
            sel = (oi_cnt >= DERIV_MIN_OI_OBS) & (cur >= 0)
            ok_oi = sel & np.isfinite(oi_chg_1d) & np.isfinite(oi_chg_3d)
            oi_as_of = np.where(sel, cur_ts, -1)
        if len(ls_pos):
            ls_cnt = np.where(has_obs, np.searchsorted(ls_pos, w1, side="right")
                              - np.searchsorted(ls_pos, w0, side="left"), 0)
            cl = np.searchsorted(ls_pos, w1, side="right") - 1
            cls = np.clip(cl, 0, len(ls_pos) - 1)
            curl = np.where(cl >= 0, ls_pos[cls], -1)
            curl = np.where((curl >= w0) & (curl <= w1) & has_obs, curl, -1)
            got_ls = curl >= 0
            ls_top = np.where(got_ls, ls_vals[np.where(cl >= 0, cls, 0)], np.nan)
            ls_as_of = np.where(got_ls, dt[np.where(got_ls, curl, 0)], -1)
            ok_ls = got_ls

    # —— F1 修正点：观测不足 25 时，涨幅必须不可算（与 deriv_as_of 同）——
    below_min = oi_cnt < DERIV_MIN_OI_OBS
    oi_chg_1d_raw = oi_chg_1d.copy()
    oi_chg_3d_raw = oi_chg_3d.copy()
    oi_chg_1d = np.where(below_min, np.nan, oi_chg_1d)
    oi_chg_3d = np.where(below_min, np.nan, oi_chg_3d)
    oi_as_of = np.where(below_min, -1, oi_as_of)

    pts = ((np.isfinite(oi_chg_1d) & (oi_chg_1d >= OI_1D_TH)) * W["oi_chg_1d"]
           + (np.isfinite(oi_chg_3d) & (oi_chg_3d >= OI_3D_TH)) * W["oi_chg_3d"]
           + (np.isfinite(ls_top) & (ls_top < LS_TOP_TH)) * W["ls_top"]).astype(np.int64)
    status = np.where(ok_oi & ok_ls, 0, np.where(ok_oi | ok_ls, 1, 2))   # 0=ok 1=partial 2=failed
    status = np.where(has_obs, status, 2)
    return dict(points=pts, status=status, oi_chg_1d=oi_chg_1d, oi_chg_3d=oi_chg_3d,
                ls_top=ls_top, oi_cnt=oi_cnt, ls_cnt=ls_cnt, oi_as_of=oi_as_of,
                ls_as_of=ls_as_of, has_obs=has_obs,
                oi_chg_1d_raw=oi_chg_1d_raw, oi_chg_3d_raw=oi_chg_3d_raw,
                oi_gated=below_min & has_obs)


# ===========================================================================
# R1 / F2：排名必须先排除不可评分列
# ===========================================================================
#
# 主代理反例（evidence → synthetic.F2_rank_masks）：
#   v3 先排序、后把不可评分列改成 32767，导致有效列的名次被不可评分列挤占，
#   例如 [2, 32767, 3] 得到有效名次 {3, 2} 而不是连续的 {1, 2}。
#   随机对照与趋势对照都受影响（总分排序因为 -1 天然排最后而侥幸不受影响）。
#
# 修正：把不可评分列在**排序前**置为 -inf，使其排在最后并被覆盖为 32767；
#   可评分列名次连续 1..k，并列仍按列序（symbol 字典序升序）。

def _rank_rows_r4(vals: np.ndarray, scoreable: np.ndarray) -> np.ndarray:
    """按行算名次（1=最好）。不可评分列不参与排序、名次记 32767。"""
    n_rows = vals.shape[0]
    rank = np.empty(vals.shape, dtype=np.int16)
    step = 1024
    for a in range(0, n_rows, step):
        b = min(n_rows, a + step)
        blk = vals[a:b].astype(np.float64)
        ok = scoreable[a:b]
        masked = np.where(ok, blk, -np.inf)
        order = np.argsort(-masked, axis=1, kind="stable")
        rk = np.empty(order.shape, dtype=np.int16)
        rows = np.arange(order.shape[0])[:, None]
        rk[rows, order] = np.arange(1, order.shape[1] + 1, dtype=np.int16)[None, :]
        rk[~ok] = 32767
        rank[a:b] = rk
    return rank


def rank_matrix_r4(total: np.ndarray) -> np.ndarray:
    """每小时横截面名次。分母 = 该小时全部可评分对象。"""
    return _rank_rows_r4(total.astype(np.int16), total >= 0)


def random_rank_matrix_r4(seed: int, total: np.ndarray) -> np.ndarray:
    """固定随机种子的对照排序（每行一个随机排列，只在可评分列之间排）。"""
    rng = np.random.default_rng(seed)
    rnd = rng.random(total.shape, dtype=np.float32)
    return _rank_rows_r4(rnd, total >= 0)


# ===========================================================================
# 评估器（K 线侧复用 v3，合约侧换 r4）
# ===========================================================================

def evaluate_r4(symbol: str, candles: np.ndarray, mdt: np.ndarray, oi: np.ndarray,
                ls: np.ndarray, has_futures: bool, decision_ms: np.ndarray,
                lag_hours: int = 0) -> dict:
    """与 v3 `evaluate` 同结构，只有合约侧换成 `deriv_scores_r4`（F1）。"""
    d = decision_ms.astype(np.int64)
    n = len(d)
    fr = H.kline_frame(candles) if len(candles) else None
    ts = fr["ts"] if fr else np.empty(0, dtype=np.int64)

    adopted = np.full(n, -1, dtype=np.int64)
    has_candle = np.zeros(n, dtype=bool)
    any_earlier = np.zeros(n, dtype=bool)
    n_closed = np.zeros(n, dtype=np.int64)
    if len(ts):
        target = d - INTERVAL_MS
        pos = np.searchsorted(ts, target)
        pos_safe = np.clip(pos, 0, max(len(ts) - 1, 0))
        has_candle = (pos < len(ts)) & (ts[pos_safe] == target)
        adopted = np.where(has_candle, pos_safe, -1)
        earlier = np.searchsorted(ts, target, side="right") - 1
        any_earlier = earlier >= 0
        n_closed = np.where(any_earlier, earlier + 1, 0)

    score = np.full(n, -1, dtype=np.int64)
    trend = np.zeros(n, dtype=bool)
    breakout = np.zeros(n, dtype=bool)
    if has_candle.any():
        sc, tr_, br_ = H.kline_scores(fr, np.clip(adopted, 0, max(len(ts) - 1, 0)))
        score = np.where(has_candle, sc, -1)
        trend = np.where(has_candle, tr_, False)
        breakout = np.where(has_candle, br_, False)

    short = has_candle & (n_closed < (BOX + 3 - 1))
    scored = has_candle & ~short & (score >= 0)
    score = np.where(scored, score, -1)
    trend = np.where(scored, trend, False)
    breakout = np.where(scored, breakout, False)

    if has_futures:
        if len(mdt):
            dv = deriv_scores_r4(mdt, oi, ls, d, lag_hours)
            dpts = dv["points"]; dstat = dv["status"]
            dchg1 = dv["oi_chg_1d"]; dchg3 = dv["oi_chg_3d"]; dlst = dv["ls_top"]
            d_oi_cnt = dv["oi_cnt"]; d_gated = dv["oi_gated"]
            d_raw1 = dv["oi_chg_1d_raw"]; d_raw3 = dv["oi_chg_3d_raw"]
        else:
            dpts = np.zeros(n, dtype=np.int64); dstat = np.full(n, 2, dtype=np.int8)
            dchg1 = np.full(n, np.nan); dchg3 = np.full(n, np.nan); dlst = np.full(n, np.nan)
            d_oi_cnt = np.zeros(n, dtype=np.int64); d_gated = np.zeros(n, dtype=bool)
            d_raw1 = np.full(n, np.nan); d_raw3 = np.full(n, np.nan)
    else:
        dpts = np.zeros(n, dtype=np.int64); dstat = np.full(n, -1, dtype=np.int8)
        dchg1 = np.full(n, np.nan); dchg3 = np.full(n, np.nan); dlst = np.full(n, np.nan)
        d_oi_cnt = np.zeros(n, dtype=np.int64); d_gated = np.zeros(n, dtype=bool)
        d_raw1 = np.full(n, np.nan); d_raw3 = np.full(n, np.nan)

    total = np.where(scored, score + dpts, -1)
    selected = scored & (total >= MIN_SCORE)
    status = np.where(selected, ST_SELECTED,
                      np.where(scored, ST_BELOW,
                               np.where(~has_candle,
                                        np.where(any_earlier, ST_SKIP_GAP, ST_SKIP_NODATA),
                                        ST_SKIP_SHORT))).astype(np.int8)
    return dict(status=status, score_kline=score.astype(np.int8),
                score_deriv=dpts.astype(np.int8), score_total=total.astype(np.int8),
                selected=selected, trend=trend, breakout=breakout,
                deriv_status=dstat.astype(np.int8), oi_chg_1d=dchg1, oi_chg_3d=dchg3,
                ls_top=dlst, oi_valid_obs=d_oi_cnt, n_closed=n_closed,
                oi_gated=d_gated, oi_chg_1d_raw=d_raw1, oi_chg_3d_raw=d_raw3)


# ===========================================================================
# R3：事件库按**真实时间**建窗与合波，峰值指向真实未来高点
# ===========================================================================
#
# 主代理反例：
#   · P8  `detect_events` 把 720 根当 30 天、段间下标差当 48 小时。
#         插入 60 天缺口后，距起点 1,540 小时的高点仍被算进起点的 7/30 天涨幅。
#   · P9  `peak_ms` 实际取「未来倍数最大所对应的参考根」，不是未来价格峰值；
#         手算反例中它与起点相同，真正高点在后面。
#
# 修正：
#   · 未来窗口 = open_time ∈ [close_t, close_t + (W-1)*1h]，**按真实时刻**。
#     无缺口时恰等于旧口径 [t+1, t+W]；有缺口时不会把远点挤进短窗口。
#   · 48 小时合波按**时间差**判定（旧口径用下标差）。
#   · `peak_ms` = 该窗口峰值所在的**真实 K 线开盘时刻**；7/30/90 天各自存峰值与时间。
#   · 参考根、参考收盘可用时刻、峰值时刻**分开存**。
#   · 成熟（时间已过去）与资料完整（窗口内不缺线）**分开记录**，按事件实际参与窗口判定。

def _window_bounds(ts: np.ndarray, bar_ms: int, window_hours: int) -> tuple[np.ndarray, np.ndarray]:
    """每个 t 的未来窗口（索引区间 [lo[t], hi[t])）。

    未来 = `open_time ∈ [ts[t]+bar_ms, ts[t]+bar_ms+(W-1)*1h]`。
    即参考收盘可用时刻起、到窗口截止时刻（含）为止开盘的 K 线。
    """
    close_t = ts + bar_ms
    lo = np.searchsorted(ts, close_t, side="left")
    hi = np.searchsorted(ts, close_t + (window_hours - 1) * HR, side="right")
    return lo, hi


def _sliding_max_argmax(values: np.ndarray, lo: np.ndarray,
                        hi: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """对每个 t 求 values[lo[t]:hi[t]] 的最大值及其下标；lo/hi 单调不减 → O(n)。

    空窗返回 (nan, -1)。单调双端队列，队首即当前窗口最大值所在下标。
    """
    n = len(values)
    safe = np.where(np.isfinite(values), values, -np.inf)
    out = np.full(n, np.nan)
    arg = np.full(n, -1, dtype=np.int64)
    dq: deque[int] = deque()
    nxt = 0
    for t in range(n):
        h = int(hi[t]); l = int(lo[t])
        while nxt < h:
            v = safe[nxt]
            while dq and safe[dq[-1]] <= v:
                dq.pop()
            dq.append(nxt)
            nxt += 1
        while dq and dq[0] < l:
            dq.popleft()
        if dq and np.isfinite(safe[dq[0]]):
            out[t] = values[dq[0]]
            arg[t] = dq[0]
    return out, arg


def detect_events_r4(candles: np.ndarray) -> list[dict]:
    """独立事件库（R3 修正版）。不依赖评分、不接收任何未来标签。"""
    n = len(candles)
    if n < 60:
        return []
    ts = candles[:, 0].astype(np.int64)
    high = candles[:, 2]
    low = candles[:, 3]
    close = candles[:, 4]
    bar_ms = INTERVAL_MS

    m: dict[str, np.ndarray] = {}
    mature: dict[str, np.ndarray] = {}
    missing: dict[str, np.ndarray] = {}
    peak_idx: dict[str, np.ndarray] = {}
    for key, win in WINDOWS.items():
        lo, hi = _window_bounds(ts, bar_ms, win)
        mx, arg = _sliding_max_argmax(high, lo, hi)
        with np.errstate(invalid="ignore", divide="ignore"):
            m[key] = mx / close
        close_t = ts + bar_ms
        mature[key] = ts[-1] >= (close_t + (win - 1) * HR)
        missing[key] = np.maximum(0, win - (hi - lo)).astype(np.int64)
        peak_idx[key] = arg

    inwave = np.isfinite(m["30d"]) & (m["30d"] >= 2.0)
    idx = np.flatnonzero(inwave)
    if len(idx) == 0:
        return []

    # 连续游程 + 时间差 <= 48h 合并（**按真实时间**，不是下标差）
    segs: list[tuple[int, int]] = []
    s = int(idx[0]); prev = int(idx[0])
    for k in idx[1:]:
        k = int(k)
        if ts[k] - ts[prev] > EVENT_MERGE_GAP_H * HR:
            segs.append((s, prev)); s = k
        prev = k
    segs.append((s, prev))

    out: list[dict] = []
    for a, b in segs:
        # —— C6：合并前的**真实连续段数**与连接证据（不再固定 1）——
        sub = idx[(idx >= a) & (idx <= b)]
        if len(sub) == 0:
            n_seg, seg_spans, merge_gaps = 0, [], []
        else:
            brk = (np.diff(sub) != 1) | (np.diff(ts[sub]) != bar_ms)
            bounds = np.concatenate([[0], np.flatnonzero(brk) + 1, [len(sub)]])
            n_seg = int(len(bounds) - 1)
            seg_spans = [{"start_ms": int(ts[sub[bounds[k]]]),
                          "end_ms": int(ts[sub[bounds[k + 1] - 1]]),
                          "n_hours": int(bounds[k + 1] - bounds[k])}
                         for k in range(n_seg)]
            merge_gaps = [round(float(ts[sub[bounds[k]]] - ts[sub[bounds[k] - 1]]) / HR, 3)
                          for k in range(1, n_seg)]
        peaks: dict[str, dict] = {}
        for key in ("7d", "30d", "90d"):
            seg_m = m[key][a:b + 1]
            if not np.any(np.isfinite(seg_m)):
                peaks[key] = {"ref_bar_ms": None, "ms": None, "mult": None}
                continue
            k = a + int(np.nanargmax(seg_m))
            pi = int(peak_idx[key][k])
            peaks[key] = {
                "ref_bar_ms": int(ts[k]),
                "ms": int(ts[pi]) if pi >= 0 else None,
                "mult": round(float(m[key][k]), 4),
            }
        mx30 = peaks["30d"]["mult"]
        tier = 2.0
        if mx30 is not None:
            for t in EVENT_TIERS:
                if mx30 >= t:
                    tier = t
                    break
        # 峰值（30 天窗）所在真实 K 线；旧口径错误地指向参考根
        pi30 = -1
        if peaks["30d"]["ms"] is not None:
            pi30 = int(np.searchsorted(ts, peaks["30d"]["ms"], side="left"))
        # 从参考收盘到峰值的收盘回撤（含参考根，恒 <= 0）
        if pi30 >= a:
            path = close[a:pi30 + 1]
            run = np.maximum.accumulate(path)
            ddser = path / run - 1.0
            max_dd = float(ddser.min())
            idx20 = np.flatnonzero(ddser <= -0.20)
            new_high_after = bool(len(idx20)
                                  and np.any(high[a + int(idx20[0]) + 1:pi30 + 1] >= run[int(idx20[0])]))
        else:
            max_dd = 0.0
            new_high_after = False

        span = int((ts[b] - ts[a]) // HR) + 1
        internal_gap = bool(np.any(np.diff(ts[a:b + 1]) > bar_ms)) if b > a else False
        mat: dict[str, bool] = {}
        comp: dict[str, bool] = {}
        miss_max: dict[str, int] = {}
        for key, win in WINDOWS.items():
            mat[key] = bool(ts[-1] >= (ts[b] + bar_ms + (win - 1) * HR))
            miss_max[key] = int(missing[key][a:b + 1].max())
            comp[key] = bool(miss_max[key] == 0 and not internal_gap)

        out.append({
            "start_ms": int(ts[a]), "end_ms": int(ts[b]),
            "ref_bar_ms": int(ts[a]),
            "ref_close_available_ms": int(ts[a] + bar_ms),
            "ref_close": float(close[a]),
            "peak_ms": peaks["30d"]["ms"],                 # ← 真实未来高点（P9 修正）
            "peaks": peaks,
            "max_m30": mx30,
            "max_m7": peaks["7d"]["mult"],
            "max_m90": peaks["90d"]["mult"],
            "tier": tier,
            "n_segments": n_seg,                     # ← C6：合并前真实连续段数
            "segments": seg_spans,                   # ← 连接证据（每段的起止与小时数）
            "merge_gaps_hours": merge_gaps,          # ← 相邻段之间的真实间隔
            "span_hours": span,
            "mature_7d": mat["7d"], "mature_30d": mat["30d"], "mature_90d": mat["90d"],
            "complete_7d": comp["7d"], "complete_30d": comp["30d"], "complete_90d": comp["90d"],
            "missing_bars_30d": miss_max["30d"], "missing_bars_90d": miss_max["90d"],
            "has_internal_gap": internal_gap,
            "max_drawdown_to_peak": round(max_dd, 4),
            "new_high_after_20pct_dd": new_high_after,
            "bar_ms": bar_ms,
        })
    return out


def _events_worker_r4(sym: str) -> dict:
    g = _guard()
    months = H.spot_months(sym)
    if not months:
        g.uninstall()
        return {"symbol": sym, "n_events": 0, "rows": 0, "network_blocked": g.blocked}
    candles = H.read_spot(sym, months)
    evs = detect_events_r4(candles)
    for e in evs:
        e["symbol"] = sym
    EV5.mkdir(parents=True, exist_ok=True)
    with (EV5 / f"{sym}.jsonl").open("w", encoding="utf-8") as fh:
        for e in evs:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")
    g.uninstall()
    return {"symbol": sym, "n_events": len(evs), "rows": int(len(candles)),
            "network_blocked": g.blocked,
            "first": R.ms_to_iso(int(candles[0, 0])) if len(candles) else None,
            "last": R.ms_to_iso(int(candles[-1, 0])) if len(candles) else None}


def cmd_events_r4(workers: int = 12) -> dict:
    ensure_dirs()
    syms = H.usdt_spot_symbols()
    log(f"events: 开始，{len(syms)} 个对象，workers={workers}（按真实时间建窗）")
    t0 = time.perf_counter()
    res = []
    done = 0
    with ProcessPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_events_worker_r4, s): s for s in syms}
        for f in as_completed(futs):
            res.append(f.result())
            done += 1
            if done % 100 == 0:
                log(f"events: {done}/{len(syms)}")
    total = sum(r["n_events"] for r in res)
    ev = load_events_r4()
    tiers = Counter()
    mat30 = Counter()
    comp30 = Counter()
    seg_hist = Counter()
    seg_gaps: list[float] = []
    for e in ev:
        tiers[str(e["tier"])] += 1
        mat30["mature" if e["mature_30d"] else "immature"] += 1
        comp30["complete" if e["complete_30d"] else "partial"] += 1
        # C6：合并前真实连续段数（旧版恒为 1，看不出「一波被拆成几段」）
        seg_hist[str(int(e.get("n_segments", 1)))] += 1
        seg_gaps.extend(float(g) for g in (e.get("merge_gaps_hours") or []))
    seg_ints = sorted(int(k) for k in seg_hist)
    summary = {
        "generated_at_utc": R.ms_to_iso(int(time.time() * 1000)),
        "symbols": len(syms), "total_events": total,
        "symbols_with_events": sum(1 for r in res if r["n_events"]),
        "elapsed_sec": round(time.perf_counter() - t0, 2),
        "merge_gap_hours": EVENT_MERGE_GAP_H,
        "windows_hours": WINDOWS,
        "window_rule": "未来 = open_time ∈ [close_t, close_t+(W-1)*1h]，按真实时刻",
        "by_tier": dict(tiers),
        "mature_30d": dict(mat30), "complete_30d": dict(comp30),
        "n_segments": {
            "histogram": {str(k): int(seg_hist[str(k)]) for k in seg_ints},
            "min": (seg_ints[0] if seg_ints else None),
            "max": (seg_ints[-1] if seg_ints else None),
            "events_with_multiple_segments": int(sum(v for k, v in seg_hist.items() if int(k) > 1)),
            "merge_gaps_hours_stats": _describe(seg_gaps),
            "note": ("合并前真实连续段数：同一事件内相邻段的时间间隔 < merge_gap_hours 才合并。"
                     "旧版恒输出 1，无法反映「一波被拆成几段」。"),
        },
        "per_symbol": sorted(res, key=lambda r: -r["n_events"]),
    }
    (OUT5 / "events_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1),
                                              encoding="utf-8")
    log(f"events: 完成，事件总数 {total}，耗时 {summary['elapsed_sec']}s；"
        f"档位 {dict(tiers)}；段数 {summary['n_segments']['histogram']}"
        f"（多段事件 {summary['n_segments']['events_with_multiple_segments']}）")
    return summary


def load_events_r4() -> list[dict]:
    out = []
    for p in sorted(EV5.glob("*.jsonl")):
        for ln in p.read_text(encoding="utf-8").splitlines():
            if ln.strip():
                out.append(json.loads(ln))
    return out


# ===========================================================================
# R2：参考价、结果窗口与路径
# ===========================================================================
#
# 主代理反例：
#   · P1 参考价是**开盘**时，参考根自己的盘中高点属于可观察的后续；v3 整根排除，漏报。
#   · P2 收盘路径 100→200→150，「最大回撤」报 +50%（实际是最低价相对入场，且漏掉入场点）。
#   · P3 100→80→200，达标前回撤报 0（起始点从累计高点里消失了）。
#   · P4 首次达标根内部先后不清，v3 把达标后的收盘挪到达标前，伪造 -44% 回撤。
#   · P5 只剩 0/1/719/720 四根、期间大量缺线，仍报 complete + mature。
#   · P6 KEYUSDT 参考价迟到 40,620 分钟仍报 complete。
#   · P7 未到期未达标的片段被塞进最终失败比例。
#   · P10 窗口截止时刻才开盘的一根，其后续高点被算进已截止窗口。
#
# 修正：
#   · 参考价 = 信号可用后**下一可用开盘**；参考根之后的同根高低价计入未来。
#   · 窗口按**真实时刻**截止：`open_time < ref_open + W`（截止时刻才开盘的一根不算）。
#   · 「时间已过去 / 资料连续完整 / 是否达到幅度」三件事**分开**记录。
#   · 参考价必须落在所声明的下一根时间上；跳过整根即记 `ref_delayed`，不得当正常参考。
#   · 未到期（或资料不全）且未达标 → 判 `undetermined`，**不进最终失败比例**；
#     未到期但已触及 → 记 `reach_2x`（已发生的事实）。
#   · 「相对入场的最大下跌」与「收盘路径从已有高点的最大回落」**分列**，都不给正值。
#   · 达标前下跌**含入场点**；首次达标根内部先后不明 → 给 `strict / worst` 上下界。
#   · 等待时间给到所用 K 线精度（小时），不伪装精确成交时刻。

def forward_from_series_r4(c: np.ndarray, ref_ms: int, bar_ms: int,
                           windows: dict | None = None) -> dict | None:
    """信号后果（R2 修正版）。`c` 列 = [ts,o,h,l,c,v]，按 ts 升序。"""
    windows = windows or WINDOWS
    n = len(c)
    if n == 0:
        return None
    ts = c[:, 0]
    high = c[:, 2]
    low = c[:, 3]
    close = c[:, 4]
    i = int(np.searchsorted(ts, float(ref_ms), side="left"))
    if i >= n:
        return None
    ref_open_ms = float(ts[i])
    ref = float(c[i, 1])
    if not (ref > 0) or not np.isfinite(ref):
        return None
    last_ms = float(ts[-1])
    expected_ref_ms = math.ceil(float(ref_ms) / bar_ms) * bar_ms
    skipped = int(round((ref_open_ms - expected_ref_ms) / bar_ms))
    out: dict = {
        "ref_price": round(ref, 10),
        "ref_open_utc": R.ms_to_iso(int(ref_open_ms)),
        "ref_lag_min": int((ref_open_ms - float(ref_ms)) // 60_000),
        "ref_skipped_bars": skipped,
        "ref_delayed": bool(skipped > REF_DELAY_TOLERANCE_BARS),
        "last_bar_utc": R.ms_to_iso(int(last_ms)),
        "bar_ms": int(bar_ms),
    }
    for key, win in windows.items():
        t_end = ref_open_ms + win * HR
        hi = int(np.searchsorted(ts, t_end, side="left"))   # open >= 截止时刻 → 不算
        nb = hi - i
        expected_bars = int(round(win * HR / bar_ms))
        missing = max(0, expected_bars - nb)
        elapsed = bool(last_ms + bar_ms >= t_end)
        if nb <= 0:
            out[key] = {"max_touch": None, "max_close": None, "max_dd_from_entry": None,
                        "max_close_drawdown": None, "peak_ms": None, "elapsed": elapsed,
                        "data_state": "no_bars", "expected_bars": expected_bars,
                        "n_bars": 0, "missing_bars": expected_bars}
            continue
        hh = high[i:hi]; cc = close[i:hi]; ll = low[i:hi]
        run = np.maximum.accumulate(np.concatenate(([ref], cc)))
        ddser = np.concatenate(([ref], cc)) / run - 1.0
        state = "complete" if (missing == 0 and not out["ref_delayed"]) else "partial"
        out[key] = {
            "max_touch": round(float(hh.max()) / ref, 4),
            "max_close": round(float(cc.max()) / ref, 4),
            "max_dd_from_entry": round(min(0.0, float(ll.min()) / ref - 1.0), 4),
            "max_close_drawdown": round(float(ddser.min()), 4),
            "peak_ms": int(ts[i + int(np.argmax(hh))]),
            "elapsed": elapsed, "data_state": state,
            "expected_bars": expected_bars, "n_bars": int(nb), "missing_bars": int(missing),
        }
    # —— 等待时间（含参考根；只在 30 天主窗内判定）——
    w30 = WINDOWS["30d"]
    hi30 = int(np.searchsorted(ts, ref_open_ms + w30 * HR, side="left"))
    hh30 = high[i:hi30]
    waits: dict = {}
    for mult in FWD_WAIT_MULTS:
        hit = np.flatnonzero(hh30 >= ref * mult) if len(hh30) else np.empty(0, dtype=int)
        waits[f"wait_hours_to_{mult}x"] = (
            round(float(ts[i + int(hit[0])] - ref_open_ms) / HR, 3) if len(hit) else None)
    out["waits"] = waits
    out["first_2x_hours"] = waits["wait_hours_to_2.0x"]
    # —— 达标前下跌（含入场点；首次达标根内部先后不明 → 上下界）——
    hit2 = np.flatnonzero(hh30 >= ref * 2.0) if len(hh30) else np.empty(0, dtype=int)
    if len(hit2):
        k2 = i + int(hit2[0])
        pre = close[i:k2]
        if len(pre) == 0:
            strict = 0.0
            run_before = ref
        else:
            path = np.concatenate(([ref], pre))
            run = np.maximum.accumulate(path)
            strict = float((path / run - 1.0).min())
            run_before = float(run[-1])
        cand = float(low[k2]) / run_before - 1.0
        out["dd_before_2x"] = round(strict, 4)                 # 确定的部分
        out["dd_before_2x_worst"] = round(min(strict, cand), 4)  # 计入达标根最低价的保守下界
        out["dd_before_2x_uncertain"] = bool(cand < strict)
    else:
        out["dd_before_2x"] = None
        out["dd_before_2x_worst"] = None
        out["dd_before_2x_uncertain"] = False
    # —— 四维分开（数据状态 / 时间是否过去 / 涨幅事实 / 判定）——
    w = out.get("30d") or {}
    touch = w.get("max_touch")
    state = w.get("data_state", "no_bars")
    elapsed = bool(w.get("elapsed", False))
    final_eligible = bool(elapsed and state == "complete")
    if touch is None:
        gain = "unknown"
    elif touch >= 2.0:
        gain = "reach_2x"
    elif touch >= 1.5:
        gain = "reach_1_5x_only"
    else:
        gain = "below_1_5x"
    if touch is None:
        verdict = VERDICT_NO_REFERENCE
    elif gain == "reach_2x":
        verdict = "reach_2x"                    # 已发生的事实，与是否到期无关
    elif final_eligible:
        verdict = gain
    else:
        verdict = VERDICT_UNDETERMINED          # 未到期/资料不全且未达标 → 不是失败
    if gain == "reach_2x":
        if out["dd_before_2x_uncertain"]:
            path_fact = "reach_2x_dd_unknown"
        elif out["dd_before_2x_worst"] is not None and out["dd_before_2x_worst"] <= FWD_DD_THRESHOLD:
            path_fact = "reach_2x_after_dd"
        else:
            path_fact = "reach_2x_no_dd"
    elif touch is None:
        path_fact = "none"
    elif (w.get("max_close_drawdown") is not None
          and w["max_close_drawdown"] <= FWD_DD_THRESHOLD):
        path_fact = "no_2x_dd"
    else:
        path_fact = "no_2x_shallow"
    out["dim"] = {"data_state": state, "elapsed": elapsed,
                  "final_eligible": final_eligible, "gain_observed": gain,
                  "verdict": verdict, "path": path_fact}
    return out


def _forward_worker_r4(args: tuple) -> dict:
    sym, lag_hours, legacy_ok = args
    g = _guard()
    out = forward_dir(lag_hours) / f"{sym}.jsonl"
    if out.exists() and legacy_ok:
        # C7 复用：逐片段详情只依赖 signals（未变）+ 原始价格 + forward_from_series_r4（未变）。
        # 直接读回旧产物，**不覆盖** r4 原文件。
        n = sum(1 for ln in out.read_text(encoding="utf-8").splitlines() if ln.strip())
        g.uninstall()
        return {"symbol": sym, "skipped": True, "reused": True, "n_episodes": n,
                "network_blocked": g.blocked}
    sig = signals_dir(lag_hours) / f"{sym}.jsonl"
    if not sig.exists():
        g.uninstall()
        return {"symbol": sym, "skipped": True, "n_episodes": 0, "network_blocked": g.blocked}
    eps = [json.loads(ln) for ln in sig.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if not eps:
        g.uninstall()
        return {"symbol": sym, "skipped": False, "n_episodes": 0, "network_blocked": g.blocked}
    months5 = H.spot_months_interval(sym, "5m")
    c5 = H.read_spot_interval(sym, "5m", months5) if months5 else np.empty((0, 6), dtype=np.float64)
    c1 = None
    rows = []
    n_5m = n_1h = n_none = n_delayed = 0
    for ep in eps:
        ref_ms = int(ep["start_ms"])
        fp = forward_from_series_r4(c5, ref_ms, 300_000) if len(c5) else None
        gran = "5m"
        if fp is None:
            if c1 is None:
                c1 = H.read_spot(sym, H.spot_months(sym))
            fp = forward_from_series_r4(c1, ref_ms, INTERVAL_MS) if len(c1) else None
            gran = "1h"
        if fp is None:
            n_none += 1
            rows.append({
                "symbol": sym, "start_utc": ep["start_utc"], "start_ms": ref_ms,
                "granularity": "none", "n_hours": ep["n_hours"],
                "first_score": int(ep["score_kline"]) + int(ep["score_deriv"]),
                "score_total_max": ep["score_total_max"],
                "deriv_status": ep.get("deriv_status"),
                "has_futures": ep.get("deriv_status") not in (None, "no_futures"),
                "ref_price": None, "ref_open_utc": None, "ref_lag_min": None,
                "ref_skipped_bars": None, "ref_delayed": None, "last_bar_utc": None,
                "bar_ms": None,
                **{k: {"max_touch": None, "max_close": None, "max_dd_from_entry": None,
                       "max_close_drawdown": None, "peak_ms": None, "elapsed": False,
                       "data_state": "no_reference", "expected_bars": None,
                       "n_bars": 0, "missing_bars": None} for k in WINDOWS},
                "waits": {f"wait_hours_to_{m}x": None for m in FWD_WAIT_MULTS},
                "first_2x_hours": None, "dd_before_2x": None,
                "dd_before_2x_worst": None, "dd_before_2x_uncertain": False,
                "dim": {"data_state": "no_reference", "elapsed": False,
                        "final_eligible": False, "gain_observed": "unknown",
                        "verdict": VERDICT_NO_REFERENCE, "path": "none"},
            })
            continue
        if gran == "5m":
            n_5m += 1
        else:
            n_1h += 1
        if fp["ref_delayed"]:
            n_delayed += 1
        rows.append({
            "symbol": sym, "start_utc": ep["start_utc"], "start_ms": ref_ms,
            "granularity": gran, "n_hours": ep["n_hours"],
            "first_score": int(ep["score_kline"]) + int(ep["score_deriv"]),
            "score_total_max": ep["score_total_max"],
            "deriv_status": ep.get("deriv_status"),
            "has_futures": ep.get("deriv_status") not in (None, "no_futures"),
            **fp,
        })
    d = forward_dir(lag_hours)
    d.mkdir(parents=True, exist_ok=True)
    with (d / f"{sym}.jsonl").open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    g.uninstall()
    return {"symbol": sym, "skipped": False, "n_episodes": len(rows),
            "gran_5m": n_5m, "gran_1h": n_1h, "no_ref": n_none, "ref_delayed": n_delayed,
            "network_blocked": g.blocked}


def _pct(values, q):
    arr = np.asarray([v for v in values if v is not None and np.isfinite(v)], dtype=float)
    return round(float(np.percentile(arr, q)), 4) if len(arr) else None


def _describe(values) -> dict:
    arr = np.asarray([v for v in values if v is not None and np.isfinite(v)], dtype=float)
    if len(arr) == 0:
        return {"n": 0}
    return {"n": int(len(arr)), "mean": round(float(arr.mean()), 4),
            "p25": round(float(np.percentile(arr, 25)), 4),
            "median": round(float(np.median(arr)), 4),
            "p75": round(float(np.percentile(arr, 75)), 4),
            "p90": round(float(np.percentile(arr, 90)), 4),
            "max": round(float(arr.max()), 4)}


def load_forward_r4(lag_hours: int = 0) -> list[dict]:
    out = []
    for p in sorted(forward_dir(lag_hours).glob("*.jsonl")):
        for ln in p.read_text(encoding="utf-8").splitlines():
            if ln.strip():
                out.append(json.loads(ln))
    return out


def summarize_forward_r4(rows: list[dict]) -> dict:
    """把逐片段结果聚合成「选中后分别怎样」。

    R4 要求：分数组按**首个信号当时**的分数（`first_score`），
    片段未来最高分（`score_total_max`）只作事后描述单列。
    """
    def split(rs):
        return {
            "n": len(rs),
            "verdict": dict(Counter(r["dim"]["verdict"] for r in rs)),
            "gain_observed": dict(Counter(r["dim"]["gain_observed"] for r in rs)),
            "path": dict(Counter(r["dim"]["path"] for r in rs)),
            "final_eligible": int(sum(1 for r in rs if r["dim"]["final_eligible"])),
            "max_touch_30d": _describe([(r.get("30d") or {}).get("max_touch") for r in rs]),
            "max_close_30d": _describe([(r.get("30d") or {}).get("max_close") for r in rs]),
            "max_dd_from_entry_30d": _describe([(r.get("30d") or {}).get("max_dd_from_entry") for r in rs]),
            "max_close_drawdown_30d": _describe([(r.get("30d") or {}).get("max_close_drawdown") for r in rs]),
            "wait_hours_to_2x": _describe([r["waits"].get("wait_hours_to_2.0x") for r in rs]),
            "wait_hours_to_1_5x": _describe([r["waits"].get("wait_hours_to_1.5x") for r in rs]),
            "dd_before_2x": _describe([r.get("dd_before_2x") for r in rs]),
            "dd_before_2x_worst": _describe([r.get("dd_before_2x_worst") for r in rs]),
            "dd_before_2x_uncertain": int(sum(1 for r in rs if r.get("dd_before_2x_uncertain"))),
        }

    verdict = Counter(r["dim"]["verdict"] for r in rows)
    gain = Counter(r["dim"]["gain_observed"] for r in rows)
    path = Counter(r["dim"]["path"] for r in rows)
    gran = Counter(r["granularity"] for r in rows)
    state = Counter(r["dim"]["data_state"] for r in rows)
    keys = ("reach_2x", "reach_1_5x_only", "below_1_5x", VERDICT_UNDETERMINED,
            VERDICT_NO_REFERENCE)
    verdict_fixed = {k: verdict.get(k, 0) for k in keys}

    # 最终失败比例只用「已到期且资料完整」的片段（R2 要求）
    final_rows = [r for r in rows if r["dim"]["final_eligible"]]
    final_gain = Counter(r["dim"]["gain_observed"] for r in final_rows)
    n_final = len(final_rows)
    final_ratio = {k: (round(final_gain.get(k, 0) / n_final, 6) if n_final else None)
                   for k in ("reach_2x", "reach_1_5x_only", "below_1_5x")}

    # 先跌后涨：确定口径与保守口径都给（首次达标根内部先后不明 → 上下界）
    reached = [r for r in rows if r["dim"]["gain_observed"] == "reach_2x"]
    certain = sum(1 for r in reached if (r.get("dd_before_2x") or 0) <= FWD_DD_THRESHOLD)
    possible = sum(1 for r in reached
                   if (r.get("dd_before_2x_worst") or 0) <= FWD_DD_THRESHOLD)

    # C2：探索 / 后段按**真实结果窗口**分组；跨界项真正排除，只留在专表
    split_ms = H.R.parse_utc(REVIEW_START)
    explore, explore90, review = [], [], []
    cross30, cross90 = [], []
    for r in rows:
        if r["dim"]["verdict"] == VERDICT_NO_REFERENCE or not r.get("ref_open_utc"):
            continue
        t0 = r["start_ms"]
        if t0 < split_ms:
            c30 = (t0 + 30 * 24 * HR) >= split_ms
            c90 = (t0 + 90 * 24 * HR) >= split_ms
            if c30:
                cross30.append(r)          # 30 天结果窗跨过切点 → 不进探索组
            else:
                explore.append(r)          # 30 天干净的探索段（主窗）
            if c90:
                cross90.append(r)
            else:
                explore90.append(r)
        else:
            review.append(r)

    return {
        "n_episodes": len(rows),
        "verdict": verdict_fixed,
        "verdict_reconciles": sum(verdict_fixed.values()) == len(rows),
        "gain_observed": {k: gain.get(k, 0) for k in
                          ("reach_2x", "reach_1_5x_only", "below_1_5x", "unknown")},
        "path_fact": dict(path),
        "data_state": dict(state),
        "ref_granularity": dict(gran),
        "ref_delayed_episodes": int(sum(1 for r in rows if r.get("ref_delayed"))),
        "final_eligible": n_final,
        "final_ratio": final_ratio,
        "final_ratio_denominator": n_final,
        "reach_2x_share_with_dd20_certain": (round(certain / len(reached), 6) if reached else None),
        "reach_2x_share_with_dd20_possible": (round(possible / len(reached), 6) if reached else None),
        "reach_2x_denominator": len(reached),
        "overall": split(rows),
        "by_granularity": {g: split([r for r in rows if r["granularity"] == g])
                           for g in sorted(gran)},
        "by_segment": {
            "explore_2022_2024": split(explore),                 # 30 天结果窗干净
            "explore_2022_2024_90d_clean": split(explore90),      # 90 天结果窗干净
            "review_2025_2026": split(review),
            "cross_30d_only": split(cross30),                     # 跨界专表（不参与探索统计）
            "cross_90d_only": split(cross90),
        },
        "crossover_excluded": {"cross_30d": len(cross30), "cross_90d": len(cross90),
                               "explore_30d_clean": len(explore),
                               "explore_90d_clean": len(explore90),
                               "review": len(review),
                               "reconciles": (len(explore) + len(cross30) + len(review)
                                              == sum(1 for r in rows
                                                     if r["dim"]["verdict"] != VERDICT_NO_REFERENCE
                                                     and r.get("ref_open_utc")))},
        "by_has_futures": {"with_perp": split([r for r in rows if r.get("has_futures")]),
                           "no_perp": split([r for r in rows if not r.get("has_futures")])},
        # R4：按**首个信号当时**的分数分组（唯一可用于「当时排序是否有信息」的口径）
        "by_first_score": {f"first_score>={s}": split([r for r in rows if r["first_score"] >= s])
                           for s in (5, 8, 10, 12)},
        # 事后描述：片段未来最高分（**不能**用来证明当时排序更好）
        "by_max_score_expost_only": {
            f"score_total_max>={s}": split([r for r in rows if r["score_total_max"] >= s])
            for s in (5, 8, 10, 12)},
        "note": ("参考价 = 信号可用后第一根完整 K 线**开盘价**；参考根之后同根高低价计入未来。"
                 "窗口按真实时刻截止（open_time < ref_open + W）。"
                 "`verdict` 已把「未到期/资料不全且未达标」单列为 undetermined，不进最终失败比例；"
                 "最高触及不是已实现收益。`by_first_score` 是当时口径，`by_max_score_expost_only` 只是事后描述。"),
    }


def write_forward_tables_r4(rows: list[dict], lag_hours: int,
                            out_dir: Path | None = None) -> None:
    import csv as _csv
    out_dir = out_dir or OUT5
    cols = ["symbol", "start_utc", "granularity", "n_hours", "first_score", "score_total_max",
            "deriv_status", "ref_price", "ref_open_utc", "ref_lag_min", "ref_skipped_bars",
            "ref_delayed", "wait_hours_to_1.5x", "wait_hours_to_2.0x",
            "dd_before_2x", "dd_before_2x_worst", "dd_before_2x_uncertain",
            "dim_data_state", "dim_elapsed", "dim_final_eligible", "dim_gain_observed",
            "dim_verdict", "dim_path"]
    for key in ("7d", "30d", "90d"):
        cols += [f"{key}_max_touch", f"{key}_max_close", f"{key}_max_dd_from_entry",
                 f"{key}_max_close_drawdown", f"{key}_elapsed", f"{key}_data_state",
                 f"{key}_n_bars", f"{key}_expected_bars", f"{key}_missing_bars", f"{key}_peak_utc"]
    with ((out_dir) / f"forward_detail_lag{lag_hours}.csv").open("w", encoding="utf-8",
                                                            newline="") as fh:
        w = _csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            rec = {c: r.get(c) for c in ("symbol", "start_utc", "granularity", "n_hours",
                                         "first_score", "score_total_max", "deriv_status",
                                         "ref_price", "ref_open_utc", "ref_lag_min",
                                         "ref_skipped_bars", "ref_delayed", "dd_before_2x",
                                         "dd_before_2x_worst", "dd_before_2x_uncertain")}
            rec["wait_hours_to_1.5x"] = r["waits"].get("wait_hours_to_1.5x")
            rec["wait_hours_to_2.0x"] = r["waits"].get("wait_hours_to_2.0x")
            rec["dim_data_state"] = r["dim"]["data_state"]
            rec["dim_elapsed"] = r["dim"]["elapsed"]
            rec["dim_final_eligible"] = r["dim"]["final_eligible"]
            rec["dim_gain_observed"] = r["dim"]["gain_observed"]
            rec["dim_verdict"] = r["dim"]["verdict"]
            rec["dim_path"] = r["dim"]["path"]
            for key in ("7d", "30d", "90d"):
                blk = r.get(key) or {}
                rec[f"{key}_max_touch"] = blk.get("max_touch")
                rec[f"{key}_max_close"] = blk.get("max_close")
                rec[f"{key}_max_dd_from_entry"] = blk.get("max_dd_from_entry")
                rec[f"{key}_max_close_drawdown"] = blk.get("max_close_drawdown")
                rec[f"{key}_elapsed"] = blk.get("elapsed")
                rec[f"{key}_data_state"] = blk.get("data_state")
                rec[f"{key}_n_bars"] = blk.get("n_bars")
                rec[f"{key}_expected_bars"] = blk.get("expected_bars")
                rec[f"{key}_missing_bars"] = blk.get("missing_bars")
                rec[f"{key}_peak_utc"] = (R.ms_to_iso(blk["peak_ms"])
                                          if blk.get("peak_ms") is not None else None)
            w.writerow(rec)
    cross = Counter((r["dim"]["elapsed"], r["dim"]["data_state"], r["dim"]["verdict"])
                    for r in rows)
    lines = ["elapsed,data_state,verdict,n"]
    for (el, st, vd), c in sorted(cross.items(), key=lambda kv: (str(kv[0][0]), kv[0][1], kv[0][2])):
        lines.append(f"{el},{st},{vd},{c}")
    (out_dir / f"forward_crosstab_lag{lag_hours}.csv").write_text("\n".join(lines), encoding="utf-8")


def cmd_forward_r4(workers: int = 12, lag_hours: int = 0, limit: int | None = None) -> dict:
    ensure_dirs()
    FWD4.mkdir(parents=True, exist_ok=True)
    syms = H.usdt_spot_symbols()
    if limit:
        syms = syms[:limit]
    log(f"forward: 开始，{len(syms)} 个对象，workers={workers}，lag={lag_hours}h")
    t0 = time.perf_counter()
    fwd_legacy_ok = _legacy_reuse_ok("forward_detail")
    res = []
    done = 0
    with ProcessPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_forward_worker_r4, (s, lag_hours, fwd_legacy_ok)): s for s in syms}
        for f in as_completed(futs):
            res.append(f.result())
            done += 1
            if done % 50 == 0 or done == len(syms):
                log(f"forward: {done}/{len(syms)}  已用 {time.perf_counter()-t0:.0f}s")
    rows = load_forward_r4(lag_hours)
    summary = summarize_forward_r4(rows)
    summary.update({
        "generated_at_utc": R.ms_to_iso(int(time.time() * 1000)),
        "lag_hours": lag_hours, "workers": workers, "symbols": len(syms),
        "forward_detail_reused": bool(fwd_legacy_ok),
        "symbols_with_episodes": sum(1 for r in res if r.get("n_episodes")),
        "elapsed_sec": round(time.perf_counter() - t0, 2),
        "network_blocked_attempts": int(sum(r.get("network_blocked", 0) for r in res)),
    })
    (OUT5 / f"forward_summary_lag{lag_hours}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    write_forward_tables_r4(rows, lag_hours)
    log(f"forward: {len(rows)} 片段；verdict {summary['verdict']}，"
        f"最终可判 {summary['final_eligible']}，耗时 {summary['elapsed_sec']}s")
    return summary


# ===========================================================================
# 原版逐时点重放（r4 分片；F1 修正后的评分）
# ===========================================================================

def _baseline_worker_r4(args: tuple) -> dict:
    sym, lag_hours, overwrite, fp, legacy_ok = args
    g = _guard()
    sd = shards_dir(lag_hours)
    sd.mkdir(parents=True, exist_ok=True)
    shard = sd / f"{sym}.npz"
    meta = sd / f"{sym}.json"
    reuse_rejected = None
    if shard.exists() and meta.exists() and not overwrite:
        try:
            m = json.loads(meta.read_text(encoding="utf-8"))
            # C7：复用前核对评分核指纹 + lag + 输入身份；不符即重算，不静默读旧分片。
            # `legacy_ok`：由 closeout/reuse_decision.json 证明「评分核与当前逐字节一致」时，
            # 允许复用缺少逐分片指纹字段的 r4 原分片（只读，不修改原产物）。
            rec_fp = m.get("scoring_core_fingerprint")
            lag_match = int(m.get("lag_hours", -99)) == int(lag_hours)
            if lag_match and (rec_fp == fp or (rec_fp is None and legacy_ok)):
                g.uninstall()
                return {"symbol": sym, "skipped": True, "reuse_checked": True,
                        "reuse_fingerprint": rec_fp or ("legacy:" + fp[:16] if legacy_ok else None),
                        "network_blocked": g.blocked, **m}
            reuse_rejected = ("lag_mismatch" if not lag_match
                              else "scoring_core_fingerprint_mismatch")
        except Exception:  # noqa: BLE001
            reuse_rejected = "meta_unreadable"
    months_all = H.spot_months(sym)
    if not months_all:
        g.uninstall()
        return {"symbol": sym, "skipped": False, "n_hours": 0, "note": "no_spot_1h",
                "network_blocked": g.blocked}
    months = [m for m in months_all if m >= "2021-11"]
    candles = H.read_spot(sym, months)
    if len(candles) == 0:
        g.uninstall()
        return {"symbol": sym, "skipped": False, "n_hours": 0, "note": "no_spot_rows",
                "network_blocked": g.blocked}
    ts0 = int(candles[0, 0]); tsN = int(candles[-1, 0])
    h_first = max(0, int(math.ceil((ts0 + INTERVAL_MS - MAIN_START_MS) / INTERVAL_MS)))
    h_last = min(int(N_HOURS) - 1, int((tsN + INTERVAL_MS - MAIN_START_MS) // INTERVAL_MS))
    if h_last < h_first:
        g.uninstall()
        return {"symbol": sym, "skipped": False, "n_hours": 0, "note": "outside_main_range",
                "network_blocked": g.blocked,
                "candle_span": [R.ms_to_iso(ts0), R.ms_to_iso(tsN)]}
    dms = GRID_MS[h_first:h_last + 1]

    has_fut = R.futures_1h_dir(sym).is_dir()
    mdt = oi = ls = None
    m_days = 0
    if has_fut:
        days = [d for d in H.metrics_days(sym) if d >= "2021-12-20"]
        m_days = len(days)
        mdt, oi, ls = H.read_metrics_hourly(sym, days)
    res = evaluate_r4(sym, candles, mdt if mdt is not None else np.empty(0, dtype=np.int64),
                      oi if oi is not None else np.empty(0), ls if ls is not None else np.empty(0),
                      has_fut, dms, lag_hours=lag_hours)

    sel = res["selected"]
    episodes = []
    if sel.any():
        idxs = np.flatnonzero(sel)
        breaks = np.flatnonzero(np.diff(idxs) > 1)
        starts = np.concatenate([[0], breaks + 1])
        ends = np.concatenate([breaks, [len(idxs) - 1]])
        for a, b in zip(starts, ends):
            s_h = int(idxs[a]); e_h = int(idxs[b])
            s_ms = int(dms[s_h])
            ref = int(np.searchsorted(candles[:, 0], s_ms - INTERVAL_MS))
            if ref >= len(candles) or int(candles[ref, 0]) != s_ms - INTERVAL_MS:
                continue
            tot = res["score_total"][s_h:e_h + 1]
            episodes.append({
                "symbol": sym, "start_ms": s_ms, "start_utc": R.ms_to_iso(s_ms),
                "end_utc": R.ms_to_iso(int(dms[e_h])),
                "n_hours": int(e_h - s_h + 1),
                "score_total_max": int(tot.max()), "score_total_min": int(tot.min()),
                "score_kline": int(res["score_kline"][s_h]),
                "score_deriv": int(res["score_deriv"][s_h]),
                "trend": int(res["trend"][s_h]), "breakout": int(res["breakout"][s_h]),
                "deriv_status": (DERIV_STATUS_NAMES.get(int(res["deriv_status"][s_h]), "na")
                                 if int(res["deriv_status"][s_h]) >= 0 else "no_futures"),
                "qv24_approx": H.quote_volume_24h(candles, ref),
            })
    sigd = signals_dir(lag_hours)
    sigd.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        shard,
        h_first=np.int64(h_first), n_hours=np.int64(len(dms)),
        status=res["status"], score_kline=res["score_kline"],
        score_deriv=res["score_deriv"], score_total=res["score_total"],
        selected=res["selected"], trend=res["trend"], breakout=res["breakout"],
        deriv_status=res["deriv_status"], oi_valid_obs=res["oi_valid_obs"],
    )
    with (sigd / f"{sym}.jsonl").open("w", encoding="utf-8") as fh:
        for ep in episodes:
            fh.write(json.dumps(ep, ensure_ascii=False) + "\n")
    meta_doc = {
        "symbol": sym, "h_first": int(h_first), "n_hours": int(len(dms)),
        "span_start_utc": R.ms_to_iso(int(dms[0])), "span_end_utc": R.ms_to_iso(int(dms[-1])),
        "has_futures_market": bool(has_fut),
        "candles_loaded": int(len(candles)), "metrics_days_read": int(m_days),
        "n_selected_hours": int(sel.sum()), "n_episodes": len(episodes),
        "n_oi_gated_hours": int(res["oi_gated"].sum()),
        "status_counts": {STATUS_NAMES[k]: int((res["status"] == k).sum()) for k in STATUS_NAMES},
        "deriv_status_counts": {DERIV_STATUS_NAMES.get(k, str(k)): int((res["deriv_status"] == k).sum())
                                for k in sorted(set(res["deriv_status"].tolist()))},
        "lag_hours": lag_hours,
        "scoring_core_fingerprint": fp,     # C7：供下次复用核对
    }
    meta.write_text(json.dumps(meta_doc, ensure_ascii=False, indent=1), encoding="utf-8")
    g.uninstall()
    return {"symbol": sym, "skipped": False, "reuse_rejected": reuse_rejected,
            "network_blocked": g.blocked, **meta_doc}


def cmd_baseline_r4(workers: int = 12, lag_hours: int = 0, overwrite: bool = False,
                    limit: int | None = None, out_dir: Path | None = None) -> dict:
    ensure_dirs()
    out_dir = out_dir or OUT5          # 本批：汇总写 closeout，**不覆盖** r4 原产物
    syms = H.usdt_spot_symbols()
    if limit:
        syms = syms[:limit]
    log(f"baseline: 开始，{len(syms)} 个对象，workers={workers}，lag={lag_hours}h（F1 修正版）")
    t0 = time.perf_counter()
    fp = scoring_core_fingerprint()
    legacy_ok = _legacy_reuse_fingerprint() is not None
    res = []
    done = 0
    with ProcessPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_baseline_worker_r4, (s, lag_hours, overwrite, fp, legacy_ok)): s
                for s in syms}
        for f in as_completed(futs):
            res.append(f.result())
            done += 1
            if done % 50 == 0 or done == len(syms):
                log(f"baseline: {done}/{len(syms)}  已用 {time.perf_counter()-t0:.0f}s")
    total_hours = sum(r.get("n_hours", 0) for r in res)
    summary = {
        "generated_at_utc": R.ms_to_iso(int(time.time() * 1000)),
        "main_range": {"start": MAIN_START, "end": MAIN_END, "grid_hours": int(N_HOURS)},
        "lag_hours": lag_hours, "workers": workers,
        "scoring_core_fingerprint": fp,
        "legacy_reuse_allowed": bool(legacy_ok),
        "symbols": len(syms), "symbols_skipped_reused": sum(1 for r in res if r.get("skipped")),
        "symbols_reuse_rejected": sum(1 for r in res if r.get("reuse_rejected")),
        "reuse_reject_reasons": dict(Counter(r.get("reuse_rejected")
                                             for r in res if r.get("reuse_rejected"))),
        "total_symbol_hour_cells_with_status": int(total_hours),
        "total_symbol_hour_cells_note": (
            "C8 口径：这是「(对象, 小时) 中**有状态标记**的格数」，不是小时数。"
            "小时数（全网格）= grid_hours = 41,616。两者不可混用。"),
        "grid_hours": int(N_HOURS),
        "n_selected_hours": int(sum(r.get("n_selected_hours", 0) for r in res)),
        "n_episodes": int(sum(r.get("n_episodes", 0) for r in res)),
        "n_oi_gated_hours": int(sum(r.get("n_oi_gated_hours", 0) for r in res)),
        "n_oi_gated_note": (
            "C8 口径：这是「OI 有效观测不足被门控的格数」，是**门控计数**，"
            "**不是**受影响数。真实改分/改入选数量见 `diff_vs_v3.json` 的 `scoring_cell_diff`。"),
        "elapsed_sec": round(time.perf_counter() - t0, 2),
        "per_symbol": sorted(res, key=lambda r: -r.get("n_hours", 0)),
    }
    (out_dir / f"baseline_summary_lag{lag_hours}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"baseline: {len(syms)} 对象 / {total_hours} 有状态格 / 选中 {summary['n_selected_hours']} / "
        f"片段 {summary['n_episodes']} / OI 观测不足被门控 {summary['n_oi_gated_hours']} 格"
        f"（门控≠受影响数），耗时 {summary['elapsed_sec']}s")
    return summary


def load_matrices_r4(lag_hours: int = 0, with_components: bool = False) -> dict:
    syms = H.usdt_spot_symbols()
    S = len(syms)
    total = np.full((int(N_HOURS), S), -1, dtype=np.int8)
    selected = np.zeros((int(N_HOURS), S), dtype=bool)
    trend = np.zeros((int(N_HOURS), S), dtype=bool)
    status = np.full((int(N_HOURS), S), ST_NOT_OBSERVABLE, dtype=np.int8)
    present = np.zeros(S, dtype=bool)
    kline = np.full((int(N_HOURS), S), -1, dtype=np.int8) if with_components else None
    deriv = np.full((int(N_HOURS), S), -1, dtype=np.int8) if with_components else None
    for j, s in enumerate(syms):
        p = shards_dir(lag_hours) / f"{s}.npz"
        if not p.exists():
            continue
        with np.load(p) as z:
            hf = int(z["h_first"]); nh = int(z["n_hours"])
            total[hf:hf + nh, j] = z["score_total"]
            selected[hf:hf + nh, j] = z["selected"]
            trend[hf:hf + nh, j] = z["trend"]
            status[hf:hf + nh, j] = z["status"]
            if with_components:
                kline[hf:hf + nh, j] = z["score_kline"]
                deriv[hf:hf + nh, j] = z["score_deriv"]
        present[j] = True
    out = {"symbols": syms, "total": total, "selected": selected, "trend": trend,
           "status": status, "present": present}
    if with_components:
        out["kline"] = kline
        out["deriv"] = deriv
    return out


def load_signals_r4(lag_hours: int = 0) -> list[dict]:
    out = []
    for p in sorted(signals_dir(lag_hours).glob("*.jsonl")):
        for ln in p.read_text(encoding="utf-8").splitlines():
            if ln.strip():
                out.append(json.loads(ln))
    return out


# ===========================================================================
# R3：反向漏选——起点前曾观察 / 仅中途首次观察 / 之后观察 分开
# ===========================================================================
#
# 旧版把「事件起点前 30 天曾入选」一律记成 `及时发现`（3,335），`出现过晚` 分支永远不可达。
# 修正：三类事实独立保存，去向可加总，并给出中途出现后的**剩余机会**（同一参考规则）。

def cmd_reverse_r4(lag_hours: int = 0) -> dict:
    mat = load_matrices_r4(lag_hours)
    syms = mat["symbols"]
    sidx = {s: j for j, s in enumerate(syms)}
    selected = mat["selected"]
    status = mat["status"]
    total = mat["total"]
    events = load_events_r4()
    # C5 复用：逐片段 forward 详情已给出「从片段起点参考价起的真实 30 天」，
    # 而「首次发现时刻」正好就是某个片段起点 → 直接按 (symbol, start_ms) 查表，
    # 不再为每个对象重读 5m 原始数据（更快，且参考价/精度/完整性语义与 forward 完全一致）。
    fwd_idx: dict[tuple[str, int], dict] = {}
    for r in load_forward_r4(lag_hours):
        fwd_idx[(r["symbol"], int(r["start_ms"]))] = r
    main_start_ms = MAIN_START_MS
    main_end_ms = MAIN_END_MS

    def hidx(ms: int) -> int:
        return int((ms - main_start_ms) // HR)

    price_cache: dict[tuple[str, int], tuple[np.ndarray, int] | None] = {}

    def series_for(sym: str, lag_hours: int = 0,
                   prefer_5m: bool = False) -> tuple[np.ndarray, int] | None:
        """返回 (K 线序列, **真实** bar_ms)。

        C5 口径修正：精度必须**如实记录**，不能拿 1h 数据却按 5m 记。
        默认用 1h（快、保守）；`prefer_5m=True` 时才读 5m（只给少量关键样本用）。
        返回的 bar_ms 由数据实际中位间隔判定，不用固定常量。
        """
        key = (sym, int(lag_hours), bool(prefer_5m))
        if key not in price_cache:
            arr = np.empty((0, 6))
            bar = HR
            if prefer_5m:
                months5 = H.spot_months_interval(sym, "5m")
                arr = (H.read_spot_interval(sym, "5m", months5) if months5
                       else np.empty((0, 6)))
                bar = 300_000
            if len(arr) < 2:
                arr = H.read_spot(sym, H.spot_months(sym))
                bar = HR
            if len(arr) >= 2:
                d = np.diff(arr[:, 0])
                med = int(np.median(d)) if len(d) else bar
                # 真实精度：中位间隔 ≥ 半小时就按 1h 记
                bar = HR if med >= HR // 2 else 300_000
            price_cache[key] = (arr, bar) if len(arr) else None
        return price_cache[key]

    rows = []
    for e in events:
        s = e["symbol"]
        j = sidx.get(s)
        rec = dict(e)
        for k in ("pre_selected", "mid_selected", "late_selected"):
            rec[k] = False
        rec["first_mid_utc"] = None
        rec["first_late_utc"] = None
        rec["n_scored_pre"] = 0
        rec["n_scored_ev"] = 0
        # C5：首次发现的完整证据
        rec["first_discovery_kind"] = None
        rec["first_discovery_utc"] = None
        rec["first_discovery_ms"] = None
        rec["remaining_ref_price"] = None
        rec["remaining_ref_open_utc"] = None
        rec["remaining_bar_ms"] = None
        rec["remaining_ref_delayed"] = None
        rec["remaining_30d_touch"] = None
        rec["remaining_30d_data_state"] = None
        rec["remaining_30d_elapsed"] = None
        rec["remaining_verdict"] = None
        rec["remaining_path"] = None
        rec["remaining_opportunity"] = None
        rec["remaining_source"] = None
        rec["before_recorded_peak"] = None
        if j is None or not mat["present"][j]:
            rec.update({"disposition": "不可观察", "reason": "no_baseline_shard"})
            rows.append(rec); continue
        t0 = e["start_ms"]; t1 = e["end_ms"]
        if t0 > main_end_ms or t1 < main_start_ms:
            rec.update({"disposition": "不可观察", "reason": "event_outside_main_range"})
            rows.append(rec); continue
        p0 = max(0, hidx(t0 - 30 * 86400_000)); p1 = min(int(N_HOURS) - 1, hidx(t0) - 1)
        e0 = max(0, hidx(t0)); e1 = min(int(N_HOURS) - 1, hidx(t1))
        l0 = min(int(N_HOURS) - 1, hidx(t1) + 1); l1 = min(int(N_HOURS) - 1, hidx(t1) + 30 * 24)
        pre_sel = selected[p0:p1 + 1, j] if p1 >= p0 else np.zeros(0, dtype=bool)
        ev_sel = selected[e0:e1 + 1, j] if e1 >= e0 else np.zeros(0, dtype=bool)
        late_sel = selected[l0:l1 + 1, j] if l1 >= l0 else np.zeros(0, dtype=bool)
        pre_tot = total[p0:p1 + 1, j] if p1 >= p0 else np.zeros(0, dtype=np.int8)
        ev_tot = total[e0:e1 + 1, j] if e1 >= e0 else np.zeros(0, dtype=np.int8)
        rec["pre_selected"] = bool(pre_sel.any())
        rec["mid_selected"] = bool(ev_sel.any())
        rec["late_selected"] = bool(late_sel.any())
        rec["n_scored_pre"] = int((pre_tot >= 0).sum())
        rec["n_scored_ev"] = int((ev_tot >= 0).sum())
        if rec["mid_selected"]:
            rec["first_mid_utc"] = R.ms_to_iso(int(GRID_MS[e0 + int(np.argmax(ev_sel))]))
        if rec["late_selected"]:
            rec["first_late_utc"] = R.ms_to_iso(int(GRID_MS[l0 + int(np.argmax(late_sel))]))

        # —— C5：首次真正发现（pre → mid → late 里最早的那个），按它的参考价复算剩余机会 ——
        disc_h = None; disc_kind = None
        if pre_sel.any():
            disc_h, disc_kind = p0 + int(np.argmax(pre_sel)), "pre"
        elif ev_sel.any():
            disc_h, disc_kind = e0 + int(np.argmax(ev_sel)), "mid"
        elif late_sel.any():
            disc_h, disc_kind = l0 + int(np.argmax(late_sel)), "late"
        if disc_h is not None:
            disc_ms = int(GRID_MS[disc_h])
            rec["first_discovery_kind"] = disc_kind
            rec["first_discovery_ms"] = disc_ms
            rec["first_discovery_utc"] = R.ms_to_iso(disc_ms)
            fr = fwd_idx.get((s, disc_ms))
            fp = None
            if fr is not None and fr.get("ref_price") is not None:
                rec["remaining_source"] = "forward_reuse"
                fp = {"ref_price": fr.get("ref_price"), "ref_open_utc": fr.get("ref_open_utc"),
                      "bar_ms": fr.get("bar_ms"), "ref_delayed": fr.get("ref_delayed"),
                      "30d": fr.get("30d") or {}, "dim": fr.get("dim") or {}}
            else:
                # 罕见回退：发现时刻不是片段起点 → 用 1h 数据重算，精度如实记 1h
                got = series_for(s, lag_hours, prefer_5m=False)
                if got is not None:
                    arr, bar = got
                    fp = forward_from_series_r4(arr, disc_ms, bar)
                    if fp is not None:
                        rec["remaining_source"] = "series_1h"
            if fp is not None:
                w30 = fp.get("30d") or {}
                rec["remaining_ref_price"] = fp["ref_price"]
                rec["remaining_ref_open_utc"] = fp["ref_open_utc"]
                rec["remaining_bar_ms"] = fp["bar_ms"]
                rec["remaining_ref_delayed"] = fp["ref_delayed"]
                rec["remaining_30d_touch"] = w30.get("max_touch")
                rec["remaining_30d_data_state"] = w30.get("data_state")
                rec["remaining_30d_elapsed"] = w30.get("elapsed")
                rec["remaining_verdict"] = fp["dim"]["verdict"]
                rec["remaining_path"] = fp["dim"]["path"]
                rec["remaining_opportunity"] = w30.get("max_touch")
        if e.get("peak_ms") is not None and rec["first_discovery_ms"] is not None:
            rec["before_recorded_peak"] = bool(rec["first_discovery_ms"] < e["peak_ms"])
        total_scored = rec["n_scored_pre"] + rec["n_scored_ev"]
        if total_scored == 0 and not rec["late_selected"]:
            codes = np.concatenate([status[p0:p1 + 1, j] if p1 >= p0 else np.zeros(0, np.int8),
                                    status[e0:e1 + 1, j] if e1 >= e0 else np.zeros(0, np.int8)])
            dom = STATUS_NAMES[int(Counter(codes.tolist()).most_common(1)[0][0])] if len(codes) else "na"
            rec.update({"disposition": "缺资料", "reason": f"窗口内无可评分小时，主状态={dom}"})
        elif rec["pre_selected"]:
            rec.update({"disposition": "起点前曾观察", "reason": None})
        elif rec["mid_selected"]:
            rec.update({"disposition": "仅中途首次观察", "reason": None})
        elif rec["late_selected"]:
            rec.update({"disposition": "之后观察", "reason": None})
        else:
            rec.update({"disposition": "低分或排名靠后", "reason": "窗口内从未入选"})
        rows.append(rec)

    with (OUT5 / f"reverse_events_lag{lag_hours}.jsonl").open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    disp = Counter(r["disposition"] for r in rows)
    tier_disp: dict = {}
    for r in rows:
        tier_disp.setdefault(str(r["tier"]), Counter())[r["disposition"]] += 1
    cross = Counter((r["pre_selected"], r["mid_selected"], r["late_selected"]) for r in rows)
    late = [r for r in rows if r["disposition"] == "之后观察"]
    late_with = [r for r in late if r["remaining_30d_touch"] is not None]
    late_reach2 = [r for r in late_with if r["remaining_30d_touch"] >= 2.0]
    by_kind: dict = {}
    for r in rows:
        k = r["first_discovery_kind"]
        if k is None:
            continue
        by_kind.setdefault(k, []).append(r["remaining_30d_touch"])
    summary = {
        "generated_at_utc": R.ms_to_iso(int(time.time() * 1000)),
        "n_events": len(rows),
        "disposition": dict(disp),
        "disposition_reconciles": sum(disp.values()) == len(rows),
        "by_tier": {k: dict(v) for k, v in sorted(tier_disp.items(), key=lambda kv: -float(kv[0]))},
        "pre_mid_late_crosstab": {
            f"pre={int(k[0])}_mid={int(k[1])}_late={int(k[2])}": v for k, v in sorted(cross.items())},
        # C5：首次发现与剩余机会（按首次真正发现的参考价复算，不倒补峰前高点）
        "first_discovery_kind_counts": dict(Counter(
            r["first_discovery_kind"] for r in rows if r["first_discovery_kind"])),
        "remaining_30d_touch_by_kind": {k: _describe(v) for k, v in sorted(by_kind.items())},
        "mid_only_remaining_opportunity": _describe(
            [r["remaining_30d_touch"] for r in rows if r["disposition"] == "仅中途首次观察"]),
        "late_observations": {
            "n": len(late),
            "n_before_recorded_peak": int(sum(1 for r in late if r.get("before_recorded_peak"))),
            "n_with_remaining_computed": len(late_with),
            "n_remaining_reach_2x": len(late_reach2),
            "remaining_30d_touch": _describe([r["remaining_30d_touch"] for r in late_with]),
            "remaining_data_state": dict(Counter(r["remaining_30d_data_state"] for r in late_with)),
            "remaining_bar_ms": dict(Counter(r["remaining_bar_ms"] for r in late_with)),
            "remaining_source": dict(Counter(r.get("remaining_source") for r in late_with)),
            "note": ("『之后观察』只说明有记录，**不等于**已证实过晚；"
                     "剩余机会从**首次真正发现**的参考价起算，不倒补峰值前高点，"
                     "也不自动判为仍有价值。"),
        },
        "note": ("`出现过晚`（之后观察）现为可达分支；三类事实独立保存，去向可加总。"
                 "起点前 30 天出现过**不自动算**可操作捕捉。"
                 "C5：所有可用首次发现都保留参考时间/价格/精度/资料与到期状态/倍数/路径来源；"
                 "退回 1h 数据时精度如实记为 3600000（不再固定按 5m 记）。"
                 "范围过滤（闸门）历史不可重建 → unknown；24h 报价成交额近似只作敏感性。"),
    }
    (OUT5 / f"reverse_summary_lag{lag_hours}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"reverse: {len(rows)} 事件 → {dict(disp)}")
    return summary


# ===========================================================================
# R5：关注数量完整对照 + 真实人工负担
# ===========================================================================
#
# 旧版问题：
#   · 曲线只算到 60，且把「每轮 N」与「每天 N」混用；4 个已捕捉事件的最佳名次是 66/73/62/67。
#   · 随机对照不断换币，每天不同币数远高于评分排序（180 vs 70），
#     这不是「同等工作量下评分无甄别力」，而是两种排序的人工量本来就不同。
#
# 修正：
#   · 曲线从**首位到全可评分范围**（不截 60）。
#   · 同一排序同时报：每轮人数 N、每日不同币数、首次加入、退出后重入、持续关注占用。
#   · 总分 / 简单趋势 / 3 个固定随机种子在**同一币池、同一资料、同一节奏**下比较。

ATT_N_VALUES = (5, 10, 20, 30, 50, 70, 100, 150, 200, 300, 500, 754)
# C4：固定 2/3/6 小时节奏（与小时底账并列，不另造时钟）
ATT_CADENCES = (1, 2, 3, 6)


def _event_best_rank(rank: np.ndarray, events: list[dict], sidx: dict,
                     present: np.ndarray) -> list[int | None]:
    out: list[int | None] = []
    for e in events:
        j = sidx.get(e["symbol"])
        if j is None or not present[j]:
            out.append(None); continue
        t0 = e["start_ms"]
        w0 = max(0, int((t0 - 30 * 86400_000 - MAIN_START_MS) // HR))
        w1 = min(int(N_HOURS) - 1, int((t0 - MAIN_START_MS) // HR))
        if w1 < 0 or w0 > w1:
            out.append(None); continue
        m = int(rank[w0:w1 + 1, j].min())
        out.append(m if m < 32767 else None)
    return out


def _covered_curve(best_ranks: list[int | None], max_n: int) -> dict:
    hist = Counter(m for m in best_ranks if m is not None)
    covered = {}
    run = 0
    for n in range(1, max_n + 1):
        run += hist.get(n, 0)
        covered[n] = run
    return covered


def _workload(rank: np.ndarray, N: int, n_syms: int, cadence: int = 1) -> dict:
    """C4 修正版人工负担：按**各查询时刻的名单变化**统计，小时级重入不漏。

    底账 = 每个查询时刻的名单（`rank <= N`）。`cadence` 小时查询一次，
    同一底账派生 1/2/3/6 小时节奏，不另造时钟。
      · 首次加入 = 某对象第一次出现在名单里（每个对象最多计一次）
      · 重入     = 除首次之外的每一次「上一查询时刻不在名单、这一时刻在」
      · 提醒     = 每次进入（首次 + 重入）都会产生一条提醒
    """
    mask = rank <= N
    n_hours = mask.shape[0]
    q = np.arange(0, n_hours, max(1, int(cadence)))
    sub = mask[q]                                   # (n_query, S)
    n_q = sub.shape[0]
    if n_q == 0:
        return {"N": int(N), "cadence_hours": int(cadence), "n_query_points": 0}
    prev = np.vstack([np.zeros((1, n_syms), dtype=bool), sub[:-1]])
    entry = sub & ~prev                             # 每次进入（含首次）
    ever = np.zeros(n_syms, dtype=bool)
    first_mask = np.zeros_like(entry)
    for k in range(n_q):
        newf = entry[k] & ~ever
        first_mask[k] = newf
        ever |= entry[k]
    first_total = int(first_mask.sum())
    entry_total = int(entry.sum())
    re_total = entry_total - first_total

    # 每日去重名单规模（按真实日 = 24 小时）
    n_days = n_hours // 24
    day_sets = [mask[a:a + 24].any(axis=0) for a in range(0, n_days * 24, 24)]
    daily = np.asarray([int(ds.sum()) for ds in day_sets], dtype=float) if day_sets else np.zeros(1)
    # 每个查询时刻的名单人数
    per_q = np.asarray([int(sub[k].sum()) for k in range(n_q)], dtype=float)
    # 每日的首次加入 / 重入（按查询时刻归属到日）
    q_day = (q // 24).astype(np.int64)
    fa_by_day = np.zeros(max(n_days, 1), dtype=float)
    re_by_day = np.zeros(max(n_days, 1), dtype=float)
    for k in range(n_q):
        d = int(q_day[k])
        if 0 <= d < len(fa_by_day):
            fa_by_day[d] += float(first_mask[k].sum())
            re_by_day[d] += float((entry[k] & ~first_mask[k]).sum())
    # 持续关注占用：连续留在名单里的查询次数（× 节奏 = 小时）
    runs: list[int] = []
    for j in range(n_syms):
        col = sub[:, j]
        if not col.any():
            continue
        idx = np.flatnonzero(col)
        brk = np.flatnonzero(np.diff(idx) > 1)
        starts = np.concatenate([[0], brk + 1])
        ends = np.concatenate([brk, [len(idx) - 1]])
        runs.extend(int(idx[e] - idx[s] + 1) * int(cadence) for s, e in zip(starts, ends))
    return {
        "N": int(N),
        "cadence_hours": int(cadence),
        "n_query_points": int(n_q),
        "per_query_mean": round(float(per_q.mean()), 4),
        "per_query_median": float(np.median(per_q)),
        "daily_distinct": {"mean": round(float(daily.mean()), 2),
                           "median": float(np.median(daily)),
                           "p90": float(np.percentile(daily, 90)),
                           "max": float(daily.max())},
        "first_added_total": first_total,
        "first_added_per_day_median": float(np.median(fa_by_day)),
        "re_added_total": int(re_total),
        "re_added_per_day_median": float(np.median(re_by_day)),
        "reminders_total": entry_total,
        "occupancy_hours": _describe([float(x) for x in runs]),
        "n_days": n_days,
    }


def _occupancy(rank: np.ndarray, N: int, n_syms: int) -> dict:
    """持续关注占用：单个对象连续留在前 N 的小时数分布。"""
    mask = rank <= N
    runs: list[int] = []
    for j in range(n_syms):
        col = mask[:, j]
        if not col.any():
            continue
        idx = np.flatnonzero(col)
        brk = np.flatnonzero(np.diff(idx) > 1)
        starts = np.concatenate([[0], brk + 1])
        ends = np.concatenate([brk, [len(idx) - 1]])
        runs.extend(int(idx[e] - idx[s] + 1) for s, e in zip(starts, ends))
    return _describe([float(x) for x in runs])


def cmd_attention_r4(lag_hours: int = 0, max_rank: int | None = None) -> dict:
    mat = load_matrices_r4(lag_hours)
    syms = mat["symbols"]
    sidx = {s: j for j, s in enumerate(syms)}
    total = mat["total"]
    trend = mat["trend"]
    present = mat["present"]
    events = load_events_r4()
    scoreable = total >= 0
    n_syms = len(syms)
    top = max_rank or n_syms

    rankings: dict[str, np.ndarray] = {
        "score": rank_matrix_r4(total),
        "trend": _rank_rows_r4(trend.astype(np.int16), scoreable),
    }
    for i in range(3):
        seed = 20261009 + i
        rankings[f"random_{seed}"] = random_rank_matrix_r4(seed, total)

    out: dict = {"max_rank": int(top), "n_symbols": n_syms,
                 "n_events": len(events), "n_days": int(N_HOURS) // 24,
                 "workload_N_values": list(ATT_N_VALUES),
                 "cadences_hours": list(ATT_CADENCES)}
    for name, rk in rankings.items():
        best = _event_best_rank(rk, events, sidx, present)
        out.setdefault("covered_by_n", {})[name] = _covered_curve(best, top)
        out.setdefault("event_best_rank_none", {})[name] = int(sum(1 for b in best if b is None))
        # C4：小时底账（cadence=1）与 2/3/6 小时节奏都从同一底账派生
        out.setdefault("workload", {})[name] = {
            f"cadence{c}": {str(N): _workload(rk, N, n_syms, cadence=c)
                            for N in ATT_N_VALUES if N <= n_syms}
            for c in ATT_CADENCES}
    # 持续关注占用：只对评分排序的三个代表 N 计算（其余排序口径相同，不重复跑）
    out["occupancy_score"] = {str(N): _occupancy(rankings["score"], N, n_syms)
                              for N in (20, 70, 200) if N <= n_syms}
    sel_per_hour = mat["selected"].sum(axis=1)
    out["load"] = {
        "selected_per_hour_mean": float(sel_per_hour.mean()),
        "selected_per_hour_median": float(np.median(sel_per_hour)),
        "selected_per_hour_p90": float(np.percentile(sel_per_hour, 90)),
        "selected_per_hour_max": int(sel_per_hour.max()),
        "scoreable_per_hour_mean": float(scoreable.sum(axis=1).mean()),
        "scoreable_per_hour_median": float(np.median(scoreable.sum(axis=1))),
    }
    out["note"] = ("每轮 N 不等于每天 N；随机排序不断换币，每日不同币数天然更高，"
                   "不能读成「同等工作量下评分无甄别力」。"
                   "曲线已算到全可评分范围，不再截在 60。"
                   "C4：重入按**小时底账**统计，小时级反复进出不漏；"
                   "2/3/6 小时节奏由同一底账派生，降频不等于保住了机会。")
    (OUT5 / f"attention_lag{lag_hours}.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    # 可读曲线
    lines = ["n,score_covered,trend_covered,random_mean_covered"]
    for n in range(1, top + 1):
        rm = float(np.mean([out["covered_by_n"][f"random_{20261009+i}"][n] for i in range(3)]))
        lines.append(f"{n},{out['covered_by_n']['score'][n]},"
                     f"{out['covered_by_n']['trend'][n]},{rm:.1f}")
    (OUT5 / f"attention_curve_lag{lag_hours}.csv").write_text("\n".join(lines), encoding="utf-8")
    log(f"attention: 事件 {len(events)}；曲线到 {top}；"
        f"每小时选中 均值 {out['load']['selected_per_hour_mean']:.1f}")
    return out


# ===========================================================================
# R5：保留刻度 + 重复提醒 + 占用时间（全范围）
# ===========================================================================

def cmd_retention_r4(lag_hours: int = 0, max_rank: int | None = None) -> dict:
    mat = load_matrices_r4(lag_hours)
    syms = mat["symbols"]
    sidx = {s: j for j, s in enumerate(syms)}
    selected = mat["selected"]
    present = mat["present"]
    events = load_events_r4()
    rank = rank_matrix_r4(mat["total"])
    top = max_rank or len(syms)

    def window_best(e):
        j = sidx.get(e["symbol"])
        if j is None or not present[j]:
            return None
        t0 = e["start_ms"]
        w0 = max(0, int((t0 - 30 * 86400_000 - MAIN_START_MS) // HR))
        w1 = min(int(N_HOURS) - 1, int((t0 - MAIN_START_MS) // HR))
        if w1 < 0 or w0 > w1:
            return None
        return j, w0, w1

    captured, uncaptured = [], []
    for e in events:
        got = window_best(e)
        if got is None:
            continue
        j, w0, w1 = got
        (captured if selected[w0:w1 + 1, j].any() else uncaptured).append(e)
    base_n = len(captured)
    ranks = []
    for e in captured:
        j, w0, w1 = window_best(e)
        m = int(rank[w0:w1 + 1, j].min())
        ranks.append(m if m < 32767 else None)
    covered = {n: sum(1 for m in ranks if m is not None and m <= n) for n in range(1, top + 1)}
    scale = H.retention_scale(covered, base_n, top)

    eps_by_sym: dict[str, list[tuple[int, int]]] = {}
    for ep in load_signals_r4(lag_hours):
        eps_by_sym.setdefault(ep["symbol"], []).append((ep["start_ms"], ep["n_hours"]))
    reminders, occupancy_h = [], []
    for e in captured:
        lst = eps_by_sym.get(e["symbol"], [])
        t0 = e["start_ms"]
        reminders.append(sum(1 for (s, _) in lst if t0 - 30 * 86400_000 <= s <= t0))
    for lst in eps_by_sym.values():
        for (_, nh) in lst:
            occupancy_h.append(nh)

    doc = {
        "generated_at_utc": R.ms_to_iso(int(time.time() * 1000)),
        "lag_hours": lag_hours, "max_rank": int(top),
        "baseline_captured_events": base_n,
        "baseline_uncaptured_events": len(uncaptured),
        "retention_scale": scale,
        "covered_by_n": covered,
        "reminders_per_event_30d": _describe(reminders),
        "episode_occupancy_hours": _describe(occupancy_h),
        "episode_occupancy_days": _describe([h / 24.0 for h in occupancy_h]),
        "note": ("保留刻度只是完整曲线的**阅读刻度**，不是用户已批准的漏选容忍线。"
                 "曲线已算到全可评分范围（不再截 60），够不到就如实写 unreachable。"),
    }
    (OUT5 / f"retention_lag{lag_hours}.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"retention: 已捕捉 {base_n}；刻度 " +
        " ".join(f"{k}={v['required_n']}" for k, v in scale.items()))
    return doc


# ===========================================================================
# 覆盖核对 + 完整性（r4）
# ===========================================================================

def cmd_universe_r4() -> dict:
    ensure_dirs()
    spot = H.usdt_spot_symbols()
    fut = set(H.usdt_futures_symbols())
    met = set(H.usdt_metrics_symbols())
    rows = []
    for s in spot:
        mo = H.spot_months(s)
        dy = H.metrics_days(s)
        rows.append({
            "symbol": s,
            "spot_1h_months": len(mo),
            "spot_1h_first": mo[0] if mo else None,
            "spot_1h_last": mo[-1] if mo else None,
            "has_futures_market": s in fut,
            "metrics_days": len(dy),
            "metrics_first": dy[0] if dy else None,
            "metrics_last": dy[-1] if dy else None,
        })
    fut_only = sorted(fut - set(spot))
    met_only = sorted(met - set(spot))
    doc = {
        "generated_at_utc": R.ms_to_iso(int(time.time() * 1000)),
        "main_range": {"start": MAIN_START, "end": MAIN_END, "grid_hours": int(N_HOURS)},
        "counts": {
            "spot_usdt_symbols": len(spot),
            "futures_usdt_symbols": len(fut),
            "metrics_usdt_symbols": len(met),
            "same_name_all_three": len(set(spot) & fut & met),
            "spot_with_futures": sum(1 for r in rows if r["has_futures_market"]),
            "spot_without_futures": sum(1 for r in rows if not r["has_futures_market"]),
            "futures_without_spot": len(fut_only),
            "metrics_without_spot": len(met_only),
        },
        "examples_futures_without_spot": fut_only[:30],
        "examples_metrics_without_spot": met_only[:30],
        "unknowns": [
            "历史市值：归档无序列，无法重建当时的市值闸门",
            "当时交易资格：文件存在不等于当时该现货对在交易",
            "公开可得时间：归档未记录分片发布时刻，只有下载时间",
            "更名/换币（KLAY→KAIA 等）：未做身份映射，只表现为序列缺口",
            "metrics 全市场覆盖自 2021-12-01 起（2020 仅 BTCUSDT 等少数）",
        ],
        "symbols": rows,
    }
    (OUT5 / "coverage.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1),
                                        encoding="utf-8")
    log(f"universe: {len(spot)} spot / {len(fut)} futures / {len(met)} metrics；"
        f"有合约 {doc['counts']['spot_with_futures']}，无合约 {doc['counts']['spot_without_futures']}")
    return doc


def cmd_integrity_r4(lag_hours: int = 0) -> dict:
    """完整性：所有去向对账、未知/未成熟、实际范围、原始分片引用。"""
    mat = load_matrices_r4(lag_hours)
    syms = mat["symbols"]; status = mat["status"]
    cnt = Counter()
    for k, name in STATUS_NAMES.items():
        cnt[name] = int((status == k).sum())
    total_cells = int(N_HOURS) * len(syms)
    events = load_events_r4()
    doc = {
        "generated_at_utc": R.ms_to_iso(int(time.time() * 1000)),
        "grid_hours": int(N_HOURS), "symbols": len(syms),
        "total_cells": total_cells,
        "status_counts": dict(cnt),
        "sum_check": sum(cnt.values()) == total_cells,
        "present_shards": int(mat["present"].sum()),
        "events_total": len(events),
        "events_immature_30d": sum(1 for e in events if not e["mature_30d"]),
        "events_immature_90d": sum(1 for e in events if not e["mature_90d"]),
        "events_partial_30d": sum(1 for e in events if not e["complete_30d"]),
        "events_partial_90d": sum(1 for e in events if not e["complete_90d"]),
        "unknown_scope": ["历史市值", "当时交易资格", "分片发布时刻", "更名/换币身份"],
        "note": ("not_observable = 该小时该对象在归档中无可用 K 线（未观测），不是「未入选」。"
                 "事件 `mature`/`complete` 已按真实时刻判定（R3）。"),
    }
    (OUT5 / f"integrity_lag{lag_hours}.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"integrity: cells={total_cells} 对账={doc['sum_check']}；"
        f"事件 {len(events)}（30 天未成熟 {doc['events_immature_30d']}，"
        f"资料不全 {doc['events_partial_30d']}）")
    return doc


# ===========================================================================
# R6：补充结果生成器（恢复可复跑入口）
# ===========================================================================
#
# 主代理裁定：原补充脚本未保存 → 本批必须给出**可复跑的生成入口**，
# 并统一使用已修正的窗口、分母与输出。全部由 r4 冻结分片 + r4 事件表派生。
#
# 五个产物（与旧引用一一对应，但口径已订正）：
#   supplementary_decision_curve_lag{n}.json  —— P@N / lift@N / 召回 / 每日去重名单
#   supplementary_ranking_lift_lag{n}.json    —— 各排序的横截面 lift + 召回 + 门槛表
#   supplementary_feature_lift_lag{n}.json    —— 单特征排序 lift
#   supplementary_time_split_lag{n}.json      —— 探索段 / 后段分段 lift
#   supplementary_recall_daily_lag{n}.json    —— 召回与每日去重名单规模
#
# 口径（与 ready_for_glm.md 第 6 节一致，分母已修正）：
#   E[j,h] = 1 若对象 j 在 [h+1, h+720] 内有事件起点（档位 ≥ 目标）
#   基准    = 该小时全部**可评分**对象上 E 的均值
#   P@N     = 该小时按排序取前 N（并列按 symbol 升序）的 E 均值，再对小时取平均
#             （分母 = Σ_h min(N, 该小时可评分对象数)）

SUP_N_MAX = 60
SUP_TIERS = (("tier2", 2.0), ("tier3", 3.0), ("tier10", 10.0))
SUP_DAILY_N = (10, 20, 30)
FEATURES_DIR = OUT5 / "features"
SUP_FEATURES = ("dist60h", "ret30d", "ret7d", "volratio", "boxwidth", "qv24")
SPLIT_HOUR = int((H.R.parse_utc(REVIEW_START) - MAIN_START_MS) // HR)

# --- C3：直接后果标签（从「当时参考价」起、真实未来 30 天窗口） ---
OUTCOME_HORIZON_H = 720                      # 30 天真实后果窗口，与 forward 30 天口径一致
OUTCOME_STATES = ("complete", "partial_tail", "gap", "no_reference")
OUTCOME_VERDICTS = ("reach_2x", "reach_1_5x_only", "below_1_5x",
                    "undetermined", "no_reference")
OUTCOME_STATE_CODE = {s: i for i, s in enumerate(OUTCOME_STATES)}
OUTCOME_VERDICT_CODE = {s: i for i, s in enumerate(OUTCOME_VERDICTS)}


def features_dir(lag_hours: int) -> Path:
    return FEATURES_DIR / f"lag{int(lag_hours)}"


def _features_worker_r4(args: tuple) -> dict:
    """逐币算横截面特征（**C1 修正版**）。

    修正两件事：
      ① **时钟对齐**：特征在决策小时 h 只能用「已收盘且已可用」的资料 ——
         即 `open_time = 决策时刻 − 1h` 的那根（与主评分 `evaluate` 的 adopted 根一致）。
         因此 K 线 i 只服务决策小时 `(ts_i − MAIN_START)/1h + 1`，不再服务它自己的开盘小时。
      ② **真实时间窗 + 缺口**：回看窗口按真实小时数建窗；窗口内小时数不足
         （历史不足或有缺口）→ 该特征记 NaN（声明不可用），不静默用更少根数顶替。
    """
    sym, lag_hours = args
    g = _guard()
    months = H.spot_months(sym)
    if not months:
        g.uninstall()
        return {"symbol": sym, "rows": 0, "network_blocked": g.blocked}
    c = H.read_spot(sym, months)
    if len(c) < 2:
        g.uninstall()
        return {"symbol": sym, "rows": 0, "network_blocked": g.blocked}
    ts = c[:, 0].astype(np.int64)
    high = c[:, 2]; low = c[:, 3]; close = c[:, 4]; vol = c[:, 5]
    import pandas as pd
    # 重采样到连续整点网格：缺口小时 = NaN，rolling(min_periods=W) 遇缺口自动给 NaN
    start = int(ts[0])
    rel = ((ts - start) // HR).astype(np.int64)
    grid = pd.RangeIndex(int(rel[-1]) + 1)
    hs = pd.Series(high, index=rel).reindex(grid)
    ls_ = pd.Series(low, index=rel).reindex(grid)
    cs = pd.Series(close, index=rel).reindex(grid)
    vs = pd.Series(vol, index=rel).reindex(grid)

    def rmax(s, w):
        return s.rolling(w, min_periods=w).max()

    def rmin(s, w):
        return s.rolling(w, min_periods=w).min()

    def rmean(s, w):
        return s.rolling(w, min_periods=w).mean()

    dist60h = (cs / rmax(hs, 1440) - 1.0)          # 含当根：close_t / max(high[t-1439..t])
    ret30d = (cs / cs.shift(720) - 1.0)            # 需要恰好 t-720 那根
    ret7d = (cs / cs.shift(168) - 1.0)
    boxwidth = (rmax(hs, 168) - rmin(ls_, 168)) / rmean(cs, 168)
    volratio = vs / rmean(vs, 168)
    qv24 = (cs * vs).rolling(24, min_periods=24).sum()

    abs_hour = (ts - MAIN_START_MS) // HR            # 该 K 线的开盘小时
    keep = (abs_hour + 1 >= 0) & (abs_hour + 1 < int(N_HOURS))
    sel = np.flatnonzero(keep)
    fd = features_dir(lag_hours)
    fd.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        fd / f"{sym}.npz",
        hour_idx=(abs_hour[sel] + 1).astype(np.int64),      # ← 决策小时 = 开盘小时 + 1
        dist60h=dist60h.to_numpy()[rel[sel]].astype(np.float32),
        ret30d=ret30d.to_numpy()[rel[sel]].astype(np.float32),
        ret7d=ret7d.to_numpy()[rel[sel]].astype(np.float32),
        volratio=volratio.to_numpy()[rel[sel]].astype(np.float32),
        boxwidth=boxwidth.to_numpy()[rel[sel]].astype(np.float32),
        qv24=qv24.to_numpy()[rel[sel]].astype(np.float32),
    )
    g.uninstall()
    return {"symbol": sym, "rows": int(len(sel)), "network_blocked": g.blocked}


def cmd_features_r4(workers: int = 12, lag_hours: int = 0) -> dict:
    ensure_dirs()
    syms = H.usdt_spot_symbols()
    log(f"features: 开始，{len(syms)} 个对象，workers={workers}")
    t0 = time.perf_counter()
    res = []
    done = 0
    with ProcessPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_features_worker_r4, (s, lag_hours)): s for s in syms}
        for f in as_completed(futs):
            res.append(f.result())
            done += 1
            if done % 100 == 0 or done == len(syms):
                log(f"features: {done}/{len(syms)}  已用 {time.perf_counter()-t0:.0f}s")
    summary = {
        "generated_at_utc": R.ms_to_iso(int(time.time() * 1000)),
        "lag_hours": lag_hours, "symbols": len(syms),
        "symbols_with_features": sum(1 for r in res if r["rows"]),
        "total_rows": int(sum(r["rows"] for r in res)),
        "elapsed_sec": round(time.perf_counter() - t0, 2),
        "features": list(SUP_FEATURES),
    }
    (OUT5 / f"features_summary_lag{lag_hours}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"features: {summary['symbols_with_features']} 对象 / {summary['total_rows']} 行，"
        f"耗时 {summary['elapsed_sec']}s")
    return summary


def _load_feature_matrix(name: str, lag_hours: int, syms: list[str]) -> np.ndarray:
    M = np.full((int(N_HOURS), len(syms)), np.nan, dtype=np.float32)
    fd = features_dir(lag_hours)
    for j, s in enumerate(syms):
        p = fd / f"{s}.npz"
        if not p.exists():
            continue
        with np.load(p) as z:
            if name not in z.files:
                continue
            M[z["hour_idx"], j] = z[name]
    return M


def _order_and_rank(value: np.ndarray, scoreable: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """按行降序（value 越大越好）给排序与名次；不可评分列排最后、名次 32767。"""
    Hn, S = value.shape
    order = np.empty((Hn, S), dtype=np.int32)
    rank = np.full((Hn, S), 32767, dtype=np.int16)
    step = 1024
    for a in range(0, Hn, step):
        b = min(Hn, a + step)
        blk = np.where(scoreable[a:b], value[a:b].astype(np.float64), -np.inf)
        od = np.argsort(-blk, axis=1, kind="stable")
        order[a:b] = od
        rk = np.empty(od.shape, dtype=np.int16)
        rows = np.arange(od.shape[0])[:, None]
        rk[rows, od] = np.arange(1, od.shape[1] + 1, dtype=np.int16)[None, :]
        rk[~scoreable[a:b]] = 32767
        rank[a:b] = rk
    return order, rank


def _precision_by_n(rank: np.ndarray, E: np.ndarray, scoreable: np.ndarray,
                    n_max: int = SUP_N_MAX) -> dict:
    """P@N：分子 = Σ_h 前 N 命中，分母 = Σ_h min(N, 该小时可评分对象数)。"""
    Hn, S = rank.shape
    Em = (E & scoreable)
    # 按名次重排 E，再累计
    idx = np.argsort(rank, axis=1, kind="stable")
    Es = np.take_along_axis(Em, idx, axis=1)
    cum = np.cumsum(Es, axis=1, dtype=np.int64)
    n_sc = scoreable.sum(axis=1).astype(np.int64)
    valid = n_sc > 0
    cv = cum[valid]
    nv = n_sc[valid]
    out = {}
    for n in range(1, n_max + 1):
        if n > S:
            break
        den = int(np.minimum(nv, n).sum())
        out[n] = (round(float(cv[:, n - 1].sum()) / den, 6) if den else None)
    return out


def _base_rate(E: np.ndarray, scoreable: np.ndarray) -> float | None:
    n = int(scoreable.sum())
    if n == 0:
        return None
    return round(float(E[scoreable].sum()) / n, 8)


def _recall_any_30d(rank: np.ndarray, events: list[dict], sidx: dict,
                    present: np.ndarray, tier_min: float,
                    n_max: int = SUP_N_MAX) -> dict:
    best: list[int] = []
    for e in events:
        if e["tier"] < tier_min:
            continue
        j = sidx.get(e["symbol"])
        if j is None or not present[j]:
            continue
        s = int((e["start_ms"] - MAIN_START_MS) // HR)
        if s < 0 or s >= int(N_HOURS):
            continue
        w0 = max(0, s - 720); w1 = min(int(N_HOURS) - 1, s - 1)
        best.append(int(rank[w0:w1 + 1, j].min()) if w1 >= w0 else 32767)
    return {str(n): int(sum(1 for m in best if m <= n)) for n in range(1, n_max + 1)}


def _recall_at_start(rank: np.ndarray, events: list[dict], sidx: dict,
                     present: np.ndarray, tier_min: float,
                     n_max: int = SUP_N_MAX) -> dict:
    vals: list[int] = []
    for e in events:
        if e["tier"] < tier_min:
            continue
        j = sidx.get(e["symbol"])
        if j is None or not present[j]:
            continue
        s = int((e["start_ms"] - MAIN_START_MS) // HR)
        if s <= 0 or s >= int(N_HOURS):
            continue
        vals.append(int(rank[s - 1, j]))
    return {str(n): int(sum(1 for m in vals if m <= n)) for n in range(1, n_max + 1)}


def _daily_distinct(rank: np.ndarray, n: int) -> dict:
    Hn = rank.shape[0]
    cnts = []
    for h0 in range(0, Hn, 24):
        h1 = min(h0 + 23, Hn - 1)
        cnts.append(int((rank[h0:h1 + 1] <= n).any(axis=0).sum()))
    arr = np.asarray(cnts, dtype=float)
    return {"mean": round(float(arr.mean()), 4), "median": float(np.median(arr)),
            "p90": round(float(np.percentile(arr, 90)), 4), "max": int(arr.max()),
            "days": int(len(arr))}


def _pct_rank_rows(M: np.ndarray) -> np.ndarray:
    """按行做分位归一（0=最小，1=最大）；NaN 保持 NaN。"""
    out = np.full(M.shape, np.nan, dtype=np.float32)
    valid = np.isfinite(M)
    n = valid.sum(axis=1, keepdims=True)
    order = np.argsort(np.where(valid, M, np.inf), axis=1, kind="stable")
    rk = np.empty(order.shape, dtype=np.float32)
    rows = np.arange(M.shape[0])[:, None]
    rk[rows, order] = np.arange(M.shape[1], dtype=np.float32)[None, :]
    with np.errstate(invalid="ignore", divide="ignore"):
        out = np.where(valid, rk / np.maximum(n - 1, 1), np.nan).astype(np.float32)
    return out


def _build_labels(events, sidx, present) -> dict:
    S = len(sidx)
    out = {}
    for name, tmin in SUP_TIERS:
        E = np.zeros((int(N_HOURS), S), dtype=bool)
        n_in = 0
        for e in events:
            if e["tier"] < tmin:
                continue
            j = sidx.get(e["symbol"])
            if j is None or not present[j]:
                continue
            s = int((e["start_ms"] - MAIN_START_MS) // HR)
            if s < 0 or s >= int(N_HOURS):
                continue
            n_in += 1
            lo = max(0, s - 720); hi = min(int(N_HOURS) - 1, s - 1)
            if hi >= lo:
                E[lo:hi + 1, j] = True
        out[name] = (E, n_in)
    return out


# ===========================================================================
# C3：直接后果标签 —— 从「当时参考价」起的真实 30 天窗口
# ===========================================================================
#
# 旧标签 `_build_labels` 标的是「未来 30 天内出现**事后事件起点**」——那是"事后事件"
# 的代理，不是"从当前参考价起、随后 30 天涨到两倍"的直接后果。实测第 0 小时该标签
# 为真、而实际 30 天最高触及仅 1×。本段另立**直接后果标签**，二者分名分列：
#   · `outcome_*`  —— 直接后果（本段）：从决策小时当时的参考价起，真实未来 720h 最高触及
#   · `event_start_proxy` —— 旁证（`_build_labels`）：未来窗口内是否出现事后事件起点
# 未到期（主范围末端）、价格缺口、无参考价一律**不默认为负例**，另列 `data_state`。

def outcome_dir(lag_hours: int) -> Path:
    return OUTCOME5 / f"lag{int(lag_hours)}"


def _outcome_worker_r4(args: tuple) -> dict:
    """逐币算直接后果标签（C3）。参考价 = 决策小时上一根已收盘 K 线的 close。

    决策小时 h 用 `open_time = MAIN_START + (h−1)h` 的那根（与主评分 adopted 根一致）；
    未来窗口 = 该根之后的 720 根（h .. h+719）。
    """
    sym, lag_hours = args
    g = _guard()
    months = H.spot_months(sym)
    if not months:
        g.uninstall()
        return {"symbol": sym, "rows": 0, "network_blocked": g.blocked}
    c = H.read_spot(sym, months)
    if len(c) < 2:
        g.uninstall()
        return {"symbol": sym, "rows": 0, "network_blocked": g.blocked}
    import pandas as pd
    ts = c[:, 0].astype(np.int64)
    high = c[:, 2].astype(np.float64)
    close = c[:, 4].astype(np.float64)
    a = (ts - MAIN_START_MS) // HR                    # K 线开盘所在的绝对小时
    keep = (a >= 0) & (a < int(N_HOURS))
    if not keep.any():
        g.uninstall()
        return {"symbol": sym, "rows": 0, "network_blocked": g.blocked}
    n = int(N_HOURS)
    gh = np.full(n, np.nan, dtype=np.float64)         # 网格 high（缺口 = NaN）
    gc = np.full(n, np.nan, dtype=np.float64)
    gh[a[keep]] = high[keep]
    gc[a[keep]] = close[keep]

    hs = pd.Series(gh)
    Wn = OUTCOME_HORIZON_H
    # fm[i] = max(gh[i−Wn+1 .. i])（pandas 滚动窗是**后向**的）
    # 未来窗口 [a+1 .. a+Wn] 的最大值 = fm[a+Wn]
    fm = hs.rolling(Wn, min_periods=1).max().to_numpy()
    fc = hs.notna().rolling(Wn, min_periods=1).sum().to_numpy()
    future_max = np.concatenate([fm[Wn:], np.full(Wn, np.nan)])
    future_cnt = np.concatenate([fc[Wn:], np.full(Wn, np.nan)])

    have = ~np.isnan(gh)
    obs = np.flatnonzero(have)
    first_h, last_h = int(obs[0]), int(obs[-1])

    rows = []
    for h in range(first_h, last_h + 1):              # 决策小时 h 用 a = h−1 的参考价
        a_idx = h - 1
        ref = gc[a_idx] if a_idx >= 0 else np.nan
        if not np.isfinite(ref) or ref <= 0:
            state, verd, elapsed, touch = "no_reference", "no_reference", 0, np.nan
        else:
            win_end = a_idx + Wn
            cnt = future_cnt[a_idx]
            elapsed = int(cnt) if np.isfinite(cnt) else 0
            if win_end > min(n - 1, last_h):
                state = "partial_tail"
            elif elapsed < Wn:
                state = "gap"
            else:
                state = "complete"
            mx = future_max[a_idx]
            touch = float(mx / ref) if (np.isfinite(mx) and mx > 0) else np.nan
            if state != "complete" or not np.isfinite(touch):
                verd = "undetermined"
            elif touch >= 2.0:
                verd = "reach_2x"
            elif touch >= 1.5:
                verd = "reach_1_5x_only"
            else:
                verd = "below_1_5x"
        rows.append((h, ref, touch, OUTCOME_STATE_CODE[state],
                     OUTCOME_VERDICT_CODE[verd], elapsed))
    if not rows:
        g.uninstall()
        return {"symbol": sym, "rows": 0, "network_blocked": g.blocked}
    arr = np.asarray(rows, dtype=np.float64)
    od = outcome_dir(lag_hours)
    od.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        od / f"{sym}.npz",
        hour_idx=arr[:, 0].astype(np.int64),
        ref_close=arr[:, 1].astype(np.float32),
        max_touch=arr[:, 2].astype(np.float32),
        state=arr[:, 3].astype(np.int8),
        verdict=arr[:, 4].astype(np.int8),
        elapsed=arr[:, 5].astype(np.int16),
    )
    g.uninstall()
    return {"symbol": sym, "rows": int(len(rows)), "network_blocked": g.blocked}


def load_outcome_r4(lag_hours: int = 0) -> dict:
    """装配直接后果标签为 H×S 矩阵（C3）。"""
    syms = H.usdt_spot_symbols()
    S = len(syms)
    n = int(N_HOURS)
    elig = np.zeros((n, S), dtype=bool)               # 仅 complete：可判定的二值标签
    E2 = np.zeros((n, S), dtype=bool)                 # complete 且触及 2×
    E15 = np.zeros((n, S), dtype=bool)                # complete 且触及 1.5×
    state = np.full((n, S), OUTCOME_STATE_CODE["no_reference"], dtype=np.int8)
    verdict = np.full((n, S), OUTCOME_VERDICT_CODE["no_reference"], dtype=np.int8)
    elapsed = np.zeros((n, S), dtype=np.int16)
    touch = np.full((n, S), np.nan, dtype=np.float32)
    present = np.zeros(S, dtype=bool)
    od = outcome_dir(lag_hours)
    for j, s in enumerate(syms):
        p = od / f"{s}.npz"
        if not p.exists():
            continue
        with np.load(p) as z:
            hi = z["hour_idx"]
            state[hi, j] = z["state"]
            verdict[hi, j] = z["verdict"]
            elapsed[hi, j] = z["elapsed"]
            touch[hi, j] = z["max_touch"]
            comp = z["state"] == OUTCOME_STATE_CODE["complete"]
            elig[hi[comp], j] = True
            t = z["max_touch"][comp]
            E2[hi[comp], j] = t >= 2.0
            E15[hi[comp], j] = t >= 1.5
        present[j] = True
    return {"symbols": syms, "elig": elig, "E2": E2, "E15": E15, "state": state,
            "verdict": verdict, "elapsed": elapsed, "touch": touch, "present": present}


def cmd_outcome_r4(workers: int = 12, lag_hours: int = 0) -> dict:
    """C3：生成直接后果标签并给出**统一分母**下的排序评价。"""
    ensure_dirs()
    t0 = time.perf_counter()
    syms = H.usdt_spot_symbols()
    log(f"outcome: 开始，{len(syms)} 个对象，workers={workers}，lag={lag_hours}h")
    res = []
    done = 0
    with ProcessPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_outcome_worker_r4, (s, lag_hours)): s for s in syms}
        for f in as_completed(futs):
            res.append(f.result())
            done += 1
            if done % 100 == 0 or done == len(syms):
                log(f"outcome: {done}/{len(syms)}  已用 {time.perf_counter()-t0:.0f}s")

    O = load_outcome_r4(lag_hours)
    mat = load_matrices_r4(lag_hours, with_components=True)
    total, kline, deriv, trend = mat["total"], mat["kline"], mat["deriv"], mat["trend"]
    scoreable = total >= 0
    elig = O["elig"] & scoreable                      # 统一：可判定 ∧ 可评分

    st_cnt = {s: int((O["state"] == OUTCOME_STATE_CODE[s]).sum()) for s in OUTCOME_STATES}
    vd_cnt = {s: int((O["verdict"] == OUTCOME_VERDICT_CODE[s]).sum()) for s in OUTCOME_VERDICTS}
    hours_with_elig = int((elig.sum(axis=1) > 0).sum())
    summary = {
        "generated_at_utc": R.ms_to_iso(int(time.time() * 1000)),
        "note": ("C3 直接后果标签：从决策小时当时的参考价（上一根已收盘 K 线 close）起，"
                 "真实未来 720h 的最高触及 / 参考价。complete 才可判定；"
                 "主范围末端=partial_tail、价格缺口=gap、无参考价=no_reference 一律**不默认为负例**。"),
        "lag_hours": lag_hours,
        "horizon_hours": OUTCOME_HORIZON_H,
        "main_range": {"start": MAIN_START, "end": MAIN_END, "grid_hours": int(N_HOURS)},
        "label_kind": "direct_outcome",
        "is_proxy_label": False,
        "state_counts": st_cnt,
        "verdict_counts": vd_cnt,
        "state_definitions": {
            "complete": "参考价存在，且窗口 720h 全在数据范围内且无缺口 → 可判定",
            "partial_tail": "窗口末端超出主范围/数据末端 → 未到期，不作负例",
            "gap": "窗口内缺 K 线 → 观测不完整，不作负例",
            "no_reference": "决策小时上一根 K 线缺失 → 无参考价",
        },
        "cells_state_total": int(sum(st_cnt.values())),
        "cells_eligible": int(elig.sum()),
        "cells_ineligible_not_negative": int((O["state"] != OUTCOME_STATE_CODE["complete"]).sum()),
        "hours_with_eligible": hours_with_elig,
        "grid_hours": int(N_HOURS),
        "symbols": len(syms),
        "symbols_with_outcome": int(sum(1 for r in res if r["rows"])),
        "total_rows": int(sum(r["rows"] for r in res)),
        "elapsed_sec": round(time.perf_counter() - t0, 2),
    }
    (OUT5 / f"outcome_summary_lag{lag_hours}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")

    # --- 统一分母的评价：排序与分母都只在 eligible 上 ---
    base2 = _base_rate(O["E2"], elig)
    base15 = _base_rate(O["E15"], elig)
    rankings = {"score_total": total.astype(np.int16),
                "score_kline": kline.astype(np.int16),
                "score_deriv": deriv.astype(np.int16),
                "trend": trend.astype(np.int16)}
    # combo3（与 supplementary 同定义）
    d60 = _load_feature_matrix("dist60h", lag_hours, syms)
    r30 = _load_feature_matrix("ret30d", lag_hours, syms)
    bw = _load_feature_matrix("boxwidth", lag_hours, syms)
    combo3 = (-_pct_rank_rows(d60) - _pct_rank_rows(r30) + _pct_rank_rows(bw)) / 3.0
    del d60, r30, bw
    sc3 = np.isfinite(combo3)
    by_rank = {}
    for name, val in rankings.items():
        sc = (val >= 0) if name != "trend" else scoreable
        by_rank[name] = _rank_rows_r4(val, sc & elig)
    by_rank["combo3"] = _rank_rows_r4(np.nan_to_num(combo3, nan=0.0), sc3 & elig)

    by_ranking = {}
    for name, rk in by_rank.items():
        by_ranking[name] = {}
        for tname, E in (("reach_2x", O["E2"]), ("reach_1_5x", O["E15"])):
            b = _base_rate(E, elig)
            p = _precision_by_n(rk, E, elig)
            by_ranking[name][tname] = {
                "base": b, "precision": {str(k): v for k, v in p.items()},
                "lift": {str(k): (round(v / b, 6) if (v is not None and b) else None)
                         for k, v in p.items()}}

    # --- C2：时间切分按**真实结果窗口**分组，跨界项从探索统计中真正排除 ---
    hh = np.arange(int(N_HOURS))
    seg_masks = {
        "train_2022_2024": (hh < SPLIT_HOUR) & ((hh + OUTCOME_HORIZON_H) < SPLIT_HOUR),
        "review_2025_2026": hh >= SPLIT_HOUR,
    }
    cross_train_excluded = int((((hh < SPLIT_HOUR))
                                & ((hh + OUTCOME_HORIZON_H) >= SPLIT_HOUR)).sum())
    segments = {}
    for sname, mask in seg_masks.items():
        sm = mask[:, None] & elig
        segments[sname] = {"hours": int(mask.sum()), "eligible_cells": int(sm.sum()),
                           "rankings": {}}
        for name, rk in by_rank.items():
            segments[sname]["rankings"][name] = {}
            for tname, E in (("reach_2x", O["E2"]), ("reach_1_5x", O["E15"])):
                b = _base_rate(E, sm)
                p = _precision_by_n(rk, E, sm)
                segments[sname]["rankings"][name][tname] = {
                    "base": b, "precision": {str(k): v for k, v in p.items()}}

    lift_doc = {
        "generated_at_utc": R.ms_to_iso(int(time.time() * 1000)),
        "note": ("C3：**直接后果**标签（reach_2x / reach_1_5x）下的排序评价。"
                 "排序与分母统一：只在 `eligible`（complete 且可评分）格上排名，"
                 "分母 = Σ_h min(N, 该小时 eligible 数)，未到期/缺口/无参考价**不进分母**。"),
        "lag_hours": lag_hours, "horizon_hours": OUTCOME_HORIZON_H,
        "label_kind": "direct_outcome", "is_proxy_label": False,
        "base_rate": {"reach_2x": base2, "reach_1_5x": base15},
        "n_eligible_cells": int(elig.sum()),
        "hours_with_eligible": hours_with_elig,
        "by_ranking": by_ranking,
        "time_split": {
            "split_hour": SPLIT_HOUR, "review_start_utc": REVIEW_START,
            "cross_boundary_hours_excluded_from_train": cross_train_excluded,
            "note": ("训练段只保留结果窗口完全落在切点之前的决策小时；"
                     "窗口跨切点的小时**不进训练统计**（C2）。"),
            "segments": segments,
        },
        "elapsed_sec": round(time.perf_counter() - t0, 2),
    }
    (OUT5 / f"outcome_lift_lag{lag_hours}.json").write_text(
        json.dumps(lift_doc, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"outcome: eligible {int(elig.sum())} 格 / {hours_with_elig} 小时；"
        f"reach_2x 基准 {base2}；耗时 {summary['elapsed_sec']}s")
    return {"summary": summary, "lift": lift_doc, "elapsed_sec": summary["elapsed_sec"]}


def _label_definitions() -> dict:
    """C3：把补充研究用到的标签**明确命名**，防止把旁证标签当成直接后果。"""
    return {
        "event_start_proxy": {
            "kind": "proxy",
            "definition": ("决策小时起未来 30 天内**出现事后事件起点**（事件表由独立"
                           "`detect_events_r4` 事后判定）→ 真。"),
            "caveat": ("这是**旁证**：不等于「从当时参考价起随后 30 天涨到两倍」。"
                       "实测存在标签为真而 30 天最高触及仅 1× 的小时。"),
            "used_in": ["supplementary_decision_curve", "supplementary_ranking_lift",
                        "supplementary_feature_lift", "supplementary_time_split",
                        "supplementary_recall_daily"],
        },
        "direct_outcome_reach_2x": {
            "kind": "direct",
            "definition": ("从决策小时当时的参考价（上一根已收盘 K 线 close）起，真实未来"
                           "720h 最高触及 ≥ 2×。仅在 data_state=complete 时可判定。"),
            "caveat": "未到期/缺口/无参考价一律不判为负例，另列。",
            "used_in": ["outcome_lift"],
        },
        "direct_outcome_reach_1_5x": {
            "kind": "direct",
            "definition": "同上，阈值 1.5×。仅在 data_state=complete 时可判定。",
            "caveat": "同上。",
            "used_in": ["outcome_lift"],
        },
    }


def cmd_supplementary_r4(lag_hours: int = 0, workers: int = 12) -> dict:
    """R6：补充结果的**可复跑生成入口**（旧脚本已丢失，本函数是唯一真源）。"""
    ensure_dirs()
    t0 = time.perf_counter()
    mat = load_matrices_r4(lag_hours, with_components=True)
    syms = mat["symbols"]
    sidx = {s: j for j, s in enumerate(syms)}
    present = mat["present"]
    total = mat["total"]
    scoreable = total >= 0
    events = load_events_r4()
    labels = _build_labels(events, sidx, present)

    # 特征缓存（缺则自动生成）
    fd = features_dir(lag_hours)
    if len(list(fd.glob("*.npz"))) < len(syms):
        cmd_features_r4(workers, lag_hours)

    rankings = {
        "score_total": total.astype(np.int16),
        "score_kline": mat["kline"].astype(np.int16),
        "score_deriv": mat["deriv"].astype(np.int16),
        "trend": mat["trend"].astype(np.int16),
    }
    rank_cache: dict[str, np.ndarray] = {}
    for name, val in rankings.items():
        # score_kline / score_deriv 自带 -1 表示不可评分；trend 是 0/1，可评分范围同总分
        sc = (val >= 0) if name != "trend" else (total >= 0)
        rank_cache[name] = _rank_rows_r4(val, sc)

    n_inrange = {k: v[1] for k, v in labels.items()}
    E2 = labels["tier2"][0]

    # --- 决策曲线（score_total 排序，tier2 标签）---
    rk_score = rank_cache["score_total"]
    base2 = _base_rate(E2, scoreable)
    prec = _precision_by_n(rk_score, E2, scoreable)
    lift = {str(k): (round(v / base2, 6) if (v is not None and base2) else None)
            for k, v in prec.items()}
    dc = {
        "generated_at_utc": R.ms_to_iso(int(time.time() * 1000)),
        "note": ("r4 补充口径（可复跑）：按小时横截面 top-N；分母 = Σ_h min(N, 可评分对象数)。"
                 "由 r4 冻结分片 + r4 事件表派生。标签为**旁证**（event_start_proxy），"
                 "非直接后果；直接后果见 outcome_lift。"),
        "lag_hours": lag_hours,
        "label_kind": "event_start_proxy",
        "is_proxy_label": True,
        "label_definitions": _label_definitions(),
        "hours_used": int((scoreable.sum(axis=1) > 0).sum()),
        "base_rate_30d_event": base2,
        "precision_at_n": {str(k): v for k, v in prec.items()},
        "lift_at_n": lift,
        "recall_any_in_30d": _recall_any_30d(rk_score, events, sidx, present, 2.0),
        "recall_at_event_start_hour": _recall_at_start(rk_score, events, sidx, present, 2.0),
        "recall_any_in_30d_tier10": _recall_any_30d(rk_score, events, sidx, present, 10.0),
        "n_inrange_events": n_inrange,
        "n_tier10_inrange": n_inrange.get("tier10"),
        "selected_per_day_distinct": _daily_distinct(
            np.where(mat["selected"], np.int16(1), np.int16(32767)), 1) | {"days": int(N_HOURS) // 24},
        "scoreable_per_day_distinct": _daily_distinct(
            np.where(scoreable, np.int16(1), np.int16(32767)), 1),
        "elapsed_sec": round(time.perf_counter() - t0, 2),
    }
    (OUT5 / f"supplementary_decision_curve_lag{lag_hours}.json").write_text(
        json.dumps(dc, ensure_ascii=False, indent=1), encoding="utf-8")

    # --- 各排序的 lift + 召回 + 门槛表 ---
    by_ranking = {}
    for name, rk in rank_cache.items():
        by_ranking[name] = {}
        for tname, (E, _) in labels.items():
            b = _base_rate(E, scoreable)
            p = _precision_by_n(rk, E, scoreable)
            by_ranking[name][tname] = {
                "base": b, "precision": {str(k): v for k, v in p.items()},
                "lift": {str(k): (round(v / b, 6) if (v is not None and b) else None)
                         for k, v in p.items()}}
    threshold_table = []
    for th in range(2, 13):
        cells = int((total >= th).sum())
        if cells == 0:
            continue
        rec = {"threshold": th, "cells": cells,
               "pct_of_scoreable": round(cells / int(scoreable.sum()), 6),
               "precision": {}}
        for tname, (E, _) in labels.items():
            sub = total >= th
            rec["precision"][tname] = (round(float(E[sub].mean()), 8) if cells else None)
        threshold_table.append(rec)
    rl = {
        "generated_at_utc": R.ms_to_iso(int(time.time() * 1000)),
        "note": "r4 补充口径（可复跑）：各排序横截面命中率、召回与门槛表。标签为**旁证**。",
        "lag_hours": lag_hours,
        "label_kind": "event_start_proxy",
        "is_proxy_label": True,
        "label_definitions": _label_definitions(),
        "n_inrange_events": n_inrange,
        "by_ranking": by_ranking,
        "recall_any_30d_score_total": _recall_any_30d(rk_score, events, sidx, present, 2.0),
        "recall_at_start_score_total": _recall_at_start(rk_score, events, sidx, present, 2.0),
        "threshold_table": threshold_table,
        "elapsed_sec": round(time.perf_counter() - t0, 2),
    }
    (OUT5 / f"supplementary_ranking_lift_lag{lag_hours}.json").write_text(
        json.dumps(rl, ensure_ascii=False, indent=1), encoding="utf-8")

    # --- 单特征 lift ---
    feat_results: dict = {}
    feat_results["score_total"] = {
        t: {"base": by_ranking["score_total"][t]["base"],
            "precision": by_ranking["score_total"][t]["precision"]}
        for t in by_ranking["score_total"]}
    for name in SUP_FEATURES:
        M = _load_feature_matrix(name, lag_hours, syms)
        sc = np.isfinite(M)
        for tag, val in (("asc", -M), ("desc", M)):
            rk = _rank_rows_r4(np.nan_to_num(val, nan=0.0).astype(np.float32), sc)
            key = f"{name}_{tag}"
            feat_results[key] = {}
            for tname, (E, _) in labels.items():
                b = _base_rate(E, scoreable & sc)
                p = _precision_by_n(rk, E, scoreable & sc)
                feat_results[key][tname] = {
                    "base": b, "precision": {str(k): v for k, v in p.items()}}
        del M
    fl = {
        "generated_at_utc": R.ms_to_iso(int(time.time() * 1000)),
        "note": ("r4 补充口径（可复跑）：单特征横截面排序命中率（分母=Σ_h min(N, 可评分)）；"
                 "asc=取特征最小 N 个。特征为因果计算（含当根）。标签为**旁证**。"),
        "lag_hours": lag_hours,
        "label_kind": "event_start_proxy",
        "is_proxy_label": True,
        "label_definitions": _label_definitions(),
        "features": list(SUP_FEATURES),
        "results": feat_results,
        "elapsed_sec": round(time.perf_counter() - t0, 2),
    }
    (OUT5 / f"supplementary_feature_lift_lag{lag_hours}.json").write_text(
        json.dumps(fl, ensure_ascii=False, indent=1), encoding="utf-8")

    # --- 时间切分（C2：按真实结果窗口分组，跨界项真正排除）---
    # 标签窗口 = 决策小时后 720h（30 天）。训练段只保留窗口**完全落在切点之前**的小时；
    # 跨切点的小时不进训练统计（也不进复核统计，避免"用未来标签选规则"）。
    hh = np.arange(int(N_HOURS))
    cross_mask = (hh < SPLIT_HOUR) & ((hh + OUTCOME_HORIZON_H) >= SPLIT_HOUR)
    seg_masks = {
        "train_2022_2024": (hh < SPLIT_HOUR) & ((hh + OUTCOME_HORIZON_H) < SPLIT_HOUR),
        "review_2025_2026": hh >= SPLIT_HOUR,
    }
    segments = {}
    for sname, mask in seg_masks.items():
        sm = mask[:, None] & scoreable
        segments[sname] = {"hours": int(mask.sum()), "eligible_cells": int(sm.sum()),
                           "rankings": {}}
        for rname, rk in rank_cache.items():
            segments[sname]["rankings"][rname] = {}
            for tname, (E, _) in labels.items():
                b = _base_rate(E, sm)
                p = _precision_by_n(rk, E, sm)
                segments[sname]["rankings"][rname][tname] = {
                    "base": b, "precision": {str(k): v for k, v in p.items()}}
    # combo3 分段（dist60h 升、ret30d 升、boxwidth 降 等权分位平均）
    d60 = _load_feature_matrix("dist60h", lag_hours, syms)
    r30 = _load_feature_matrix("ret30d", lag_hours, syms)
    bw = _load_feature_matrix("boxwidth", lag_hours, syms)
    combo3 = (-_pct_rank_rows(d60) - _pct_rank_rows(r30) + _pct_rank_rows(bw)) / 3.0
    del d60, r30, bw
    sc3 = np.isfinite(combo3)
    rk3 = _rank_rows_r4(np.nan_to_num(combo3, nan=0.0), sc3)
    for sname, mask in seg_masks.items():
        sm = mask[:, None] & scoreable & sc3
        segments[sname]["rankings"]["combo3"] = {}
        for tname, (E, _) in labels.items():
            b = _base_rate(E, sm)
            p = _precision_by_n(rk3, E, sm)
            segments[sname]["rankings"]["combo3"][tname] = {
                "base": b, "precision": {str(k): v for k, v in p.items()}}
    ts = {
        "generated_at_utc": R.ms_to_iso(int(time.time() * 1000)),
        "note": ("r4 补充口径（可复跑）：探索段 2022-2024（训练）/ 后段 2025-2026（复核）；"
                 "combo3 = 等权分位平均(dist60h 越低越好, ret30d 越低越好, boxwidth 越宽越好)。"
                 "C2：训练段已按真实结果窗口排除跨切点小时。"),
        "lag_hours": lag_hours,
        "label_kind": "event_start_proxy",
        "is_proxy_label": True,
        "label_definitions": _label_definitions(),
        "detail": {"split_hour": SPLIT_HOUR,
                   "cross_boundary_hours_excluded_from_train": int(cross_mask.sum()),
                   "segments": segments},
        "elapsed_sec": round(time.perf_counter() - t0, 2),
    }
    (OUT5 / f"supplementary_time_split_lag{lag_hours}.json").write_text(
        json.dumps(ts, ensure_ascii=False, indent=1), encoding="utf-8")

    # --- 召回与每日去重名单规模 ---
    recall_sets = {"score_total": rk_score, "combo3": rk3}
    d60b = _load_feature_matrix("dist60h", lag_hours, syms)
    scb = np.isfinite(d60b)
    recall_sets["dist60h_asc"] = _rank_rows_r4(np.nan_to_num(-d60b, nan=0.0).astype(np.float32), scb)
    del d60b
    rd = {"generated_at_utc": R.ms_to_iso(int(time.time() * 1000)),
          "note": "r4 补充口径（可复跑）：新排序的召回与每日去重名单规模。标签为**旁证**。",
          "lag_hours": lag_hours, "label_kind": "event_start_proxy", "is_proxy_label": True,
          "recall": {}, "daily_distinct_topN": {}}
    for rname, rk in recall_sets.items():
        rd["recall"][rname] = {}
        for tname, tmin in SUP_TIERS:
            rd["recall"][rname][tname] = {
                "n": n_inrange[tname],
                "recall": _recall_any_30d(rk, events, sidx, present, tmin)}
    for rname in ("score_total", "combo3"):
        for n in SUP_DAILY_N:
            rd["daily_distinct_topN"][f"{rname}_top{n}"] = _daily_distinct(recall_sets[rname], n)
    rd["elapsed_sec"] = round(time.perf_counter() - t0, 2)
    (OUT5 / f"supplementary_recall_daily_lag{lag_hours}.json").write_text(
        json.dumps(rd, ensure_ascii=False, indent=1), encoding="utf-8")

    log(f"supplementary: 5 个产物已写出，耗时 {ts['elapsed_sec']}s")
    return {"decision_curve": dc, "ranking_lift": rl, "feature_lift": fl,
            "time_split": ts, "recall_daily": rd, "elapsed_sec": ts["elapsed_sec"]}


# ===========================================================================
# 新旧差异明细（r4 vs v3，只读对照）
# ===========================================================================

def _scoring_cell_diff(lag_hours: int = 0) -> dict:
    """C8：逐格对照 r4 与 v3 的评分 / 入选，给出**真实改分与改入选数量**。

    只统计两侧都存在且都可评分的重叠格；跨度不同的对象单独列出，不混进改分计数。
    """
    v3d = V3_OUT / "shards" / f"lag{int(lag_hours)}"
    r4d = shards_dir(lag_hours)
    n_both = n_score_changed = n_score_up = n_score_down = 0
    n_sel_changed = n_sel_gained = n_sel_lost = 0
    span_mismatch: list[dict] = []
    per_symbol: list[dict] = []
    for s in H.usdt_spot_symbols():
        p4 = r4d / f"{s}.npz"
        p3 = v3d / f"{s}.npz"
        if not p4.exists() or not p3.exists():
            continue
        with np.load(p4) as z4, np.load(p3) as z3:
            h4, n4 = int(z4["h_first"]), int(z4["n_hours"])
            h3, n3 = int(z3["h_first"]), int(z3["n_hours"])
            if (h4, n4) != (h3, n3):
                span_mismatch.append({"symbol": s, "r4": [h4, n4], "v3": [h3, n3]})
            lo, hi = max(h4, h3), min(h4 + n4, h3 + n3)
            if hi <= lo:
                continue
            a = np.arange(lo - h4, hi - h4)
            b = np.arange(lo - h3, hi - h3)
            s4 = z4["score_total"][a]
            s3 = z3["score_total"][b]
            e4 = z4["selected"][a]
            e3 = z3["selected"][b]
            both = (s4 >= 0) & (s3 >= 0)
            n_both += int(both.sum())
            d = both & (s4 != s3)
            n_score_changed += int(d.sum())
            n_score_up += int((d & (s4 > s3)).sum())
            n_score_down += int((d & (s4 < s3)).sum())
            sd = e4 != e3
            n_sel_changed += int(sd.sum())
            n_sel_gained += int((sd & e4).sum())
            n_sel_lost += int((sd & e3).sum())
            if sd.any():
                per_symbol.append({"symbol": s, "selected_changed": int(sd.sum()),
                                   "score_changed": int(d.sum())})
    return {
        "lag_hours": lag_hours,
        "cells_compared_both_scoreable": n_both,
        "score_changed_cells": n_score_changed,
        "score_changed_pct_of_compared": (round(n_score_changed / n_both, 6) if n_both else None),
        "score_increased_cells": n_score_up,
        "score_decreased_cells": n_score_down,
        "selected_changed_cells": n_sel_changed,
        "selected_gained_cells": n_sel_gained,
        "selected_lost_cells": n_sel_lost,
        "n_symbols_span_mismatch": len(span_mismatch),
        "span_mismatch_examples": span_mismatch[:20],
        "top_symbols_by_selection_change": sorted(
            per_symbol, key=lambda r: -r["selected_changed"])[:30],
        "note": ("这是**真实受影响数量**：逐格比较 r4 与 v3 分片。"
                 "F1/F2 只会**减少**分数，因此 `score_decreased_cells` 是评分修正的直接作用面；"
                 "`selected_changed_cells` 才是入选结果的变化面。"
                 "**不能**用「OI 观测不足被门控的格数」当受影响数——那是门控计数，不是改分计数。"),
    }


def cmd_diff(lag_hours: int = 0) -> dict:
    """逐项对照 r4 与 v3 的事件库 / 反向关联 / 主结果，给出差异去向与原因。"""
    ensure_dirs()
    ev4 = load_events_r4()
    v3_ev = []
    for p in sorted((V3_OUT / "events").glob("*.jsonl")):
        for ln in p.read_text(encoding="utf-8").splitlines():
            if ln.strip():
                v3_ev.append(json.loads(ln))

    def key(e):
        return (e["symbol"], int(e["start_ms"]))

    m4 = {key(e): e for e in ev4}
    m3 = {key(e): e for e in v3_ev}
    only4 = sorted(set(m4) - set(m3))
    only3 = sorted(set(m3) - set(m4))
    both = sorted(set(m4) & set(m3))
    tier_changed, peak_changed, mature_changed = [], [], []
    for k in both:
        a, b = m4[k], m3[k]
        if a["tier"] != b["tier"]:
            tier_changed.append({"symbol": k[0], "start_utc": R.ms_to_iso(k[1]),
                                 "v3_tier": b["tier"], "r4_tier": a["tier"]})
        if a.get("peak_ms") != b.get("peak_ms"):
            peak_changed.append({"symbol": k[0], "start_utc": R.ms_to_iso(k[1]),
                                 "v3_peak_utc": R.ms_to_iso(b["peak_ms"]) if b.get("peak_ms") else None,
                                 "r4_peak_utc": R.ms_to_iso(a["peak_ms"]) if a.get("peak_ms") else None})
        if bool(a.get("mature_30d")) != bool(b.get("mature_30d")):
            mature_changed.append({"symbol": k[0], "start_utc": R.ms_to_iso(k[1]),
                                   "v3_mature_30d": bool(b.get("mature_30d")),
                                   "r4_mature_30d": bool(a.get("mature_30d"))})
    t3 = Counter(str(e["tier"]) for e in v3_ev)
    t4 = Counter(str(e["tier"]) for e in ev4)

    # 主时段十倍事件的去向（v3 旧口径 vs r4 修正口径）
    def tenx(evs):
        out = []
        for e in evs:
            if e["tier"] >= 10.0:
                out.append({"symbol": e["symbol"], "start_utc": R.ms_to_iso(int(e["start_ms"])),
                            "tier": e["tier"], "max_m30": e.get("max_m30")})
        return out

    ten3 = tenx(v3_ev)
    ten4 = tenx(ev4)
    doc = {
        "generated_at_utc": R.ms_to_iso(int(time.time() * 1000)),
        "lag_hours": lag_hours,
        "events": {
            "v3_total": len(v3_ev), "r4_total": len(ev4),
            "kept_same_key": len(both), "r4_only": len(only4), "v3_only": len(only3),
            "by_tier_v3": dict(t3), "by_tier_r4": dict(t4),
            "r4_only_examples": [{"symbol": s, "start_utc": R.ms_to_iso(ms)}
                                 for s, ms in only4[:40]],
            "v3_only_examples": [{"symbol": s, "start_utc": R.ms_to_iso(ms)}
                                 for s, ms in only3[:40]],
            "tier_changed": tier_changed[:200], "n_tier_changed": len(tier_changed),
            "peak_changed": peak_changed[:200], "n_peak_changed": len(peak_changed),
            "mature_changed": mature_changed[:200], "n_mature_changed": len(mature_changed),
        },
        "tenx_events": {"v3": ten3, "r4": ten4,
                        "v3_n": len(ten3), "r4_n": len(ten4)},
        "scoring_cell_diff": _scoring_cell_diff(lag_hours),
        "reason": ("差异主因：① 7/30/90 天窗口与 48h 合波由「根数」改为**真实时刻**（R3）；"
                   "② `peak_ms` 由参考根改为真实未来高点；"
                   "③ 事件检测参考收盘与信号可用后开盘分开（R2）。"
                   "评分口径（F1/F2）不影响事件表本身，但影响入选与关联。"),
    }
    (OUT5 / "diff_vs_v3.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1),
                                          encoding="utf-8")
    # 可读版
    lines = [
        "# r4 vs v3 差异明细（只读对照）", "",
        f"- 事件总数：v3 {len(v3_ev)} → r4 {len(ev4)}",
        f"- 键相同（symbol+start）：{len(both)}；仅 r4 {len(only4)}；仅 v3 {len(only3)}",
        f"- 档位变化 {len(tier_changed)}；峰值时间变化 {len(peak_changed)}；"
        f"30 天成熟标记变化 {len(mature_changed)}",
        f"- 十倍事件：v3 {len(ten3)} → r4 {len(ten4)}", "",
    ]
    sc = doc["scoring_cell_diff"]
    lines += [
        "## 逐格评分/入选差异（C8：真实受影响数量）", "",
        f"- 两侧都可评分的重叠格：{sc['cells_compared_both_scoreable']:,}",
        f"- **改分格**：{sc['score_changed_cells']:,}"
        f"（占比较 {sc['score_changed_pct_of_compared']}）"
        f" → 升 {sc['score_increased_cells']:,} / 降 {sc['score_decreased_cells']:,}",
        f"- **改入选格**：{sc['selected_changed_cells']:,}"
        f" → 新增入选 {sc['selected_gained_cells']:,} / 取消入选 {sc['selected_lost_cells']:,}",
        f"- 跨度不一致对象：{sc['n_symbols_span_mismatch']}",
        "- 说明：F1/F2 只**减少**分数，`降` 即评分修正的直接作用面；"
        "**门控格数 ≠ 受影响数**。", "",
        "## r4 新增事件（前 40）", "",
    ]
    lines += [f"- {d['symbol']} {d['start_utc']}" for d in doc["events"]["r4_only_examples"]]
    lines += ["", "## v3 有、r4 无（前 40）", ""]
    lines += [f"- {d['symbol']} {d['start_utc']}" for d in doc["events"]["v3_only_examples"]]
    lines += ["", "## 十倍事件对照", "",
              "| 口径 | 数量 | 列表 |", "|---|---|---|",
              f"| v3 | {len(ten3)} | " + ", ".join(
                  f"{d['symbol']}@{d['start_utc']}" for d in ten3) + " |",
              f"| r4 | {len(ten4)} | " + ", ".join(
                  f"{d['symbol']}@{d['start_utc']}" for d in ten4) + " |"]
    (OUT5 / "diff_vs_v3.md").write_text("\n".join(lines), encoding="utf-8")
    log(f"diff: 事件 v3 {len(v3_ev)} → r4 {len(ev4)}；新增 {len(only4)}，消失 {len(only3)}；"
        f"十倍 {len(ten3)} → {len(ten4)}")
    return doc


# ===========================================================================
# CLI
# ===========================================================================

def main() -> int:
    ap = argparse.ArgumentParser(
        description="历史双向研究 · 有限返修 r4（离线，只写 reports/history_research_ds/r4/）")
    ap.add_argument("cmd", choices=["universe", "events", "baseline", "forward", "reverse",
                                    "attention", "retention", "supplementary", "features",
                                    "outcome", "integrity", "diff", "all"])
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--lag-hours", type=int, default=0)
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--limit", type=int, default=None, help="只跑前 N 个对象（冒烟）")
    ap.add_argument("--max-rank", type=int, default=None, help="关注曲线上限（默认全范围）")
    args = ap.parse_args()

    rep = verify_reuse()
    if not rep["match"]:
        log("警告：复用的 v3 模块哈希不符（已由环境变量放行），结果可能不可比。")

    guard = R.NetworkGuard()
    guard.install()
    try:
        if args.cmd == "universe":
            cmd_universe_r4()
        elif args.cmd == "events":
            cmd_events_r4(args.workers)
        elif args.cmd == "baseline":
            cmd_baseline_r4(args.workers, args.lag_hours, args.overwrite, args.limit)
        elif args.cmd == "forward":
            cmd_forward_r4(args.workers, args.lag_hours, args.limit)
        elif args.cmd == "reverse":
            cmd_reverse_r4(args.lag_hours)
        elif args.cmd == "attention":
            cmd_attention_r4(args.lag_hours, args.max_rank)
        elif args.cmd == "retention":
            cmd_retention_r4(args.lag_hours, args.max_rank)
        elif args.cmd == "supplementary":
            cmd_supplementary_r4(args.lag_hours, args.workers)
        elif args.cmd == "features":
            cmd_features_r4(args.workers, args.lag_hours)
        elif args.cmd == "outcome":
            cmd_outcome_r4(args.workers, args.lag_hours)
        elif args.cmd == "integrity":
            cmd_integrity_r4(args.lag_hours)
        elif args.cmd == "diff":
            cmd_diff(args.lag_hours)
        else:
            cmd_universe_r4()
            cmd_events_r4(args.workers)
            for lag in (0, 1):
                cmd_baseline_r4(args.workers, lag, args.overwrite, args.limit)
                cmd_forward_r4(args.workers, lag, args.limit)
                cmd_reverse_r4(lag)
                cmd_attention_r4(lag, args.max_rank)
                cmd_retention_r4(lag, args.max_rank)
                cmd_integrity_r4(lag)
            cmd_supplementary_r4(args.lag_hours, args.workers)
            cmd_outcome_r4(args.workers, args.lag_hours)
            cmd_diff(args.lag_hours)
    finally:
        guard.uninstall()
        log(f"网络外连拦截计数 = {guard.blocked}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
