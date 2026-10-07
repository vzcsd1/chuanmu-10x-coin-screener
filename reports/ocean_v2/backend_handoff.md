# 后台接线契约 · 观潮 V2 交给 K3

**给谁看**：Kimi K3（做美术、镜头、前端实现）。
**这一份只讲「能调用什么、返回什么、什么时候用」，不改策略、不改后台。**
**只读核对，未修改任何前端与后台文件；未触发真实全市场查询。**

最后更新：2026-10-07（ds4.1f）

---

## 1. 结论先说（K3 只需记这 6 条）

1. **后端现成可用，不需要你改 `ui/app.py`。** Flask 薄适配层已跑通：调用现有 `川沐十倍币筛选.scan()`，不改评分、不改排序。
2. **你只碰 `ui/static/`。** 契约稳定，直接按本文件接。
3. **两个读接口 + 一个写接口**：`GET /api/state`（轮询状态）、`GET /api/terms`（术语提示）、`POST /api/scan`（一键查询）。
4. **业务动作只有一个**：点按钮 → `POST /api/scan` → 轮询 `GET /api/state` 到 `status != "running"`。**查询立即开始，动画不能延迟它。**
5. **状态机 5 态**：`idle / running / success / empty / failed`。六种「数据状态」里，`deriv_status` 是**卡片内**的合约数据状态，跟扫描状态机不是一回事（见 §5）。
6. **演示模式能覆盖全部状态、且不写真实结果**，K3 做视觉样片一律用演示模式（`-Demo`），不要拿真实扫描做动效测试。

---

## 2. 路由清单（实际存在，已逐个复跑确认）

| 方法 | 路径 | 用途 | 成功 | 失败/边界 |
|---|---|---|---|---|
| GET | `/` | 返回 `ui/static/index.html` | 200 | — |
| GET | `/api/state` | 取当前状态快照（前端主轮询） | 200 JSON | 无鉴权要求（只绑 127.0.0.1） |
| GET | `/api/terms` | 取术语大白话（`{id: {zh, plain}}`） | 200 JSON，读不到返回 `{}` | 读不到不报错，前端应自动隐藏提示 |
| POST | `/api/scan` | 启动一轮查询 | 200 `{ok:true, started_at}` | **409 `{ok:false,error:"running"}`**＝已有任务在跑；**403**＝跨站来源被拒 |

**服务绑定**：`127.0.0.1:8765`（`CHUANMU_UI_PORT` 可改）。仅本机可访问。

**启动方式**（K3 用演示模式做样片）：
```powershell
powershell -ExecutionPolicy Bypass -File .\start_ui.ps1 -Demo   # 演示模式：内置样例，不联网、不写真实结果
powershell -ExecutionPolicy Bypass -File .\start_ui.ps1         # 真实模式：调用现有筛选器
```
停止：在启动窗口按 Ctrl+C。

---

## 3. `POST /api/scan` —— 一键查询

- 请求体：**无**。空 POST 即可。
- **单任务锁**：`running` 期间再点 → 409，前端应提示「上一轮还在进行中」并保持按钮禁用。
- **不阻塞**：立即返回 200，扫描在后台线程跑。**真实扫描可能几分钟**，前端必须轮询而不能等响应。
- **动画纪律**：收到 200 即代表业务已开始，转场动画不得推迟这次请求的发出。

响应示例：
```json
{ "ok": true, "started_at": 1791381000000 }
```
409 / 403 均为 JSON 或空体，前端按状态码分支即可。

---

## 4. `GET /api/state` —— 状态快照（前端轮询主接口）

### 4.1 顶层字段（**实测**，共 10 个）

| 字段 | 类型 | 含义 | K3 注意 |
|---|---|---|---|
| `mode` | `"demo"｜"real"` | 是否演示模式 | **必须显式显示演示横幅**，不能让演示数据冒充真实 |
| `status` | `"idle"｜"running"｜"success"｜"empty"｜"failed"` | 扫描状态机 | 决定整个画面状态 |
| `started_at` | ms 时间戳｜null | 本轮开始时间 | `running` 时用来显示已耗时 |
| `finished_at` | ms 时间戳｜null | 本轮结束时间 | 文案用 |
| `server_now` | ms 时间戳 | 服务端当前时间 | 可作时钟基准 |
| `error` | string｜null | 失败原因（**如实呈现，不可吞**） | `failed` 时显示 |
| `rows` | 数组｜null | **本轮**结果（`idle` 时为 null） | 仅 `success` 直接用 |
| `last` | 对象｜null | **上次成功**结果 `{status,finished_at,count,rows}` | `failed`/`running`/`idle` 时用它做「保留旧结果」 |
| `min_score` | int｜null | 当前评分门槛（实测 5） | 只显示，**不可改**，不可当作上涨概率 |
| `paused` | 数组 | 限流暂停中的域 | 见 §4.4 |

### 4.2 前端轮询节奏（现有实现，可直接沿用）

