# ready_for_review · glm 包2 r2（2026-10-14）

2026-10-14 主代理验收批注：完整验收未通过；本页作为r2作者交接快照保留。ds r2已交，不能继续沿用未到依赖判断。实际范围、时间与标签错误及可保留结果唯一见 `reports/a1_stage2_review/r2/summary.md`。

交 ds 独立复验 / 主代理验收。旧交付（../detail、../summary.md、../scripts）原样保留，本目录为独立版本。

## 输入范围

- `data/archive_raw/spot/klines/<SYM>/1d|1h/*.zip`（月度归档，主区间 2022-01-01~2026-09-30 UTC）
- 身份：spot 顶层目录名恰以 `USDT` 结尾 → 754 个；可观察 711（与包1 destinations_2h 的 711 标的双向零差，
  见 `detail/p1_identity_symbols.csv`）
- 规格：`run_spec.json`（运行前写定）；定义与手算预期：`event_definition.md`

## 代码与配置摘要

| 文件 | 作用 | 产物 |
|---|---|---|
| `scripts/lib_archive.py` | 归档读取库（逐文件毫秒/微秒统一、区间过滤） | — |
| `scripts/m0_inventory.py` | 四态清单 + run_spec | `detail/m0_universe.csv`、`run_spec.json` |
| `scripts/m2_handcalc.py` | 小样本手算预期（BTC/SEI/BIO/TUSD/FTT） | 控制台，全部 ✅ |
| `scripts/m2_natural_paths.py` | 两套参数自然路径（滚动波谷，无旧波谷复用） | `detail/events_diag_30.csv`(10,696行)、`events_fixed_50.csv`(5,094行)、`m2_natural_summary.json` |
| `scripts/m2_fixed_outcomes.py` | 小时点固定 7/30/90 天后果 | `detail/fixed_outcomes_hourly.parquet`(15,971,987行)、`fixed_outcomes_meta.json` |
| `scripts/m2_consistency.py` | 自然路径 vs 底表同窗核验 | `detail/m2_consistency_check.csv`、`m2_consistency_result.json` |
| （分析型，无独立脚本） | 旧841匹配/归因 | `detail/legacy841_match_r2.csv`、`legacy841_miss_attribution.csv` |

## 输出行数与校验

- events_diag_30 = 10,696；events_fixed_50 = 5,094；两表 `(symbol,trough_date)` 重复=0（硬断言）
- 底表 15,971,987 行 / 711 币 / 0 读取错误；`complete_*` 与 `gap_hours_*` 随行
- 一致性：9,135 个完整窗事件，小时 vs 日线剩余空间相对差中位/P99/最大 = 0.00000%
- 手算五例（SEI/BIO/IOTX/TUSD/BTC）全部符合 `event_definition.md` 预期

## 本次变化（相对旧包2）

1. 数据源 `data/1d`(642文件,2022-06起) → 归档 1d+1h（711标的，2022-01~2026-09），与包1对齐。
2. 三种对象分开：自然路径（30%/50% 诊断对）与固定后果底表分离；旧 10,464 行表**不再当契约关注重入**。
3. 修复：61根冒充60日、缺口恢复首根跳过、新段复用旧波谷、启动信号次日才可知（保存信号可用时刻与参考后剩余空间）。
4. 过程中抓出并修复 3 个新实现 bug（币安2025微秒时间戳、fwd_agg 前向窗口错位 hrs、单位阈值）——
   均由"手算先行/一致性核验"流程暴露，证据在会话与脚本注释。
5. IOTX 16,360x、BIO 首日等极端案例按「路径描述 vs 剩余空间 vs 同日未知」分列，不再混算。

## 已知限制

见 `summary.md` 末节。最重要：**实际名单关注成本未做**（等 ds 包1 r2 的实际入选名单）；排序曲线同样等 M1。
