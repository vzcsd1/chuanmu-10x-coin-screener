# 实时合约取数可靠性 · 取证与最小修复（ds4.1f · 2026-10-07）

> 对应任务书 `tasks/ds41f_live_futures_reliability.md`。
> **本轮未请求任何真实行情、未清暂停、未换出口、未动后台归档采集。**
> 证据目录：`reports/live_futures_ds/`；测试：`tests/test_live_futures_ds.py`。

---

## 一、正文先回答任务书的四个问题

| 问题 | 结论 |
|---|---|
| **单次扫描自身是否可能超限？** | **典型条件下不会，但不是"必安全"。** N≈218 时 `/futures/data/*` 436 次（额度 1000/5min 的 43.6%），N=400 时 800 次（80%）。⚠️ 该算术假设"无回退 + 窗口内无其它用量 + 出口独占"；若大户接口失败走回退口径则 N→3N=654 次（N=400 时 1200 次，**单轮即超**），叠加多进程/重启或共享出口同样可能使单轮触限。**详见 `request_budget.md` 第三节 R2 更正。** |
| **依据是什么？** | ① 官方 legacy 文档逐页核实 `/futures/data/*` 三个端点 **Request Weight = 0** 但有**独立「IP rate limit 1000 requests/5min」**；② 用**注入假取数**跑真实 `public_selection` 实测单轮调用数；③ 历史真实扫描 CSV 行数确定 N≈210–220。详见 `request_budget.md`。 |
| **当前出口原因能否认定？** | **只能部分认定。** 能认定：这条独立额度**没有任何代码覆盖**，是本链路最紧约束，**5 分钟内多次启动扫描足以打满**。不能认定：本次 12:05 封禁由"K3 测试轮次多"直接造成——UI 有单任务互斥锁（连点返回 409），且无 UI 运行日志记录轮次；**触发者身份证据不足**。 |
| **实际还缺什么证据？** | ① UI/扫描的**运行日志**（实际轮次与时刻）；② 响应头 `X-MBX-USED-WEIGHT-*` 的**实际额度读数**；③ **共享出口**（IP 203.10.99.42）同时段的其他流量；④ 服务方**首次开始封禁**的时刻（本地记录的是"首次被本程序观察到"）。 |

**不能把本次归因为"K3 测试次数多"**：单任务锁已排除连点并发；真正被证实的是一条
**代码从未覆盖的独立额度**，任何在 5 分钟内启动 ≥3 次扫描的场景都会命中它。

---

## 二、只读取证（未触碰任何接口）

统一 UTC+8。

| 项 | 实测 |
|---|---|
| 暂停文件 | `results/rate_limit_state.json` 键 `futures\|http://127.0.0.1:7897` |
| 记录时间 / 期限 | 记录 **12:05:31.294**；服务方期限 **21:56:51.631**；原错误 418 / -1003 |
| 含义 | **期限不是保证届时必恢复**，也不是无人运行时自动启动扫描的计划 |
| 归档采集 | PID 29652 存活，持续完成整块（**未因该暂停停下**）；`pause_events=0` |
| 两种入口 | 归档走 `data.binance.vision`（文件站），实时筛选走 `fapi.binance.com`；**前者不读后者暂停文件** |
| 文件修改时间 | 只作**关联证据**，不等于触发者身份 |

---

## 三、根因证据

### 3.1 真实 UI 请求链（已追踪）

```
ui/app.py: real_scan()  →  川沐十倍币筛选.py: scan(cfg)
   → base.connect_exchanges(cfg)          # 现货/合约各自 load_markets（分域收窄）
   → base.public_selection(spot, futures, cfg, okx)
        ├─ exchange.fetch_tickers()                    [现货，1 次]
        ├─ futures.fetch_tickers()                     [合约，1 次，走 guard]
        ├─ okx.load_markets() + fetch_tickers()        [OKX，各 1 次]
        ├─ for symbol in N:                            [逐币]
        │     ├─ exchange.fetch_ohlcv(...)             [现货 klines，N 次]
        │     └─ fetch_deriv_context(...)              [合约]
        │           ├─ fapiDataGetOpenInterestHist        ← /futures/data/*
        │           ├─ fapiDataGetTopLongShortPositionRatio ← /futures/data/*
        │           └─ fetch_long_short_ratio_history      ← 回退口径，也是 /futures/data/*
        └─ 第二遍：展示行 fetch_open_interest + fetch_funding_rate [各 M 次]
```

`ScanManager.start()` 有互斥锁：**同时只允许一个扫描任务，连点返回 409**——所以
"多轮"不是靠连点产生的，而是**多次独立启动/重启进程**（每次重启清空进程内 `_DERIV_CACHE`）。

### 3.2 关键机制（官方文档 + 源码）

