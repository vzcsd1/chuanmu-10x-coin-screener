# R3 · 交接给 glm（ds 阻塞已修完）

> 本文件由 **ds4.1f** 在完成 `tasks/live_futures_r2.md` 顶部最新返修要求的 ds 部分后写入。
> **取代 `reports/live_futures_ds/r2/ready_for_glm.md`**（r2 那份已过期，请勿据此接手）。
> 交接后 ds 停止编辑核心文件。

---

## 一、核心文件摘要

| 文件 | SHA256 | 行数 |
|---|---|---:|
| `binance_box_strategy.py` | `b2c050134f0d827903d79416dd7b81c1b9690e568b8e805ff3b2de30ee66848c` | 1577 |
| `tests/test_live_futures_ds.py` | `05f9b37e859b7b40d69e2c23772bdc90219ef255fdc2970556242a34650c5b68` | 734 |

> 哈希基于写入本文件时的磁盘内容。若 glm 后续编辑 `binance_box_strategy.py`，哈希自然变化，**属预期**。

---

## 二、本轮改动范围（只碰请求/暂停/额度）

### 2.1 新增 / 重写

| 符号 | 行 | 说明 |
|---|---:|---|
| `_StateLock` | 352 | **重写**：进程内 `threading.RLock`（按路径共享）+ 跨进程文件锁 + **线程本地可重入** |
| `save` | 514 | 改为「加锁 + 整表写」；docstring 标注**不合并**其它实例改动，仅播种/单实例收尾用 |
| `_write_locked` | 525 | **新增**：已持锁时的写盘（内部用） |
| `record_pause` | 558 | 改为「锁内 `reload()` → `max` 合并 → 写回」 |
| `mark_probe` | 582 | 改为「锁内 `reload()` → 只更新探测字段 → 写回」 |
| `clear` | 592 | 改为「锁内 `reload()` → 删除 → 写回」 |
| `clear_if_expired` | 600 | 改为「锁内 `reload()` → **按磁盘期限**判断 → 未到期不写盘」 |
| `reserve_data_request` | 648 | 改为「自带锁 + `reload()` + 预约 + 写回」 |
| `_fetch_deriv_context_live` 的 `call()` | 991 | 去掉外层 `with locked()`，直接调 `reserve_data_request` |

### 2.2 未改动

评分 / 权重 / 门槛 / 币池 / `deriv_status` 取值 / 整轮返回结构 / `ui/` / 启动脚本 /
归档下载器 / `data_quota` 滑动窗口语义 / `DATA_QUOTA_LIMIT=600` 与窗口长度。

---

## 三、新的写入纪律（glm 编辑时请遵守）

1. **任何修改状态文件的操作，必须是「加锁 → `reload()` → 合并或条件判断 → 写回」**。
   只加锁不重读、或重读后不做条件判断，都会退回"旧副本覆盖新暂停/新额度"。
2. **不要在生产代码里调用 `save()`**。它不合并磁盘上的其它实例改动。
   并发场景请用 `record_pause` / `mark_probe` / `clear` / `clear_if_expired` /
   `reserve_data_request`——它们已经自带锁与重读。
3. **`locked()` 可重入**（同一实例、同一线程）：锁内再调上述方法不会死锁；
   但**不要跨实例在同一线程里嵌套**（不同实例不共享重入计数，会自锁死）。
4. **`clear_if_expired` 只清已到期的旧暂停**：它按**磁盘上的期限**判断，
   晚到的成功不会抹掉等待期间由其它实例登记的新期限。
5. **额度预约必须走 `reserve_data_request`**（自带锁 + 重读），不要自己先 `reload()` 再手动 `note` + `save`。
6. 429/418 必须经 `_LIMIT_ERRORS` 传播：登记暂停后立刻停止，不得继续另一取数分支。
7. 暂停不得靠换出口绕过（`_connect_domain` 命中限流后 `banned=True` 直接 `break`）。

---

## 四、测试结果与复跑命令

```bash
cd <项目根>

# ds 全量（35 项）
PYTHONPATH=. py -3.10 tests/test_live_futures_ds.py            # → Ran 35 tests ... OK

# 主代理报告里的最小复现
PYTHONPATH=. py -3.10 reports/live_futures_ds/r3/repro_final_review.py
# → NEW_PAUSE_PRESERVED True

# 修复前后行为对比（同进程还原 R2 旧实现）
PYTHONPATH=. py -3.10 reports/live_futures_ds/r3/before_after.py
# → 修复后 7/7；修复前 1/7（6 项失败，见 r3/summary.md 第四节）

# 其它回归（169 项）
PYTHONPATH=. py -3.10 -m unittest tests.test_request_governor tests.test_candidate_pool \
  tests.test_inventory_manifest tests.test_archive_download_ds tests.test_archive_queue_ds \
  tests.test_archive_content_audit_glm
```

**合计 204 项全绿**（35 + 169）。

**注意**：`tests/` 无 `__init__.py`；直接 `py -3.10 tests/xxx.py` 可跑，
用 `-m unittest` 时必须带 `PYTHONPATH=.`，否则报 `Start directory is not importable`。

### 4.1 关于 `tests/test_live_futures_acceptance_glm.py`

- glm 的 **R2 用例已全部通过**（含回退计分、整轮摘要契约）。
- 仅剩 **4 项 R3 用例**失败，全在 `R3FieldCompletenessTests`：
  `test_r3_short_oi_history_is_not_ok`、`test_r3_trailing_zero_oi_not_masked_by_fresh_ls`、
  `test_r3_inner_zeros_do_not_break_completeness`、`test_r3_normal_complete_sample_unchanged`。
- 这正是 `reports/live_futures_r2_final_review.md` 的 **glm 阻塞**（历史不足 / 尾零掩盖完整性），
  **属 glm 本轮待做，与 ds 改动无关**。

---

## 五、ds 侧声明

- **本轮不再编辑核心文件**（`binance_box_strategy.py`、`tests/test_live_futures_ds.py`）。
- 未请求真实行情、未清生产暂停、未换出口、未动后台归档采集（PID 29652 保持运行）、未加缓存平台。
- 未改评分 / 权重 / 门槛 / 币池 / UI / 数据完整性逻辑 / 整轮返回结构。
- 不宣布原封禁根因已确定；不把测试通过写成实时恢复或收益达标。
- **ds 不自行试扫**，真实单轮验收方案由主代理验收后再决定。
