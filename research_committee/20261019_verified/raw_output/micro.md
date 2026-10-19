# D/E 组研究产出：市场微观结构与衍生品 + 量化研究/机器学习/算法搜索

日期：2026-10-19。状态：全部研究卡均为**待验证**；结构完整不等于假设成立。
诚实披露：本组与另外两组（A/C、B/F）是**同一模型（Kimi-K3）的独立上下文**，不是真人也不是不同模型投票。本产出只基于共享约束、项目文件独立取证与本轮联网检索，未读其他代理产出，未读 constraints.md 禁读清单中的任何文件。

---

## 1. 瓶颈定位（D/E 视角）

A~K 环节链为本组按可取证文件自行映射的理解：A 发现（币池范围）/ B 时点（何时看见）/ C 过滤（闸门）/ D 排序 / E 观察（有限关注成本）/ F 进入 / G 持有 / H 兑现 / I 继续参与 / J 研究方法（验证纪律）/ K 目标与测量口径。若与主代理定义不一致，以主代理为准。

**本组判断：瓶颈最可能在 J（研究方法）、K（测量口径）、D（排序）、B（时点）四环，其次是 G（持有）。**

- **J 研究方法（最可疑）**：现有"已验证"结论的可信度被四重折扣：① 07 号脚本在训练/测试划分前用全样本中位数填补（`research/07_oos_validate.py:155`，取证记录见 `PROJECT_CONTEXT.md:136`）；② 测试期已被用于比较阈值，"两个指标一起变好也不能证明没有过拟合"（`PROJECT_CONTEXT.md:265`）；③ 事件定义换参数后重合率仅 0.24~0.42，下游区分度"仅对该定义成立"（`KNOWLEDGE.md:137` 第 2.6 节 ②）；④ 核心信号区分度逐年衰减，某信号 2024 +24.7pp → 2026≈0（同上 ③）。四条叠加意味着：在讨论任何新策略之前，先不知道旧结论该打几折。
- **K 测量口径**：合约侧权重最大的两项（OI 1日/3日增，合计 7 分，满分 18）用的是 **OI 价值**（`binance_box_strategy.py:1010` 取 `sumOpenInterestValue`；`research/05_deriv_features.py:241-245` 用 `oi_val_last`），价值变化含价格成分，"不等于持仓数量增长"（`KNOWLEDGE.md:102-103` 已明示）。关键反证：**05 号脚本明明已下载并缓存了数量字段 `oi_amt_last`（`research/05_deriv_features.py:88`），却从未进入特征表**——分解实验的数据早就存在，只是没人算。
- **D 排序**：最终排序键是 `(score, futures_quote_volume)` 加和分数（`binance_box_strategy.py:1365`），分数是六项线性相加（`binance_box_strategy.py:69-77`）。用户实操瓶颈是"71 币过多、有限时间先看谁"（`STRATEGY_REVIEW.md:17`），这是 top-k 排序问题，现有框架从未按排序目标优化或评估过。`STRATEGY_REVIEW.md:297` 已指出"全部入选覆盖好、排前差 → 更可能要改排序"，但无后续。
- **B 时点**：研究只在"启动前一天"拍一次快照（`research/03_build_features.py:192` 取倒数第二根；`research/05_deriv_features.py:250-252` 取当日最后观测），`STRATEGY_REVIEW.md:256` 已自认"只在启动前一天拍一次特征，不能充分回答这个问题"。特征出现的**顺序与速度**（OI 先动还是价先动）在单时点快照里根本不可见。
- **G 持有（次要但真实）**：`signal()` 给固定 2R 止盈（`binance_box_strategy.py:832`，`reward_risk=2.0` 在 `:116`），与"保留多倍行情利润"的目标直接冲突，`STRATEGY_REVIEW.md:80` 已标注此风险。这不是本组主线，但任何进入改进都会被它吃掉上限。
- **反例（防止把瓶颈全推给方法）**：A/C 环已有相当证据支撑——闸门只漏 2.7% 爆拉币、0% 十倍币（`binance_box_strategy.py:104-106` 注释引用的实测），5 分门槛覆盖 616/822（`PROJECT_CONTEXT.md:260`）；范围与过滤暂不是最弱环。10x+ 组有 4 例启动前一天毫无征兆（`KNOWLEDGE.md:69`），说明发现环存在信息上限，但样本仅 4 例，不能升级为定论。

---

## 2. 研究卡（13 张，其中 subversive 3 张：DE-2、DE-9、DE-12）

### DE-1 OI 价值/数量分解：把"涨了的钱"和"新开的仓"分开

