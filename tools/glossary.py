#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""术语表工具：从 terms.yaml 单一真源渲染学习文档，并校验重复/遗漏。

为什么要有这个脚本
    用户要的是「能动态更新、避免重复」的项目学习文档。
    手写 Markdown 词典做不到——同一个词会在不同文档里被解释两遍，
    久了互相矛盾，也没人知道哪份是对的。

本项目的解法（单一真源）
    glossary/terms.yaml      唯一的数据源。加词只改这里。
    LEARNING.md              渲染产物。**不要手改**，改完跑 build 会覆盖。
    本脚本                   build = 重新渲染；check = 查重与校验。

命令
    py -3.10 tools/glossary.py build     重新生成 LEARNING.md
    py -3.10 tools/glossary.py check     只校验，不写文件（CI / 提交前跑）
    py -3.10 tools/glossary.py list      按分类列出术语 id

依赖
    只用标准库 + 可选 PyYAML。系统没装 yaml 时自动降级到内置迷你解析器，
    因为 terms.yaml 的格式是刻意设计得足够简单的子集。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
YAML_PATH = ROOT / "glossary" / "terms.yaml"
OUT_PATH = ROOT / "LEARNING.md"

# --- 文档里词典部分的起止锚点，脚本只替换这一段，保留人写的前言 ---
BEGIN = "<!-- GLOSSARY:BEGIN 由 tools/glossary.py 自动生成，请勿手改 -->"
END = "<!-- GLOSSARY:END -->"

CAT_ORDER = ["基础概念", "数据源", "指标", "评分条件", "方法论", "工程", "风险"]

REQUIRED = ("id", "zh", "cat", "plain", "where")

# 归一化用：去掉空白、全角/半角、大小写差异后再比较，防止
# "OI 1日增" 与 "oi1日增" 被当成两个不同术语。
_NORM_RE = re.compile(r"[\s\-_·・/]+")


def norm(text: str) -> str:
    text = str(text).strip().lower()
    text = text.replace("（", "(").replace("）", ")").replace("，", ",").replace("：", ":")
    return _NORM_RE.sub("", text)


# --------------------------------------------------------------------------
# YAML 读取：优先 PyYAML，缺失时用内置迷你解析器
# --------------------------------------------------------------------------
def _mini_yaml(path: Path) -> dict:
    """极简 YAML 子集解析器：支持 meta 映射、terms 列表、每项标量与字符串。

    只覆盖 terms.yaml 实际使用的语法，不追求通用性。
    """
    meta: dict = {}
    terms: list[dict] = []
    cur: dict | None = None
    section = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith("meta:"):
            section, cur = "meta", None
            continue
        if line.startswith("terms:"):
            section, cur = "terms", None
            continue
        if section == "meta":
            if ":" in line:
                k, _, v = line.partition(":")
                key = k.strip()
                if key == "note":       # note 是可读长文本，读到即停止 meta 区
                    meta[key] = _scalar(v)
                    section = None
                    continue
                meta[key] = _scalar(v)
            continue
        if section == "terms":
            if line.startswith("  - "):
                cur = {}
                terms.append(cur)
                rest = line[4:]
                if ":" in rest:
                    k, _, v = rest.partition(":")
                    cur[k.strip()] = _scalar(v)
            elif line.startswith("    ") and cur is not None and ":" in line:
                k, _, v = line.strip().partition(":")
                cur[k.strip()] = _scalar(v)
    return {"meta": meta, "terms": terms}


def _scalar(text: str):
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if text in ("true", "false"):
        return text == "true"
    if text in ("", "null", "~"):
        return None
    return text


