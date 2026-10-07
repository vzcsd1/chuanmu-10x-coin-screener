#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""archive_download_ds · 可靠归档下载器（独立工具）

对应任务规格 `tasks/ds41f_archive_download.md`。本工具**不 import 任何策略/研究模块**，
只读已核实的公开归档 URL，不做范围推断（范围不由赢家、评分或当前活跃币种决定）。

设计要点（逐条对应任务书要求）：

1. 固定输入字段：source_url / market / dataset / symbol / interval /
   period_start / period_end / remote_size_bytes / checksum_url / discovered_at_utc。
2. **先验证已存文件，再从未完成项里挑本批** —— 不重复旧脚本"先取前 N 个再跳过"的缺陷
   （那是本工具存在的原因之一）。每成功一份立即落盘状态；`.part` 片段永远不算完成。
3. 完成前检查三件事：压缩包可打开且含 CSV、`remote_size_bytes` 一致（若提供）、
   来源校验值一致（若提供）。**没有校验值就如实标注"未验证来源校验值"，不谎称已验证。**
4. 落盘记录：字节数、获取时间、校验结果、状态。状态分类
   `success` / `not_found` / `paused_ratelimit` / `failed_transient` /
   `content_anomaly` / `skipped_complete` / `budget_stopped`。
   **404 只代表本次未找到，不证明历史从未存在。**
5. 只列不下载的 `plan` 模式（零网络请求）；`--max-files` / `--max-bytes` /
   `--max-minutes` / `--min-free-gib` 预算；默认单连接、请求间隔 ≥1 秒
   （**初始实施参数，不是来源允许额度的保证**），服务端要求时进一步减速。
6. 429/418/Retry-After：保存暂停时间到状态文件，本次**立即停止后续请求**，重启不丢。
   暂时性网络错误做有限次重试；限流/未找到不重试。
7. 并发互斥（锁文件）；中断保留进度；**不安装任何定时任务**。
8. 压缩包路径安全检查（zip-slip）：任何解包目标必须落在输出目录内。
9. 同路径内容变化**保留版本**：旧原始资料改名留存，不静默覆盖。
10. CSV 解析差异（列名/无表头/毫秒微秒）只**记录特征**，不静默转换丢原始值；
    原始压缩包原样保存。

用法：
  py -3.10 tools/archive_download_ds.py plan  --manifest reports/archive_download_ds/manifest_first_batch.csv
  py -3.10 tools/archive_download_ds.py fetch --manifest reports/archive_download_ds/manifest_first_batch.csv \
      --max-files 30 --max-bytes 104857600 --max-minutes 15 --min-free-gib 5
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import logging
import re
import shutil
import sys
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "data" / "archive_raw"
DEFAULT_STATUS_CSV = ROOT / "reports" / "archive_download_ds" / "download_status.csv"

# 与 glm 任务一致的固定字段（见 tasks/glm53f_data_inventory.md 第 4 条）
FIELDS = ["source_url", "market", "dataset", "symbol", "interval",
          "period_start", "period_end", "remote_size_bytes", "checksum_url",
          "discovered_at_utc"]

# —— 状态分类 ——
S_SUCCESS = "success"                  # 下载并通过全部可得校验
S_NOT_FOUND = "not_found"              # 404/403：本次未找到
S_PAUSED = "paused_ratelimit"          # 429/418/Retry-After：已登记暂停
S_TRANSIENT = "failed_transient"       # 网络类错误，重试后仍失败
S_ANOMALY = "content_anomaly"          # 内容异常（损坏/无 CSV/校验值不符）
S_COMPLETE = "skipped_complete"        # 本地已有且验证完整，本次跳过
S_BUDGET = "budget_stopped"            # 因预算（文件数/字节/时间）未开始或中止
S_PENDING = "pending"                  # 计划中，尚未处理
S_UNVERIFIED = "unverified_pending"    # 归档已存但来源校验值暂时取不到，**不算完成**

CHECK_VERIFIED = "verified"            # 来源校验值一致
CHECK_NO_SOURCE = "no_checksum_source"  # 来源确认未发布校验值（404）—— 不能声称已验证
CHECK_MISMATCH = "mismatch"            # 校验值不一致 → 不得完成
CHECK_FETCH_FAILED = "checksum_fetch_failed"  # 校验值**获取失败**（限流/网络）→ 待复核，≠ 来源无校验
CHECK_NA = "n/a"                       # 未做校验（如未找到）

# 只有这两种校验结论才允许把一份文件记为「完成」
CHECK_CONCLUSIVE = (CHECK_VERIFIED, CHECK_NO_SOURCE)

_BAN_TS_RE = re.compile(r"banned until[^\d]*(\d{10,13})", re.IGNORECASE)
_RETRY_AFTER_RE = re.compile(r"retry[-_ ]?after\D{0,5}(\d{1,7})", re.IGNORECASE)

LOCK_NAME = ".archive_download.lock"
STATE_NAME = "_state.json"
# 队列循环检查"停止哨兵"的文件名（放在归档目录下，与锁同处）。
# 命中即：当前块跑完后保存状态并正常退出。由 archive_queue_ds 的 `stop` 子命令创建。
STOP_NAME = ".archive_download.stop"

# 本地退避阶梯（毫秒）：**只在来源没给恢复时间时**使用，逐级放大、封顶 4 小时。
# 这只是本地的等待档位，不代表来源的允许额度；来源给了明确解封时刻时一律以来源为准，
# 绝不因为"4 小时封顶"就把来源的长期限截短。
LOCAL_BACKOFF_LADDER_MS = (30 * 60_000, 60 * 60_000, 120 * 60_000, 240 * 60_000)


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_ms() -> int:
    return int(time.time() * 1000)


def human_ts(ms: int | float | None) -> str:
    """毫秒时间戳 → 本地可读时间；0/None 返回"未指定"。

    暂停原因要写进状态和日志，人得看得懂"到几点为止"。
    """
    try:
        value = int(ms or 0)
    except (TypeError, ValueError):
        return "未指定"
    if value <= 0:
        return "未指定"
    return datetime.fromtimestamp(value / 1000.0).strftime("%Y-%m-%d %H:%M:%S")


def host_of(url: str) -> str:
    return urlsplit(str(url or "")).netloc.lower()


def safe_seg(value: Any, fallback: str = "_") -> str:
    """把任意字符串压成安全的单层路径片段（防 `..`、分隔符、盘符、非法字符）。

    **必须单射**：只要有字符被替换，就追加**原始值**的短摘要。
    否则不同来源会塌缩成同一路径而互相覆盖——实测 `牛来USDT` 与 `龙虾USDT`
    都变成 `__USDT`，两个币的归档写进同一文件，前者被改名成 `.stale-` 丢失。
    """
    raw = str(value if value is not None else "").strip()
    raw = raw.replace("\\", "/").split("/")[-1]         # 丢掉目录部分
    raw = raw.split(":")[-1]                            # 丢掉盘符
    text = re.sub(r"[^A-Za-z0-9._\-]", "_", raw)
    if text != raw:
        # 有字符被替换 → 加原始值的短摘要，保证不同来源不共用路径
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]
        text = f"{text[:100]}~{digest}"
    if text in ("", ".", ".."):
        return fallback
    return text[:120]


def parse_pause_until(headers: dict[str, Any], body: str, now: int | None = None) -> int:
    """从响应头/响应体解析恢复时间（毫秒）；解析不到返回 0。"""
    now = now_ms() if now is None else now
    lowered = {str(k).lower(): v for k, v in (headers or {}).items()}
    ra = lowered.get("retry-after")
    if ra:
        try:
            return now + int(float(str(ra).strip())) * 1000
        except (TypeError, ValueError):
            pass
    match = _BAN_TS_RE.search(str(body or ""))
    if match:
        ts = int(match.group(1))
        return ts * 1000 if ts < 10 ** 12 else ts
    match = _RETRY_AFTER_RE.search(str(body or ""))
    if match:
        return now + int(match.group(1)) * 1000
    return 0


