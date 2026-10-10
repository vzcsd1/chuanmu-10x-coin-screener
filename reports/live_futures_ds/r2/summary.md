# R2 · 额度机制与暂停语义修复（ds4.1f · 2026-10-07）

> ⚠️ **2026-10-07 R3 补记（原处更正，不覆盖原文）**：本报告第三节"已修"清单里，
> 「跨进程原子预约」一项**当时并不完整**——只有预约路径加了锁，
> 暂停登记 / 恢复探测 / 条件清除仍是"改内存副本 → 整表写盘"，会**覆盖其它实例刚登记的新暂停**。
> 该阻塞由主代理 `reports/live_futures_r2_final_review.md` 指出（`NEW_PAUSE_PRESERVED=False`），
> 已在 **R3** 修完：四条修改路径统一为「锁内重读 → 合并/条件判断 → 保存」。
> 最新状态请看 `r3/summary.md` 与 `r3/ready_for_glm.md`；**本报告的 SHA256 与契约已过期**。

> 对应任务书 `tasks/live_futures_r2.md` 的 **ds 部分**。
> **本轮未请求任何真实行情、未清暂停、未换出口、未动后台归档采集（PID 29652 保持运行）、
> 未加磁盘行情缓存、未改评分/权重/门槛/币池/整轮返回结构。**
> 交付文件：`binance_box_strategy.py`、`tests/test_live_futures_ds.py`、本目录两份报告。

---

## 一、先复现，再修（任务书第 1 条）

用主代理（Grok 审核）给出的最小复现脚本，**先确认三个反例真实存在**，再动真实请求路径。

| 反例 | 复现现象 | 判定 |
|---|---|---|
| ① **计数未保存** | `note_data_request()` 后隔离检查状态文件：`path.exists()` 为 `False`（期望 `True`）——计数只留内存，另一进程/重启看不见 | 成立 |
| ② **重载不同步** | 写方写 `used=2` 后，读方 `reload()` 仍是 `used=1`——`reload()` 只同步 `domains`，漏掉额度窗口 | 成立 |
| ③ **固定窗口边界双突发** | 在 `299999ms` 登记满 600 次后，`300000ms` 时 `data_quota_delay_ms()` 返回 **0**——边界处允许再发满一轮 | 成立 |
| ④（Grok 第 4 阻塞项）**成功路径可清域暂停** | `RequestGuard.run` 成功后无条件 `clear(key)`——晚到的成功会把等待期间新登记的暂停抹掉 | 成立 |

四个反例全部成立，与 `reports/live_futures_grok_review.md` 一致。修复后各有对应用例转绿（见第四节）。

---

## 二、修复清单（真实请求路径，`binance_box_strategy.py`）

### 2.1 跨进程原子预约

- 新增模块级 `_lock_fh` / `_unlock_fh` / `_StateLock`：
  用**旁路锁文件 + 操作系统文件锁**（Windows `msvcrt.locking` LK_LOCK / POSIX `fcntl.flock`），
  把「读—查—预约—保存」保护成**不可被另一进程插入**的原子动作。
  选文件锁而不是 `O_CREAT|O_EXCL`，因为**内核会在进程崩溃时自动释放锁**，不留陈旧锁。
- 新增 `RateLimitState.locked()`（`path=None` 时为 no-op，测试/内存模式安全）。
- 新增 `reserve_data_request(egress)`：**检查 + 预约 + 落盘**一次完成，返回需等待的毫秒数。
  `call()` 在 `with state.locked():` 内先 `reload()` 再 `reserve_data_request()`。

### 2.2 滑动窗口（消除边界双突发）

- `data_quota` 由「固定窗口计数」改为**时间戳列表** `dict[出口 -> list[int]]`。
- `data_quota_delay_ms(egress)` 只统计最近 `DATA_QUOTA_WINDOW_MS` 内的请求，
  因此**任意连续 5 分钟窗口**都不超 `DATA_QUOTA_LIMIT`——固定窗口在边界处"两次各发满"的缺陷消除。
- 官方只写 "IP rate limit 1000 requests/5min"，**未明确窗口类型、也未明确是否跨端点共享**；
  本实现按**更保守**的滑动窗口执行，并把本地预算压到官方额度的 60%（`DATA_QUOTA_LIMIT=600`）。

### 2.3 重载同步额度

- `reload()` 现在**同时**同步 `domains`（暂停表）与 `data_quota`（额度窗口），不再只同步前者。