def load(path: Path = YAML_PATH) -> dict:
    try:
        import yaml  # type: ignore
    except ImportError:
        return _mini_yaml(path)
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# --------------------------------------------------------------------------
# check：查重 + 完整性校验
# --------------------------------------------------------------------------
def check(data: dict) -> tuple[list[str], list[str]]:
    """返回 (错误, 警告)。错误会让 build 失败，警告只提示。"""
    errors: list[str] = []
    warns: list[str] = []
    terms = data.get("terms") or []
    if not terms:
        return ["terms 为空"], warns

    # 1) 唯一性：id / zh / en 三个命名空间各自不得重复
    seen: dict[tuple[str, str], str] = {}
    for i, t in enumerate(terms, 1):
        if not isinstance(t, dict):
            errors.append(f"第 {i} 项不是映射")
            continue
        tid = t.get("id", f"<第{i}项无id>")
        for key in ("id", "zh", "en"):
            val = t.get(key)
            if not val:
                continue
            for piece in str(val).split("/"):   # "lift / 提升倍数" 拆开逐段查重
                n = norm(piece)
                if not n:
                    continue
                sig = (key, n)
                if sig in seen:
                    errors.append(
                        f"重复：{key}='{piece.strip()}' 同时出现在 "
                        f"id={seen[sig]} 与 id={tid}")
                else:
                    seen[sig] = str(tid)

    # 2) 必填字段
    for t in terms:
        for key in REQUIRED:
            if not t.get(key):
                errors.append(f"id={t.get('id', '?')} 缺少必填字段 {key}")

    # 3) 分类必须在白名单内
    for t in terms:
        cat = t.get("cat")
        if cat and cat not in CAT_ORDER:
            errors.append(f"id={t.get('id')} 的分类 '{cat}' 不在 {CAT_ORDER}")

    # 4) 交叉引用检查：plain 中若出现**疑似术语**的英文记号（全大写、或含下划线），
    #    它应当能在词典里找到（作为 id / 中文名 / 英文名）。
    #    只做提示（warns），不阻断 build —— 自然语言里难免出现普通英文词。
    #    普通小写英文词不检查，避免大量误报。
    #    （2026-10-05 修复：原实现循环体只有 continue，等于从不检查。）
    known: set[str] = set()
    for t in terms:
        if t.get("id"):
            known.add(norm(str(t["id"])))
        for key in ("zh", "en"):
            for part in str(t.get(key) or "").split("/"):
                n = norm(part)
                if n:
                    known.add(n)
    # 通用专有名词 / 缩写：读者已知，不算"未登记术语"，避免噪音
    ignore = {"btc", "eth", "api", "csv", "okx", "json", "sql", "http", "https", "url"}
    for t in terms:
        plain = str(t.get("plain", ""))
        for token in re.findall(r"[A-Za-z][A-Za-z0-9_]*", plain):
            if len(token) < 2:
                continue
            if not (token.isupper() or "_" in token):
                continue                       # 普通英文词，跳过
            key = re.sub(r"\d+$", "", token)   # EMA50 → EMA
            if norm(key) and norm(key) not in known and norm(key) not in ignore:
                warns.append(
                    f"id={t.get('id')} 的 plain 提到 '{token}'，"
                    f"但词典里没有对应条目（补词或改写）")

    # 5) 建议性警告
    for t in terms:
        if not t.get("analogy"):
            warns.append(f"id={t.get('id')} 没有比喻（analogy），建议补一个")
        if not t.get("caution"):
            warns.append(f"id={t.get('id')} 没有 caution（常见误解），可选")

    # 6) 排序：分类缺失
    cats = {t.get("cat") for t in terms}
    for cat in sorted(x for x in cats if x):
        if cat not in CAT_ORDER:
            warns.append(f"分类 '{cat}' 未登记在 CAT_ORDER，将排在最后")
    return errors, warns


# --------------------------------------------------------------------------
# build：渲染 LEARNING.md
# --------------------------------------------------------------------------
def render(data: dict) -> str:
    terms = [t for t in (data.get("terms") or []) if isinstance(t, dict)]
    meta = data.get("meta") or {}
    by_cat: dict[str, list[dict]] = {}
    for t in terms:
        by_cat.setdefault(t.get("cat", "未分类"), []).append(t)

    out: list[str] = [BEGIN, ""]
    out.append(f"<!-- meta: version={meta.get('version')} "
               f"updated={meta.get('updated')} 共 {len(terms)} 条 -->")
    out.append("")
    out.append(f"**共 {len(terms)} 条术语** · 更新于 {meta.get('updated', '未知')} "
               f"· 版本 {meta.get('version', '?')}")
    out.append("")

    # 分类速览
    out.append("| 分类 | 条数 | 这一组在讲什么 |")
    out.append("|---|---:|---|")
    blurb = {
        "基础概念": "币圈最底层的东西，先看懂这些才看得懂后面的",
        "数据源": "数据从哪来、免费还是收费、有什么坑",
        "指标": "价格和持仓算出来的各种数值",
        "评分条件": "真正决定一个币能不能进候选池的规则",
        "方法论": "判断策略真假的方法，也是本项目最值钱的部分",
        "工程": "写代码和跑程序会用到的概念",
        "风险": "可能亏钱或让项目失败的事",
    }
    for cat in CAT_ORDER:
        items = by_cat.get(cat) or []
        if not items:
            continue
        out.append(f"| {cat} | {len(items)} | {blurb.get(cat, '')} |")
    for cat, items in by_cat.items():
        if cat not in CAT_ORDER:
            out.append(f"| {cat} | {len(items)} |  |")
    out.append("")

    # 逐条
    for cat in CAT_ORDER + [c for c in by_cat if c not in CAT_ORDER]:
        items = by_cat.get(cat)
        if not items:
            continue
        out.append(f"## {cat}")
        out.append("")
        for t in items:
            title = t.get("zh", t.get("id"))
            en = t.get("en")
            head = f"### {title}"
            if en:
                head += f"　`{en}`"
            out.append(head)
            out.append("")
            out.append(f"**大白话**：{t.get('plain', '')}")
            out.append("")
            if t.get("analogy"):
                out.append(f"**打个比方**：{t['analogy']}")
                out.append("")
            if t.get("where"):
                out.append(f"**在项目哪里**：`{t['where']}`")
                out.append("")
            if t.get("caution"):
                out.append(f"> ⚠️ {t['caution']}")
                out.append("")
        out.append("")
    out.append(END)
    return "\n".join(out)


