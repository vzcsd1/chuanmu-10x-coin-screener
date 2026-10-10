# glm 历史双向研究独立验收 · 准备记录（2026-10-09）

状态：**验收已完成**，正式结论见 `summary.md`。本文件记录准备与执行过程。

## 版本核对
- 交接入口 `reports/history_research_ds/ready_for_glm.md` 存在，4 个冻结 SHA 全部 MATCH
  （history_research_ds.py `bbbaa689` / test_history_research_ds.py `850aab41` /
  b4_replay_ds.py `e4c6e9af` / binance_box_strategy.py `d1cc20d8`）。
- integrity_lag0.json `sum_check=true`；reverse 去向和 = 5932。

## 交付物（glm 独立目录）
| 文件 | 内容 |
|---|---|
| `tests/test_history_acceptance_glm.py` | 8 类手算反例，27 项测试，全绿 |
| `tools/history_acceptance_glm.py` | 聚合复算：汇总自洽 A、attention 全量对账 B、原始价格复算 C、缺口复核 D |
| `reports/history_research_glm/acceptance_checks.json` | 22 项聚合检查，22/22 PASS |
| `reports/history_research_glm/_recompute_details.json` | 27 个主时段 ≥10× 事件逐一复算明细 |
| `reports/history_research_glm/summary.md` | 正式验收结论 |

## 过程中修正的自身错误（诚实记录）
1. 测试 3 处 `assertAlmostEqual` 位置参数语法错 → `msg=`。
2. metrics 追加行时间用了 T0+1h（实际在 decision 之前，落在窗口内）→ 改到 decision 之后。
3. span_hours 手算 91 错：t=90 处 259.67/130=1.997<2，W=[0..89] → 90。
4. 聚合复算月份构造 bug（对 int 切片）→ 展开月份区间。
5. fp 对账口径错：ds 的 `max_m30` 是事件内最大 m30，不是游程起点处 30d 窗值；起点窗内
   够不到 10× 是正常的（爆拉在段内更晚）→ 改为「事件内 m30 max 对账 + fp 仅验证有后续数据」。

## 中间发现（详见 summary 第 3 节）
- F1：deriv_scores 的 <25 OI 观测门槛只作用于 status，points 照加（生产应不给分）。
- F2：random_rank_matrix 不可评列挤占名次槽位（50 seed 中 21 例），系统性压低 random 覆盖。
- G1：reverse_events 无「中途发现后的剩余机会」字段。
- G2：cmd_reverse「出现过晚」分支死代码（first≤w1 恒真）。
