/* 观潮 · 海面场景模块（自研 GLSL 高度场海洋，WebGL 实时渲染）
 *
 * 职责与边界：
 * - 只负责画面：海面、天空、镜头、转场水光；不发起任何网络请求。
 * - 单 rAF 循环；重复查询只改镜头参数，不新建循环。
 * - pixelRatio 封顶 1.5，实测掉帧自动降内部分辨率；后台标签暂停绘制。
 * - 对外只暴露 init / setMotion / transition / lightSweep / pointer / measureFPS。
 */
"use strict";

window.Ocean = (() => {
  const VERT = `
attribute vec2 aPos;
void main(){ gl_Position = vec4(aPos, 0.0, 1.0); }
`;

  /* 低机位海面：射线与高度场求交得真实浪峰剪影；Fresnel 反射天空；
   * 波峰泡沫、金色日路、青绿透光、距离雾到海平线。 */
  const FRAG = `
precision highp float;

uniform vec2  uRes;
uniform float uTime;
uniform vec3  uCam;     // 相机位置
uniform vec2  uAim;     // 视轴偏移（视差）
uniform float uSweep;   // 水光掠过进度 0..1，<0 关闭
uniform float uDetail;  // 细节档（掉帧降级用）0.6..1.0

const vec3 MOON = normalize(vec3(0.38, 0.30, -0.88));

float hash(vec2 p){
  p = fract(p * vec2(234.34, 435.345));
  p += dot(p, p + 34.23);
  return fract(p.x * p.y);
}

float hash3(vec3 p){
  p = fract(p * 0.3183099 + vec3(0.1, 0.2, 0.3));
  p *= 17.0;
  return fract(p.x * p.y * p.z * (p.x + p.y + p.z));
}

float noise(vec2 p){
  vec2 i = floor(p), f = fract(p);
  vec2 u = f * f * (3.0 - 2.0 * f);
  float a = hash(i);
  float b = hash(i + vec2(1.0, 0.0));
  float c = hash(i + vec2(0.0, 1.0));
  float d = hash(i + vec2(1.0, 1.0));
  return mix(mix(a, b, u.x), mix(c, d, u.x), u.y);
}

/* ---------- 星空 ---------- */
float starLayer(vec3 rd, float scale, float density, float t){
  vec3 p = rd * scale;
  vec3 c = floor(p);
  vec3 f = fract(p) - 0.5;
  float sel = hash3(c);
  if (sel > density) return 0.0;
  vec3 sp = vec3(hash3(c + 7.1), hash3(c + 3.7), hash3(c + 9.3)) - 0.5;
  float d = length(f - sp * 0.8);
  float star = smoothstep(0.10, 0.0, d);
  float bright = 0.35 + 0.65 * hash3(c + 5.5);
  float tw = 0.72 + 0.28 * sin(t * (0.6 + sel * 4.0) + sel * 40.0);
  return star * bright * tw;
}

vec3 skyColor(vec3 rd){
  float h = clamp(rd.y, 0.0, 1.0);
  // 深靛夜空：天顶近黑蓝，地平线泛起雾蓝
  vec3 zen = vec3(0.008, 0.016, 0.045);
  vec3 mid = vec3(0.028, 0.06, 0.13);
  vec3 hor = vec3(0.075, 0.14, 0.23);
  vec3 col = mix(mix(hor, mid, smoothstep(0.0, 0.24, h)), zen, smoothstep(0.18, 0.75, h));

  // 地平线一线微亮（远处城市辉光），撑住纵深；用冷紫蓝，不读作落日
  float warm = pow(max(dot(normalize(vec3(rd.x, 0.0, rd.z)), normalize(vec3(-0.72, 0.0, -0.69))), 0.0), 5.0);
  col += vec3(0.13, 0.17, 0.30) * warm * smoothstep(0.16, 0.0, rd.y) * 0.55;

  // 银河带：斜贯天空的微弱亮带 + 星密度提升
  float band = exp(-pow(dot(rd, normalize(vec3(0.48, 0.30, 0.82))), 2.0) * 16.0);
  float neb = band * (0.4 + 0.6 * noise(vec2(rd.x * 9.0 + rd.y * 4.0, rd.y * 7.0)));
  col += vec3(0.16, 0.22, 0.38) * neb * 0.35 * smoothstep(0.02, 0.2, rd.y);

  // 星星：密小星 + 疏亮星，近地平线淡出；水中倒影同源
  float starGate = smoothstep(0.01, 0.18, rd.y);
  float s = starLayer(rd, 60.0, 0.10, uTime) * 0.8
          + starLayer(rd, 24.0, 0.05, uTime * 0.7) * 1.6;
  col += vec3(0.82, 0.88, 1.0) * s * starGate * (0.55 + band * 1.2);

  // 小月：柔和月盘 + 收敛光晕（不刺眼）
  float mdot = max(dot(rd, MOON), 0.0);
  float disc = smoothstep(0.99965, 0.99985, mdot);
  float halo = pow(mdot, 700.0);
  col += vec3(0.92, 0.96, 1.05) * disc * 1.35;
  col += vec3(0.42, 0.52, 0.78) * halo * 0.5;
  col += vec3(0.2, 0.28, 0.48) * pow(mdot, 60.0) * 0.25;

  // 高空微云
  float cl = noise(vec2(rd.x * 6.0 / max(rd.y, 0.06), rd.y * 3.0) + vec2(uTime * 0.006, 0.0));
  cl = smoothstep(0.65, 0.95, cl) * smoothstep(0.03, 0.2, rd.y) * 0.045;
  col += vec3(0.35, 0.45, 0.65) * cl;
  return col;
}

/* ---------- 海浪高度场 ---------- */
float swell(vec2 p, float t){
  float h = 0.0;
  h += sin(dot(p, vec2(0.055, 0.031)) + t * 0.62) * 0.55;
  h += sin(dot(p, vec2(-0.037, 0.068)) + t * 0.47) * 0.38;
  h += sin(dot(p, vec2(0.021, -0.052)) + t * 0.35) * 0.30;
  return h;
}

float chop(vec2 p, float t, int oct){
  float h = 0.0, amp = 0.55, fr = 0.34, sum = 0.0;
  vec2 dir = vec2(0.94, 0.34);
  for (int i = 0; i < 6; i++){
    if (i >= oct) break;
    vec2 q = p * fr + dir * (t * (0.5 + 0.13 * float(i)));
    float n = noise(q);
    n = 1.0 - abs(2.0 * n - 1.0);
    n = pow(n, 1.7);
    h += n * amp;
    sum += amp;
    amp *= 0.5;
    fr *= 1.85;
    dir = normalize(vec2(dir.y + 0.63, -dir.x + 0.29));
  }
  return h / max(sum, 1e-4);
}

float seaHeight(vec2 p, float t, int oct){
  return swell(p, t) + chop(p, t, oct) * 1.05;
}

/* 细节随距离衰减，远处趋平接海平线 */
float atten(float d){ return exp(-d * 0.016); }

float mapH(vec2 p, float t, float d){
  int oct = d < 26.0 ? 6 : (d < 70.0 ? 4 : 2);
  oct = int(float(oct) * uDetail + 0.5);
  if (oct < 1) oct = 1;
  return seaHeight(p, t, oct) * atten(d);
}

vec3 seaNormal(vec2 p, float t, float d){
  float e = 0.035 + d * 0.0035;
  float hC = mapH(p, t, d);
  float hX = mapH(p + vec2(e, 0.0), t, d);
  float hZ = mapH(p + vec2(0.0, e), t, d);
  return normalize(vec3(hC - hX, e, hC - hZ));
}

/* ---------- 泡沫 ---------- */
float foamMask(vec2 p, float t, float d, float h){
  float crest = smoothstep(0.5, 1.0, h);
  float breakup = noise(p * 2.6 + vec2(t * 0.7, -t * 0.4));
  breakup = smoothstep(0.32, 0.72, breakup);
  float streaks = noise(p * vec2(1.2, 5.0) + vec2(0.0, t * 1.1));
  streaks = smoothstep(0.55, 0.9, streaks) * 0.6;
  return clamp(crest * (breakup * 0.9 + streaks), 0.0, 1.0) * atten(d * 0.55);
}

/* ---------- 海面着色 ---------- */
vec3 seaColor(vec3 ro, vec3 rd, vec3 pos, float t){
  float d = length(pos - ro);
  vec3 n = seaNormal(pos.xz, t, d);
  float h = mapH(pos.xz, t, d);

  float fres = pow(1.0 - max(dot(n, -rd), 0.0), 5.0) * 0.92 + 0.08;
  vec3 refl = skyColor(reflect(rd, n));

  // 月光下的水体：深靛为底，浪尖透出微青（特效化处理，不写实）
  float towardMoon = pow(max(dot(rd, MOON), 0.0), 5.0);
  float hh = clamp(h * 0.8 + 0.35, 0.0, 1.0);
  vec3 deep = vec3(0.004, 0.022, 0.052);
  vec3 lift = vec3(0.015, 0.16, 0.20);
  vec3 body = mix(deep, lift, hh * hh);
  body += vec3(0.02, 0.20, 0.22) * towardMoon * hh * 0.9;

  vec3 col = mix(body, refl, fres);

  // 月光海路：银色碎闪
  float spec = pow(max(dot(reflect(rd, n), MOON), 0.0), 380.0);
  float sparkle = 1.0 + 3.5 * noise(pos.xz * 6.5 + vec2(t * 2.3, -t * 1.7));
  col += vec3(0.75, 0.85, 1.0) * spec * sparkle;

  // 白沫：月光染银 + 极微弱青光（蓝眼泪意象，克制）
  float foam = foamMask(pos.xz, t, d, h);
  vec3 foamCol = vec3(0.55, 0.68, 0.76) * (0.35 + 0.65 * max(dot(n, MOON), 0.0));
  col = mix(col, foamCol, foam * 0.9);
  col += vec3(0.04, 0.20, 0.19) * foam * foam;

  float fog = 1.0 - exp(-d * 0.011);
  vec3 fogCol = skyColor(normalize(vec3(rd.x, 0.015, rd.z)));
  return mix(col, fogCol, fog);
}

/* ---------- 射线-高度场求交（近处出真实浪形剪影） ---------- */
float intersectSea(vec3 ro, vec3 rd, float t){
  if (rd.y >= -0.004) return -1.0;
  float tPlane = -ro.y / rd.y;
  float lo = max(tPlane - 3.2, 0.0);
  float hi = tPlane + 0.6;
  float step0 = (hi - lo) / 6.0;
  float prevT = lo;
  float prevF = ro.y + rd.y * lo - mapH((ro + rd * lo).xz, t, length((ro + rd * lo) - ro));
  for (int i = 1; i <= 6; i++){
    float tc = lo + step0 * float(i);
    vec3 pc = ro + rd * tc;
    float fc = pc.y - mapH(pc.xz, t, tc);
    if (fc < 0.0){ hi = tc; lo = prevT; break; }
    prevT = tc; prevF = fc;
    hi = tc + step0;
  }
  for (int i = 0; i < 7; i++){
    float mid = 0.5 * (lo + hi);
    vec3 pm = ro + rd * mid;
    float fm = pm.y - mapH(pm.xz, t, mid);
    if (fm < 0.0) hi = mid; else lo = mid;
  }
  float tHit = 0.5 * (lo + hi);
  return tHit < tPlane + 12.0 ? tHit : tPlane;
}

void main(){
  vec2 uv = (gl_FragCoord.xy * 2.0 - uRes) / uRes.y;

  vec3 ro = uCam;
  vec3 fwd = normalize(vec3(uAim.x * 0.14, -0.085 + uAim.y * 0.06, -1.0));
  vec3 rgt = normalize(cross(fwd, vec3(0.0, 1.0, 0.0)));
  vec3 up = cross(rgt, fwd);
  vec3 rd = normalize(fwd * 1.55 + uv.x * rgt + uv.y * up);

  float t = uTime;
  vec3 col;
  float tSea = intersectSea(ro, rd, t);
  if (tSea > 0.0){
    col = seaColor(ro, rd, ro + rd * tSea, t);
  } else {
    col = skyColor(rd);
  }

  /* 转场水光掠过：一道亮雾水痕扫过画面 + 细闪 */
  if (uSweep >= 0.0){
    float band = smoothstep(uSweep - 0.30, uSweep, uv.x * 0.5 + 0.5)
               * (1.0 - smoothstep(uSweep, uSweep + 0.30, uv.x * 0.5 + 0.5));
    float mist = band * smoothstep(0.35, -0.25, uv.y) * 0.5;
    float glint = band * noise(gl_FragCoord.xy * 0.11 + t * 7.0) * 0.35;
    col += (vec3(0.65, 0.85, 0.95) * mist + vec3(0.85, 0.92, 1.0) * glint) * 0.85;
  }

  /* 调色与暗角 */
  col = pow(col, vec3(0.9));
  col *= 1.35;
  float vig = 1.0 - 0.32 * pow(length(uv * vec2(0.62, 0.85)), 2.2);
  col *= clamp(vig, 0.0, 1.0);

  gl_FragColor = vec4(col, 1.0);
}
`;

  /* ---------------- JS 侧 ---------------- */

  let canvas = null;
  let gl = null;
  let prog = null;
  let uni = {};
  let raf = null;
  let rendering = false;
  let motionOn = true;
  let startT = 0;
  let lastFrameT = 0;

  // 镜头：默认低机位；转场前推+轻抬升
  const camFar = { y: 2.35, z: 0.0 };
  const camNear = { y: 3.15, z: -5.2 };
  const cam = { y: camFar.y, z: camFar.z };
  let camAnim = null;     // {from, to, t0, dur, resolve}
  let sweepStart = -1;    // 水光掠过起始时间，<0 关闭
  let sweepDur = 1600;
  const aim = { x: 0, y: 0, tx: 0, ty: 0 };
  let driftT = Math.random() * 100;

  // 自适应画质：持续窗口化评估（降快升慢、最小间隔防抖），不再是一次性判定
  let resScale = 1.0;
  let detail = 1.0;
  let perfFrames = 0, perfAccum = 0;
  let lastAdapt = 0;          // 上次调整时刻；init 时设为启动静默期，避免加载忙时误降
  const PERF_WIN = 120;       // 每窗口统计帧数（~2s）
  const ADAPT_GAP = 8000;     // 相邻调整最小间隔，防频繁升降
  const RAISE_GAP = 30000;    // 升档需距上次调整更久，升慢

  function compile(type, src) {
    const sh = gl.createShader(type);
    gl.shaderSource(sh, src);
    gl.compileShader(sh);
    if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) {
      console.error("Ocean shader:", gl.getShaderInfoLog(sh));
      return null;
    }
    return sh;
  }

  function resize() {
    if (!canvas) return;
    const pr = Math.min(window.devicePixelRatio || 1, 1.5) * resScale;
    const w = Math.max(2, Math.round(canvas.clientWidth * pr));
    const h = Math.max(2, Math.round(canvas.clientHeight * pr));
    if (canvas.width !== w || canvas.height !== h) {
      canvas.width = w;
      canvas.height = h;
      if (gl) gl.viewport(0, 0, w, h);
    }
  }

  function drawFrame(nowMs) {
    const t = (nowMs - startT) / 1000;

    // 镜头补间
    if (camAnim) {
      const k = Math.min(1, (nowMs - camAnim.t0) / camAnim.dur);
      const e = k < 0.5 ? 4 * k * k * k : 1 - Math.pow(-2 * k + 2, 3) / 2; // easeInOutCubic
      cam.y = camAnim.from.y + (camAnim.to.y - camAnim.from.y) * e;
      cam.z = camAnim.from.z + (camAnim.to.z - camAnim.from.z) * e;
      if (k >= 1) { camAnim.resolve(); camAnim = null; }
    }

    // 视轴：鼠标视差 + 极缓漂移（避免持续摇镜）
    driftT += 0.016;
    const driftX = Math.sin(driftT * 0.05) * 0.22;
    const driftY = Math.sin(driftT * 0.037 + 1.7) * 0.10;
    aim.x += (aim.tx + driftX - aim.x) * 0.03;
    aim.y += (aim.ty + driftY - aim.y) * 0.03;

    let sweep = -1;
    if (sweepStart >= 0) {
      const k = (nowMs - sweepStart) / sweepDur;
      if (k >= 1.15) sweepStart = -1;
      else sweep = k * 1.3 - 0.15;
    }

    gl.uniform2f(uni.uRes, canvas.width, canvas.height);
    gl.uniform1f(uni.uTime, t);
    gl.uniform3f(uni.uCam, 0, cam.y, cam.z);
    gl.uniform2f(uni.uAim, aim.x, aim.y);
    gl.uniform1f(uni.uSweep, sweep);
    gl.uniform1f(uni.uDetail, detail);
    gl.drawArrays(gl.TRIANGLES, 0, 3);

    // 持续性能自适应：窗口化统计帧间隔。先快降保流畅（>24ms 即降），
    // 确有富余且稳定一段时间再慢升（<17.2ms，距上次调整 ≥30s）。
    // 跳过 dt>250ms 的帧（后台恢复/极端尖峰），避免污染窗口统计。
    if (rendering && lastFrameT) {
      const dt = nowMs - lastFrameT;
      if (dt < 250) {
        perfFrames++;
        perfAccum += dt;
        if (perfFrames >= PERF_WIN && nowMs - lastAdapt >= ADAPT_GAP) {
          const avg = perfAccum / perfFrames;
          if (avg > 24 && (resScale > 0.6 || detail > 0.6)) {
            resScale = Math.max(0.6, resScale - 0.2);
            detail = Math.max(0.6, detail - 0.2);
            resize();
            console.info(`[观潮] 窗口平均帧耗时 ${avg.toFixed(1)}ms，已降低渲染分辨率至 ${resScale}`);
            lastAdapt = nowMs;
          } else if (avg < 17.2 && resScale < 1.0 && nowMs - lastAdapt >= RAISE_GAP) {
            resScale = Math.min(1.0, resScale + 0.1);
            detail = Math.min(1.0, detail + 0.1);
            resize();
            console.info(`[观潮] 帧率持续富余，恢复渲染分辨率至 ${resScale}`);
            lastAdapt = nowMs;
          }
          perfFrames = 0; perfAccum = 0; // 无论是否调整都重开窗口，避免陈旧样本累积
        }
      }
    }
    lastFrameT = nowMs;
  }

  function loop(now) {
    if (!rendering) return;
    drawFrame(now);
    raf = requestAnimationFrame(loop);
  }

  function init(canvasEl) {
    canvas = canvasEl;
    gl = canvas.getContext("webgl", { antialias: false, alpha: false })
      || canvas.getContext("experimental-webgl", { antialias: false, alpha: false });
    if (!gl) return false;
    const vs = compile(gl.VERTEX_SHADER, VERT);
    const fs = compile(gl.FRAGMENT_SHADER, FRAG);
    if (!vs || !fs) { gl = null; return false; }
    prog = gl.createProgram();
    gl.attachShader(prog, vs);
    gl.attachShader(prog, fs);
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
      console.error("Ocean link:", gl.getProgramInfoLog(prog));
      gl = null;
      return false;
    }
    gl.useProgram(prog);
    const buf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
    const loc = gl.getAttribLocation(prog, "aPos");
    gl.enableVertexAttribArray(loc);
    gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
    for (const name of ["uRes", "uTime", "uCam", "uAim", "uSweep", "uDetail"]) {
      uni[name] = gl.getUniformLocation(prog, name);
    }
    startT = performance.now();
    lastAdapt = startT + 6000; // 启动静默期：加载/编译 shader/首帧忙时不评估降档
    resize();
    window.addEventListener("resize", resize);
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) pauseLoop();
      else if (motionOn) startLoop();
    });
    drawFrame(performance.now()); // 立即出一帧，静帧降级也有画面
    return true;
  }

  function startLoop() {
    if (rendering || !gl) return;
    rendering = true;
    lastFrameT = 0;
    raf = requestAnimationFrame(loop);
  }

  function pauseLoop() {
    rendering = false;
    if (raf) cancelAnimationFrame(raf);
    raf = null;
  }

  return {
    /** 初始化；失败返回 false（调用方走 CSS 静帧后备）。 */
    init: (canvasEl) => {
      const ok = init(canvasEl);
      if (ok && motionOn) startLoop();
      return ok;
    },
    /** 动效开关；关闭时停在当前帧（静帧后备），开启即恢复。 */
    setMotion(on) {
      motionOn = !!on;
      if (!gl) return;
      if (motionOn) startLoop();
      else { pauseLoop(); drawFrame(performance.now()); }
    },
    /** 首次查询：镜头前推 + 轻抬升 + 水光掠过；返回 Promise。 */
    transition() {
      if (!gl || !motionOn) return Promise.resolve();
      sweepDur = 1600;
      sweepStart = performance.now();
      return new Promise((resolve) => {
        camAnim = { from: { ...cam }, to: { ...camNear }, t0: performance.now(), dur: 1600, resolve };
      });
    },
    /** 再次查询的轻量水光（不重复镜头运动）。 */
    lightSweep() {
      if (!gl || !motionOn) return;
      sweepDur = 750;
      sweepStart = performance.now();
    },
    /** 鼠标视差：nx/ny ∈ [-1, 1]，只影响镜头/背景。 */
    pointer(nx, ny) {
      aim.tx = nx * 0.9;
      aim.ty = -ny * 0.5;
    },
    /** 实测帧率：采样 seconds 秒，回调 {avg, min}。 */
    measureFPS(seconds, cb) {
      const deltas = [];
      const t0 = performance.now();
      let prev = t0;
      const sample = (now) => {
        deltas.push(now - prev);
        prev = now;
        if (now - t0 < seconds * 1000) {
          requestAnimationFrame(sample);
        } else {
          deltas.shift();
          const avg = 1000 / (deltas.reduce((a, b) => a + b, 0) / deltas.length);
          const min = 1000 / Math.max(...deltas);
          cb({ avg: Math.round(avg * 10) / 10, min: Math.round(min * 10) / 10 });
        }
      };
      requestAnimationFrame(sample);
    },
    get ready() { return !!gl; },
  };
})();
