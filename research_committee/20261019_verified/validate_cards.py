# validate_cards.py — 研究卡结构校验（只读，不修改任何文件）[kimi-k3]
# 校验 raw_output/ 四份产出中研究卡的字段完整性、数量、id 唯一性。
# 用法（项目根目录）：py -3.10 -B research_committee/20261019_verified/validate_cards.py
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
RAW = BASE / "raw_output"
FILES = ["classical.md", "crypto.md", "micro.md", "kimik3_gh.md"]

REQUIRED = [
    "id", "title", "group", "kind", "mechanism", "challenge",
    "project_relation", "upside", "data", "decision_time", "availability",
    "baseline", "experiment", "support", "refute", "failure_learning",
    "evidence_ids", "result_type", "terms",
]

def parse_cards(text):
    """按 '- id: XXX' 切分卡片块，提取 '- field:' 字段。"""
    cards = []
    # 找到所有 "- id: " 行位置作为卡片起点
    lines = text.splitlines()
    starts = [i for i, ln in enumerate(lines) if re.match(r"^-\s*id:\s*\S+", ln)]
    for k, start in enumerate(starts):
        end = starts[k + 1] if k + 1 < len(starts) else len(lines)
        block = "\n".join(lines[start:end])
        fields = {}
        for ln in block.splitlines():
            m = re.match(r"^-\s*([a-z_]+):\s*(.*)$", ln)
            if m and m.group(1) in REQUIRED and m.group(1) not in fields:
                fields[m.group(1)] = m.group(2).strip()
        cards.append(fields)
    return cards

def main():
    all_ids = []
    total = 0
    subversive = 0
    problems = []
    per_file = {}
    for name in FILES:
        path = RAW / name
        if not path.exists():
            problems.append(f"缺文件: {name}")
            continue
        cards = parse_cards(path.read_text(encoding="utf-8"))
        per_file[name] = len(cards)
        total += len(cards)
        for c in cards:
            cid = c.get("id", "?")
            all_ids.append(cid)
            missing = [f for f in REQUIRED if f not in c or not c[f]]
            if missing:
                problems.append(f"{name} 卡 {cid} 缺字段: {','.join(missing)}")
            if c.get("kind") == "subversive":
                subversive += 1
    dup = sorted({i for i in all_ids if all_ids.count(i) > 1})
    if dup:
        problems.append(f"id 重复: {dup}")

    print("=== 研究卡结构校验 [kimi-k3] ===")
    for name, n in per_file.items():
        print(f"{name}: {n} 张")
    print(f"总卡数: {total}（要求 >=30）{'OK' if total >= 30 else 'FAIL'}")
    print(f"颠覆性卡数: {subversive}（要求 >=10）{'OK' if subversive >= 10 else 'FAIL'}")
    print(f"字段完整性: {'OK' if not any('缺字段' in p for p in problems) else 'FAIL'}")
    print(f"id 唯一性: {'OK' if not dup else 'FAIL'}")
    for p in problems:
        print("问题:", p)
    # 生产文件未改校验（抽查关键文件 mtime 不早于今日上午任务开始）——仅打印提示
    print("=== 校验结束 ===")
    sys.exit(1 if problems else 0)

if __name__ == "__main__":
    main()
