# B4 第 1 步 · 有限返修（r2）摘要（ds，2026-10-09）

**一句话**：按主代理审核报告 `reports/b4_stage1_review/summary.md` 的 D1–D5 逐项返修了历史接入与测试隔离；
同一份 **576 时点清单**重跑后，**行数不变（576→576）**，**变化 215 行**，全部可归因到"输入重建/语义修正"，
**没有任何权重、门槛、币池或生产评分被改动**；旧 r1 交付、生产文件与审核目录哈希**逐份未变**。

- 目标边界（保留）：在活下来的前提下反复捕捉多倍行情；十倍只是一档目标不是入选硬下限；
  不能转成通用胜率优化、提高门槛压短名单或固定小止盈。
- 本批范围：**只做审核裁定的第一阶段返修与重跑**，不跑全历史、不联网、不改生产评分/权重/门槛/币池、
  不重启归档、不新增 UI/数据库。
- 本批影响的是**"历史发现证据是否可信"**，**不是**进入/持有/兑现，也没有新增任何收益或胜率证据。

---

## 1. 先复现审核反例（修复前，留证）

`py -3.10 -B reports/b4_stage1_review/probes.py`（只读冻结交付，向审核目录写结果）：
结果与审核报告完全一致，`reports/b4_stage1_review/probe_results.json` 哈希 **未变**
（`6ab40e80…`）。原样输出留证于 `probe_before_stdout.txt` / `probe_before_reviewer_copy.json`。

| 编号 | 修复前（复现值） | 生产假接口参照 |
|---|---|---|
| D1 稀疏 OI（仅 T−72h/T−24h/T 三点） | 回放 **10 分 / ok** | **3 分 / partial** |
| D1 接口窗口（300 条旧观测） | 回放拿窗口外旧 OI 加分，**10 分 / ok** | **3 分 / partial** |
| D1 LS 尾行 inf | 回放 **7 分** | **10 分**（过滤后回退到前一有效点） |
| D2 预热（26 根闭合） | 回放 **kline_short_history / 0 分** | 生产 **27 行通过 / 7 分** |
| D3 未评分 K 线 | 回放 **selected=True**（总分 7） | 生产会在合约取数前 `continue` |
| D4 lag=1h | 加载/不加载 T 行 → `stale_tail 1→0`，输出**不等** | — |
| D5 自测落盘 | `run_pilot` 写固定 `OUT_DIR`（= r1 顶层） | — |

## 2. 逐项返修（D1–D5）

### D1 合约契约对齐（`deriv_as_of`）
按生产 `binance_box_strategy.py:1005-1100` 的顺序与窗口重建，**未改生产函数**：

1. **可得截止唯一**：先按 `decision − lag` 截断，再取**接口窗口**（`limit = DERIV_LOOKBACK = 200` 条 1h）。
   超出窗口的旧观测不再参与（修复前会拿 300 条里的旧 OI 当基准）。
2. **OI 过滤**：`isfinite(v) and v > 0`（修复前只用 `notna() and >0`，会把 `inf` 当有效值）。
3. **最小观测数**：有效 OI 观测 **>= 25** 条才允许算 `oi_chg_1d/3d`；否则保持 `None`（**不可算 ≠ 0 分**）。
4. **LS 过滤**：只要求 `isfinite(ratio)`，**合法零值保留**（不自行改成必须正数）。
5. **24h/72h 基准**在同一窗口、同一过滤后的序列里回退取 `<= 截止` 的最后一个观测。
6. **读取跨度**：`metrics` 读取从 5 天扩到 `ceil(200/24)+2 = 11` 天，
   覆盖真实请求窗口（8.33 天）——修复前 pilot 少读数据，会把"没读到"误报成"数据缺失"。
   （`cost.json.metrics_warmup_days = 11`；K 线侧 12 天不变。）

