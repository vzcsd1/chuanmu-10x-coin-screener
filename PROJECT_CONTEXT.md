# 项目接续说明

阅读与离线核验日期：2026-10-04（Asia/Shanghai）。本文件记录本轮阅读结果；历史研究结论仍链接原报告，不复制为新的策略定稿。

2026-10-04 后续状态更新：用户要求扩大观察池，策略默认值和合约限制已调整，当前配置、复核结果与回退方式统一见 [CANDIDATE_POOL_CHANGE.md](CANDIDATE_POOL_CHANGE.md)。下文保留初次阅读现场，已失效的当前状态描述标记更正。

## 1. 本轮需求入账

- 用户要求：阅读项目，以便后续完善；随后要求继续。
- 完成范围：目录与已有说明、在线筛选核心代码、研究数据流、历史评分产物、运行环境及离线一致性核验。
- 本轮仅新增接续说明，并在已有长期记忆中加入入口、纠正已核实过时的环境记录；不改业务代码、权重、原始数据或研究产物。
- 未运行在线扫描、重新下载数据或重建历史特征，因此本轮不对网络连通性、接口当前可用性和策略盈利能力作结论。

## 2. 当前项目是什么

这是币安公开行情观察池筛选器，没有下单路径。现货价格与成交量负责量价评分，永续合约的 OI（未平仓合约价值）变化和大户持仓多空比负责合约评分；OKX、市值、资金费率等补充展示信息。

主要实现集中在 `binance_box_strategy.py`。研究部分是先找历史爆拉事件、再比较其启动前与同币种平静期的特征；它不是逐日全市场、包含成交和退出的收益回测。

| 路径 | 职责与阅读入口 |
|---|---|
| `binance_box_strategy.py` | 权重与配置、网络连接、指标、两类评分、排序、表格与 CSV；核心入口见第 514、732、764 行 |
| `川沐十倍币筛选.py` | 调用同一筛选函数，过滤门槛后打印 JSON；第 23 行 |
| `run_chuanmu.py` | 按文件名寻找中文入口并执行；第 6 行 |
| `启动川沐筛选.bat` | 调用 PATH 中的 python 执行上述启动器；第 12 行 |
| `requirements-binance-box.txt` | 在线扫描依赖：ccxt、pandas、requests |
| `research/` | 数据下载、事件识别、特征构建、条件比较、时间切分验证与修订评分复核 |
| `data/` | 日线、小时线、合约摘要缓存、样本表和统计产物 |
| `results/` | 两次已有扫描快照，分别为 6 行、8 行；不是持续观察记录 |
| `backup_原始版_20261004/` | 修订前的五个启动与策略文件；本轮未修改 |
| `.workbuddy-ai/memory/` | 既有历史工作记录；阅读时需区分旧结论、后续更正和本轮核验 |

~~当前目录不是 Git 仓库，`git status --short` 返回 not a git repository。未发现测试目录或测试配置；本轮执行了独立的离线检查。~~（2026-10-04 后续核验：当前目录已有 Git 仓库，扩池改动新增 `tests/test_candidate_pool.py`；初次阅读记录不再代表当前状态。）

## 3. 在线筛选链路与当前评分

~~链路：读取配置 → 连接币安现货与合约 → 拉行情与补充数据 → 筛选 USDT 现货成交额前列标的 → 计算量价指标 → 要求存在活跃 USDT 线性永续合约 → 计算合约评分 → 按核心分、合约成交额排序 → 按门槛展示。~~（2026-10-04：合约改为可选，其他主要步骤保留；无合约时保留量价评分。）

权重唯一声明处为 `binance_box_strategy.py:63`，满分 18；计算函数仍分散在在线与研究代码中。~~默认门槛为 6~~（2026-10-04 已调整，见 [候选池改动](CANDIDATE_POOL_CHANGE.md) 第 2 节），辅助分不参与门槛。

| 核心条件 | 分值 | 当前在线判定 |
|---|---:|---|
| 收盘高于 EMA50（50 根 K 线的指数平均价格） | 5 | `close > ema` |
| OI 一日增长 | 4 | `>= 10%` |
| OI 三日增长 | 3 | `>= 20%` |
| 大户持仓多空比 | 3 | `< 1` |
| 收盘突破此前箱体上沿 | 2 | `close > box_high` |
| 最近 24 根成交量超过此前 24 根 | 1 | 默认 1h 时对应 24 小时 |

