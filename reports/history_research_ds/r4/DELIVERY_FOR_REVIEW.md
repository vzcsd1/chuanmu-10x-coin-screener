# r4 最终交付说明（供外部验收）

> **2026-10-10 主代理验收批注**：本说明所列产物已收到，不能作为“零阻断”或最终策略验收通过证明。五个待判断问题已由主代理在 `reports/history_research_review/r4/summary.md` §5 逐项裁定，无需用户重批；新增独立反例与有限收口规格见该报告及 `tasks/history_r4_closeout_20261010.md`。原正文保留为本次交付记录。

> 本文件是**自包含**的：阅读者不需要本项目此前任何对话或文档。
> 请按第 6 节逐条核对；第 8 节是需要你判断的具体问题。

---

## 1. 背景（阅读者需要知道的最小上下文）

- **项目**：币安现货「箱体突破 + 控盘结构」选币器，离线研究线。
  在币安公开归档（现货 1h/5m K 线、合约 metrics）上做「历史双向研究」：
  ① 正向——历史被选中的币后来怎样；② 反向——后来爆拉的币当初有没有被选中。
- **上一版（v3）**：产物在 `reports/history_research_ds/`，由主代理验收后**判定不通过**，
  问题清单与证据见 `reports/history_research_review/summary.md` 与 `evidence_20261010.json`。
- **本轮（r4）**：按 `tasks/ds_history_repair_20261010.md` 的 R1–R6 做**有限返修**——
  只做**计算订正**，不新增策略、不改生产代码、不联网、不新增外部数据。
- **交付完成**：已全量重算并冻结，交接单 `r4/ready_for_glm.md`，本批已停止编辑。

---

## 2. 交付物清单

| 角色 | 路径 | SHA-256（前 16） | 字节 |
|---|---|---|---|
| r4 实现（唯一新增代码） | `tools/history_research_ds_r4.py` | `1f970b73e912b70e` | 108,987 |
| r4 测试 | `tests/test_history_research_ds_r4.py` | `4ddae946740f2939` | 15,304 |
| **交接单（主入口）** | `reports/history_research_ds/r4/ready_for_glm.md` | — | — |
| 新旧差异明细 | `reports/history_research_ds/r4/diff_vs_v3.md` / `.json` | — | — |
| 自检复核报告 | `reports/history_research_ds/r4/independent_verification.md` | — | — |
| 全量运行日志 | `reports/history_research_ds/r4/run_all.log` | — | — |
| 全部数值产物 | `reports/history_research_ds/r4/*.json` / `*.csv` / `shards/` / `signals/` / `events/` / `forward/` | — | 3.3 GB 合计 |

**未改动的文件**（本批的硬约束）：

| 文件 | SHA-256（前 16） | 最后修改 |
|---|---|---|
| `tools/history_research_ds.py`（v3 冻结版，被复用） | `95e9056bba795673` | 2026-10-10 10:48 |
| `tests/test_history_research_ds.py`（v3 测试） | `e8dfabc10dd513ee` | 2026-10-10 10:48 |
| `binance_box_strategy.py`（生产评分） | `d1cc20d875a991b8` | 2026-10-08 18:26 |
| `tools/b4_replay_ds.py`（已验收接入） | `e4c6e9afdf36f944` | 2026-10-09 12:25 |

> 上述四个文件的最后修改时间均**早于** r4 全量运行启动时刻（2026-10-10 11:44 本地时间），
> 可据此确认 r4 没有写回 v3 目录、生产代码或公共文档。

---

## 3. 环境与运行方式

- Windows；Python **必须用 `py -3.10`**（只有 3.10 装了 numpy/pandas）。
- **全程离线**：r4 运行期安装 socket 守卫，`网络外连拦截计数 = 0`。
- 主时段 `2022-01-01 → 2026-08-xx`，小时格 **41,616**，对象 **754**，格子总数 **31,378,464**。

```bash
# 全量重算（约 20 分钟）
py -3.10 -u tools/history_research_ds_r4.py all --workers 12

# 只复跑补充结果生成器（约 2 分钟）
py -3.10 -u tools/history_research_ds_r4.py supplementary --lag-hours 0

# 测试
py -3.10 -B -m unittest discover -s tests -p "test_history_research_ds_r4.py" -v
py -3.10 -B -m unittest discover -s tests -p "test_history_research_ds.py" -v
```

运行前 `verify_reuse()` 会校验被复用的 v3 模块哈希；不符即拒绝启动（防止悄悄换口径后仍复用旧分片）。