### D2 预热门槛按真实输入行数对齐（`score_timepoint`）
判定改为 `len(indicators(闭合窗口 + 占位行)) < box_period + 3 (=27)`，与生产
`len(df) < cfg.box_period + 3` 同口径。**占位行（代表形成中那根）计入行数** →
门槛从"27 根闭合"纠正为"26 根闭合"。边界 25/26/27 均已测试。

### D3 未评分不得 selected、未知不得变零
- `score_kline` / `score_total` 在 K 线未评分时记 **`None`**（修复前 `int(x or 0)` 把未知写成 0）。
- `selected` 必须 **K 线已评分** 且总分过门槛；新增 `screening_status`
  （`selected` / `scored_below_threshold` / `skipped:<原因>`）。
- 合约侧诊断照旧计算，但用 `score_deriv_diagnostic_only` 与 `threshold_compare.is_selection_signal=false`
  显式标注**不是入选信号**。历史市值/交易资格仍记 `unknown`。

### D4 延迟统一可得截止
`oi_stale_tail` / `ls_stale_tail` 的统计区间改为 `(采用点, 可得截止]`，
与采用点、基准、状态共用同一个界。修复后 lag=1h 时"加载 T 行"与"不加载 T 行"**输出完全相同**。

### D5 输出目录隔离
- 默认输出目录改为 `reports/b4_stage1_ds/r2`（**不再指向 r1 顶层**）；
  新增 `--out-dir` 与 `set_out_dir()`。
- 测试**全部显式传临时目录**，并新增 `test_run_pilot_does_not_touch_r1`（跑前后比对 r1 逐文件哈希）。
- 试跑 / 测试 / 延迟敏感性三份留证互不覆盖：`r2/`、`r2/test_log.txt`、`r2/lag1/`。

## 3. 修复后独立对照（`d1_d5_probes.json`）

同一批合成输入同时喂**回放**与**生产假接口**（`_fetch_deriv_context_live`），逐字段比较：

| 用例 | 回放 | 生产假接口 | 一致 |
|---|---|---|---|
| D1a 稀疏 OI（3 个观测） | partial / **3 分**，`oi_chg=None` | partial / 3 分 | ✅ |
| D1b 接口窗口（300 条） | partial / **3 分** | partial / 3 分 | ✅ |
| D1c LS 尾行 inf | ok / **10 分**，回退到 T−1h | ok / 10 分 | ✅ |
| D1d LS 合法零值 | ok / **10 分**，`ls_top=0.0` 保留 | ok / 10 分 | ✅ |
| D2 预热 25/26/27 根 | 26 行不足 / 27 行 ok（突破例 **7 分**） | 27 行通过 / 7 分 | ✅ |
| D3 未评分 + 合约满分 | `selected=false`、`score_total=null` | 生产会跳过 | ✅ |
| D4 lag=1h 加载 vs 不加载 T 行 | **whole_output_equal = true** | — | ✅ |
| D5 默认输出目录 | `reports/b4_stage1_ds/r2`（非 r1 顶层） | — | ✅ |

## 4. 576 时点重跑差异（同一清单，`diff_vs_r1.md/json`）

| 指标 | r1 | r2 |
|---|---:|---:|
| 行数 | 576 | **576** |
| 变化行数 | — | **215** |
| `selected` | 229 | **224** |
| **未评分却 selected**（D3 缺陷） | **6** | **0** |
| K 线状态 | gap 91 / short 78 / none 41 / ok 366 | gap 91 / short **75** / none 41 / ok **369** |
| 合约状态 | failed 175 / partial 161 / ok 192 / no_futures 48 | failed **127** / partial 161 / ok **240** / no_futures 48 |
| 去向 | — | selected 224 / below_threshold 145 / skipped 207 |
| 采用点 ≠ decision−1h | 1 | 1 |

**215 行的逐条归因（四类，互不重叠）**：

