#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全量核验 content_anomaly 集合（只读，不修改队列/台账/文件）。

对每一项独立核对：
  1. 身份：state key 是否能唯一映射到磁盘 .part 路径（且文件存在）
  2. ZIP 结构：能否作为 zip 打开、testzip、内有 CSV
  3. 本地 SHA256
  4. 当前官方 CHECKSUM（.CHECKSUM 文件里的哈希）
  5. 大小证据：本地实际大小 vs 清单 remote_size_bytes vs 官方 HEAD Content-Length

注意：CHECKSUM 文件**只含哈希，不含大小**；大小需另取 HEAD。
输出：reports/archive_download_ds_full/anomaly_full_audit.csv + 控制台汇总。
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import sys
import time
import zipfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
import archive_download_ds as ad  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "data" / "archive_raw" / "_state.json"
OUTDIR = ROOT / "data" / "archive_raw"
QUEUE = ROOT / "reports" / "archive_download_ds_full" / "queue_v2.csv"
REPORT = ROOT / "reports" / "archive_download_ds_full" / "anomaly_full_audit.csv"

UA = {"User-Agent": "chuanmu-anomaly-audit/0.1"}


def safe_name(symbol: str, dataset: str, period: str) -> str:
    """文件名与 Entry.filename 同构；文件名本身不做转义（只转目录层）。"""
    return f"{symbol}-{dataset}-{period}"


def http_get(url: str, timeout: float = 25.0) -> bytes:
    req = Request(quote_url(url), headers=UA)
    with urlopen(req, timeout=timeout) as r:
        return r.read()


def quote_url(url: str) -> str:
    """URL 里可能有中文交易对（如 币安人生USDT），urllib 只接受 ASCII。
    只对 path 段做百分号编码，不动 scheme/host 与已有转义。"""
    from urllib.parse import quote, urlsplit, urlunsplit
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, quote(p.path, safe="/%"), p.query, p.fragment))


def http_head_len(url: str, timeout: float = 25.0) -> int | None:
    """取官方对象当前大小。CHECKSUM 不含大小，必须单独取。"""
    req = Request(quote_url(url), headers=UA, method="HEAD")
    try:
        with urlopen(req, timeout=timeout) as r:
            cl = r.headers.get("Content-Length")
            return int(cl) if cl else None
    except (HTTPError, URLError, OSError):
        return None


def load_anomaly() -> list[tuple[str, dict]]:
    with STATE.open(encoding="utf-8") as f:
        d = json.load(f)
    return [(k, v) for k, v in d["records"].items()
            if isinstance(v, dict) and v.get("status") == "content_anomaly"]


def load_queue_index() -> dict[str, dict]:
    """清单索引：source_url -> 行。用于取 remote_size_bytes 与 checksum_url。"""
    idx: dict[str, dict] = {}
    with QUEUE.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            idx[row["source_url"]] = row
    return idx


