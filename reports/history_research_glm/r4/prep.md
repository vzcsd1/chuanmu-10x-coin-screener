# glm r4 独立复验 · 准备状态（2026-10-10）

> **2026-10-10 主代理接续批注**：下文“未冻结”是准备时点快照，当前 r4 交接已存在。复跑发现的三项持仓失败来自倒序输入，合波样例实际仅有一合格段，两项 forward 报错是新字段未接好；不能将这些当作 ds 缺陷。与此同时，独立检查发现真实研究阻塞。证据与最新接续见 `reports/history_research_review/r4/summary.md` 和 `tasks/history_r4_closeout_20261010.md`；先修自己的准备，待 closeout 冻结后正式复验，不改 ds 文件。

> **2026-10-10 closeout 轮更新（glm）**：按 closeout 任务书第 3 节完成六红灯修复与阻塞预期建立，测试重写为 32 项（见下节）。**DIAG 结果：32/32 全绿，0 失败 0 错误 0 跳过。**

> **2026-10-10 §5.2 睡前接续更新（glm）**：聚合工具补成可用版（closeout 入口+冻结校验+行级独立复算+锚点核查）；C1/C3 接实际生成路径的独立反例补齐；27/22 锚点原始行情核查完成。**closeout/ready_for_glm.md 仍未出现 → 未冻结，正式复验未开始。** 恢复命令见文末。

## §5.2 交付（本轮新增）

### 1) 聚合工具 `tools/history_acceptance_glm_r4.py`（可用版）
- 入口改为 `closeout/ready_for_glm.md`；冻结判定 = 存在 + **停止编辑声明** + 交接单声明 SHA 与当前 `tools/history_research_ds_r4.py` 匹配 + 有配置/输入身份清单。文件存在≠冻结（旧 `r4/ready_for_glm.md` 已显式标废弃）。
- `aggregate` 子命令：从**行级明细**独立复算（forward_detail CSV / events jsonl / reverse jsonl），对照 ds 汇总。当前（pre-freeze 快照）结果：**7 类对账全 PASS**——verdict 计数、跨期分组（独立 REVIEW_START=2025-01-01 常量）、最终失败比例、reach_2x 先跌后涨份额、事件行级 by_tier/mature/complete、reverse 去向计数、attention 曲线内部一致性（如实标注为 ds-对-ds 内部一致性，非独立复算：小时级排名底账不可得，避免第二份全历史）。
- `anchors` 子命令与 `status`（SHA 清单）/`resume`（恢复命令）见下。

### 2) C1/C3 实路径独立反例（tests，新增 6 项，全绿）
- **C1（`TestC1FeaturesRealEntry`，5 项）**：调用**实际特征生成路径** `_features_worker_r4`（真实归档样本 0GUSDT，输出经 FEATURES_DIR 重定向只写 `replay/features/`，ds 模块属性测试后还原）。naive 参照只用「已收盘」根逐根复算 ret30d/ret7d/dist60h **全量数组**对账一致 + 缺口 NaN 语义一致——若生成器把未完成根泄进决策格必然失配（等价主代理「改第 1500 根」探针的注入式证明）。另证：决策小时=开盘小时+1；lag0/lag1 逐数组一致（lag 在下游时钟应用，未为单特征另造时钟）。**实路径证据：通过。**
  - 未验：缺口 NaN 情形（抽样 200 对象归档全部连续无缺口 → 该断言 skip，如实记录）。
- **C3（`TestC3LabelVsDirectForward`，1 项）**：同一 fixture 下事件起点标签（`_build_labels`）与直接未来结果（`forward_from_series_r4`）在窗口边界小时**必须分歧**：标签窗口首格 true 而其自身 30 天窗触及<2×（标签≠机会），窗口含事件起点的小时真实可达 2.6×（真实机会不得被抹掉）。**实路径证据：通过。**