| 类别 | 行数 | 内容 |
|---|---:|---|
| D1 读取跨度修复 | 48 | S03 `KLAYUSDT` 全段 `deriv_status failed→ok`。r1 只读 5 天（< 8.33 天窗口），KLAY→KAIA 过渡期的尾部无效行占满读取窗口 → 误报 failed；r2 读 11 天，窗口 131 行（2024-10-17…10-26）、有效 OI 观测 130 个 ≥ 25 → ok。**`score_deriv` 变化行数 = 0**，即这只是**诊断标签**修正，不是分数变化 |
| D2 预热差一根 | 3 | `kline_short_history→ok`：WIFUSDT 2024-03-06T16（K 线 5 分，总分 7→**12**，仍 selected）；ORDIUSDT 2023-11-08T14（0 分，总分 4）；1000SATSUSDT 2023-12-13T14（0 分，总分 3）。后两条仍低于门槛 5 |
| D3 未知不再写成 0 | 207 | 未评分行的 `score_kline`/`score_total` 由 `0` 改 `None`（其中 1 行因 D2 变为已评分，故 `score_kline` 变化共 208 行） |
| D3 未评分不得入选 | 5 | WIFUSDT 2024-03-05T12 / 03-06T08、T09、T10、T15 由 `selected True→False`（K 线 `no_kline_data`/`short_history`，r1 总分 7 是"合约分冒充实入选"）。原 6 条缺陷中第 6 条（03-06T16）经 D2 修复后**K 线真实评分 5 分**，属**正当入选** |

> 结论：**没有任何一条变化来自策略或参数调整**。`score_deriv` 变化 0 行、权重/门槛/生产评分未改。
> 重跑只是把"多算的分"与"误标的入选"校正回来。**不能把本批差异当成策略改善或恶化。**

### 附带事实（撤销旧交接的无条件说法）
- r1/r2 都有 **1 条** `kline.status=ok` 但采用点 ≠ `decision−1h`：
  `S03 KLAYUSDT 2024-10-28T04:00Z` → 采用 **02:00Z**，`gap_hours=1.0`，`n_window_holes=0`。
  即决策前一根（03:00）缺失，`gap_ms > 1h` 不成立故仍判 `ok`。
  现在每行都记 `adopted_is_immediate_prev`、`gap_hours`、`n_window_holes`，
  **不再声称"ok 恒为 decision−1h"**；也**没有**为凑一致而新增过滤规则。
- D1 窗口截断**确实生效**：493 个有窗口的记录里 **192 个** `window_rows == 200`（真的被截断）。

## 5. 延迟敏感性（`r2/lag1/`）

`--deriv-lag-hours 1` 与 lag=0 对比（576 行同键）：

| 项 | 值 |
|---|---:|
| 有差异行数 | **373** |
| `oi_as_of` / `ls_as_of` 变化 | 277 / 353 |
| `score_deriv` 变化 | **31** |
| `score_total` 变化 | 13 |
| `selected` | 224 → **222** |
| 合约 `ok` 行数 | 240 → 240 |

→ **延迟假设不是装饰**：换一个 1 小时延迟就会改 2 个入选。lag=0 是**显式假设**，
必须与结果一起引用（见 `input_contract.json.availability_basis`）。

## 6. 成本（实测，非估算）

| 步骤 | 命令 | 耗时 | 峰值工作集 |
|---|---|---:|---:|
| 边界测试（**52/52 通过**） | `py -3.10 -B -m unittest discover -s tests -p "test_b4_replay_ds.py"` | 45.8 s | — |
| r2 小段重跑（576 时点） | `py -3.10 -B tools/b4_replay_ds.py r2` | 9.2 s | 133.0 MB |
| 延迟敏感性 | `... pilot --deriv-lag-hours 1 --out-dir .../r2/lag1` | 10.7 s | 132.8 MB |
| 覆盖盘点 | **未重跑**（复用 r1 `coverage.json`） | — | — |

- r2 小段读取：95 个分片 / 1,269,550 压缩字节 / 3,897,155 解压字节 / 30,045 行；输出 1,627,488 字节。
- 全程**外连拦截计数 = 0**（`cost.json.network_blocked_attempts`、`d1_d5_probes.json` 均为 0）。
- 按币/分片读取，未一次载入全库，未复制原始库。