- id: DE-1
- title: 用数量口径重算 OI 增速，检验现有合约 7 分里有多少是价格的影子
- group: D 市场微观结构与衍生品
- kind: regular
- mechanism: OI 价值 = 数量 × 价格；价格涨 10% 而持仓不变时，价值增速同样报 +10%。若启动前真有新资金建仓，数量增速应先于或独立于价格走高；若只是价格推高价值，则该指标是动能的镜像，信息冗余。
- challenge: 现有 `oi_chg_1d/3d` 区分度 +12.6/+8.9pp（`binance_box_strategy.py:46-47`）是在价值口径上测的；本卡问的是同一现象换一个不含价格的口径后还剩多少。
- project_relation: 扩展。直接复用 05 号已缓存的 `oi_amt_last`（`research/05_deriv_features.py:88`）；与 STRATEGY_REVIEW §9.5 已列假设"持仓数量与价值分离"（`STRATEGY_REVIEW.md:312`）一致，本卡给出具体可执行形式。
- upside: 若数量口径区分度更高且价格剥离后仍有信号，合约分的含义从"追涨确认"升级为"新资金先行"，改善进入时点判断；若相反，则为 DE-2 供弹药。对应结果类型 D/B。
- data: 本地归档已有：`data/archive_raw/futures/metrics/`（5 分钟粒度，含 `sum_open_interest` 与 `sum_open_interest_value` 双字段，`research/05_deriv_features.py:44-46` 列明）；`data/metrics_cache.parquet` 已含 `oi_amt_last`。
- decision_time: 事件 pre_day 当日最后一个可得 5m 观测。
- availability: 只用 pre_day 及之前的 metrics 行；归档 ZIP 是日频文件，日内 5m 序列当时逐条可得。
- baseline: 同一批样本上价值口径的区分度（重算，不引用旧数字）；辅以简单趋势基线。
- experiment: 输入 = 822 正例 + 580 对照的 pre_day 窗口 metrics；对每个样本算 Δvalue、Δquantity、Δprice 三分量（Δvalue ≈ Δquantity + Δprice + 交互项，按 Coinwen 式分解，证据 W4）。对照 = 价值口径 vs 数量口径 vs 数量×价格交互项的区分度。判据 = 数量口径区分度差值 >5pp 且方向在 2025 前后一致（预注册）。失败条件 = 两口径差异 <2pp 或方向不稳。
- support: 项目自述价值口径含价格成分（`KNOWLEDGE.md:102-103`）；外部多个来源确认两口径可反向（W4）；`STRATEGY_REVIEW.md:85` 已把"OI 价值≠数量"列为未验证说法。
- refute: 数量口径区分度不高于价值口径；或数量增速与价格增速相关系数 >0.9（分解无增量信息）。
- failure_learning: 即使失败，也得到"OI 价值信号里价格成分占比"的定量账，知道现有 7 分该打几折。
- evidence_ids: M1, M2, W4
- result_type: D（进入与持有判断改善），若推翻则转 F（只修正解释）
- terms: OI、区分度、口径、pre_day、样本外验证

### DE-2（颠覆性）OI 合约分可能只是"已经涨了"的确认器

- id: DE-2
- title: 挑战：oi_chg_1d 的 4 分权重大半在奖励价格动能，而非建仓行为
- group: D 市场微观结构与衍生品
- kind: subversive
- mechanism: 爆拉启动前价格往往已开始抬升（02 号要求启动日 +15%，`research/02_detect_pumps.py:74-80`），pre_day 是启动日前一天（`:95`）——此时价格多半已动。价值口径 OI 与 24h 价格涨幅机械正相关；逻辑回归里 oi_chg 系数若主要由价格解释，则合约分不是独立证据维度，WEIGHTS 的"合约侧 10 分独立于一维 K 线 8 分"的组织方式（`KNOWLEDGE.md:237-238` 称两套独立证据）不成立。
- challenge: 直接挑战"K 线侧与合约侧是两套独立证据"这一核心组织假设。
- project_relation: 替代性检验。不删现有评分，先在同一底账上量共线程度。
- upside: 若成立，应把合约分改记到"数量变化"上，腾出的权重预算给真正独立的维度；改变进入判断的信息结构。结果类型 F（先纠正解释），后续才可能转 B/D。
- data: 现有 `features_deriv.csv` + metrics 缓存即可，无需新下载。
- decision_time: pre_day。
- availability: 同上。
- baseline: 价值口径 OI 对标签的边际贡献 vs 加入 24h 价格涨幅后的边际贡献。
- experiment: 在 07 号修正泄漏后的同款特征集上做三组逻辑回归（仅价格动能 / 仅 OI 价值 / 两者 + OI 数量），比较样本外 AUC 与 OI 系数衰减幅度；判据 = 加入价格后 OI 价值系数绝对值衰减 >60% 且数量项不显著 → 镜像说成立；预注册切分与指标。
- support: 价值=数量×价格算术上必然含价格（W4）；`STRATEGY_REVIEW.md:85` 已列"加入 OI 就能提高收益"为不能当已证实的说法。
- refute: 控制价格涨幅后 OI 价值仍有独立区分度（样本外 AUC 增量 >0.02），则镜像说不成立，DE-1 的分解方向升级为主攻。
- failure_learning: 否定后得到"OI 价值确实含独立信息"的较强结论，反向加固现有权重。
- evidence_ids: M1, M2, M5, W4
- result_type: F → 视结果转 B/D
- terms: OI、评分权重、口径、AUC、样本外验证

### DE-3 主动买卖失衡：klines 里躺着没用过的 taker 字段

