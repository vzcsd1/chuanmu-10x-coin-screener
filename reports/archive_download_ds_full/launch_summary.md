# 全量归档采集 · 启动摘要（2026-10-06）

> 对应任务书 `tasks/ds41f_full_archive.md`。**采集已启动并在后台连续运行**，本条不是"已完成"。

## 一、本轮修掉的阻塞问题（8 项）

主代理审核报告点名 7 项 + 本轮自查新发现 1 项。

| # | 问题 | 修法 | 为什么重要 |
|---|---|---|---|
| 1 | 校验端点限流被 `except FetchError` 吞成"来源无校验" | `RateLimited` 直接上抛 → 登记**持久暂停** | 否则会把限流误判为"来源没有校验值文件"，把**未验证**的归档标成完成 |
| 2 | 校验值取不到 = 无来源 | 新增 `checksum_fetch_failed`，**待复核、不算完成** | 区分"来源确实没发布"与"我这次没取到" |
| 3 | 校验值不符仍可完成 | `mismatch` **不得完成** | 内容对不上就不该入账 |
| 4 | 已存文件重复解压/联网复核 | `verify_local` 命中**本地摘要**即跳过 | 长任务重启时省掉大量无谓开销 |
| 5 | 互斥锁按文件年龄判过期（30 min 抢占活进程） | 改**心跳锁**：`STALE=300s` / `REFRESH=30s`，只心跳过期才判残留 | 长任务跑几小时也不会被第二个进程抢锁 |
| 6 | 状态整库重写（平方级开销） | 改 **JSONL 增量**，每份追加一行，`compact_every=5000` 才整理快照 | 25 万份规模下，整库重写会把磁盘打满 |
| 7 | 完成状态未含校验结论 | 状态分离 `verified` / `no_checksum_source` / `checksum_fetch_failed` / `mismatch` | 台账要能区分"已验证"和"没验证" |
| 8 | **（自查新增）无人值守死循环** | `_pending()` 原先只排除"文件已落盘"；**若某块首项永久失败（404/内容异常），`todo[:chunk]` 每轮取到同一批，永远推不到后面的币种**。现把本轮确定性失败项**搁置跳过**（`S_NOT_FOUND`/`S_ANOMALY`），网络类失败仍留队重试，并加"连续 3 块零进展即停"兜底 | 这是"分块自动续传"能否真正推进到全库的前提 |

## 二、队列构建结果

`tools/archive_queue_ds.py build` —— 以 **URL 路径为权威**重解析身份（清单字段有缺陷也不怕）。

| 指标 | 值 |
|---|---|
| 输入清单行 | 281,133 |
| **队列项** | **251,951** |
| **队列体积** | **17.24 GiB** |
| `problem`（无法解析） | **0** |
| `conflicts`（同键两条 URL） | **0** |
| `field_mismatch`（已按 URL 纠正） | 22,992（全为 fundingRate 币种后缀） |
| `duplicate_url`（同 URL 静默去重） | 29,182 |

| 类别 | 文件数 | 体积 |
|---|---|---|
| futures/fundingRate | 22,992 | 0.02 GiB |
| futures/metrics | 80,751 | 0.80 GiB |
| futures/klines 1h | 24,443 | 0.66 GiB |
| futures/klines 5m | 24,443 | 6.77 GiB |
| spot/klines 1d | 42,831 | 0.07 GiB |
| spot/klines 1h | 28,245 | 0.83 GiB |
| spot/klines 5m | 28,245 | 8.09 GiB |

排序按「类别 → 周期 → 市场 → 起始日 → 币种」**时间片轮转**，保证任何时刻停下，每个币种都有一点，不会出现"字母表前几个币全齐、后面一个没有"。

## 三、采集运行状态

- **启动命令**：`py -3.10 tools/archive_queue_ds.py run --chunk 2000 --interval-sec 0.02 --max-minutes 0 --min-free-gib 20`
- 双击 `启动归档采集.bat` 等价（**幂等，重跑即从断点接续**）
- PID：`download.pid` ｜ 日志：`run.log` ｜ 进度：`progress.json`
- 停止：`taskkill /PID <download.pid>`（**重启前需先删 `data/archive_raw/.archive_download.lock`**，见下）

**第 1 块实测**（已验证分块循环真正工作）：

```
completed 2800 / remaining 249151 / 本块新下 2000 / 1,749,111 B / 682 秒 / 搁置 0
```

- 吞吐 **约 3.0 份/秒**，逐份通过来源 CHECKSUM 校验（抽样 409/409 为 `verified`）。
- 中断后重启进度**完整保留**（实测中断于 800 份，重启后从 800 继续，无重下）。
- 磁盘可用 107.9 GiB（保留线 20 GiB）。
- **全量 ETA 约 26–30 小时**（fundingRate ~2h → metrics ~7.5h → klines ~14h + 大文件传输）。

## 四、性能结论（重要，别重复踩）

**瓶颈是 CloudFront 往返延迟，不是节流、不是代理。**

| 测法 | 中位延迟 |
|---|---|
| 经代理 `127.0.0.1:10884`（会话复用 20 次） | **212 ms** |
| 直连 `--noproxy`（同上） | **219 ms** |

- 两者**相同** → 换直连**无收益**，不必为此改配置。
- 把 `--interval-sec` 从 0.1 降到 0.02 **不提速**（前后都是 ~3 份/秒）。
- 单线程每文件 2 次请求（数据 + 校验）≈ 0.4 s。**唯一提速手段是并发**，已登记 `TODO.md` C2，**本轮未采用**（不愿改动已过 37 项测试的单线程管线）。

## 五、一个锁的取舍

`DownloadLock` 只按**心跳年龄**判残留（测试 `test_lock_live_owner_not_taken_over` 用不存在的 pid 也要求拦截，是**有意契约**）。因此**杀掉进程后 5 分钟内重启会被拒**——需先删锁：

```bash
rm -f data/archive_raw/.archive_download.lock
```

未擅自改成 PID 存活判定，因为会与该测试契约冲突。

## 六、测试

| 套件 | 结果 |
|---|---|
| `tests/test_archive_download_ds.py` | **37/37 通过** |
| `tests/test_archive_queue_ds.py` | **21/21 通过**（本轮 +4 条回归） |

```bash
py -3.10 -m unittest discover -s tests -p "test_archive_*.py"
```

## 七、尚未完成

- 采集**仍在进行**，进度在 `TODO.md` B6。
- metrics 完整分页、非 USDT 现货 5m/1h、币本位尚未枚举完，后续补入队列（不等全部枚举结束）。
- **原始库 ≠ 研究已接入**；全量未完成不得标完成。