# ---------------------------------------------------------------------------
# 取数层（可注入，便于离线测试）
# ---------------------------------------------------------------------------
class Resp:
    __slots__ = ("status_code", "headers", "content")

    def __init__(self, status_code: int, headers: dict[str, Any], content: bytes):
        self.status_code = status_code
        self.headers = dict(headers or {})
        self.content = content


class FetchError(Exception):
    """网络/协议类错误（可有限重试）。"""


class ByteBudgetExceeded(FetchError):
    """流式下载超过字节预算，已中止。"""


class NotFound(FetchError):
    """来源本次未找到（404/403）。"""


class RateLimited(FetchError):
    """被限流/封禁；`until_ms` 为解析到的恢复时间（0 表示未解析到）。"""

    def __init__(self, status_code: int, until_ms: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.until_ms = int(until_ms or 0)


class Paused(Exception):
    """**本路请求被暂停闸门拦下**（不是网络错误，不发请求）。

    与 `RateLimited` 的区别：`RateLimited` 是"服务器刚刚拒绝了我"，
    本异常是"**别的路**已经登记了限流暂停，我连请求都不许发"。
    多路并发下必须区分，否则晚到的成功会把别人的暂停抹掉、或继续新发请求。
    """

    def __init__(self, host: str, until_ms: int = 0, message: str = ""):
        super().__init__(message or f"{host} 处于限流暂停")
        self.host = host
        self.until_ms = int(until_ms or 0)


class RequestsFetcher:
    """默认取数器：流式读取、按预算中止。

    `pool_size<=1` 时沿用**单连接**（与旧行为完全一致）；
    `pool_size>1` 时每线程各持一个 Session——`requests.Session` 并非严格线程安全，
    多路共用同一连接可能交错响应，故按线程隔离而不是共享。
    """

    def __init__(self, user_agent: str = "chuanmu-archive-ds/0.1",
                 pool_size: int = 1):
        import requests  # 延迟导入：测试可完全绕开网络
        self._requests = requests
        self._ua = user_agent
        self.pool_size = max(1, int(pool_size))
        self._local = threading.local()
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent})

    def _session(self):
        if self.pool_size <= 1:
            return self.session
        s = getattr(self._local, "session", None)
        if s is None:
            s = self._requests.Session()
            s.headers.update({"User-Agent": self._ua})
            self._local.session = s
        return s

    def get(self, url: str, *, timeout: float = 60.0,
            max_bytes: int | None = None) -> Resp:
        try:
            r = self._session().get(url, timeout=timeout, stream=True)
        except Exception as exc:  # noqa: BLE001
            raise FetchError(f"{type(exc).__name__}: {exc}") from exc
        try:
            code = int(r.status_code)
            headers = dict(r.headers or {})
            if code in (418, 429):
                body = _safe_text(r)
                raise RateLimited(code, parse_pause_until(headers, body),
                                  f"HTTP {code}: {body[:160]}")
            if code in (403, 404):
                raise NotFound(f"HTTP {code}")
            if code != 200:
                raise FetchError(f"HTTP {code}")
            buf = bytearray()
            for chunk in r.iter_content(65536):
                if not chunk:
                    continue
                buf += chunk
                if max_bytes is not None and len(buf) > max_bytes:
                    raise ByteBudgetExceeded(
                        f"超过字节预算（已读 {len(buf)} > {max_bytes}）")
            return Resp(code, headers, bytes(buf))
        finally:
            try:
                r.close()
            except Exception:  # noqa: BLE001
                pass


def _safe_text(r) -> str:
    try:
        return str(r.text or "")
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------------------
# 清单条目
# ---------------------------------------------------------------------------
@dataclass
class Entry:
    source_url: str
    market: str = ""
    dataset: str = ""
    symbol: str = ""
    interval: str = ""
    period_start: str = ""
    period_end: str = ""
    remote_size_bytes: int | None = None
    checksum_url: str = ""
    discovered_at_utc: str = ""

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Entry":
        def text(key: str) -> str:
            value = row.get(key)
            return "" if value is None else str(value).strip()

        size_raw = text("remote_size_bytes")
        size: int | None = None
        if size_raw:
            try:
                size = int(float(size_raw))
            except ValueError:
                size = None
        return cls(source_url=text("source_url"), market=text("market"),
                   dataset=text("dataset"), symbol=text("symbol"),
                   interval=text("interval"), period_start=text("period_start"),
                   period_end=text("period_end"), remote_size_bytes=size,
                   checksum_url=text("checksum_url"),
                   discovered_at_utc=text("discovered_at_utc"))

    @property
    def key(self) -> str:
        """唯一键：含 market，保证不同市场的同名币不冲突。"""
        return "|".join([self.market, self.dataset, self.symbol,
                         self.interval, self.period_start, self.period_end])

    @property
    def filename(self) -> str:
        name = self.source_url.rstrip("/").rsplit("/", 1)[-1]
        return safe_seg(name, fallback="download.bin")

    def relpath(self) -> Path:
        """按 市场/类别/币种/周期/文件 分层，天然隔离不同市场同名币。"""
        return (Path(safe_seg(self.market, "unknown_market"))
                / safe_seg(self.dataset, "unknown_dataset")
                / safe_seg(self.symbol, "unknown_symbol")
                / safe_seg(self.interval, "no_interval")
                / self.filename)


# 校验暂时做不了时，归档字节先落这个后缀，**不进正式路径**。
# 正式路径只放"来源校验值已确认（或来源确认没发布校验值）"的文件；
# 待复核字节单独放，恢复后**只补一次校验请求**，通过就提升为正式文件、不重下整份。
PENDING_SUFFIX = ".pending"


def official_path_for(out_dir: str | Path, entry: Entry) -> Path:
    return Path(out_dir) / entry.relpath()


def pending_path_for(out_dir: str | Path, entry: Entry) -> Path:
    target = Path(out_dir) / entry.relpath()
    return target.with_name(target.name + PENDING_SUFFIX)