- id: DE-3
- title: 用 taker 主动买入占比区分"自然买盘抢筹"与"被动成交/对倒放量"
- group: D 市场微观结构与衍生品
- kind: regular
- mechanism: 放量可以来自主动吃卖单（抢筹）、主动砸买单（出货）或中性换手。klines 的 `taker_buy_base/quote_volume` 字段记录主动买入方的量，失衡度（taker_buy/total − 0.5）是公开数据里最接近"谁在主动"的代理。启动前若量增由主动买入驱动，后续延续概率应高于中性放量。
- challenge: 现有 `c_v24up`（+1 分）只看量增不看方向（`binance_box_strategy.py:875-877`）；量比 ≥1.5 已被实测为 0.0pp 噪音（`KNOWLEDGE.md:347`）——"量"这个维度目前是瞎的，本卡给它装上方向。
- project_relation: 扩展。03 号下载的 1h 数据已含 taker 字段（`research/03_build_features.py:37-39` COLS 列明），但 `_features()` 完全没用它；metrics 另有 `sum_taker_long_short_vol_ratio`，05 号取了 `taker_last`（`:252`）进特征表却从未计分、07 号 FEATURES 虽列 `taker`（`research/07_oos_validate.py:47`）但无单独区分度报告。
- upside: 若成立，量增条件从 1 分噪音升级为带方向的独立维度，改善进入判断；同时可回测能否救活已否定的"量比"类条件。结果类型 B/D。
- data: 本地已有：1h/5m klines 的 taker 列（归档 `data/archive_raw/spot/klines/*/1h|5m`）、metrics 的 taker 比。
- decision_time: pre_day 最后一根已收盘 K 线。
- availability: klines 字段随 K 线收盘即得；无需新源。
- baseline: 现有 c_v24up 的区分度；量增×方向 的组合 vs 单独量增。
- experiment: 对 822/580 样本算 pre_day 前 24h 的 taker 买入占比与其 3 日趋势；比较三组（量增且主动买 / 量增且主动卖 / 量缩）的爆拉率差。判据：组间差 >8pp 且 2025 前后同号。失败条件：组间差 <3pp。
- support: 字段存在且从未使用（代码取证 M3）；同类失衡度量在 metrics 端点长期提供（`research/05_deriv_features.py:46`）。
- refute: 主动买入占比在爆拉组与对照组分布重合（KS 统计量小）；或高失衡反而对应更低爆拉率（放量即出货）。
- failure_learning: 失败后得到"公开 taker 字段在该粒度不含方向信息"的边界结论，排除一整类微观结构幻想。
- evidence_ids: M3, M4
- result_type: D/B
- terms: 主动买卖比、24h 量增、区分度、对照组

### DE-4 真挤仓 vs 假挤仓：资金费率极值 + OI 数量骤降 + 价格拒绝回落的三联判据

- id: DE-4
- title: 区分"真实轧空进行中"与"仅看起来具备挤仓条件"
- group: D 市场微观结构与衍生品
- kind: regular
- mechanism: 轧空的可观测签名是：空头拥挤（ls_top 低）+ 费率深负（空头付费）+ 价格上涨时 OI **数量**骤降（空头被强平回补，持仓消失而非新开）。三者同现才接近"强制平仓驱动"；仅 ls_top<1 只是"具备条件"，可能躺很久不爆。清算瀑布的加速段应表现为 5m 级 OI 数量阶梯式下跳与价格上跳交替。
- challenge: 现有 `ls_top < 1` 给 3 分（`binance_box_strategy.py:73`），其注释解释为"空头拥挤存在被轧空的可能"；但"有条件"与"正在发生"是两回事，误把条件当触发会导致长时间蹲守磨损。
- project_relation: 扩展。复用 05 号 metrics + funding 缓存（`research/05_deriv_features.py:101-132`）；为"持有/继续参与"环节提供失效判据。
- upside: 从"提前蹲"变成"确认后参与"，减少机会成本；并给持有期一个可观测的失效信号（OI 数量骤平后价格不涨 = 挤仓结束）。结果类型 D。
- data: 本地已有 metrics（5m）+ fundingRate（归档 `data/archive_raw/futures/fundingRate/`）；缺口：逐笔清算（forceOrders）未确认是否在归档库（本轮只核到 klines/metrics/fundingRate 三类顶层目录），若缺则用 OI 数量 5m 骤降做代理。
- decision_time: 日内 5m 级连续判定，不只 pre_day。
- availability: metrics 5m 行当时逐条发布；funding 每 8h 结算前可观察到预期费率（归档只含已结算值，历史重放可行）。
- baseline: 仅 ls_top<1 的既有区分度；三联判据 vs 单条件的后续 5/10 日收益分布。
- experiment: 在归档 5m metrics 上重放历史事件，标注"OI 数量 1h 降 >X% 且价格 1h 涨 >Y%"的片段，检验其与后续 3 日走势的关系；对照 = 仅 ls_top 低但无 OI 骤降的样本。判据与失败条件预注册（三联组后续收益中位数差 >5pp 才算成立）。失败条件：两组分布无差。
- support: OI 下降+价格上涨=空头回补是教科级解读，且 OI 会计规则支持（W4: PerpFinder 的 OI 增减表）；项目已有 funding 与 metrics 缓存。
- refute: 三联签名在历史事件中出现频率极低（<5% 事件）或其后续收益不优于单条件 → 判据无实操价值。
- failure_learning: 得到"币安公开数据上清算瀑布的可观测下限"清单，知道哪些只能去 Coinglass 类源验证（记访问缺口）。
- evidence_ids: M4, W4
- result_type: D
- terms: 轧空、爆仓、大户持仓多空比、资金费率、OI

### DE-5 洗盘路径状态机：把"启动前一天快照"换成"破位—收回—再确认"的时序判定