| 场景 | 间隔 |
|---|---|
| `running` | 1500 ms |
| 其他 | 12000 ms |
| 请求失败 | 5000 ms 后重试 |

### 4.3 六种展示状态 → 用哪些字段（**这是 K3 最需要的映射表**）

| 展示状态 | `status` | 数据来源 | 画面要求 |
|---|---|---|---|
| ① 空闲/首屏 | `idle` | 若 `last` 存在则显示「上次结果」 | 标题+查询入口**短促显现**，不强迫等片头；不自动扫描 |
| ② 查询中 | `running` | `started_at`；若 `last` 有则同时显示旧结果 | 转场结束即**安静等待**；显示已耗时；**不循环俯冲、不伪造进度条**；无结果也保持同一构图 |
| ③ 成功 | `success` | `rows` + `min_score` | 卡片 0.5–0.9 s **分组显现**，与背景同方向同节奏；按钮/文字稳定可读 |
| ④ 零结果 | `empty` | `rows == []` | **同等级构图**，不用假标的撑场面 |
| ⑤ 失败 | `failed` | `error` + `last` | 如实显示错误；**保留旧结果**并标注「上次结果」，绝不覆盖 |
| ⑥ 旧结果/刷新恢复 | `idle`/`running`/`failed` 且 `last` 非空 | `last.rows` / `last.finished_at` | 明确标注「上次结果」；页面刷新后自动恢复（结果落盘 `results/ui/last_result.json`） |

> 判别口诀：**`status` 管「现在在干什么」，`last` 管「上一次剩下了什么」。**
> `failed` 时 `rows` 为 null —— 想要旧结果只能读 `last`，不要读 `rows`。

### 4.4 `paused` 数组（限流暂停，只读展示）

```json
[ { "domain": "futures", "until_ms": 1791381411631, "reason": "DDoSProtection: ... 418 ..." } ]
```
- `domain` 取值：`spot`（现货域）/ `futures`（合约域）。
- 实测当前**合约域正在限流**（写本文时 `paused` 非空）——这正是「合约连接失败降级、现货照常出池」的既有设计，**不是界面 bug**。
- K3 用法：显示成提示徽章即可；**不得因为 paused 就停止背景或改变筛选**，也不要把暂停时长做成倒计时进度动画去暗示行情。

---

## 5. 卡片数据契约（`rows[i]`，实测 36 个字段）

**卡片内容与排序不可改**（任务书明令）。以下为实际字段，供你摆版式。

| 分组 | 字段 | 单位/说明 |
|---|---|---|
| 身份 | `symbol` | 形如 `RIVER/USDT`。**币名 = `symbol.replace("/USDT","")`** |
| 评分 | `score` `max_score`（18）`score_kline` `score_deriv` | **规则得分，不是上涨概率**；必须带此说明 |
| 辅助分 | `extra_score` `extra_conditions` | **辅助分绝不参与门槛**（勿类比「附加题计入总分」） |
| 市场 | `market_scope` `market_cap` `spot_quote_volume` `futures_quote_volume` `okx_quote_volume` `volume_cap_ratio` `okx_contract` | 金额类可能为 null |
| 合约 | `has_futures` `deriv_status` `deriv_as_of` `oi_chg_1d` `oi_chg_3d` `open_interest_value` `oi_cap_ratio` `ls_top` `ls_chg_3d` `ls_source` `funding_rate` | 见下方格式陷阱 |
| K 线 | `c_trend` `breakout` `c_breakout` `box_width_pct` `volume_ratio` `volume_24h_growth` `volume_4h_growth` `c_v24up` | 布尔/null 混合 |
| 命中原因 | `conditions`（逗号分隔字符串）`conditions_met` `extras_deferred` | 前端已按 `,` split |

### 5.1 六个格式陷阱（**错了数字含义就错**，现有 app.js 已按此实现，请照抄语义）

| 字段 | 数据形态 | 正确显示 |
|---|---|---|
| `oi_chg_1d` / `oi_chg_3d` | **小数**（0.156 = 15.6%） | **× 100** 再加 % |
| `percentage_24h` | **已是百分数**（如 8.4 = 8.4%） | **不要再 ×100** |
| `box_width_pct` | 已是百分数 | 不要再 ×100 |
| `funding_rate` | 小数（0.0001） | ×100 显示为百分比，位数多 |
| `ls_top` | 倍数（如 0.82） | 直接 toFixed(2)；`ls_source` 含「回退」时标注回退口径 |
| 金额（`market_cap` 等） | 原始数值或 **null** | 按 K/M/B/T 缩写；**null 显示「暂无」，绝不显示 0** |

### 5.2 `deriv_status` —— 卡片内的合约数据状态（≠ 扫描状态机）

| 值 | 含义 | 显示 |
|---|---|---|
| `ok` | 正常 | 绿点 |
| `partial` | 部分缺失 | 黄点 |
| `failed` | 获取失败 | 红点 |
| `paused` | 限流暂停 | 灰点 |
| `no_futures` | 该币无合约 | 无点 |

