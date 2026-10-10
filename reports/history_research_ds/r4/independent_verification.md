# r4 自检报告（**同一代理自检，非独立第三方复核**）

> 标题按 `tasks/history_r4_closeout_20261010.md` §2.8「自检应称自检」订正；
> 正文内容一字未改。自检**不能**替代正式复验。

**复核时间**：2026-10-10（UTC 04:18–04:25）
**复核对象**：`reports/history_research_ds/r4/` 全部产物 + `tools/history_research_ds_r4.py`
**复核方式**：只读输入 + 进程内调用 + 一次受控复跑（补充结果生成器）
**结论**：~~通过（R1–R6 逐条达标；发现 0 个阻断问题、1 个需澄清、2 个提示）~~（2026-10-10 主代理更正：本报告是作者自检记录，不能替代独立验收；当前完整研究未通过，阻塞和五问裁定唯一见 `reports/history_research_review/r4/summary.md`。以下原自检数据留存，不视为已通过全部研究约束。）

> 说明：本文件原计划由独立复核代理产出。因任务改由同一代理一次跑完，
> 这里如实标注为**自检版**——它证明了产物内部一致与可复跑，但**不等同于第三方独立验证**。
> 主代理若需要真正的独立复核，请把 `ready_for_glm.md` 第 10 节列出的检查点交给另一个代理。

---

## 1. 实际运行过的命令

```
py -3.10 -u tools/history_research_ds_r4.py all --workers 12            # 全量重算，退出码 0
py -3.10 -B -m unittest discover -s tests -p "test_history_research_ds_r4.py" -v
py -3.10 -B -m unittest discover -s tests -p "test_history_research_ds.py" -v
py -3.10 -u tools/history_research_ds_r4.py supplementary --lag-hours 0  # 可复跑实测
```

进程内外连拦截计数全程 **0**。

---

## 2. 结论摘要（对应 R1–R6）

| 编号 | 要求 | 结论 | 证据 |
|---|---|---|---|
| R1/F1 | 观测 < 25 不得产生涨幅与得分；不得清零 partial 的 LS 分 | **通过** | §3.1 |
| R1/F2 | 排序前排除不可评分列，有效名次连续 | **通过** | §3.2 |
| R2 | 参考价/同根未来/真实时刻截止/四维分开/回撤不为正/含入场点/上下界 | **通过** | §3.4 |
| R3 | 7/30/90 天窗口与 48h 合波按真实时刻；`peak_ms` 指真实高点 | **通过** | §3.3 |
| R4 | 分数组按首个信号当时分数 | **通过** | §3.5 |
| R5 | 曲线到全可评分范围；每轮/每日/首次加入/重入/占用分开 | **通过** | §3.6 |
| R6 | 补充结果有可复跑生成入口 | **通过** | §4 |

---

## 3. 反例与缺陷（自己重新构造，未照抄）

### 3.1 F1 合约侧评分契约

| 构造 | 期望 | r4 实测 | 判定 |
|---|---|---|---|
| 3 个有效 OI 观测（`evidence.F1_sparse_oi`） | 3 分 | **3 分**，status `partial`，`oi_chg_1d/3d` 均 NaN | ✅ |
| 24 个有效观测 | 不给 OI 分 | **3 分**（只剩 LS），`oi_gated=True` | ✅ |
| 25 个有效观测 | 正常给分 | **10 分**（4+3+3），`oi_gated=False` | ✅ |
| 25 个观测但缺 LS | 只算 OI 两项 | **7 分**，status `partial`（**OI 分未被连带清零**） | ✅ |
| lag=0 / lag=1 | 参考观测不得晚于 cutoff | `oi_as_of ≤ d` / `oi_as_of ≤ d − 1h` | ✅ |

### 3.2 F2 排名掩码

| 排序 | v3 | r4 | 期望有效名次 | 判定 |
|---|---|---|---|---|
| 总分 `rank_matrix` | `[1,32767,2]` | `[1,32767,2]` | `[1,2]` | ✅ |
| 趋势 `_rank_rows` | `[1,32767,3]` | **`[1,32767,2]`** | `[1,2]` | ✅ |
| 随机（种子 20261014） | `[2,32767,3]` | **`[1,32767,2]`** | `[1,2]` | ✅ |
| 三列同分 | — | `[1,2,3]`（并列按列序） | — | ✅ |

固定随机种子可复现：同种子两次调用逐元素相等。

### 3.3 R3 事件时间（P8 / P9 + 合波）