## 7. 已证明 / 未证明

### 已证明
- **P1 契约一致性（合成）**：D1a–D1d 四例回放与生产假接口**逐字段相等**（修复前 10 vs 3、7 vs 10）。
- **P2 预热一致性**：25/26/27 根闭合的边界上，回放"是否 ok"与生产"是否过长度闸门"完全一致。
- **P3 语义正确**：未评分 → `selected=false`、`score_total=null`、`skipped:<原因>`；
  576 行中"未评分却 selected"由 6 → **0**。
- **P4 延迟自洽**：同一 lag 下，加载/不加载尚未可得的行，输出**完全相同**。
- **P5 隔离**：r1 冻结交付 13 份、生产文件 3 份、审核目录 4 份哈希**逐份未变**；
  默认输出目录不是 r1 顶层；测试不再落盘到交付目录。
- **P6 分母**：576 行与 r1 完全同键（仅 r1 有 0、仅 r2 有 0）。

### 未证明（必须随结论一起引用）
- **N1** 未复现线上全流程：历史市值与当时交易资格不可重建 → 只验证"同一可观察子集（K线+合约）"。
- **N2** 未逐点比对 metrics 整点行与线上 `/futures/data/*` 1h 端点是否数值相等（需联网，本批禁止）。
- **N3** 分片发布时刻未记录 → 只能按显式假设（已闭合即可用、整点观测整点可得）。
- **N4** 未评估任何收益/胜率/命中率（本批不含未来标签）。
- **N5** 全库只做文件级对账，**行级只查了 pilot**；其余分片仍可能有"文件在、内容空"。
- **N6** 身份映射未做（KLAY→KAIA、FTT 退市只表现为缺口/OI 塌陷）。
- **N7** 采用点边界：决策前恰好缺 1 根（`gap_hours == 1.0`）**仍判 ok**。
  这是既有规则（`gap_ms > 1h` 才判 gap），本批**未新增过滤**，只如实记录。

## 8. 产物清单（均在 `reports/b4_stage1_ds/r2/`）

| 文件 | 内容 |
|---|---|
| `summary.md` | 本文件 |
| `scoring_output.jsonl` | 576 条逐时点输出（新增 `screening_status`、`adopted_is_immediate_prev`、`n_window_holes`、`deriv.window` 等） |
| `diff_vs_r1.md` / `diff_vs_r1.json` | 前后变化与逐条归因（215 行明细） |
| `d1_d5_probes.json` | 修复后的 D1–D5 独立对照（回放 vs 生产假接口） |
| `probe_before_stdout.txt` / `probe_before_reviewer_copy.json` | 修复前审核反例复现留证 |
| `test_log.txt` | 边界测试完整日志（52/52 OK） |
| `run_log_r2.txt` | 本批全部命令与输出（含时间戳） |
| `hashes.json` / `r1_baseline_hashes.json` | 前后哈希与基线（证明旧交付/生产/审核目录未变） |
| `input_contract.json` | 输入契约（含接口窗口、最小观测数、可得截止、预热口径） |
| `pilot_manifest.json` | 与 r1 同哈希（`cd55f25f…`）——样本清单未变 |
| `segment_summary.json` / `raw_refs.json` / `cost.json` | 分段状态、读取分片哈希、实测成本 |
| `structure_cases.json` | 与 r1 同哈希——覆盖盘点未改，复用原证据 |
| `lag1/` | 延迟 1 小时的独立试跑（不与主试跑互相覆盖） |
| `ready_for_glm.md` | 交接与复跑（**最后写**） |

## 9. 本批不改变什么

- **不改** `binance_box_strategy.py`（`d1cc20d8…` 未变）、**不改**权重/门槛/币池、**不联网**。
- 指标来源、发布时刻、历史市值、当时交易资格等**未取证项继续写未知**；
  不因未知把项目判死，也不假装完整可交易。
- 主代理审核目录 `reports/b4_stage1_review/` **未被修改**（4 份哈希一致）。
