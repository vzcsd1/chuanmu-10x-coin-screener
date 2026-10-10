# R2 契约草案：回退口径展示化 + 整轮摘要（glm5.3f，2026-10-07）

本文件是 glm 在 ds 交接（`reports/live_futures_ds/r2/ready_for_glm.md`）前 prepared 的契约与测试设计；实现待编辑权交接收手。对应任务 `tasks/live_futures_r2.md` glm 第 1-4 条。

## 1. 回退口径展示化（不改权重/阈值）

现行为（r1 已证，Grok 复核吻合）：`_fetch_deriv_context_live` 回退分支把全市场账户比写入 `ls_top`（`binance_box_strategy.py:779`），`deriv_score` 按值 +3（`:960`）。

**修复语义**（落实现有 C4，不改任何权重）：

| 字段 | 修复后 |
|---|---|
| `ls_top` | **只**来自 `topLongShortPositionRatio`（大户持仓比，被回测验证的口径）；回退时该键整键缺席 |
| `ls_global`（新，仅展示） | 回退取到的全市场账户比原值；**键恒在**，真实大户持仓口径下为 null（与 `ls_source` 同形态，便于前端统一处理） |
| `ls_source` | 回退时 = `globalLongShortAccountRatio(回退,仅展示)`，正常 = `topLongShortPositionRatio` |
| 计分 | 回退值不进 `deriv_score` 的 ls_top 判定；真实大户持仓比照常计分 |
| `deriv_status` | 回退仍 = partial（口径不全） |

## 2. 整轮摘要（向后兼容最小扩展）

**API**：`public_selection(exchange, futures, cfg, okx=None, round_stats=None)`——可选出参 dict，缺省 None 时行为与返回值完全不变；`川沐十倍币筛选.scan(cfg, round_stats=None)` 同理返回行列表不变。UI 的 `real_scan` 传 dict 并把 `{"rows", "round"}` 包成 dict 载荷交 `ScanManager`（接受 dict 或 list，list 为旧约定）。

**字段**（覆盖入选、落选、被跳过、失败的原定扫描对象；不从最终卡片反推）：

| 字段 | 类型 | 含义 |
|---|---|---|
| `task_completed` | bool | 扫描流程走完未被打断（DomainPaused 中止时无摘要，UI 走 failed 保留 last） |
| `scope_total` | int | 过闸门且进入逐币处理的对象数（含被跳过/失败/落选） |
| `skipped_short_history` | int | K 线历史不足 `box_period+3` 被跳过数 |
| `errors` | int | 逐币处理异常数（每币一次计数） |
| `futures_expected` | int\|None | 按已加载合约市场列表"应有合约"的扫描对象数；**合约客户端不可用时 = None（未知），不填 0** |
| `futures_fetched_ok` | int | `deriv_status=="ok"` 的对象数 |
| `deriv_counts` | dict | {ok, partial, failed, paused, no_futures} 各状态计数（真实无合约/历史不足/失败/暂停分别记录） |
| `deriv_domain_state` | str | `ok` / `paused`（本轮遇到限流暂停）/ `failed`（客户端在但行情取不到）/ `no_client`（初始化失败或未返回） |
| `data_complete` | bool | `futures_expected` 非空 且 fetched_ok==expected 且 errors==0。False ⇒ 本轮不完整，不得显示完整推荐 |
| `has_passing` | bool | 存在过门槛候选 |
| `chosen_count` | int | 过门槛数量 |
| `as_of_min` | int\|None | ok 行 `deriv_as_of` 的**最小值**（不是最大；一项新数据不得掩盖另一项陈旧）；无 ok 行为 None |

判断依据：完整性按评分所需字段（WEIGHTS 六项对应的 oi_chg_1d/oi_chg_3d/ls_top）与 deriv_as_of 观测时点表达，**不设统一时效阈值**（不虚构）；合法零值（费率 0、OI 快照 0、oi 增速 0.0）不算失败；历史不足记 `skipped_short_history`，不算数据不完整，也不冒充完整评估。

## 3. 三态表达与 UI 映射

- 执行完成 = `task_completed`（未完成时 UI status=failed，rows=None，last 保留原 finished_at）。
- 数据完整 = `data_complete`；前端 `round_quality` = `complete` / `incomplete`（round 缺失时 `unknown`，演示模式即 unknown）。
- 有无候选 = `has_passing`；**数据完整 + 无候选 = 正常"本轮无候选"**，不是失败。
- UI 展示（app.js/index.html 最小改动）：状态区加一行轮次摘要：
  - complete + 有候选：`数据完整：合约 N/N，异常 0`；
  - complete + 无候选：`数据完整，本轮无候选（过门槛 0）`；
  - incomplete：`本轮不完整：合约 X/应有 Y，暂停 a 失败 b 跳过 c 异常 d——不作为完整推荐`；
  - no_client：`合约数据本轮不可用（初始化失败或暂停）——仅现货参考，不作为完整推荐`。
- 旧结果：`last` 结构含原 finished_at 与其自带 round，展示其原时间，不覆盖成当前成功（现有行为已验证，保持）。

## 4. 测试设计（tests/test_live_futures_acceptance_glm.py 追加 R2 类）

1. 回退展示不加分（修复后 score_deriv=7、ls_top 缺席、ls_global=0.9）；2. 真实大户持仓比照常 +3（回归）；3. 完整轮摘要字段断言；4. no_client ⇒ expected=None ⇒ 不完整；5. 中途暂停 ⇒ expected=3、ok=1、不完整；6. 完整+无候选=正常；7. **可见卡片全齐、落选币缺数据被摘要暴露**（用户核心场景）；8. 全部逐币失败 ⇒ 不完整而非"无候选"；9. as_of_min 取最旧；10. 不传 round_stats 时返回约定不变；11. UI 接受 dict 载荷、list 旧载荷兼容、失败保留 last。

第 1、3-9、11 项在修复前应为红（基线已存 `baseline_prefix_tests.txt`），修复后转绿；第 2、10 项全程绿（防误伤）。