- id: DE-5
- title: 用 5m/1h 序列识别洗盘路径，回答"低分是不是只是正在回撤"
- group: E 量化研究（动态评分与状态识别）
- kind: regular
- mechanism: 同一币在 pre_day 得低分，可能是"没机会"，也可能是"正处于破位洗盘段"——单时点快照无法区分。状态机（平静→破位→收回→再启动）把评分变成路径函数：破位段不加分也不丢弃，收回出现才升级关注。
- challenge: 现有研究一天拍一次（`research/03_build_features.py:192`）；`STRATEGY_REVIEW.md:248-256` 已定义洗盘的可观察路径但无实现。
- project_relation: 扩展 STRATEGY_REVIEW §8.3（`STRATEGY_REVIEW.md:241-256`）为其首个实验的具体实现形式；与 A1 双向研究并行，不冲突。
- upside: 直接服务"进入与继续参与"：减少把洗盘中的赢家误丢，也避免把真下跌当洗盘死扛。结果类型 D，潜在 C（发现提前）。
- data: 本地归档 5m/1h klines 已就位（`data/archive_raw/spot/klines/*/5m` 本轮实测存在）。
- decision_time: 每根 5m/1h 收盘。
- availability: 全部 K 线收盘即得；无未来数据。
- baseline: 同一批候选上的现有单时点评分；三参与时机按 §8.3 冻结结构窗口比较。
- experiment: 对 822 事件与对照重建 pre_day 前 30 天的 5m 路径，按预冻结参数标注破位/收回状态；比较"首次突破参与 vs 收回后参与 vs 退出后再参与"三组在同资金约束下的扣费收益与漏选数。失败条件：收回后参与组不优于首次突破组且复杂度增加。
- support: 用户已把洗盘列为核心路径（`STRATEGY_REVIEW.md:23`）；地基审计显示大量"峰后深度回撤 468 例"是拿不住而非死（`KNOWLEDGE.md:137` ① 重切）。
- refute: 破位后收回组与破位后继续跌组在事前特征上不可分（状态标注无预测力）。
- failure_learning: 失败后留下完整的洗盘路径标注数据集，供后续任何模型复用。
- evidence_ids: M4, M6
- result_type: D（潜在 C）
- terms: 前视偏差、对照组、最大回撤；洗盘（待新增）

### DE-6 变化点检测替代固定阈值：治"口径假象"的正规药

- id: DE-6
- title: 用 BOCPD/变点检测识别"平静→蓄势"切换，替代 +10%/+20% 这类固定阈值
- group: E 量化研究（状态识别）
- kind: regular
- mechanism: 固定阈值在分布漂移时会翻转符号——多空比 2026 负号就是"固定阈值+分布漂移"的口径假象，分位口径下 −0.6pp（`KNOWLEDGE.md:137` ④）。变点检测不问"是否超过 +10%"，而问"生成过程是否刚换了一组参数"（量、波动、OI 数量的水平与噪声结构突变），对漂移天然免疫。
- challenge: 现有所有计分条件都是固定阈值（`binance_box_strategy.py:80-82`）；本卡挑战的是"阈值"这种判断形式本身。
- project_relation: 并行。不改现有评分，先作为独立的"状态标签"产出，评估其与事件的时序重合度。
- upside: 若成立，信号从"程度判断"升级为"时点判断"，直接攻 B 环（发现提前量）。结果类型 C/B。
- data: 本地归档 5m/1h klines + metrics 5m；无需新源。
- decision_time: 逐条数据到达时在线更新 run-length 后验。
- availability: BOCPD 是严格在线算法（W3），只用过去与当前观测。
- baseline: 现有固定阈值条件的首次触发时间 vs 变点告警时间，比谁先、谁误报多。
- experiment: 对 200 个有完整 metrics 的事件 + 等量的无事件币种时段跑 BOCPD（观测=量、|收益|、OI 数量变化三通道）；指标 = 告警到启动的中位提前小时数、每币每月误告警数。判据：提前 >24h 且误报 <2 次/币/月。失败条件：提前量为负或误报淹没告警。
- support: BOCPD 原文为在线精确推断（W3）；固定阈值失败案例在项目内已取证（`KNOWLEDGE.md:137` ④）。
- refute: 变点告警在事件组与对照组的命中率无差异（区分度 ≈0）；或告警普遍发生在启动之后（只是另一种滞后确认）。
- failure_learning: 失败也产出"哪类通道存在可检测前兆、哪类没有"的通道级结论。
- evidence_ids: M4, M7, W3
- result_type: C/B
- terms: 前视偏差、区分度；变化点检测（待新增）

### DE-7 排序学习：有限注意力下直接优化"谁排前面"，而不是给每个币打个加和分

- id: DE-7
- title: 用 learning-to-rank（LambdaMART/ pairwise）替换"加和分+成交额决胜"的排序
- group: E 量化研究（排序学习）
- kind: regular
- mechanism: 用户的真实问题是"每天只能认真看 N 个，哪 N 个"。这是排序问题，不是打分问题：回归/分类先拟合绝对值再排序，在噪声大、标签粗（爆拉/未爆拉）时会浪费容量在中段排序上；LTR 直接优化 top-k 的排序质量（NDCG@k），把模型容量集中在名单头部——正是人工注意力所在的位置。
- challenge: 现有排序是 `(score, futures_quote_volume)`（`binance_box_strategy.py:1365`），score 是六个 0/1 条件的加权和——并列极多，决胜靠成交额，与"谁更可能爆拉"无关。`STRATEGY_REVIEW.md:297` 已指出"排前差须改排序"，无后续。
- project_relation: 并行实验。不动在线评分，离线重建同一时点的全市场候选排序做对比。
- upside: 若 top-10/top-20 的事件覆盖在同等关注成本下提升，直接缓解"71 币过多"（`STRATEGY_REVIEW.md:17`）。结果类型 B。
- data: 现有 features + 事件库即可起步；严格版需 A1 的逐日全市场底账（当前只有事件+对照样本，非全市场横截面——本项目在样本内排名已踩过退化坑，`KNOWLEDGE.md:259-262`）。
- decision_time: 每日收盘后生成次日关注排序。
- availability: 特征全为当时可得；标签用后续 30 天是否达倍数档（仅评估器可读）。
- baseline: 现有评分排序、简单趋势排序（如 ret_30d）、多个固定种子随机排序（`STRATEGY_REVIEW.md:295` 已要求此类对照）。
- experiment: 按时间前推切分（训练 <2024，验证 2024-2025，确认段 2025 后冻结不用）；LambdaMART 优化 NDCG@20；指标 = 确认段 top-10/20/50 的事件覆盖率与逐日新增关注数；预注册。失败条件：top-20 覆盖不优于现有评分 +5pp 以内。
- support: 截面排序文献中 LTR 一致优于 regress-then-rank（W2，注意其 Sharpe 数字未扣成本、只作方向参考）；项目已知"排名条件必须用全市场分母"（`KNOWLEDGE.md:257-262`）。
- refute: LTR 在确认段的 top-k 覆盖不超出现有评分 + 简单趋势基线；或增益全来自某一年的少数事件（集中度检验失败）。
- failure_learning: 失败后得到"现有六条件的信息量只够区分、不够排序"的容量上限结论。
- evidence_ids: M5, M7, W2
- result_type: B
- terms: 横截面排名、样本外验证、过拟合；排序学习（待新增）