来源：`binance_box_strategy.py:336`、`:380`。箱体宽、量比、4 小时量增、资金费率及多空比变化等保留展示，不属于上述评分条件。

注意：门槛 6 并不强制趋势成立；合约条件组合也可超过门槛。辅助信息包括市值、成交额/市值、OI/市值、OKX 合约等。入场、止损和止盈参考值由另外的 `signal()` 生成，要求突破、放量、趋势同时满足；候选通过评分不代表这些字段一定有值（第 307 行）。

## 4. 运行入口与环境

本轮确认 `py -3.10` 为 Python 3.10.9；PATH 中的 `python` 也指向同一 Python310 安装目录。已导入并核实：ccxt 4.5.77、pandas 2.3.3、numpy 2.1.2、pyarrow 25.0.1、scikit-learn 1.7.2。无需据旧记忆再安装环境。

以下命令在项目根目录的 PowerShell 中执行。联网扫描命令仅作运行说明，本轮未执行。

```powershell
# 单轮扫描：终端表格，并将完整排名保存到 results
py -3.10 -B binance_box_strategy.py --once

# 持续扫描：每轮结束后默认等待 300 秒
py -3.10 -B binance_box_strategy.py

# 精简入口：单轮扫描，打印通过门槛的 JSON
py -3.10 -B run_chuanmu.py
```

两个入口的评分函数相同，但输出行为不同：双击启动器走精简入口，不执行 `run_once()`，所以不会保存扫描 CSV、不会显示表格，也不解析 `--once`。连接过程另有 stdout 文本，所以完整标准输出不能直接当作纯 JSON 解析。来源：`run_chuanmu.py:9`、`川沐十倍币筛选.py:23`、`binance_box_strategy.py:239`、`:732`。

~~常用环境变量：`BINANCE_PROXY`、`MAX_SYMBOLS`（50）、`MIN_QUOTE_VOLUME`（5,000,000 USDT）、`MIN_SCORE`（6）、`USE_COINGECKO`（true）、`POLL_SECONDS`（300）、`CSV_DIR`。~~（2026-10-04：筛选默认值已变更，当前值及新增开关统一见 [候选池改动](CANDIDATE_POOL_CHANGE.md) 第 2 节。）PowerShell 设置语法为 `$env:MIN_SCORE = '4'`；旧报告中的 `export` 不是 PowerShell 命令。代理端口依赖用户当前配置，本轮没有验证历史端口。

## 5. 研究流程与已复核结果

研究主线与辅助脚本：

1. `01_fetch_1d.py` 枚举和下载日线；`00_fetch.py` 提供日线、小时线、metrics 和 funding 批量下载命令；`probe_s3.py` 用于数据源探查。
2. `02_detect_pumps.py` 从日线事后识别低点、峰值与启动日，输出 `pumps.json`。
3. `03_build_features.py prep` 生成事件及平静期对照的小时线下载任务；`run` 用本地主策略计算特征，输出 `features.csv`。
4. `04_compare.py` 比较量价条件；`05_deriv_features.py` 构建合约特征；`05b_patch_ls_top_chg.py` 从缓存补大户持仓比变化；`06_deriv_compare.py` 比较合约条件。
5. `07_oos_validate.py` 按 2025-01-01 切分训练和测试；`08_audit_hindsight.py` 比较触发前涨幅、检查条件含义；`09_backtest_revised.py` 在历史特征上重算修订评分及阈值表现。

本轮数据清点：日线 642 个文件，约 41.9 MiB；小时线 3,140 个文件，约 166.2 MiB。`features.csv` 为 1,421 行，有 19 条重复的币种/日期；`features_deriv.csv` 为 733 行，有 5 条重复的币种/日期。第 09 号脚本在合并前去重，得到以下结果。

| 离线核验项 | 本轮结果 | 含义 |
|---|---:|---|
| 去重合并样本 | 1,402 | 爆拉 822、对照 580；评估日期为 2022-06-07 至 2026-08-19 |
| 三个合约字段完整的样本 | 711 | 约半数样本可按完整合约口径评分，不能将其余样本等同于无信号 |
| 核心分至少 6 的样本 | 603 | 其中 437 条为爆拉案例 |
| 样本内精确率 / 召回率 | 72.47% / 53.16% | 选中样本中爆拉占比 / 已知爆拉中被选中占比；不是实盘胜率 |
| 重算与保存分数一致性 | 1,402 行一致 | K 线分、合约分、总分均与 `data/backtest_scores.csv` 一致 |

