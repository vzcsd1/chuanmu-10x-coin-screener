# B4 第 1 步 · 有限返修（r2）交接给 glm（ds，2026-10-09）

> ## 本批已停止编辑，交 glm 只读验收。
>
> 自本文件写定起，`tools/b4_replay_ds.py`、`tests/test_b4_replay_ds.py` 与
> `reports/b4_stage1_ds/r2/` 全部产物**冻结**，ds 不再修改。
> 本批**未联网、未改生产评分/权重/门槛/币池、未写生产文件、未跑全历史、未重启归档**。
> 验收范围以 `summary.md` 的「已证明 / 未证明」为准；**本批不构成策略有效或盈利结论**。

---

## 1. 环境（实测）

| 项 | 值 |
|---|---|
| OS | Windows-10-10.0.22621-SP0 |
| 解释器 | **`py -3.10`** → Python 3.10.9 |
| pandas | 2.3.3 |
| numpy | 2.1.2 |
| ccxt | 4.5.77（仅被生产模块 import，运行期无任何网络调用） |
| 网络 | 全程 **0 次外连**（运行期 socket 守卫计数） |

## 2. 冻结版本与 SHA256

| 文件 | 字节 | SHA256 |
|---|---:|---|
| `tools/b4_replay_ds.py` | 91,720 | `e4c6e9afdf36f944339aa7497349cc2b1b68e8678aa26d1eacebdc7d8df318e5` |
| `tests/test_b4_replay_ds.py` | 30,411 | `00b8a9754d5479df24351e56d66a248ad4f2b179781d1e9c1773a197a9091886` |
| **生产评分（只读引用，未改）** `binance_box_strategy.py` | 80,104 | `d1cc20d875a991b87b476aba9736fba449a9bd9351a20b17d0be16aa5ea80826` |
| **路径映射真源（只读复用）** `tools/archive_download_ds.py` | 79,907 | `ae59b1e18f02d130bcb42ee81fb904bc5ec8ec4097480690038617a19556c086` |
| **队列真源** `reports/archive_download_ds_full/queue_v3.csv` | 280,039,855 | `93fe8699ce155efa8dd7b458303bc469a9b83844c1cf6642af4342eab555ef84` |

**r1 旧交付（13 份，逐份未变）** —— 见 `hashes.json` 的 `r1_frozen_delivery`；
关键锚点：`scoring_output.jsonl = 125a0ec00c0aff5e24366db4afc0b283095ac4ef88878b7d1cdc6bb406e7c6d5`、
`summary.md = 80821683391d3b3ff21bf5b0cceb974df9958f30578ec00211a881c7e1d5ce62`、
`ready_for_glm.md = 83449224f5bf82851db21edc69b2807a9ad40ef6430f09f26bd4798dae33fe93`。

**主代理审核目录（4 份，逐份未变）** —— 见 `hashes.json` 的 `reviewer_dir_readonly`；
`probe_results.json = 6ab40e800a4ac9f8ff6d8aba86bb901b37648d52d42ca898732ca88d5fd69e0f`。

**r2 产物** —— 逐文件 SHA256 见 `r2/hashes.json`（在本文**之后**生成，以其为准）。
本文不列自身哈希（自引用）。

> `cost.json` / `segment_summary.json` / `run_log_r2.txt` 含运行时间戳/耗时字段，**重跑后 SHA256 会变**；
> `pilot_manifest.json`、`scoring_output.jsonl`、`input_contract.json`、`structure_cases.json`、
> `d1_d5_probes.json`、`diff_vs_r1.json` 是确定性的，重跑应完全一致（可比对锚点）。
> `pilot_manifest.json` 与 `structure_cases.json` 与 r1 **同哈希**
> （`cd55f25f…` / `84e12a8c…`）——样本清单与覆盖证据未变。

> ⚠ 若共享代码在本批之后变化，**不能混用两个版本宣布一致**：保留本批原结果、报告冲突，
> 并在同一个明确版本上重跑受影响的小样本。本批**未修改** `binance_box_strategy.py` /
> `tools/archive_download_ds.py`。

## 3. 复跑命令（PowerShell，项目根目录，实测通过；**不会覆盖 r1 旧交付**）

```powershell
# 0) 边界测试（52/52，约 46 秒；测试只写系统临时目录，不碰任何交付目录）
py -3.10 -B -m unittest discover -s tests -p "test_b4_replay_ds.py" -v

# 1) 返修批全流程：manifest + contract + cases + pilot + probes + diff + hashes
#    默认输出 reports/b4_stage1_ds/r2（独立子目录），不重做 coverage 百万路径扫描
py -3.10 -B tools/b4_replay_ds.py r2

# 2) 只跑 D1–D5 独立对照（回放 vs 生产假接口，秒级）
py -3.10 -B tools/b4_replay_ds.py probes

# 3) 只做 r1→r2 差异（读两个目录，写 r2/diff_vs_r1.*）
py -3.10 -B tools/b4_replay_ds.py diff

# 4) 只重算哈希（证明旧交付/生产/审核目录未变）
py -3.10 -B tools/b4_replay_ds.py hashes

# 5) 延迟敏感性：显式独立目录，与主试跑互不覆盖
py -3.10 -B tools/b4_replay_ds.py pilot --deriv-lag-hours 1 --out-dir reports/b4_stage1_ds/r2/lag1

# 6) 冒烟（每段 3 个时点，秒级；请自带独立 --out-dir，避免覆盖 r2 主产物）
py -3.10 -B tools/b4_replay_ds.py pilot --limit-points 3 --out-dir reports/b4_stage1_ds/r2/smoke
```

