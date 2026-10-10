# r4 closeout 冻结交接单（ds，2026-10-10）

> **本批到此停止编辑。** 候选比较（哪张卡更好、要不要合并）由主代理据本文件证据接续。
> 本文件是 r4 的**唯一交接入口**（`tasks/history_r4_closeout_20261010.md` §2.7 指定）。
> 旧入口 `reports/history_research_ds/r4/ready_for_glm.md` **不删不改**，仅追加一行日期指针。
> 问题与可重跑证据唯一见 `reports/history_research_review/r4/summary.md` 与该目录脚本。

---

## 0. 一句话结论

本轮只做**已取证错误的收口**：把特征时钟、时间切分、结果目标、人工量、剩余机会、事件段数六处口径改对，
并用**逐格原始差异**给出真实影响量（改分 28 格 / 改入选 1 格），
其余主结果（事件数、评分、覆盖曲线）与原 r4 逐点一致。
**没有重新设计已通过的评分（F1/F2）**，没有扩展指标，没有新增外部数据。

---

## 1. 冻结版本与配置

| 角色 | 路径 | SHA-256 | 字节 |
|---|---|---|---|
| r4 实现（本批唯一新增代码） | `tools/history_research_ds_r4.py` | `d05517c352387933683b6d620b9223e8cc2a508acd9a7242726b1d71da86218f` | 154,939 |
| r4 测试 | `tests/test_history_research_ds_r4.py` | `2ba23dc38d3608e5ba7373d3e5519d96d6b321f52c97c12e24bdffec8de55e9c` | 30,152 |
| 被复用的 v3 模块（**未改**） | `tools/history_research_ds.py` | `95e9056bba795673846958b80fbd21cd669cf3a2067b9523f653925177e25199` | 74,037 |
| v3 测试（**未改**） | `tests/test_history_research_ds.py` | `e8dfabc10dd513ee65eeddce38c1d771bfdf194b6ac5bd94eb0af48c1d45b8be` | 20,423 |
| 生产评分（**只读**） | `binance_box_strategy.py` | `d1cc20d875a991b87b476aba9736fba449a9bd9351a20b17d0be16aa5ea80826` | 80,104 |
| 已验收接入（**只读**） | `tools/b4_replay_ds.py` | `e4c6e9afdf36f944339aa7497349cc2b1b68e8678aa26d1eacebdc7d8df318e5` | 91,720 |

**配置指纹**（`config_fingerprint()`，写入 `run_manifest.json`）：

| 项 | 值 |
|---|---|
| 主时段（**C8 订正**） | **`2022-01-01T00:00:00Z → 2026-09-30T23:00:00Z`** |
| 网格小时数 | **41,616** |
| 对象数 | 754 |
| 格子总数 | 754 × 41,616 = **31,378,464** |
| 时间切分点 | `split_hour = 26304`（= 2025-01-01），`review_start = 2025-01-01T00:00:00Z` |
| `min_score` | **5**（只对核心分判定，与生产一致） |
| `WEIGHTS` | `{c_trend:5, oi_chg_1d:4, oi_chg_3d:3, ls_top:3, c_breakout:2, c_v24up:1}` |
| 窗口 | 7d=168h / 30d=720h / 90d=2160h；合波 `merge_gap_hours=48` |
| `deriv_min_oi_obs` | 25 |
| 评分核指纹 | `scoring_core_fingerprint = 947ce50ccaf01de2…` |
| 离线 | `网络外连拦截计数 = 0` |

---

## 2. 依赖复用与重算（C7）

**判定原则**：按**依赖分组**，不看文件名；任一组指纹与运行前快照不符，该组旧产物一律重算。
逐函数源码哈希与快照逐字节比对见 `closeout/reuse_decision.json`。

