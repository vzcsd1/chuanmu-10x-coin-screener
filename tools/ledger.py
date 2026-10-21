#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""想法台账工具：开实验前查「这个做过吗」，做完实验回扫「哪些旧判决该改」。

为什么要有这个脚本
    A27 那轮，我手工翻 gate_folds.csv 才发现 A20 的 15 条规则里 5 条一折都没跑出来。
    如果没翻，那个错误会一直挂着——因为项目里只有散在 5 份文档里的散文，
    没有「按想法索引」的东西，也没有「为什么停 / 什么条件重启」的字段。

    grep 能搜到关键词，前提是你记得住当时用的词。记不住就搜不到，
    搜不到就会重做——这就是这个项目一直在付的重复成本。

数据源（单一真源）
    research_ledger.json     唯一的数据源。加条目只改这里。
    research_committee/想法台账.md   渲染产物，**不要手改**。
    本脚本

命令
    py -3.10 tools/ledger.py check  <关键词>   开实验前查：做过吗？判死还是暂缓？为什么？什么条件重启？
    py -3.10 tools/ledger.py trace  <id>       一个想法的完整判决史（谁判的、什么时候、后来改成什么）
    py -3.10 tools/ledger.py reopen            可重启清单：所有带「重启条件」的暂缓/判死条目
    py -3.10 tools/ledger.py audit             体检：状态与成色是否矛盾、证据文件是否存在、TODO 作废项是否漏记
    py -3.10 tools/ledger.py stats             按状态/成色计数
    py -3.10 tools/ledger.py build             重新生成 research_committee/想法台账.md

依赖
    只用标准库（JSON，不用 PyYAML——这台机器没装）。
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEDGER = ROOT / "research_ledger.json"
OUT_MD = ROOT / "research_committee" / "想法台账.md"

# 状态与成色的合法组合（A27 立的规矩：判死必须写成色）
GRADE_OF_STATUS = {
    "判死": {"无信息", "同轴", "有害", "收益无改进"},
    "暂缓": {"测不动", "弱信号", "没测"},
}
STATUS_ORDER = ["采纳", "候选", "暂缓", "判死", "作废", "在测", "未测", "已完成"]


def load() -> dict:
    return json.loads(LEDGER.read_text(encoding="utf-8"))


def _fmt(it: dict) -> str:
    grade = f"／{it['grade']}" if it.get("grade") else ""
    return f"[{it['status']}{grade}] {it['id']} {it['name']}"


# ---------------------------------------------------------------- check

def cmd_check(kw: str) -> int:
    data = load()
    kw_l = kw.lower()
    hits = []
    for it in data["items"]:
        blob = " ".join(str(it.get(k, "")) for k in
                        ("id", "name", "claim", "why_stopped", "reopen_if"))
        blob += " " + " ".join(it.get("tested_by", []))
        if kw_l in blob.lower():
            hits.append(it)

    if not hits:
        print(f"[check] 台账里没有「{kw}」。")
        print("        → 但这不等于「没做过」：只说明它没留下判决。")
        print("        → 下一步：grep DONE.md / KNOWLEDGE.md / reports/ 再确认一次，"
              "确认没做过再开工。")
        return 0

    print(f"[check] 「{kw}」命中 {len(hits)} 条：\n")
    for it in hits:
        print(f"  {_fmt(it)}   （{it['kind']}｜最后复核 {it.get('last_review', '—')}）")
        if it.get("claim"):
            print(f"    想法：{it['claim']}")
        if it.get("why_stopped"):
            print(f"    为什么停：{it['why_stopped']}")
        if it.get("reopen_if"):
            print(f"    重启条件：{it['reopen_if']}")
        if it.get("evidence"):
            print(f"    证据：{', '.join(it['evidence'])}")
        print()

    bad = [h for h in hits if h["status"] in ("判死", "作废")]
    if bad:
        print("  ⚠ 其中有已判死/作废的条目。开工前先确认：重启条件是否已经满足？")
    return 0


# ---------------------------------------------------------------- trace

def cmd_trace(item_id: str) -> int:
    data = load()
    it = next((x for x in data["items"] if x["id"].lower() == item_id.lower()), None)
    if it is None:
        print(f"[trace] 没有 id={item_id}。用 check <关键词> 找。")
        return 1
    tag = it["status"] + (f"／{it['grade']}" if it.get("grade") else "")
    print(f"{it['id']} · {it['name']}   [{tag}]\n")
    print(f"  想法：{it.get('claim', '—')}")
    print(f"  测过：{', '.join(it.get('tested_by', [])) or '—'}")
    if it.get("why_stopped"):
        print(f"  为什么停：{it['why_stopped']}")
    if it.get("reopen_if"):
        print(f"  重启条件：{it['reopen_if']}")
    print("\n  判决史：")
    hist = it.get("history", [])
    if not hist:
        print("    （只有一条判决，没有变更）")
    for h in hist:
        print(f"    {h['d']}  {h['e']}")
        if h.get("n"):
            print(f"              ↳ {h['n']}")
    return 0


