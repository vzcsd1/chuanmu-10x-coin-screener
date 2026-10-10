# K3 结果区「海下观测舱」交付报告

> 2026-10-09 主代理后续批注：用户实操反馈海洋“一卡一卡的”，本报告的性能结论不作为已通过用户体验验收。`Ocean.measureFPS` 测的是浏览器回调间隔，不能独立证明海面及新增水光的实际更新/呈现流畅；下文原始数字保留。接续修复与证据标准见 `tasks/glm53f_ocean_performance_20261009.md`，不扩大真实行情测试授权。

日期：2026-10-09。任务书：`tasks/kimi_k3_ocean_results_20261009.md`。
**本次未发生任何真实行情调用**：全程只访问本机 `127.0.0.1:18765` 独立演示服务（已核 `mode=demo`）与本地文件，唯一一次 `POST /api/scan` 是演示假扫描。

## 1. 选定画面（≤6 行）

结果面板是一只海下观测舱：深海蓝青基底上，水面光穿过水体在面板内部形成缓慢聚散游移的光纹与斜向透光；卡片像浸在不同深度的水里，带轻微视差与指针反光；详情开合是同一空间里下一层的展开；结果到达时先有一道潮光扫过舱体，随后卡片分组显现；全部收起后干净看海。

## 2. 修改文件（仅视觉接线，未动评分/门槛/币池/后端/查询逻辑）

| 文件 | 改动 |
|---|---|
| `ui/static/result-fx.css` | 新增。舱体水光层、卡片材质与内光影、详情开合过渡（max-height/opacity/padding 约 250–450ms）、整批收起网格过渡、复制水光反馈、静态/减动态降级 |
| `ui/static/result-fx.js` | 新增。单个共享 Canvas 绘制舱体水光（非每卡一个上下文）；MutationObserver 接线结果到达→潮光一扫+卡片分组入场；IntersectionObserver 屏外卡片按需入场；指针反光跟随；`fx-still`/减动态/后台标签/整批隐藏时停绘 |
| `ui/static/index.html` | 三处接线：引入 `result-fx.css?v=1`、`result-fx.js?v=1`；`#results` 外包 `.results-shell`（容纳整批收起网格过渡与 hover 上移） |

装饰层全部 `pointer-events:none`，不挡选择/复制/滚动/按钮。工作区里其他未提交改动属早前任务，本次未触碰。

## 3. 隔离证据

- 端口：18765（任务书优先端口，启动时已核空闲）；`CHUANMU_UI_DEMO=1` 独立会话启动，未使用 `start_ui.ps1`，未接管真实服务。
- 假扫描路径：`ui/app.py` demo 分支走 `_demo_scan()`（夹具 `ui/fixtures/demo_rows.json` + 内置步进：有结果→空→失败循环），`demo` 模式下不写真实结果文件、`last` 恒为 null。后端真实入口在本次会话从未被调用。
- mode 核对：浏览器侧每次查询相关动作前，同源 `GET /api/state` 断言 `mode === "demo"`，非 demo 立即 `process.exit(2)` 中止（验证脚本实测输出 `mode=demo status=...`）；请求拦截链路上每次仍先回源复核 mode，非 demo 中止。不只信启动参数/横幅。
- 网络边界：请求拦截只放行 `127.0.0.1:18765`，其余一律 abort；无任何 Binance/OKX/CoinGecko 请求、无 ping/连通性探测。
- 缺态补假：100 张合成卡由夹具派生（改 symbol/score 等字段）；「失败保留旧结果」因 demo 的 `last` 恒 null，用拦截把真实 demo 响应改写为 failed+last（mode 原样保留）合成。**未退回真实接口补任何状态。**
- 未清暂停、未改代理/额度/扫描间隔，未重启用户进程；演示服务为本任务自建自停。

## 4. 状态覆盖（截图见 `k3/shots/`）