- `/futures/data/*` 三端点：**权重 0**、**独立 1000 次/5min/IP**（官方 legacy 文档原文）。
- ccxt 4.5.77 `enableRateLimit` 只保证 **≥50ms/请求**，**不感知**该独立额度。
- 结果：单轮 436 次约 22 秒发完 → **5 分钟窗口内第 3 轮即超**。
- 官方封禁规则：429 后退避 → 否则 418；**时长 2 分钟~3 天，随违规递增**。
  本次 **9.86 小时**属偏长，提示**重复违规**，或**共享出口**被他人流量影响（**待核实**）。

### 3.3 按证据分组

| 分组 | 项 |
|---|---|
| **已证实（本轮修复）** | ① `/futures/data/*` 独立额度未被任何代码覆盖；② 代理出口被封后**自动回退直连出口**（换出口）；③ `record_pause` **不取最晚**（短暂停可覆盖长暂停）；④ 现货 `fetch_tickers` **未纳入 guard**（运行时 429 不登记） |
| **已排除** | 权重限制（单轮现货 536/6000、合约 81/2400，远低于）；初始化请求（单轮 2 次，封禁后不重试）；连点并发（UI 互斥锁） |
| **证据不足** | 触发者身份、实际轮次、共享出口影响、服务方首次封禁时刻 |

---

## 四、最小修复（4 处，均在 `binance_box_strategy.py`）

| # | 问题 | 修复 | 失败用例 → 修复后 |
|---|---|---|---|
| 1 | `/futures/data/*` 1000/5min 额度未覆盖 | 新增 `DATA_QUOTA_LIMIT=600` / `DATA_QUOTA_WINDOW_MS=5min`；`RateLimitState` 记录窗口计数并**落盘**（跨进程/重启共享）；每个 data 请求前检查，用满则等到窗口重置 | `DataQuotaTests`（5 项，新增即通过） |
| 2 | 限流后自动回退到另一出口 | `_connect_domain`：命中 `_LIMIT_ERRORS` 后置 `banned=True`，**不再尝试下一个出口**；普通网络错误仍允许回退 | `test_limit_ban_does_not_fall_back_to_direct_egress` ❌→✅；`test_network_error_may_still_fall_back` 保持 ✅ |
| 3 | 同域短暂停覆盖长暂停 | `record_pause` 改 `effective = max(previous, until)` | `test_shorter_pause_must_not_override_longer` ❌→✅；`test_longer_pause_does_override_shorter` 保持 ✅ |
| 4 | 现货 `fetch_tickers` 未纳管 | 改为 `guard.run(SPOT_DOMAIN, exchange.fetch_tickers)` | `test_spot_ticker_ban_is_recorded` ❌→✅ |

**边界遵守**：未改评分函数 / 权重 / 筛选范围 / 门槛 / 中文扫描入口 / `ui/` / 启动脚本 /
归档下载器；未缩币池、未删关键合约字段、未提高门槛、未改策略；**未改扫描返回结构**。

### 改动文件与复跑命令

```
binance_box_strategy.py            # 4 处修复（请求/暂停/额度），约 +60 行
tests/test_live_futures_ds.py      # 新增，16 项（3 项先失败证明缺陷，修复后全绿）
reports/live_futures_ds/           # 本报告与 request_budget.md
```

```bash
# 新测试（16 项）
PYTHONPATH=. py -3.10 tests/test_live_futures_ds.py
# 全量回归
PYTHONPATH=. py -3.10 tests/test_request_governor.py        # 12
PYTHONPATH=. py -3.10 tests/test_candidate_pool.py          #  6
PYTHONPATH=. py -3.10 tests/test_live_futures_acceptance_glm.py  # 12（glm 验收，未改其文件）
PYTHONPATH=. py -3.10 tests/test_inventory_manifest.py      # 16
PYTHONPATH=. py -3.10 tests/test_archive_content_audit_glm.py    # 10
PYTHONPATH=. py -3.10 tests/test_archive_download_ds.py     # 71
PYTHONPATH=. py -3.10 tests/test_archive_queue_ds.py        # 54
```

**测试结果：新增 16/16 通过；回归 181/181 通过（合计 197 项）。**
测试全部使用 `TemporaryDirectory` 与替身对象，**不污染 `results/`、不联网**。

---

## 五、未解决问题

1. **共享出口影响无法从本机证据确认**（7897 出口 IP 203.10.99.42；9.86 小时封禁偏长）。
2. **`/futures/data/*` 额度是按 IP 的**——本机自限无法约束同 IP 的其他进程或他人流量。
3. **触发者与实际轮次无日志**：`ui/app.py` 未记录扫描起止到文件，事后只能靠文件修改时间关联。
4. **`/fapi/v1` 的 IP 权重上限**（2400/6000）**未取得官方直接引用**（文档站改版），标注待核实；
   但本次结论不依赖它。
5. 修复 1 的**落盘窗口**首次上线时窗口为空，**第一次运行仍会发出完整的 2N 次**（属正常，未超限）。

