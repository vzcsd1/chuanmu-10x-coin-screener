# R3 · 跨实例状态写入纪律修复（ds4.1f · 2026-10-07）

> 对应任务书 `tasks/live_futures_r2.md` 顶部最新返修要求，以及主代理
> `reports/live_futures_r2_final_review.md` 的 **ds 阻塞**。
> **本轮只修一个阻塞**：跨实例登记的新暂停，仍会被旧请求成功后的保存覆盖。
> **未联网、未清生产暂停、未换出口、未动后台归档采集（PID 29652 保持运行）、
> 未改评分/币池/门槛/UI/数据完整性逻辑、未加缓存平台。**

---

## 一、原样复现（修复前）

主代理报告里的最小复现原样搬运到 `repro_final_review.py`：

```bash
PYTHONPATH=. py -3.10 reports/live_futures_ds/r3/repro_final_review.py
```

| 阶段 | 输出 |
|---|---|
| 修复前 | `NEW_PAUSE_PRESERVED False` |
| 修复后 | `NEW_PAUSE_PRESERVED True` |

复现情节：A 读到一条**已过期**的暂停（`banned_until_ms = now-1`）→ 判定可以恢复请求；
请求执行期间，B 从**同一状态文件**重新加载并登记**未来一小时**的新暂停；
A 的请求成功返回 → `clear_if_expired` 用自己那份**仍显示"已过期"的旧副本**把该键删掉，
再整表写盘 → B 的新暂停消失。

---

## 二、根因

不是"忘了加锁"，而是**修改路径的读写纪律不统一**：

| 路径 | 修复前的问题 |
|---|---|
| `record_pause` | 在**本地内存副本**上 `max(previous, until)` 再整表 `save()` → 会覆盖别的实例刚登记的更晚暂停 |
| `mark_probe` | 同上，用旧副本整表写 → 会抹掉新暂停 |
| `clear_if_expired` | 用**本地副本**判断是否到期 → 本地过期、磁盘上其实有未到期的新暂停，仍会删除并写盘 |
| `reserve_data_request` | 只有调用方（`call()`）在外面加锁；方法自身不加锁、不重读，直接被调用时会基于旧快照放行 |

`save()` 是整表写：**只要内存副本是旧的，写回去就是"旧覆盖新"**。
R2 只在预约路径上加了锁，其余三条路径仍沿用"改内存 → 整表写"的旧模式，于是留下这个洞。

---

## 三、修复：统一「锁内重读 → 合并/条件判断 → 保存」

### 3.1 可重入的跨进程锁（`_StateLock`，行 352）

两层锁，解决"只锁预约"和"嵌套锁死锁"两个要求：

- **进程内**：按状态文件路径共享的 `threading.RLock` —— 同一进程里的**不同实例**也互斥
  （不能只靠 OS 文件锁，Windows 字节区间锁在同进程多句柄下语义不可靠）。
- **跨进程**：旁路锁文件 + 操作系统文件锁（Windows `msvcrt.locking` / POSIX `fcntl.flock`）；
  内核在进程崩溃时自动释放，不留陈旧锁。
- **可重入**：线程本地记录本线程已持有的路径；同线程再次进入同一路径只加计数、
  **不重复申请 OS 锁**，因此"锁内再调一个会加锁的方法"不会自锁死。

### 3.2 四条修改路径全部改为锁内重读

| 方法 | 行 | 现在的纪律 |
|---|---:|---|
| `record_pause` | 558 | 加锁 → `reload()` 取磁盘最新 → 与本次期限取 `max` → `_write_locked()` |
| `mark_probe` | 582 | 加锁 → `reload()` → 只更新 `probe_at_ms`/`paused_since_ms` → 写回（不动别人的期限） |
| `clear` | 592 | 加锁 → `reload()` → 按磁盘最新状态删除 → 写回 |
| `clear_if_expired` | 600 | 加锁 → `reload()` → **按磁盘上的期限**判断；未到期一律不动、**不写盘** |
| `reserve_data_request` | 648 | 加锁 → `reload()` → 查额度 → 预约 → 写回（方法自带锁，不再依赖调用方） |

### 3.3 `save()` 降级为底层写

- `save()`（行 514）= 加锁 + 整表写；新增私有 `_write_locked()`（行 525）= 已持锁时写盘。
- `save()` 的 docstring 明确标注：**它不合并磁盘上的其它实例改动**，只用于播种/单实例收尾；
  并发场景必须走上面四条语义方法。
- **生产代码里已无任何 `save()` 调用**（已 grep 确认），四条语义方法是唯一修改入口。

### 3.4 调用点简化

`_fetch_deriv_context_live` 的 `call()` 不再自己套 `with locked()`，直接调
`guard.state.reserve_data_request(egress)`；`while True` 的"等待后回顶部复查暂停与额度"保持不变。

---

## 四、修复前后行为对比

```bash
PYTHONPATH=. py -3.10 reports/live_futures_ds/r3/before_after.py
```

同进程内把四条路径还原成 R2 旧实现，再跑同一组 `R3CrossInstanceTests`：

| | 通过 | 失败项 |
|---|---|---|
| **修复后**（当前生产代码） | **7/7** | — |
| **修复前**（还原 R2 旧实现） | 1/7 | 见下表 6 项 |

| 失败用例 | 抓到的旧行为 |
|---|---|
| `test_new_pause_survives_late_success_via_request_guard` | 新暂停被旧成功清除（`0 != now+3600000`） |
| `test_new_quota_survives_late_success_via_request_guard` | 等待期间登记的额度被覆盖 |
| `test_record_pause_merges_disk_max_not_stale_copy` | 短暂停把磁盘上更晚的暂停缩短 |
| `test_mark_probe_keeps_new_pause` | `mark_probe` 抹掉新暂停 |
| `test_clear_if_expired_judges_on_disk_not_local_copy` | 本地副本已过期 → 误删磁盘上的新暂停 |
| `test_two_threads_same_process_only_one_reserves` | 同进程两线程双双放行（少算） |

