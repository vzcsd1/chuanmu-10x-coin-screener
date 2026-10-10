# B4 stage1 · glm 验收状态（2026-10-09，准备完成 / 验收未开始）

任务书：`tasks/glm_b4_stage1_20261009.md`。本轮完成第 3 节"现在可以做"的全部准备；
交接入口 `reports/b4_stage1_ds/ready_for_glm.md` **截至本轮结束仍未出现**（单次检查，
未轮询），按任务书第 4 节交付准备成果并列明依赖，不验收半成品。

## 1. 逐项状态（ds 任务书 §4/§5 验收清单 → glm 独立核验项）

| # | 核验项 | 状态 | 理由 / 将用的独立测试 |
|---|---|---|---|
| 1 | 蜡烛采用点：仅闭合输入不多退一根、最后一根未来值不偷用 | **未查（待交接）** | Part A 已钉住生产契约（`test_closed_only_feed_scores_second_to_last`、`test_trailing_row_content_ignored`）；交接后跑 Part B `test_score_alignment_no_extra_step_back` 对接入版复核 |
| 2 | 截止后数据变化不影响历史评分 | **未查（待交接）** | Part B `test_future_data_cannot_change_past_score`（手改截止后 K 线含最终形成值，历史评分必须不变） |
| 3 | 毫秒/微秒/字符串时间、时区与小时边界 | **未查（待交接）** | Part B `test_time_normalization_table`（5 组手算用例：ms/s/μs/ISO-Z/无时区字符串按 UTC） |
| 4 | OI 1d/3d 基准时点、历史不足不假完整、尾部无效值 | **生产侧已钉，接入侧未查** | Part A `test_oi_basis_uses_last_value_at_or_before_cutoff`（缺口→取 ≤24h 最近值 150/100−1=0.5；stale_tail=2）、`test_short_history_is_not_fake_complete`（3d 缺→None+partial）已验证生产实现；ds 接入是否复用同契约待交接核对 |
| 5 | LS 来源身份（账户比不得冒充大户持仓比） | **生产侧已钉，接入侧未查** | Part A `test_account_ratio_never_impersonates_top_position_ratio`（账户比 2.5 只进 ls_global，ls_top 保持 None）；交接后核对 ds 输出的 ls_source 字段 |
| 6 | 无合约/未知市值/退市身份区分；分母逐项对账 | **未查（待交接）** | 需 ds 的 pilot_manifest 与逐对象输出；验收时按"原定对象=评分+跳过+未知"逐项对账，未入选不得丢失、缺资料不得计为没涨 |
| 7 | 复跑 ds 自测命令 + SHA256 一致 | **未查（待交接）** | 以 ready_for_glm.md 声明的命令与环境为准，比对全部文件 SHA；文件仍在变即中止验收 |

三件事分开评价（不合并成"通过"）：**生产评分契约=已独立钉住（见下）；历史数据可得性=待 ds 交付覆盖证据；未来全量研究=本批未批准（属 9.6 节第 2 步）**。

## 2. 本轮已完成的准备（可复算）

- **实验卡订正**：`reports/b4_bidirectional_experiment_card_glm.md`（SHA256 前缀 `d8ba00c9850b8338`）。原处划线更正：gain=价格倍数−1、B0 待做、v3 下载状态（1,000,272/1,000,272 EXIT=0，队列结束≠逐文件核验）、六类路径改四维分开、撤销 20% 整批停。新增第 7 节执行参数 v2（范围核验驱动、一份底账、评分独立事件库、UTC 时钟+5m 参考价、30 天主窗口+2/2.5/3/5/10 档、重复关联两种算法、探索 2021–2022/确认 2023–2024/2025+ 标已使用）。
- **独立测试**：`tests/test_b4_acceptance_glm.py`（SHA256 前缀 `b34d5e2172626988`）。
  运行命令与环境：`PYTHONPATH=. py -3.10 -B -m unittest tests.test_b4_acceptance_glm -v`
  （Windows 11 / Python 3.10.9 / 零网络）。结果：**Ran 12 tests — OK (skipped=3)**；
  9 项 Part A 全过（生产契约手算边界），3 项 Part B 按设计跳过待交接。
  预期值来源：手算（61 根合成 K 线的 n=59/60/61 → 1/8/1 分；OI 缺口 0.5；阈值边界 0.10/0.20/1.0）+ 生产源码行号（box_score iloc[-2]/iloc[:-1]、deriv 阈值、_fetch_deriv_context_live 的 ≤cutoff 取值与 stale_tail），文件头有完整推导。
- 过程证据：首次运行 1 fail 1 error，根因是**我**的假接口行序按最新在前构造，与真实 API 的时间升序不符——已修正并在测试注释注明。失败证据保留于本轮工作日志，不删除。

## 3. 验收计划（交接后执行，不重复准备）

1. 核对 `ready_for_glm.md` 声明的 SHA256 与实际文件、输入输出一致；文件仍在变 → 中止。
2. 层 1：按其声明的 PowerShell 命令原样复跑 ds 自测，比对结果。
3. 层 2：跑 `tests/test_b4_acceptance_glm.py`（Part B 自动启用；若入口函数名不同，只接线不改预期数值）+ 按上表 #6 逐项对账分母。
4. 产出逐项接受/不接受/未查及理由到本文件；给主代理明确建议（可进基线双向报告 / 仅哪些范围可进 / 哪些必须先修）。

## 4. 依赖与边界

- 依赖：`reports/b4_stage1_ds/ready_for_glm.md`（ds 完成并停止编辑后出现）。用户转交"ds 已交付"后从第 3 节继续，本轮准备工作不重复。
- 边界：未修改 ds 任何文件、生产文件、公共文档；未联网；未扩展 UI；不轮询交接目录。

## 5. r2 阶段更新（2026-10-09，接 tasks/b4_stage1_r2_20261009.md）

- 主代理审核裁定 r1 存在 D1–D4 接入缺陷（`reports/b4_stage1_review/probe_results.json`）。
- glm 已完成：测试加载修复（G1）+ 按实际接口接线 + fixture 手算缺陷修正 + 实验卡 r2 订正。
  详见 **`r2/prep_20261009.md`**（本轮准备证据，含版本观测与两层验收预注册）。
- 关键观测：盘上 `tools/b4_replay_ds.py` 已是 r2 工作版（SHA `9235978a...`，≠r1 冻结版 `7f6373f4...`），但 r2 交接 `reports/b4_stage1_ds/r2/ready_for_glm.md` **尚未出现** → 验收仍未开始。
- 对盘上 r2 工作版：25 项独立测试全绿（准备证据，非验收，原因见 prep 第 5 节）。
