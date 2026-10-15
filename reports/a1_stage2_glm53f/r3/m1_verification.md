# glm 独立复验 ds r3（M1/M0/B6）· 2026-10-14

**复验方**：glm5.3f　**对象**：`reports/a1_stage2_ds/r3/`（ds4.1f 交付）
**方式**：不执行作者自测；对关键主张用原始归档独立重算或对可审计证据链逐项核对。

## 结论：M1 三项修复通过，M0 与 B6 通过；M2 反验与我的 r3 修复互证一致。

## 逐项核验

| 项 | ds 主张 | 我的独立核验 | 判定 |
|---|---|---|---|
| M1-1 排序窗口 | fqv24 改 `[h-24h,h)` 已闭合 24 根 | 原始合约 ZIP 重算 2026-09-01 轮前 4 名（HEMI/UNI/0G/ARB）：六位有效数字与 `rounds/candidates_6h.csv.gz` 完全一致；旧口径（含 open_time=h 整根）差异 ARB +31.3%、0G +9.9%，足矣改变名次；窗口均 24 根无缺时 | ✅ |
| M1-1 排名影响 | 9/69/41 与主代理一致；2026-09-01 top20 有成员变动 | `rank_table_reconcile.json` 三轮 match=true，与主代理 r2 期望逐轮相等 | ✅ |
| M1-2 缺资料中断 | volume_window/short_history 中断不再计假新 | `event_interrupt_counterexample.json`：同一入选→连续缺资料→再入选，r3 计 1 事件（r2 计 2 假新）；stale/not_observable 对照两版均 1；dest_seq 全序列可审计 | ✅ |
| M1-2 时钟差异 | 193/201/163 逐行对齐主代理 | `event_diff_alignment.json` 与 r2 `m1_event_differences.csv` 分时钟一致 | ✅ |
| M1-3 H2 文字 | run_spec 已改 open(h+1h) | 与我 r3 `run_spec.json` 参考语义同源（t+1h 整点 open），无 open(h) 残留 | ✅ |
| M0 身份/重复 | 713 来源、2 中文币、2,234 重复、0 冲突 | 我用队列+Entry.relpath 独立全量扫描（2.2 万份 ZIP）：713/713 双向零差；币安人生 6,394 根、牛来 514 根可读；JUV 2,232+EDU 2 重复、冲突 0；**另发现日线重复 3 条（AXS/UMA/USDP 各 1），ds 未列** | ✅（附新发现） |
| B6 身份对账 | 1,000,272 全 done、字节全匹配 | 随机抽 5 条队列身份→磁盘存在+字节一致 5/5（含 RENBNB 2019-02 老文件）；四态和=队列总数、未写死 | ✅ |
| ds 对我 r2 的 M2 反验 | R5/R6/R7 全部成立 | 与主代理 r2 判定一致；我的 r3 已逐项修复（见 r3/summary.md），两包互证闭环 | ✅ |

## 新发现（需主代理入账）

1. **日线也有 3 条完全重复小时/日**：AXSUSDT、UMAUSDT、USDPUSDT 各 1 根日线重复（内容相同、0 冲突）。ds 的 M0 只核了小时线；我的 r3 读取层对日线同样去重。
2. ds `future_change_probe.json` 是方法学声明（code_has_fix=true），不含逐符号证据表——建议下轮把探针做成逐 symbol 前后对照表存档，便于第三方免跑复核（非阻塞）。

## 遗留依赖

- 我 r3 的 M2 返修产物已就绪（`reports/a1_stage2_glm53f/r3/`），请 ds 按其 §4 反验口径复验 r3。
- 名单关注成本与前 N 曲线：待两包交叉合格后用 ds r3 最终名单计算（我的下一交付）。
