# B4 第 1 步 · 交接给 glm（ds，2026-10-09）

> ## 本批已停止编辑，交 glm 只读验收。
>
> 自本文件写定起，`tools/b4_replay_ds.py`、`tests/test_b4_replay_ds.py` 与
> `reports/b4_stage1_ds/` 全部产物**冻结**，ds 不再修改。
> 本批**未联网、未改生产评分/权重/门槛、未写生产文件、未扩大为全历史研究**。
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
| 网络 | 全程 **0 次外连**（运行期 socket 守卫计数；`cost.json.network_blocked_attempts = 0`） |

## 2. 冻结版本与 SHA256

| 文件 | 字节 | SHA256 |
|---|---:|---|
| `tools/b4_replay_ds.py` | 62,382 | `7f6373f4ab856825927e392dde0bd2b0e67077a94899ee816041318e0344b787` |
| `tests/test_b4_replay_ds.py` | 16,040 | `861614a3d8b59b24a1833f25951e68ce0e2d56253b1155e76f2c8d83a5046528` |
| **生产评分（只读引用，未改）** `binance_box_strategy.py` | 80,104 | `d1cc20d875a991b87b476aba9736fba449a9bd9351a20b17d0be16aa5ea80826` |
| **队列真源** `reports/archive_download_ds_full/queue_v3.csv` | 280,039,855 | `93fe8699ce155efa8dd7b458303bc469a9b83844c1cf6642af4342eab555ef84` |
| `reports/archive_download_ds_full/progress_v3.json` | 667 | `7d5298bf9c4f4347d4ee805367d5813455d4f8cd13f9f14913beb919bfdc0bdf` |
| **路径映射真源（只读复用）** `tools/archive_download_ds.py` | 79,907 | `ae59b1e18f02d130bcb42ee81fb904bc5ec8ec4097480690038617a19556c086` |
| `reports/b4_stage1_ds/pilot_manifest.json` | 4,098 | `cd55f25f1f83848a314044ee36429bcd2fb66ddbb7f090aabc7bfd5046490640` |
| `reports/b4_stage1_ds/scoring_output.jsonl` | 1,082,158 | `125a0ec00c0aff5e24366db4afc0b283095ac4ef88878b7d1cdc6bb406e7c6d5` |
| `reports/b4_stage1_ds/coverage.json` | 10,017 | `6f126487041d0f8fce1615ded7082b92c82ce224434d37eecf3204cb2007384a` |
| `reports/b4_stage1_ds/input_contract.json` | 4,642 | `1753d68f1090a1f3dbd0dc13816d5ac1447f9d2512305b411b079e19d33759f1` |
| `reports/b4_stage1_ds/structure_cases.json` | 7,085 | `84e12a8caf3c29ebdef9bb9dc06ff295b74ae76f8aae141e7de1939c86edc254` |
| `reports/b4_stage1_ds/segment_summary.json` | 10,675 | `1d1b7590d9bb30184977800285e0b5c42482184c972aeaa740f7bf25095bdae5` |
| `reports/b4_stage1_ds/cost.json` | 637 | `86ebaf07f400e2eb1c4335a392b638857e6740682041204ca3d37dc25f2dda03` |
| `reports/b4_stage1_ds/raw_refs.json` | 15,596 | `7fe723538321d1ed1d72971229b3f6673f1cb24cdd97149504bf39c7c14a086f` |
| `reports/b4_stage1_ds/run_log.txt` | 1,939 | `d32a64a8143e5d3d0ff450c92a390d11ad7ca631aca9d8b53e97934cdc5e32b4` |

> `coverage.json` 与 `cost.json` 含运行时间戳/耗时字段，**重跑后 SHA256 会变**；
> `pilot_manifest.json`、`scoring_output.jsonl`、`input_contract.json`、`structure_cases.json`、
> `segment_summary.json`、`raw_refs.json` 是确定性的，重跑应完全一致（这是可比对的锚点）。

