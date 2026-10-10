# B4 第 1 步 · 时间输入契约（ds，2026-10-09）

本文件回答一件事：**归档里的每个字段，原始长什么样、单位是什么、规范时间怎么定、实际采用哪一个点、凭什么说它当时可得**。
机器可读版是同目录 `input_contract.json`，由 `tools/b4_replay_ds.py contract` 生成（本文件与它内容一致）。

三种时间必须分开，任何时候都不许混为一谈：

| 时间 | 含义 | 本项目能否证明 |
|---|---|---|
| 市场观测时间 | K 线/指标的观测时刻（UTC） | ✅ 可证（就是 K 线的 `open_time`、metrics 的 `create_time`） |
| 公开可得时间 | 这条观测什么时候对外发布 | ❌ **归档未记录**，只能按显式假设处理 |
| 归档下载时间 | 我们什么时候把它下到本地 | ✅ 可证（v3 队列完成于 `2026-10-08T12:24:50Z`） |

> 因此本批**不能**称为"完整历史线上重放"——只能称为"按显式假设的离线接入"。

---

## 1. 现货 1h K 线

- **来源**：`data/archive_raw/spot/klines/<SYMBOL>/1h/<SYMBOL>-1h-<YYYY-MM>.zip`（月分片）
- **CSV**：无表头（个别月份可能有；解析按"首列能否解析成时间"判定并跳过表头行）
- **原始列**（币安标准 12 列）：
  `open_time, open, high, low, close, volume, close_time, quote_volume, count, taker_buy_base, taker_buy_quote, ignore`
- **本项目只使用**：`open_time, open, high, low, close, volume`

| 字段 | 原始值/单位 | 规范时间 | 实际采用点 |
|---|---|---|---|
| `open_time` | epoch，**毫秒**（同文件可能出现**微秒**） | UTC 毫秒：数值 `> 1e14` 视为微秒并 `/1000`（逐行归一，沿用 `research/02_detect_pumps.py` 的规则） | 只采用 `open_time + 1h <= 决策时刻` 的 K 线 |
| `open/high/low/close` | 计价币（USDT） | — | 取**最后一根已闭合** K 线 |
| `volume` | 基础币数量 | — | 同上；量增用 `iloc[:-1]`（不含形成中那根） |

- **线上等价**：`public_selection` 调 `fetch_ohlcv(symbol,'1h',limit=cfg.lookback=240)`，
  df 最后一根是**正在形成**的那根，`box_score` 读 `df.iloc[-2]`。
- **历史接入的正确做法**：窗口取最后 `lookback-1` 根已闭合 K 线，**末尾追加 1 根占位行**代表形成中那根。
  - 占位行的 OHLCV **不参与** `iloc[-2]` 的任何计算（已用测试证明：把占位值换成垃圾值，分数与分项完全不变）。
  - **反例**：只喂闭合 K 线（不加占位）会让 `iloc[-2]` **多退一根**（已用测试固定该差异）。
- **可得性依据**：月分片的发布时刻未记录 → 按"**已闭合即可用**"的保守口径；不假设分片何时发布。

---

## 2. U 本位合约 metrics（5 分钟粒度）

- **来源**：`data/archive_raw/futures/metrics/<SYMBOL>/no_interval/<SYMBOL>-metrics-<YYYY-MM-DD>.zip`
- **CSV**：有表头；5 分钟一行（约 288 行/天）
- **原始列**：
  `create_time, symbol, sum_open_interest, sum_open_interest_value, count_toptrader_long_short_ratio, sum_toptrader_long_short_ratio, count_long_short_ratio, sum_taker_long_short_vol_ratio`

### 字段映射（口径必须一一对上，不得互相替代）