### 2.4 暂停语义

- 新增 `clear_if_expired(key)`：**只在暂停已到期时**才清除该域。
  晚到的成功只能证明"自己这次通了"，不能证明等待期间由其它请求/进程登记的新暂停也解除了。
- `RequestGuard.run` 成功路径：`clear(key)` → **`clear_if_expired(key)`**。
- `_connect_domain` 连接成功后：同样改为 `clear_if_expired(key)`。

### 2.5 等待可中断 + 等待后复查

- 新增 `_sleep_interruptible(seconds, step=1.0)`：分段睡眠，Ctrl+C / 外部中断在 1 秒内生效。
- `call()` 改为 **`while True` 循环**：
  每轮先查暂停（`should_attempt`）→ 锁内 `reload` + `reserve` → 若需等待则 `_sleep_interruptible`
  → **回到循环顶部重新检查暂停与额度**，直到可立即请求才真正发出。
  → 等待期间若被登记暂停，等待结束会立刻抛 `DomainPaused`，**不发出请求**。

### 2.6 429/418 不继续另一取数分支

- `_fetch_deriv_context_live` 的三个取数分支各新增 `except _LIMIT_ERRORS`：
  429/418 已由 guard 登记为暂停，**立刻返回 `status=paused`**，不再落到后面的宽 `except Exception`
  继续请求大户多空或回退口径。

### 2.7 去掉整表回写（防丢失更新）

- `public_selection` 结尾原本有一处 `guard.state.save()`，会用**本进程的陈旧副本**盖回磁盘，
  抹掉其它进程刚登记的预约 → 少算 → 重复放行。**已删除**，只保留 `reserve_data_request` 内的逐次原子写入。

---

## 三、结论按「已修 / 未修 / 需要真实验证」拆分（任务书要求）

### ✅ 已修（离线可证，有对应用例）

| 项 | 证据用例 |
|---|---|
| 跨进程原子预约，预约即落盘 | `test_reservation_is_persisted_immediately` |
| 双进程争抢最后名额只放行一个（真实两进程） | `test_two_processes_race_last_slot_only_one_passes` |
| 无锁会丢失更新、加锁+reload 修复 | `test_lost_update_without_lock_is_fixed_by_lock_and_reload` |
| 滑动窗口，边界不提前释放 | `test_boundary_does_not_release_one_ms_early`、`test_window_slides_not_fixed` |
| 时钟回跳不提前释放 | `test_clock_jump_backward_does_not_release_early` |
| reload 同步额度窗口 | `test_reload_syncs_quota_not_only_pauses` |
| 失败请求计数不回退 | `test_failed_request_count_does_not_roll_back` |
| 等待中被暂停 → 不发请求 | `test_pause_during_wait_blocks_the_request` |
| 429/418 不继续下一取数分支 | `test_418_does_not_continue_to_next_branch` |
| 晚到成功不清除新暂停 | `test_late_success_does_not_clear_new_pause` |
| 正常扫描范围不变 | `test_normal_scan_scope_unchanged` |

### ⏸ 未修（本轮明确不做 / 范围外）

| 项 | 原因 |
|---|---|
| 磁盘行情缓存 | 任务书明确"本轮先不加磁盘行情缓存" |
| "先 OI 后 LS"大重排 | 任务书明确不做；分两段本身不减少请求，必须以统一预算调度（本轮的 `reserve` 已统一预算） |
| 共享出口（IP 203.10.99.42）上他人流量 | 本机无法约束；只能靠自限把**自己**的用量压到预算内 |
| `/fapi/v1` 的 IP 权重上限引用 | 官方文档站改版，未取得直接引用，仍标**待核实**；结论不依赖该数字 |
| glm 的整轮摘要契约（`round_stats`/`round_quality`） | 属 glm 负责范围；其验收用例当前失败是**预期**（功能未实现） |

### 🔬 需要真实验证（不得由离线全绿推出）

1. 修复后在**一次正常真实查询**中是否顺畅、是否仍触 429/418；
2. 响应头 `X-MBX-USED-WEIGHT-*` 的**实际读数**（用于判断是否真需要多出口）；
3. 服务方实际的退避/封禁时长；
4. 是否存在**同 IP 其它进程/他人流量**——本机自限无法覆盖。

---

## 四、测试结果

```bash
cd <项目根>
PYTHONPATH=. py -3.10 tests/test_live_futures_ds.py
# → Ran 27 tests ... OK
```

