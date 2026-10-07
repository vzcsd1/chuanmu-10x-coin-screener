# 实时合约完整性独立验收（glm5.3f）· 主报告

日期：2026-10-07（UTC+8）。审核对象 sha256 前缀：binance_box_strategy.py `9583c9241dba8139`、川沐十倍币筛选.py `15270d09f23bdecf`、ui/app.py `f6146375965c49d0`（共享目录若变动，本报告结论需对新摘要重核）。测试：`py -3.10 -B -m unittest tests.test_live_futures_acceptance_glm` → **12/12 通过**；全库 181 项测试通过，全程离线，未写真实 results/。

## 正文结论（≤500 字）

用户问的核心是：**"网页只见过门槛卡片，会不会掩盖整轮数据缺失"——会，已实测复现。**`川沐十倍币筛选.py:34` 只返回 `score≥min_score` 的行；因合约缺失落选的币（测试 S1/S2）不出现在任何 UI 结构里。UI 三态 success/empty/failed 均能被误触发：合约域初始化失败时，有合约的币被标成"无合约"（S1），与真无合约逐字段相同（S4）；逐币全部失败与真空结果的三元组完全一致（S8）。因此"返回卡片字段齐全"不能证明整轮完整，必须由扫描层补整轮摘要。其次，**回退多空比仍在计分**（S3b：回退口径 0.9<1 拿了 3 分），与 REASONING C4「不拿另一口径顶上计分」直接矛盾。请求预算官方证据：`/futures/data/*` 共享 **1000 请求/5 分钟**（权重 0），N=400 无缓存单轮 800 次可过，但同一小时边界两轮突发会超——这与今天 12:05 的 418 相符；跨小时缓存命中实测省全部历史请求（S7）。ds 修复**未交付**（reports/live_futures_ds/ 不存在，binance_box_strategy.py 未改动），其请求预算核对清单已留 acceptance.md。本轮不改策略、不改生产源码、不联网；真实查询与几小时节奏观察未做、标未验证。

## 八情形复现矩阵（实际函数 + 假接口 + UI 层）

| 情形 | 复现方式 | 整轮对象/所需字段 | 已取到 | UI 最终状态 | 掩盖点 |
|---|---|---|---|---|---|
| S1 合约初始化失败 | `connect_exchanges→(spot,None)`（`:464-474` 真实降级形态） | 2 币；需 oi/ls 三项 | 仅现货分 | **success**，rows=1，paused=[]，error=None | 有合约的 DOGE 被标 `no_futures`；BBB 落选不可见 |
| S2 中途限流 | 第 2 次 OI 请求抛 418 | 3 币 | A 全齐；B 仅 K 线；C 落选 | **success**，rows 内 ok/paused 混杂，paused 有记录 | C 因断供丢 10 分落选，无任何计数 |
| S3 一项字段缺失 | LS 两口径均故障 | 1 币 | oi 两项+K 线 | success | `ls_top` 整键缺席（非 null）；`ls_source` 键在值空——两种缺失形态并存 |
| S3b 回退口径 | LS 故障 + 回退可用 | 同上 | 回退值 0.9 | success，score 16 | **回退口径参与计分 +3**（`:779`+`:960`），违反 C4/STRATEGY_REVIEW §5 |
| S4 真实无合约 | BAR 不在合约市场 | 2 币 | BAR 无合约数据 | success | 三个关键字段与 S1 断供行完全相同，UI 无法区分 |
| S5 上市历史不足 | 20 根 K 线（<box_period+3=27，`:906`） | 2 币 | NEW 被跳过 | success | 无行、无计数；40 根币 `entry` 恒 None 无说明 |
| S6 合法零值 | OI 末尾 30h 全零 / 全零 / 费率 0 / OI 快照 0 | 1 币 | 见表 | success | 尾零被 `v>0`（`:729`）静默过滤仍标 ok；as_of 取两源最大（`:792`）可被较新 LS 支配；全零与接口故障同为 partial 无区分 |
| S7 旧缓存/旧结果 | 同小时二次扫描；断供中读缓存；UI last | 1 币 | 缓存（≤1h 旧） | success/failed | 断供中缓存币仍标 ok（设计如此，ok≠"此刻拉到"）；失败轮 last 保留且带原 finished_at ✓ |
| S8 空结果 vs 全失败 | 平盘 0 分 vs 逐币全抛异常 | 1 币 | 0 行 | **两者三元组完全一致**：status=empty，error=None，rows=[] | 总失败可冒充"无候选" |
| 正面对照：现货域暂停 | 预置 spot 暂停 | — | — | **failed**，rows=None，error 含"暂停"，last 保留 ✓ | 这是应有的诚实路径，已验证可用 |

## 请求预算（方法调用实测 × 官方权重，查阅 2026-10-07）