### DE-8 非线性交互：先问"哪两个条件必须同时在场"，再谈模型

- id: DE-8
- title: 检验条件交互（如"OI 数量增 × 价格未涨"），而非叠加更多单条件
- group: E 量化研究（非线性交互）
- kind: regular
- mechanism: 单条件区分度低不代表无用——项目已承认"组合、限定市场阶段仍可能有信息"（`KNOWLEDGE.md:342`）。"OI 数量增但价格还没涨"（有人在价格反映前建仓）与"OI 数量增且价格已涨"（追价）机制完全不同，加和评分把两者混成同一档分。
- challenge: 现有评分是六条件线性相加（`binance_box_strategy.py:69-77`），无法表达"同时满足才有效"。
- project_relation: 扩展。12 号脚本已在 K线=5 子组内做连续量细分（`research/12_optimize_gate.py:134-156`），本卡把该思路升级为交互项系统检验。
- upside: 若某个交互稳定，可救活若干已否定条件（换形式而非恢复原名加分），或减少误选。结果类型 B/D。
- data: 现有 features 表 + metrics 缓存。
- decision_time: pre_day。
- availability: 同现有研究。
- baseline: 线性加和评分在同切分下的 AUC/top-k。
- experiment: 预注册 ≤10 个有机制的交互项（不许穷举网格）：如 数量OI增×价格低波动、ls_top低×funding负、taker失衡×量增；用深度 ≤2 的树或显式交互项逻辑回归，时间前推三段；多重检验按 DE-9 账本校正。失败条件：无一交互通过校正后的确认段。
- support: 单条件已否定但组合未测的清单（`KNOWLEDGE.md:339-353`）；示例分解逻辑（W4）。
- refute: 交互项在训练段有效、确认段全部归零 → 证明交互空间在这批特征上是过拟合重灾区，应封禁而非扩展。
- failure_learning: 失败即得到"特征交互无增量"的封禁证据，防止后人重试。
- evidence_ids: M2, M5
- result_type: B/D
- terms: 区分度、过拟合、样本外验证、口径

### DE-9（颠覆性）多重检验校正与试验账本：先给"三个稳定信号"重新量刑

- id: DE-9
- title: 挑战：现有结论是在未计试验次数的情况下挑出的赢家，校正后可能不剩几个
- group: E 量化研究（多重比较防过拟合）
- kind: subversive
- mechanism: 项目先后测过 18+ 条件（`爆拉案例复盘报告.md` 经 `KNOWLEDGE.md:506` 摘要）、扫过 0~18 全阈值（`research/09_backtest_revised.py:147`）、比较过 8 种规则组合（`research/12_optimize_gate.py:86-97`），测试期还被反复用于比较（`PROJECT_CONTEXT.md:265`）。 Bailey–López de Prado 的 DSR 指出：不控制尝试次数，最优结果的期望在纯噪音下也显著为正（W1）。现有"+12.6pp 稳定"未经任何此类校正。
- challenge: 直接挑战 C3（"三个真正稳定的信号"，`REASONING.md:95-120`）的统计地基；不挑战其方向，挑战其置信度。
- project_relation: 基础设施级扩展：给研究链加"试验账本"（每次假设、切分、指标、结果落盘），并对存量结论做一次性追溯校正。
- upside: 知道每个旧结论该打几折后，后续所有实验卡的判据才有尺子；防止委员会三十张卡再制造一轮赢家诅咒。结果类型 F（校正认知），间接保护 A~E 全部。
- data: 现有全部研究产物 + 手工追溯的试验清单；无新数据。
- decision_time: 不适用（元方法）。
- availability: 不适用。
- baseline: 未校正的既有区分度/精确率。
- experiment: ① 重建试验次数 N（逐脚本统计条件数×切分数×阈值数，下限即可）；② 对核心结论做 BH-FDR 与 DSR 式校正（用收益代理序列时说明近似）；③ 报告校正后仍成立/存疑/归零三档清单。判据 = 哪些结论在校正后 p 值仍 <0.05；失败条件 = 试验账不可追溯（N 无法估计），则改为"从今往后记账"并标注存量结论不可校正。
- support: DSR 原文与机制（W1）；项目自身已承认"反复看测试集后再挑规则=变相调参"（`REASONING.md:60-62`）。
- refute: 若追溯显示实际独立试验次数很小（如核心三条件在首次切分即入选且阈值预冻结），则校正后结论不变，本卡转为纯流程改进。
- failure_learning: 无论如何产出一个运转的试验账本——这本身是后续所有研究的公共品。
- evidence_ids: M5, M7, W1
- result_type: F
- terms: 过拟合、样本外验证、区分度、提升倍数；试验账本（待新增）

### DE-10 相似行情检索：用形态近邻替代条件清单