判定：`row.has_futures ? row.deriv_status : "no_futures"`。
**取不到的字段是 null，不是 0** —— 缺失不冒充证据。

---

## 6. `GET /api/terms` —— 术语提示

返回 `{ "<term_id>": { "zh": "中文名", "plain": "大白话" } }`，来源是项目**唯一真源** `glossary/terms.yaml`（经 `tools/glossary.py` 解析），读失败返回 `{}`。
覆盖 id：`min_score extra_score oi_chg_1d oi_chg_3d ls_top funding_rate market_cap quote_volume c_trend box deriv_status oi`。
用法：悬停提示用 `title`；**不要自己另写一套解释**（与项目规则冲突）。

---

## 7. 已验证的接口行为（25 项隔离测试全绿）

```powershell
py -3.10 ui\test_ui.py     # → 25 项通过，0 项失败
```
覆盖：重复点击只触发一次（409）、零结果正确保存、失败保留旧结果且不覆盖落盘文件、NaN/Infinity→null、刷新恢复上次结果、演示三态（有结果/零结果/失败）且不写真实结果、跨站 POST 拒绝、numpy 标量还原。

**重要**：这些测试**注入假 scan，不联网、不碰归档采集**，可随时复跑；真实全市场查询**未在本次执行**。

---

## 8. 运行环境与能力（**实测，仅报观察到的事实**）

| 项 | 实测值 | 对你的意义 |
|---|---|---|
| OS | Windows 11 专业版 | — |
| CPU | 13th Gen Intel Core i5-13490F | — |
| 显卡 | **NVIDIA GeForce RTX 4060**（驱动 32.0.15.9186）；另有 `GameViewer Virtual Display Adapter` | **支持 WebGL/Three.js**，可跑真实三维海面 |
| 桌面分辨率 | **1440×900**（主屏，59 Hz） | ⚠️ 比 1080p **更小**；任务书目标「1080p 稳定」按此实测 |
| 浏览器 | Chrome **155.0.8059.39**、Edge **154.0.4258.62** | 两个都能用于验收 |
| Python / Flask | 3.10.9 / 3.1.3（`ui/requirements-ui.txt`：`flask>=3.0,<4.0`） | 后端不需新依赖 |

> **不做未经测量的帧率承诺。** 显卡型号只能说明「能跑 WebGL」，**不等于**跑满 60 fps。实际帧率必须由 K3 在浏览器里测（帧率/掉帧记录），本文件不背这个结论。

---

## 9. K3 的硬边界（来自任务书，逐条保留）

- **只写 `ui/static/`、必要的本地视觉依赖、`ui/README.md`**；不改 `ui/app.py`、核心策略、下载器、公共文档。
- 卡片内容与排序**不变**；评分**不是**上涨概率；不截掉多倍机会；刷新**不重跑**扫描。
- 演示数据**不得**写入真实结果路径（后端已用 `demo` 标志隔离，你只要别绕过它）。
- 镜头与效果模块**独立于请求逻辑**；限制像素密度、后台标签暂停绘制；**重复查询不创建新绘制循环**。
- 提供**暂停动态开关 + 静帧后备**，按钮/文字始终可读；避免持续摇镜、滚动劫持、强制片头、随海浪摇晃的表格。
- **接口若确实缺字段**：反馈给主代理 / ds，**不重建后台**。

---

## 10. 接线阻断排查（如你遇到问题，先按此定位）

| 现象 | 实际原因 | 处置 |
|---|---|---|
| 点按钮没反应 | 需要**空 POST**；带 JSON body 也可能正常，但最稳是 `fetch("/api/scan",{method:"POST"})` | 照现有实现 |
| 一直 409 | 上一轮还在跑（真实扫描可几分钟） | 禁用按钮 + 提示，不要重复发 |
| 卡片数字大 100 倍或小 100 倍 | 踩了 §5.1 的 ×100 陷阱 | 按表区分 `oi_chg_*` 与 `percentage_24h` |
| 失败后画面空了 | 读了 `rows`（failed 时为 null），没读 `last` | 改用 `last.rows` |
| 悬停没有术语 | `/api/terms` 返回 `{}` | 自动隐藏提示，不是错误 |
| 合约数据红/灰点 | `deriv_status` 为 failed/paused，或当前合约域限流 | **正常降级**，现货仍出池，不是 bug |

---

## 11. 本次范围声明

- **未修改**任何前端与后台文件；**未新增** Web 服务；**未中断**归档采集。
- 归档采集进程 **PID 29652 存活**（`tools/archive_queue_ds.py run --version v2 --ramp auto`，启动于 11:20:35），锁文件在位 —— **未触碰**，按原任务继续。
- 产出仅本文件：`reports/ocean_v2/backend_handoff.md`。
- **交接完成**：ds 侧接线底座已确认可用，K3 可直接按本文件实现视觉层。
