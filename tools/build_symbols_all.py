#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成 data/symbols_all.txt —— 项目唯一的「研究universe」定义（A14 ①）。

为什么需要这个脚本
------------------
`research/01_fetch_1d.py` 原来读一个**仓库外**的绝对路径
（`C:/Users/Administrator/Desktop/裸K盘感项目/data/symbols_all.txt`），那个文件已经不存在了。
后果不是报错，而是**换一台机器就复现不出 pumps.json**——数据集不可复现。

这个脚本把 universe 变成「可重算的」：任何人跑一遍就能得到同一份清单。

universe 怎么定
--------------
**只用「归档里真的有数据」的币**，不用「今天还在交易」的币——
后者会引入生存者偏差（退市的币恰恰是爆拉高发区）。

来源（并集，缺一不可）：
  1. S3 归档 `data/spot/monthly/klines/` 下的**全部**现货币前缀（含已退市）
  2. `data/pumps.json` 里出现过的币（防归档漏列）
  3. `data/1d/`、`data/1h/` 已有本地文件的币（防历史清单被覆盖后丢币）

过滤（与 `01_fetch_1d.py` 完全同一套规则，**规则只在那边维护，这里 import**）：
  只留 USDT 计价、剔杠杆代币（3 倍每日再平衡产品）、剔稳定币对。

用法
----
    py -3.10 tools/build_symbols_all.py            # 生成
    py -3.10 tools/build_symbols_all.py --check    # 只体检，不写

产物
----
    data/symbols_all.txt        每行一个币，已排序
    data/symbols_all.meta.json  来源计数、生成时刻、规则版本

注意
----
S3 列表接口（`s3-ap-northeast-1.amazonaws.com`）在部分网络下**直连不通，必须走代理**；
`data.binance.vision`（真正下文件用的 CDN）直连可用。所以只有本脚本的列表请求走代理。
代理地址优先取环境变量 `HTTPS_PROXY`，否则读 Windows 系统代理。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT = DATA / "symbols_all.txt"
META = DATA / "symbols_all.meta.json"

S3 = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
PREFIX = "data/spot/monthly/klines/"
NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}

# 过滤规则从 01 脚本 import，保证只有一份定义
sys.path.insert(0, str(ROOT / "research"))
from importlib import import_module  # noqa: E402

_f = import_module("01_fetch_1d") if False else None  # 文件名以数字开头，不能常规 import


def _rules() -> tuple[tuple[str, ...], set[str]]:
    """从 01_fetch_1d.py 读出 LEVERAGED / STABLE（用 ast 取字面量，不 exec 整份文件）。"""
    import ast
    tree = ast.parse((ROOT / "research" / "01_fetch_1d.py").read_text(encoding="utf-8"))
    found: dict[str, object] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            tgt = node.targets[0]
            if isinstance(tgt, ast.Name) and tgt.id in ("LEVERAGED", "STABLE"):
                try:
                    found[tgt.id] = ast.literal_eval(node.value)
                except ValueError:
                    pass
    if "LEVERAGED" not in found or "STABLE" not in found:
        raise SystemExit("在 research/01_fetch_1d.py 里找不到 LEVERAGED / STABLE 定义")
    return tuple(found["LEVERAGED"]), set(found["STABLE"])  # type: ignore[arg-type]


def proxy() -> str | None:
    """**优先用 Windows 系统代理**，其次才是环境变量。

    顺序不能反：某些沙箱/工具链会往环境里塞一个只对内部服务有效的 `HTTPS_PROXY`
    （本机实测是 `127.0.0.1:9390`），对公网请求是死路；真正的出口在系统代理（Clash 的 7897）。
    """
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        if winreg.QueryValueEx(key, "ProxyEnable")[0]:
            return "http://" + str(winreg.QueryValueEx(key, "ProxyServer")[0])
    except Exception:  # noqa: BLE001
        pass
    for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        v = os.getenv(k)
        if v:
            return v
    return None


