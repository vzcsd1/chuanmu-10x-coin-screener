# 实时合约取数请求预算（ds4.1f · 2026-10-07）

> 全部为**离线统计**：用注入假取数跑真实 `public_selection` 调用链，不联网、不写真实 results/。
> 复跑：`PYTHONPATH=. py -3.10 tests/test_live_futures_ds.py`

---

## 一、端点与官方规则（查阅日期 2026-10-07）

### 1.1 `/futures/data/*` —— 本次关键

| 端点 | Request Weight | 独立限制 |
|---|---:|---|
| `GET /futures/data/openInterestHist` | **0** | **IP rate limit 1000 requests/5min** |
| `GET /futures/data/topLongShortPositionRatio` | **0** | **IP rate limit 1000 requests/5min** |
| `GET /futures/data/globalLongShortAccountRatio` | **0** | **IP rate limit 1000 requests/5min** |

来源（官方 legacy 文档，逐页核对，均含原文 "IP rate limit 1000 requests/5min"）：
- `https://developers.binance.com/legacy-docs/derivatives/usds-margined-futures/market-data/rest-api/Open-Interest-Statistics`
- `https://developers.binance.com/legacy-docs/derivatives/usds-margined-futures/market-data/rest-api/Top-Trader-Long-Short-Ratio`
- `https://developers.binance.com/legacy-docs/derivatives/usds-margined-futures/market-data/rest-api/Long-Short-Ratio`

**要点**：这三个端点 **权重是 0**——它们**不消耗** `/fapi/v1` 的 REQUEST_WEIGHT，
但有**自己的一条 1000 次 / 5 分钟 / IP 的额度**。只看 `/fapi/v1` 的 2400 权重会完全漏掉它。

### 1.2 现货域（官方 GitHub 文档）

| 端点 | Weight | 来源 |
|---|---:|---|
| `GET /api/v3/exchangeInfo` | 20 | `raw.githubusercontent.com/binance/binance-spot-api-docs/master/rest-api.md` |
| `GET /api/v3/ticker/24hr`（省略 symbol） | 80 | 同上 |
| `GET /api/v3/klines` | 2（固定，不按 limit 分档） | 同上 |

### 1.3 合约域（ccxt 4.5.77 源码，本机已安装依赖）

`C:\Users\Administrator\AppData\Local\Programs\Python\Python310\lib\site-packages\ccxt\binance.py`
（`fapiPublic` / `fapiData` 段，行 878–920）

| 端点 | cost（= 权重） |
|---|---:|
| `fapi/v1/exchangeInfo` | 1 |
| `fapi/v1/ticker/24hr`（无 symbol） | 40 |
| `fapi/v1/klines`（limit ≤ 99） | 1 |
| `fapi/v1/openInterest` | 1 |
| `fapi/v1/fundingRate` | 1 |
| `fapiData/openInterestHist` | 1（ccxt 内部记账；**官方权重 0**） |
| `fapiData/topLongShortPositionRatio` | 1（同上） |

> ccxt 官方文档站 2026-10 已改为 SPA，直接抓取返回空；合约侧权重以**已安装依赖源码**为准，
> 与官方 legacy 文档核对一致（`/futures/data/*` 官方权重 0 见 1.1）。

### 1.4 IP 权重上限（**未取得官方直接引用，标注待核实**）

- 现货 `REQUEST_WEIGHT`：官方文档站改版后未能抓到确切数字；第三方（deepwiki 等）一致引用 **6000/min**。**标未知，不据此下结论。**
- 合约 `REQUEST_WEIGHT`：同上，第三方引用 **2400/min**。**标未知。**
- 影响：**本次结论不依赖这两个数字**——瓶颈在 1.1 的独立额度（权重 0，与它们无关）。

### 1.5 429 / 418 规则（官方，已取得原文）

- 超出限速 → **429**；收到 429 后**不退避**继续请求 → **418 自动封禁**。
- 封禁时长**随违规次数递增，2 分钟 ~ 3 天**；429/418 均带 `Retry-After`。
- **限速基于 IP**（不是 API Key）。
- 来源：`developers.binance.com/docs/binance-spot-api-docs/rest-api/general-api-information`（与 USDⓈ-M general-info 一致）。

---

## 二、单轮扫描实际请求（N = 通过闸门的币数）

**实测方法**：`tests/test_live_futures_ds.py::RequestBudgetTests` 用真实 `public_selection` + 假取数对象计数。

**N 的实测值**：历史真实扫描落盘的 `results/candidates-*.csv` 行数
（= `public_selection` 实际进入评分循环的币数）：