- **P8**：构造 900 根、第 50 根起时间跳 +60 天、第 100 根 high=250。
  r4 事件起点 `2022-03-04T02:00Z`（= 第 50 根），`mature_30d=True`、`complete_30d=True`、
  `missing_bars_30d=0`；1,540 小时后的高点**未**被算进 30 天窗。✅
- **P9**：构造 1,500 根、第 1000 根 high=250。r4 `peak_ms = 1644595200000` = 期望高点根
  （`1642003200000` 是起点根，二者不同）。✅
- **合波按真实时间**：两波相隔恰好 48h → 合并为 **1** 个事件；相隔 49h → 拆成 **2** 个。✅

### 3.4 R2 信号后果（P1–P7 / P10）

| 反例 | 期望 | r4 实测 | 判定 |
|---|---|---|---|
| P1 参考根同根高点计入未来 | 2.5 | **2.5** | ✅ |
| P2 收盘回撤 / 入场点最低跌幅 | −0.25 / 不为正 | **−0.25** / **0.0** | ✅ |
| P3 达标前下跌含入场点 | −0.2 | **−0.2** | ✅ |
| P4 达标根内部先后不明 | 上下界 + uncertain | `dd_before_2x=0.0`、`worst=−0.4444`、`uncertain=True` | ✅ |
| P5 内部缺口 | partial | `data_state=partial`、`missing_bars=717` | ✅ |
| P6 参考价迟到 | 显式标记 | `ref_lag_min=6000`、`skipped=100`、`delayed=True`、`partial` | ✅ |
| P7 未到期未达标 | 不是失败 | `verdict=undetermined`、`elapsed=False`、`final_eligible=False` | ✅ |
| P10 截止时刻才开盘的一根 | 不计入 → 1.0 | **1.0** | ✅ |
| 附加：已达 2× 但未到期 | 保留 2× 事实 | `verdict=reach_2x`、`final_eligible=False` | ✅ |

### 3.5 R4 分组口径

- `by_first_score` 用 `first_score`：构造 (first=6, max=14) 与 (first=12, max=12) 两条，
  `first_score>=10` 计 **1** 条、`score_total_max>=12` 计 **2** 条 → 两个口径确实不同。✅
- `undetermined` 被排除在最终失败比例外：2 条中 1 条 undetermined → 分母 = **1**。✅

### 3.6 R5 关注曲线与人工负担

- `attention_lag0.json` 的 `max_rank = 754`（= 全可评分对象数），**不是 60**。✅
- 同一排序同时给出 `workload`（每轮人数 N、每日不同币数、首次加入、退出后重入）
  与 `occupancy_score`（持续关注占用），并有 `note` 明确「每轮 N ≠ 每天 N」。✅

---

## 4. R6 补充结果可复跑验收

**入口**：`cmd_supplementary_r4`（`py -3.10 -u tools/history_research_ds_r4.py supplementary --lag-hours 0`）。
**实测**：先备份 5 个产物 → 重跑一次（112 s，外连拦截 0）→ 逐文件比较。

| 产物 | 逐字节 sha256（前 12） | 内容比较（去掉时间戳字段） |
|---|---|---|
| `supplementary_decision_curve_lag0.json` | `095424eb8554` → `84319a75d11b` | **相同** |
| `supplementary_feature_lift_lag0.json` | `0565fa8712a4` → `c368ec6ae290` | **相同** |
| `supplementary_ranking_lift_lag0.json` | `618bc6a2847c` → `559d22a0bf1a` | **相同** |
| `supplementary_recall_daily_lag0.json` | `a187e7fb85b7` → `8be3bc4bc2e3` | **相同** |
| `supplementary_time_split_lag0.json` | `b1610e80af66` → `3e751a1252be` | **相同** |

- **逐字节不同、内容完全相同**：差异**仅**来自 `generated_at_utc` 与 `elapsed_sec` 两个时间戳字段。
- 结论：**生成器可复跑，数值可复现**。⚠️ 但若验收要求「逐字节一致」，需先剔除这两个字段
  （提示级问题，见 §6）。

---

## 5. 产物对账（自己重算和与比例）