---

## 4. 本轮修了什么（R1–R6）

| 编号 | 旧缺陷 | r4 的做法 |
|---|---|---|
| **R1/F1** | 合约侧评分在**有效 OI 观测只有 3 条**时仍算出涨幅并给 10 分；而参考实现只给 3 分 | 观测 < 25 时把 `oi_chg_1d/3d` 记为不可算、`oi_as_of = -1`。**注意**：不是「把所有 partial 的持仓分一律清零」——大户持仓比（LS）仍**独立**按其自身可用条件计分；状态 `ok/partial/failed` 照旧 |
| **R1/F2** | 横截面排名**先排序、后屏蔽**不可评分列，导致有效列名次被挤占（如 `[2,32767,3]`） | 排序**前**把不可评分列置 `-inf`，可评分列名次连续 1..k，并列仍按列序（symbol 升序） |
| **R3** | 7/30/90 天窗口与 48 小时合波把**数组下标当时间**；`peak_ms` 错误地指向参考根 | 窗口改为 `open_time ∈ [close_t, close_t+(W−1)·1h]`（真实时刻）；合波按真实时间差；`peak_ms` 指向真实未来高点；`mature/complete` 按实际参与窗口判定 |
| **R2** | 参考根**同根**盘中价被当成未来；「最大回撤」出现正值；未到期未达标的片段被塞进失败比例 | 参考价 = 信号可用后第一根完整 K 线**开盘价**；参考根同根高低价计入未来；窗口按真实时刻截止；「时间是否过去 / 资料是否完整 / 是否达幅 / 判定」四维分开；未到期未达标记 `undetermined`；最大下跌不给正值；达标前下跌含入场点；首次达标根内部先后不明时给上下界并标 `uncertain` |
| **R4** | 用片段**未来最高分**分组，等于事后诸葛 | 新增 `first_score`（首个信号**当时**的分），`by_first_score` 用它分组；`by_max_score_expost_only` 单列、只作事后描述 |
| **R5** | 关注曲线截到 60；把「每轮 N」写成「每天 N」 | 曲线算到全可评分范围（754）；同一排序同时报每轮人数、每日不同币数、首次加入、退出后重入、持续关注占用 |
| **R6** | 补充结果脚本丢失，只留产物 | 新增 `cmd_supplementary_r4` 作为**唯一可复跑生成入口**（含特征缓存 `cmd_features_r4`） |

---

## 5. 关键结果（r4，lag0）

**事件库**：5,935 个事件（v3 5,932）；档位 2.0:3810 / 2.5:871 / 3.0:882 / 5.0:261 / 10.0:111。
主时段内 tier10（十倍）事件 **27** 个。

**逐时点重放**：754 对象 / 15,995,445 小时格 / 选中 7,534,798 格 / 片段 **723,702** /
**OI 观测不足被门控 20,725 格**（这一列是 F1 修正的直接可见量）。

**信号后果**（723,702 片段，对账 `true`）：
已达 2× 34,825 / 仅达 1.5× 77,201 / 未达 1.5× 580,352 /
**undetermined（未到期或资料不全且未达标）31,319** / no_reference 5；
最终可判（已到期 + 资料完整）**690,937**，失败比例 83.99%。
达标前先跌 ≥20%：确定口径 49.96%、保守口径 50.07%（分母 34,825）。

**反向漏选**（5,935 全对账）：起点前曾观察 3,335 / 仅中途首次观察 53 /
**之后观察 22**（旧版是永不可达的死分支）/ 不可观察 2,525。

**关注数量与人工负担**（N=20，lag0）：
总分排序每日不同币数中位 **70**、随机排序 **275**、简单趋势 **64**；
总分排序持续关注占用中位 2 小时、P90 11 小时。
保留刻度（已捕捉 3,335）：`80%→17、90%→23、95%→30、100%→73`（全范围下已可达）。

**补充结果**（横截面 lift，tier2，@20）：`dist60h_asc` **1.64**、
`combo3`（等权分位平均）训练段 1.50 / 后段 2.00（**两段同向**）、
`score_deriv` 1.05、`score_total` 1.04、`trend` 1.03、`score_kline` 1.01。
**但** `combo3` 前 20 的召回只有 34.3%（总分排序 84.7%），每日去重名单中位仅 27 个币
→ **不能当召回网用**。

---

## 6. 验收检查点（逐条给期望值，可直接复算）