> ⚠ 若共享代码在本批之后发生变化，**不能混用两个版本宣布一致**：
> 应保留本批原结果、报告冲突，并在同一个明确版本上重跑受影响的小样本。
> 本批**未修改** `binance_box_strategy.py` / `tools/archive_download_ds.py`。

## 3. 复跑命令（PowerShell，项目根目录，实测通过）

```powershell
# 0) 边界测试（33/33，约 25 秒）
py -3.10 -m unittest discover -s tests -p "test_b4_replay_ds.py" -v

# 1) 样本清单（先定样本，再算分）
py -3.10 tools/b4_replay_ds.py manifest

# 2) 时间输入契约
py -3.10 tools/b4_replay_ds.py contract

# 3) 覆盖与逐份对账（约 43 秒；峰值工作集约 3.5 GB）
py -3.10 tools/b4_replay_ds.py coverage

# 4) 真实结构案例证据
py -3.10 tools/b4_replay_ds.py cases

# 5) 小段实际试跑（约 8 秒；峰值工作集约 130 MB）
py -3.10 tools/b4_replay_ds.py pilot

# 一次性跑完 1~5（同一进程，峰值内存按 coverage 计）
py -3.10 tools/b4_replay_ds.py all
```

- 冒烟（只跑每段 3 个时点，秒级）：`py -3.10 tools/b4_replay_ds.py pilot --limit-points 3`
- 衍生侧延迟敏感性：`py -3.10 tools/b4_replay_ds.py pilot --deriv-lag-hours 1`

## 4. 输入 / 输出路径

| 角色 | 路径 |
|---|---|
| 输入 · 原始归档 | `data/archive_raw/`（只读；spot 1h、futures metrics 等） |
| 输入 · 队列真源 | `reports/archive_download_ds_full/queue_v3.csv` |
| 输入 · 生产评分 | `binance_box_strategy.py`（只 import `indicators` / `box_score` / `deriv_score`） |
| 输出 · 全部产物 | `reports/b4_stage1_ds/`（本目录） |

## 5. 通过 / 失败 / 未查项

### 通过

- 边界测试 **33/33 通过**（取点契约、隔离未来、时间格式、小时分界、历史不足、无合约、缺口、尾部不可用、口径不冒充、分母对账）。
- 队列 **1,000,272 行逐份对账 missing = 0**；抽样 400 份字节全对。
- pilot **576 个时点全部有输出**（12 段 × 48 点），无空结果冒充通过。
- 外连拦截计数 **0**。

### 失败

- 无。**未出现**需要用空结果掩盖的异常。

### 未查（显式列出，不得当作已通过）

1. metrics 整点行与线上 `/futures/data/*` 1h 端点**未逐点比对**（需联网，本批禁止）。
2. 全库**行级**覆盖未查（只查了 pilot 12 段；其余分片仅文件级对账）。
3. 历史市值、当时交易资格、分片发布时刻**不可重建** → 线上全流程未复现。
4. 退市/更名身份映射未做（KLAY→KAIA、FTT 退市等只表现为缺口）。
5. 未来标签（后续收益/持有路径）**未实现**，按任务书留给下一批。

## 6. 建议 glm 独立验收的检查点

1. 复跑第 0 步与第 5 步，比对 `scoring_output.jsonl` 的 SHA256 是否与上表一致。
2. 抽查任意一条记录：`field_adoption.kline_adopted_candle_utc` 是否等于 `decision_time_utc − 1h`。
3. 抽查 `deriv.ls_source_field` 是否恒为 `sum_toptrader_long_short_ratio`，且
   `alternatives.ls_top_account` / `ls_global` 从不进入 `score_deriv`。
4. 复核 `pilot_manifest.json` 的选样理由**不含任何收益/涨幅信息**（只写格式/边界/缺失覆盖）。
5. 复核 `summary.md` 第 2 节的「未证明」是否与产物一致（有无把"部分验证"写成"复现线上"）。