def build(data: dict) -> str:
    block = render(data)
    if OUT_PATH.exists():
        text = OUT_PATH.read_text(encoding="utf-8")
        if BEGIN in text and END in text:
            head = text.split(BEGIN)[0]
            tail = text.split(END, 1)[1] if END in text else ""
            text = head + block + tail
        else:
            text = text.rstrip() + "\n\n" + block + "\n"
    else:
        text = HEADER + "\n" + block + "\n"
    OUT_PATH.write_text(text, encoding="utf-8")
    return text


HEADER = """# 项目学习文档 · 新手术语词典

> 这份文档是给**完全没接触过币圈和量化**的人看的。
> 每个词都用大白话解释，配一个日常比喻，并指出它出现在项目哪个文件。
>
> **本文的词典部分是自动生成的，不要手改**——手改会在下次生成时被覆盖。
> 要加词或改解释，只改 `glossary/terms.yaml`，然后跑：
> ```bash
> py -3.10 tools/glossary.py build
> ```
> 这样同一个术语永远只有一处定义，不可能出现两份互相矛盾的解释。
>
> ---
>
> ## 怎么用这份文档
>
> 1. **看不懂项目文档时**：来这里搜关键词，先看「大白话」，再看「打个比方」。
> 2. **看不懂某个条件为什么这样设**：看该条的「在项目哪里」去读对应代码/脚本。
> 3. **看到 ⚠️ 标记**：那是本项目踩过的坑或容易误解的地方，**必读**。
> 4. **想给别人解释**：直接复制那一条，不用自己组织语言。
>
> ## 读项目的推荐顺序
>
> ```
> 基础概念 → 数据源 → 指标 → 评分条件 → 方法论 → 风险
> ```
>
> 「方法论」和「风险」这两组最值钱——它们决定你能不能分辨
> 一个策略是真的有效，还是只是看起来有效。
"""


def main(argv: list[str]) -> int:
    cmd = (argv[1] if len(argv) > 1 else "check").lower()
    if not YAML_PATH.exists():
        print(f"[错误] 找不到 {YAML_PATH}")
        return 2
    data = load()

    if cmd == "list":
        for cat in CAT_ORDER:
            items = [t for t in (data.get("terms") or [])
                     if isinstance(t, dict) and t.get("cat") == cat]
            if not items:
                continue
            print(f"[{cat}] {len(items)} 条")
            for t in items:
                print(f"    {t.get('id'):<24} {t.get('zh', '')}")
        return 0

    errors, warns = check(data)
    if errors:
        print(f"[查重失败] {len(errors)} 个错误：")
        for e in errors:
            print(f"  ✗ {e}")
        return 1
    print(f"[查重通过] {len(data.get('terms') or [])} 条术语，无重复")
    if warns:
        print(f"[提示] {len(warns)} 条建议：")
        for w in warns[:20]:
            print(f"  · {w}")
        if len(warns) > 20:
            print(f"  · ...另有 {len(warns) - 20} 条")

    if cmd == "build":
        build(data)
        print(f"[已生成] {OUT_PATH.relative_to(ROOT)}")
    elif cmd != "check":
        print(f"[未知命令] {cmd}；可用：build / check / list")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
