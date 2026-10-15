# 共用来源身份口径（r3）

**来源：ds r3 按 v3 下载队列真实身份重建，供 glm 复验/共用。**
生成脚本：`reports/a1_stage2_ds/r3/scripts/m0_source_identity.py`
机器可读摘要：`reports/a1_stage2_ds/r3/m0_source_identity_summary.json`
逐身份表：`reports/a1_stage2_ds/r3/detail/source_identity_table.csv`
逐文件表：`reports/a1_stage2_ds/r3/detail/identity_file_paths.csv`
重复小时表：`reports/a1_stage2_ds/r3/detail/duplicate_hours.csv`
冲突表：`reports/a1_stage2_ds/r3/detail/duplicate_conflicts.csv`

---

## 1. 为什么不是「目录名 endswith('USDT')」

`tools/archive_download_ds.py` 的 `safe_seg()` 对非 ASCII / 特殊字符名追加 SHA1 短摘要：

| 真实 symbol | 安全目录段 | 安全文件名（示例） |
|---|---|---|
| 币安人生USDT | `____USDT~3fa2de8eda` | `____USDT-1h-2026-01.zip~391e4e796b` |
| 牛来USDT | `__USDT~c8c8e45d71` | `__USDT-1h-2026-09.zip~db0d6576d7` |

因此按目录/文件名猜身份会**漏掉合法归档**。正确做法：读队列真实 `symbol`，用
`Entry.from_row(row).relpath()` 定位磁盘（`parts[0]=market`、`parts[1]=dataset`、
`parts[2]=symbol 安全段`、`parts[3]=interval`、`parts[4]=filename`）。

## 2. 身份口径（本次本地研究范围）

- 范围：`market=spot`、`dataset=klines`、`interval=1h`、`symbol` 以 `USDT` 结尾、
  `period_start` 落在 `[2022-01-01, 2026-09-30]`。
- 结果：**713 个真实 symbol**，1h 文件 **22,180 份**，磁盘缺失 **0**，读取错误 **0**。
- 两包 r2 只交 711 个，共同漏掉：

| 来源币名 | 可读小时归档 | 主区间小时行 |
|---|---:|---:|
| 币安人生USDT | 9 份（2026-01..2026-09） | 6,394 |
| 牛来USDT | 1 份（2026-09） | 514 |

## 3. 重复小时（读取层去重，原始库不动）

| symbol | 源行数 | 唯一小时 | 多出行 | 重复小时 | 内容冲突 | 源月份 |
|---|---:|---:|---:|---:|---:|---|
| JUVUSDT | 43,847 | 41,615 | 2,232 | 744 | 0 | 2022-03 |
| EDUUSDT | 30,038 | 30,036 | 2 | 2 | 0 | 2026-06 |

规则：同一 `(symbol, open_time)` 保留首条、按 `open_time` 去重；内容不一致单列「冲突未知」，
不静默改数。本次两币冲突均为 **0**。

**行数恒等式**：`15,976,661 唯一小时 + 2,234 额外重复 = 15,978,895 源行`。
（主代理 r2 所说 `15,971,987` 是**交付行数**，含 2,234 重复；交付≠独立小时点。）

合计去重后本研究范围现货 1h 小时点 = **15,976,661**。

## 4. 不保证的事

- 文件存在 ≠ 当时具备交易资格；交易资格一律标 `unknown`。
- 713 只表示**本次明确的本地研究范围**，不代表币安历史全部 USDT 交易对。
- 359 个身份在范围内有内部缺口（`h_missing_internal_hours > 0`），逐身份见表。

## 5. 复跑

```powershell
py -3.10 -B -u reports/a1_stage2_ds/r3/scripts/m0_source_identity.py
py -3.10 -B -u reports/a1_stage2_ds/r3/scripts/m0_wiring_check.py
```
首次约 5 秒，只读不联网。