研究样本的爆拉占比为 58.63%，因为对照特意从有过爆拉的币种中抽取。既有报告已披露这种选择偏差，见 [策略前视与马后炮审计](策略前视与马后炮审计.md) 第 5 节、[策略修订与反向回测报告](策略修订与反向回测报告.md) 第 4 节。没有全市场逐日验证、交易成本和退出规则检验，不能从这些数字推出可交易收益。

报告阅读顺序： [项目改进方案](项目改进方案.md) → [爆拉案例复盘报告](爆拉案例复盘报告.md) → [策略前视与马后炮审计](策略前视与马后炮审计.md) → [策略修订与反向回测报告](策略修订与反向回测报告.md)。它们保留了逐步修订的历史，旧描述不一定代表当前代码。

## 6. 后续完善前必须注意的差异

以下是本轮已取证的差异或明确待验证项，不表示已经修复。

### 6.1 在线与研究共享权重，但尚未共享完整判定规则

- 临界值：在线用 `>=`，研究用 `>`。输入 OI1日=0.10、OI3日=0.20、多空比=1.0，在线合约分为 7，研究为 0。来源：`binance_box_strategy.py:396`、`research/09_backtest_revised.py:82`；第 07 号脚本也使用严格大于。
- 缺失值：在线逐项评分；第 09 号脚本只要三个字段中缺一项，整组变为缺失、计算总分时填 0。输入 0.15、缺失、1.2，在线合约分为 4，研究结果为缺失。来源：`binance_box_strategy.py:380`、`research/09_backtest_revised.py:73`。
- 当前 1,402 条样本中，14 条合约字段部分缺失；相同输入经两套函数计算，有 3 条合约分不同，其中 1 条影响门槛 6 的入选结果。本轮只比较相同特征输入，未模拟历史在线接口。

### 6.2 多空比回退会混用不同统计对象

大户持仓接口报错时，`fetch_deriv_context()` 将全市场账户比写入 `ls_top`。虽然 `ls_source` 标记了回退来源，但调用方仍按大户持仓比 `<1` 计分，表格与命中条件也按大户显示。来源：`binance_box_strategy.py:475`、`:600`。后续应明确回退值仅展示还是允许参与独立规则，避免来源标记存在但判定仍混用。

### 6.3 合约数据覆盖与结果标签有关

`research/05_deriv_features.py:151` 只为 `gain >= 2.0` 的爆拉事件构建合约特征，即峰值/低点至少三倍；K 线样本则从两倍起收录。按第 09 号脚本的分档，本轮确认两至三倍组 375 条中，完整合约数据为 0 条；三至五倍组 329 条中为 217 条。

所以全样本、合约完整子集、不同涨幅组不能直接当成等价人群比较。已有 AUC（排序区分能力）提升应保留其子集限定，不足以证明全市场增益；缺失不仅可能来自接口失败，也来自研究采样规则。

### 6.4 从头重建会改变被称为“原规则”的基线

`03_build_features.py:219` 调用当前 `base.box_score()`，并在第 273 行覆盖 `features.csv`；`09_backtest_revised.py:197` 却将该文件的 `box_score` 列称作原规则评分。现存数据仍保留旧分数，本轮与新 K 线分比较只有 142/1,402 行相同。重新运行构建后，新旧规则比较的旧基线会被替换。完善前应先明确版本化的原规则函数、特征列与数据产物，不能直接重跑整条流水线覆盖现有结果。

### 6.5 评估时点尚未显式统一

历史小时线裁剪到 `pre_day` 的 23:00 开盘，再使用倒数第二根，因此完整数据下量价信号取 22:00 开盘的那根；合约摘要取该日最后有效观测。在线量价也取倒数第二根，而合约函数各自读取接口最新记录。来源：`03_build_features.py:179`、`:192`、`05_deriv_features.py:86`、`binance_box_strategy.py:443`。

这是已确认的时间口径差异；是否导致某个具体策略时点使用了当时不可得数据，需统一决策时间后再验证。本轮不将旧报告“没有前视偏差”的静态判断扩展成对整条研究与在线链路的保证。

### 6.6 配置和扫描范围存在容易误解的地方