| 状态 | 证据 |
|---|---|
| 正常结果（5 卡全景） | `v_full5.png`、`after2_results5.png` |
| 约 100 张合成卡 | `after2_top100.png`、`after2_scrolled100.png` |
| 裁掉所有边框后面板/单卡内部 | `v_noborder_panel.png`、`v_noborder_card.png`（水光与层次仍在，主题不依赖边框） |
| hover 指针反光 | `v_hover.png` |
| 详情开合连拍 130/260/480ms | `v_detail_t1/t2/t3.png`（连续过渡无跳变）；快速开合×3 `v_detail_rapid.png`（无残影、按钮可用） |
| 复制反馈 | `v_copy_flash.png`（局部水光一闪，无声） |
| 全部收起/展开 | `v_collapse_mid.png`、`v_collapsed.png`（干净看海）、`v_expanded.png` |
| 暂停动态 | `v_still.png`（静态模式下详情开合实测可用，随后恢复动态） |
| 空结果 | `v_state_empty.png`（同一舱体材质） |
| 查询失败 | `v_state_failed.png` |
| 失败保留旧结果 | `v_stale_failed.png`（拦截合成，徽章+通知+旧卡均在） |
| 过滤 | `after2_filter100.png`（`SYN01` → 10 张可见） |

录屏：`k3/result_fx_demo.mp4`（21 秒，1920×1080，48fps H.264，12MB）。内容：静止水光 3s → hover 两卡反光 → 详情开合 → 全部收起看海 → 全部展开 → 平滑滚动 → 回顶静止。帧源为 CDP `Page.screencast`（1013 帧），本地 OpenCV 合成——因本机精简版 ffmpeg 缺 image2/mjpeg，puppeteer `page.screencast` 的 webm 路线失败，已改用此离线管线。

## 5. 性能观察（同机同窗口 1920×1080，Edge headless，`--enable-gpu`，deviceScaleFactor=1）

测量方式：静止场景用页面自带 `Ocean.measureFPS(5s)`；滚动场景 `window.scrollTo` 步进 + rAF 间隔统计。

| 场景 | 改前（13:40） | 改后（13:51） | 改后复测（14:09） |
|---|---|---|---|
| 5 卡静止 | — | — | avg 59.9 / min 59.5 |
| 100 卡静止 | — | — | avg 59.9 / min 59.5 |
| 100 卡滚动最差帧 | min 11.2 | min 14.1 | avg 59.1 / min 11.9 |

滚动最差单帧在 11–14 间波动，改前即存在（来自滚动重排/入场叠加），装饰层未带来可测劣化；静止场景满帧。console 无错误无警告。100 卡过滤、详情开合实测响应正常。

回归测试：`node ui/test_app_poll.js` 40/40 通过；`py -3.10 -B ui/test_ui.py` 25/25 通过。

## 6. 自检对照（任务书第 5 节六问）

遮掉边框仍有海潮主题（见 `v_noborder_*`）；停鼠标后水光持续（录屏开头/结尾静止段）；文字数字稳定不漂浮；同轮轮询不重播入场（app.js 指纹机制 + result-fx 只在新批次播潮光，test_app_poll F 组覆盖）；全部收起后干净看海（`v_collapsed.png`）；动态关闭后完整可用（`v_still.png` + 静态下开合实测）。

## 7. 未验证项

- puppeteer 原生 `page.screencast` webm 产出失败（本机 ffmpeg 精简构建不支持），已用 CDP 帧 + OpenCV 的 mp4 替代；webm 版本未交。
- 真实扫描下的展示未验证——这正是任务书要求，不补测。
- 触屏无 hover 路径未实机测试（代码上反光仅为 hover 增强，开合/过滤不依赖 hover）。
- 滚动最差帧（min≈11.9）改前已存在，本次未做根治（不属视觉美化范围）。

## 8. 最小撤回

删 `ui/static/result-fx.css`、`ui/static/result-fx.js`；`index.html` 去掉两行引入、把 `#results` 从 `.results-shell` 里放回原位即可，无其他耦合。
