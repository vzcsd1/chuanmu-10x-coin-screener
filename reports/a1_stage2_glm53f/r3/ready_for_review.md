# glm r3 交 ds 独立复验：ready_for_review

**交付方**：glm5.3f　**复验方**：ds4.1f（按你的 `m2_recheck_independent.py` 口径）　**日期**：2026-10-14
**目录**：`reports/a1_stage2_glm53f/r3/`　**保留**：r2 与原始库零改动

---

## 0. 一句话

按共用 r3 任务书 §4 完成 M2 返修：参考严格 t+1h、缺口正计数、期末不填补、
回撤按时间先后与事件边界、上市标签用可证最早观测、旧 841 与 BIO 附页固化；
一致性检查自带原始解析器（与你我两边不共用参考价）。

---

## 1. 输入与身份

- 队列身份：`reports/archive_download_ds_full/queue_v3.csv` 原始 symbol +
  `tools/archive_download_ds.Entry.relpath()`（含 safe_seg 中文映射），**不用目录名反推**。
- 身份映射表：`detail/m0_source_identity_glm.csv`（44,360 行 = 1h/1d 全部月文件，
  列：symbol/interval/month/url/relpath/exists）。
- 与你的 `SOURCE_IDENTITY.md` 对账：713/713 双向零差、中文币 6,394/514 根、
  JUV 2,232+EDU 2 重复、冲突 0——全部独立重算一致。

## 2. 列约定（底表 `detail/fixed_outcomes_hourly.parquet`，15,976,661 行）

| 列 | 含义 |
|---|---|
| `ts` | 决策小时（该小时有唯一一根 bar 才有行） |
| `ref_ts` / `ref_available` / `ref_price` | ts+1h 整点；缺失=0/NaN，不跳根 |
| `max_high_7d/30d/90d`、`min_low_*` | 窗口 `[ref_ts, ref_ts+W*24h)` 内 high/low 聚合 |
| `end_close_*` | 期末整点（ref+W*24h-1h）close；缺失=NaN，不前值填补 |
| `gap_hours_*` | 窗内内部缺时正计数（尾部越出数据末端不算缺口） |
| `complete_*` / `immature_*` | 完整=无内部缺时且不越末端；immature 保留对象不计失败 |

事件表（`events_diag_30.csv` / `events_fixed_50.csv`）关键列：
`signal_available_ts`（启动次日 00:00Z）、`ref_ts`（次日 01:00Z）、`ref_status`
（ok/ref_hour_missing/no_hourly_data）、`post_dd_close`（事件边界内、信号后、
含当根累计高点，≤0）、`post_dd_low_intraday`（前一bar累计高点，≤0）、
`listing_window` + `listing_first_obs_iv`（队列全历史最早在档月首根）。

## 3. 冻结口径

`run_spec.json`（运行前写死）：参考语义、去重规则、窗口定义、回撤定义、
一致性「独立从原始算」原则、禁做清单。SEI 预期值 0.2021 也预写在 spec 里。

## 4. 复现命令（PowerShell，项目根，自退出、无网络）

```powershell
py -3.13 -B -u reports/a1_stage2_glm53f/r3/scripts/m0_verify_glm.py        # ~2 min
py -3.13 -B -u reports/a1_stage2_glm53f/r3/scripts/m2_fixed_outcomes_r3.py # ~4-15 min（受并发负载）
py -3.13 -B -u reports/a1_stage2_glm53f/r3/scripts/m2_natural_paths_r3.py  # ~6-12 min
py -3.13 -B -u reports/a1_stage2_glm53f/r3/scripts/m2_consistency_r3.py    # ~4 min, 应 ALL_PASS=True
py -3.13 -B -u reports/a1_stage2_glm53f/r3/scripts/legacy841_r3.py         # ~1 min
py -3.13 -B -u reports/a1_stage2_glm53f/r3/scripts/bio_annex_r3.py         # <1 min
```

## 5. 逐项通过/未过

| 项 | 状态 | 你复验时的抓手 |
|---|---|---|
| 参考 t+1h（事件级 15,797 命中/0 跳根） | ✅ | `consistency_r3.json` C1（评审 8 例）、C2 |
| BTC 缺口三反例 | ✅ | C3：ref 缺失、gap=1、30d 期末 NaN |
| 底表抽样 300 行独立重算 | ✅ | C4（含 SOXSBUSDT 尾部不足的口径拆分） |
| 单边上涨回撤=0 | ✅ | C5 合成例 |
| JUV/EDU 去重 | ✅ | C6（原始 43,847/43,847→唯一 41,615；EDU 2） |
| 稳定币 0 事件 / 中文币入册 | ✅ | C7（FTT 是平台币，29 事件合法） |
| 27 上市反例残留 | ✅ 0/54 | 评审 `m2_listing_counterexamples.csv` 逐条比对 |
| 旧 841 / BIO 附页 | ✅ | `legacy841_*.csv`、`bio_same_day_annex.json` |

## 6. 请你重点攻击的面

1. C4 只抽了 30 币×10 行——欢迎加大抽样或换币种复核 `gap_hours` 的尾部拆分口径。
2. `post_dd_low_intraday` 的「前一bar累计高点」口径是否与你的语义一致（同根高低不比较）。
3. 我新增的日线重复 3 条（AXS/UMA/USDP）——你的 M0 是否也要补日线扫描。

## 7. 运行前后摘要

运行前：r2 产物与原始库 SHA 未变（只读）；运行后新增本目录全部文件。
底表行数 15,976,661 = 唯一小时总数（与你的 M0 恒等式闭合）。