- `REQUIRE_OKX` 被读取但未用于筛选判断，默认 true 不代表强制要求 OKX 合约。来源：`binance_box_strategy.py:113`、`:204`、`:585`。
- ~~扫描先按现货成交额截取前 50，再要求币安活跃合约，因此最终数量可能更少；不等于全市场或小市值全覆盖。来源：第 647、549 行。~~（2026-10-04 已放宽，保留的上限及金额门槛见 [候选池改动](CANDIDATE_POOL_CHANGE.md) 第 2 节。）
- `BINANCE_TIMEFRAME` 可修改，但 4h/24h 量增仍按固定根数计算；非 1h 时，文字与时间含义会不同。来源：第 181、365 行。
- 研究依赖 numpy、pyarrow、scikit-learn 未完整列入当前依赖文件；本机已具备，但不能据此保证新环境可复现。
- 第 07 号脚本直接内连接且不先去重，第 09 号脚本去重后左连接；二者的样本集合、缺失处理不同，报告数字不能不加区分地拼接。来源：`07_oos_validate.py:45`、`09_backtest_revised.py:53`。
- **2026-10-04 后续评价新增发现：** 第 07 号逻辑回归在划分训练与测试之前执行 `X.fillna(X.median())`（`research/07_oos_validate.py:145`），训练缺失值因而使用了包含测试期的信息，属于预处理的信息泄漏。本次只读合并为 748 行；全样本与训练期中位数有 14 个特征不同。该发现限定于这个模型评估路径，不等于第 09 号评分全部用了未来标签；其性能影响须修复后重测。
- 历史扫描 CSV 没有独立的逐行信号时间、策略版本与配置快照；重建决策现场仍缺信息。来源：`binance_box_strategy.py:623`、`:723`。

### 6.7 样本已放宽、“19 / 几个”的出处核对（2026-10-05）

用户质疑：ds4.1f 指出历史样本早已纳入翻倍行情，为何此前评价听起来只剩 19 个币甚至几个？本节是这次数量口径核对的唯一记录。

**核对结论：历史事件库已经从两倍价格行情起收录，ds4.1f 截图中的这点正确。十倍从来不是当前事件库的硬性下限。此前评价反复突出十倍子组，没有同时交代全部多倍事件的覆盖，评价侧重点有偏差；撤回由此引出的“整个项目只研究十倍、总共只剩十几个或几个币”的推论。**

已回查本会话原话：“现有 24 个十倍以上案例，默认门槛选中 9 个；提高门槛后，只剩 4 个、再到 2 个。”其计算限定为十倍以上子组，评分门槛依次是 6、8、10；“默认 6”仅描述当时状态。没有在已检索到的原话中找到“整个项目只有 19 个币”；文件中可复现的 19 是 5 分门槛覆盖的十倍事件数。不能把文件中相同的数字当成另一段未找到原话的确切出处。

依据与分母：

- `research/02_detect_pumps.py:28` 的 `MIN_GAIN=1.0` 表示上涨 100%，即价格两倍；代码同时要求 30 日收盘涨幅达到这个阈值并定位局部峰值，事件标签使用峰日高价/回看低价。因此库中不是所有形式的翻倍行情，也不是从实时信号价出发的交易收益。
- `data/pumps.json` 有 841 条事件、396 个不同币种，价格倍数中位数 3.0983。`data/features.csv` 的正例也有 841 行。
- `research/09_backtest_revised.py:53` 按币种、日期、标签去重后，保存评分表有 822 条正例和 580 条对照。少掉的 19 行是被既有去重键合并的记录，不能直接认定为 19 个独立机会消失。
- 当前在线默认值是 4 分、最多扫描 200 个、最低日成交额 100 万 USDT、合约可选。当前配置与变更依据仍以 [CANDIDATE_POOL_CHANGE.md](CANDIDATE_POOL_CHANGE.md) 为准；历史两倍标签与这次在线扩池是两项不同改变。

固定读取现存 `data/backtest_scores.csv`，下面每格均为**历史正例事件条数**，不是当日候选数或独立币种数：

| 事后价格倍数下限 | 该组事件 / 不同币种 | ≥4分 | ≥5分 | ≥6分 | ≥8分 | ≥10分 |
|---|---:|---:|---:|---:|---:|---:|
| 2倍 | 822 / 396 | 698 | 692 | 437 | 142 | 57 |
| 2.5倍 | 673 / 367 | 560 | 554 | 352 | 127 | 57 |
| 3倍 | 447 / 295 | 367 | 361 | 246 | 111 | 57 |
| 5倍 | 118 / 104 | 98 | 95 | 63 | 26 | 12 |
| 10倍 | 24 / 23 | 20 | 19 | 9 | 4 | 2 |