# ---------------------------------------------------------------- reopen

def cmd_reopen() -> int:
    data = load()
    items = [x for x in data["items"]
             if x["status"] in ("暂缓", "判死") and x.get("reopen_if")]
    print(f"[reopen] 带「重启条件」的条目 {len(items)} 条\n")
    for it in items:
        print(f"  {_fmt(it)}")
        print(f"    重启条件：{it['reopen_if']}")
        print()
    no_cond = [x for x in data["items"] if x["status"] == "暂缓" and not x.get("reopen_if")]
    if no_cond:
        print(f"  ⚠ {len(no_cond)} 条「暂缓」没写重启条件（违反「暂缓但不排除」）：")
        for it in no_cond:
            print(f"    {it['id']} {it['name']}")
    return 0


# ---------------------------------------------------------------- audit

def cmd_audit() -> int:
    data = load()
    items = data["items"]
    err, warn = [], []

    ids = Counter(x["id"] for x in items)
    for k, v in ids.items():
        if v > 1:
            err.append(f"重复 id：{k} 出现 {v} 次")

    for it in items:
        st, g = it["status"], it.get("grade")
        if st in GRADE_OF_STATUS:
            if not g:
                err.append(f"{it['id']} 状态是「{st}」但没写成色（A27 纪律第 5 条）")
            elif g not in GRADE_OF_STATUS[st]:
                err.append(f"{it['id']} 状态「{st}」与成色「{g}」矛盾"
                           f"（允许：{'/'.join(sorted(GRADE_OF_STATUS[st]))}）")
        if st in ("判死", "暂缓", "候选", "采纳"):
            if not it.get("evidence"):
                warn.append(f"{it['id']} 是「{st}」但没有证据文件")
            for p in it.get("evidence", []):
                if not (ROOT / p).exists():
                    err.append(f"{it['id']} 的证据文件不存在：{p}")
            if not it.get("why_stopped") and st != "采纳":
                warn.append(f"{it['id']} 没有「为什么停」")
        if st == "暂缓" and not it.get("reopen_if"):
            warn.append(f"{it['id']} 暂缓但没写重启条件（「暂缓但不排除」需要条件）")

    # TODO 的「已作废」表是否都进了台账
    todo = ROOT / "TODO.md"
    if todo.exists():
        txt = todo.read_text(encoding="utf-8")
        m = re.search(r"## 已作废、不要再做.*?\n(.*?)(?=\n## |\Z)", txt, re.S)
        if m:
            rows = [ln.split("|")[1].strip() for ln in m.group(1).splitlines()
                    if ln.strip().startswith("|") and "---" not in ln]
            rows = [r for r in rows if r and not r.startswith("旧建议")]

            def norm(s: str) -> str:
                return re.sub(r"[`\s，。；：、（）()\-—/]", "", s).lower()

            def bigrams(s: str) -> set[str]:
                s = norm(s)
                return {s[i:i + 2] for i in range(len(s) - 1)} or {s}

            led = [(x["id"], bigrams(x["name"] + x.get("claim", ""))) for x in items]
            missing = []
            for r in rows:
                rb = bigrams(r)
                best = max(((len(rb & lb) / max(len(rb), 1), i) for i, lb in led),
                           default=(0, ""))
                if best[0] < 0.5:
                    missing.append(f"{r[:24]}…（最像 {best[1]}，重合 {best[0]:.0%}）")
            if missing:
                warn.append(f"TODO「已作废」有 {len(missing)} 条没进台账：" + "；".join(missing))

    print(f"[audit] {len(items)} 条｜错误 {len(err)}｜警告 {len(warn)}")
    for e in err:
        print(f"  ✗ {e}")
    for w in warn:
        print(f"  ⚠ {w}")
    if not err and not warn:
        print("  ✓ 全部通过")
    return 1 if err else 0


# ---------------------------------------------------------------- stats