**给 glm 的契约建议（本轮未改返回结构）**：整轮"因额度/暂停导致合约缺失"应与"真实无合约"可区分。
现有 `deriv_status ∈ {ok, partial, failed, paused, no_futures}` + `deriv_as_of` 已能承载；
建议 glm 在验收里**显式核对** `paused` 与 `no_futures` 不得混同，并确认零结果/失败不会被写成正常空态。

---

## 六、多出口（多 IP）方案比较（**仅比较，本轮未实现**）

> 依据任务书："较大架构、第三方付费源、专用出口方案只比较成本、限制、维护和回退，不擅自购买或实现。"

### 6.1 事实：币安限速**确实按 IP**

- 官方原文（spot general-api-information 与 USDⓈ-M general-info 一致）：
  **"限速基于 IP，不是 API Key"**；429/418 均按 IP 记。
- 响应头 `X-MBX-USED-WEIGHT-(intervalNum)(intervalLetter)` 返回的是**该 IP 的已用权重**。
- 本项目**只读公共数据、无 API Key** → **只受 IP 类限制**（订单类限制按账户，不适用）。
- 本链路有**两条**按 IP 的限制：
  ① `/fapi/v1` REQUEST_WEIGHT（2400/min，**待核实**）；
  ② `/futures/data/*` **1000 次/5min**（**已核实**，官方 legacy 文档原文）。

### 6.2 两种用法必须分开

| 用法 | 判定 |
|---|---|
| **A. 被封之后换 IP 继续请求** | ❌ **禁止**。官方规则是"429 后必须退避，继续请求 → 418，**封禁时长递增**"。换 IP 只是让服务端看到"另一个 IP 继续违规"，**不解决根因**（新出口同样会被 2N/轮打满）。也违反本项目两条任务书的硬约束："不得用换 IP/出口绕过已知暂停"。 |
| **B. 平时把正常请求分摊到多个出口** | ⚠️ 技术可行（额度按 IP，多出口线性分摊），但**当前没有必要**，见下表。 |

### 6.3 方案 B 的成本 / 限制 / 维护 / 回退

| 维度 | 说明 |
|---|---|
| 有效性 | 额度按 IP，N 个出口 ≈ N 倍额度；**仅在"单出口确实不够用"时才有意义** |
| 成本 | 当前 7897 是机场（可能已有多节点，无需新增采购）；稳定多出口通常需付费 |
| 限制 | 跨 IP 的同源行为可能触发风控；出口质量（延迟/可用性）参差；额度按 IP 分账后仍可能被同 IP 他人占用 |
| 维护 | 出口探活、轮换策略、失败回退、额度分账，都是**新工程** |
| 回退 | 可随时退回单出口（改动集中在代理选择一处） |
| **收益** | **当前为 0**：修复后单轮 436 次 < 自限 600 < 官方 1000/5min；正常节奏（几小时一次）离上限很远 |

### 6.4 当前判断

- 多 IP **现在解决不了任何已被证实的问题**，反而会**掩盖**"到底是不是自己的流量把额度打满"这个尚未查清的问题。
- **建议：先按修复后方案跑一次真实查询**，用响应头 `X-MBX-USED-WEIGHT-*` 的实际读数判断是否真的需要多出口。
- 只有在**实测确认"单出口单轮正常扫描也会被打"**，或用户明确要求把节奏提高到"5 分钟内多轮"时，才值得启动方案 B 的评估。

---

## 七、一次受控真实查询方案（**交主代理审核后再执行；本轮不联网**）

**前置**：遵守现有暂停期限（**21:56:51.631**）；**到期不等于必恢复**；不探测重试、不清暂停、不换出口。

| 项 | 约定 |
|---|---|
| 执行时机 | 优先**利用用户下一次正常查询**，不为验收额外发起扫描；禁止密集连扫 |
| 唯一进程 | 只允许**一个**扫描进程；执行前确认没有其他 UI/CLI 扫描实例 |
| 币池 / 出口 | 正常币池（N≈218）、正常出口（当前环境配置，**不切换**） |
| 额度预算 | 单轮 `/futures/data/*` ≈ 2N（约 436 次）< 自限 600 < 官方 1000；**5 分钟内不得再发起第二轮** |
| 停止条件 | **遇 429/418 立即保存并停止**，不探测重试；记录错误原文 |
| 记录内容 | ① 请求数量与总耗时；② 可取得的响应额度信息（`X-MBX-USED-WEIGHT-*`）；③ 合约字段覆盖（`deriv_status` 分布、`deriv_as_of`）；④ 数据观测时间 |
| 后续节奏 | 按用户原定**几小时一次**观察下一轮；如仍受限，保留记录并停止扩展 |

**不得**以离线全绿或单次成功声称持续可用；**不得**声称盈利已验证。