### 3) 27/22 锚点核查（`anchors` → `replay/anchors.json`）
- **27 个 v3 ≥10× 锚点**（源：`reports/history_research_glm/_recompute_details.json`）：**27/27 全部映射**到 r4/closeout 事件（symbol+起点被事件段包含，起点差 0.0h 的 27 例中含 tier=10.0 对应）。每例已直接读本地原始归档**独立复算**：起点前收盘/起点根开盘两种参考价的 30 天最大触及 + 窗内实有根数/缺根数（27 例中 1 例缺 437 根，其余完整）。
  - 口径说明：v3 的 `fp_m30_at_start` 混入事件内窗口语义（实测例：BIFI v3 值 63.78 而原始 30 天窗触及仅 1.41×），**不可由原始数据直接复现** → 原始复算值独立保存，v3 值仅作对照不判定。
- **22 条「之后观察」**：旧 r4 与 closeout 均为 22 条（去向计数 3335/2525/53/22 完全一致）；**closeout 已补齐剩余机会字段 22/22、其中 before_recorded_peak=17**——与主代理报告的 C5 缺口（22 条均无字段值、17 条峰前）吻合：**C5 的字段缺失在 closeout 产物中已关闭**。

### 4) 冻结状态与恢复命令
- `closeout/ready_for_glm.md`：**不存在**（closeout 目录已有大量产物：events/forward/outcome/attention/reverse 等 + run_closeout.log EXIT=0，但按任务书不能据此宣布交付完成）。
- 工作副本测试：38 项 DIAG 全绿（1 项缺口断言 skip）；未冻结且无 DIAG 时产品类整体 skip、自检始终运行。
- **恢复命令**（冻结交接出现后依序执行，无需用户转告）：
  ```
  python tools/history_acceptance_glm_r4.py status        # 冻结校验必须 frozen=true
  python -m unittest tests.test_history_acceptance_glm_r4 -v   # 去 DIAG 全绿（skip/expectedFailure 不计通过）
  python tools/history_acceptance_glm_r4.py aggregate     # 行级独立复算 → replay/aggregate.json
  python tools/history_acceptance_glm_r4.py anchors       # 锚点+原始复算 → replay/anchors.json
  # 然后写 reports/history_research_glm/r4/closeout/summary.md 后停止
  ```
- **缺口清单**：① attention 数量曲线无独立复算（需小时级排名底账；独立性由 naive_rank 参照实现承担）；② 归档无缺口样本 → C1 缺口 NaN 情形未实测；③ v3 fp 口径不可复现（已声明，不判定）；④ 正式复验全部待冻结后执行（去 DIAG 全跑、明细聚合对账定稿、replay 内补充生成器重跑对比）。

---

## 以下为早前 closeout 轮记录（2026-10-10 日内，适用版本=当时工作副本）

## closeout 轮六红灯处置（glm 侧，全部完成）

| 红灯 | 处置 | 结果 |
|---|---|---|
| 持仓样例 dt 倒序 | fixture 改升序 `T0-(79-k)*HOUR`、涨价放最新端（我方错误自证） | 5 项 OI/LS 测试全绿 |
| 合波样例非两段 | 改用主代理 C6 fixture（180 段 400/180=2.22 合格），先 naive 确认两段 (0,719)/(760,1479)、间隔 40h | id3/id4 全绿 |
| forward 字段未接 | 适配 `max_close_drawdown/max_dd_from_entry/elapsed/data_state/final_eligible/verdict/ref_lag_min/ref_delayed/ref_skipped_bars/missing_bars` | 9 项 forward 测试全绿 |
| C2 跨期排除 | 接 `summarize_forward_r4` 真实键（`crossover_excluded.explore_30d_clean/cross_30d`、`first_score`、`score_total_max`） | **已通过**（见下） |
| C4 日内重入 | 接 `_workload` 实际入口 | **已通过**（见下） |
| C6 n_segments | 接 `detect_events_r4` 实际入口 | **已通过**（见下） |

## ⚠️ 与主代理 r4 报告的独立差异（须在 closeout 冻结时复核）

当前 ds 工作副本（2026-10-10 实测，未改 ds 任何文件）上，主代理报告的三项"真实阻塞"
**在本机 fixture 上均已不复现**：

