# 海洋 UI「一卡一卡」性能修复报告

- 任务书：`tasks/glm53f_ocean_performance_20261009.md`
- 执行日期：2026-10-09
- 测量环境（前后完全一致）：有头 Edge 154.0.4258.62，viewport 1416×808 / DPR 1，RTX 4060（ANGLE Direct3D11），独立 demo 服务 `127.0.0.1:18765`（`CHUANMU_UI_DEMO=1`），100 卡用 sessionStorage `synth100` 由 5 行夹具派生
- 测量工具：`tools/cdp_harness.mjs`（零依赖 CDP：rAF 采样 + screencast 呈现帧 + 按画布归属钩 drawArrays/putImageData）、`tools/analyze_frames.py`

---

## 1. 一句话结论

「一卡一卡」的最可见来源是**舱体水光画布以恒定 50.1ms（约 20fps）步进绘制**，与 60fps 的海面/卡片叠在一起形成可见的顿挫；已把该画布移植为 WebGL 着色器（同一公式），实绘间隔降至 **16.7ms（60fps）**，同时主线程、呈现帧节奏与基线持平或更好。海洋画面、窗口内部水光、过滤/复制/详情/收起全部保留。

## 2. 根因证据（基线取证，四层成本区分）

| 层 | 证据（基线） | 判定 |
|---|---|---|
| 首页海面（ocean.js WebGL） | rAF median 16.7ms、海面实绘 1809 帧/30s（60fps） | 不是瓶颈，保留 |
| **舱体水光（result-fx.js 2D 逐像素）** | **实绘间隔恒定 median 50.1ms（40ms 节流 + rAF 步进量化），30s 只画 601 帧（vs rAF 1805 帧）**，跨全部场景一致 | **主根因，本次修复对象** |
| 卡片动画（.card::before/::after CSS） | 分层探针：关掉卡片装饰 p95 35.3→32.0ms，有小而真实的贡献；关面板 backdrop-blur 几乎不变（35.3→35.0） | 次要；本次不动面板模糊（无证据支持），只优化卡片指针处理 |
| 滚动布局（100 卡） | rAF 干净（p95 17.0）；详情开合 p95 23.5ms（max-height 过渡固有）；到达瞬间 1×88.1ms 帧 + 77ms longtask（DOM 重建固有） | 固有成本，不在本次最小修复范围 |

辅助证据：`baseline_cards5_hover` 出现 1×82.2ms rAF 尖峰——指针以 ~125Hz 逐事件 `getBoundingClientRect()` + 3 次 `style.setProperty`（其中 `--hover` 每次 move 都重复写）；`result-fx.js` 的 `degraded` 降档会被 `resize()` 按初始规则重算撤销；`ocean.js` 的性能自适应只在前 150 帧判定一次（`perfChecked` 后永不再评估）。

## 3. 修复内容（最小修复，按证据逐条对应）

| # | 文件 | 修改 | 对应根因 |
|---|---|---|---|
| F1 | `ui/static/result-fx.js` | 舱体水光 canvas 移植为 **WebGL 着色器**（三组正弦干涉公式原样移植到 GLSL，y 轴翻转对齐；2×2 半像素超采样内建柔化；时间用 rAF 时间戳 = 真实经过时间；60fps 不再节流）。WebGL 不可用时回退原 2D 路径（保留 40ms 节流与降档）；上下文丢失时停用水光、面板功能不受影响 | 主根因（20fps 步进） |
| F2 | 同上 | 指针移动**合帧**：事件只记坐标，rAF 里统一一次读写；rect 按页面滚动位置缓存（未滚动直接复用）；`--hover` 改为 pointerover/pointerout **进入/离开各写一次**（原来每次 move 都写） | hover 场景 82ms 尖峰 |
| F3 | 同上 | 降档档位持久化：`degradeScale` 记录档位，`resize()` 在降档后的有效尺寸上重算，**不再撤销降档** | 降档被 resize 撤销 |
| F4 | `ui/static/ocean.js` | 一次性 150 帧判定（`perfChecked`）改为**持续窗口化自适应**：每 120 帧统计平均帧间隔，>24ms 降档（快降）、<17.2ms 且距上次调整 ≥30s 才升档（慢升）、相邻调整 ≥8s 防抖、启动 6s 静默期、跳过 dt>250ms 的后台恢复帧 | 海面降档不恢复/不持续 |
| F6 | `ui/static/result-fx.css` | `.cabin-fx` 去掉 `blur(1.5px)`（柔化已由 shader 内超采样承担），保留 `saturate(1.15)`；其余视觉（mix-blend-mode:screen、opacity、光柱/明暗层、卡片动画）逐字未动 | 少一层每帧合成过滤 |
| — | `ui/static/index.html` | `ocean.js?v=5`、`result-fx.js?v=2`、`result-fx.css?v=2`（缓存版本号） | — |
| — | `tools/cdp_harness.mjs` | 探针适配：drawArrays 按 canvas 归属分流（`#sea`→ocean、`.cabin-fx`→cabin），兼容 WebGL2；putImageData 钩保留（2D 回退可测）。**基线数据在新旧钩下语义一致，前后可比** | 测量基建 |
| — | `tools/analyze_frames.py` | 修复平均帧率印刷 bug（多乘 1000） | 工具 |