| # | 检查 | 期望值 |
|---|---|---|
| 1 | `tests/test_history_research_ds_r4.py` | **30 / 30 通过**，0 跳过 |
| 2 | `tests/test_history_research_ds.py`（v3，未改） | **36 / 36 通过** |
| 3 | `forward_summary_lag0.json` → `verdict_reconciles` | `true`；verdict 五项之和 = **723,702** |
| 4 | `forward_summary_lag0.json` → `final_eligible` | **690,937**（= `data_state.complete`） |
| 5 | `forward_summary_lag0.json` → `verdict.undetermined` | **31,319**（旧版被混进失败比例） |
| 6 | `integrity_lag0.json` → `sum_check` | `true`；status 之和 = **31,378,464** |
| 7 | `reverse_summary_lag0.json` → `disposition_reconciles` | `true`；四项之和 = **5,935** |
| 8 | `reverse_summary_lag0.json` → `disposition["之后观察"]` | **22**（旧版恒为 0） |
| 9 | `attention_lag0.json` → `max_rank` | **754**（不是 60） |
| 10 | `attention_lag0.json` → `workload.score.20.daily_distinct.median` | **70** |
| 11 | `attention_lag0.json` → `workload.random_20261009.20.daily_distinct.median` | **275** |
| 12 | `retention_lag0.json` → `retention_scale["100%"]["required_n"]` | **73**（lag1 为 **77**） |
| 13 | `baseline_summary_lag0.json` → `n_episodes` 与 `forward_summary_lag0.json` → `n_episodes` | 逐位相等 = **723,702** |
| 14 | `supplementary_*`（5 个文件）复跑 | 去掉 `generated_at_utc`/`elapsed_sec` 后**内容完全相同** |
| 15 | 被复用文件哈希 | `tools/history_research_ds.py` = `95e9056bba795673`…（未变） |

**反例复现**（12 项，均已在测试中固化）：F1（3 观测 → **3 分**）、F2（有效名次恢复连续 `[1,2]`）、
P1 **2.5**、P2 **−0.25**、P3 **−0.2**、P4 上下界 **−0.4444** + `uncertain`、
P5 **partial**、P6 `ref_lag_min` **6000**、P7 **undetermined**、P8 1,540h 高点不算进 30 天窗、
P9 `peak_ms` 指向真实高点根、P10 **1.0**。

---

## 7. 已知边界与未完成项（**不要**当作已完成）

1. **历史市值闸门不可重建**：归档无市值序列 → 当时的成交额/市值闸门无法复原，只能记 unknown。
2. **当时交易资格不可知**：归档文件存在 ≠ 当时该现货对在交易。
3. **分片公开可得时刻不可知**：归档只记录下载时间。
4. **更名/换币（KLAY→KAIA 等）未做身份映射**，只表现为序列缺口。
5. **参考价粒度分组退化**：754 个对象的 5m 与 1h 覆盖一致，回退组为空。
6. **未测任何买卖点、持有期与兑现规则**。
7. **`combo3` 只实现了等权分位平均**，权重与「boxwidth 用降序」仍属待验证假设。
8. **两张候选卡的组合效果未测**：不得把两张独立统计拼接成「组合有效」。
9. **本批的复核是自检版**，不等于第三方独立实现验证。
10. **体积**：r4 目录 3.3 GB，C 盘剩 37 GB（93% 已用）。

---

## 8. 请你（验收方）判断的问题

1. **F1 的修法是否被接受**：r4 选择「观测 < 25 时只门控 OI 两项、LS 独立计分」，
   而**没有**采纳「所有 partial 的持仓分一律清零」的备选修法。这个取舍对吗？
2. **`retention_scale` 的 `100%` 刻度**：lag0 得 73、lag1 得 77，差异来自两个 lag 的入选集合
   本身不同（不是 bug）。保留刻度应以哪个 lag 为准，还是并列报告？
3. **补充结果的逐字节可复跑**：产物含 `generated_at_utc` / `elapsed_sec`，
   因此逐字节比对必然不同，但去掉这两个字段后内容完全一致。这个验收口径可以接受吗？
4. **横截面 lift ≈ 1.0–1.05 是否足够**支撑「总分排序在名单内部有甄别力」的结论？
   如果不接受（例如认为该看 AUC 而非横截面 lift），请指出应改用什么口径。
5. **候选卡 A（总分前 N 当召回网）与候选卡 B（用 `dist60h`/`combo3` 在名单内再排序）**
   是否成立？B 的 lift 更高（1.64）但召回只有 34.3%，且两卡组合效果**未测**。
   是否允许继续验证「先看 A∩B」？
