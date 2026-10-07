"""观潮 · 个人选币工作台 —— Flask 本地薄适配层。

只做一件事：把现有 `川沐十倍币筛选.scan(cfg)` 包成一个本地网页入口。
不复制评分、排序或筛选实现；不改策略；只绑定 127.0.0.1。

运行：
    py -3.10 ui/app.py            # 真实模式
    set CHUANMU_UI_DEMO=1 && py -3.10 ui/app.py   # 演示模式（内置样例，不写真实结果）
"""
from __future__ import annotations

import importlib.util
import json
import logging
import math
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, abort, jsonify, request, send_from_directory

PROJECT_ROOT = Path(__file__).resolve().parent.parent
UI_DIR = Path(__file__).resolve().parent
RESULT_DIR = PROJECT_ROOT / "results" / "ui"
LAST_RESULT_PATH = RESULT_DIR / "last_result.json"
DEMO_FIXTURE_PATH = UI_DIR / "fixtures" / "demo_rows.json"
PORT = int(os.getenv("CHUANMU_UI_PORT", "8765"))

sys.path.insert(0, str(PROJECT_ROOT))  # 让 binance_box_strategy 可导入

_base = None  # binance_box_strategy 模块（惰性加载）
_scan_mod = None


def _load_base():
    global _base
    if _base is None:
        import binance_box_strategy as base  # noqa: PLC0415
        _base = base
    return _base


def _load_scan_module():
    """按明确路径载入 川沐十倍币筛选.py，避免执行 __main__。"""
    global _scan_mod
    if _scan_mod is None:
        target = PROJECT_ROOT / "川沐十倍币筛选.py"
        spec = importlib.util.spec_from_file_location("chuanmu_scan", target)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _scan_mod = mod
    return _scan_mod


def real_scan():
    """直接调用原 scan(base.env_config())，不复制任何筛选逻辑。"""
    base = _load_base()
    mod = _load_scan_module()
    return mod.scan(base.env_config())