- id: DE-10
- title: 在 5m/1h 路径空间检索"历史上长得像的启动段"，作为评分之外的独立证据通道
- group: E 量化研究（异常检测与相似检索）
- kind: regular
- mechanism: 条件清单只能表达被命名过的模式；爆拉前的量价/OI 联合形态（缩量平台→量渐增→小幅抬升→加速）可能没有现成名字。把 pre_day 前 N 天的多维序列做成嵌入/特征向量，检索历史最近邻，统计近邻的后续结果分布——让历史自己投票，而不是让人起名字。
- challenge: 现有方法每加一条规则都要先有人命名它；本卡挑战"规则必须先有名字"的生产方式。
- project_relation: 并行。不替代评分，作为第二意见通道，与 DE-7 的排序可组合。
- upside: 若近邻投票有区分度，可捕捉现有六条件无法表达的蓄势形态。结果类型 B/C。
- data: 本地归档 1h/5m klines + metrics；需先解决同一币不同时期的量纲归一（用自身分位数，不用全市场横截面，避开 4.2 节的坑）。
- decision_time: 每日收盘。
- availability: 特征只用过去窗口；近邻库只含检索时点之前已完结的案例。
- baseline: 随机近邻（打乱时间）、现有评分。
- experiment: 构造 20~50 维路径特征（收益、量比、波动、OI 数量变化的分段统计），欧氏/DTW 距离取 k=20 近邻；指标 = 近邻正例率对当前样本标签的区分度 + 时间前推确认。判据：区分度 >8pp 且确认段同号。失败条件：近邻投票与随机无差。
- support: 形态聚类是成熟工具；项目已具备全量原始路径数据（M4）。诚实标注：本方向在加密公开市场数据上的直接先例本轮未取证到一手文献，属推测性移植。
- refute: 近邻结果对距离度量/窗口长度极度敏感（换个参数结论翻转）→ 说明捕捉到的是噪声指纹。
- failure_learning: 失败即排除"无监督形态"整条路线，并留下路径特征库供 DE-7/DE-11 复用。
- evidence_ids: M4
- result_type: B/C
- terms: 对照组、区分度、过拟合

### DE-11 自动规则发现 + 预注册淘汰：遗传编程只做"提案机"，判决交给冻结确认段

- id: DE-11
- title: 用遗传编程在探索集挖掘可读规则，在从未触碰的确认段一次性判决
- group: E 量化研究（自动规则发现与假设淘汰）
- kind: regular
- mechanism: 人工提假设的速度是瓶颈之一。GP 在价格/量/OI 数量的简单算子空间进化布尔规则，优势是产出**可读**（不像黑箱），且搜索日志天然就是 DE-9 要的试验账本（每一代个体都是一次已记录的尝试）。
- challenge: 挑战"假设只能由人提出"的生产方式；同时直面 Allen & Karjalainen 1999 的教训：GP 挖出的规则样本外扣费后无一致超额（W5）——所以本卡的纪律设计（探索/确认彻底分离、确认段只许用一次）比搜索算法本身更重要。
- project_relation: 并行。产出物是"待人工审核的规则提案"，不直接进评分。
- upside: 把假设产能从"人每周几条"提到"机器每轮几百条+自动淘汰"，攻 J 环的研究效率瓶颈。结果类型 F→B（先证明流程能产出存活规则）。
- data: 现有 features + 归档原始序列。
- decision_time: 规则判定时点 = pre_day 式时点或逐日。
- availability: 探索集 ≤2024；确认段 2025 后冻结，全项目只准用一次。
- baseline: 人提的既有条件集；随机生成的规则集（同数量、同搜索量）。
- experiment: 预注册：适应度 = 探索集区分度 − 复杂度罚；搜索预算封顶（如 10 万个体评估）；产出 top-10 规则进确认段一次性评估；判据 = ≥1 条规则确认段区分度 >5pp 且经 DE-9 账本校正后存活；失败条件 = 全部归零（这本身就是 Allen&Karjalainen 式结论在本项目数据上的复现）。
- support: GP 挖规则有 25 年文献史与明确的失败前科（W5）；多重检验校正工具已备（W1）。
- refute: top 规则在确认段无一存活 → 证明该算子空间 + 该数据下自动发现无产出，封禁此路线。
- failure_learning: 失败留下完整搜索轨迹（哪些形式被反复进化出来又死掉），是比结论更耐用的知识。
- evidence_ids: M5, W1, W5
- result_type: F→B
- terms: 过拟合、样本外验证、区分度；试验账本（待新增）

### DE-12（颠覆性）权重不该是常量：信号逐年衰减要求权重有时效，"单一真源"需版本化