面板 `backdrop-filter: blur(16px)` **未动**（分层探针无证据支持）；app.js **未动**（过滤/复制/详情/收起逻辑原样，回归测试证明）；未减少候选数量、未删除任何视觉层。

## 4. 前后测量（同浏览器、同窗口、同假数据、同参数）

### 4.1 rAF 主线程帧间隔（ms）

| 场景 | 基线 median/p95/max（>50ms 帧数） | 修复后 median/p95/max（>50ms 帧数） |
|---|---|---|
| home 首页海面 | 16.7 / 16.8 / 17.9（0） | 16.7 / 16.8 / 19.5（0） |
| cards5_hover 指针画圈 | 16.7 / 17.2 / **82.2**（1） | 16.7 / 17.2 / **23.9**（0） |
| detail_toggle 详情开合 | 16.7 / 17.4 / 18.4（0） | 16.7 / 17.3 / 18.4（0） |
| collapse_toggle 收起/展开 | 16.7 / 16.9 / 24.5（0） | 16.7 / 16.9 / 20.1（0） |
| detail_toggle_100 | 16.8 / **23.5** / 27.9（0） | 16.8 / **20.2** / 24.5（0） |
| cards100_scroll 滚动 | 16.7 / 17.0 / 22.1（0） | 16.7 / 17.0 / 19.2（0） |
| arrival_100 到达瞬间 | 16.7 / 17.0 / 88.1（1）+ longtask 77ms | 16.7 / 17.0 / 78.8（1）+ longtask 73ms |

### 4.2 舱体水光实际绘制（本次修复的核心指标）

| 场景（时长） | 基线 实绘帧数 / 间隔 median→max | 修复后 实绘帧数 / 间隔 median→max |
|---|---|---|
| cards5_hover（30s） | 603 帧 / 50.1→85.6ms | **1814 帧 / 16.7→20.5ms** |
| detail_toggle（20s） | 408 帧 / 50.1→51.7ms | **1220 帧 / 16.7→18.5ms** |
| cards100_scroll（30s） | 601–602 帧 / 50.1→52.1ms | **1801 帧 / 16.7→19.1ms** |
| detail_toggle_100（20s） | 402 帧 / 50.1→67.7ms | **1188 帧 / 16.7→24.4ms** |
| arrival_100（12s） | 245 帧 / 50.1→109.4ms | **732 帧 / 16.7→78.6ms** |

水光实绘帧数提升 **3 倍**，与 rAF 帧数逐一对齐（1814/1814、1801/1801、1188/1188），20fps 步进消失。
（collapse_toggle 的 cabin max 1383.6/1435.2ms 是收起状态停画到展开恢复的自然间隔，前后一致，非卡顿。）

### 4.3 呈现帧（screencast 实际送帧，100 卡滚动 12s）

| 指标 | 基线 | 修复后 |
|---|---|---|
| 平均帧率 | 53.8 fps | 53.8 fps |
| 间隔 median / p95 / max | 16.42 / 35.53 / 46.13 ms | 16.76 / 34.32 / 46.32 ms |
| >50ms / >100ms | 0 / 0 | 0 / 0 |

分层呈现帧探针（`pres_fixed_meta.json`）与基线同构：全开层 median 16.4 / p95 34.9 / >50×0；`no_cabinjs`、`all_off` 层的大间隔是「画面无变化不送帧」的采样特性，不可比（基线结论不变）。
滚动时 p95≈35ms 由输入节奏（260px/140ms 步进）决定，非渲染问题；本修复不改变该节奏，消除的是水光层 20fps 步进叠加出的可见顿挫。

### 4.4 回归测试

- `node ui/test_app_poll.js`：**40 项通过，0 失败**
- `py -3.10 -B ui/test_ui.py`：**25 项通过，0 失败**（含演示模式隔离、跨站 POST 403、不写真实结果文件）

## 5. 录屏与截图（实际画面证据）

- 录屏（真实节奏，非重铺帧）：`raw/fixed_cards100_scroll_frames/`（655 帧 jpg + `fixed_cards100_scroll_timestamps.json` 间隔分布，见 4.3）；基线对照 `raw/base_pres_cards100_scroll_timestamps.json`
- 截图：`shots/fixed_cards5.png`（首页海面 + 5 卡 + 面板内潮光）、`shots/fixed_noborder_inner.png`（100 卡裁边框内部水光证据）、`fixed_cards100_top/mid.png`、`fixed_detail_open.png`、`fixed_collapsed_sea.png`、`fixed_cards100_scroll_end.png`
- 截图目检：月亮/星空/海浪/月光海路正常，面板内部斜向潮光清晰可见，卡片墙与文字可读性正常（GL 版光纹比 blur(1.5px) 版略锐利，光带位置/色调/强度保持）