| 组 | 覆盖函数 | 快照比对 | 处理 |
|---|---|---|---|
| `scoring_core` | `deriv_scores_r4` / `_rank_rows_r4` / `evaluate_r4` / `rank_matrix_r4` / `random_rank_matrix_r4` | **逐字节一致** | **只读复用** r4 原 `shards/` `signals/`（未重算评分） |
| `forward_detail` | `forward_from_series_r4` | **逐字节一致** | **只读复用** r4 原 `forward/lag{0,1}/*.jsonl`（逐片段明细） |

**必须重算（本批已全部重算）**：events（C6 段数）、forward 汇总（C2 跨界排除）、reverse（C5 剩余机会）、
features（C1 时钟/缺口）、supplementary（C2/C3 标签与切分）、attention/retention（C4 人工量）、
outcome（C3 直接后果）、integrity、diff。

**证据文件**：`closeout/reuse_decision.json`、`closeout/source_snapshot_before.py`（运行前源码快照）、
`closeout/run_manifest.json`（代码/输入/产物身份清单）、`closeout/r4_originals_before.json` / `r4_originals_after.json`。

> 复用效果：baseline 由 ~166 s 降到 ~2 s；forward 逐片段由数百秒降到数秒。

---

## 3. C1–C8 逐项：改了什么 / 实测证据

| 项 | 旧问题 | 本轮做法 | 实测证据（期望值 → 结果） |
|---|---|---|---|
| **C1 特征时间** | 用**当根** close/high/low/volume 算特征后放到该根**开盘时间**的小时格；主评分在该时点只用**上一根已完成** K 线 → 两时钟错开，未收盘 K 线可污染当时特征 | K 线 i 只服务决策小时 `(ts_i − MAIN_START)/1h + 1`；重采样到**连续整点网格**（缺口小时 = NaN），`rolling(min_periods=W)` 遇缺口自动给 NaN | 决策小时 1500 的未完成 K 线不再改变该时点特征（`ret30d` 不被从 0.0 抬到 0.5）；特征文件 743 → **754**（754 个对象中 **711** 个有可用特征），行数 **15,969,585** |
| **C2 切分** | 跨界项加进 `cross30/cross90` 后**仍无条件**进探索组 | 按**真实结果窗口**分组：跨界项**不进探索组**，只留专表；补充结果的时间切分同样执行 | `cross_boundary` 期望 explore=0 → 实测 `explore_30d_clean=408,883`、`cross_30d=13,432`、`cross_90d=37,656`、`review=301,382`、`reconciles=true` |
| **C3 结果目标** | `_build_labels` 标的是「未来 30 天出现**事后事件起点**」，被当成「从当前参考价起随后 30 天涨到两倍」 | 新增 `_outcome_worker_r4` / `cmd_outcome_r4`：从**当时参考价**起真实 720h 最高触及；与旁证标签**分名分列**；未到期/缺口/无参考价建**有效性掩码**，不默认负例；排序与分母统一只在 `eligible` 上 | 旁证标签 `label_at_start=true` 但实际 30 天最高触及仅 **1.0×** → 两个标签现在分开列；`eligible_cells=15,208,478` / 40,175 小时 |
| **C4 人工量** | 先每天合并再与前一天比，无法算小时级重入 | 小时底账（每次查询时刻名单变化）+ 固定 1/2/3/6 小时节奏（`ATT_CADENCES`）**同底账派生** | 同日重入不再漏；cadence1 N=20：首次 682 / 重入 **184,633** / 提醒 185,315；cadence6 重入 59,292（降频≠保住机会） |
| **C5 剩余机会** | 22 条「之后观察」全部**没有**剩余机会值 | 记**首次真正发现时刻**（pre→mid→late 最早），从该时刻起算剩余机会；**复用 forward 逐片段结果**，未命中才回退 1h 并如实记精度 | `late n=22`、`before_recorded_peak=17`、`with_remaining=22`、`remaining_source={"forward_reuse":22}`；不倒补峰值前高点 |
| **C6 事件段数** | 恒输出 `n_segments=1` | 合并前**真实连续段数**；事件增 `n_segments`/`segments`/`merge_gaps_hours` | 独立例（`[0,719]` + `[760,1479]`，间隔 41h）= **1 事件 2 段**；全量段数分布 `{'1':1409,'2':981,…,'37':1}`，**4,526 / 5,935 个事件是多段** |
| **C7 冻结/复用** | `verify_reuse()` 只核对 v3 模块摘要 | 新增 `scoring_core_fingerprint()`（含常量）与 `reuse_decision.json` 分组指纹 + 逐字节比对；`_baseline_worker_r4`/`_forward_worker_r4` 复用前核对指纹 | `all_groups_identical_to_snapshot=true`；`scoring_core` 与 `forward_detail` 均 `identical_to_snapshot=true` |
| **C8 报告订正** | 日期写 `2026-08-xx`、15,995,445 被当小时数、20,725 门控数被当受影响数、自检被称独立复核 | 见 `closeout/report_corrections.md`；baseline 汇总字段改名（`total_symbol_hour_cells_with_status` + `grid_hours`）；`_scoring_cell_diff` 逐格给真实受影响数 | 主范围 = **41,616 小时**；**改分格 28 / 改入选格 1**（不是 20,725） |