| 组 | 数量 | 说明 |
|---|---:|---|
| `RequestBudgetTests` | 6 | 请求预算（第一轮，沿用） |
| `GovernorFixTests` | 5 | 出口回退/暂停期限（第一轮，沿用） |
| `DataQuotaTests` | 5 | 额度自限（**本轮改到滑动窗口 API**） |
| `R2QuotaSemanticsTests` | 10 | **本轮新增**：落盘/重载/失败不回退/边界/时钟跳变/等待中暂停/429 不续分支/晚到成功/正常范围 |
| `R2CrossProcessTests` | 1 | **本轮新增**：真实双进程争抢最后名额 |

**27/27 通过。** 全部使用 `TemporaryDirectory` 与替身对象，**不联网、不写真实 `results/`**；
等待用**假时钟**（`FakeClock`）推进，不真等 5 分钟。

### 4.1 双进程用例的证据强度

- **正向**：两个真实子进程（`subprocess`）在 `go` 文件上自旋同步后同时争抢，结果
  `delays = [0, >0]`，且最终落盘计数恰为 `DATA_QUOTA_LIMIT`——**只放行一个，不多算不少算**。
- **反向**：去掉锁的对照实验表明，无锁时两个请求者会读到**同一份旧快照**并双双放行，
  后写覆盖前写导致**计数少算一次（丢失更新）**。实测中该竞态窗口很窄（微秒级），
  **不保证每次都能复现超发**，但它是确定性存在的正确性隐患——这正是必须上跨进程锁的理由。
  `test_lost_update_without_lock_is_fixed_by_lock_and_reload` 用确定性方式把这一点钉死。

### 4.2 回归

```bash
PYTHONPATH=. py -3.10 tests/test_request_governor.py tests.test_candidate_pool.py \
    tests.test_inventory_manifest.py tests.test_archive_download_ds.py \
    tests.test_archive_queue_ds.py tests.test_archive_content_audit_glm.py
```

上述 **183 项全部通过**。另有 `tests/test_live_futures_acceptance_glm.py` **11 项失败/报错**，
失败原因是 `KeyError: 'round_quality'` 与 `TypeError: run_scan() got an unexpected keyword argument 'round_stats'`
——即 glm 本轮新增的**整轮摘要契约**用例，对应功能尚未实现，**与本轮改动无关**
（`round_stats`/`round_quality` 在 `binance_box_strategy.py` 中根本不存在，属 glm 待做）。

---

## 五、过度结论更正（任务书要求）

| 旧表述 | 更正 |
|---|---|
| "**单轮必安全**" | 改为"**典型条件下单轮不超**"。436/800 只是"无回退 + 窗口内无其它用量 + 出口独占"三个假设同时成立时的算术；回退口径 N→3N=654（N=400 时 1200，**单轮即超**）、多进程/重启叠加、共享出口都可让单轮触限。已同步改到 `../request_budget.md` 第三节与 `../summary.md` 第一节。 |
| "**CLI 跨整点双突发**" | **不成立**。`main` 在 `run_once` 后 `sleep(poll_seconds)`（默认 300 秒），单进程 CLI **不会在同一个 5 分钟窗口发两轮**。该说法出自 glm 报告，`reports/live_futures_grok_review.md` 第 13 行已驳回；本轮再次确认。UI 快速重查、多进程、重启、共享出口是**另行待核实**的情形，不能混用时间模型。 |
| "**原事故根因已确定**" | **不宣布**。本轮只证明"一条独立额度未被覆盖 + 若干计数/语义缺陷"，属**必要条件**层面的证据；触发者身份、实际轮次、共享出口影响仍证据不足。 |

---

## 六、未解决问题

1. 共享出口影响无法从本机证据确认；本机自限只能约束自己发起的请求。
2. `/fapi/v1` 的 IP 权重上限仍**待核实**（不依赖该数字）。
3. 触发者与实际轮次**无日志**：`ui/app.py` 未把扫描起止写入文件，事后只能靠文件修改时间关联。
4. 真实一次查询的额度读数与退避时长**未验证**（本轮禁止联网）。
5. 修复后的**首次上线窗口为空**，第一次运行仍会发出完整的 2N 次（属正常，未超自限）。

---

## 七、交给 glm 的接续

见同目录 `ready_for_glm.md`。**ds 本轮到此为止，不再编辑核心文件。**