def main() -> int:
    rows = load_anomaly()
    qidx = load_queue_index()
    print(f"异常总数 {len(rows)}；清单索引 {len(qidx)} 项")

    out: list[dict] = []
    stat = {"ok": 0, "mismatch": 0, "missing": 0, "badzip": 0,
            "no_ck": 0, "no_part": 0, "net_err": 0}

    for i, (key, rec) in enumerate(sorted(rows), 1):
        parts = key.split("|")
        # key 结构：market|dataset|symbol|interval|period_start|period_end
        market, dataset, symbol = parts[0], parts[1], parts[2]
        interval = parts[3] if len(parts) > 3 else ""
        period = parts[4] if len(parts) > 4 else ""
        src_url = (f"https://data.binance.vision/data/{market}/um/daily/{dataset}/"
                   f"{symbol}/{symbol}-{dataset}-{period}.zip")
        # 复用 .part：路径 == out_dir / entry.relpath() + ".part"
        # 直接调用 downloader 自己的 Entry.relpath()，避免重实现 safe_seg 的转义规则
        # （中文交易对如 币安人生USDT 的目录会被转义并附 sha1 摘要，自己拼会找不到）。
        _e = ad.Entry(
            source_url=src_url, market=market, dataset=dataset, symbol=symbol,
            interval=interval, period_start=period, period_end=period,
            remote_size_bytes=None, checksum_url=src_url + ".CHECKSUM",
            discovered_at_utc="",
        )
        rel = _e.relpath()
        part_path = OUTDIR / rel.parent / (rel.name + ".part")

        row: dict = {
            "key": key, "market": market, "dataset": dataset, "symbol": symbol,
            "period": period, "src_url": src_url,
            "part_path": str(part_path.relative_to(ROOT)) if part_path else "",
            "list_bytes": (qidx.get(src_url) or {}).get("remote_size_bytes", ""),
            "state_bytes": rec.get("bytes", ""),
            "note": str(rec.get("note", ""))[:60],
        }

        # 1) 身份 + 存在性
        if not part_path.exists():
            row["verdict"] = "PART_MISSING"
            stat["no_part"] += 1
            out.append(row)
            continue
        local_size = part_path.stat().st_size
        row["local_bytes"] = local_size

        # 2) ZIP 结构
        try:
            blob = part_path.read_bytes()
            with zipfile.ZipFile(io.BytesIO(blob)) as z:
                bad = z.testzip()
                names = z.namelist()
                csvs = [n for n in names if n.lower().endswith(".csv")]
            row["zip_ok"] = (bad is None)
            row["zip_members"] = len(names)
            row["zip_csv"] = csvs[0] if csvs else ""
            if bad is not None or not csvs:
                row["verdict"] = "ZIP_STRUCT_BAD"
                stat["badzip"] += 1
                out.append(row)
                continue
        except Exception as exc:  # noqa: BLE001
            row["zip_ok"] = False
            row["verdict"] = f"ZIP_OPEN_FAIL:{type(exc).__name__}"
            stat["badzip"] += 1
            out.append(row)
            continue

        # 3) 本地 SHA256
        local_sha = hashlib.sha256(blob).hexdigest()
        row["local_sha256"] = local_sha

        # 4) 当前官方 CHECKSUM（只含哈希）
        ck_url = src_url + ".CHECKSUM"
        row["checksum_url"] = ck_url
        try:
            txt = http_get(ck_url).decode("utf-8", "replace")
            off_sha = txt.split()[0] if txt.strip() else ""
        except HTTPError as exc:
            off_sha = ""
            row["ck_err"] = f"HTTP {exc.code}"
            stat["net_err"] += 1
        except (URLError, OSError) as exc:
            off_sha = ""
            row["ck_err"] = type(exc).__name__
            stat["net_err"] += 1
        row["official_sha256"] = off_sha
        if not off_sha:
            stat["no_ck"] += 1

        # 5) 官方当前大小（CHECKSUM 不提供，单独 HEAD）
        off_len = http_head_len(src_url)
        row["official_bytes"] = off_len if off_len is not None else ""

        # 判定
        if off_sha and local_sha == off_sha:
            row["verdict"] = "SHA_MATCH"
            stat["ok"] += 1
            if off_len is not None and off_len != local_size:
                row["verdict"] = "SHA_MATCH_BUT_SIZE_DIFF"
        elif off_sha:
            row["verdict"] = "SHA_MISMATCH"
            stat["mismatch"] += 1
        else:
            row["verdict"] = "NO_OFFICIAL_CK"

        out.append(row)
        if i % 100 == 0:
            print(f"  进度 {i}/{len(rows)} … 已ok={stat['ok']} "
                  f"mismatch={stat['mismatch']} net_err={stat['net_err']}")
            sys.stdout.flush()
        time.sleep(0.03)

    # 输出
    cols = ["key", "market", "dataset", "symbol", "period", "verdict",
            "local_bytes", "state_bytes", "list_bytes", "official_bytes",
            "zip_ok", "zip_members", "zip_csv", "local_sha256", "official_sha256",
            "src_url", "checksum_url", "part_path", "note", "ck_err"]
    with REPORT.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in out:
            w.writerow(r)

    print()
    print("=== 全量核验汇总 ===")
    for k, v in stat.items():
        print(f"  {k:14s} {v}")
    print(f"\n明细已写入 {REPORT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