- id: DE-12
- title: 挑战：WEIGHTS 常量与"区分度逐年衰减"实测直接矛盾，应改为滚动校准的版本化权重
- group: E 量化研究（在线学习/权重时效）
- kind: subversive
- mechanism: 地基审计实测核心信号区分度逐年衰减，某信号 2024 +24.7pp → 2026≈0（`KNOWLEDGE.md:137` ③）。信号被越来越多人看到就会失效——这是市场自适应，不是数据错误。常量权重隐含"2018 年的区分度在 2026 年仍成立"，与项目自己的实测矛盾。
- challenge: 直接挑战"WEIGHTS 是全项目唯一真源、改权重只改这一处"（`binance_box_strategy.py:66-67`、`KNOWLEDGE.md:276-277`）所固化的"权重无时间维"假设；不挑战单一真源工程价值，挑战其静态语义。
- project_relation: 扩展：WEIGHTS 仍单一真源，但变为"版本化序列"（每年/每季度一个校准版本，附有效期与溯源）；在线用当期版本，回测用历史对应版本。
- upside: 让评分自动退出失效信号、放大当季有效信号，防止策略被"保质期" silently 吃掉。结果类型 B/D。
- data: 现有逐年样本即可；严格版需 A1 全市场底账。
- decision_time: 每个校准窗口末。
- availability: 校准只用窗口内已完结数据；前视纪律 = 版本 v_t 只能用在 t 之后。
- baseline: 常量权重在 2025-2026 确认段的表现。
- experiment: 滚动窗口（如 24 个月训练+6 个月生效）重估各条件区分度，映射为分段权重；比较常量 vs 滚动在确认段的 top-k 覆盖与误选数；预注册窗口长度与映射函数。判据：滚动版确认段覆盖提升 >5pp。失败条件：滚动版因逐年噪声追逐而不如常量（衰减信号上滚动估计方差大于偏差收益）。
- support: 衰减已实测（M7）；McLean & Pontiff 式发表后衰减的同类机制（W1 搜索摘要中提及，本轮未取得原文，标为摘要级）。
- refute: 逐年区分度波动被证明主要来自样本量小而非真衰减（如各年事件数 <30）→ 滚动估计不可行，常量反而是正确正则化。
- failure_learning: 失败留下"各条件区分度的年度方差表"，直接告诉后人哪些信号根本不值得滚动跟踪。
- evidence_ids: M7, W1
- result_type: B/D
- terms: 评分权重、样本外验证、过拟合；信号保质期（待新增）

### DE-13 反事实标签：把"跟着 BTC 涨"从事件库里剥离再重测一切

- id: DE-13
- title: 用 BTC 剥离后的反事实涨幅重建事件标签，检验现有结论有多少是水位幻觉
- group: E 量化研究（因果/反事实）
- kind: regular
- mechanism: 地基审计已发现约 1/3 事件涨幅带"跟着 BTC 水位"成分（`KNOWLEDGE.md:137` ①）。一个币从 1 涨到 3，若同期 BTC 涨了 50%，币自身的"超额涨幅"远小于名义三倍。用 BTC 同期收益剥离后的反事实涨幅重定事件标签与倍数档，现有区分度、命中率、分档表全部要在这个更干净的标签上重签。
- challenge: 挑战"价格倍数=币自身行情"这一默认标签语义；标签错，下游全错（与 KNOWLEDGE 2.6 ② 的事件定义敏感性同族）。
- project_relation: 扩展地基审计 ① 的后续；与 TODO A1 主线互补，不重叠。
- upside: 若剥离后事件库缩水或区分度重排，所有历史结论的折扣线从"约估"变成"定量"。结果类型 F（校正）→ 重新校准后才谈 A/B。
- data: 本地日线已有 BTC 与全部币种；无新源。
- decision_time: 标签重建是离线步骤；后续判定沿用原时点。
- availability: BTC 日线当时可得，无前视。
- baseline: 原名义标签下的既有统计。
- experiment: 对每个事件计算窗口内 BTC 收益，反事实倍数 = (1+币涨幅)/(1+BTC涨幅)（或用残差收益口径，预注册选一种）；重建事件库（同阈值）；重跑 09/11 的关键表；判据 = 剥离前后"十倍组"名单变化与核心条件区分度排名变化幅度；失败条件 = 两者几乎不变（水位污染对结论无实质影响，折扣线①可降级）。
- support: 地基审计已定量（M7）；02 号事件定义对参数敏感（`research/02_detect_pumps.py:30-33`，M7 同条）。
- refute: 剥离后结论不变 → 本假设被推翻，BTC 水位不再是读涨幅时的必要折扣。
- failure_learning: 两种结局都产出"标签语义对结论敏感性"的定量结论。
- evidence_ids: M7
- result_type: F
- terms: 爆拉、口径、对照组

---

## 3. 证据清单

项目内取证（本轮亲自读文件核实）：

- M1 `binance_box_strategy.py:1010`（OI 取 `sumOpenInterestValue` 价值口径）、`:69-77`（WEIGHTS）、`:1365`（排序键）、`:832`/`:116`（固定 2R 止盈）、`:46-47`（OI 区分度注释）、`:80-82`（固定阈值常量）、`:73`（ls_top 轧空注释）、`:66-67`（权重单一真源注释）。取得状态：已读全文。
- M2 `research/05_deriv_features.py:44-46`（metrics 含数量+价值双字段）、`:88`（`oi_amt_last` 已缓存未使用）、`:151`（合约特征仅 gain≥2 事件）、`:241-245`（增速用价值算）、`:252`（taker 进表未用）、`:101-132`（funding 缓存）。取得状态：已读全文。
- M3 `research/03_build_features.py:37-39`（1h 数据含 taker_buy 字段）、`:192`（单时点快照）、`:219`（特征调用 box_score）。取得状态：已读全文。
- M4 归档库结构：`data/archive_raw/` 下实测存在 `spot/klines/{1d,1h,5m}`、`futures/metrics`、`futures/fundingRate`（本轮 ls 命令核实）；规模约 100 万份 ZIP 见 `KNOWLEDGE.md:107-108`；KNOWLEDGE 3.1 节明确"新增研究维度必须回原始库"（`KNOWLEDGE.md:205-209`）。取得状态：目录级核实，未逐份校验内容。
- M5 `research/07_oos_validate.py:155`（划分前全样本中位数填补）、`:44-50`（FEATURES 含 taker）、`:40`（SPLIT）；`research/09_backtest_revised.py:147`（全阈值扫描）、`:73-85`（合约缺失 NaN 语义）；`research/12_optimize_gate.py:86-97`（八规则对比）、`:134-156`（K线=5 内细分）；`research/11_hit_rate_current.py:45`（env_config）、`:76-96`（闸门复现）；`research/02_detect_pumps.py:30-33,69,74-80,95,98`（事件定义参数）。取得状态：已读全文。
- M6 `STRATEGY_REVIEW.md:80`（止盈风险）、`:241-256`（洗盘路径定义）、`:256`（单时点不足自认）、`:285-297`（双向验证与排序）、`:312`（数量/价值分离假设）、`:17`（71 币过多）、`:295`（随机对照要求）。取得状态：已读全文。
- M7 `KNOWLEDGE.md:137`（地基审计四条折扣线）、`:222-232`（多空比三口径）、`:339-353`（已否定条件表）、`:342`（"组合仍可能有信息"）、`:259-262`（样本内排名退化坑）、`:237-238`（两套独立证据说法）、`:276-277`（WEIGHTS 单一真源）、`:69`（4 例无征兆）、`:102-103`（OI 价值≠数量）；`REASONING.md:60-62`（变相调参自认）、`:95-120`（C3 三稳定信号）；`PROJECT_CONTEXT.md:136`（泄漏取证）、`:265`（测试期复用）、`:115`（合约覆盖与标签相关）、`:260`（5 分覆盖 616/822）。取得状态：已读全文。