来源：[USDⓈ-M Market Data](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data)、[Spot REST](https://raw.githubusercontent.com/binance/binance-spot-api-docs/master/rest-api.md)。

| 请求 | 每轮次数（实测口径） | 官方权重 | 官方限制 |
|---|---|---|---|
| 现货 ticker/24hr | 1 | 80 | IP 权重制；上限值未核实（需 exchangeInfo，本轮禁探测） |
| 现货 klines 1h×240 | N（≤400） | 2/次 | 同上 |
| **/futures/data/openInterestHist** | N | **0** | **IP 1000 请求/5 分钟**（共享）；仅近 1 月 |
| **/futures/data/topLongShortPositionRatio** | N（回退时 +N） | **0** | **IP 1000 请求/5 分钟**（共享）；仅近 30 天 |
| fapi ticker/24hr | 1 | 40 | fapi REQUEST_WEIGHT 2400/分钟 |
| OI 快照+费率（展示行） | 2×shown | 1+1 | 费率另有 500/5min/IP 共享限制 |
| CoinGecko/OKX | 4 / 2 | 独立服务 | 本轮未核实其限额 |

**推论（有依据）**：N=400、缓存未命中的一轮 = 800 次 /futures/data/ 请求。单轮 <1000 可过；但 300 秒轮询跨小时、或 10:59 与 11:01 各扫一次（缓存按自然小时对齐，`:669`）→ 5 分钟窗口内最多 1600 次 → **超 1000/5min，触发 429→418**。该模式与 `results/rate_limit_state.json` 今日 12:05 的 418（封至 21:56:51+08）相符；"几小时一次"只在跨小时边界前后两次查询时仍可能双突发。ccxt enableRateLimit（`:277`）50ms 节流≈20 次/秒，800 次约 40 秒发完——不构成对 1000/5min 的保护。

## 最小结果契约建议（扫描层提供，不由前端猜）

字段名复用已有定义（deriv_status/deriv_as_of/has_futures/extras_deferred）；范围覆盖"进入既有扫描流程且需要合约字段的全部对象"（过闸门且被逐币处理者）；历史长度按当前策略实际所需（K 线 lookback 240×1h；合约 DERIV_LOOKBACK 200×1h 覆盖 3 日窗口），**不另设时间阈值**：

```json
{
  "task_completed": true,      // 全流程走完未被 DomainPaused 中止（现货暂停=false）
  "data_complete": false,      // futures_fetched_ok == futures_expected 且 errors==0
  "has_passing": true,         // 存在过门槛候选
  "scope_total": 387,          // 过闸门且逐币处理开始的对象数（含被跳过者另计）
  "futures_expected": 302,     // 其中按合约市场列表应有合约者（futures=None 时=0 并置 deriv_domain_state）
  "futures_fetched_ok": 291,   // deriv_status==ok 的对象数（partial/failed/paused 不计）
  "deriv_domain_state": "ok",  // ok|paused|failed|no_client —— 消除 S1/S4 的 no_futures 混同
  "skipped_short_history": 5,  // 消除 S5 不可见
  "errors": 0,                 // 逐币异常计数（消除 S8 混同）
  "as_of_min": 1760000000000   // 全字段最旧观测时间（取代 max 语义，消除 S6 陈旧掩盖）
}
```

展示规则：`data_complete=false` 时结果可查看但必须标"本轮不完整（缺 N/M 合约数据、errors=E）"，不得当作完整推荐；旧完整结果只在 last 中带原 finished_at 展示。三者关系：task_completed ∧ data_complete ∧ has_passing 各自独立，任一为 false 都不能显示"成功"。

## 三条最小路线比较（只比较，未实施）

| 路线 | 收益 | 成本/维护 | 限制 | 结论 |
|---|---|---|---|---|
| 1. 修现有请求节奏 | 消除跨小时双突发（唯一有官方数据支持的触发模式） | 最小：轮次间按 1000/5min 简单计数，代码量小，可回退 | 不解决 F1-F3 可见性；不等于数据恢复 | **先做**，且需与整轮摘要分开验收 |
| 2. 解决共享出口 | 若 418 确由共享出口造成可根治 | 专用出口有成本与维护 | **未证实**：本轮 418 的 IP 归属/共享流量无证据（scan_boundary_review 也未证实） | 不实施，等 ds 取证 |
| 3. 补同口径数据源 | 合约域断供时的平行预警 | 第三方源需按 C4 先验证等价性 | metrics 归档是日级，**不是实时接口替代** | 不实施 |

## 分层验收状态

| 层 | 状态 |
|---|---|
| L1 离线正确性 | ✅ 12/12（tests/test_live_futures_acceptance_glm.py，2.2s） |
| L2 一次真实查询 | ❌ 未做（本轮禁联网；暂停至 21:56+08）——清单见 acceptance.md |
| L3 几小时一次节奏连续观察 | ❌ 未做，标未验证 |

"防止不完整结果冒充成功"（L1 已证可行 + 契约建议）**不等于**"真正恢复完整数据"（需 L2/L3）。