def load_manifest(path: str | Path) -> list[Entry]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"清单不存在：{p}")
    if p.suffix.lower() == ".json":
        raw = json.loads(p.read_text(encoding="utf-8"))
        rows = raw if isinstance(raw, list) else raw.get("entries", [])
    else:
        with p.open("r", encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
    entries = [Entry.from_row(r) for r in rows if str(r.get("source_url", "")).strip()]
    return entries


# ---------------------------------------------------------------------------
# 状态（暂停跨重启 + 逐份结果）
# ---------------------------------------------------------------------------
class State:
    """快照 + 增量日志（JSONL）。

    **每份文件只追加一行**，不再重写整库——否则大库的累计写入量随文件数近似平方增长。
    日志行数超过 `compact_every` 时整理成快照并清空日志。读取 = 快照 + 重放日志。
    """

    def __init__(self, path: str | Path | None, compact_every: int = 5000):
        self.path = Path(path) if path else None
        self.journal_path = (self.path.with_suffix(".journal.jsonl")
                             if self.path else None)
        self.compact_every = int(compact_every)
        self.paused: dict[str, dict[str, Any]] = {}
        self.records: dict[str, dict[str, Any]] = {}
        # 本地退避档位：host -> 已用到第几级（**跨重启保持**，不因重启清零）
        self.backoff: dict[str, int] = {}
        self.journal_lines = 0
        # 多路并发时**单一写入者**：所有变更串行化，日志行不会交错撕裂
        self._lock = threading.RLock()
        self.load()

    def load(self) -> None:
        if self.path is not None and self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self.paused = dict(raw.get("paused") or {})
                self.records = dict(raw.get("records") or {})
                self.backoff = {k: int(v) for k, v in (raw.get("backoff") or {}).items()}
            except Exception:  # noqa: BLE001
                # 快照损坏按"无暂停、无记录"处理（fail-open），靠日志重建
                self.paused, self.records, self.backoff = {}, {}, {}
        if self.journal_path is not None and self.journal_path.exists():
            bad = 0
            with self.journal_path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    self.journal_lines += 1
                    # **逐行容错**：一行坏掉不能让后面的行全部丢失
                    try:
                        self._replay(json.loads(line))
                    except Exception:  # noqa: BLE001
                        bad += 1
            if bad:
                logging.warning("状态日志 %s 有 %d 行损坏，已跳过并重放其余 %d 行",
                                self.journal_path, bad, self.journal_lines - bad)

    def _replay(self, obj: dict[str, Any]) -> None:
        kind = obj.get("t")
        if kind == "rec":
            key = obj.get("k")
            if key:
                self.records.setdefault(key, {}).update(obj.get("v") or {})
        elif kind == "pause":
            # 重放也必须用"取最晚"合并，否则重启后一条后到的短暂停
            # 会把日志里更早登记的长限制压短——等于绕过 set_pause 的保护。
            self._merge_pause(obj.get("h", ""), dict(obj.get("v") or {}))
        elif kind == "backoff":
            host = obj.get("h", "")
            level = int((obj.get("v") or {}).get("level", 0) or 0)
            if level > 0:
                self.backoff[host] = level
            else:
                self.backoff.pop(host, None)
        elif kind == "unpause":
            self.paused.pop(obj.get("h", ""), None)

    def _append(self, obj: dict[str, Any]) -> None:
        with self._lock:
            if self.journal_path is None:
                return
            self.journal_path.parent.mkdir(parents=True, exist_ok=True)
            with self.journal_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
            self.journal_lines += 1
            if self.journal_lines >= self.compact_every:
                self.save(force=True)

    def save(self, force: bool = False) -> None:
        """默认是**空操作**（增量已落盘）；`force=True` 或日志过大时才整理快照。"""
        with self._lock:
            if self.path is None:
                return
            if not force and self.journal_lines < self.compact_every:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"version": 1, "updated_at_utc": utcnow_iso(),
                       "paused": self.paused, "records": self.records,
                       "backoff": self.backoff}
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            tmp.replace(self.path)
            if self.journal_path is not None and self.journal_path.exists():
                try:
                    self.journal_path.unlink()
                except OSError:
                    pass
            self.journal_lines = 0

    # —— 暂停 ——
    def paused_until(self, host: str) -> int:
        return int((self.paused.get(host) or {}).get("until_ms", 0))

    def is_paused(self, host: str, now: int | None = None) -> bool:
        now = now_ms() if now is None else now
        until = self.paused_until(host)
        return bool(until and now < until)

    def pause_source(self, host: str) -> str:
        """这次暂停的期限来自哪里：`source`（来源给了明确解封时刻）/ `local`（本地退避档位）。"""
        return str((self.paused.get(host) or {}).get("deadline_source") or "")

    # —— 本地退避档位（来源没给恢复时间时才用）——
    def backoff_level(self, host: str) -> int:
        return int(self.backoff.get(host, 0) or 0)

    def take_local_backoff_ms(self, host: str) -> tuple[int, int]:
        """取本域名当前该用的本地等待档位，并**推进一级**。

        返回 `(档位序号从 0 起, 毫秒)`。档位跨重启保持（写进状态日志），
        所以"重启就清零、于是又去撞限流"不会发生。
        """
        with self._lock:
            lv = min(self.backoff_level(host), len(LOCAL_BACKOFF_LADDER_MS) - 1)
            ms = LOCAL_BACKOFF_LADDER_MS[lv]
            nxt = min(lv + 1, len(LOCAL_BACKOFF_LADDER_MS) - 1)
            self.backoff[host] = nxt
            self._append({"t": "backoff", "h": host, "v": {"level": nxt}})
            return lv, ms

    def reset_backoff(self, host: str) -> None:
        """有请求成功 → 说明已经恢复，本地等待档位归零（下次限流从最短档重来）。"""
        with self._lock:
            if self.backoff.pop(host, None) is not None:
                self._append({"t": "backoff", "h": host, "v": {"level": 0}})

    def _merge_pause(self, host: str, payload: dict[str, Any]) -> dict[str, Any]:
        """同一域名**保留所有未到期期限中的最晚者**，返回最终生效的载荷。

        短期限不能覆盖长限制，长限制可以延长短限制；不同域名各自独立
        （`self.paused` 本就按 host 分桶，天然隔离）。
        调用方必须已持有 `self._lock`。
        """
        old = self.paused.get(host) or {}
        old_until = int(old.get("until_ms", 0) or 0)
        new_until = int(payload.get("until_ms", 0) or 0)
        if old_until > new_until:
            # 后到的更短期限：不缩短，保留原有更长限制与其原因
            merged = dict(payload)
            merged["until_ms"] = old_until
            merged["reason"] = old.get("reason") or payload.get("reason", "")
            merged["saved_at_utc"] = old.get("saved_at_utc") or payload.get("saved_at_utc")
            merged["kept_longer_until_ms"] = old_until
            merged["dropped_shorter_until_ms"] = new_until
            self.paused[host] = merged
            return merged
        self.paused[host] = payload
        return payload

    def set_pause(self, host: str, until_ms: int, reason: str,
                  backoff_ms: int | None = None) -> None:
        """登记暂停。**来源给了明确期限就照办，没给才用本地退避阶梯。**

        - `until_ms` 是未来的有效时刻 → `deadline_source="source"`，严格等到那时
          （哪怕超过几小时也不缩短），本地档位**不动**。
        - `until_ms` 缺失/已过期 → 用本地阶梯的当前档（30 分钟 → 1 小时 → 2 小时 → 4 小时），
          并把档位推进一级；档位写进状态日志，**跨重启保持**。
        - `backoff_ms` 显式传入时优先用它（测试与特殊调用用）。
        """
        with self._lock:
            now = now_ms()
            explicit = bool(until_ms and until_ms > now)
            if explicit:
                until = int(until_ms)
                note = str(reason or "")[:200]
                source = "source"
            else:
                if backoff_ms is not None:
                    lv = -1
                    local_ms = int(backoff_ms)
                else:
                    lv, local_ms = self.take_local_backoff_ms(host)
                until = now + local_ms
                tag = (f"本地第 {lv + 1} 档" if lv >= 0
                       else f"本地 {local_ms // 60_000} 分钟档")
                note = (f"未解析到恢复时间，按{tag}等待 {local_ms // 60_000} 分钟；"
                        f"{str(reason or '')[:160]}")
                source = "local"
            payload = {"until_ms": until, "reason": note, "saved_at_utc": utcnow_iso(),
                       "deadline_source": source}
            # 只登记"最晚者"：多路并发下两条在途请求会各收到一份限流头，
            # 后到的短暂停不得提前解封先到的长限制。
            effective = self._merge_pause(host, payload)
            self._append({"t": "pause", "h": host, "v": effective})

    def clear_pause(self, host: str) -> None:
        """**无条件**清除暂停。只应给"确实已经恢复"的显式流程用（如人工/到期探测）。

        ⚠️ **不要在下载成功的收尾路径调用**：多路并发下，一个晚到的成功请求
        会把其他工作者刚登记、尚未到期的暂停一起抹掉，限流保护就失效了。
        收尾请用 `clear_pause_if_expired()`。
        """
        with self._lock:
            if host in self.paused:
                del self.paused[host]
                self._append({"t": "unpause", "h": host})

    def clear_pause_if_expired(self, host: str, now: int | None = None) -> bool:
        """**只在暂停已到期时**才清除；返回是否真的清了。

        多路限流交错的核心修复：成功的收尾不能解除**比它更新**的暂停。
        这里用"是否已到期"作等价判据——未到期的暂停一律不动，
        到期后由恢复探测（或任意一次成功收尾）解除。
        """
        with self._lock:
            info = self.paused.get(host)
            if not info:
                return False
            now = now_ms() if now is None else now
            if int(info.get("until_ms", 0)) > now:
                return False
            del self.paused[host]
            self._append({"t": "unpause", "h": host})
            return True

    # —— 记录 ——
    def set_record(self, entry: Entry, **fields: Any) -> dict[str, Any]:
        with self._lock:
            rec = self.records.setdefault(entry.key, {})
            rec.update(fields)
            rec["key"] = entry.key
            self._append({"t": "rec", "k": entry.key,
                          "v": {**fields, "key": entry.key}})
            return rec

    def key_for(self, entry: Entry) -> dict[str, Any]:
        return self.records.get(entry.key) or {}


