# B6 · v3 归档独立收尾对账（ds4.1f，2026-10-14）

~~**结论：四态对账通过，无写入者，无待验证/坏文件残留。**~~（2026-10-14 主代理更正：40项抽样通过，完整身份对账未过；脚本完成数写死且路径未按安全映射处理，见 `reports/a1_stage2_review/r2/summary.md` §6。）本条只做只读核对，
**未启动采集、未删除任何文件**（任务书明确禁止据此重启采集或清理）。

## 1. 无写入者核对

| 证据 | 值 |
|---|---|
| 退出记录 `run_v3.exitcode.txt` | `2026-10-08T20:24:59+0800 EXIT=0 argv=run --version v3 --ramp auto` |
| 运行日志末行 | `队列已无未完成项，采集结束`；`退出：新下 61396 份 / 655012362 字节 / 122792 请求 / 4192 秒` |
| 运行进程 | 无归档采集进程（当时的 python 进程为本轮 A1 重放自己的 worker，不写归档） |
| 锁/心跳文件 | 目录内只有 `blocks_v2.jsonl`、`blocks_v3.jsonl`（块日志），无活动锁 |
| 历史 pid 文件 | `download.pid`=43772、`download_v2.pid`=37548（均已退出，属旧版本） |

## 2. 四态对账（只读）

```
py -3.10 tools/archive_queue_ds.py status --version v3
版本 v3 | 队列 1,000,272（旧分母 251,951）
已完成 1,000,272 | 剩余 0 | 完成字节 43,159,115,823
四态：{'done': 1000272}
```

- 四态之和 = 队列总数 **1,000,272**，无「缺失 / 待验证 / 坏文件」。
- 独立实现「URL → 本地路径」映射（`market/dataset/symbol/interval/filename`）后自行扫盘：
  - 磁盘正式文件 **1,000,305** 个；`.pending` / `.part` 残留 **0** 个。
  - 差额 **33** 个，逐项有解释（见 `evidence/b6_extra_files.json`）：
    - 29 个**旧命名遗留**（`__USDT` / `___USDT` 等塌缩名，2026-10-06 非 ASCII 单射修复之前产生）；
    - 2 个 `.stale-` 改名留档（同一修复期间保留的原文件）；
    - 2 个状态文件 `_state.json` / `_state.journal.jsonl`（落在归档根目录，不属队列）。
- 独立抽样 40 项（固定种子 20261014，跨 11 个类别）：**40/40** 本地存在、
  字节数与队列 `remote_size_bytes` 一致、ZIP 可读（`evidence/b6_sample_checks.json`）。

## 3. 队列构成（按市场/类别/周期）

| 类别 | 条数 |
|---|---:|
| futures/metrics | 634,399 |
| spot/klines/5m | 121,207 |
| futures-cm/metrics | 91,651 |
| spot/klines/1d | 42,831 |
| futures/klines/5m | 28,212 |
| spot/klines/1h | 28,245 |
| futures/klines/1h | 24,443 |
| futures/fundingRate | 22,992 |
| futures-cm/klines/5m | 4,186 |
| futures-cm/fundingRate | 2,105 |
| futures/klines/1d | 1 |

## 4. 边界与未做

- 未做全库 100 万份逐份字节/ZIP 重验（成本过高，且下载期已逐份过来源 CHECKSUM）；
  本次是「状态对账 + 独立抽样」，不声称「每一份都当场重验过」。
- 未清理那 33 个遗留文件（任务书禁止据此删文件）。
- 不据此推断研究字段与时间全覆盖——**归档完整 != 研究字段齐全**，覆盖边界另见
  `reports/a1_stage2_ds/r2/` 与 `reports/b4_stage1_ds/coverage.md`。