各行是累计子组，不能相加。4 分在全部历史多倍正例上覆盖 698/822；十倍子组只是其中的 20/24。所有标签均为事后低点到高点，且部分低倍组缺少合约数据；这张表不能决定哪个倍数最赚钱，也不能证明当前线上只会选出多少币。

两份旧扫描 CSV 分别保存 6、8 行排名，按 6 分过滤分别剩 4、1 行；它们没有完整配置快照，不能当成放宽后的新扫描结果。未执行新线上扫描，当前实时结果数未知。

复跑（项目根目录 PowerShell；只读，不联网、不覆盖任何数据）：

```powershell
@'
import json
from pathlib import Path
import pandas as pd
p = pd.DataFrame(json.loads(Path('data/pumps.json').read_text(encoding='utf-8')))
f = pd.read_csv('data/features.csv')
b = pd.read_csv('data/backtest_scores.csv')
print('raw_events / symbols / median_multiple:', len(p), p.symbol.nunique(), (p.gain + 1).median())
print('feature_positives / rows_removed_by_key:', int(f.label.eq(1).sum()), int(f.duplicated(['symbol', 'pre_day', 'label']).sum()))
print('saved_positive / control:', int(b.label.eq(1).sum()), int(b.label.eq(0).sum()))
for multiple in [2, 2.5, 3, 5, 10]:
    sub = b[b.label.eq(1) & b.gain.ge(multiple - 1)]
    print('multiple / events / symbols / scores_4_5_6_8_10:', multiple, len(sub), sub.symbol.nunique(), [int(sub.score_full.ge(s).sum()) for s in [4, 5, 6, 8, 10]])
for path in sorted(Path('results').glob('candidates-*.csv')):
    scan = pd.read_csv(path)
    print('snapshot / rows / at_score_6:', path.name, len(scan), int(scan.score.ge(6).sum()))
'@ | py -3.10 -B -
```

## 7. 本轮自检与复跑方法

实测通过：15 个非备份 Python 文件的语法解析；Python 3.10 环境导入；本地样本重算；保存分数比较；临界值和缺失值差异复现。未运行写入产物的研究 `main()`，不覆盖数据。

下列命令只读本地文件，不联网；`-B` 禁止生成字节码缓存。在项目根目录执行：

```powershell
@'
import ast, runpy
from pathlib import Path
import numpy as np
import pandas as pd
import binance_box_strategy as base
files = list(Path('.').glob('*.py')) + list(Path('research').glob('*.py'))
for path in files:
    ast.parse(path.read_text(encoding='utf-8-sig'), filename=str(path))
bt = runpy.run_path('research/09_backtest_revised.py', run_name='context_check')
m = bt['add_scores'](bt['load']())
saved = pd.read_csv('data/backtest_scores.csv')
for key in ['score_kline', 'score_deriv', 'score_full']:
    assert np.allclose(m[key], saved[key], equal_nan=True), key
chosen = m[m.score_full >= base.Config().min_score]
online = np.array([base.deriv_score(r.oi_chg_1d, r.oi_chg_3d, r.ls_top)[0] for r in m.itertuples()])
print('syntax files:', len(files), 'samples:', len(m), 'selected:', len(chosen))
print('precision:', chosen.label.mean(), 'recall:', chosen.label.sum() / m.label.sum())
print('score differences:', int((online != m.score_deriv.fillna(0)).sum()))
print('selection differences:', int(((m.score_kline + online >= 6) != (m.score_full >= 6)).sum()))
for values in [(0.10, 0.20, 1.0), (0.15, np.nan, 1.2)]:
    frame = pd.DataFrame([values], columns=['oi_chg_1d', 'oi_chg_3d', 'ls_top'])
    print('probe:', values, 'online:', base.deriv_score(*values)[0], 'research:', bt['score_deriv'](frame)[0])
'@ | py -3.10 -B -
```

## 8. 后续建议与当前边界

建议下一轮先统一评分判定、缺失值和多空比来源，并固化旧规则基线，再统一启动输出和运行记录。理由是这些问题已有代码证据，且会影响入选结果和研究可复现性。

随后再做逐日全市场验证、冻结规则后的新时期验证和观察池后续表现跟踪。事件数据、催化剂和界面可以继续完善，但目前不宜通过新增权重掩盖上述差异。具体实施范围由下一轮需求决定，本轮没有启动任何这些改造。

