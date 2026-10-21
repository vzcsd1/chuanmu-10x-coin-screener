#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""前向采集器（M12 / M13 / M14 的原料，2026-10-21 立）

为什么要前向采集
----------------
有些数据**只能从今天开始攒**——历史买不到、也没有归档。晚一天就永久少一天。
但在动手之前先做了一轮归档核查，结论和脑暴文档的原判断不一样：

| 机制 | 原判断 | 实测（2026-10-21） | 结论 |
|---|---|---|---|
| M12 盘口 ±5% 厚度不对称 | 「只能从今天开始抓，历史买不到」 | `futures/um/daily/bookDepth` **仍在每日发布**：30 秒一个快照、±1~5% 十个分带、带 depth 与 notional；454/691 个 universe 币有 | **判断错，可回补**（用 `tools/backfill_bookdepth_ds.py`），**不需要前向** |
| M13 做市商库存 / 价差骤缩 | 只能前向 | `bookTicker` 归档**在 2024-03-30 就停更了**（月包到 2024-04） | **必须前向**。REST `/fapi/v1/ticker/bookTicker` 一次拿全市场，便宜 |
| M14 清算热力图 | 免费源不可靠 | `liquidationSnapshot` 归档为空；`forceOrder` 只有 WS | **必须前向**。但本机 fstream WS 收不到推送帧（见下） |

本机网络的两个硬事实（都实测过，别再试第二遍）
----------------------------------------------
1. `fapi.binance.com` **直连不通**，必须走系统代理（Clash 的 `127.0.0.1:7897`，是混合端口，
   HTTP 与 SOCKS5 都支持）。环境变量里的 `HTTPS_PROXY=127.0.0.1:9390` 是沙箱内部代理，对公网是死路。
2. **futures WebSocket（`fstream.binance.com`）只回订阅回执、不推市场数据**：
   `{"result":null,"id":1}` 能收到，之后 `btcusdt@aggTrade` 30 秒 0 条、`!miniTicker@arr` 15 秒 0 条。
   作为对照，spot WS（`stream.binance.com:9443`）同样配置下 10 秒收 15 条。
   换 HTTP 代理 / SOCKS5、换 443 / 9443、换 `websocket-client` 与 `aiohttp` 两个客户端，结果都一样。
   → **所以清算流在本机采不到**。`liquidations` 这条 lane 保留、带预检自动降级，
   换一个代理节点或换网络后会自动开始工作，不需要改代码。

三条 lane
---------
- `bookticker`（M13）：每 `--bt-interval` 秒一次 `/fapi/v1/ticker/bookTicker`（无 symbol，一次返回全市场
  798 个合约，约 120KB，weight 5）。记 bid/ask 价与量，用来算价差与两侧挂单不平衡。
- `oi5m`（M14 的估算输入）：每 `--oi-interval` 秒对每个合约取最近 12 根 5 分钟 OI。
  **为什么必须自己存**：`/futures/data/openInterestHist` 只保留最近 30 天，
  而日度归档 `metrics` 只有日频。想要 5 分钟粒度的历史 OI，只能自己攒。
- `liquidations`（M14 的真实印痕）：WS `!forceOrder@arr`。带 90 秒预检，收不到数据就降级 30 分钟再试。

存储与健康
----------
写 `data/forward/<lane>/YYYY-MM-DD.jsonl.gz`（按 UTC 日期切，追加 + 定期 flush，崩溃最多丢一个 flush 周期）。
**为什么不用 parquet 直接追加**：parquet 不能安全地多批追加，而按小时写小文件会产生几千个碎片。
JSONL 追加是崩溃安全的，分析前跑一次 `compact` 转 parquet。

健康日志写 `data/forward/_health/YYYY-MM-DD.jsonl`，每轮一行。
**这条比数据本身更重要**：长期采集最典型的失败是「进程还活着但一条数据没写」，
所以 `status` 命令读的是健康日志与文件行数，不是进程是否存在。

用法
----
    py -3.10 tools/forward_collect_ds.py run                     # 三条 lane 全开
    py -3.10 tools/forward_collect_ds.py run --lanes bookticker   # 只采价差
    py -3.10 tools/forward_collect_ds.py once --lane bookticker   # 单轮冒烟
    py -3.10 tools/forward_collect_ds.py status                   # 今天采了多少
    py -3.10 tools/forward_collect_ds.py compact --day 2026-10-21 # JSONL → parquet
