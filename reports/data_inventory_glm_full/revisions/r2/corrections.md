# 清单修正 r2

生成时间：2026-10-06T09:09:47Z

1. 月度覆盖去重键补上 symbol（market+dataset+symbol+interval+月）：
   修复跨币误删，恢复保留 164 行（逐行见 dedupe_diff.csv）。
   分月度/市场/周期隔离测试见 tests/test_inventory_manifest.py。
2. list_universe/probe_extensions 双斜杠修复后重探：结论见 probe_evidence.json，
   旧「五类皆空」结论作废（以本轮实测为准，注明日期）。
3. rawscan 改读 ds 状态文件（_state.json），按 market|dataset|symbol|interval|周期
   键匹配，处理 futures↔futures-um 别名；匹配/文件存在/来源校验分开报告。

旧分片与 index.csv 原样保留；本目录 index_v2.csv 为消费真源。
## 2026-10-06 追加（第二轮修正实测结果）

1. **月度去重键缺 symbol**：修复后从原 staging 离线重算，恢复 164 行（现货 74：2023 年 72 + 2025 年 2；U 本位 90：2021 年 9 + 2022 年 1 + 2026 年 80），与主代理独立快照一致。5 个分片重写：其中 1 个替代旧分片（futures-um_klines_5m_daily_2026），4 个为原被清空的年份新文件。cm 无恢复（其月度覆盖本来就同币对齐）。
2. **probe_extensions 双斜杠**：修复后五类目录全部实测非空——markPriceKlines 1,039 符号、indexPriceKlines 979、premiumIndexKlines 982、bookDepth 1,000（抽样 6,458 文件 2.8GB）、bookTicker 315（抽样 6 文件 20.1GB，单文件约 3.3GB）。旧「五类皆空」结论作废；只交可得性与体积，未枚举全量、未下载。
3. **rawscan 改读状态文件**：快照 2026-10-06T09:29:48Z，35,018 条记录：35,004 匹配 v2 清单、35,018 文件存在、35,018 来源校验通过；14 条为首批验证的旧键格式（裸月/裸日），文件在且校验通过，对应月份已在 v2 清单（如 ADAUSDT funding 2024-01 → 2024-01-01~2024-01-31）。清单中尚无状态记录 4,972,374 行（未下载部分，未触发任何重新下载）。

## 2026-10-06 追加二（rawscan 口径更正）

- **剩余量分母改为最终 index_v2.csv（去重后 1,000,270 行）**：此前报告的"清单中尚无状态 4,972,374"是去重前 staging（月度+日度重叠重复计入）的口径，**作废**。正式剩余未下载 = **961,471 行**（futures-um|metrics 618,602、spot|klines 192,275、futures-um|klines 52,652、futures-cm|metrics 91,651、futures-cm|klines 4,186、futures-cm|fundingRate 2,105）。
- **「来源校验通过 38,812」= 读取状态既有 checksum_status 字段**，本次未实测；快照 2026-10-06T10:13:57Z，状态 updated_at 09:37:18Z，范围仅覆盖 ds 已写入的记录（futures-um metrics/funding/klines 等），不代表全部类别。
- 匹配兼容三种键形态：完整日期 15,797、裸月 23,014；1 条未匹配记录待 ds 核对（见 archive_raw_inventory.csv match 列为空者）。