## 9. 更新后接续核对（2026-10-05）

用户要求：项目已更新，先阅读。已依次阅读 TODO、DONE、KNOWLEDGE，核对主程序改动、新研究脚本、相关报告与扫描产物。只做离线检查及文档纠错，没有改策略代码、调整参数、启动扫描或覆盖研究产物。本节保存核对细节，知识索引见 KNOWLEDGE.md 第 2.5 节，未完成工作统一进 TODO.md。

### 9.1 当前实现与保存结果

- 当前 Config：min_score=5；成交额范围 500,000–50,000,000 USDT；max_market_cap=2,000,000,000；max_symbols=400；require_futures=False。市值缺失不拦截。它们替代本文前面阅读现场的旧默认值，环境变量仍可覆盖。
- 新增 research/11_hit_rate_current.py 与 12_optimize_gate.py；08 号脚本新增从本地日线构建市场排名的方法。排名使用本地可读取数据，是否覆盖当时全部市场未由本轮确认。
- 20261005-134652-843815 扫描 CSV 保存 217 行排名，其中 107 行达到 5 分、78 行达到 6 分。较早的 112232 快照保存 211 行，其中对应 171、121 行。不同时间不能直接归因于参数改变；本轮只核对保存文件，未实时联网。
- 6 项现有离线测试通过，66 条术语查重通过；主程序、research、tools、tests 下 Python 文件语法解析通过。测试通过不代表策略假设或全部研究口径成立。

### 9.2 新报告的“19 个十倍事件”有另一处来源错误

第 6.7 节解释的是旧评分表的 5 分子组；**本次新报告中的 19 并不是那个口径**。新脚本 `research/11_hit_rate_current.py` 的 load() 使用 `[2,3,5,10,inf]` 直接切分 gain，但从事件检测脚本到 features_audit.csv，gain 均为价格倍数减 1。本次按相同数据键确认，新旧 gain 一致。

| 检查项 | 当前脚本结果 | 按价格倍数解释应得的结果 |
|---|---:|---:|
| 822 条正例中被分入涨幅组 | 447 | 822 |
| 没有分入任何涨幅组 | 375 | 0 |
| 标为十倍以上的事件 | 19（实际 gain≥10，即价格≥11倍） | 24（gain≥9） |
| 该组经过现有过滤且达到 5 分 | 15/19 | 17/24 |
| 该组经过现有过滤且达到 6 分 | 9/19 | 9/24 |
| 该组通过现有过滤 | 19/19 | 22/24 |

> **2026-10-05 代码已修**：`research/11_hit_rate_current.py` 的 `TIER_BINS` 由 `[2,3,5,10]`
> 改为 `[1,2,4,9]`（与 `09_backtest_revised.py` 一致）；`02_detect_pumps.py` 的 `TIERS`
> 标签同步更正。重跑后 10x+ 为 **24** 条、命中 **17** 条，与右列一致；375 条正例不再掉出分档。
> 相关文档（`REASONING.md`、`CANDIDATE_POOL_CHANGE.md`、`爆拉币命中率与优化空间.md`、`KNOWLEDGE.md`）已同步。

五条未进入新报告十倍组的记录涉及 XNOUSDT、NFPUSDT、SUPERUSDT、STOUSDT、LOOMUSDT；第 9.4 节复跑命令输出各条记录的完整日期、涨幅和身份。

全部事件的 5 分覆盖 616/822、6 分覆盖 395/822 可以复现，不受此次分组错误影响；它们仍是历史事件统计，不是真实交易胜率。历史市值已知行数为 0，因此这些数字没有验证市值上限的历史效果。成交额过滤已参与；按正确十倍定义，存在被过滤的事件，不能继续引用“十倍零漏报”。

### 9.3 不能当成完成成果的说明