## 6. 隔离证据（零真实行情）

- 只启动独立 demo 服务（`CHUANMU_UI_DEMO=1`，端口 18765，启动前端口空闲检查）；每次 POST /api/scan 前节点侧先 GET 同源 /api/state 断言 `mode==='demo'`（复核两次），页面侧 fetch 包装同样断言，非 demo 立即抛错
- 浏览器 CDP Fetch 只放行 `http://127.0.0.1:18765/*`：整个测量期间 `raw/network_isolation.log` 除 allow 规则声明外**零条拦截记录**——浏览器从未尝试访问任何其它地址
- 100 卡为 sessionStorage 标志从 5 行夹具派生（SYN01..SYN100），demo 服务 `scan_fn=None`，真实入口一调用即失败
- 每轮测量结束 harness 显式 taskkill 自己启动的浏览器与 demo 服务进程，无遗留

## 7. 未解决项 / 已知边界

1. **到达瞬间仍有 1 个 ~79ms 帧 + 73ms longtask**：100 卡 DOM 全量重建的固有成本，位于 app.js（任务书范围外，未动）；水光已改 GPU 后不再叠加其上。
2. **详情开合（100 卡）p95 20.2ms**：max-height 过渡触发整卡重排的固有成本；修复后比基线（23.5ms）更好，但未归零。
3. **滚动呈现帧 p95≈35ms**：由测量输入节奏决定（260px/140ms 步进），非渲染问题。
4. **K3 前序报告的 11.9 FPS 与本次不符**：那是 Edge headless 环境（无 GPU 合成路径）测得；本次有头窗口 + RTX 4060 下主线程与呈现帧均干净。两环境差异如实记录，未采信 K3 数字作为本次依据。
5. **水光视觉略变**：去掉 CSS blur(1.5px) 后光纹比原来稍锐利（柔化由 shader 2×2 超采样承担）；公式、色调、光带位置一致。如需完全复刻旧观感，见撤回方式。
6. **2D 回退路径性能与原版相同**（40ms 节流 + 降档，含 F3 修复）：仅在 WebGL 不可用时生效，本机未触发。

## 8. 撤回方式

基线文件哈希见 `raw/baseline_digests.json`（ocean.js `81d79193…`、result-fx.js `0c2fe441…`、result-fx.css `958255be…`、style.css `6bf4a908…`、app.js `1e964f45…`、index.html `ee111109…`）。本次共改 4 个项目文件（style.css、app.js 未动）：

- **整体撤回**：用 git（若在版本控制内）`git checkout -- ui/static/`；否则按下表逐文件还原
- `ui/static/result-fx.js` → 还原 `0c2fe441…` 对应版本（改动：水光 GL 化 + 指针合帧 + 降档持久化）
- `ui/static/ocean.js` → 还原 `81d79193…`（改动：drawFrame 内 perf 段 + 变量声明 + init 3 行）
- `ui/static/result-fx.css` → 还原 `958255be…`（改动：仅 `.cabin-fx` 的 filter 一行，`blur(1.5px) saturate(1.15)`）
- `ui/static/index.html` → 还原 `ee111109…`（改动：3 处 `?v=` 版本号）
- 修改后哈希：result-fx.js `3f416010…`、ocean.js `f6c81815…`、result-fx.css `84cc28f4…`、index.html `ee12dbed…`
- 最小局部撤回（只回水光 GL，保留其它）：把 result-fx.js 中 `useGL` 置为恒 `false`（`const useGL = false;`）即回到 2D 路径（仍保留 F2/F3 改进）；CSS 的 blur 需同时手动加回

## 9. 测量产物索引

| 文件 | 内容 |
|---|---|
| `raw/baseline*.json`、`raw/baseline2_*.json` | 修复前 7 场景 rAF + 两画布实绘 + longtask |
| `raw/fixed_*.json`、`raw/fixed2_*.json` | 修复后同参数 7 场景 |
| `raw/pres_fixed_meta.json`、`raw/base_pres_*.json` | 分层呈现帧探针前后 |
| `raw/fixed_cards100_scroll_timestamps.json` + `frames/` | 修复后录屏 655 帧 + 间隔分析 |
| `raw/network_isolation.log` | 零真实行情请求证据 |
| `raw/baseline_digests.json` | 撤回依据（基线哈希） |
| `shots/fixed_*.png` | 修复后画面（含水光视觉证据） |
| `tools/cdp_harness.mjs`、`tools/analyze_frames.py` | 测量工具（可复跑） |
