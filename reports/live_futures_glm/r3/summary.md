# R3 返修交付（glm5.3f）· 2026-10-07

对应 `tasks/live_futures_r2.md` 顶部最新要求与 `reports/live_futures_r2_final_review.md` 的 glm 阻塞。交接依据：`reports/live_futures_ds/r3/ready_for_glm.md`（SHA256 `b2c050134f0d8279…` 核对一致；r2 旧标记未采用）。**本轮只修"数据不完整却显示完整"**，未改研究公式、未设统一时效阈值、未做键/null 格式统一与动效。

## 正文结论

评审两项阻塞均已按"字段级"修复并复现存证：**修复前**（`review_repro_before_fix.txt`）`short → data_complete=True/ok`、`old → data_complete=True, as_of_min=1700716400000（被较新 LS 掩盖）`；**修复后**（`review_repro_after_fix.txt`）`short → data_complete=False/partial`（3 日变化不可算即不算取齐），`old → data_complete=False、as_of_min=1700608400000`（各字段**实际采用时间**的最小值，旧数据不再被盖住）且 `deriv_status=ok` 保留——字段都可计算是事实，"无法确认新鲜度"体现在整轮 `stale_rows=1` 与逐字段观测时间上。完整性判定从"数 status=ok"改为：**每个评分所需字段（oi_chg_1d/oi_chg_3d/ls_top）可计算 + 各自观测时间被保留 + 源数据尾部不存在"比采用点更新却不可用"的观测**。正常完整样本评分/排序/入选集合不变（专项回归钉），全库 233/233、UI 25/25、ds 35/35 全绿。ds 新暂停反例独立复跑 `NEW_PAUSE_PRESERVED=True`，另加跨实例加强探针通过——**ds 部分接受**。

## 1. 修复前后对比（评审两例）

| 案例 | 修复前（摘要 b2c05013） | 修复后（本轮 9ff20141） |
|---|---|---|
| 1. OI 仅 40 根小时数据 | `oi_chg_3d=None` 却 `deriv_status=ok`、`data_complete=True`（25 根即 ok_oi=True） | `oi_chg_3d` 不可算 → `ok_oi` 不置真 → **partial**；摘要 `data_complete=False` |
| 2. OI 尾部 30h 零值被过滤 | 用第 169 号时点算分；`as_of_min` 取"每币 max 再跨币 min"= 最新 LS 时间，掩盖 OI 落后 30h | 行级新增 `oi_as_of/ls_as_of`（各字段实际采用时间）与 `oi/ls_stale_tail`（源数据里比采用点更新却不可用的尾部观测数=30）；摘要 `oi_as_of_min=1700608400000`、`ls_as_of_min=1700716400000`、`as_of_min`=两者最小、`stale_rows=1` → **data_complete=False** |
| 正常完整样本 | — | 评分 16、排序、入选集合不变；`stale_rows=0`、`data_complete=True`（专项测试） |
| 序列中部合法零值 | — | 不影响完整性：计算基点仍是最新的可用观测（专项测试，区分"合法零值"与"尾部不可用"） |

判定规则（无时效阈值）：**可计算性**按字段（历史不足→该字段不可算→partial）；**新鲜度疑点**按结构事实——源序列尾部存在不可用观测（零值或缺字段）时无法区分"真零"与"缺口"，按任务要求"无法确认时保留事实并标不完整"，不猜、不掩盖；**合法零值**在序列中部被过滤不触发（基点仍最新）；**接口失败/暂停**沿用 failed/paused。

## 2. 改动与测试

| 文件 | 改动 |
|---|---|
| `binance_box_strategy.py` | `_fetch_deriv_context_live`：逐字段可计算标记（1d/3d 分开）、`oi_as_of/ls_as_of`、`oi/ls_stale_tail`；`ok` 收紧为"三个评分字段全部可计算"；`public_selection`：`stale_rows`、`oi_as_of_min/ls_as_of_min`、`as_of_min`（=跨字段实际采用时间最小值）、行级 4 个新键（键恒在、无值 null） |
| `ui/static/app.js` | 不完整摘要行加"陈旧 N"计数 |
| `tests/test_live_futures_acceptance_glm.py` | 新增 `R3FieldCompletenessTests` 4 项；`test_r2_as_of_min_not_max` 按 R3 语义更新（注明日期） |

```
PYTHONPATH=. py -3.10 -B -m unittest tests.test_live_futures_acceptance_glm  # 29/29 OK
PYTHONPATH=. py -3.10 -B -m unittest discover -s tests -p "test_*.py"        # 233/233 OK
PYTHONPATH=. py -3.10 -B ui/test_ui.py                                       # 25/25 通过
```

基线与复现存证：`r3/baseline_prefix_tests.txt`（4 红）、`r3/review_repro_before_fix.txt`、`r3/review_repro_after_fix.txt`。契约增补见第 4 节。

## 3. ds 部分独立验收 —— 接受

- 评审反例复跑：`PYTHONPATH=. py -3.10 reports/live_futures_ds/r3/repro_final_review.py` → **NEW_PAUSE_PRESERVED True**（修复前为 False）。
- 自有探针在最终代码上全部仍通过：跨实例预约可见（R2探针1）、滑动窗口无边界双突发（R2探针2）、晚到成功不清未到期暂停（R2探针3）、**跨实例加强探针**（B 登记新暂停后，A 的旧过期副本成功返回不删除新暂停——评审阻塞的直接反例）。
- 交接纪律遵守：本次 glm 改动未触碰任何状态文件写入路径，`save()` 未在生产代码新增调用。
- 保留意见（不阻塞）：`save()` 的"不合并"语义已由 docstring 标注仅限播种/单实例收尾；后续调用方需遵守 ds 第三节的写入纪律。

## 4. 契约增补（R3）

- 行级新键（键恒在，无值 null）：`oi_as_of`、`ls_as_of`（该字段计算实际采用的数据时点）、`oi_stale_tail`、`ls_stale_tail`（源数据尾部比采用点更新却不可用的观测数）。
- 摘要新键：`stale_rows`（ok 行中存在尾部不可用观测的行数）、`oi_as_of_min`、`ls_as_of_min`；`as_of_min` 语义改为**各字段实际采用时间的最小值**（r2 的"每币 max 再跨币 min"作废，见 r3 报告）。
- `deriv_status` 取值集合不变（ok/partial/failed/paused/no_futures），但 **ok 的判定收紧**为三个评分字段全部可计算；副作用：历史不足的币不再进入小时缓存（旧实现会把这种"假 ok"缓存一小时），同小时内重复扫描会对这些币多 2 次请求/轮——是诚实性与请求量的交换，如实记录。
- `data_complete` 新增条件 `stale_rows == 0`。

## 5. 交付时点文件摘要

| 文件 | sha256 前 16 |
|---|---|
| binance_box_strategy.py | 9ff20141a46b239b |
| 川沐十倍币筛选.py | 779b01b01552904f（本轮未改） |
| ui/app.py | 88579ddc72c9ecc4（本轮未改） |
| ui/static/app.js | 8682eae8b85028f8 |

**完成即停。** 通过后进入正常节奏的真实查询验收（方案沿用 r2 summary.md 第 4 节，本轮未执行、未联网）；不再扩展格式统一、美化或新工程。
