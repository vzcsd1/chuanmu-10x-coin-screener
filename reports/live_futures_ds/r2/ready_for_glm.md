# ⛔ 已作废（2026-10-07）——请改用 `reports/live_futures_ds/r3/ready_for_glm.md`

> 本文件是 R2 的旧交接标记。主代理最终验收发现 ds 仍有一个阻塞
> （跨实例登记的新暂停会被旧请求成功后的保存覆盖），ds 已按
> `tasks/live_futures_r2.md` 顶部返修要求修完并重新交付到 **r3/**。
> **r2 的核心文件 SHA256、改动范围与契约均已过期，不要据此接手。**
> 下面内容仅作历史留存，**不再代表当前代码**。

---

# R2 · 交接给 glm（ds 部分已完成）

> 本文件由 **ds4.1f** 在完成 `tasks/live_futures_r2.md` 的 ds 部分后写入。
> 目的：把核心文件现状、测试结果与接口契约交代清楚，交给 glm 接续（glm 负责整轮摘要契约等）。
> **ds 本轮到此为止，不再编辑核心文件。**

---

## 一、核心文件摘要（本轮改动范围）

| 文件 | SHA256 | 行数 | 本轮改动 |
|---|---|---:|---|
| `binance_box_strategy.py` | `34357e353558574b3e0f94b644c3dfa875fd8fb84b804155ea8bae4c90192e34` | 1440 | +194 / −6 |
| `tests/test_live_futures_ds.py` | `3923f3144be10ee35a2117e99f72b44a61ba3ffcca3a17d0cc83ca1d34ed7060` | 596 | +321 |

> 哈希基于写入本文件时的磁盘内容。若 glm 后续编辑 `binance_box_strategy.py`，哈希自然变化，**属预期**。

### 1.1 新增（模块级）

| 符号 | 行 | 作用 |
|---|---:|---|
| `_lock_fh` / `_unlock_fh` | 330 / 341 | 对文件句柄加/解排他锁（Windows `msvcrt` / POSIX `fcntl`） |
| `_StateLock` | 351 | 跨进程互斥上下文管理器；`path=None` 时 no-op；内核崩溃自动释放锁 |
| `_sleep_interruptible` | 391 | 分段睡眠，中断 1 秒内生效 |
| `DATA_QUOTA_LIMIT = 600` / `DATA_QUOTA_WINDOW_MS = 5*60_000` | 833 / 834 | `/futures/data/*` 自限（官方 1000/5min 的 60%） |

### 1.2 `RateLimitState`（行 401）公开 API

| 方法 | 行 | 说明 |
|---|---:|---|
| `load(path)` | 416 | 读暂停表 **与** 额度窗口 |
| `reload()` | 442 | **同时**同步 `domains` 与 `data_quota`（R2 修：原来只同步 domains） |
| `save()` | 450 | 原子写（`.tmp` + `replace`） |
| `should_attempt(key)` | 463 | 暂停/探测冷却判断（内部先 `reload`） |
| `until(key)` | 479 | 暂停期限 |
| `record_pause(key, until, reason)` | 482 | **期限只延长不缩短**（`max(previous, until)`） |
| `mark_probe(key)` | 499 | 标记恢复探测 |
| `clear(key)` | 506 | 无条件清（**仅兼容保留**，新代码请用 `clear_if_expired`） |
| `clear_if_expired(key)` | 511 | **只在已到期时清除** |
| `locked()` | 528 | 跨进程锁上下文管理器 |
| `data_quota_delay_ms(egress)` | 532 | 滑动窗口，返回需等待毫秒 |
| `note_data_request(egress)` | 548 | 仅内存登记 |
| `reserve_data_request(egress)` | 556 | **检查 + 预约 + 落盘**；须在 `locked()` 内调用 |

### 1.3 `RequestGuard`（行 575）

- `key(domain)` → `f"{domain}|{proxy or 'direct'}"`（**未改**）。
- `run(domain, fn, *args, **kwargs)`（行 585）：
  - 暂停中 → 抛 `DomainPaused`；
  - `_LIMIT_ERRORS`（429/418）→ `record_pause` 后**重新抛出**；
  - 成功 → `clear_if_expired(key)`（R2 修：原为无条件 `clear`）。

### 1.4 取数链

- `fetch_deriv_context`（行 841）：小时对齐缓存，只缓存 `status == "ok"`。
- `_fetch_deriv_context_live`（行 874）：`call()` 内 **`while True`**：
  `should_attempt` → `with locked(): reload(); reserve_data_request(egress)` →
  需等待则 `_sleep_interruptible` → **回顶部复查**；三个分支各有 `except _LIMIT_ERRORS` → 返回 `paused`。
- `public_selection`（行 1033）：**已删除结尾的整表 `guard.state.save()`**（防丢失更新）。

---

## 二、状态文件 schema（**已变更，注意兼容**）

```jsonc
{
  "domains":   { "futures|direct": { "banned_until_ms": 0, "probe_at_ms": 0,
                                     "paused_since_ms": 0, "reason": "" } },
  "data_quota": { "futures|direct": [ 1700000000000, 1700000000012, ... ] }
}
```

- `data_quota` 从第一轮的 `{"used": N, "window_start_ms": T}` **改为时间戳列表**（滑动窗口）。
- **已核查**：除 `RateLimitState` 自身外，只有 `ui/app.py::read_pauses()` 读该文件，
  且**只读 `domains`**，不受 `data_quota` 变更影响。**无其它读者。**

---

## 三、测试结果与复跑命令

```bash
cd <项目根>

# ds 全部离线测试（27 项）
PYTHONPATH=. py -3.10 tests/test_live_futures_ds.py
# → Ran 27 tests ... OK

# 只跑双进程用例
PYTHONPATH=. py -3.10 -m unittest tests.test_live_futures_ds.R2CrossProcessTests -v

# 回归（183 项，全绿）
PYTHONPATH=. py -3.10 -m unittest tests.test_request_governor tests.test_candidate_pool \
  tests.test_inventory_manifest tests.test_archive_download_ds tests.test_archive_queue_ds \
  tests.test_archive_content_audit_glm
```

**注意**：`tests/` 目录无 `__init__.py`，直接 `python tests/xxx.py` 可跑；
用 `-m unittest` 时必须带 `PYTHONPATH=.`，否则报 `Start directory is not importable`。

### 3.1 关于 `tests/test_live_futures_acceptance_glm.py` 当前 11 项失败

**与本轮改动无关，属 glm 待实现功能**：

- `KeyError: 'round_quality'`
- `TypeError: run_scan() got an unexpected keyword argument 'round_stats'`
  （`run_scan` 是 glm 测试文件内第 170 行的本地 helper，它把 `round_stats=` 透传给
  `public_selection`，而该形参**当前不存在**）

即 glm 需要在 `binance_box_strategy.py` 上补**整轮摘要契约**（`round_stats` / `round_quality`）。
**ds 未实现、也不应实现它**，因为该文件按任务书约定"先 ds 后 glm"，ds 阶段已结束。

---

## 四、给 glm 的接口契约要点（改动时勿破坏）

1. **额度预约必须走 `reserve_data_request`，且必须在 `with state.locked():` 内**。
   只加 Python 内存锁、或只 `note_data_request()` 不落盘，都会退回"漏算后重复放行"。
2. **不要恢复 `public_selection` 结尾的整表 `guard.state.save()`**——会用陈旧副本覆盖
   其它进程的预约，导致丢失更新。
3. **成功清暂停只能用 `clear_if_expired`**，不得用无条件 `clear`（晚到成功会抹掉新暂停）。
4. **`record_pause` 的期限只延长不缩短**（`max`），不要在别处另写一套取最晚逻辑。
5. **429/418 必须经 `_LIMIT_ERRORS` 传播**：登记暂停后立刻停止，不得继续另一取数分支。
6. **暂停不得靠换出口绕过**：`_connect_domain` 命中 `_LIMIT_ERRORS` 后 `banned=True` 直接
   `break`，只有网络类错误才允许回退。
7. **额度窗口是滑动窗口**，别改回固定窗口（边界处会双突发）。
8. 整轮摘要契约请**只加字段、不改现有 `deriv_status` 取值**：
   `ok / partial / failed / paused / no_futures`（`paused` 与 `no_futures` 必须可区分）。

---

## 五、ds 侧声明

- **本轮不再编辑核心文件**（`binance_box_strategy.py`、`tests/test_live_futures_ds.py`）。
- 未请求真实行情、未清暂停、未换出口、未动后台归档采集（PID 29652 保持运行）、未加磁盘行情缓存。
- 未改评分 / 权重 / 门槛 / 币池 / 整轮返回结构 / `ui/` / 启动脚本。
- 结论按「已修 / 未修 / 需要真实验证」拆分，见 `summary.md` 第三节；
  **不宣布原事故根因已确定**，并已更正旧报告的"单轮必安全""CLI 跨整点双突发"两处过度结论。