| 文件 | 行数（减表头） |
|---|---:|
| candidates-20261005-134652 | **217** |
| candidates-20261005-112232 | 211 |
| candidates-20261004-172121 | 8 |
| 配置上限 `max_symbols` | 400 |

→ **常用币池 N ≈ 210–220**，配置上限 400。

### 2.1 单轮请求（缓存冷启动，N = 218）

| 域 | 端点 | 次数 | 权重 |
|---|---|---:|---:|
| 现货 | exchangeInfo | 1 | 20 |
| 现货 | ticker/24hr | 1 | 80 |
| 现货 | klines | 218 | 436 |
| 合约 | exchangeInfo | 1 | 1 |
| 合约 | ticker/24hr | 1 | 40 |
| 合约 | **openInterestHist** | **218** | **0** |
| 合约 | **topLongShortPositionRatio** | **218** | **0** |
| 合约 | openInterest / fundingRate（仅展示行 M≈20） | 40 | 40 |

| 口径 | 单轮 | 上限 | 占用 |
|---|---:|---:|---:|
| 现货权重 | **536** | 6000（待核实） | 8.9% |
| 合约权重 | **81** | 2400（待核实） | 3.4% |
| **`/futures/data/*` 次数** | **436** | **1000 / 5min** | **43.6%** |

### 2.2 上界（N = 400，配置满）

| 口径 | 单轮 | 上限 | 占用 |
|---|---:|---:|---:|
| 现货权重 | 900 | 6000 | 15% |
| 合约权重 | 841 | 2400 | 35% |
| **`/futures/data/*` 次数** | **800** | **1000 / 5min** | **80%** |

### 2.3 其他情形（实测）

| 情形 | 变化 |
|---|---|
| 同小时第 2 轮（缓存命中） | 合约历史接口 **0 次**（只缓存 `status=ok`） |
| 进程重启（内存缓存清零） | 回到 **2N 次**——**这是 5 分钟窗口内重复打满的来源** |
| 大户接口失败走回退口径 | 每币 **+1 次**（N → 3N = 654 次） |
| 合约域暂停 | 合约历史 **0 次**，现货 K 线照常 N 次 |
| 初始化失败 | `load_markets` 各域 1 次，封禁后**不再重试**（修复后也不切换出口） |

---

## 三、结论：**在本机自限生效后**单轮不超；多轮会超

> ⚠️ **2026-10-07 R2 更正**：早前写的"单轮必安全"是**过度结论**。436/800 只是
> "无回退调用 + 窗口内无其它用量 + 出口独占"三个假设**同时成立**时的算术。
> 以下任一情形即可让**单轮本身**触限：
> ① 大户接口失败走回退口径 → 每币 +1 次，N → **3N = 654 次**（N=400 时 1200 次，**单轮即超**）；
> ② 5 分钟窗口内已有其它进程 / 上一次重启留下的用量（重启清空的是进程内缓存，**不清零额度窗口**）；
> ③ 共享出口（IP 203.10.99.42）上他人的流量。
> 因此正确表述是"**单轮在典型条件下不超**"，**不是"单轮必安全"**，更不能据此断言本次事故与轮次无关。

- **典型单轮**（N≈218、无回退、窗口干净）：436 次（43.6%）；N=400 时 800 次（80%），均 < 1000。
- **但 5 分钟窗口内：N≈218 跑 3 轮（1308 次）、N=400 跑 2 轮（1600 次）即超。**
- **ccxt 的 `enableRateLimit`（50ms/请求）完全不知道这条独立额度**——436 次请求最快约 22 秒发完，
  代码里**没有任何一处**按 1000/5min 自限（R2 起已补上自限，见 `r2/summary.md`）。
- "间隔几小时一次"降低全天总量，但**不改变单轮突发**，也**不能解释为何会在某个时刻被打封**。

**能认定 / 不能认定**：
- ✅ **能认定**：`/futures/data/*` 的 1000/5min 独立额度未被任何代码覆盖，是本链路最紧的约束。
- ✅ **能认定**：单轮 436–800 次，**5 分钟内多次启动扫描**足以打满。
- ❌ **不能认定**：本次 12:05 的封禁由"K3 测试轮次多"直接造成——UI 有单任务互斥锁（连点返回 409），
  且无 UI 运行日志记录实际轮次；触发者身份**证据不足**。
- ⚠️ **待核实**：封禁长达 **9.86 小时**（官方区间 2 分钟~3 天），提示**重复违规**；
  7897 是**共享出口**（IP 203.10.99.42），同 IP 其他流量无法从本机证据排除。
