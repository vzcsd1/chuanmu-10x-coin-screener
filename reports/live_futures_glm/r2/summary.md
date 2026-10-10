# R2 返修交付（glm5.3f）· 2026-10-07

对应 `tasks/live_futures_r2.md` glm 部分。交接依据：`reports/live_futures_ds/r2/ready_for_glm.md`，核心文件 SHA256 `34357e353558574b…` 核对一致后接手；本报告落盘时点的新摘要见文末。

## 正文结论

两项核心修复均已实现并全绿：**① 回退账户多空比不再冒充大户持仓比计分**——回退值改入展示字段 `ls_global`（键恒在、值为 null 或原值），`ls_top` 只来自 `topLongShortPositionRatio`；同时拆除主端点响应内联的"账户比算成持仓比"隐藏回退（`:934-939` 旧逻辑），这是 r1 未发现、本轮取证时新定位的同类问题。**② 整轮缺数据不再被落选币隐藏**——`public_selection`/`scan` 新增可选 `round_stats` 出参（不传时行为与返回值完全不变），UI 载荷升级 `{"rows","round"}`，快照新增 `round`/`round_quality` 三态；"可见卡片全齐、落选币缺数据"场景有专项测试钉住。修复前基线（11 红）与修复后（25/25 绿）、全库 221/221、UI 25/25 均已存档。**ds 补丁独立验收：接受**——Grok 指出的四个阻塞项在交接版逐一核销，三个对抗性探针（跨实例预约可见、滑动窗口无边界双突发、晚到成功不清未到期暂停）全部通过。受控真实单轮验收方案已交，本轮零真实行情请求、未碰归档采集。

## 1. 修复前后行为对比（不是通过数，是行为）

| 场景 | 修复前（基线摘要 4fbeff67/34357e35） | 修复后（本轮） |
|---|---|---|
| 大户接口失败、回退 0.9 | `ls_top=0.9` **+3 分**（score 16），仅 deriv_status=partial 标注 | `ls_top` 缺席、`ls_global=0.9` 仅展示、**+0 分**（score 13），partial 保留 |
| 主端点 longShortRatio 缺失但带账户字段 | 内联换算成 `ls_top` 计分（同口径冒充，r1 未发现） | 不再换算进 series；无持仓比则 ok_ls 不置真 |
| 合约初始化失败 | `futures_expected` 无此概念，全部币标 no_futures 冒充"真无合约" | `futures_expected=None`（未知）、`deriv_domain_state="no_client"`、`data_complete=false` |
| 中途限流（C 币被暂停后落选） | UI 只见 ok 卡片，C 的缺失不可见 | 摘要 `futures_fetched_ok=2/expected=3`、`deriv_counts.paused=1`、`round_quality="incomplete"` |
| 逐币全部失败 | UI status=empty，与"真空结果"三元组全同 | 行数仍 0，但 `errors≥1`、`data_complete=false`、前端明示"不能据此认为无候选" |
| 完整数据 + 无候选 | empty（与失败混同） | `data_complete=true, has_passing=false` → 正常"本轮无候选" |
| 两源陈旧不一 | `as_of` 取 max，OI 陈旧可被 LS 掩盖 | 摘要新增 `as_of_min`（ok 行最旧观测时间） |

不变项（回归钉证明）：真实大户持仓比照常 +3；不传 `round_stats` 时 `public_selection`/`scan` 返回约定不变；评分/权重/门槛/币池/排序零改动；演示模式与旧 list 载荷兼容。

## 2. 改动范围与测试

| 文件 | 改动 |
|---|---|
| `binance_box_strategy.py` | 回退展示化（2 处）+ `round_stats` 摘要（签名、计数、行字段 `ls_global`、结尾填充） |
| `川沐十倍币筛选.py` | `scan(cfg, round_stats=None)` 透传，返回约定不变 |
| `ui/app.py` | `real_scan` 包 `{"rows","round"}`；ScanManager 接受 dict/list 双载荷；snapshot 增 `round`/`round_quality`；失败轮 round=None、last 保留原时间 |
| `ui/static/app.js` + `index.html` + `style.css` | 状态区新增一行轮次摘要（complete/incomplete 文案与计数展示）；卡片详情把"全市场账户比（回退，仅供参考）"单独列出；无动效改动 |
| `tests/test_live_futures_acceptance_glm.py` | 新增 R2 三类 13 项；S3b 回归钉改为修复后行为（注明日期） |
| `tests/test_request_governor.py` | 仅 1 处：回退标签断言更新为新文案 + 补 `ls_top` 缺席断言（标签属本修复契约面） |

