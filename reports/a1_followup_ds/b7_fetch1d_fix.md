# B7 · 修 `research/01_fetch_1d.py` 的续传与完整性缺陷（ds4.1f，2026-10-14）

**结论：两处缺陷已修，隔离测试 7/7 通过。** 未联网、未覆盖原数据（只改代码 + 新增离线测试）。

## 1. 修了什么

| # | 旧行为 | 为什么错 | 新行为 |
|---|---|---|---|
| ① | `todo = list(plan)[:limit]` **先截前 N 个**，再在循环里 `if dst.exists(): skip` | 永远只看计划表最前面 N 个币；这 N 个都下过 → 每轮「新下 0 / 跳过 N」，**必然空转** | 先对**全部**币按月校验完整性，得到「未完成清单」，再取前 `limit` 个 |
| ② | `if dst.exists()` 即视为完成 | 某月下载失败时留下的残缺 parquet 会被永久跳过，缺口留存 | 完整 = parquet **实际覆盖** `data/plan_1d.json` 里该币的全部月份；缺哪个月就补哪个月 |

另外两处顺带改进（不改变语义，只为安全与可复算）：

- **不覆盖原数据**：补采时先读入本地已有行，与新月合并、按 `open_time` 去重，
  再写临时文件后原子替换（`.parquet.tmp` → `replace`），不整份丢弃。
- **逐月台账**：新增 `data/state_1d.jsonl`，逐 (币, 月) 记录 `ok/fail` 与时间，
  便于以后复算「哪个月是失败后补上的」。
- 新增 `--dry-run`（只列本轮会补哪些币、缺哪些月，**不发请求**）与
  `--raw-dir / --journal / --plan`（隔离测试用，默认行为不变）。

## 2. 隔离验收（`research/01_fetch_1d_test.py`，全程离线）

在临时目录里把网络层换成假来源（内存造 ZIP），人为预置一份「缺 2022-07」的残缺文件：

```
PASS  only_missing_month_requested     首轮只请求 2022-07（不是整币重下）
PASS  existing_rows_preserved          原有 56 行全部保留，补后 84 行
PASS  months_now_complete              三个月齐全
PASS  second_run_no_work               再跑一次「待办 0」（旧版会空转）
PASS  limit_does_not_spin              --limit 1 且已完整 → 仍待办 0
PASS  detects_removed_month            人为删掉 2022-08 → 能识别为缺月并只补该月
PASS  journal_written                  逐月台账已写
```

复跑：`py -3.10 -B research/01_fetch_1d_test.py`

## 3. 边界

- 缺口核查必须分别注明「对旧计划」和「对完整来源目录」——本次修的是**对计划**的完整性；
  来源目录里有没有这个月，要靠 `plan` 阶段列举，本项不代替 B6 的归档对账。
- 实网补采**不作为**离线验收条件（任务书要求），本轮不发真实请求。
- 未改 `data/1d/` 下任何既有 parquet；测试全部在临时目录。