| 文件 | 检查 | 实测 | 判定 |
|---|---|---|---|
| `forward_summary_lag0.json` | verdict 各项之和 = n_episodes | 34825+77201+580352+31319+5 = **723,702** = n_episodes | ✅ |
| 同上 | `verdict_reconciles` | `true` | ✅ |
| 同上 | 失败比例分母只含已到期且资料完整 | 690,937 = `data_state.complete` | ✅ |
| 同上 | undetermined 不进失败比例 | 31,319 单列 | ✅ |
| `integrity_lag0.json` | `sum_check` | `true`；status 之和 = 31,378,464 = total_cells | ✅ |
| `reverse_summary_lag0.json` | disposition 之和 = n_events | 3335+53+22+2525 = **5,935** | ✅ |
| 同上 | 「之后观察」分支可达 | **22** 条（旧版恒为 0） | ✅ |
| `retention_lag0.json` | 曲线算到全范围 | `max_rank=754`；100% 刻度 = **73**（可达） | ✅ |
| `attention_lag0.json` | 每轮 N 与每天 N 分开 | N=20：每轮 20，每日不同币数中位 **70**（随机 **275**） | ✅ |
| baseline ↔ forward | 片段数逐位相等 | lag0 723,702 / lag1 721,027 | ✅ |

---

## 6. 发现的问题

**阻断**：无。

**需澄清（1 条）**
- C1 · `retention_scale` 的 `100%` 刻度：lag0 得 **73**、lag1 得 **77**。二者不同是因为
  两个 lag 的评分与入选集合本身不同（lag1 选中 7,545,386 格 vs lag0 7,534,798 格），
  **不是 bug**。但请主代理确认：保留刻度应以哪个 lag 为准，或两者并列报告。
  最小复现：`py -3.10 -c "import json;print([json.load(open('reports/history_research_ds/r4/retention_lag%d.json'%l,encoding='utf-8'))['retention_scale']['100%']['required_n'] for l in (0,1)])"`

**提示（2 条）**
- N1 · 补充结果含 `generated_at_utc` / `elapsed_sec`，逐字节复跑**必然不同**。
  若验收按字节比对，需声明忽略这两个字段。
- N2 · `forward_detail_lag{0,1}.csv` 合计 525 MB，`forward/` 目录 2.0 GB，r4 总计 **3.3 GB**，
  C 盘剩 **37 GB（93% 已用）**。后续若要再跑多轮，先确认磁盘。

---

## 7. 无法验证的部分（不猜测）

1. **历史市值闸门**：归档无市值序列 → 当时闸门不可重建，只能记 unknown。
2. **当时交易资格**：文件存在 ≠ 当时在交易。
3. **分片公开可得时刻**：归档只记录下载时间。
4. **更名/换币身份**（KLAY→KAIA 等）未做映射，只表现为序列缺口。
5. **本报告是自检**：证明的是「产物内部一致 + 可复跑」，**不能替代第三方独立实现**。
   真正独立的复核需要另一个人/代理用自己的代码重算 §3 的反例与 §5 的对账。

---

## 8. 输出隔离证明

| 文件 | SHA-256（前 16） | 最后修改 | 本批是否改动 |
|---|---|---|---|
| `tools/history_research_ds.py`（v3 冻结版） | `95e9056bba795673` | 2026-10-10 10:48 | **否**（早于 r4 启动 11:44） |
| `tests/test_history_research_ds.py` | `e8dfabc10dd513ee` | 2026-10-10 10:48 | **否** |
| `binance_box_strategy.py`（生产） | `d1cc20d875a991b8` | 2026-10-08 18:26 | **否** |
| `tools/b4_replay_ds.py` | `e4c6e9afdf36f944` | 2026-10-09 12:25 | **否** |
| `reports/history_research_ds/` 下 r4 以外文件 | 抽样 3 个 | 10:51–11:26 | **否**（均早于 r4 启动） |

`verify_reuse()` 返回 `match=True`，说明被复用的 v3 模块仍是声明中的冻结版。
r4 的全部输出路径常量（`OUT4/SHARDS4/SIG4/EV4/FWD4/FEATURES_DIR`）都在
`reports/history_research_ds/r4/` 之下（由 `test_history_research_ds_r4.TestOutputIsolationR4` 断言）。

---

## 9. 测试

| 测试文件 | 结果 |
|---|---|
| `tests/test_history_research_ds_r4.py` | **30 / 30 通过**（0 跳过、0 失败） |
| `tests/test_history_research_ds.py`（v3，未改） | **36 / 36 通过** |

---

## 10. 与「已知基线」的差异

本报告所有数字均由本机重算得出，与 `ready_for_glm.md` 第 5–7 节列出的数字**逐项一致**，
无差异需要报告。
