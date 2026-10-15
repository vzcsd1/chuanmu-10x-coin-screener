# A1 阶段 2 · 包 1 产物索引（ds4.1f）

任务：`tasks/a1_stage2_contract_20261012.md` §3 包 1。口径契约 §2，**不改评分、不改门槛、不联网**。

## 产物

| 文件 | 是什么 |
|---|---|
| `summary.md` | 一句话结论 + 关键数字 + 抽验 + 待裁定（**先读这个**） |
| `口径请示.md` | 8 处契约未钉死的口径，本包默认 + 备选，**请主代理逐条裁定** |
| `ledger/ledger_{2,3,6}h.csv` | **时序底账**：每轮各环人数（可观察/不可观察/数据缺失/成交额闸门/排序靠后/池/跳过/未入选/入选） |
| `rounds/candidates_{2,3,6}h.csv.gz` | **每轮候选**：入选行（symbol, h, total, kscore, refpx, event_seq, is_new_event） |
| `rounds/destinations_{2,3,6}h.csv.gz` | **全体去向明细**：每轮每个可观察标的的唯一去向 + 客观判据（qv24/kst/kscore/total/refpx） |
| `rounds/clock_comparison.csv` | 三个时钟横向对照 |
| `rounds/aggregate_stats.json` | 聚合总表（机器可读） |
| `scripts/replay_stage2.py` | 逐时点重放引擎（可 `--selftest`） |
| `scripts/deriv_fast.py` | 合约侧取值的向量化实现（与 b4 `deriv_as_of` 逐点等价） |
| `scripts/aggregate_stage2.py` | 底账 + 去向组装 |
| `_tmp/sym_*.parquet` | 中间产物：每标的逐时点客观事实（674MB，可删；删了要重跑约 43 分钟） |

> 注：本仓 `.gitignore` 规定 `reports/**` 只入库 `.md`，故上表的 `.csv/.csv.gz/.json/.parquet`
> **只在本地磁盘**、不进 git（与既有各包一致）。若要入库需主代理决定是否加白名单。

## 去向枚举（§2.5 六环 → §3 四桶）

| 去向值 | 含义 | 桶 |
|---|---|---|
| `not_observable` | 当时无任何已闭合 K 线（未上市/已停） | 不可观察 |
| `data_missing:stale` | 有档案但当时断档/停更 | 数据缺失 |
| `gated:volume` | 成交额不在 [50万, 5000万] | 范围过滤(闸门) |
| `gated:volume_unknown` | 成交额不可得（<24 根已闭合） | 范围过滤(闸门) |
| `gated:max_symbols` | 过闸门但排名超 400（**本重放从未触发**） | 排序靠后 |
| `skipped:kline_short_history` | K 线预热不足（<27 行） | 跳过 |
| `scored_below_threshold` | 已评分、总分 < 5 | 未入选 |
| `selected` | 总分 ≥ 5 | 入选 |

## 复现

```bash
# 0) 自检（切片法 vs 完全忠实法；应输出 0 分歧）
py -3.10 reports/a1_stage2_ds/scripts/replay_stage2.py --selftest

# 1) 逐时点重放（711 标的 × 主时段；16 核约 43 分钟）
py -3.10 -u reports/a1_stage2_ds/scripts/replay_stage2.py --workers 16

# 2) 聚合出底账 + 逐轮去向
py -3.10 -u reports/a1_stage2_ds/scripts/aggregate_stage2.py
```

环境：`py -3.10`（pandas 2.3.3 / numpy 2.1.2 / pyarrow 25.0.1）。不联网（无任何 socket 调用）。

## 口径速查（详见 `口径请示.md`）

- 可观察＝该整点有**当前**已闭合 1h K 线；断档算数据缺失。
- 成交额闸门＝归档 1h `quote_volume` **最近 24 根已闭合之和**（超出 B4 契约的新口径，待追认）。
- 市值闸门＝历史不可重建 → 按冻结函数 **fail-open 不拦**，标 `unknown`。
- 时钟锚点＝UTC 00:00；2h=偶数点，3h=3 的倍数，6h=6 的倍数。
- §2.3 事件：「连续 24h 不符合」只累计**可观察且已评分但未入选**的墙钟时长，缺数据不算。
- 参考价＝信号可用后下一完整时点（1h 粒度即本小时 K 线收盘），拿不到标缺失。
