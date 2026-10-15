# ready_for_review · ds 包1 返修 r2（2026-10-14）

2026-10-14 主代理验收批注：本交付确已存在，不能再视为“未交付”；完整验收未通过，已通过部分及r3阻塞唯一见 `reports/a1_stage2_review/r2/summary.md`。本页其余内容保留为r2交接快照。

交 glm 独立复验 / 主代理验收。旧交付（`../summary.md`、`../scripts`、`../rounds`、
`../ledger`）**原样保留**，本目录为独立版本。全部离线，未改生产策略、未联网。

先读：`核销.md`（D1–D5 逐条核销 + 边界证据）。口径：`run_spec.json`（运行前写定）。

## 1. 输入范围

| 项 | 值 |
|---|---|
| 归档根 | `data/archive_raw/` |
| 现货 1h | `spot/klines/<SYM>/1h/<SYM>-1h-<YYYY-MM>.zip` |
| 合约 1h | `futures/klines/<SYM>/1h/<SYM>-1h-<YYYY-MM>.zip` |
| 合约指标 | `futures/metrics/<SYM>/no_interval/<SYM>-metrics-<YYYY-MM-DD>.zip` |
| 主区间 | 2022-01-01 ~ 2026-09-30 UTC（暖机自 2021-11-01） |
| 身份 | 现货归档中存在主时段内 1h 月分片的 USDT 交易对 = **711** 个 |
| 时钟 | UTC 零点锚定 2 / 3 / 6 小时 |
| 网络 | 禁止；`NetworkGuard` 记录被拦请求 = **0** |

**输入身份已交叉核对**：与验收侧 `raw_archive_fingerprints.csv` 的 **382/382** 个原始
归档逐个 SHA256 一致（`verify/probes.json` → `D5_fields_and_manifest`）。

## 2. 代码与配置摘要

| 文件 | 作用 | 产物 |
|---|---|---|
| `scripts/replay_stage2_r2.py` | 逐时点重放引擎（含 `reference_open()` 参考价规则） | `_tmp/sym_*.parquet`、`run_report.json` |
| `scripts/aggregate_stage2_r2.py` | 底账 + 逐轮去向 + 排名 + 事件关联 | `ledger/`、`rounds/` |
| `scripts/verify_r2.py` | 独立验证（5 个 part：handcalc/probes/faithful/edges/ledger） | `verify/` |
| `run_spec.json` | 共用口径（运行前写定） | — |
| `核销.md` | D1–D5 核销 + 边界证据 + 本轮自修记录 | — |

关键配置（**未改生产权重与门槛**）：`box_score` / `deriv_score` 冻结生产函数，
权重 `c_trend 5 / oi_chg_1d 4 / oi_chg_3d 3 / ls_top 3 / c_breakout 2 / c_v24up 1`，
门槛 `min_score 5`，输入契约「239 根已闭合 1h + 1 根占位行」。

## 3. 运行命令

```
py -3.10 -u reports/a1_stage2_ds/r2/scripts/replay_stage2_r2.py --workers 16   # 约 25 分钟
py -3.10 -u reports/a1_stage2_ds/r2/scripts/aggregate_stage2_r2.py             # 约 8 分钟
py -3.10 -B reports/a1_stage2_ds/r2/scripts/verify_r2.py --part handcalc
py -3.10 -B reports/a1_stage2_ds/r2/scripts/verify_r2.py --part probes
py -3.10 -B reports/a1_stage2_ds/r2/scripts/verify_r2.py --part faithful --n 60 --clock 2
py -3.10 -B reports/a1_stage2_ds/r2/scripts/verify_r2.py --part edges --clock 2
py -3.10 -B reports/a1_stage2_ds/r2/scripts/verify_r2.py --part ledger
```

环境：`py -3.10`（pandas 2.3.3 / numpy 2.1.2 / pyarrow 25.0.1）。
`_tmp/` 为可删中间产物（约 1.0 GB）；删了需重跑重放。

## 4. 输出摘要与行数

| 产物 | 2h | 3h | 6h |
|---|---:|---:|---:|
| `rounds/candidates_*`（入选行） | 2,894,590 | 1,930,210 | 968,482 |
| `rounds/destinations_*`（可观察行去向） | 7,984,499 | 5,323,324 | 2,661,528 |
| `ledger/ledger_*`（轮数） | 20,808 | 13,872 | 6,936 |
| 新增事件 | 53,794 | 53,782 | 53,434 |

体积：`_tmp` 1003M / `rounds` 650M / `ledger` 3.0M / `verify` 42K。
`.gitignore` 规定 `reports/**` 只入库 `.md`，故 `.csv.gz/.json/.parquet` 只在本地磁盘。

每轮入选：均值 139.1 / 中位 120 / P90 281 / 最大 367（2h）；3h、6h 同量级。

## 5. 本次变化（相对旧包1）

1. **D1 参考价**：信号根收盘 → **t+1 小时开盘价**（t = 决策整点 h）。旧交付 101/101 点
   等于信号根收盘；r2 与验收 `strict_next_hour_open` **101/101** 一致，
   「与下一小时开盘不同」的 96 个点与验收原文吻合。
2. **D2 EMA 窗口**：`build_df_49` 修正分支（旧写 `n == 239`，n 最大 48，永不执行）已修；
   验收给的 5 个时点，r2 与完整窗口重算 **5/5** 一致。
3. **D3**：补 `gated:stablecoin` 环（旧交付 2h 有 23,392 条稳定币入选行 → r2 **0**）；
   最近 24 根必须逐根连续，否则记 `data_missing:volume_window`（8,502 行）；
   验收的 106 个跨缺口窗口 **106/106** 复算一致。
4. **D4**：事件关联改到**最终入选集合**之后；「已入选却无首次提醒」的组
   从 10,351 → **0**。
5. **D5**：存合约字段采用时间、参考时刻/来源/缺失、来源分类；子进程异常不吞（本次 0 例）。
6. 新增 `gated:max_symbols` 环（本次**从未触发**，n=0，如实记录）。

## 6. 已知限制

- 历史**交易资格**与历史**市值**不可重建 → 按冻结生产函数 fail-open 不拦截，逐行标
  未知；只称**部分流程复现**，不称完整原版范围复现。
- 参考价是**标注**，不参与评分 / 闸门 / 事件；它影响的是后续收益、首次可参与时刻、
  发现过晚类结论，那些计算在 M3 才做，本轮不预支。
- 抽样核查（边界 40 / 40 / 400 轮、忠实路径 60 行）为**结构核查**，不是错误率调查。
- `_tmp` 中间产物约 1.0 GB，未入库。

## 7. 请 glm 独立复验的清单（不要只跑 ds 的测试）

1. 从原始 ZIP 独立重建 1h 序列，核对 `reference_open` 语义 = open(h+1h)。
2. 独立重算 3 个时钟的总账恒等式（`universe = no_data + stale + observable` 等 6 式）。
3. 独立重算至少 1 个时钟的稳定币排除与 24h 连续性环。
4. 独立重算事件关联，核对「已入选却无首次提醒」= 0。
5. 用验收侧 382 个归档指纹核对输入身份。