### 3.1 结果接对证明（§5.1.2：补充比较确实用了纠正后的特征与直接未来结果）

| 环节 | 读的是哪份数据 | 代码位置 | 结论 |
|---|---|---|---|
| 补充比较·排序分 | `closeout/` 复用的评分分片（评分核未变） | `cmd_supplementary_r4` → `load_matrices_r4` | 与 r4 原分片同源，未另立口径 |
| 补充比较·事件表 | `closeout/events/`（**C6 段数已重算**） | `cmd_supplementary_r4` → `load_events_r4()` | 用本轮事件，不是旧事件表 |
| 补充比较·单特征 / combo3 | `closeout/features/lag{0,1}/`（**C1 时钟+缺口已修正**） | `_load_feature_matrix()` → `features_dir()` | 特征确为本轮纠正版 |
| 补充比较·标签 | `_build_labels` 的 `event_start_proxy`（**旁证**） | 各产物 `label_kind / is_proxy_label` | 产物内**显式**标注 `is_proxy_label=True`，并指向 `outcome_lift` |
| 直接未来结果 | `closeout/outcome/`（从当时参考价起真实 720h 最高触及） | `cmd_outcome_r4` / `_outcome_worker_r4` | 独立文件、独立标签，不与旁证混用 |
| 直接后果·combo3 | 同上特征缓存（C1 修正版） | `cmd_outcome_r4` → `_load_feature_matrix` | 两处 combo3 定义一致 |

**lag0 / lag1 的输入与结果对应**：两情景各自的 `shards/signals/forward/reverse/attention/retention` 独立，
`lag1` 的评分用 `lag=1h` 的 OI/LS 观测，数值确实不同（如 forward 片段 723,702 vs 721,027），
**不是**同一份结果复制两份；两情景并列给出，不挑更优。

**查询频率对照（C4，同一份小时底账派生）**：见 §5 表；1h 节奏的重入/提醒显著高于 6h 节奏，
**降频不等于保住机会**（覆盖由同一底账派生，不随降频变化）。

---

## 4. 主结果（lag0 / lag1 并列，不挑更优情景）

### 4.1 逐时点重放（baseline，复用原分片）

| 指标 | lag0 | lag1 |
|---|---|---|
| 对象 / 有状态格 / 网格小时 | 754 / 15,995,445 / 41,616 | 同 |
| 选中小时格 | 7,534,798 | 7,545,386 |
| 选中片段 | 723,702 | 721,027 |
| OI 观测不足被门控 | 20,725 | 20,770 |

> 门控数是**计数**，**不是**受影响数（受影响数见 4.5）。

### 4.2 信号后果（forward）