> `test_nested_locked_does_not_deadlock` 在旧实现下也通过（旧代码不加锁，自然不嵌套），
> 它是**新设计的守门用例**，不是回归对照。

---

## 五、测试结果

```bash
cd <项目根>
PYTHONPATH=. py -3.10 tests/test_live_futures_ds.py          # → Ran 35 tests ... OK
PYTHONPATH=. py -3.10 -m unittest tests.test_request_governor tests.test_candidate_pool \
  tests.test_inventory_manifest tests.test_archive_download_ds tests.test_archive_queue_ds \
  tests.test_archive_content_audit_glm                        # → Ran 169 tests ... OK
```

**35 + 169 = 204 项全绿。**

### 5.1 新增 `R3CrossInstanceTests`（7 项，全部走**两个独立实例**）

| 用例 | 验证 |
|---|---|
| `test_new_pause_survives_late_success_via_request_guard` | 真实 `RequestGuard.run` 路径：新暂停保留 |
| `test_new_quota_survives_late_success_via_request_guard` | 真实 `RequestGuard.run` 路径：暂停与额度**都不丢** |
| `test_record_pause_merges_disk_max_not_stale_copy` | 暂停登记合并取最晚 |
| `test_mark_probe_keeps_new_pause` | 恢复探测不覆盖新暂停 |
| `test_clear_if_expired_judges_on_disk_not_local_copy` | 条件清除看磁盘最新；到期仍能正常清除 |
| `test_nested_locked_does_not_deadlock` | 嵌套加锁不死锁（线程看门狗 15s） |
| `test_two_threads_same_process_only_one_reserves` | 同进程两线程争抢最后名额只过一个 |

### 5.2 已保留的既有测试（任务书要求）

| 保留项 | 用例 |
|---|---|
| 滑动窗口 | `test_window_slides_not_fixed`、`test_boundary_does_not_release_one_ms_early`、`test_clock_jump_backward_does_not_release_early` |
| 双进程预约 | `test_two_processes_race_last_slot_only_one_passes`（真实 `subprocess`） |
| 429/418 停止请求 | `test_418_does_not_continue_to_next_branch` |
| 失败计数不回退 | `test_failed_request_count_does_not_roll_back` |
| 等待中被暂停不请求 | `test_pause_during_wait_blocks_the_request` |

> `test_lost_update_without_lock_is_fixed_by_lock_and_reload` 已按新语义拆成两条：
> `test_raw_whole_table_write_loses_update`（反例对照）与
> `test_public_api_never_loses_update`（修复后行为）——因为 `reserve_data_request`
> 现在自带锁，"两个实例不加锁"的旧演示已不再适用。

### 5.3 与 glm 测试的关系

`tests/test_live_futures_acceptance_glm.py` 中 glm 的 R2 用例**已全通过**；
仅剩 4 项 **R3** 用例失败（`R3FieldCompletenessTests`：历史不足 / 尾零掩盖完整性），
正是 `reports/live_futures_r2_final_review.md` 里的 **glm 阻塞**，属 glm 本轮待做，
**与本轮 ds 改动无关**。

---

## 六、结论拆分

### ✅ 已修（离线可证）

- 四条修改路径统一「锁内重读 → 合并/条件判断 → 保存」。
- 跨实例新暂停、新额度都不会被旧请求成功后的保存覆盖（含真实 `RequestGuard.run` 路径）。
- 嵌套加锁不自锁死；同进程多实例/多线程互斥有效。
- 预约方法自带锁，不再依赖调用方是否记得加锁。

### ⏸ 未修（本轮明确不做）

- 磁盘行情缓存、先 OI 后 LS 的大重排、整轮摘要契约（glm 负责）、评分/币池/门槛/UI 一律未动。
- `save()` 仍是整表写：它不合并其它实例改动。**生产代码已无调用**；
  若将来有人新增调用点，需自行遵守锁内重读纪律（已在 docstring 标注）。

### 🔬 需要真实验证（不得由离线全绿推出）

1. 一次正常真实查询是否顺畅、是否仍触 429/418；
2. `X-MBX-USED-WEIGHT-*` 实际读数；
3. 共享出口上他人流量 —— 本机自限无法覆盖，**不保证留余量就不会被他人影响**；
4. 多进程真实交错（本轮用子进程只覆盖了"争抢最后一个名额"这一种时序）。

---

## 七、未解决问题

1. 共享出口影响无法从本机证据确认。
2. `/fapi/v1` 的 IP 权重上限仍**待核实**（结论不依赖该数字）。
3. 触发者与实际轮次无日志（`ui/app.py` 未记录扫描起止）。
4. 本轮仍**不宣布原封禁根因已确定**；不把测试通过写成实时恢复或收益达标。

---

## 八、交付

| 文件 | 说明 |
|---|---|
| `reports/live_futures_ds/r3/summary.md` | 本报告 |
| `reports/live_futures_ds/r3/ready_for_glm.md` | 交接：SHA256 + 测试结果 + 停止编辑声明 |
| `reports/live_futures_ds/r3/repro_final_review.py` | 主代理复现脚本（原样） |
| `reports/live_futures_ds/r3/before_after.py` | 修复前后行为对比 |
| `tests/test_live_futures_ds.py` | 新增 `R3CrossInstanceTests`（7 项） |

**ds 到此停止，不再编辑核心文件。**
