/* 观潮 · 海下观测舱 —— 结果面板内部光影（独立模块，可整体撤除）
 *
 * 接线原则：不改 app.js。全部通过 MutationObserver / IntersectionObserver /
 * 事件委托接入现有状态流；任何一步抛错只损失光影，面板与功能保持静态可用。
 *
 * 职责：
 *   ① 舱体共享 caustics canvas —— WebGL 着色器实现（同一公式移植到 GLSL，
 *      GPU 单 pass，主线程近零成本，60fps；2×2 超采样替代 CSS blur 的柔化）。
 *      WebGL 不可用时回退原 2D 逐像素实现（保留 40ms 节流与降档）。
 *      静态/后台/面板隐藏/整批收起即停，停时留静帧。
 *   ② 结果到达后面板内一道潮光（只在新一批卡片出现时播，轮询不重播）
 *   ③ 卡片入场：视口内分组升起，视口外滚动到才显现
 *   ④ 卡片指针反光（材质层视差）与复制操作的局部水光反馈
 *   ⑤ 与 #motion-toggle 联动（读 aria-pressed / localStorage，规则与 app.js 一致）
 */
"use strict";
(() => {
  const panel = document.querySelector(".panel");
  const results = document.getElementById("results");
  if (!panel || !results) return;

  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const saved = localStorage.getItem("chuanmu-motion");
  let motion = saved !== null ? saved === "1" : !reduced;
  let collapsed = false;

  /* ================= 舱体 caustics ================= */

  const cabin = document.createElement("div");
  cabin.className = "cabin";
  cabin.setAttribute("aria-hidden", "true");
  const cv = document.createElement("canvas");
  cv.className = "cabin-fx";
  cabin.appendChild(cv);
  panel.insertBefore(cabin, panel.firstChild);

  const tide = document.createElement("div");
  tide.className = "tide-flash";
  tide.setAttribute("aria-hidden", "true");
  panel.insertBefore(tide, panel.firstChild);

  /* 渲染后端：优先 WebGL。小画布单 pass 在 GPU 上成本可忽略，
   * 消除 2D 版逐像素 putImageData 的主线程开销（约每帧 4–9ms）。 */
  const glAttrs = { alpha: false, antialias: false, depth: false, stencil: false };
  const gl = cv.getContext
    ? (cv.getContext("webgl", glAttrs) || cv.getContext("experimental-webgl", glAttrs))
    : null;
  const useGL = !!gl;
  const ctx2d = useGL ? null : (cv.getContext ? cv.getContext("2d") : null);

  /* 与 2D 版逐像素公式完全一致的三组正弦干涉；y 轴翻转对齐 canvas 坐标。
   * 2×2 半像素偏移采样等效内建柔化（替代 CSS blur(1.5px)，少一层合成过滤）。 */
  const CAUSTIC_FRAG = `
precision mediump float;
uniform vec2 uRes;
uniform float uTime;
vec3 caustic(vec2 frag){
  float xx = frag.x / uRes.x;
  float yy = 1.0 - frag.y / uRes.y;
  float u = xx * 4.2;
  float v = yy * 2.4;
  float fy = 1.18 - 0.62 * yy;
  float c = sin(u * 1.9 + uTime * 0.55 + sin(v * 2.6 - uTime * 0.42))
          + sin(v * 2.2 - uTime * 0.48 + sin(u * 1.4 + uTime * 0.61))
          + sin((u + v) * 1.25 + uTime * 0.3);
  c = 1.0 - abs(c) * 0.3333;
  float l = c * c; l *= l;
  l *= fy * (0.45 + 0.55 * xx);
  return vec3(14.0 + l * 60.0, 40.0 + l * 165.0, 62.0 + l * 175.0) / 255.0;
}
void main(){
  vec3 col = caustic(gl_FragCoord.xy + vec2(-0.75, -0.75))
           + caustic(gl_FragCoord.xy + vec2( 0.75, -0.75))
           + caustic(gl_FragCoord.xy + vec2(-0.75,  0.75))
           + caustic(gl_FragCoord.xy + vec2( 0.75,  0.75));
  gl_FragColor = vec4(col * 0.25, 1.0);
}`;

  const CAUSTIC_VERT = `
attribute vec2 aPos;
void main(){ gl_Position = vec4(aPos, 0.0, 1.0); }`;

  let W = 0, H = 0;
  let img = null, buf32 = null;      // 2D 回退路径专用
  let gu = null;                     // GL uniform 位置
  let raf = null, running = false, lastDraw = 0;
  let slowFrames = 0, degradeScale = 1;

  function initGL() {
    try {
      const sh = (type, src) => {
        const s = gl.createShader(type);
        gl.shaderSource(s, src);
        gl.compileShader(s);
        if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) {
          throw new Error(gl.getShaderInfoLog(s) || "shader compile failed");
        }
        return s;
      };
      const vs = sh(gl.VERTEX_SHADER, CAUSTIC_VERT);
      const fs = sh(gl.FRAGMENT_SHADER, CAUSTIC_FRAG);
      const p = gl.createProgram();
      gl.attachShader(p, vs);
      gl.attachShader(p, fs);
      gl.linkProgram(p);
      if (!gl.getProgramParameter(p, gl.LINK_STATUS)) {
        throw new Error(gl.getProgramInfoLog(p) || "program link failed");
      }
      gl.useProgram(p);
      const buf = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, buf);
      gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
      const loc = gl.getAttribLocation(p, "aPos");
      gl.enableVertexAttribArray(loc);
      gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
      gu = {
        res: gl.getUniformLocation(p, "uRes"),
        time: gl.getUniformLocation(p, "uTime"),
      };
      cv.addEventListener("webglcontextlost", (e) => {
        e.preventDefault();
        console.warn("[观潮] 舱体水光 WebGL 上下文丢失，水光停用（面板功能不受影响）");
        running = false;
        if (raf) cancelAnimationFrame(raf);
        raf = null;
      });
      return true;
    } catch (err) {
      console.warn("[观潮] 舱体水光 WebGL 初始化失败：", (err && err.message) || err);
      return false;
    }
  }
  const glReady = useGL ? initGL() : false;

  function resize() {
    if (useGL ? !glReady : !ctx2d) return;
    const k = useGL ? 0.30 : 0.22;
    const capW = useGL ? 420 : 340, capH = useGL ? 280 : 220;
    const baseW = Math.max(120, Math.min(capW, Math.round(panel.clientWidth * k)));
    const baseH = Math.max(60, Math.min(capH, Math.round(panel.clientHeight * k)));
    /* 降档档位持久化：任何尺寸变化都在降档后的有效尺寸上重算，不再撤销降档 */
    const w = Math.max(60, Math.round(baseW * degradeScale));
    const h = Math.max(40, Math.round(baseH * degradeScale));
    if (w === W && h === H) return;
    W = w; H = h;
    cv.width = W; cv.height = H;
    if (useGL) {
      gl.viewport(0, 0, W, H);
    } else {
      img = ctx2d.createImageData(W, H);
      buf32 = new Uint32Array(img.data.buffer);
    }
    if (!running) draw(performance.now() / 1000); // 尺寸变化时补一帧静帧
  }

  function draw(t) {
    if (useGL) {
      gl.uniform2f(gu.res, W, H);
      gl.uniform1f(gu.time, t);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
      /* GL 路径不做降档：主线程回调仅 2 次 uniform + 1 次 draw，
       * GPU 成本随分辨率几乎不变（<0.1ms）；rAF 间隔异常时瓶颈在页面其它层。 */
    } else {
      draw2D(t);
    }
  }

  /* 水面干涉亮丝：三组嵌套正弦，^4 锐化成光纹；上亮下深、右上（月亮方位）微亮（2D 回退） */
  function draw2D(t) {
    const t0 = performance.now();
    let i = 0;
    for (let y = 0; y < H; y++) {
      const v = (y / H) * 2.4;
      const fy = 1.18 - 0.62 * (y / H);
      for (let x = 0; x < W; x++) {
        const u = (x / W) * 4.2;
        let c = Math.sin(u * 1.9 + t * 0.55 + Math.sin(v * 2.6 - t * 0.42))
              + Math.sin(v * 2.2 - t * 0.48 + Math.sin(u * 1.4 + t * 0.61))
              + Math.sin((u + v) * 1.25 + t * 0.3);
        c = 1 - Math.abs(c) * 0.3333;
        let l = c * c; l *= l;
        l *= fy * (0.45 + 0.55 * (x / W));
        const r = 14 + l * 60, g = 40 + l * 165, b = 62 + l * 175;
        buf32[i++] = (255 << 24) | ((b | 0) << 16) | ((g | 0) << 8) | (r | 0);
      }
    }
    ctx2d.putImageData(img, 0, 0);

    /* 自适应降档：持续超预算就整体降一档（档位记入 degradeScale，resize 不撤销；只降一次） */
    if (degradeScale > 0.5) {
      const cost = performance.now() - t0;
      slowFrames = cost > 13 ? slowFrames + 1 : 0;
      if (slowFrames >= 12) {
        degradeScale = 0.5;
        slowFrames = 0;
        resize();
        console.info("[观潮] 舱体水光已降档（2D 回退路径）");
      }
    }
  }

  function loop(now) {
    if (!running) return;
    raf = requestAnimationFrame(loop);
    if (!useGL && now - lastDraw < 40) return;   // 2D 回退：~25fps 足够表达缓慢聚散
    lastDraw = now;
    draw(now / 1000);                            // rAF 时间戳 = 真实经过时间
  }

  function shouldRun() {
    return !!((useGL ? glReady : ctx2d) && motion && !reduced && !panel.hidden && !document.hidden && !collapsed);
  }

  function start() {
    if (running || !(useGL ? glReady : ctx2d)) return;
    running = true;
    lastDraw = 0;
    raf = requestAnimationFrame(loop);
  }

  function stop() {
    running = false;
    if (raf) cancelAnimationFrame(raf);
    raf = null;
    if ((useGL ? glReady : ctx2d) && W) draw(performance.now() / 1000); // 停在一帧有内容的静帧，不留黑底
  }

  function refresh() { if (shouldRun()) start(); else stop(); }

  /* ================= 卡片入场（视口内分组，视口外按需） ================= */

  const io = ("IntersectionObserver" in window) ? new IntersectionObserver((entries) => {
    for (const e of entries) {
      if (!e.isIntersecting) { e.target.classList.add("fx-wait"); continue; }
      io.unobserve(e.target);
      e.target.classList.remove("fx-wait");
      e.target.classList.add(e.target.dataset.fxBatch ? "fx-rise" : "fx-rise-now");
      delete e.target.dataset.fxBatch;
    }
  }, { rootMargin: "0px 0px 80px", threshold: 0.02 }) : null;

  function observeCard(card, batch) {
    if (!io || !motion || reduced) return;   // 静态/降级：卡片直接可见
    if (batch) card.dataset.fxBatch = "1";
    io.observe(card);
  }

  /* 新一批结果 → 潮光 + 入场观察。轮询指纹不变时 app.js 不动 DOM，这里自然不重播。 */
  let hadCards = results.childElementCount > 0;
  new MutationObserver((muts) => {
    let added = 0, removed = 0;
    const newCards = [];
    for (const m of muts) {
      added += m.addedNodes.length;
      removed += m.removedNodes.length;
      for (const n of m.addedNodes) {
        if (n.nodeType === 1 && n.classList && n.classList.contains("card")) newCards.push(n);
      }
    }
    const has = results.childElementCount > 0;
    if (added > 0 && (!hadCards || removed > 0) && motion && !reduced) {
      tide.classList.remove("play");
      void tide.offsetWidth;
      tide.classList.add("play");
    }
    hadCards = has;
    const batch = newCards.length > 0;
    for (const c of newCards) observeCard(c, batch);
  }).observe(results, { childList: true });

  tide.addEventListener("animationend", () => tide.classList.remove("play"));

  /* ================= 指针反光 / 复制反馈 / 舱体视差 ================= */

  /* 指针移动合帧：事件只记坐标，rAF 统一一次读写。
   * （原实现 ~125Hz 逐事件 getBoundingClientRect + 3 次 setProperty。）
   * rect 按页面滚动位置缓存：同一屏内直接复用，滚动后重取一次。 */
  let pmPending = false, pmX = 0, pmY = 0, pmCard = null;
  const pmRects = new WeakMap();
  function applyPointer() {
    pmPending = false;
    const card = pmCard;
    if (!card || !card.isConnected) return;
    let entry = pmRects.get(card);
    const sy = window.scrollY;
    if (!entry || entry.sy !== sy) {
      entry = { r: card.getBoundingClientRect(), sy };
      pmRects.set(card, entry);
    }
    const r = entry.r;
    card.style.setProperty("--mx", (((pmX - r.left) / r.width) * 100).toFixed(1) + "%");
    card.style.setProperty("--my", (((pmY - r.top) / r.height) * 100).toFixed(1) + "%");
  }
  results.addEventListener("pointermove", (e) => {
    const card = e.target.closest(".card");
    if (!card) return;
    pmX = e.clientX; pmY = e.clientY; pmCard = card;
    if (!pmPending) { pmPending = true; requestAnimationFrame(applyPointer); }
  }, { passive: true });

  /* hover 开关只在进入/离开写一次（原实现每次 move 都写 --hover=1） */
  let hovered = null;
  results.addEventListener("pointerover", (e) => {
    const card = e.target.closest(".card");
    if (card && card !== hovered) {
      if (hovered && hovered.isConnected) hovered.style.setProperty("--hover", "0");
      hovered = card;
      card.style.setProperty("--hover", "1");
    }
  }, { passive: true });
  results.addEventListener("pointerout", (e) => {
    const card = e.target.closest(".card");
    if (card && !card.contains(e.relatedTarget)) {
      card.style.setProperty("--hover", "0");
      if (hovered === card) hovered = null;
    }
  }, { passive: true });

  results.addEventListener("click", (e) => {
    const btn = e.target.closest(".card-actions button");
    if (!btn) return;
    const card = btn.closest(".card");
    card.classList.remove("flash");
    void card.offsetWidth;
    card.classList.add("flash");
  });

  let parallaxPending = false;
  panel.addEventListener("pointermove", (e) => {
    if (parallaxPending || !motion) return;
    parallaxPending = true;
    requestAnimationFrame(() => {
      parallaxPending = false;
      const r = panel.getBoundingClientRect();
      const nx = ((e.clientX - r.left) / r.width) * 2 - 1;
      const ny = ((e.clientY - r.top) / r.height) * 2 - 1;
      cabin.style.transform = `translate(${nx * 5}px, ${ny * 4}px)`;
    });
  }, { passive: true });

  /* ================= 动静开关 / 后台 / 收起 联动 ================= */

  function setFxMotion(on) {
    motion = !!on;
    document.body.classList.toggle("fx-still", !motion);
    if (!motion && io) {
      io.disconnect();
      for (const c of results.querySelectorAll(".fx-wait, .fx-rise, .fx-rise-now")) {
        c.classList.remove("fx-wait", "fx-rise", "fx-rise-now");
      }
    }
    refresh();
  }

  const toggleBtn = document.getElementById("motion-toggle");
  if (toggleBtn) {
    toggleBtn.addEventListener("click", () => {
      // app.js 的处理器先注册先执行，这里读到的已是新状态
      setFxMotion(toggleBtn.getAttribute("aria-pressed") === "true");
    });
  }

  new MutationObserver(() => {
    collapsed = results.classList.contains("collapsed");
    refresh();
  }).observe(results, { attributes: true, attributeFilter: ["class"] });

  new MutationObserver(() => refresh())
    .observe(panel, { attributes: true, attributeFilter: ["hidden"] });

  document.addEventListener("visibilitychange", refresh);

  if ("ResizeObserver" in window) new ResizeObserver(resize).observe(panel);
  else window.addEventListener("resize", resize);

  /* 初始化 */
  document.body.classList.toggle("fx-still", !motion);
  resize();
  refresh();
  for (const c of results.querySelectorAll(".card")) observeCard(c, true);
})();