1. **C2 跨期只数不排**：`summarize_forward_r4(:1084-1100)` 现按真实结果窗分组，
   跨界行只进 `cross_30d/cross_90d` 专表，`explore_30d_clean=0` —— 已排除。
2. **C4 日内重入漏记**：`_workload(:1651)` 已是「C4 修正版」小时级口径，
   日内 0h 进/1h 出/2h 重入 → `re_added_total=1` —— 不漏。
3. **C6 n_segments 固定 1**：`detect_events_r4` 对两段间隔 40h fixture 返回
   1 事件 `n_segments=2`；间隔 >48h 分立 2 事件 —— 正确。

可能解释：ds 在主代理复核后已修复（closeout 正在进行）、或主代理探针调用方式/快照版本不同。
**这三项测试已从 expectedFailure 转为正式断言并通过；若冻结版复核仍现红灯，以冻结版为准重新归因。**
C1（特征当根因果）/C3（标签≠机会语义）维持独立预期：C1 naive 自检绿、待 closeout 特征产物转正式断言；C3 标签语义绿（`_build_labels` 以 `MAIN_START_MS`=2022-01-01 为格子 0 点，我方 fixture 基准已对齐）。

## closeout 轮测试文件（tests/test_history_acceptance_glm_r4.py，32 项）

- 期望自检 8 项（IDxx 手算，始终运行）+ 产品断言 24 项（r4 冻结前无 DIAG 时整类 skip）。
- 首轮 DIAG 曾暴露 6 处我方问题，全部修至绿：
  ① `cls.fn` 未包 staticmethod → `self.fn(...)` 把 self 当首参（R2/R3 全体 ERROR）；
  ② id4 fixture 中段 400/100=4 使 W 连续出两段 → 改单脉冲 `[100]*720+[260]*24+[100]*700`；
  ③ C3 事件基准误用 T0 → 改 `MAIN_START_MS + s*HOUR`；
  ④ C2 row 缺 `first_score`/`score_total_max` 键 → 补齐；
  ⑤ uncertain 语义：达标根 low 须低于此前路径才触发（low=ref 不触发，我方断言错）→ low=80；
  ⑥ close 参考/迟到参考 fixture 参考点后无数据 → 参考点之后补根、缺口改中段。
- 运行方式：`GLM_R4_DIAG=1 python -m unittest tests.test_history_acceptance_glm_r4`（诊断预接）
  / 无开关且未冻结 → 产品类 skip、自检 8 项仍绿。

## 旧「待对账差异」订正（上轮 prep 结论被主代理裁定/本轮推翻的部分）

1. ~~"25 点 OI=0 分待对账"~~ → **我方样例倒序错误**（裁定采纳）。升序 fixture 下：
   24 点=0 分、25 点跨 72h=7 分、25 连续行跨 24h=4 分（3d 基准不在窗内=NaN 合法）、
   LS 缺失不扣合法 OI 分 —— 全部实测通过，无差异。
2. ~~"forward 字段名未定"~~ → 已按 r4 真实字段全量断言，9 项全绿。
3. ~~"48h 合并 fixture r4 得 1 事件疑 R3 未修净"~~ → 该样例只有一合格段（我方样例错）；
   真两段 fixture 下合并/n_segments 行为正确（见上 C6）。
4. max_m90=4.0 在 90 天窗内属正确行为 —— 维持原订正。

## 冻结后验收步骤（第 4 节，不变）

1. 核对 `ready_for_glm.md` 冻结 SHA（源码/固定卡/配置/对应输出）；
2. 冻结版直接跑 `python -m unittest tests.test_history_acceptance_glm_r4 -v`（skip 清零、全绿才算产品通过）；
3. 按 r4 交接产物补全聚合对账，只写 `reports/history_research_glm/r4/replay/`；
4. 旧 27 个十倍事件逐项对照 r4 新去向；每天不同币数、重入、关注占用另行复算。

**状态：准备完成，r4 未冻结（`reports/history_research_ds/r4/ready_for_glm.md` 不存在），
正式验收未开始、0 项产品通过。** 按任务书第 4 节：交准备状态即停，不高频轮询。