def cmd_stats() -> int:
    data = load()
    items = data["items"]
    print(f"[stats] {len(items)} 条｜更新于 {data.get('updated')}\n")
    print("  按状态：")
    for k, v in Counter(x["status"] for x in items).most_common():
        print(f"    {k:<6} {v}")
    print("\n  按成色（判死/暂缓）：")
    for k, v in Counter(x["grade"] for x in items if x.get("grade")).most_common():
        print(f"    {k:<10} {v}")
    print("\n  按类型：")
    for k, v in Counter(x["kind"] for x in items).most_common():
        print(f"    {k:<4} {v}")
    return 0


# ---------------------------------------------------------------- build

def cmd_build() -> int:
    data = load()
    items = data["items"]
    L = []
    A = L.append
    A("# 想法台账（人话版）")
    A("")
    A(f"> 2026-10-21｜数据源 `research_ledger.json`｜**本文由 `tools/ledger.py build` 生成，不要手改**")
    A("")
    A("**这份文档回答三个问题**：这个想法做过吗？做到哪一步？什么条件下值得重开？")
    A("")
    A("**为什么有这份东西 / 怎么落地**：见 `research_committee/追溯机制_20261021.md`；"
      "规则在 `AGENTS.md`「想法台账（追溯机制）」一节；知识条目在 `KNOWLEDGE.md` §2.20。")
    A("")
    A("**边界**：只放「已经做出判决」的条目。待做事项在 `TODO.md`，"
      "没测过的想法在 `变量清单_92机制可算性分级`。")
    A("")
    A("**判死成色（A27 立的规矩）**：")
    A("")
    A("| 成色 | 含义 | 能不能重开 |")
    A("|---|---|---|")
    for k, v in data["grade_vocab"].items():
        can = "**能**（是测不动，不是无效）" if k in ("测不动", "没测", "弱信号") else "要新证据"
        A(f"| {k} | {v} | {can} |")
    A("")
    A("---")
    A("")

    for st in STATUS_ORDER:
        grp = [x for x in items if x["status"] == st]
        if not grp:
            continue
        A(f"## {st}（{len(grp)} 条）")
        A("")
        A("| 编号 | 名称 | 类型 | 成色 | 为什么停 | 重启条件 |")
        A("|---|---|---|---|---|---|")
        for it in grp:
            why = (it.get("why_stopped") or "—").replace("|", "\\|")
            re_ = (it.get("reopen_if") or "—").replace("|", "\\|")
            A(f"| `{it['id']}` | {it['name']} | {it['kind']} | {it.get('grade', '—')} | {why} | {re_} |")
        A("")

    # 判决变更史：只有改判过的
    changed = [x for x in items if len(x.get("history", [])) > 1]
    if changed:
        A("---")
        A("")
        A(f"## 判决变更史（{len(changed)} 条改过判）")
        A("")
        A("**这一节是「追溯」的核心**：同一件事在不同时间被判成不同结果，"
          "说明当时的判据有问题——这比单条结论更有价值。")
        A("")
        for it in changed:
            tag = it["status"] + (f"／{it['grade']}" if it.get("grade") else "")
            A(f"### {it['id']} · {it['name']}　→ 现在 `[{tag}]`")
            A("")
            for h in it["history"]:
                A(f"- **{h['d']}**　{h['e']}" + (f"　—— {h['n']}" if h.get("n") else ""))
            A("")

    A("---")
    A("")
    A("## 怎么用这份台账（三步）")
    A("")
    A("1. **开新实验前**：`py -3.10 tools/ledger.py check 关键词`。"
      "命中判死/作废就先确认重启条件是否满足；没命中也要 grep 一遍 `DONE.md`。")
    A("2. **实验做完后**：把结论写进 `research_ledger.json`（含成色、为什么停、重启条件），"
      "再跑 `build`。")
    A("3. **回扫**（最容易漏的一步）：问一句「**这个结论让哪些旧的判死/暂缓可以重开？**」"
      "有的话改它们的 `reopen_if` 或状态，并在 `history` 里记一笔。")
    A("")
    A("`py -3.10 tools/ledger.py audit` 会检查：状态与成色是否矛盾、证据文件是否存在、"
      "暂缓有没有写重启条件、TODO 的作废项有没有漏记。")
    A("")
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text("\n".join(L), encoding="utf-8")
    print(f"[build] 已生成 {OUT_MD.relative_to(ROOT)}（{len(items)} 条）")
    return 0


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 0
    cmd, *rest = argv
    if cmd == "check":
        return cmd_check(rest[0] if rest else "")
    if cmd == "trace":
        return cmd_trace(rest[0] if rest else "")
    if cmd == "reopen":
        return cmd_reopen()
    if cmd == "audit":
        return cmd_audit()
    if cmd == "stats":
        return cmd_stats()
    if cmd == "build":
        return cmd_build()
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