"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import os
import signal
import sys
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "forward"
HEALTH = OUT / "_health"

FAPI = "https://fapi.binance.com"
WS_FUTURES = "wss://fstream.binance.com/ws"

ALL_LANES = ("bookticker", "oi5m", "liquidations")

# `/futures/data/*` 的 IP 限额是 1000 请求 / 5 分钟。主策略也在用同一批端点，
# 所以这里只敢用 70%，留 30% 给实盘，避免采集把实盘挤死。
OI_BUDGET_PER_5MIN = 700


# ------------------------------------------------------------------ 基础设施


def proxy() -> str | None:
    """系统代理优先，其次环境变量。

    **顺序不能反**：某些沙箱会往环境里塞一个只对内部服务有效的 HTTPS_PROXY
    （本机实测 `127.0.0.1:9390`），对公网是死路。真正的出口在系统代理（Clash 7897）。
    `BINANCE_PROXY` 仍可强制覆盖，方便换节点。
    """
    forced = (os.getenv("BINANCE_PROXY") or "").strip()
    if forced:
        return forced
    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
            ) as key:
                if winreg.QueryValueEx(key, "ProxyEnable")[0]:
                    server = str(winreg.QueryValueEx(key, "ProxyServer")[0]).strip()
                    if server:
                        return server if "://" in server else f"http://{server}"
        except Exception:  # noqa: BLE001
            pass
    for name in ("HTTPS_PROXY", "https_proxy"):
        v = (os.getenv(name) or "").strip()
        if v and "9390" not in v:  # 9390 是沙箱内部代理，明确跳过
            return v
    return None


def today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def lane_path(lane: str, day: str | None = None) -> Path:
    d = OUT / lane
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{day or today_utc()}.jsonl.gz"


