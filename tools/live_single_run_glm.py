"""Live single-run acceptance runner (glm, 2026-10-08).

Thin wrapper for tasks/glm_live_single_run.md. Rules honored here:
- Calls ui.app.real_scan() exactly ONCE (same entry as the web page).
- No warmup/ping requests, no retry, no loop, no second scan.
- Never modifies production code; only observes.
- A read-only observer wraps binance_box_strategy.public_selection so the FULL
  ranked table (before min_score filtering) is saved; the original function is
  called exactly once and its return value is returned unchanged.
- All artifacts go to reports/live_single_run_glm/run_<ts>/ only.
- On DomainPaused or any error: save evidence and stop (no fallback scan).

Usage:
    py -3.10 -u tools/live_single_run_glm.py
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BJ = timezone(timedelta(hours=8))

STRATEGY_ENV_VARS = [
    "BINANCE_TIMEFRAME", "LOOKBACK", "BOX_PERIOD", "MIN_QUOTE_VOLUME",
    "MAX_QUOTE_VOLUME", "MAX_MARKET_CAP", "MAX_SYMBOLS", "VOLUME_MULTIPLIER",
    "EMA_PERIOD", "ATR_PERIOD", "MIN_SCORE", "USE_COINGECKO",
    "REQUEST_TIMEOUT_MS", "REQUIRE_OKX", "REQUIRE_FUTURES", "DERIV_CACHE",
    "RATE_LIMIT_STATE_FILE", "CSV_DIR", "HTTP_PROXY", "HTTPS_PROXY",
]

RUN_TAG = datetime.now(BJ).strftime("run_%Y%m%d_%H%M%S")
RUN_DIR = ROOT / "reports" / "live_single_run_glm" / RUN_TAG

log = logging.getLogger("live_single_run")


def bjnow() -> datetime:
    return datetime.now(BJ)


def bj_str(ms: int | None) -> str | None:
    if not ms:
        return None
    return datetime.fromtimestamp(int(ms) / 1000, BJ).isoformat(timespec="seconds")


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def mask_proxy(url: str | None) -> str | None:
    if not url:
        return url
    return re.sub(r"(//)[^@/]+@", r"\1***@", url)


def cfg_snap_proxy_only(cfg) -> dict:
    return {"proxy_masked": mask_proxy(cfg.proxy)}


def dump_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1),
                    encoding="utf-8")


def setup_logging() -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(RUN_DIR / "run.log", encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def read_rate_state() -> dict | None:
    p = ROOT / "results" / "rate_limit_state.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"_raw_file": str(p)}


def annotate_pause(state) -> dict | None:
    if not isinstance(state, dict):
        return state
    out = json.loads(json.dumps(state))  # deep copy
    domains = out.get("domains")
    if isinstance(domains, dict):
        for key, item in domains.items():
            if isinstance(item, dict) and item.get("banned_until_ms"):
                item["banned_until_bj"] = bj_str(int(item["banned_until_ms"]))
    return out


def main() -> int:
    setup_logging()
    t0_wall = time.monotonic()
    t0 = bjnow()
    log.info("=== live single run start: %s (Beijing) ===", t0.isoformat())

    # --- code version snapshot -------------------------------------------------
    files = ["binance_box_strategy.py", "川沐十倍币筛选.py", "ui/app.py",
             "ui/static/app.js", "tools/live_single_run_glm.py"]
    versions = {f: sha256_of(ROOT / f) for f in files}
    for f, h in versions.items():
        log.info("sha256 %s = %s", f, h)

    # --- config snapshot (non-sensitive) ---------------------------------------
    import binance_box_strategy as base  # noqa: PLC0415
    cfg = base.env_config()

    # --- pre-start proxy guard: mismatch => exit BEFORE any scan call ----------
    # resolve_proxy() order: BINANCE_NO_PROXY > BINANCE_PROXY > HTTPS_PROXY >
    # https_proxy > HTTP_PROXY > http_proxy > ALL_PROXY > all_proxy > registry.
    # The authoritative value is what env_config().proxy actually resolved to.
    env_snapshot = {
        k: mask_proxy(os.getenv(k)) for k in
        ("BINANCE_NO_PROXY", "BINANCE_PROXY", "HTTPS_PROXY", "https_proxy",
         "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")
        if os.getenv(k) is not None
    }
    expected_proxy = (os.getenv("EXPECTED_PROXY") or "").strip()
    proxy_ok = (not expected_proxy) or (cfg.proxy == expected_proxy)
    log.info("proxy precheck: resolved=%s expected=%s ok=%s env=%s",
             mask_proxy(cfg.proxy), mask_proxy(expected_proxy or None),
             proxy_ok, json.dumps(env_snapshot, ensure_ascii=False))
    if not proxy_ok:
        meta_fail = {
            "status": "precheck_failed_proxy",
            "error": ("resolved proxy %r != expected %r; aborting BEFORE any "
                      "scan call, zero requests attempted"
                      % (mask_proxy(cfg.proxy), mask_proxy(expected_proxy))),
            "started_bj": t0.isoformat(timespec="seconds"),
            "finished_bj": bjnow().isoformat(timespec="seconds"),
            "code_sha256": versions,
            "config": cfg_snap_proxy_only(cfg),
            "env_snapshot_masked": env_snapshot,
        }
        dump_json(RUN_DIR / "meta.json", meta_fail)
        log.error("precheck failed: %s", meta_fail["error"])
        return 2

    env_set = {k: os.getenv(k) for k in STRATEGY_ENV_VARS if os.getenv(k)}
    cfg_snap = {
        "timeframe": cfg.timeframe, "lookback": cfg.lookback,
        "box_period": cfg.box_period,
        "min_quote_volume": cfg.min_quote_volume,
        "max_quote_volume": cfg.max_quote_volume,
        "max_market_cap": cfg.max_market_cap,
        "max_symbols": cfg.max_symbols,
        "min_score": cfg.min_score,
        "use_coingecko": cfg.use_coingecko,
        "require_okx": cfg.require_okx,
        "require_futures": cfg.require_futures,
        "deriv_cache": cfg.deriv_cache,
        "timeout_ms": cfg.timeout_ms,
        "proxy_masked": mask_proxy(cfg.proxy),
        "env_snapshot_masked": env_snapshot,
        "strategy_env_vars_set": env_set,
        "pool_rule": ("USDT spot pairs, volume gate [min,max], market-cap gate, "
                      "stablecoins excluded; NO pool shrink this round"),
    }
    log.info("config: %s", json.dumps(cfg_snap, ensure_ascii=False))

    state_before = annotate_pause(read_rate_state())
    dump_json(RUN_DIR / "rate_limit_before.json", state_before)
    log.info("rate_limit_state before: %s",
             json.dumps(state_before, ensure_ascii=False))

    # --- read-only observer around public_selection -----------------------------
    # Saves the FULL ranked rows (before min_score filter). The original
    # function is called exactly once; behavior/retries/throttling untouched.
    captured: dict = {}
    orig_public_selection = base.public_selection

    def observing_public_selection(*args, **kwargs):
        result = orig_public_selection(*args, **kwargs)
        captured["ranked"] = result
        captured["at"] = bjnow().isoformat(timespec="seconds")
        log.info("observer: public_selection returned %s ranked rows",
                 len(result) if isinstance(result, list) else "non-list")
        return result

    base.public_selection = observing_public_selection

    # --- the ONE real scan via the web entry function ---------------------------
    import ui.app as uiapp  # noqa: PLC0415  (flask import only, server not started)

    status = "unknown"
    error_text = None
    result_payload = None
    try:
        raw = uiapp.real_scan()  # called ONCE
        result_payload = {
            "rows": uiapp.sanitize(raw.get("rows")),
            "round": uiapp.sanitize(raw.get("round")),
        }
        status = "completed"
    except Exception as exc:  # noqa: BLE001  (DomainPaused or any failure)
        if isinstance(exc, getattr(base, "DomainPaused", Exception)):
            status = "paused"
        else:
            status = "error"
        error_text = f"{type(exc).__name__}: {exc}"
        log.error("scan aborted: %s", error_text)
        log.error(traceback.format_exc())

    t1 = bjnow()
    duration_s = round(time.monotonic() - t0_wall, 1)

    # --- save artifacts ----------------------------------------------------------
    dump_json(RUN_DIR / "meta.json", {
        "status": status,
        "error": error_text,
        "started_bj": t0.isoformat(timespec="seconds"),
        "finished_bj": t1.isoformat(timespec="seconds"),
        "duration_seconds": duration_s,
        "code_sha256": versions,
        "config": cfg_snap,
        "runner": "tools/live_single_run_glm.py",
        "entry": "ui.app.real_scan() called exactly once (same function the "
                 "web /api/scan path uses; no browser needed)",
    })

    if result_payload is not None:
        dump_json(RUN_DIR / "full_result.json", result_payload)
    if "ranked" in captured:
        dump_json(RUN_DIR / "ranked_full.json", {
            "captured_at_bj": captured["at"],
            "count": len(captured["ranked"]),
            "rows": uiapp.sanitize(captured["ranked"]),
        })
    state_after = annotate_pause(read_rate_state())
    dump_json(RUN_DIR / "rate_limit_after.json", state_after)
    log.info("rate_limit_state after: %s",
             json.dumps(state_after, ensure_ascii=False))
    log.info("=== finished: status=%s duration=%ss ===", status, duration_s)
    return 0 if status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
