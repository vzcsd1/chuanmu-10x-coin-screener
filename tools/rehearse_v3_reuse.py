#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""在**沙盒**里预演真实纳管场景（不碰生产目录、不联网、不下载）。

复刻生产的三件事，验证"换 v3 清单 + 开 --reuse-parts"确实能一次纳管：

  1. 清单记**旧大小**、本地 `.part` 是**新大小** → 旧清单下判异常；
  2. 换成 v3 清单（尺寸=当前官方）→ 复用 `.part` 应转正，**且不发 ZIP 请求**；
  3. 转正后再跑一次 → 零请求、状态不回退。

用法：py -3.10 tools/rehearse_v3_reuse.py
"""
from __future__ import annotations

import io
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))

import archive_download_ds as ad  # noqa: E402

FIXED = (2024, 1, 1, 0, 0, 0)


def make_zip(member: str, pad: int) -> bytes:
    """`pad` 只是**大致**撑体积；真实字节数含 zip 头尾开销，
    所以下面一律用 `len(real)` 当"当前官方大小"，不要拿 pad 直接比。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(zipfile.ZipInfo(member, date_time=FIXED),
                   "open_time,open,high,low,close,volume,close_time,quote_volume,"
                   "trades,taker_buy_base,taker_buy_quote,ignore\n"
                   "1704067200000,1,2,0.5,1.5,10,0,10,1,5,5,0\n")
        if pad:
            z.writestr(zipfile.ZipInfo("padding.bin", date_time=FIXED),
                       b"x" * pad, compress_type=zipfile.ZIP_STORED)
    return buf.getvalue()


class CountingFetcher:
    def __init__(self, routes):
        self.routes = dict(routes)
        self.calls = []

    def get(self, url, *, timeout=60.0, max_bytes=None):
        self.calls.append(url)
        if url not in self.routes:
            raise ad.NotFound(f"HTTP 404: {url}")
        v = self.routes[url]
        return ad.Resp(200, {}, v() if callable(v) else v)


def entry(url, size):
    return ad.Entry(source_url=url, market="futures", dataset="metrics",
                    symbol="0GUSDT", interval="", period_start="2026-06-28",
                    period_end="2026-06-28", remote_size_bytes=size,
                    checksum_url=url + ".CHECKSUM",
                    discovered_at_utc="2026-10-06T00:00:00Z")


def opts(tmp, out, **kw):
    base = dict(out_dir=out, state_path=out / "_state.json", status_csv=None,
                max_files=0, max_bytes=0, max_minutes=0, min_free_gib=0,
                interval_sec=0, timeout=5, retries=0)
    base.update(kw)
    return ad.Options(**base)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    # 真实情形：清单记旧大小，当前官方/本地字节更大（差值 100% 为正）
    url = "https://data.binance.vision/data/futures/um/daily/metrics/0GUSDT/0GUSDT-metrics-2026-06-28.zip"
    real = make_zip("0GUSDT-metrics-2026-06-28.csv", pad=10000)
    new_size = len(real)                # 当前官方大小 = 本地字节（全量核验结论）
    old_size = new_size - 326           # 清单里记的旧大小（差 326，与实测中位一致）
    digest = ad.sha256_hex(real)
    print(f"构造：旧清单={old_size}  当前官方=本地={new_size}  差={new_size-old_size}")

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        out = tmp / "out"

        # —— 场景 1：旧清单 + 开复用 → 仍判异常（复用了但校验不过，不转正）——
        e_old = entry(url, old_size)
        part = ad.part_path_for(out, e_old)
        part.parent.mkdir(parents=True, exist_ok=True)
        part.write_bytes(real)
        f1 = CountingFetcher({url + ".CHECKSUM": f"{digest}  x.zip\n".encode()})
        o1 = opts(tmp, out, reuse_parts=True)
        r1, _ = ad.run_fetch([e_old], o1, fetcher=f1, sleeper=lambda s: None)
        zip_hits1 = [u for u in f1.calls if u == url]
        print(f"[场景1 旧清单+复用] 状态={r1[0]['status']:<16} "
              f"ZIP请求={len(zip_hits1)}  期望=content_anomaly/0")
        s1_ok = r1[0]["status"] == ad.S_ANOMALY and not zip_hits1

        # —— 场景 2：v3 清单（尺寸=官方）+ 开复用 → 转正，零 ZIP 请求 ——
        part.write_bytes(real)            # 复位（场景1 未消费）
        e_new = entry(url, new_size)
        f2 = CountingFetcher({url + ".CHECKSUM": f"{digest}  x.zip\n".encode()})
        o2 = opts(tmp, out, reuse_parts=True)
        o2.state_path = o1.state_path
        r2, _ = ad.run_fetch([e_new], o2, fetcher=f2, sleeper=lambda s: None)
        zip_hits2 = [u for u in f2.calls if u == url]
        official = out / e_new.relpath()
        print(f"[场景2 v3清单+复用] 状态={r2[0]['status']:<16} "
              f"ZIP请求={len(zip_hits2)}  正式文件={official.exists()}  期望=success/0/True")
        s2_ok = (r2[0]["status"] == ad.S_SUCCESS and not zip_hits2
                 and official.exists())

        # —— 场景 3：再跑一次 → 零请求、不回退 ——
        f3 = CountingFetcher({})
        o3 = opts(tmp, out, reuse_parts=True)
        o3.state_path = o1.state_path
        r3, _ = ad.run_fetch([e_new], o3, fetcher=f3, sleeper=lambda s: None)
        print(f"[场景3 重跑]        状态={r3[0]['status']:<16} "
              f"总请求={len(f3.calls)}  摘要保留={r3[0]['checksum_sha256'] == digest}  "
              f"期望=skipped_complete/0/True")
        s3_ok = (r3[0]["status"] == ad.S_COMPLETE and not f3.calls
                 and r3[0]["checksum_sha256"] == digest)

    ok = s1_ok and s2_ok and s3_ok
    print()
    print("预演结论：", "全部符合预期 —— 可纳管" if ok else "存在不符合项，先别纳管")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
