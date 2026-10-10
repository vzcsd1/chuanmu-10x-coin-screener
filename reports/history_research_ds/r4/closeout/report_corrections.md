# r4 closeout · C8 报告订正

**依据**：`tasks/history_r4_closeout_20261010.md` §2.8（C8 报告订正）。
**对象**：`reports/history_research_ds/r4/ready_for_glm.md`、`r4/independent_verification.md`
及一切引用其数字的下游文本。
**原则**：只订正**措辞与口径**，不重写旧结论；旧文件不删不改（旧入口只追加日期指针）。

---

## 1. 逐条订正

| # | 旧写法（错/易误读） | 订正后 | 依据 |
|---|---|---|---|
| 1 | 主时段 `2022-01-01 → 2026-08-xx` | 主时段 **`2022-01-01T00:00:00Z → 2026-09-30T23:00:00Z`（UTC）** | `history_research_ds.py` 的 `MAIN_END = "2026-09-30T23:00:00Z"`；`N_HOURS = (MAIN_END_MS − MAIN_START_MS)//1h + 1 = 41616` |
| 2 | 「对象 / 小时格 = 754 / **15,995,445**」 | 15,995,445 是 **「(对象, 小时) 中**有状态标记**的格数」**，**不是小时数**。小时数（全网格）= **41,616**。 | `baseline_summary_lag0.json` 原字段 `total_symbol_hours` 已改名 `total_symbol_hour_cells_with_status`，并新增 `grid_hours: 41616` 与口径说明 |
| 3 | 「**OI 观测不足被门控 20,725**」被当作受影响规模 | 20,725 是**门控计数**（被闸门挡住、不计 OI 分的格数），**≠ 受影响数**。真实改分/改入选数量由**逐格原始差异**给出：见 `diff_vs_v3.json → scoring_cell_diff`。 | `_scoring_cell_diff()` 逐格比较 r4 与 v3 分片；`n_oi_gated_note` 已写入 baseline 汇总 |
| 4 | 「r4 **独立复核**报告」（`independent_verification.md`） | 该文件由**同一代理**产出，应称**自检**；不能替代第三方独立验收。 | 该文件自身第 3 段已声明为自检版；本批在交接入口统一改称自检 |
| 5 | 「旧分片能否复用：**必须重算**」 | 按**依赖分组**判定：评分核（`scoring_core`）与逐片段 forward（`forward_detail`）**源码未变 → 原产物只读复用**；其余（事件段数、forward 汇总、reverse、features、supplementary、attention/retention、outcome、integrity、diff）**必须重算**。判定与证据见 `reuse_decision.json`。 | `reuse_decision.json` 记录各组函数源码哈希与快照逐字节比对结果 |
| 6 | 测试数「30/30 通过」 | closeout 后为 **53 项**（新增 C1–C8 反例与复用守卫）。 | `tests/test_history_research_ds_r4.py` |
| 7 | 「r4 目录 3.3 GB」 | 原 r4 目录体积不变（本批新计算写 `r4/closeout/`）；closeout 新增体积单独列出，不与原目录混算。 | `r4_originals_before.json` / 运行后清单 |
| 8 | 以 mtime 证明「目录未改」 | **不以 mtime 为证**。改用**内容哈希**：运行前后各算一次 `r4/`（除 `closeout/`）全部文件的 SHA-256，逐文件比对。 | `r4_originals_before.json` vs 运行后清单（见 `run_manifest.json`） |

---

## 2. 口径速查（写报告时按这张表用词）

| 词 | 含义 | 值 |
|---|---|---|
| **小时数 / 网格小时** | 主时段的小时刻度总数 | **41,616** |
| **有状态格数** | (对象, 小时) 中有状态标记的格 | 15,995,445 |
| **格子总数** | 对象 × 网格小时 | 754 × 41,616 = **31,378,464** |
| **门控格数** | OI 有效观测 < 25 被门控、不计 OI 分的格 | 20,725 |
| **改分格数** | r4 与 v3 都评分、且分数不同的格 | 见 `diff_vs_v3.json → scoring_cell_diff.score_changed_cells` |
| **改入选格数** | r4 与 v3 入选状态不同的格 | 见 `...scoring_cell_diff.selected_changed_cells` |
| **eligible 格数（直接后果）** | 参考价存在 + 未来 720h 无缺口且不越界 | 见 `outcome_summary_lag0.json → cells_eligible` |

---

## 3. 与旧文件的关系

- 旧入口 `reports/history_research_ds/r4/ready_for_glm.md` **不删不改**，仅在文首追加一行日期指针，指向本批冻结入口。
- 旧文件里被本表订正的措辞，**以本表为准**；旧文件中的数值结论（事件数、verdict 分布等）**未被本批推翻**，除非 `diff_vs_v3.json` 另有说明。
- 自检文件 `independent_verification.md`：H1 已按 §2.8 订正为
  「r4 自检报告（**同一代理自检，非独立第三方复核**）」，**正文一字未改**。
  （本批共 4 处授权改动，清单见 `run_manifest.json → r4_originals_integrity`。）