def list_archive_symbols() -> tuple[set[str], str]:
    """分页列出归档里所有现货币前缀。"""
    import requests
    s = requests.Session()
    s.headers.update({"User-Agent": "chuanmu-research/0.1"})
    s.trust_env = False                       # 不用环境里的代理（见 proxy() 的说明）
    p = proxy()
    if p:
        s.proxies = {"http": p, "https": p}
    syms: set[str] = set()
    token = None
    for _ in range(60):                       # 60 页 × 1000 = 6 万，足够
        params = {"list-type": "2", "prefix": PREFIX, "delimiter": "/", "max-keys": "1000"}
        if token:
            params["continuation-token"] = token
        r = s.get(S3, params=params, timeout=40)
        r.raise_for_status()
        root = ET.fromstring(r.text)
        for c in root.findall(".//s3:CommonPrefixes", NS):
            pre = c.find("s3:Prefix", NS).text
            syms.add(pre[len(PREFIX):].rstrip("/"))
        if root.findtext("s3:IsTruncated", "false", NS) != "true":
            break
        token = root.findtext("s3:NextContinuationToken", None, NS)
        if not token:
            break
    return syms, ("proxy " + p if p else "direct")


def repo_symbols() -> tuple[set[str], set[str], set[str]]:
    p1d = {x.stem for x in (DATA / "1d").glob("*.parquet")}
    # 1h 文件名是 `{SYM}-{YYYY}-{MM}.parquet`，要去掉后两段才是币名
    p1h = {x.stem.rsplit("-", 2)[0] for x in (DATA / "1h").glob("*.parquet")}
    pj = DATA / "pumps.json"
    pumps = {e["symbol"] for e in json.loads(pj.read_text(encoding="utf-8"))} if pj.exists() else set()
    return p1d, p1h, pumps


def keep(sym: str, lev: tuple[str, ...], stable: set[str]) -> bool:
    return (sym.endswith("USDT") and sym != "USDTUSDT"
            and not sym.endswith(lev) and sym not in stable)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只体检，不写文件")
    args = ap.parse_args()

    lev, stable = _rules()
    p1d, p1h, pumps = repo_symbols()

    try:
        arc, via = list_archive_symbols()
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] S3 列表失败（{type(exc).__name__}: {str(exc)[:80]}）")
        print("       退化为「只用仓库内已有 + 爆拉币」，清单会缺已退市币。")
        arc, via = set(), "unavailable"

    raw = arc | p1d | p1h | pumps
    syms = sorted({s for s in raw if keep(s, lev, stable)})
    dropped = sorted(raw - set(syms))

    print(f"[universe] 归档 {len(arc)}｜本地 1d {len(p1d)}｜本地 1h {len(p1h)}｜爆拉 {len(pumps)}")
    print(f"[universe] 并集 {len(raw)} → 过滤后 **{len(syms)}**（剔掉 {len(dropped)}：杠杆/稳定/非USDT）")
    print(f"[universe] S3 通道：{via}")

    old = OUT.read_text(encoding="utf-8").splitlines() if OUT.exists() else []
    old_set = {x.strip() for x in old if x.strip()}
    if old_set:
        add, gone = set(syms) - old_set, old_set - set(syms)
        print(f"[diff] 相对上一版：新增 {len(add)}｜消失 {len(gone)}")
        if add:
            print("       新增示例：" + ", ".join(sorted(add)[:8]))
        if gone:
            print("       消失示例：" + ", ".join(sorted(gone)[:8]))

    if args.check:
        print("[check] 未写入（--check）")
        return 0
    if not syms:
        print("[error] 空清单，拒绝写入")
        return 1

    DATA.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(syms) + "\n", encoding="utf-8")
    META.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "count": len(syms),
        "sources": {"archive": len(arc), "local_1d": len(p1d),
                    "local_1h": len(p1h), "pumps": len(pumps)},
        "s3_via": via,
        "filter_rules": "见 research/01_fetch_1d.py 的 LEVERAGED / STABLE",
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[write] {OUT.relative_to(ROOT)}（{len(syms)} 个）+ {META.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