- **显式覆盖输出目录**：`--out-dir <路径>`（默认 `reports/b4_stage1_ds/r2`）。
- ⚠ **不要**用 `--out-dir reports/b4_stage1_ds`（那会覆盖 r1 冻结交付）。
  `all` 子命令含 coverage（百万路径扫描，约 45 秒 / 峰值 3.5 GB），本批**未运行**，本批不需要。
- 复核主代理反例（只读，向审核目录写同名结果；已实测其输出哈希不变）：
  `py -3.10 -B reports/b4_stage1_review/probes.py`

## 4. 输入 / 输出路径

| 角色 | 路径 |
|---|---|
| 输入 · 原始归档（只读） | `data/archive_raw/`（spot 1h、futures metrics 等） |
| 输入 · 队列真源 | `reports/archive_download_ds_full/queue_v3.csv` |
| 输入 · 生产评分（只 import） | `binance_box_strategy.py` |
| 输入 · 覆盖证据（**复用 r1，未重跑**） | `reports/b4_stage1_ds/coverage.json` |
| 输入 · 主代理反例（只读） | `reports/b4_stage1_review/` |
| 输出 · 本批全部产物 | `reports/b4_stage1_ds/r2/`（主试跑） |
| 输出 · 延迟敏感性 | `reports/b4_stage1_ds/r2/lag1/` |
| 旧交付 · **只读，未覆盖** | `reports/b4_stage1_ds/*`（r1 顶层 13 份） |

## 5. 通过 / 失败 / 未查项

### 通过

- 边界测试 **52/52 通过**（新增 D1–D5 对抗性断言：接口窗口、≥25 有效 OI 观测、
  `inf` 过滤、LS 合法零值、预热 25/26/27 边界、未评分不得入选、延迟自洽、输出隔离）。
- D1a–D1d 四个合成用例：回放与生产假接口**逐字段相等**（修复前分别为 10 vs 3、10 vs 3、7 vs 10）。
- 576 时点全部有输出，与 r1 **同键**（仅 r1 有 0、仅 r2 有 0）；"未评分却 selected" 6 → **0**。
- r1 冻结交付 13 份、生产文件 3 份、审核目录 4 份哈希**逐份未变**（`hashes.json`）。
- 外连拦截计数 **0**（`cost.json` 与 `d1_d5_probes.json`）。

### 失败

- 无。

### 未查（显式列出，不得当作已通过）

1. metrics 整点行与线上 `/futures/data/*` 1h 端点**未逐点比对**（需联网，本批禁止）。
2. 全库**行级**覆盖未查（只查了 pilot 12 段；其余分片仅文件级对账，沿用 r1 证据）。
3. 历史市值、当时交易资格、分片发布时刻**不可重建** → 线上全流程未复现。
4. 退市/更名身份映射未做（KLAY→KAIA、FTT 退市只表现为缺口/OI 塌陷）。
5. 未来标签（后续收益/持有路径）**未实现**，按任务书留给下一批。
6. 采用点边界未改：决策前恰好缺 1 根（`gap_hours == 1.0`）**仍判 ok**；
   本批只如实记录（`adopted_is_immediate_prev=false`），**未新增过滤**。
7. 延迟 1h 会改变 2 个入选（224→222）→ lag=0 是**显式假设**，未取证。

## 6. 建议 glm 独立验收的检查点

1. 复跑第 0 步与第 1 步，比对 `r2/scoring_output.jsonl`、`d1_d5_probes.json`、`diff_vs_r1.json`
   的 SHA256 是否与 `r2/hashes.json` 一致。
2. 抽查 `d1_d5_probes.json`：`D1a–D1d` 的 `equal` 必须全为 `true`，
   `D4_delay_cutoff.whole_output_equal` 必须为 `true`。
3. 抽查 `diff_vs_r1.json`：`selected_that_are_unscored_r2` 必须为 **0**；
   `changed_field_counts.score_deriv` 必须为 **0**（证明分数未变、只有语义/标签变）。
4. 抽查任一条记录：`deriv.window.endpoint_limit == 200`、`deriv.oi_min_obs_required == 25`、
   `field_adoption.deriv_availability_cutoff_utc` 等于 `decision_time − lag`。
5. 抽查 `screening_status` 与 `selected` 一致：`selected=True ⇒ kline.status=="ok"` 且 `score_total!=null`。
6. 复核 `summary.md` 第 4 节的 215 行归因是否与 `diff_vs_r1.json` 完全对得上
   （48 + 3 + 207 + 5，且互不重叠；`score_kline` 变化 208 = 207 + 1 条因 D2 转为已评分）。
7. 复核 `hashes.json`：`r1_frozen_delivery` / `production_files_readonly` /
   `reviewer_dir_readonly` 三组 `all_unchanged` 必须为 `true`。