# ---------------------------------------------------------------------------
# 预算与锁
# ---------------------------------------------------------------------------
class Budget:
    def __init__(self, max_files: int = 0, max_bytes: int = 0,
                 max_minutes: float = 0.0, started: float | None = None):
        self.max_files = int(max_files or 0)
        self.max_bytes = int(max_bytes or 0)
        self.max_minutes = float(max_minutes or 0)
        self.started = time.time() if started is None else started
        self.files = 0
        self.bytes = 0
        self._lock = threading.Lock()   # 多路共享同一个总预算

    @property
    def deadline(self) -> float | None:
        if not self.max_minutes:
            return None
        return self.started + self.max_minutes * 60.0

    def can_start_file(self) -> tuple[bool, str]:
        with self._lock:
            if self.max_files and self.files >= self.max_files:
                return False, f"已达文件数上限 {self.max_files}"
            if self.max_bytes and self.bytes >= self.max_bytes:
                return False, f"已达字节上限 {self.max_bytes}"
            dl = self.deadline
            if dl and time.time() >= dl:
                return False, f"已达时间上限 {self.max_minutes} 分钟"
            return True, ""

    def remaining_bytes(self) -> int | None:
        with self._lock:
            if not self.max_bytes:
                return None
            return max(0, self.max_bytes - self.bytes)

    def spend(self, n: int) -> None:
        with self._lock:
            self.bytes += int(n or 0)

    def count_file(self) -> None:
        with self._lock:
            self.files += 1


LOCK_STALE_SEC = 300.0     # 心跳超过这个时长才判定持有者已死
LOCK_REFRESH_SEC = 30.0    # 持有者刷新心跳的间隔


def _pid() -> int:
    try:
        import os
        return os.getpid()
    except Exception:  # noqa: BLE001
        return 0


def _hostname() -> str:
    try:
        import socket
        return socket.gethostname()
    except Exception:  # noqa: BLE001
        return "?"


class DownloadLock:
    """基于**心跳**的互斥锁。

    旧实现只看锁文件年龄（1800 秒后直接删除再写），长时间运行仍活着的下载进程
    会被第二个写入者抢占。这里改为：持有者每 `refresh_sec` 秒刷新心跳，
    只有**心跳**超过 `stale_sec` 才判定为残留。活进程因此永远不会被抢占。
    释放时校验 token，避免删掉别人的锁。
    """

    def __init__(self, out_dir: Path, stale_sec: float = LOCK_STALE_SEC,
                 refresh_sec: float = LOCK_REFRESH_SEC):
        self.path = Path(out_dir) / LOCK_NAME
        self.stale_sec = float(stale_sec)
        self.refresh_sec = float(refresh_sec)
        self.token = uuid.uuid4().hex
        self.acquired = False
        self._last_touch = 0.0
        self._lock = threading.Lock()   # 多路并发 touch/release 时保护锁文件

    def _read(self) -> dict[str, Any]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            return {}

    def owner_alive(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        info = self._read()
        heartbeat = info.get("heartbeat_at")
        if heartbeat is None:
            try:
                heartbeat = self.path.stat().st_mtime
            except OSError:
                return False
        try:
            return (now - float(heartbeat)) < self.stale_sec
        except (TypeError, ValueError):
            return True  # 读不懂就保守认为有人在用

    def acquire(self) -> tuple[bool, str]:
        if self.path.exists():
            if self.owner_alive():
                info = self._read()
                age = time.time() - float(info.get("heartbeat_at") or 0)
                return False, (f"另一份下载进程可能正在运行（pid={info.get('pid')}，"
                               f"心跳 {age:.0f} 秒前），本次不启动")
            logging.warning("锁 %s 心跳已过期（>%.0fs），判定为残留并接管",
                            self.path, self.stale_sec)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write()
        self.acquired = True
        return True, "已取得下载锁"

    def _write(self) -> None:
        payload = {"token": self.token, "pid": _pid(), "host": _hostname(),
                   "acquired_at_utc": utcnow_iso(), "heartbeat_at": time.time()}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)
        self._last_touch = time.time()

    def touch(self, force: bool = False) -> None:
        if not self.acquired:
            return
        if not force and (time.time() - self._last_touch) < self.refresh_sec:
            return
        with self._lock:
            if self._read().get("token") != self.token:
                return  # 锁已被他人接管，不再刷新
            self._write()

    def release(self) -> None:
        if not self.acquired:
            return
        with self._lock:
            if self._read().get("token") == self.token:
                try:
                    self.path.unlink()
                except OSError:
                    pass
        self.acquired = False


# ---------------------------------------------------------------------------
# 校验：本地已有文件 / 远端内容
# ---------------------------------------------------------------------------
def _first_csv(z: zipfile.ZipFile) -> str | None:
    names = [n for n in z.namelist() if n.lower().endswith(".csv")]
    return names[0] if names else None


def check_zip_paths(z: zipfile.ZipFile) -> tuple[bool, str]:
    """zip-slip 检查：成员名不得是绝对路径或含 `..`。"""
    for name in z.namelist():
        normalized = name.replace("\\", "/")
        if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
            return False, f"压缩包含绝对路径成员：{name}"
        if any(part == ".." for part in normalized.split("/")):
            return False, f"压缩包含上跳路径成员：{name}"
    return True, "ok"


def inspect_content(entry: Entry, content: bytes) -> tuple[bool, str]:
    """下载后检查压缩包与内容结构；只记录解析特征，不做静默转换。"""
    if not content:
        return False, "内容为空"
    if entry.remote_size_bytes and len(content) != entry.remote_size_bytes:
        return False, f"字节数与清单不符（实收 {len(content)} ≠ {entry.remote_size_bytes}）"
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            bad = z.testzip()
            if bad is not None:
                return False, f"压缩包损坏（首个坏成员 {bad}）"
            ok, why = check_zip_paths(z)
            if not ok:
                return False, why
            csv_name = _first_csv(z)
            if csv_name is None:
                return False, "压缩包内没有 CSV"
            with z.open(csv_name) as fh:
                head = fh.read(8192)
    except zipfile.BadZipFile as exc:
        return False, f"不是有效压缩包：{exc}"
    except Exception as exc:  # noqa: BLE001
        return False, f"读取压缩包失败：{type(exc).__name__}"

    lines = [l for l in head.decode("utf-8", "replace").splitlines() if l.strip()]
    if not lines:
        return False, "CSV 无内容行"
    header = not lines[0][:1].isdigit()
    first = lines[1] if header and len(lines) > 1 else lines[0]
    cols = len(first.split(","))
    ts_unit = _ts_unit(first.split(",")[0] if first.split(",") else "")
    note = (f"csv={csv_name}, cols={cols}, "
            f"header={'yes' if header else 'no'}, ts={ts_unit}")
    return True, note


def _ts_unit(raw: str) -> str:
    token = str(raw).strip().strip('"')
    if not token.isdigit():
        return "unknown"
    n = len(token)
    if n >= 16:
        return "us"
    if n >= 12:
        return "ms"
    if n == 10:
        return "s"
    return "unknown"


