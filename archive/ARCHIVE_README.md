# 归档说明 · 2026-10-19

本目录存放**已被取代或完全重复**的文件。**不是删除**，随时可取回。

---

## 本次归档（2026-10-19 20:18）

| 文件 | 原位置 | 为什么归档 | 可安全删除吗 |
|---|---|---|---|
| `part1_现状与瓶颈.md` | `research_committee/` | 内容 100% 合并入 `多倍币捕捉能力突破研究_研究委员会交付_20261019.md` | ✅ 可以 |
| `part2a_委员会讨论.md` | 同上 | 同上 | ✅ |
| `part2b_假设库.md` | 同上 | 同上 | ✅ |
| `part3_社区与反方.md` | 同上 | 同上 | ✅ |
| `part4_实验与结论.md` | 同上 | 同上 | ✅ |
| `classical.json` | `research_committee/20261019_verified/raw_output/` | 与同目录 `classical.md` 内容重复（45KB JSON → 59KB md） | ✅ |

**重复度证据**：主报告 45,375 字 vs 5 个 part 合计 45,399 字，差 24 字（仅模型标签注释）。主报告含全部 `H01`~`H30` 卡片。

---

## 明确**不归档**的文件（及原因）

| 文件 | 为什么不删 |
|---|---|
| `space-bunny_返工/{A组C组,B组F组,D组E组,G组H组}_独立发散_*.md`（44 万字） | **八项字段的唯一来源**。总交付只有 37KB，每行仅"机制/判据/推翻"三列，八字段必须靠这四份拼装 |
| `space-bunny_取证/`（204KB） | 96 条外部来源的原始取证记录，返工版只引用了 37 条 |
| `20261019_verified/raw_output/*.md` | kimi-k3 的分组发散原文，`report.md` 只做了汇总 |
| `20261019_verified/final/*` | gpt6astra 与 kimi 的正式交付 |
| `reports/` 下各目录（合计约 16GB） | **是 A1 底账的原料**，不是文档。`.gitignore` 已挡住不入库，但删了就无法复算 |

---

## 真正的空间占用在哪

`reports/` 下：

| 目录 | 体积 |
|---|---:|
| `history_research_ds` | 6.1 GB |
| `a1_stage2_ds` | 4.4 GB |
| `data_inventory_glm_full` | 2.4 GB |
| `a1_stage2_glm53f` | 1.4 GB |
| `ocean_v2` | 1.2 GB |
| `archive_download_ds_full` | 604 MB |

**这些不是"重复文件"，是原始数据分片**。要省空间只能压缩或迁到冷存储，不能删——删了 `08_audit_hindsight.py` 等脚本就跑不了。是否处理，等 A1 验收后再定。

---

## 另一处需要人工确认的重复

`upload_for_gemini/A_独立模式_必传/A1_事实底账.md` 与 `upload_for_grok/G2_事实底账.md` **是同一份文件的两个副本**（为了两个网页端分别上传）。**保留两份是有意的**，不是重复垃圾；若以后只用其中一个模型，可删另一个。

---

## 恢复方法

```bash
cd "D:/Projects/川沐十倍币筛选"
mv "archive/20261019_重复与中间产物/part2b_假设库.md" research_committee/
```

需要哪个搬哪个，不影响其他文件。