| 指标 | lag0 | lag1 |
|---|---|---|
| 片段数 | 723,702 | 721,027 |
| verdict 对账 | **true** | **true** |
| 已达 2× / 仅 1.5× / 未达 1.5× | 34,825 / 77,201 / 580,352 | 34,664 / 76,843 / 578,298 |
| undetermined | 31,319 | 31,217 |
| no_reference | 5 | 5 |
| 最终可判 | 690,937 | 688,374 |

### 4.3 反向漏选（reverse）

| 去向 | lag0 | lag1 |
|---|---|---|
| 起点前曾观察 | 3,335 | 3,335 |
| 仅中途首次观察 | 53 | 53 |
| 之后观察 | **22** | **22** |
| 不可观察 | 2,525 | 2,525 |
| 对账 | true | true |

### 4.4 关注数量与人工负担（attention / retention）

- 覆盖曲线（score 排序，≤N 名，分母 = 5,935 事件中曾进排名的 3,341）：
  N=10→2,190；N=20→**2,879**；N=30→3,171；N=60→3,331；N=70→**3,335**；N=754→3,341。
  **与原 r4 逐点相同**（N=10/20/30/60/70 分别 2190/2879/3171/3331/3335）→ 相对原版**保留 100%**，新增/新漏 = 0。
- 保留刻度（已捕捉 3,335 / 未捕捉 63）：lag0 `80%→17、90%→23、95%→30、100%→73`；lag1 `100%→77`。
  **100% 已可达**（旧版截 60 够不到）；这只是阅读刻度，**不是已批准的漏选容忍线**。

### 4.5 补充结果（旁证标签）与直接后果（C3 新增）

- **旁证标签**（`event_start_proxy`，未来 30 天出现事件起点）：`base=0.11869`，`hours_used=41,615`；
  P@1/20/60 = 0.1269 / 0.1230 / 0.1184，lift = 1.069 / **1.036** / 0.997。
- **各排序 lift（tier2 @20）**：`score_deriv 1.051` > `score_total 1.036` > `trend 1.028` > `score_kline 1.008`。
- **单特征 lift（tier2 @20）**：`dist60h_asc 1.605` > `ret30d_asc 1.526` > `ret7d_asc 1.369` > `boxwidth_desc 1.334`。
- **时间切分（combo3）**：训练段 `base 0.1498 / P@20 0.2229`（lift 1.49）；后段 `base 0.0844 / P@20 0.1672`（lift 1.98）—— **两段同向**；跨界排除 720 小时。
- **直接后果（C3 新增）**：`eligible=15,208,478` 格 / 40,175 小时；`reach_2x` 基准 **0.05137**、`reach_1_5x` 基准 0.16732；
  **combo3 的 reach_2x P@20 lift = 1.599**（score_total 1.269、score_deriv 1.223、trend 1.077）。

### 4.6 事件与差异（r4 vs v3，只读对照）

- 事件总数 v3 5,932 → r4 **5,935**（新增 342 / 消失 339）；档位仅最低档 2.0：3,807 → 3,810。
- 峰值时间变化 5,593；30 天成熟标记变化 80；十倍事件 **111 → 111**（主时段内 **27**，与 v3 一致）。
- **逐格评分/入选差异（C8）**：两侧都可评分重叠格 15,960,233；**改分 28 格（全部为降）**；
  **改入选 1 格（取消入选 1、新增 0）**；跨度不一致 0。

---

## 5. 能判断取舍的表（§5.1.3）

> `N` 是**每轮（每小时一次查询）**看多少个币，**不等于每天只看这些币**；
> 「每天不同币」才是每日实际负担。原版 71 币是用户的负担锚点，不等于历史每轮固定 71 币。