| 本项目字段 | 归档字段 | 线上对应端点字段 | 单位 | 计分？ |
|---|---|---|---|---|
| `oi_value` | `sum_open_interest_value` | `sumOpenInterestValue`（`/futures/data/openInterestHist`） | USDT 名义价值 | ✅ 计分 |
| `oi_amount` | `sum_open_interest` | —（同接口另有） | 基础币数量 | ❌ 只记录 |
| `ls_top` | `sum_toptrader_long_short_ratio` | `topLongShortPositionRatio`（大户**持仓**多空比） | 无量纲比值 | ✅ 计分 |
| `ls_top_account` | `count_toptrader_long_short_ratio` | — | 无量纲比值 | ❌ **禁止冒充大户持仓比** |
| `ls_global` | `count_long_short_ratio` | `globalLongShortAccountRatio`（全市场**账户**比） | 无量纲比值 | ❌ **禁止冒充大户持仓比** |

**实证差异**（`structure_cases.json` → `ls_ratio_variants`，AAVEUSDT 2024-06-01 12:00 UTC）：

| 口径 | 值 |
|---|---:|
| 大户**持仓**比（计分用） | **1.184** |
| 大户**账户**比（禁用） | 2.509 |
| 全市场**账户**比（禁用） | 2.431 |

三者差一倍以上 —— 混用会直接改变 `ls_top < 1` 这条 3 分条件的判定。
**OI 数量 vs 价值**（同一天同一小时）：数量 `170,783.6`，价值 `17,918,767.7`（差约 105 倍），同样不可互换。

### 时间与采用点

- `create_time` 是**字符串** `"YYYY-MM-DD HH:MM:SS"`（无时区标记），本项目**按 UTC 解析**为毫秒 epoch。
- 线上用 **1h 周期**端点；归档是 5m → **只取整点行（`minute==0`）**近似该 1h 序列。
- **采用点规则（与线上一致）**：先**滤掉不可用观测**（非有限或 `<=0`），再取**最后一根可用观测**作为采用点；
  比采用点更新但不可用的观测数单独记为 `oi_stale_tail` / `ls_stale_tail`。
- **窗口**：`oi_chg_1d` = 采用点 / (采用点−24h) − 1；`oi_chg_3d` = 采用点 / (采用点−72h) − 1；
  `ls_chg_3d` = 采用点 − (采用点−72h)（仅展示，不计分）。
- **状态语义**：`ok` = `oi_chg_1d` 与 `oi_chg_3d` 均可算**且** `ls_top` 可用；`partial` = 二者之一可用；`failed` = 都不可用。

### 显式假设与未查项

- ⚠ **假设**：整点观测在整点即可得（`--deriv-lag-hours 0`）。公开延迟未知，**这是假设不是证据**。
- ⚠ 线上端点只保留最近 30 天；归档可覆盖更早 → 属"归档更全"，**不改口径**。
- ❌ **未查**：没有逐点比对 metrics 整点行与线上 `/futures/data/*` 1h 端点是否数值相等（需联网，本批禁止）。

---

## 3. 无法重建的门槛字段（必须计入未知，不能用今天的值倒填）

| 字段 | 为什么不可重建 | 处理 |
|---|---|---|
| 历史市值 | 归档里没有历史市值序列 | 标 `unknown`；本批只验证"同一可观察子集（K线+合约）上的评分" |
| 当时交易资格 | 归档只证明文件存在，不等于当时该现货对在交易 | 标 `unknown` |
| 退市/更名/换币 | 未做身份映射（如 KLAY→KAIA） | 记入未知；同名匹配是**保守下界** |
| 分片发布时刻 | 未记录 | 按显式假设处理 |

**结论**：本批可以证明"历史输入能按当时可知信息送入当前评分并得到可解释、可重算的结果"；
**不能**证明"复现了线上全流程"，也**不能**据此判断策略优劣。

---

## 4. 数据覆盖边界（实测，详见 `coverage.md`）

- **metrics 全市场覆盖起点 = 2021-12-01**（2020 年仅 BTCUSDT 有 122 天）。→ **2021-12 之前的衍生侧不可重建**。
- 现货 1h 归档覆盖 756 个标的；其中 473 个同时有 metrics，283 个没有。
- 队列 1,000,272 行 → **逐份对账 missing = 0**（每一行都能在本地找到权威路径的文件）；
  另有 29 份本地文件不在队列内（早期非 ASCII 币种路径塌缩遗留）+ 2 份 `.stale-` 重命名文件。