外部取证（本轮 WebSearch，均为搜索摘要级，未读全文，不得升级为全文证据）：

- W1 Bailey & López de Prado (2014) "The Deflated Sharpe Ratio", JPM 40(5), SSRN abstract 2460551；另见 davidhbailey.com 公开 PDF 摘要片段。取得状态：摘要+公式片段。支持：多重检验与选择偏差会系统性抬高最优回测表现，需按试验次数校正。不支持：任何本项目具体数字。另一摘要级：McLean & Pontiff (2016) 发表后衰减（经 QuantInsti 文章转述，未取得原文）。
- W2 Poh, Lim, Zohren, Roberts "Building Cross-Sectional Systematic Strategies By Learning to Rank"（arXiv 2012.07149 的中文/英文摘要页）。取得状态：摘要级。支持：排序问题上 LTR 优于 regress-then-rank 的方向性结论。不支持：其 Sharpe 倍数（批评者指出未扣换手成本，aligrithm.com 摘要级）、加密市场适用性。
- W3 Adams & MacKay (2007) "Bayesian Online Changepoint Detection", arXiv:0710.3742。取得状态：摘要级（Princeton 书目页、HuggingFace 页、讲解博客）。支持：存在严格在线、只用过去观测的变点推断算法。不支持：在本项目数据上的效果。
- W4 OI 价值/数量口径：Athenum 博客（2026-07-24 实例：价值 +3.03% vs 数量 −0.85% vs 价格 +3.92%）；CoinScreener 博客（z-score + 数量变化双测试的告警设计）；PerpFinder 指南（OI 会计规则：双开+1/双平−1/换手不变）；Coinwen（数量/重估价分解记账法）。取得状态：博客级，非交易所官方文档。支持：价值口径含价格成分、两口径可反向、分解可行。不支持：币安 `sum_open_interest` 字段的官方定义细节。
- W5 Allen & Karjalainen (1999) "Using genetic algorithms to find technical trading rules", JFE 51(2):245-271（Google Scholar / Mendeley 摘要）。取得状态：摘要级。支持：GP 挖出的规则样本外扣费后无一致超额——本组 DE-11 的反面教材与纪律依据。不支持：GP 在本项目数据上必然失败。

## 4. 访问缺口清单

1. 币安官方文档中 `sum_open_interest` vs `sum_open_interest_value` 的字段定义原文本轮未直接取证（W4 为第三方解读）；DE-1/DE-2 实施前须以归档实际数据核对两字段数值关系自证。
2. 逐笔清算（forceOrders）与 aggTrades 是否存在于币安公开归档、是否已在 `data/archive_raw/` 内：本轮只核实到顶层三类（klines/metrics/fundingRate），未逐层枚举；DE-4 的清算瀑布研究在确认前只能用 OI 数量骤降做代理。
3. Coinglass/Coinalyze 等第三方清算与全市场 OI 源未访问（需登录/付费风险），DE-4 的跨所验证缺口保留。
4. W1/W2/W5 均为摘要级；若任何一张卡进入实施，应先取得原文核对方法细节。
5. "形态检索/异常检测在加密公开数据上的先例"未找到一手文献，DE-10 的移植性为推测，已在卡内标注。
6. 项目内 `reports/` 下 A1 各轮报告（r2/r3 summary 等）按约束只作研究状态证据引用其被转述的结论，本轮未逐份深读；cards 中涉及 A1 的判断均以 PROJECT_CONTEXT/KNOWLEDGE/TODO 的转述为准。

## 5. 待新增术语清单（只列名与一句话，定义权交主代理入 glossary/terms.yaml）

1. **洗盘**：LEARNING.md 正文出现（:457）但无词条；建议定义按 `STRATEGY_REVIEW.md:243` 的"可观察路径候选"表述（跌破先前结构、停留、收回等，不含操纵者意图断言）。
2. **变化点检测**（changepoint detection）：判断序列生成参数何时发生突变的在线/离线方法族（本组用于替代固定阈值）。
3. **排序学习**（learning-to-rank）：直接优化名单排序质量（如 NDCG@k）而非逐个打分的机器学习方法族。
4. **试验账本**：记录每一次假设、参数、切分与结果的强制日志，用于多重检验校正与防止只展示赢家。
5. **信号保质期**：某条件区分度随时间衰减的现象与度量（对应 KNOWLEDGE 2.6 ③，现无词条）。
6. **数量口径 OI**（相对价值口径）：以 `sum_open_interest`（持仓数量）计算的 OI 变化，区别于含价格成分的价值口径。

（本文件写完即停；不做交叉审阅，不修改任何其他文件。）