- 在线 deriv_score 使用 >= 并逐项处理缺失，新 11 号脚本使用 >，三个字段不全则整组不计分。同一批输入仍有 3 条合约分不同。共享 WEIGHTS 没有消除这些差异。
- 07 号脚本仍在训练/测试划分前填补全体样本中位数。不能由 08 号局部排名修正推导“整个项目无前视问题”。已有测试期又被用来比较阈值，两个指标一起变好也不能证明没有过拟合。
- 新测试 test_min_score_is_a_deliberate_choice_not_a_math_floor 固定了 min_score≥5 的约束；这是策略选择，不是程序正确性的必然条件。5 分不强制趋势成立，合约分组合也可以达到；4 分可由单日 OI 项独立达到。
- 当前候选过滤聚焦较小标的，但“市值大所以数学上不可能十倍”“几个案例无信号所以所有量价方法已到头”均超出证据。原目标仍是反复捕捉多倍行情，十倍只是分档；洗盘及较低倍数研究仍按 STRATEGY_REVIEW.md 第 8 节。
- TODO 中直接重定义 pre_day 并从头覆盖重跑的步骤，与保留旧基线的要求冲突；现有 02 号检测脚本也没有 OI 输入。此方案只能作为独立实验候选，不能按旧步骤直接执行。

### 9.4 只读复跑

项目根目录 PowerShell：

```powershell
@'
import runpy
import numpy as np
import binance_box_strategy as base
n = runpy.run_path('research/11_hit_rate_current.py', run_name='reading_check')
m = n['score_live'](n['apply_gates'](n['load']()))
p = m[m.label.eq(1)]
print('positive / missing_tier:', len(p), int(p.tier.isna().sum()))
for floor in [9, 10]:
    g = p[p.gain.ge(floor)]
    print('gain_floor / n / gate / score5 / score6:', floor, len(g), int(g.pass_gate.sum()), int((g.pass_gate & g.score.ge(5)).sum()), int((g.pass_gate & g.score.ge(6)).sum()))
print(p.loc[p.gain.ge(9) & p.gain.lt(10), ['symbol', 'pre_day', 'gain']].to_string(index=False))
print('all_score5 / score6:', int((p.pass_gate & p.score.ge(5)).sum()), int((p.pass_gate & p.score.ge(6)).sum()))
online = np.array([base.deriv_score(r.oi_chg_1d, r.oi_chg_3d, r.ls_top)[0] for r in m.itertuples()])
print('derivative_score_differences:', int((online != m.s_deriv.fillna(0)).sum()))
print('historical_market_caps_known:', int(m.gate_cap_known.sum()))
'@ | py -3.10 -B -
py -3.10 -B -m unittest discover -s tests -v
py -3.10 -B tools/glossary.py check
```

## 10. 请求治理接入（2026-10-05，P0–P2）

用户批准按 P0（请求经济）→ P1（限流退让）→ P2（通路拆分+诚实降级）连续实施，当日完成。改前备份：`backup_20261005_request_governor/`。

- **分域连接与降级**：`build_exchange` 按域收窄 `fetchMarkets`（现货只加载现货、合约只加载 U 本位线性；ccxt 默认两个域都请求，合约被封会连带现货初始化失败）。`connect_exchanges` 现货、合约分别连接，合约失败降级为 `None`，扫描按仅现货继续。
- **限流退让**：429/418 按「域+出口」登记暂停（状态文件 `results/rate_limit_state.json`，跨重启、同机共享；损坏按 fail-open）；恢复时间解析自响应体 `banned until <毫秒>` 或 `Retry-After`，解析不到按 15 分钟保守退让并在日志注明；到期后只发一次恢复探测，失败进入 5 分钟冷却。
- **请求经济**：1h 粒度的 OI/多空比按小时对齐缓存（`DERIV_CACHE=false` 可关，只缓存状态 ok 的结果）；市值按天缓存（`results/coingecko_caps.json`）；OI 快照与资金费率（不计分字段）推迟到第二遍，只对过门槛或展示行补取，其余行标 `extras_deferred`。
- **诚实降级**：扫描 CSV 新增 `deriv_status`（ok/partial/failed/paused/no_futures）、`deriv_as_of`（数据观测时间）、`extras_deferred` 列；表格新增「合约数据」列；多空比回退口径一律标 `partial`（不冒充大户持仓口径，论证见 REASONING.md C4）。
- **新增环境变量**：`DERIV_CACHE`（默认 true）、`RATE_LIMIT_STATE_FILE`（默认 `<csv_dir>/rate_limit_state.json`）。
- **验收**：`tests/test_request_governor.py` 12 项 + `tests/test_candidate_pool.py` 6 项全部通过，覆盖封禁解析、跨重启暂停、单次探测、降级出池、小时缓存、展示行延后取数、fetchMarkets 分域、市值缓存。请求量为代码结构估算，实测单轮耗时/请求数留待观察计划第 8 节的试运行测量。