def sha256_hex(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    """流式摘要，避免把几百 MB 的 5m 归档整体读进内存。"""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def verify_local(path: Path, entry: Entry,
                 expected_digest: str | None = None) -> tuple[bool, str]:
    """本地文件是否可视为「完整」。

    - 有**已存来源摘要**时优先比对摘要：命中即视为完整，**不再解压**（省掉每轮全库解压）。
    - 没有摘要时退回结构检查（尺寸 + 可解压 + 含 CSV）。
    - 片段/损坏/尺寸不符一律不算完成。
    """
    if not path.exists():
        return False, "本地不存在"
    size = path.stat().st_size
    if size <= 0:
        return False, "本地文件为空"
    if entry.remote_size_bytes and size != entry.remote_size_bytes:
        return False, f"本地字节数不符（{size} ≠ {entry.remote_size_bytes}）"
    if expected_digest:
        actual = sha256_file(path)
        if actual != expected_digest:
            return False, f"本地摘要与已存来源摘要不符（{actual[:12]} ≠ {expected_digest[:12]}）"
        return True, "本地摘要与已存来源摘要一致"
    try:
        with zipfile.ZipFile(path) as z:
            if z.testzip() is not None:
                return False, "本地压缩包损坏"
            if _first_csv(z) is None:
                return False, "本地压缩包内无 CSV"
    except zipfile.BadZipFile:
        return False, "本地文件不是有效压缩包"
    except Exception as exc:  # noqa: BLE001
        return False, f"本地校验失败：{type(exc).__name__}"
    return True, "本地完整"


def local_digest_for(state: "State", entry: Entry) -> str | None:
    """取该键**已确认过的来源摘要**；未确认过则返回 None（不能拿它当完成依据）。"""
    rec = state.records.get(entry.key) or {}
    if rec.get("checksum_status") == CHECK_VERIFIED and rec.get("checksum_sha256"):
        return str(rec["checksum_sha256"])
    return None


def parse_checksum_file(text: str) -> str:
    """`<sha256>  <filename>` 或仅 `<sha256>`；返回小写摘要，解析不到返回空串。"""
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        token = line.split()[0]
        if re.fullmatch(r"[0-9a-fA-F]{64}", token):
            return token.lower()
    return ""


# ---------------------------------------------------------------------------
# 下载器
# ---------------------------------------------------------------------------
@dataclass
class Options:
    out_dir: Path = DEFAULT_OUT
    state_path: Path | None = None
    status_csv: Path | None = DEFAULT_STATUS_CSV
    max_files: int = 30
    max_bytes: int = 100 * 1024 * 1024
    max_minutes: float = 15.0
    min_free_gib: float = 5.0
    interval_sec: float = 1.0
    timeout: float = 60.0
    retries: int = 2
    force: bool = False
    dry_run: bool = False
    backoff_sec: float = 1.0
    workers: int = 1        # 并发路数；1 = 与旧单连接行为一致
    compact_every: int = 5000   # 状态日志多少行整理一次快照（大库要调大）
    # 「同时在途的响应字节」上限。现有实现把响应整个读进内存，
    # 所以把并发从 4 拉到 32 时必须同时约束在途字节，否则大 K 线文件会吃爆内存。
    inflight_bytes_budget: int = 512 * 1024 * 1024
    # 上一次 `run_fetch` 实际发出的 HTTP 请求数（含 ZIP / CHECKSUM / 重试 / 恢复探测）。
    # 队列工具用它统计"请求数"，不另开计数池。
    last_request_count: int = 0


class InflightGate:
    """限制**同时在途的响应字节**，防止提高并发后大文件把内存吃爆。

    预算按队列里的 `remote_size_bytes` **预扣**，响应读完后归还。
    两个刻意的设计：

    * **预计就超预算的单个文件不拒绝**：先等空场，再让它单独占用预算跑完。
      任务要求"不能跳过"，所以宁可慢也不能丢。
    * 没有已知大小的条目按 `default_bytes` 保守预扣（宁高不低）。
    """

    def __init__(self, budget: int, default_bytes: int = 256 * 1024):
        self.budget = max(0, int(budget or 0))
        self.default_bytes = int(default_bytes)
        self.used = 0
        self.peak = 0
        self.waits = 0
        self._cv = threading.Condition()

    def reserve(self, expected: int | None) -> int:
        """预扣预算并返回实际预扣量；调用方必须把这个值原样交给 `release`。"""
        if self.budget <= 0:
            return 0
        want = int(expected) if expected and expected > 0 else self.default_bytes
        # 单份就超过整个预算 → 夹到预算上限，等空场后独占跑完（不跳过）
        want = min(want, self.budget)
        with self._cv:
            while self.used + want > self.budget and self.used > 0:
                self.waits += 1
                self._cv.wait(0.25)
            self.used += want
            self.peak = max(self.peak, self.used)
            return want

    def release(self, reserved: int) -> None:
        with self._cv:
            self.used = max(0, self.used - int(reserved))
            self._cv.notify_all()


class Downloader:
    def __init__(self, fetcher: Any, opts: Options,
                 sleeper: Callable[[float], None] = time.sleep,
                 pace_lock: Any | None = None):
        self.fetcher = fetcher
        self.opts = opts
        self._sleep = sleeper
        self._last_request_at = 0.0
        # 多路共享**同一把**节奏锁 → 全局限速，而不是每路各限一次
        self._pace_lock = pace_lock or threading.Lock()
        # 请求计数：**ZIP、CHECKSUM、重试、恢复探测全部算**（共用一个出口 `_get`）。
        # 报告里"请求数"必须是这个数，不能只数下载成功的份数。
        self._count_lock = threading.Lock()
        self.requests = 0

    # —— 请求节流：全局间隔 ≥ interval_sec（多路共用同一张发号）——
    def _pace(self) -> None:
        gap = self.opts.interval_sec
        if gap <= 0:
            return
        with self._pace_lock:
            wait = self._last_request_at + gap - time.time()
            if wait > 0:
                self._sleep(wait)
            self._last_request_at = time.time()

    def _get(self, url: str, max_bytes: int | None = None,
             state: "State | None" = None) -> Resp:
        """**所有** HTTP 请求（数据 / 校验 / 重试）的唯一出口。

        暂停检查必须放在**节奏等待之后**：等待期间别的路可能刚登记了限流暂停，
        等完再查才拦得住。已发出的请求允许安全收尾（不打断），
        但一旦处于暂停期，本路**不得再新发任何请求**——包括校验请求。
        """
        self._pace()
        if state is not None:
            host = host_of(url)
            if state.is_paused(host):
                raise Paused(host, state.paused_until(host),
                             f"{host} 处于限流暂停（至 {human_ts(state.paused_until(host))}），"
                             f"本路不再发请求")
        with self._count_lock:
            self.requests += 1
        return self.fetcher.get(url, timeout=self.opts.timeout, max_bytes=max_bytes)

    # —— 校验值 ——
    def _checksum(self, entry: Entry, digest: str,
                  state: "State | None" = None) -> tuple[str, str, str]:
        """比对来源校验值。**三种失败必须分开**：

        - 来源没有校验值文件（404）→ `no_checksum_source`，可算完成但须标注；
        - 校验值**取不到**（限流/网络）→ `checksum_fetch_failed`，**待复核、不算完成**；
        - 取到但不一致 → `mismatch`，**不得完成**。

        校验端点与数据端点**同一个 host**，限流时 `RateLimited` 直接上抛，
        由调用方登记暂停并停止本轮——绝不能被当成"来源无校验"吞掉。
        """
        if not entry.checksum_url:
            return CHECK_NO_SOURCE, "", "来源未提供校验值文件"
        try:
            resp = self._get(entry.checksum_url, max_bytes=4096, state=state)
        except NotFound:
            return CHECK_NO_SOURCE, "", "来源未发布校验值文件（404）"
        except (RateLimited, Paused):
            raise
        except (FetchError, ByteBudgetExceeded) as exc:
            return CHECK_FETCH_FAILED, "", f"校验值获取失败（临时，待复核）：{exc}"
        expect = parse_checksum_file(resp.content.decode("utf-8", "replace"))
        if not expect:
            return CHECK_FETCH_FAILED, "", "校验值文件无法解析（待复核）"
        if digest == expect:
            return CHECK_VERIFIED, digest, "来源校验值一致"
        return CHECK_MISMATCH, digest, f"校验值不符（本地 {digest[:12]} ≠ 来源 {expect[:12]}）"

    def recheck_checksum(self, entry: Entry, state: "State",
                         path: Path | None = None) -> dict[str, Any]:
        """只重取校验值（**不重下归档**），用于待复核项。

        `path` 省略时自动解析：优先正式文件，其次 `.pending`（校验曾被暂停拦下、
        字节已保留）。**校验通过且源是 `.pending` → 提升为正式文件**，
        因此"收到归档但当时不能校验"不会导致重下整份。
        """
        official = official_path_for(self.opts.out_dir, entry)
        pending = pending_path_for(self.opts.out_dir, entry)
        if path is not None:
            src = Path(path)
        else:
            src = official if official.exists() else pending
        if not src.exists():
            return self._record(entry, S_ANOMALY,
                                note="本地既无正式文件也无待复核字节，需重新下载")
        try:
            digest = sha256_file(src)
        except OSError as exc:
            return self._record(entry, S_ANOMALY, note=f"读取本地文件失败：{exc}")
        try:
            ck_status, ck_value, ck_note = self._checksum(entry, digest, state)
        except Paused as exc:
            # 别的路已登记暂停 → 本路连校验请求都不发，字节原样保留待复核
            return self._record(entry, S_PAUSED, bytes_=src.stat().st_size,
                                checksum=CHECK_FETCH_FAILED, path=str(src),
                                note=f"处于限流暂停，未发校验请求：{exc}；字节保留待复核")
        except RateLimited as exc:
            state.set_pause(host_of(entry.source_url), exc.until_ms,
                            f"校验端点限流：{exc}")
            return self._record(entry, S_PAUSED, bytes_=src.stat().st_size,
                                checksum=CHECK_FETCH_FAILED, path=str(src),
                                note=f"校验端点限流，已登记暂停：{exc}；字节保留待复核")
        if ck_status == CHECK_MISMATCH:
            return self._record(entry, S_ANOMALY, bytes_=src.stat().st_size,
                                checksum=ck_status, checksum_value=ck_value,
                                note=f"{ck_note}；本地文件保留待查", path=str(src))
        if ck_status in CHECK_CONCLUSIVE and src != official:
            # 校验通过 → 把待复核字节**提升**为正式文件（不重下整份）。
            # 来源无校验值时要先确认内容确实是可用归档，避免把坏字节"转正"。
            if ck_status == CHECK_NO_SOURCE:
                ok, why = inspect_content(entry, src.read_bytes())
                if not ok:
                    return self._record(entry, S_ANOMALY, bytes_=src.stat().st_size,
                                        checksum=ck_status, checksum_value=ck_value,
                                        note=f"内容异常：{why}；字节保留待查", path=str(src))
            official.parent.mkdir(parents=True, exist_ok=True)
            src.replace(official)
            src = official
        status = S_COMPLETE if ck_status in CHECK_CONCLUSIVE else S_UNVERIFIED
        return self._record(entry, status, bytes_=src.stat().st_size,
                            checksum=ck_status, checksum_value=ck_value,
                            note=ck_note, path=str(src))

    # —— 写盘：原子 + 保留旧版本 ——
    def _write(self, entry: Entry, content: bytes, checksum: str) -> Path:
        target = self.opts.out_dir / entry.relpath()
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_name(target.name + ".part")
        part.write_bytes(content)
        if target.exists():
            if self.opts.force:
                # 强制重下：旧资料改名保留，不覆盖
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                keep = target.with_name(f"{target.name}.stale-{stamp}")
                target.replace(keep)
            else:
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                target.replace(target.with_name(f"{target.name}.stale-{stamp}"))
        part.replace(target)
        return target

    def _write_pending(self, entry: Entry, content: bytes) -> Path:
        """**校验暂时做不了**时保留原始字节（`.pending`），不进正式路径。

        这样"已收到归档、只差校验"不必重下整份；校验通过后由
        `recheck_checksum()` 提升为正式文件。
        """
        pending = pending_path_for(self.opts.out_dir, entry)
        pending.parent.mkdir(parents=True, exist_ok=True)
        tmp = pending.with_name(pending.name + ".tmp")
        tmp.write_bytes(content)
        tmp.replace(pending)
        return pending

    # —— 单份下载 ——
    def fetch_one(self, entry: Entry, budget: Budget, state: State) -> dict[str, Any]:
        host = host_of(entry.source_url)
        attempts = 0
        last_error = ""
        while attempts < max(1, self.opts.retries + 1):
            attempts += 1
            try:
                resp = self._get(entry.source_url, state=state,
                                 max_bytes=budget.remaining_bytes())
            except Paused as exc:
                # 暂停期（可能由**别的路**登记）→ 一次请求都不发，直接停手
                return self._record(entry, S_PAUSED, attempts=attempts,
                                    checksum=CHECK_NA,
                                    note=f"处于限流暂停，未发请求：{exc}")
            except NotFound as exc:
                return self._record(entry, S_NOT_FOUND, note=f"本次未找到（{exc}）",
                                    attempts=attempts, checksum=CHECK_NA)
            except RateLimited as exc:
                state.set_pause(host, exc.until_ms, str(exc))
                return self._record(entry, S_PAUSED, attempts=attempts,
                                    checksum=CHECK_NA,
                                    note=f"已登记暂停：{exc}；本次停止后续请求")
            except ByteBudgetExceeded as exc:
                return self._record(entry, S_BUDGET, attempts=attempts,
                                    checksum=CHECK_NA, note=str(exc))
            except FetchError as exc:
                last_error = str(exc)
                if attempts <= self.opts.retries:
                    self._sleep(self.opts.backoff_sec * attempts)
                    continue
                return self._record(entry, S_TRANSIENT, attempts=attempts,
                                    checksum=CHECK_NA, note=last_error)
            else:
                break
        content = resp.content
        budget.spend(len(content))
        budget.count_file()

        try:
            ck_status, ck_value, ck_note = self._checksum(entry, sha256_hex(content), state)
        except Paused as exc:
            # **归档已经到手**，只是别的路登记了暂停 → 原始字节必须保留到 .pending，
            # 记 `checksum_fetch_failed`（待复核，**不算完成**），恢复后只补校验不重下整份
            path = self._write_pending(entry, content)
            return self._record(entry, S_PAUSED, bytes_=len(content), attempts=attempts,
                                checksum=CHECK_FETCH_FAILED, path=str(path),
                                note=f"处于限流暂停，未发校验请求：{exc}；"
                                     f"归档字节已保留待复核，恢复后只补校验")
        except RateLimited as exc:
            # 校验端点被封：与数据端点共用暂停状态，本次立即停手；归档字节同样保留
            state.set_pause(host, exc.until_ms, f"校验端点限流：{exc}")
            path = self._write_pending(entry, content)
            return self._record(entry, S_PAUSED, bytes_=len(content), attempts=attempts,
                                checksum=CHECK_FETCH_FAILED, path=str(path),
                                note=f"校验端点限流，已登记暂停：{exc}；"
                                     f"归档字节已保留待复核，恢复后只补校验")

        if ck_status == CHECK_MISMATCH:
            part = self.opts.out_dir / entry.relpath()
            part.parent.mkdir(parents=True, exist_ok=True)
            part.with_name(part.name + ".part").write_bytes(content)
            return self._record(entry, S_ANOMALY, bytes_=len(content), attempts=attempts,
                                checksum=ck_status, checksum_value=ck_value, note=ck_note)

        ok, note = inspect_content(entry, content)
        if not ok:
            part = self.opts.out_dir / entry.relpath()
            part.parent.mkdir(parents=True, exist_ok=True)
            part.with_name(part.name + ".part").write_bytes(content)
            return self._record(entry, S_ANOMALY, bytes_=len(content), attempts=attempts,
                                checksum=ck_status, checksum_value=ck_value,
                                note=f"内容异常：{note}；原始字节存为 .part")

        path = self._write(entry, content, ck_value)
        # **只在暂停已到期时**才解除：否则一个晚到的成功会把别的路刚登记的
        # 未到期暂停抹掉，限流保护失效（多路限流交错的核心 bug）
        state.clear_pause_if_expired(host)
        # 这一次真的下成功了 → 说明来源已经可用，本地退避档位归零
        # （下次万一再限流，从最短档重新开始，而不是接着上次的 4 小时）
        state.reset_backoff(host)
        # 归档内容有效，但来源校验值暂时取不到 → **不算完成**，下次只补校验不重下
        status = S_SUCCESS if ck_status in CHECK_CONCLUSIVE else S_UNVERIFIED
        return self._record(entry, status, bytes_=len(content), attempts=attempts,
                            checksum=ck_status, checksum_value=ck_value,
                            note=f"{note}；{ck_note}", path=str(path))

    def _record(self, entry: Entry, status: str, **kw: Any) -> dict[str, Any]:
        rec = {"key": entry.key, "status": status, "market": entry.market,
               "dataset": entry.dataset, "symbol": entry.symbol,
               "interval": entry.interval, "period_start": entry.period_start,
               "period_end": entry.period_end, "source_url": entry.source_url,
               "bytes": int(kw.pop("bytes_", 0) or 0),
               "fetched_at_utc": utcnow_iso(),
               "checksum_status": kw.pop("checksum", CHECK_NA),
               "checksum_sha256": kw.pop("checksum_value", ""),
               "attempts": int(kw.pop("attempts", 0) or 0),
               "path": kw.pop("path", ""),
               "note": kw.pop("note", "")}
        return rec


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def _simple_record(entry: Entry, status: str, note: str) -> dict[str, Any]:
    """未真正发起请求的条目也要有完整状态行（便于对账：总数必须守恒）。"""
    return {"key": entry.key, "status": status, "market": entry.market,
            "dataset": entry.dataset, "symbol": entry.symbol,
            "interval": entry.interval, "period_start": entry.period_start,
            "period_end": entry.period_end, "source_url": entry.source_url,
            "bytes": 0, "fetched_at_utc": "", "checksum_status": CHECK_NA,
            "checksum_sha256": "", "attempts": 0, "path": "", "note": note}


def plan_rows(entries: Iterable[Entry], out_dir: Path, state: State,
              force: bool = False) -> list[dict[str, Any]]:
    rows = []
    for e in entries:
        path = out_dir / e.relpath()
        if path.exists() and not force:
            ok, why = verify_local(path, e, local_digest_for(state, e))
            status = S_COMPLETE if ok else "incomplete_local"
        else:
            ok, why = (False, "本地不存在")
            status = S_PENDING
        rows.append({"key": e.key, "status": status, "local_note": why,
                     "target": str(path), "bytes_expected": e.remote_size_bytes or "",
                     "source_url": e.source_url})
    return rows


def run_fetch(entries: list[Entry], opts: Options,
              fetcher: Any | None = None,
              sleeper: Callable[[float], None] = time.sleep) -> tuple[list[dict[str, Any]], str]:
    """返回 (每份结果, 汇总说明)。dry_run 时零网络请求。"""
    out_dir = Path(opts.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    opts.last_request_count = 0          # 每次调用重置，避免沿用上一块的计数
    state = State(opts.state_path or (out_dir / STATE_NAME),
                  compact_every=getattr(opts, "compact_every", 5000))

    free = shutil.disk_usage(out_dir).free
    if opts.min_free_gib and free < opts.min_free_gib * 2 ** 30:
        return [], (f"磁盘可用空间 {free / 2**30:.1f} GiB < 下限 "
                    f"{opts.min_free_gib} GiB，未开始下载")

    if opts.dry_run:
        rows = plan_rows(entries, out_dir, state, opts.force)
        return rows, f"dry-run：仅列出 {len(rows)} 项，未发任何请求"

    lock = DownloadLock(out_dir)
    locked, lock_msg = lock.acquire()
    if not locked:
        return [], lock_msg

    _REC_KEYS = ("status", "path", "bytes", "checksum_status", "checksum_sha256",
                 "attempts", "fetched_at_utc", "note")

    def _stash(e: Entry, rec: dict[str, Any]) -> None:
        state.set_record(e, **{k: rec.get(k, "") for k in _REC_KEYS})

    results: list[dict[str, Any]] = []
    try:
        # ① 先验证已存文件：有**已存来源摘要**就比摘要（不解压、不联网）
        todo: list[Entry] = []        # 需要下载
        recheck: list[Entry] = []     # 文件已在，只缺来源校验值
        for e in entries:
            path = out_dir / e.relpath()
            if opts.force:
                todo.append(e)
                continue
            if not path.exists():
                # 正式文件不在，但有**待复核字节**（校验曾被暂停拦下）→
                # 只补校验，绝不重下整份
                if pending_path_for(out_dir, e).exists():
                    recheck.append(e)
                else:
                    todo.append(e)
                continue
            expected = local_digest_for(state, e)
            ok, why = verify_local(path, e, expected)
            if not ok:
                todo.append(e)
                continue
            prev = state.records.get(e.key) or {}
            if prev.get("checksum_status") in CHECK_CONCLUSIVE:
                # 跳过时**保留**已确认的校验结论，不能被空值抹掉
                rec = {**_simple_record(e, S_COMPLETE, f"{why}；跳过"),
                       "bytes": path.stat().st_size, "path": str(path),
                       "checksum_status": prev.get("checksum_status", CHECK_NA),
                       "checksum_sha256": prev.get("checksum_sha256", ""),
                       "fetched_at_utc": prev.get("fetched_at_utc", "")}
                results.append(rec)
                _stash(e, rec)
            else:
                recheck.append(e)

        # ② 暂停期内不发任何请求
        host = host_of(entries[0].source_url) if entries else ""
        if host and state.is_paused(host):
            for e in todo + recheck:
                results.append(_simple_record(e, S_PAUSED, f"{host} 处于暂停期，未发请求"))
            return results, (f"共 {len(entries)} 项：本地完整 "
                             f"{len(entries) - len(todo) - len(recheck)}，"
                             f"因限流暂停未处理 {len(todo) + len(recheck)}")

        workers = max(1, int(getattr(opts, "workers", 1) or 1))
        pace_lock = threading.Lock()
        dl = Downloader(fetcher or RequestsFetcher(pool_size=workers), opts,
                        sleeper=sleeper, pace_lock=pace_lock)
        budget = Budget(opts.max_files, opts.max_bytes, opts.max_minutes)
        # 在途字节预算：并发拉高时防止大文件把内存吃爆
        gate = InflightGate(getattr(opts, "inflight_bytes_budget", 0))
        # 限流后**不再发新请求**：已发出的安全收尾，未开始的记为暂停
        stop_event = threading.Event()
        stopped_reason = ""
        stop_status = ""

        def _work(e: Entry, mode: str) -> dict[str, Any]:
            nonlocal stopped_reason, stop_status
            if stop_event.is_set():
                return _simple_record(e, S_PAUSED, "已触发限流暂停；未发请求")
            can, why = budget.can_start_file()
            if not can:
                stopped_reason, stop_status = why, S_BUDGET
                return _simple_record(e, S_BUDGET, why)
            lock.touch()
            if state.is_paused(host_of(e.source_url)):
                stopped_reason, stop_status = "限流暂停", S_PAUSED
                return _simple_record(e, S_PAUSED, "处于暂停期，未发请求")
            # ③ 补校验只取 CHECKSUM，**不重下归档**；④ 正常下载
            # 预算按"这一份预计要占多少内存"预扣：下载用队列里的大小，
            # 补校验只要一份很小的 CHECKSUM。
            reserved = gate.reserve(e.remote_size_bytes if mode == "download" else 4096)
            try:
                rec = (dl.recheck_checksum(e, state)
                       if mode == "recheck" else dl.fetch_one(e, budget, state))
            finally:
                gate.release(reserved)
            if rec["status"] == S_PAUSED:
                stopped_reason, stop_status = "限流暂停", S_PAUSED
                stop_event.set()
            _stash(e, rec)
            return rec

        # 补校验在前、下载在后；一旦停止（限流/预算），
        # **剩余每一项都要留下状态记录**，不能静默丢弃。
        jobs = ([(e, "recheck") for e in recheck]
                + [(e, "download") for e in todo])
        if workers <= 1 or len(jobs) <= 1:
            for job in jobs:
                results.append(_work(*job))
        else:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                for rec in ex.map(lambda j: _work(*j), jobs):
                    results.append(rec)
        opts.last_request_count = int(dl.requests)   # 供队列工具统计请求数
        state.save()
    finally:
        lock.release()

    counts: dict[str, int] = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    total_bytes = sum(r["bytes"] for r in results if r["status"] == S_SUCCESS)
    summary = (f"共 {len(entries)} 项 | 本地完整 {counts.get(S_COMPLETE, 0)} | "
               f"新下成功 {counts.get(S_SUCCESS, 0)} | 待复核 {counts.get(S_UNVERIFIED, 0)} | "
               f"未找到 {counts.get(S_NOT_FOUND, 0)} | "
               f"限流暂停 {counts.get(S_PAUSED, 0)} | 网络失败 {counts.get(S_TRANSIENT, 0)} | "
               f"内容异常 {counts.get(S_ANOMALY, 0)} | 预算停止 {counts.get(S_BUDGET, 0)} | "
               f"成功字节 {total_bytes:,}")
    if stopped_reason:
        summary += f" | 停止原因：{stopped_reason}"
    return results, summary


def run_verify(entries: list[Entry], opts: Options,
               fetcher: Any | None = None,
               sleeper: Callable[[float], None] = time.sleep) -> list[dict[str, Any]]:
    """复核本地库：重算本地 sha256 并与来源 CHECKSUM 比对。**不下载归档**。"""
    out_dir = Path(opts.out_dir)
    state = State(opts.state_path or (out_dir / STATE_NAME))
    dl = Downloader(fetcher or RequestsFetcher(), opts, sleeper=sleeper)
    rows: list[dict[str, Any]] = []
    for e in entries:
        path = out_dir / e.relpath()
        if not path.exists():
            rows.append(_simple_record(e, S_PENDING, "本地不存在，未验证"))
            continue
        size = path.stat().st_size
        digest = sha256_file(path)
        ok, why = verify_local(path, e, local_digest_for(state, e))
        try:
            ck_status, ck_value, ck_note = dl._checksum(e, digest, state)  # noqa: SLF001
        except Paused as exc:
            rows.append(_simple_record(e, S_PAUSED, f"处于限流暂停，未发校验请求：{exc}"))
            continue
        except RateLimited as exc:
            state.set_pause(host_of(e.source_url), exc.until_ms, f"复核时校验端点限流：{exc}")
            rows.append(_simple_record(e, S_PAUSED, f"校验端点限流：{exc}"))
            continue
        if not ok or ck_status == CHECK_MISMATCH:
            status = S_ANOMALY
        elif ck_status in CHECK_CONCLUSIVE:
            status = S_COMPLETE
        else:
            status = S_UNVERIFIED   # 校验值取不到 → 待复核，不算完成
        note = f"{why}；{ck_note}"
        state.set_record(e, status=status, path=str(path), bytes=size,
                         checksum_status=ck_status, checksum_sha256=ck_value,
                         note=note)
        rows.append({"key": e.key, "status": status, "market": e.market,
                     "dataset": e.dataset, "symbol": e.symbol,
                     "interval": e.interval, "period_start": e.period_start,
                     "period_end": e.period_end, "source_url": e.source_url,
                     "bytes": size,
                     "fetched_at_utc": state.records.get(e.key, {}).get("fetched_at_utc", ""),
                     "checksum_status": ck_status, "checksum_sha256": ck_value,
                     "attempts": 0, "path": str(path), "note": note})
    state.save(force=True)
    return rows


STATUS_COLS = ["key", "status", "market", "dataset", "symbol", "interval",
               "period_start", "period_end", "bytes", "fetched_at_utc",
               "checksum_status", "checksum_sha256", "attempts", "path", "note",
               "source_url"]


def status_rows_from_state(entries: Iterable[Entry], state: State) -> list[dict[str, Any]]:
    """导出**累计**状态：每份文件取它最好的一次结果。

    `State.set_record` 用 update 合并，所以后一轮的 `skipped_complete` 不会
    抹掉前一轮 success 记下的字节数、校验值和获取时间。
    """
    rows: list[dict[str, Any]] = []
    for e in entries:
        rec = state.records.get(e.key)
        base: dict[str, Any] = {"key": e.key, "market": e.market,
                                "dataset": e.dataset, "symbol": e.symbol,
                                "interval": e.interval,
                                "period_start": e.period_start,
                                "period_end": e.period_end,
                                "source_url": e.source_url}
        if rec:
            for k in ("status", "bytes", "fetched_at_utc", "checksum_status",
                      "checksum_sha256", "attempts", "path", "note"):
                base[k] = rec.get(k, "")
        else:
            base.update({"status": S_PENDING, "bytes": 0, "fetched_at_utc": "",
                         "checksum_status": CHECK_NA, "checksum_sha256": "",
                         "attempts": 0, "path": "", "note": "未处理"})
        rows.append(base)
    return rows


def write_status_csv(results: list[dict[str, Any]], path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=STATUS_COLS, extrasaction="ignore")
        w.writeheader()
        for r in results:
            w.writerow(r)
    return p


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="可靠归档下载器（只读公开归档，不碰策略）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--manifest", required=True, help="清单 CSV/JSON")
        p.add_argument("--out-dir", default=str(DEFAULT_OUT))
        p.add_argument("--state", default="")
        p.add_argument("--status-csv", default=str(DEFAULT_STATUS_CSV))

    p_plan = sub.add_parser("plan", help="只列计划，不发任何请求")
    common(p_plan)
    p_plan.add_argument("--force", action="store_true")

    p_verify = sub.add_parser("verify", help="复核本地库与来源校验值，不下载归档")
    common(p_verify)
    p_verify.add_argument("--interval-sec", type=float, default=1.0)
    p_verify.add_argument("--timeout", type=float, default=60.0)

    p_fetch = sub.add_parser("fetch", help="执行下载（受预算约束）")
    common(p_fetch)
    p_fetch.add_argument("--max-files", type=int, default=30, help="0=不限")
    p_fetch.add_argument("--max-bytes", type=int, default=100 * 1024 * 1024, help="0=不限")
    p_fetch.add_argument("--max-minutes", type=float, default=15.0, help="0=不限")
    p_fetch.add_argument("--min-free-gib", type=float, default=5.0)
    p_fetch.add_argument("--interval-sec", type=float, default=1.0)
    p_fetch.add_argument("--timeout", type=float, default=60.0)
    p_fetch.add_argument("--retries", type=int, default=2)
    p_fetch.add_argument("--dry-run", action="store_true")
    p_fetch.add_argument("--force", action="store_true")
    p_fetch.add_argument("--workers", type=int, default=1,
                         help="并发路数；1=单连接（旧行为）。多路共享总节奏/预算/暂停")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    entries = load_manifest(args.manifest)
    out_dir = Path(args.out_dir)
    state_path = Path(args.state) if args.state else None
    print(f"清单 {args.manifest}：{len(entries)} 项 | 输出 {out_dir}")

    if args.cmd == "plan":
        state = State(state_path or (out_dir / STATE_NAME))
        rows = plan_rows(entries, out_dir, state, force=args.force)
        counts: dict[str, int] = {}
        for r in rows:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        for r in rows:
            print(f"  [{r['status']:<16}] {r['target']}")
        print(f"[plan] {counts}（未发任何请求）")
        return 0

    if args.cmd == "verify":
        vopts = Options(out_dir=out_dir, state_path=state_path,
                        interval_sec=args.interval_sec, timeout=args.timeout)
        rows = run_verify(entries, vopts)
        write_status_csv(rows, args.status_csv)
        counts: dict[str, int] = {}
        for r in rows:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        verified = sum(1 for r in rows if r["checksum_status"] == CHECK_VERIFIED)
        print(f"状态表（复核）-> {args.status_csv}")
        print(f"[verify] {counts} | 来源校验值一致 {verified}/{len(rows)} | 未下载任何归档")
        return 0

    opts = Options(out_dir=out_dir, state_path=state_path,
                   status_csv=Path(args.status_csv),
                   max_files=args.max_files, max_bytes=args.max_bytes,
                   max_minutes=args.max_minutes, min_free_gib=args.min_free_gib,
                   interval_sec=args.interval_sec, timeout=args.timeout,
                   retries=args.retries, force=args.force, dry_run=args.dry_run,
                   workers=args.workers)
    results, summary = run_fetch(entries, opts)
    if results and args.status_csv:
        # 导出累计状态（不是只导本轮），避免后一轮 skipped_complete 抹掉校验信息
        state = State(state_path or (out_dir / STATE_NAME))
        write_status_csv(status_rows_from_state(entries, state), args.status_csv)
        print(f"状态表（累计）-> {args.status_csv}")
    print(f"[fetch] {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
