# glm 复验 ds r3 · 互审交付（ready_for_review）

**复验方**：glm5.3f　**被复验**：`reports/a1_stage2_ds/r3/`（ds4.1f）
**日期**：2026-10-17　**依据**：`tasks/glm_r3_followup_20261014.md`（「ds r3 到达后，独立验排序不读未来、缺资料不产生新提醒及全账恒等式」）
**方式**：不跑 ds 的自检脚本；恒等式从 ds 产物文件独立复算，行情数字从原始合约/现货 ZIP 独立重算。不联网、未改任何 r2/r3 交付。

---

## 0. 一句话结论

ds r3 的 M0/M1/B6 全部复验通过；ds 对 glm r3 的复验（其脚本 A~G 段）也全部通过——**两包 r3 交叉合格，可以请主代理收口进 M3**。互审各自咬出一个新发现：ds 日线扫描比我多发现 1 条重复（ADAUSDT）；ds 新增 G 段指出「同根 K 线盲区」的口径限制（非错误，需登记）。

## 1. 我对 ds r3 的复验（逐项）

| 项 | 方法 | 结果 |
|---|---|---|
| **V1 排序不读未来** | 代码核 `_futures_reference_index`（末根 open_time=h−1h，[h-24h,h) 已闭合，`replay_stage2_r3.py:291`）；随机 40 (币,轮) 从合约 ZIP 独立重算 fqv24；2024-06-01 轮 top10 逐位对照 | 40/40 一致、**top10 全部逐位相同**（BTC 13,295,783,600.0905 等）✅ |
| **V2 缺资料不产生新提醒** | 代码核 `INTERRUPT_NONQUAL` 确认扩到 4 类；核其合成反例（volume_window/short_history：r2=2 假新、r3=1）；真实数据放宽扫描 2h 全量 | 反例成立；真实 8 案例零违规（1 例 ADAUSDT 疑似，逐轮手工复算确认事件实际开始于 03-12，距重入选 13.7 天≥7 天，**new=1 合法**）✅ |
| **V3 全账恒等式** | 独立读 ledger/destinations 逐轮重分组复算（不跑 selfcheck 脚本） | 三时钟 A（底账=不可观察+可观察）、B（可观察=7 环和）、universe 恒 713、dest 行数=可观察和、零信号轮 12/8/4 保留，**全绿** ✅ |
| **V4 新事件数与归因** | 独立求和 candidates 的 is_new_event | 53,790 / 53,771 / 53,426 全对上；新增两币 2h 贡献 22 个新事件，与 ds 归因一致 ✅ |
| **V5 M0 身份** | 与 glm 独立 M0（`r3/detail/m0_verify_summary.json`）对照 | 713=713 集合相等 ✅（见 §3 ADA 发现） |
| **V6 B6 抽样** | 随机 12 个已完成文件独立核字节+ZIP 可读 | 12/12 ✅ |
| 关注成本/前 N 曲线 | `../r3/list_cost.md` 已基于 ds r3 名单算毕；本次核对 new_events 等口径一致 | ✅ 无需重算 |

脚本：`verify_ds_r3.py`（第一轮，含一次我方路径 bug：合约路径误写 `futures/um/klines`，实际为 `futures/klines`，已修正重跑 `verify_ds_r3_round2.py`）；结果：`verify_ds_r3_summary.json`、`verify_ds_r3_round2.json`。

## 2. ds 对 glm r3 的复验（收编其结果）

ds 的 `review_glm_r3.py` 首次运行中断于 B 段（边跑边修所致）；本次其修好版完整跑通（`detail/glm_r3_review/`，2026-10-17 17:33–17:39）：

| 段 | 内容 | 结果 |
|---|---|---|
| A | 713 币日线重复全扫 | **4 条重复**：ADA/AXS/UMA/USDP 各 1，冲突 0 |
| B | 事件参考时刻 | 15,797 ok 全部严格 =信号+1h；5 条缺失逐一回原始 ZIP 属实 ✅ |
| C | 底表加大抽样（119 币/3,980 行）独立重算 | end_close/max_high/min_low/gap 全 0 mismatch（最大相对误差 0.0），tail 行 complete 全 False ✅ |
| D | 上市标签 27 例反例 | 全部修复（still_flagged=0，全表 131 与声称一致）✅ |
| E | 回撤 | 两表 15,802 事件 0 正值；合成单边=0 ✅ |
| F | 底表行数 vs M0 唯一小时数 | 15,976,661 = 15,976,661 ✅ |
| G | **新攻击面：同根 K 线盲区** | 10,695 事件中 54 个 no_pullback_intraday=True，其中 44 个（81%）同根内 high→low 实际跌超 10%（极端 FORTH −70%）⚠️ 见 §4 |

**glm r3 的 M2（原 R5/R6/R7）经 ds 复验全部通过**。ds r3 ready_for_review §8「glm M2 未过」判定针对的是 glm r2，已被 glm r3 返修取代。

## 3. 互审新发现（需主代理入账）

1. **ADAUSDT 日线重复 1 条**（ds 发现，glm 日线扫描漏报；AXS/UMA/USDP 双方一致）。日线重复共 **4 条、0 冲突**。对 1h 主线计算无影响（1h 重复此前双方一致为 2,234 行、0 冲突，读取层已按 open_time 去重）；日线若用于月度清单/上市判断需同规则去重。
2. **G 段口径限制（非错误）**：1h K 线内部 open/high/low/close 先后不可知，故 glm 的盘中回撤是**下界**、`no_pullback_intraday=True` 不能读成「盘中没回踩」。方向保守（不夸大回撤），但读数需带此限制。建议登记进 `REASONING.md` 与术语表（`intraday_drawdown` 条目加 caution），并在 M3 使用该字段时按「至少跌了这么多」解读。
3. **ds 审查脚本曾中断 + 边跑边修**（如实记录）：`review_glm_r3.py` 首次中断于 B 段无产物，修好版已完整跑通；我方代跑旧版在 C 段崩于 KeyError（新旧版本竞态），未采纳代跑结果，以其修好版产物为准。

## 4. 结论与请示

- **两包 r3 交叉合格**：ds r3（M0/M1/B6）与 glm r3（M2）互相复验全部通过，无未决阻塞。
- 请主代理：① 收口两包 r3；② 入账 §3 两条新发现（ADA 日线重复、同根盲区口径限制）；③ 批准进入 M3。
- 复跑命令：

```powershell
# glm 复验 ds r3（本轮，约 2 分钟）
py -3.10 -B -u reports/a1_stage2_glm53f/ds_r3_review/verify_ds_r3_round2.py
# ds 复验 glm r3（ds 已跑完；重跑约 60 分钟）
py -3.10 -B -u reports/a1_stage2_ds/r3/scripts/review_glm_r3.py
```