| 每轮 N | 每天不同币（中位） | 累计首次加入 | 累计重入（1h 节奏） | 累计提醒（1h） | 累计提醒（6h 节奏） | 绝对机会覆盖（≤N 名） |
|---|---|---|---|---|---|---|
| 20 | 70 | 682 | 184,633 | 185,315 | 59,943 | 2,879（86.2%） |
| 30 | 103 | 698 | 277,349 | 278,047 | 89,310 | 3,171（94.9%） |
| 70 | 209 | 703 | 611,192 | 611,895 | 191,026 | 3,335（99.8%） |
| 754 | 379 | 704 | 364 | 1,068 | 720 | 3,341（100%） |

**lag1 同表（每轮 N=20 / 30 / 70）**：每天不同币 68 / 101 / 209；
覆盖 2,862 / 3,177 / 3,335。

- **相对原版保留**：覆盖曲线与原 r4 逐点相同 → **100%**；**新增 / 新漏 = 0**（评分与事件集未变）。
- **资料不足数**（事件级，与 N 无关）：5,935 个事件中 30 天未成熟 **177**、资料不全 **1,825**。
- **持续关注占用**（score N=20，lag0）：中位 **2 小时**、P90 11 小时、最长 913 小时（lag1：2 / 12 / 828）。
- **「71 币」锚点**：原版 71 币/轮 ≈ **N=70 行** → 覆盖 3,335（99.8%），但**每天实际看到 209 个不同币**。
  **每轮上限 ≠ 每天只看这些币**。

**查询频率对照（C4：1/2/3/6 小时，同一份小时底账派生）**

| 每轮 N | 节奏 | 累计首次加入 | 累计重入 | 累计提醒 | 每天不同币(中位) | 机会覆盖 |
|---|---|---|---|---|---|---|
| 20 | 1h | 682 | 184,633 | 185,315 | 70 | 2,879 |
| 20 | 2h | 674 | 118,942 | 119,616 | 70 | 2,879 |
| 20 | 3h | 669 | 91,703 | 92,372 | 70 | 2,879 |
| 20 | 6h | 651 | 59,292 | 59,943 | 70 | 2,879 |
| 70 | 1h | 703 | 611,192 | 611,895 | 209 | 3,335 |
| 70 | 6h | 701 | 190,325 | 191,026 | 209 | 3,335 |

- 降频**只压提醒数、不改变覆盖**（覆盖由排名底账派生，与查询频率无关）；
  反而首次加入 682 → 651（N=20），说明降频会漏掉一些**短暂的首次进入**。
  **不能**声称「降频保住了机会」。
- **lag1（N=20）**：1h 首次 680 / 重入 176,763 / 提醒 177,443；6h 首次 649 / 重入 58,551 / 提醒 59,200。

> 上表只给数量关系，**不替你挑 N**；可接受的漏选比例由主代理定。

---

## 6. 重点案例锚点（§5.1.4）

见 `closeout/anchors.md`（人读）与 `closeout/anchors.json`（机器读）。内容：

- **主时段 27 个十倍事件**：旧 ID（v3 键 `symbol@start_utc`）→ 本轮同键对应；
  列档位、**段数**、发现时刻、可用参考价与精度、剩余触达、剩余判定、资料状态、去向；
  同键缺失者给「最近同币事件相差小时」作为差异原因，**不强行凑旧数、不静默删消失项**。
- **22 条「之后观察」**：列首次真正发现时刻、参考价与精度、lag0/lag1 剩余触达与判定、
  资料状态、是否在记录峰值之前。

---

## 7. 实测命令与耗时

