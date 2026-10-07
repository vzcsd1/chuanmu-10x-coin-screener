"""观潮工作台薄适配层隔离测试：注入假 scan，不联网、不碰归档下载任务。

运行：py -3.10 ui\\test_ui.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as ui_app  # noqa: E402

PASSED = []
FAILED = []


def check(name: str, cond: bool, detail: str = ""):
    (PASSED if cond else FAILED).append(name)
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))


def wait_status(client, want, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = client.get("/api/state").get_json()
        if state["status"] == want:
            return state
        time.sleep(0.05)
    return client.get("/api/state").get_json()


def make_manager(scan_fn, tmp: Path, demo=False, demo_rows=None, demo_delay=0.01):
    return ui_app.ScanManager(
        scan_fn, tmp / "last_result.json", demo=demo,
        demo_rows=demo_rows, demo_delay=demo_delay, sleep=time.sleep)


def test_single_flight(tmp: Path):
    gate = threading.Event()
    calls = []

    def slow_scan():
        calls.append(1)
        gate.wait(timeout=5)
        return [{"symbol": "AAA/USDT", "score": 6, "max_score": 18}]

    client = ui_app.create_app(make_manager(slow_scan, tmp)).test_client()
    r1 = client.post("/api/scan")
    r2 = client.post("/api/scan")
    check("重复点击只触发一次扫描（409）", r1.status_code == 200 and r2.status_code == 409)
    gate.set()
    state = wait_status(client, "success")
    check("扫描只执行了一次", len(calls) == 1, f"calls={len(calls)}")
    check("成功状态带结果", state["rows"] and state["rows"][0]["symbol"] == "AAA/USDT")


def test_zero_result_saved(tmp: Path):
    client = ui_app.create_app(make_manager(lambda: [], tmp)).test_client()
    client.post("/api/scan")
    state = wait_status(client, "empty")
    check("零结果状态为 empty", state["status"] == "empty")
    saved = json.loads((tmp / "last_result.json").read_text(encoding="utf-8"))
    check("零结果正确保存", saved["status"] == "empty" and saved["rows"] == []
          and saved["count"] == 0)


def test_failure_keeps_last(tmp: Path):
    outcomes = [[{"symbol": "BBB/USDT", "score": 7, "max_score": 18}],
                RuntimeError("模拟网络异常")]

    def flaky():
        item = outcomes.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    client = ui_app.create_app(make_manager(flaky, tmp)).test_client()
    client.post("/api/scan")
    wait_status(client, "success")
    saved_before = (tmp / "last_result.json").read_text(encoding="utf-8")

    client.post("/api/scan")
    state = wait_status(client, "failed")
    check("失败后状态为 failed", state["status"] == "failed")
    check("失败信息如实呈现", "模拟网络异常" in (state["error"] or ""))
    check("失败保留上次结果", state["last"]["rows"][0]["symbol"] == "BBB/USDT")
    saved_after = (tmp / "last_result.json").read_text(encoding="utf-8")
    check("失败不覆盖上次成功文件", saved_before == saved_after)


def test_nan_becomes_null(tmp: Path):
    def dirty():
        return [{"symbol": "CCC/USDT", "score": 5, "max_score": 18,
                 "percentage_24h": float("nan"), "funding_rate": float("inf"),
                 "oi_chg_1d": 0.12, "market_cap": None}]

    client = ui_app.create_app(make_manager(dirty, tmp)).test_client()
    client.post("/api/scan")
    state = wait_status(client, "success")
    row = state["rows"][0]
    check("NaN 转为 null", row["percentage_24h"] is None)
    check("Infinity 转为 null", row["funding_rate"] is None)
    check("合法小数值保持正确", abs(row["oi_chg_1d"] - 0.12) < 1e-12)
    check("本身为 None 保持 None", row["market_cap"] is None)
    raw = (tmp / "last_result.json").read_text(encoding="utf-8")
    check("落盘 JSON 无 NaN 字面量", "NaN" not in raw and "Infinity" not in raw)


def test_refresh_restores_last(tmp: Path):
    client1 = ui_app.create_app(make_manager(
        lambda: [{"symbol": "DDD/USDT", "score": 9, "max_score": 18}], tmp)).test_client()
    client1.post("/api/scan")
    wait_status(client1, "success")

    # 模拟「刷新/重开」：新建 manager + app，从磁盘恢复
    client2 = ui_app.create_app(make_manager(lambda: [], tmp)).test_client()
    state = client2.get("/api/state").get_json()
    check("重开后状态回到 idle", state["status"] == "idle")
    check("重开后恢复上次结果", state["last"]["rows"][0]["symbol"] == "DDD/USDT")


def test_demo_isolation(tmp: Path):
    rows = [{"symbol": "DEMO/USDT", "score": 8, "max_score": 18}]
    client = ui_app.create_app(
        make_manager(None, tmp, demo=True, demo_rows=rows)).test_client()

    client.post("/api/scan")
    s1 = wait_status(client, "success")
    check("演示第 1 轮：有结果", s1["rows"][0]["symbol"] == "DEMO/USDT")
    check("演示模式标注 mode=demo", s1["mode"] == "demo")

    client.post("/api/scan")
    s2 = wait_status(client, "empty")
    check("演示第 2 轮：零结果", s2["status"] == "empty")

    client.post("/api/scan")
    s3 = wait_status(client, "failed")
    check("演示第 3 轮：失败", s3["status"] == "failed" and "演示" in (s3["error"] or ""))

    check("演示不写真实结果文件", not (tmp / "last_result.json").exists())
    check("演示状态 last 始终为空", s3["last"] is None)


def test_cross_site_rejected(tmp: Path):
    client = ui_app.create_app(make_manager(lambda: [], tmp)).test_client()
    res = client.post("/api/scan", headers={"Origin": "http://evil.example.com"})
    check("跨站 POST 被拒绝（403）", res.status_code == 403)
    res2 = client.post("/api/scan", headers={"Origin": "http://127.0.0.1:8765"})
    check("同源 POST 放行", res2.status_code == 200)
    wait_status(client2 := client, "empty")


def test_sanitize_numpy_like():
    class FakeNumpy:
        def __init__(self, v): self._v = v
        def item(self): return self._v

    out = ui_app.sanitize({"a": FakeNumpy(1.5), "b": [FakeNumpy(float("nan"))]})
    check("numpy 风格标量被还原", out["a"] == 1.5 and out["b"] == [None])


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        for i, fn in enumerate((test_single_flight, test_zero_result_saved,
                                test_failure_keeps_last, test_nan_becomes_null,
                                test_refresh_restores_last, test_demo_isolation,
                                test_cross_site_rejected)):
            tmp = base / f"case{i}"
            tmp.mkdir()
            fn(tmp)
    test_sanitize_numpy_like()
    print(f"\n{len(PASSED)} 项通过，{len(FAILED)} 项失败")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