## 已交付（本批文件边界内）

| 文件 | 内容 | 当前状态 |
|---|---|---|
| `tests/test_history_acceptance_glm_r4.py` | 正确期望测试 24 项：期望自检 9 项（朴素参照实现+纯手算，始终运行）+ 产品断言 15 项（r4 冻结前显式 skip，不计通过） | 期望自检 9/9 绿 |
| `tools/history_acceptance_glm_r4.py` | 聚合复算骨架（版本核对/对账入口） | 待 r4 产物定型后补全对账项 |
| 本文件 | 准备状态 + DIAG 差异清单 | — |

旧 27 项中 F1/F2 的错误断言（10 分锚、名次空洞锚）**不在本文件沿用**；旧文件未改动。

## 验收方法修正（任务书第 2 节落实）

1. 期望由 `tests/..._r4.py` 顶部**独立朴素参照实现**建立（`naive_fwd`/`naive_m30_max_timed`/
   `naive_rank`，逐根 Python 循环 + 9 项手算自检），不再把同一 ds 函数调两遍当独立标准。
2. C2 修正：月分片按真实命名 `YYYY-MM` 读取（`TestC2ArchiveNaming`），0G/1MBABYDOGE/A2Z
   三例实测标准命名文件**存在**（旧"归档确无分片"证据作废）；验收时继续区分
   文件存在/有效行/上市预热/缺口。
3. 断言范围如实标注：`max_touch/max_close/max_dd/mature` 逐字段断言，不做"全部对上"表述。

## DIAG 预跑差异清单（GLM_R4_DIAG=1 对 r4 工作版，**非验收结论**）

r4 工作版（`tools/history_research_ds_r4.py`，仍在写）已确认生效的修复：
- 3 点 OI 例 = 仅 LS 3 分（F1 修复生效）；
- 开盘参考同根 high 250 可记录；达标根内先后不明给界限；迟到参考暴露 `ref_lag_min`；
- `rank_matrix_r4`/`random_rank_matrix_r4`：`[[5,-1,4,5]]` → `[1,32767,3,2]`，
  有效名次连续、无效列不挤占（F2 修复生效，随机与正式排名一致）；
- `detect_events_r4`：`peak_ms` 指向真实高点（260 根），缺口处断开游程不跨合，
  `complete_30d=False`、缺口显式留存。

**待对账差异（冻结后正式验收重点）**：
1. **25 个有效 OI 观测 = 0 分**：`evaluate_r4` 承认 `oi_cnt=25` 但 `oi_chg_1d=NaN`
   （连续 25 行 100→150，24h 基准在窗内）。生产契约应得 1d 4 分；稀疏 25 点跨 72h
   应得 7 分。若 r4 对基准可得性另有定义，需在交接单写明，否则按不通过处理。
2. `forward_from_series_r4` 输出字段名与 v3 不同（无 `max_dd`/`mature` 顶层键）——
   接口未定，测试以显式 skip 等待交接单字段表。
3. 48h 合并 fixture（下标差 41h、真实时间差 91h）r4 得 1 事件——若合并仍按下标，
   即 R3 未修净；待冻结后复核。
4. `max_m90` 在缺口 fixture 中=4.0：手算确认 400 根距起点 2159h<2160h，**在 90 天窗内**，
   属正确行为（我方断言已修正），不是缺陷。

## 冻结后验收步骤（第 4 节）

1. 核对 `ready_for_glm.md` 冻结 SHA（源码/固定卡/配置/对应输出）；
2. 去掉 DIAG 开关跑 `py -3.10 -B -m unittest tests.test_history_acceptance_glm_r4 -v`
   （skip 清零、全绿才算产品通过）；
3. 按 r4 交接产物补全聚合对账（范围/状态/参考/缺口/成熟/去向/分数分组/阶段/排名/
   原版保留/新增漏选），只写 `reports/history_research_glm/r4/replay/`；
4. 旧 27 个十倍事件逐项对照 r4 新去向；每天不同币数、重入、关注占用另行复算。