```
# 全量（唯一可复跑入口；新计算全部写 closeout/，不覆盖 r4 原产物）
py -3.10 -u tools/history_research_ds_r4.py all
#   → 日志 reports/history_research_ds/r4/closeout/run_closeout.log，末尾 EXIT=0

# 分步（按需）
py -3.10 -u tools/history_research_ds_r4.py events
py -3.10 -u tools/history_research_ds_r4.py baseline --lag-hours 0
py -3.10 -u tools/history_research_ds_r4.py forward  --lag-hours 0
py -3.10 -u tools/history_research_ds_r4.py reverse  --lag-hours 0
py -3.10 -u tools/history_research_ds_r4.py attention --lag-hours 0
py -3.10 -u tools/history_research_ds_r4.py retention --lag-hours 0
py -3.10 -u tools/history_research_ds_r4.py integrity --lag-hours 0
py -3.10 -u tools/history_research_ds_r4.py features --lag-hours 0
py -3.10 -u tools/history_research_ds_r4.py supplementary --lag-hours 0
py -3.10 -u tools/history_research_ds_r4.py outcome  --lag-hours 0
py -3.10 -u tools/history_research_ds_r4.py diff

# 测试
py -3.10 -B -m unittest discover -s tests -p "test_history_research_ds_r4.py"   # 53 项全过
py -3.10 -B -m unittest discover -s tests -p "test_history_research_ds.py"      # 36 项全过（v3 未受影响）

# 交接辅助（只读输入，只写 closeout/）
py -3.10 reports/history_research_ds/r4/closeout/_make_manifest.py
py -3.10 reports/history_research_ds/r4/closeout/_make_anchors.py
```

**耗时（冻结源码整轮）**：events ≈ 75 s；baseline 每 lag ≈ 2 s（复用）；forward 每 lag ≈ 90–112 s；
reverse 每 lag ≈ 2 min；attention 每 lag ≈ 5–6 min；retention/integrity 数秒；
features ≈ 15 s；supplementary ≈ 96 s；outcome ≈ 25 s；diff 数秒。全程离线。

---

## 8. 未查范围与限制

1. **历史市值闸门不可重建**：归档无市值序列 → 当时的「成交额/市值」闸门记 unknown。
2. **当时交易资格不可知**：文件存在 ≠ 当时该现货对在交易。
3. **分片发布时刻不可知**：归档只记录下载时间。
4. **更名/换币（KLAY→KAIA 等）未做身份映射**，只表现为序列缺口。
5. **未测任何买卖点、持有期与兑现规则**。
6. **`combo3` 只按等权分位平均实现**，权重与「boxwidth 降序」的依据仍属待验证假设。
7. **A/B 两张卡的组合效果未测**；两卡独立统计**不得**直接相乘或拼接。
8. **`outcome`（直接后果）只算到 lag0**；lag1 未跑。不影响 C3 结论方向，但「两延迟并列」在直接后果上只给了 lag0。
9. **原 r4 目录的 4 处改动全部为授权改动**（`run_manifest.json → r4_originals_integrity.unexpected_changed` 为空）：
   - `r4/baseline_summary_lag{0,1}.json`：首次（已被取代的）试跑按旧硬编码路径写入；源码已加输出路径参数并重跑，
     **数值字段完全一致**，仅 `generated_at_utc` / `elapsed_sec` / `per_symbol` 顺序不同。
   - `r4/ready_for_glm.md`：按 §2.7 追加日期指针，原结论文字未改。
   - `r4/independent_verification.md`：按 §2.8「自检应称自检」订正 H1 标题，正文一字未改。
   **其余 r4 原产物零改动**（7,205 个文件内容哈希比对，不以 mtime 为证）。
10. **本批未改动**：生产代码、v3 产物与 v3 测试、`KNOWLEDGE.md` 等公共知识文档、glm 文件。

---

## 9. 停止编辑声明

本批（ds 的 closeout 部分）**到此停止编辑**。已冻结：

- 源码：`tools/history_research_ds_r4.py`（`d05517c3…`）、`tests/test_history_research_ds_r4.py`（`2ba23dc3…`）
- 产物：`reports/history_research_ds/r4/closeout/` 全部（含 `run_manifest.json`）
- 入口：本文件

交接后不再边改边等验收，不自行转入生产或新候选实验。
候选正式定稿由主代理按本文件证据负责。