```
PYTHONPATH=. py -3.10 -B -m unittest tests.test_live_futures_acceptance_glm   # 25/25 OK
PYTHONPATH=. py -3.10 -B -m unittest discover -s tests -p "test_*.py"         # 221/221 OK
PYTHONPATH=. py -3.10 -B ui/test_ui.py                                        # 25/25 通过
```

基线证据：`r2/baseline_prefix_tests.txt`（修复前 11 红，含暂态注记）；契约：`r2/contract.md`。

## 3. ds 最终补丁独立验收 —— 接受

Grok 曾定位四个阻塞项，本轮在交接版（34357e35）逐一核销：

| 阻塞项 | 交接版状态 | 独立验证 |
|---|---|---|
| note_data_request 不落盘 | `reserve_data_request` 在锁内检查+预约+`save()`（`:556-567`） | 探针1：A 实例预约 → B 实例 reload 可见 ✓ |
| reload 不同步 data_quota | `reload()` 同时同步两表 | 同上（B 读到时间戳列表）✓ |
| 固定窗口边界双突发 | 滑动窗口 `data_quota_delay_ms` | 探针2：塞满 600 后 [T, T+W) 全程等待、T+W 才放行 ✓ |
| 成功清暂停抹掉新期限 | `RequestGuard.run` 改 `clear_if_expired` | 探针3：未到期暂停不被晚到成功清除 ✓ |

另核对：`_connect_domain` 命中 `_LIMIT_ERRORS` 后 `banned=True; break`（不换出口绕封，`:653`）；`public_selection` 结尾整表 save 已删除（防陈旧副本覆盖他进程预约）；状态文件新增 `data_quota` 键，`ui/app.py::read_pauses` 只读 `domains` 不受影响；ds 测试 27/27 OK。

**保留意见（不阻塞接受）**：① `/futures/data/*` 1000/5min 是否跨端点共享一个桶仍是官方未明示的假设，600 自限是保守选择；② 共享出口他人流量无法被本地计数覆盖，留余量不等于不受影响；③ 这两项 ds 已如实标注，无需返修。

## 4. 受控真实单轮验收方案（只交方案，本轮未执行）

前置：继续遵守最新暂停期限（21:56:51+08 已过时，以届时 `results/rate_limit_state.json` 为准）；到期≠必恢复；唯一扫描进程；不探测、不清暂停、不换出口。

1. **时机**：利用用户下一次正常查询，不额外发起；执行前确认无其他扫描实例（`results/*.lock`、UI 进程）。
2. **观察点 A（执行完成）**：UI 状态行；若 failed，保存错误原文与 `paused` 快照，停止。
3. **观察点 B（数据完整）**：状态区轮次摘要行——合约 `N/M`、暂停/失败/跳过/异常计数、`incomplete` 徽标是否如实；`/api/state` 的 `round` 字段整体存档。
4. **观察点 C（候选）**：`has_passing` 与卡片数一致；empty + complete 才是"本轮无候选"。
5. **记录**：请求数/耗时（日志）、可得的响应额度头、`deriv_as_of_min` 与当前时间差、CSV（CLI 路径时全量 ranked）中 deriv ok 数 vs UI rows 数（核对落选暴露）。
6. **停止条件**：遇 429/418 立即保存并停止；本轮不连扫。几小时一次节奏的持续可用性仍需后续多轮观察（L3），本方案只覆盖单轮（L2）。

## 5. 本轮文件摘要（交付时点）

| 文件 | sha256 前 16 |
|---|---|
| binance_box_strategy.py | 3d8c92c75ec8a28f |
| 川沐十倍币筛选.py | 779b01b01552904f |
| ui/app.py | 88579ddc72c9ecc4 |
| ui/static/app.js | 725ff9d9626f8265 |
| ui/static/index.html | 17b6f59b40558823 |

剩余问题：见 summary.md 第 3 节保留意见；`ls_top` 缺失时"整键缺席 vs 键值 null"的形态不一致仍存在（属 r1 缺陷 6，本轮未扩大修复面，建议主代理裁决是否统一）。**到此停止，不追加扩展。**