class Writer:
    """按 UTC 日期滚动的 JSONL.gz 追加写入器。

    只做一件事：把一批 dict 追加到当天文件并 flush。跨天自动换文件。
    gzip 用 'ab' 追加多个 member，`gzip.open` / pandas 都能正常读回。
    """

    def __init__(self, lane: str) -> None:
        self.lane = lane
        self._day = None
        self._fh = None
        self.total = 0

    def write(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        day = today_utc()
        if day != self._day:
            self.close()
            self._day = day
            self._fh = gzip.open(lane_path(self.lane, day), "ab", compresslevel=6)
        for r in rows:
            self._fh.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            self._fh.write(b"\n")
        self._fh.flush()
        self.total += len(rows)
        return len(rows)

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:  # noqa: BLE001
                pass
            self._fh = None


def health(rec: dict) -> None:
    HEALTH.mkdir(parents=True, exist_ok=True)
    rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **rec}
    with open(HEALTH / f"{today_utc()}.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


class Budget:
    """滑动 5 分钟窗口的调用计数，防止采集把实盘挤死。"""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.hits: deque[float] = deque()

    def _trim(self) -> None:
        cut = time.time() - 300
        while self.hits and self.hits[0] < cut:
            self.hits.popleft()

    def take(self) -> bool:
        self._trim()
        if len(self.hits) >= self.limit:
            return False
        self.hits.append(time.time())
        return True

    def used(self) -> int:
        self._trim()
        return len(self.hits)


# ------------------------------------------------------------------ lane: 价差


async def lane_bookticker(session: aiohttp.ClientSession, w: Writer, interval: float,
                          stop: asyncio.Event, px: str | None) -> None:
    """M13：全市场最优买卖价，每 interval 秒一次。

    一次调用拿全部合约（无 symbol 形式，weight 5）。798 个币约 120KB，
    所以 60 秒一次的成本是 5 weight/分钟、约 173MB/天流量。
    **粒度选择**：价差骤缩是分钟级现象，60 秒够用；要更细可以调小 interval，
    但流量与行数是线性增长的（798 行/轮）。
    """
    while not stop.is_set():
        t0 = time.time()
        try:
            async with session.get(f"{FAPI}/fapi/v1/ticker/bookTicker", proxy=px,
                                   timeout=aiohttp.ClientTimeout(total=30)) as r:
                r.raise_for_status()
                data = await r.json()
            ts = int(time.time() * 1000)
            rows = []
            for d in data:
                try:
                    bid = float(d["bidPrice"])
                    ask = float(d["askPrice"])
                except (KeyError, TypeError, ValueError):
                    continue  # 停牌/交割中的合约会给 0 或空串，跳过不污染
                if bid <= 0 or ask <= 0 or ask < bid:
                    continue
                rows.append({
                    "symbol": d["symbol"], "ts": ts,
                    "bid": bid, "ask": ask,
                    "bid_qty": float(d["bidQty"] or 0), "ask_qty": float(d["askQty"] or 0),
                })
            n = w.write(rows)
            health({"lane": "bookticker", "ok": True, "n": n,
                    "ms": int((time.time() - t0) * 1000)})
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            health({"lane": "bookticker", "ok": False,
                    "err": f"{type(e).__name__}: {e}"[:200],
                    "ms": int((time.time() - t0) * 1000)})
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


# ------------------------------------------------------------------ lane: OI 5m


async def _oi_one(session: aiohttp.ClientSession, sym: str, limit: int,
                  px: str | None) -> list[dict]:
    url = f"{FAPI}/futures/data/openInterestHist"
    async with session.get(url, params={"symbol": sym, "period": "5m", "limit": limit},
                           proxy=px, timeout=aiohttp.ClientTimeout(total=25)) as r:
        if r.status != 200:
            return []
        data = await r.json()
    out = []
    for d in data or []:
        try:
            out.append({"symbol": sym, "ts": int(d["timestamp"]),
                        "oi": float(d["sumOpenInterest"]),
                        "oi_val": float(d["sumOpenInterestValue"])})
        except (KeyError, TypeError, ValueError):
            continue
    return out


async def lane_oi5m(session: aiohttp.ClientSession, w: Writer, symbols: list[str],
                    interval: float, limit: int, concurrency: int,
                    stop: asyncio.Event, px: str | None) -> None:
    """M14 的估算输入：5 分钟粒度 OI。

    **两个设计点，都不是随便定的：**

    ① **匀速发，不打包成一批。** 798 个合约如果一次性打出去，5 分钟窗口内就吃掉 798 次调用，
    超过给自己定的 700 上限，于是每轮都会静默漏掉最后 ~100 个币。
    所以按 `interval × 0.8 / 币数` 的节奏逐个发（默认约 1.8 秒一个，一轮约 24 分钟），
    5 分钟只用掉约 167 次，给实盘留足余量。

    ② **内存去重。** 每轮取最近 12 根（1 小时），相邻两轮必然重叠，
    不去重的话同一根会被写十几遍。用 `seen` 集合挡掉，超过 3 小时的就清掉。
    **重启后 `seen` 是空的，第一个小时会有重复**——`compact` 会按 (symbol, ts) 去重。
    """
    budget = Budget(OI_BUDGET_PER_5MIN)
    sem = asyncio.Semaphore(concurrency)
    seen: set[tuple[str, int]] = set()
    written = 0

    while not stop.is_set():
        t0 = time.time()
        gap = max(0.02, (interval * 0.8) / max(len(symbols), 1))
        pending: list[dict] = []
        written = 0
        done = 0

        async def one(sym: str) -> None:
            nonlocal written, done
            if not budget.take():
                return
            async with sem:
                try:
                    batch = await _oi_one(session, sym, limit, px)
                except Exception:  # noqa: BLE001
                    return
            for r in batch:
                k = (r["symbol"], r["ts"])
                if k in seen:
                    continue
                seen.add(k)
                pending.append(r)
            done += 1
            # 边采边落盘：一轮要跑 20 多分钟，攒到最后再写的话中途挂掉就全丢了
            if len(pending) >= 200:
                written += w.write(pending)
                pending.clear()
            if done % 100 == 0:
                health({"lane": "oi5m", "ok": True, "phase": "progress", "done": done,
                        "total": len(symbols), "new_total": written,
                        "budget_used_5min": budget.used()})

        tasks = []
        for sym in symbols:
            if stop.is_set():
                break
            tasks.append(asyncio.create_task(one(sym)))
            await asyncio.sleep(gap)  # 控制发送节奏，别把 5 分钟预算一次打光
        if tasks:
            await asyncio.gather(*tasks)
        written += w.write(pending)
        pending.clear()

        # 清掉 3 小时前的 key，避免集合无限长
        cut = int((time.time() - 3 * 3600) * 1000)
        if len(seen) > 50_000:
            seen = {k for k in seen if k[1] >= cut}

        health({"lane": "oi5m", "ok": True, "phase": "round", "done": done,
                "new_total": written, "symbols": len(symbols),
                "budget_used_5min": budget.used(),
                "ms": int((time.time() - t0) * 1000)})

        left = interval - (time.time() - t0)
        if left > 0:
            try:
                await asyncio.wait_for(stop.wait(), timeout=left)
            except asyncio.TimeoutError:
                pass


# ------------------------------------------------------------------ lane: 清算


async def lane_liquidations(session: aiohttp.ClientSession, w: Writer,
                            preflight: float, retry: float,
                            stop: asyncio.Event, px: str | None) -> None:
    """M14 的真实清算印痕。本机大概率采不到，见模块文档第 2 条。

    预检逻辑：连上 → 发 SUBSCRIBE → 等 `preflight` 秒。
    只收到 `{"result":null,"id":...}`（订阅回执）而没有数据帧，就判定这条链路不可用，
    降级 `retry` 秒再试。**不静默失败、也不反复重连刷日志**。
    """
    while not stop.is_set():
        try:
            async with session.ws_connect(f"{WS_FUTURES}", proxy=px, heartbeat=30) as ws:
                await ws.send_json({"method": "SUBSCRIBE",
                                    "params": ["!forceOrder@arr"], "id": 1})
                got = 0
                deadline = time.time() + preflight
                while time.time() < deadline and not stop.is_set():
                    try:
                        msg = await asyncio.wait_for(ws.receive(), timeout=max(1.0, deadline - time.time()))
                    except asyncio.TimeoutError:
                        break
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        continue
                    d = json.loads(msg.data)
                    if "result" in d and "id" in d:
                        continue  # 订阅回执，不算数据
                    o = (d.get("o") or {})
                    if o.get("s"):
                        w.write([{
                            "symbol": o.get("s"), "ts": int(o.get("T") or 0),
                            "side": o.get("S"), "price": float(o.get("p") or 0),
                            "qty": float(o.get("q") or 0),
                            "avg_price": float(o.get("ap") or 0),
                            "status": o.get("X"),
                        }])
                        got += 1
                if got == 0:
                    health({"lane": "liquidations", "ok": False,
                            "err": f"预检 {preflight:.0f}s 内只收到订阅回执、无数据帧；"
                                   "fstream 推送在本机代理下不可用，降级重试"})
                    await asyncio.wait_for(stop.wait(), timeout=retry)
                    continue
                # 预检通过 → 正常收
                health({"lane": "liquidations", "ok": True, "n": got, "phase": "preflight"})
                while not stop.is_set():
                    msg = await asyncio.wait_for(ws.receive(), timeout=120)
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        break
                    d = json.loads(msg.data)
                    o = (d.get("o") or {})
                    if not o.get("s"):
                        continue
                    w.write([{
                        "symbol": o.get("s"), "ts": int(o.get("T") or 0),
                        "side": o.get("S"), "price": float(o.get("p") or 0),
                        "qty": float(o.get("q") or 0),
                        "avg_price": float(o.get("ap") or 0),
                        "status": o.get("X"),
                    }])
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            health({"lane": "liquidations", "ok": False,
                    "err": f"{type(e).__name__}: {e}"[:200]})
            try:
                await asyncio.wait_for(stop.wait(), timeout=min(retry, 120))
            except asyncio.TimeoutError:
                pass


# ------------------------------------------------------------------ 命令


async def cmd_run(args) -> int:
    lanes = [x.strip() for x in args.lanes.split(",") if x.strip()]
    bad = [x for x in lanes if x not in ALL_LANES]
    if bad:
        print(f"[fatal] 未知 lane: {bad}；可选 {list(ALL_LANES)}", file=sys.stderr)
        return 2

    px = proxy()
    print(f"[forward] 代理 {px or '（直连）'}")
    print(f"[forward] lanes {lanes}  →  {OUT}")
    if "bookticker" in lanes:
        print(f"[forward]   bookticker 每 {args.bt_interval:.0f}s（约 "
              f"{798 * 86400 / max(args.bt_interval, 1) / 1e6:.2f}M 行/天）")
    if "oi5m" in lanes:
        print(f"[forward]   oi5m 每 {args.oi_interval:.0f}s × 最近 {args.oi_limit} 根，"
              f"预算上限 {OI_BUDGET_PER_5MIN}/5min")

    stop = asyncio.Event()

    def _sig(*_a):
        print("\n[forward] 收到停止信号，正在收尾 ...")
        stop.set()

    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(s, _sig)
        except (ValueError, OSError):
            pass

    conn = aiohttp.TCPConnector(limit=32, ttl_dns_cache=300)
    async with aiohttp.ClientSession(connector=conn, trust_env=False) as session:
        tasks = []
        writers: dict[str, Writer] = {}

        if "bookticker" in lanes:
            writers["bookticker"] = Writer("bookticker")
            tasks.append(asyncio.create_task(
                lane_bookticker(session, writers["bookticker"], args.bt_interval, stop, px)))
        if "oi5m" in lanes:
            writers["oi5m"] = Writer("oi5m")
            syms = await fetch_futures_symbols(session, px)
            print(f"[forward]   合约数 {len(syms)}")
            tasks.append(asyncio.create_task(
                lane_oi5m(session, writers["oi5m"], syms, args.oi_interval,
                          args.oi_limit, args.oi_concurrency, stop, px)))
        if "liquidations" in lanes:
            writers["liquidations"] = Writer("liquidations")
            tasks.append(asyncio.create_task(
                lane_liquidations(session, writers["liquidations"],
                                  args.ws_preflight, args.ws_retry, stop, px)))

        await stop.wait()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for w in writers.values():
            print(f"[forward] {w.lane}: 本次共写 {w.total} 行")
            w.close()
    return 0


async def fetch_futures_symbols(session: aiohttp.ClientSession, px: str | None) -> list[str]:
    """取全部 USDT 本位永续合约（只留 TRADING 状态，避免交割/停牌合约白跑）。"""
    url = f"{FAPI}/fapi/v1/exchangeInfo"
    async with session.get(url, proxy=px, timeout=aiohttp.ClientTimeout(total=40)) as r:
        r.raise_for_status()
        info = await r.json()
    out = []
    for s in info.get("symbols", []):
        if s.get("contractType") != "PERPETUAL":
            continue
        if s.get("quoteAsset") != "USDT":
            continue
        if s.get("status") != "TRADING":
            continue
        out.append(s["symbol"])
    return sorted(out)


async def cmd_once(args) -> int:
    """单轮冒烟：跑一次指定 lane，验证能拿到数据、能落盘。"""
    px = proxy()
    lane = args.lane
    w = Writer(lane)
    conn = aiohttp.TCPConnector(limit=16)
    async with aiohttp.ClientSession(connector=conn, trust_env=False) as session:
        if lane == "bookticker":
            async with session.get(f"{FAPI}/fapi/v1/ticker/bookTicker", proxy=px,
                                   timeout=aiohttp.ClientTimeout(total=30)) as r:
                r.raise_for_status()
                data = await r.json()
            ts = int(time.time() * 1000)
            rows = []
            for d in data:
                bid, ask = float(d["bidPrice"] or 0), float(d["askPrice"] or 0)
                if bid > 0 and ask >= bid:
                    rows.append({"symbol": d["symbol"], "ts": ts, "bid": bid, "ask": ask,
                                 "bid_qty": float(d["bidQty"] or 0),
                                 "ask_qty": float(d["askQty"] or 0)})
            n = w.write(rows)
            print(f"[once] bookticker 写 {n} 行 → {lane_path(lane)}")
        elif lane == "oi5m":
            syms = await fetch_futures_symbols(session, px)
            sample = syms[:args.oi_concurrency * 2]
            rows = []
            for s in sample:
                try:
                    rows += await _oi_one(session, s, args.oi_limit, px)
                except Exception as e:  # noqa: BLE001
                    print(f"  {s} 失败 {type(e).__name__}")
            n = w.write(rows)
            print(f"[once] oi5m 写 {n} 行（{len(sample)} 个合约）→ {lane_path(lane)}")
        else:
            print("[once] liquidations 只能常驻跑（WS 预检要时间），用 `run --lanes liquidations`")
            return 2
    w.close()
    return 0


def cmd_status(args) -> int:
    """看今天采了多少。读健康日志 + 文件，不看进程是否存活。"""
    day = args.day or today_utc()
    print(f"=== 前向采集状态 {day} (UTC) ===")
    for lane in ALL_LANES:
        p = OUT / lane / f"{day}.jsonl.gz"
        rows = 0
        partial = False
        if p.exists():
            # 文件可能正在被采集进程追加，gzip 流读到一半会抛 EOFError。
            # 这时报「已读到多少」而不是 -1，否则看起来像坏了。
            try:
                with gzip.open(p, "rt", encoding="utf-8") as f:
                    while True:
                        try:
                            line = f.readline()
                        except Exception:  # noqa: BLE001
                            partial = True
                            break
                        if not line:
                            break
                        rows += 1
            except Exception:  # noqa: BLE001
                partial = True
        size = f"{p.stat().st_size / 1e6:.1f}MB" if p.exists() else "—"
        tail = "（文件正在写入，计数可能偏小）" if partial else ""
        print(f"  {lane:14} 行={rows:>10,}  文件={size}{tail}")

    hp = HEALTH / f"{day}.jsonl"
    if hp.exists():
        ok = fail = 0
        last = {}
        with open(hp, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                last[r.get("lane")] = r
                if r.get("ok"):
                    ok += 1
                else:
                    fail += 1
        print(f"\n  健康记录 ok={ok} fail={fail}")
        for lane, r in last.items():
            flag = "✅" if r.get("ok") else "❌"
            extra = f"n={r.get('n')}" if r.get("ok") else str(r.get("err", ""))[:110]
            print(f"  {flag} {lane:14} {r.get('ts')}  {extra}")
    else:
        print("\n  （今天还没有健康记录）")
    return 0


def cmd_compact(args) -> int:
    """JSONL.gz → parquet，供分析用。按 (symbol, ts) 去重（重启后第一小时会重复）。"""
    import pandas as pd

    lanes = [x.strip() for x in args.lanes.split(",") if x.strip()] or list(ALL_LANES)
    for lane in lanes:
        src = OUT / lane / f"{args.day}.jsonl.gz"
        if not src.exists():
            continue
        df = pd.read_json(src, lines=True, compression="gzip")
        n0 = len(df)
        if {"symbol", "ts"} <= set(df.columns):
            df = df.drop_duplicates(subset=["symbol", "ts"]).sort_values(["ts", "symbol"])
        dst = OUT / lane / f"{args.day}.parquet"
        df.to_parquet(dst, index=False)
        print(f"[compact] {lane}: {n0:,} → {len(df):,} 行（去重 {n0-len(df):,}）"
              f" → {dst.name} ({dst.stat().st_size/1e6:.1f}MB)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="前向采集器（M13 价差 / M14 清算 / OI 5m）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="常驻采集")
    r.add_argument("--lanes", default=",".join(ALL_LANES))
    r.add_argument("--bt-interval", type=float, default=60.0, help="价差采样间隔（秒）")
    r.add_argument("--oi-interval", type=float, default=1800.0, help="OI 轮询间隔（秒）")
    r.add_argument("--oi-limit", type=int, default=12, help="每轮取最近多少根 5m")
    r.add_argument("--oi-concurrency", type=int, default=6)
    r.add_argument("--ws-preflight", type=float, default=90.0, help="WS 预检等待（秒）")
    r.add_argument("--ws-retry", type=float, default=1800.0, help="WS 不可用后多久重试（秒）")
    r.set_defaults(func=lambda a: asyncio.run(cmd_run(a)))

    o = sub.add_parser("once", help="单轮冒烟")
    o.add_argument("--lane", required=True, choices=["bookticker", "oi5m", "liquidations"])
    o.add_argument("--oi-limit", type=int, default=12)
    o.add_argument("--oi-concurrency", type=int, default=6)
    o.set_defaults(func=lambda a: asyncio.run(cmd_once(a)))

    s = sub.add_parser("status", help="看今天采了多少")
    s.add_argument("--day", default="")
    s.set_defaults(func=cmd_status)

    c = sub.add_parser("compact", help="JSONL → parquet")
    c.add_argument("--day", required=True)
    c.add_argument("--lanes", default="")
    c.set_defaults(func=cmd_compact)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
