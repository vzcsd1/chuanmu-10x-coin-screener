# 归档下载器与首批采集 · 交付摘要

任务规格：`tasks/ds41f_archive_download.md`。日期：2026-10-06。

## 正文

已交付独立的可靠归档下载器 `tools/archive_download_ds.py`，含 `plan`（零请求）、
`fetch`（受预算约束）、`verify`（只复核不重下）三种模式；离线测试 23 项全部通过。
随后按规格执行了受限首批采集：**18 份归档、635,983 字节、36 秒**，全部落在
`data/archive_raw/`，**18/18 与来源 CHECKSUM 一致**，无未完成项。

下载器修掉了旧脚本的两个缺陷：一是"先取前 N 个再跳过"导致的空转——本工具**先验证
已存文件、再从未完成项挑本批**，实测第 2 批确实下的是第 9~16 项；二是文件存在即视为
完成的误判——完成需同时通过压缩包可读、字节数与清单一致、来源校验值一致三项，
且没有校验值时会标注"未验证来源校验值"而非谎称已验证。

首批只用于验证格式与可靠性，**不代表资料库范围**：样本仅 4 个主流币
（ADA/ATOM/LINK/DOT）、时段 2022-06 至 2024-06、只覆盖币安 USDT 现货与 U 本位合约，
不含下架币、小市值币与更早历史。全量范围待 glm 盘点结果，**本任务完成不等于
全量资料库完成**，不应据此启动长期采集。

## 1. 交付物

| 文件 | 说明 |
|---|---|
| `tools/archive_download_ds.py` | 下载器（唯一新增代码） |
| `tests/test_archive_download_ds.py` | 23 项离线测试（注入式假取数器，不联网） |
| `reports/archive_download_ds/manifest_first_batch.csv` | 首批清单 18 条 |
| `reports/archive_download_ds/download_status.csv` | 累计状态表（含字节/时间/校验值/备注） |
| `reports/archive_download_ds/status_run1.csv`、`status_run2.csv` | 分批验证留痕 |
| `data/archive_raw/` | 原始压缩包（市场/类别/币种/周期分层） |

## 2. 测试命令与结果

```bash
py -3.10 -m unittest discover -s tests -p "test_archive_download_ds.py" -v   # 23 passed
py -3.10 -m unittest discover -s tests                                        # 38 passed（含既有 18 项）
```

覆盖任务书点名的 7 个场景，另有 12 项边界：zip-slip 拒绝、校验值不符、无校验值不谎称
已验证、404 单列、暂时错误有限重试、plan 零请求、并发锁拒绝、磁盘下限拒绝、
强制重下保留旧版本、清单字段往返、路径片段消毒、状态文件损坏降级。

## 3. 首批实测

| 批次 | 新下 | 本地完整 | 预算停止 | 耗时 |
|---|---:|---:|---:|---:|
| 第 1 批（`--max-files 8`） | 8 | 0 | 10 | 16 s |
| 第 2 批（`--max-files 8`） | 8 | 8 | 2 | 16 s |
| 收尾（`--max-files 30`） | 2 | 16 | 0 | 4 s |
| 幂等复跑 | 0 | 18 | 0 | — |

- 实际：**18 文件 / 635,983 B / 36 s**；请求 36 次（18 归档 + 18 校验值）。
- 状态：`skipped_complete` 18；校验值 `verified` 18。
- 未完成原因：**无**。无 404、无限流、无网络失败、无内容异常。
- **下一次是否真的前进**：是。第 2 批下的是清单第 9~16 项，前 8 项被识别为本地完整，
  未重复下载；再跑一次为 0 新下载。
- 独立复核：逐份重算 sha256 与来源 CHECKSUM 比对，不一致 0 条；独立解包确认
  metrics 8 列、fundingRate 3 列、klines 12 列。

## 4. 复跑命令（PowerShell）

```powershell
# 只列计划，不发请求
py -3.10 tools/archive_download_ds.py plan --manifest reports/archive_download_ds/manifest_first_batch.csv

# 按审核后的清单继续下载（示例预算：30 文件 / 100 MiB / 15 分钟）
py -3.10 tools/archive_download_ds.py fetch `
  --manifest reports/archive_download_ds/manifest_first_batch.csv `
  --max-files 30 --max-bytes 104857600 --max-minutes 15 --min-free-gib 5

# 复核本地库与来源校验值（不重下归档）
py -3.10 tools/archive_download_ds.py verify --manifest reports/archive_download_ds/manifest_first_batch.csv
```

## 5. 吞吐与耗时推算

实测 18 文件 / 36 s ≈ **2.0 s/文件 ≈ 30 文件/分钟**。瓶颈是默认 1 秒请求间隔的礼貌节流，
不是带宽（36 次请求 × ≥1 s ≈ 36 s，与实测吻合）。该间隔是**初始实施参数，不是来源
允许额度的保证**，服务端要求时须进一步减速。

按同一参数外推（**仅供量级参考，不是承诺**）：

| 规模 | 外推耗时 |
|---:|---|
| 1,000 文件 | ≈ 33 分钟 |
| 10,000 文件 | ≈ 5.6 小时 |
| 100,000 文件 | ≈ 2.3 天 |

外推**未计入**限流暂停、失败重试、来源降速，且全量文件数尚未由 glm 盘点确定，
因此**不得当作全量完成时间**。

## 6. 未完成与下一步（需主代理审核）

- 全量范围、体积与分批顺序：等 `tasks/glm53f_data_inventory.md` 的盘点结果。
- 本次未启动长期采集、未安装任何定时任务。
- 建议主代理审核后再决定是否扩大清单；扩大前应先确认来源额度与磁盘预算。