def sanitize(value):
    """NaN/Infinity → None；numpy/pandas 标量 → 原生类型；其余原样保留。"""
    if isinstance(value, dict):
        return {str(k): sanitize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(v) for v in value]
    if isinstance(value, bool) or value is None or isinstance(value, (int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return sanitize(value.item())
        except Exception:  # noqa: BLE001
            return str(value)
    return str(value)


def now_ms() -> int:
    return int(time.time() * 1000)


def load_terms() -> dict:
    """从 glossary/terms.yaml 抽取 UI 用到的术语大白话（单一真源，不另造解释）。

    复用项目自己的 tools/glossary.py 解析器；失败时返回空表，前端自动隐藏提示。
    """
    wanted = ["min_score", "extra_score", "oi_chg_1d", "oi_chg_3d", "ls_top",
              "funding_rate", "market_cap", "quote_volume", "c_trend", "box",
              "deriv_status", "oi"]
    try:
        spec = importlib.util.spec_from_file_location(
            "project_glossary", PROJECT_ROOT / "tools" / "glossary.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        data = mod.load(PROJECT_ROOT / "glossary" / "terms.yaml")
        out = {}
        for term in data.get("terms") or []:
            tid = term.get("id")
            if tid in wanted and term.get("zh") and term.get("plain"):
                out[tid] = {"zh": str(term["zh"]), "plain": str(term["plain"])}
        return out
    except Exception:  # noqa: BLE001
        logging.warning("术语表读取失败，界面将不显示指标解释", exc_info=True)
        return {}


class ScanManager:
    """单任务状态机：idle / running / success / empty / failed。

    - 只允许一个扫描任务（锁互斥，多点/刷新不会产生重复请求）
    - 成功（含零结果）才写入 last_result；失败绝不覆盖上次成功
    - 演示模式不写盘、不读盘，与真实结果完全隔离
    """

    def __init__(self, scan_fn, result_path: Path | None, demo: bool = False,
                 demo_rows: list | None = None, demo_delay: float = 2.2,
                 sleep=time.sleep):
        self._scan_fn = scan_fn
        self._result_path = result_path
        self._demo = demo
        self._demo_rows = demo_rows or []
        self._demo_delay = demo_delay
        self._sleep = sleep
        self._demo_step = 0
        self._lock = threading.Lock()
        self.status = "idle"
        self.started_at: int | None = None
        self.finished_at: int | None = None
        self.error: str | None = None
        self.rows: list | None = None
        self.last = None if demo else self._load_last()

    def _load_last(self):
        try:
            if self._result_path and self._result_path.exists():
                data = json.loads(self._result_path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("rows"), list):
                    return data
        except Exception:  # noqa: BLE001
            logging.warning("上次结果文件读取失败，按无历史处理", exc_info=True)
        return None

    def _save_last(self, payload: dict) -> None:
        if self._demo or self._result_path is None:
            return
        try:
            self._result_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._result_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            tmp.replace(self._result_path)
        except Exception:  # noqa: BLE001
            logging.warning("结果保存失败（不影响本次展示）", exc_info=True)

    def start(self) -> bool:
        with self._lock:
            if self.status == "running":
                return False
            self.status = "running"
            self.started_at = now_ms()
            self.finished_at = None
            self.error = None
        threading.Thread(target=self._run, daemon=True, name="chuanmu-scan").start()
        return True

    def _demo_scan(self):
        """演示夹具：依次呈现 有结果 → 零结果 → 查询失败，循环往复。"""
        self._sleep(self._demo_delay)
        step = self._demo_step % 3
        self._demo_step += 1
        if step == 0:
            return self._demo_rows
        if step == 1:
            return []
        raise RuntimeError("演示：模拟的一次查询失败（内置样例，非真实错误）")

    def _run(self) -> None:
        try:
            raw = self._demo_scan() if self._demo else self._scan_fn()
            rows = sanitize(raw if isinstance(raw, list) else [])
            finished = now_ms()
            payload = {
                "version": 1,
                "status": "success" if rows else "empty",
                "started_at": self.started_at,
                "finished_at": finished,
                "count": len(rows),
                "rows": rows,
            }
            self._save_last(payload)
            with self._lock:
                self.status = payload["status"]
                self.rows = rows
                self.finished_at = finished
                self.error = None
                if not self._demo:
                    self.last = payload
        except Exception as exc:  # noqa: BLE001 —— 失败要如实呈现，不能吞
            base = _load_base()
            if isinstance(exc, getattr(base, "DomainPaused", Exception)):
                message = f"本轮未执行：{exc}"
            else:
                logging.exception("本轮扫描失败")
                message = f"{type(exc).__name__}: {exc}"
            with self._lock:
                self.status = "failed"
                self.finished_at = now_ms()
                self.error = message
                self.rows = None

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "mode": "demo" if self._demo else "real",
                "status": self.status,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "server_now": now_ms(),
                "error": self.error,
                "rows": self.rows,
                "last": self.last,
                "min_score": current_min_score(),
                "paused": read_pauses(),
            }


_cfg_cache: dict = {}


def current_min_score() -> int | None:
    try:
        if "min_score" not in _cfg_cache:
            _cfg_cache["min_score"] = _load_base().env_config().min_score
        return _cfg_cache["min_score"]
    except Exception:  # noqa: BLE001
        return None


def read_pauses() -> list:
    """只读展示原后端的限流暂停状态；读不到就当没有，不虚构。"""
    try:
        base = _load_base()
        cfg = base.env_config()
        state = base.RateLimitState.load(base.default_state_path(cfg))
        now = base._now_ms()
        out = []
        for key, item in state.domains.items():
            until = int(item.get("banned_until_ms") or 0)
            if until and now < until:
                out.append({
                    "domain": key.split("|")[0],
                    "until_ms": until,
                    "reason": str(item.get("reason") or "")[:200],
                })
        return out
    except Exception:  # noqa: BLE001
        return []


def _same_local_origin() -> bool:
    """只放行明确来自本机页面的请求（白名单，不靠 Host 相等，防 DNS 重绑定）。"""
    origin = request.headers.get("Origin") or request.headers.get("Referer")
    if not origin:
        return True  # 非浏览器客户端不带 Origin；绑定 127.0.0.1 已是边界
    netloc = urlparse(origin).netloc.lower()
    return netloc in {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}


def create_app(manager: ScanManager, terms: dict | None = None) -> Flask:
    app = Flask(__name__, static_folder=str(UI_DIR / "static"),
                static_url_path="/static")
    app.config["JSON_AS_ASCII"] = False

    @app.get("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    @app.get("/api/state")
    def api_state():
        return jsonify(manager.snapshot())

    @app.get("/api/terms")
    def api_terms():
        return jsonify(terms or {})

    @app.post("/api/scan")
    def api_scan():
        if not _same_local_origin():
            abort(403)
        if not manager.start():
            return jsonify({"ok": False, "error": "running"}), 409
        return jsonify({"ok": True, "started_at": manager.started_at})

    return app


def build_manager(demo: bool) -> ScanManager:
    if demo:
        rows = json.loads(DEMO_FIXTURE_PATH.read_text(encoding="utf-8"))
        return ScanManager(None, None, demo=True, demo_rows=rows)
    return ScanManager(real_scan, LAST_RESULT_PATH)


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    demo = (os.getenv("CHUANMU_UI_DEMO") or "").strip().lower() in ("1", "true", "yes")
    manager = build_manager(demo)
    app = create_app(manager, terms=load_terms())
    url = f"http://127.0.0.1:{PORT}"
    print("观潮 · 个人选币工作台")
    print(f"模式: {'演示（内置样例，不写真实结果）' if demo else '真实（调用现有筛选器）'}")
    print(f"地址: {url}   停止: 在本窗口按 Ctrl+C")
    app.run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False,
            threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
