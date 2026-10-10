#!/usr/bin/env node
/* 离线测试：ui/static/app.js 的「轮询不缩回展开详情」修复（A + B）
 *
 * 为什么要在 Node 里跑：bug 是纯前端行为（轮询 → 重建 DOM → 展开态丢失），
 * Python 侧的 ui/test_ui.py 只覆盖后端接口，够不着。
 *
 * 做法：搭一个最小 DOM 桩（document/Element），把 app.js 当普通脚本加载，
 * 然后**直接驱动真实的 render()**，断言：
 *   B1 数据没变 → 不重建 DOM（卡片节点身份不变）
 *   B2 数据变了 → 重建
 *   A1 展开后重渲染 → 仍然展开（不会被缩回去）
 *   A2 再点一次 → 收起，且状态被记住
 *   A3 数据变了导致重建 → 已展开的卡片依然展开
 *   C  过滤逻辑统一后行为不变
 *
 * 运行：node ui/test_app_poll.js     （退出码 0 = 全通过）
 */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const APP = path.join(__dirname, "static", "app.js");

/* ---------------- 最小 DOM 桩 ---------------- */
class ClassList {
  constructor() { this.set = new Set(); }
  add(...c) { c.forEach((x) => this.set.add(x)); }
  remove(...c) { c.forEach((x) => this.set.delete(x)); }
  contains(c) { return this.set.has(c); }
  /* 真实 DOM 的 toggle 支持第二个 force 参数：给定布尔值时按它设定、
     不再翻转。app.js 的 setCollapsed 正是用 toggle(cls, bool) —— 桩若忽略
     force，就会变成"每次都翻转"，产生桩自造的假故障。 */
  toggle(c, force) {
    if (force === undefined) {
      if (this.set.has(c)) { this.set.delete(c); return false; }
      this.set.add(c); return true;
    }
    if (force) { this.set.add(c); return true; }
    this.set.delete(c); return false;
  }
}

let NODE_SEQ = 0;

class Node2 {
  constructor(tag) {
    this.tagName = String(tag || "").toUpperCase();
    this._uid = ++NODE_SEQ;
    this.childNodes = [];
    this.parentNode = null;
    this.classList = new ClassList();
    this.dataset = {};
    this.attributes = {};
    this.style = { setProperty() {} };
    this._text = "";
    this.hidden = false;
    this.title = "";
    this._classNames = "";
  }
  /* classList 与 className 必须双向同步——app.js 两种写法都在用
     （createElement 走 className，运行时切换走 classList）。 */
  get className() { return this._classNames; }
  set className(v) {
    this._classNames = String(v || "");
    this.classList.set = new Set(this._classNames.split(/\s+/).filter(Boolean));
  }
  get children() { return this.childNodes.filter((n) => n instanceof Node2); }
  get childElementCount() { return this.children.length; }
  get firstChild() { return this.childNodes[0] || null; }
  appendChild(n) {
    n.parentNode = this;
    this.childNodes.push(n);
    return n;
  }
  remove() {
    if (this.parentNode) {
      const i = this.parentNode.childNodes.indexOf(this);
      if (i >= 0) this.parentNode.childNodes.splice(i, 1);
      this.parentNode = null;
    }
  }
  setAttribute(k, v) { this.attributes[k] = String(v); }
  getAttribute(k) { return this.attributes[k]; }
  addEventListener(ev, fn) {
    (this._handlers = this._handlers || {})[ev] = fn;
  }
  fire(ev) { if (this._handlers && this._handlers[ev]) this._handlers[ev](); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  querySelectorAll(sel) {
    // 测试只用到 ".card" / ".btn-label" / ".elapsed" 三种选择器
    const cls = sel.startsWith(".") ? sel.slice(1) : null;
    if (!cls) return [];
    const out = [];
    const walk = (n) => {
      for (const c of n.children) {
        if (c.classList.contains(cls)) out.push(c);
        walk(c);
      }
    };
    walk(this);
    return out;
  }
  get textContent() {
    if (this.childNodes.length === 0) return this._text;
    return this.childNodes.map((n) => n.textContent).join("");
  }  set textContent(v) {
    this._text = v === null || v === undefined ? "" : String(v);
    /* 真实 DOM 里 textContent = "x" 会**新建一个文本节点**（而不是把字符串
       挂在元素上），所以 firstChild 是有东西的。app.js 里
       `expandBtn.firstChild.textContent = ...` 依赖这个语义；
       桩若不建文本节点，那行就会在 firstChild=null 上炸掉——
       那是桩的假故障，不是 app.js 的问题。 */
    this.childNodes = [];
    if (this._text !== "") {
      const t = new Node2("#text");
      t._text = this._text;
      t.parentNode = this;
      this.childNodes.push(t);
    }
  }
}

/* 按 id 建一批固定节点，供 app.js 顶部的 $("#...") 查找 */
const BY_ID = {};
const IDS = ["demo-banner", "stage", "panel", "scan-btn-stage", "scan-btn",
  "status-line", "round-line", "last-line", "progress", "pause-badges",
  "notice-area", "results-title", "results-badge", "results-count", "filter",
  "results", "empty-state", "idle-state", "motion-toggle", "toast",
  "empty-sub", "sea", "collapse-btn", "collapsed-note"];

function freshDoc() {
  NODE_SEQ = 0;
  Object.keys(BY_ID).forEach((k) => delete BY_ID[k]);
  for (const id of IDS) {
    const n = new Node2("div");
    n.id = id;
    BY_ID[id] = n;
  }
  // 扫描按钮需要一个 .btn-label 子节点（setButtons 会查它）
  for (const id of ["scan-btn-stage", "scan-btn"]) {
    const lab = new Node2("span");
    lab.className = "btn-label";
    BY_ID[id].appendChild(lab);
  }
  // 折叠开关需要一个 .collapse-label 子节点（setCollapsed 会查它）
  {
    const lab = new Node2("span");
    lab.className = "collapse-label";
    BY_ID["collapse-btn"].appendChild(lab);
  }
}

freshDoc();

const documentStub = {
  querySelector: (sel) => {
    if (sel.startsWith("#")) return BY_ID[sel.slice(1)] || null;
    if (sel.startsWith(".")) {
      // app.js 只查过 ".stage-note"
      const n = new Node2("div");
      n.className = sel.slice(1);
      return n;
    }
    return null;
  },
  createElement: (tag) => new Node2(tag),
  createTextNode: (t) => {
    const n = new Node2("#text");
    n.textContent = t === null || t === undefined ? "" : String(t);
    return n;
  },
  body: new Node2("body"),
  execCommand: () => true,
};

/* ---------------- 加载被测代码 ---------------- */
const sandbox = {
  document: documentStub,
  window: {
    matchMedia: () => ({ matches: false }),
    innerWidth: 1200,
    innerHeight: 800,
    addEventListener() {},
    isSecureContext: true,
  },
  navigator: {},
  localStorage: { getItem: () => null, setItem() {} },
  setTimeout: () => 0,
  clearTimeout: () => {},
  setInterval: () => 0,
  clearInterval: () => {},
  requestAnimationFrame: (fn) => fn(),
  console,
  fetch: () => Promise.reject(new Error("no network in test")),
  JSON, Math, Date, Number, String, Array, Object, Set, Boolean, Error,
};

sandbox.Ocean = {
  init: () => false,
  setMotion() {},
  pointer() {},
  transition: () => Promise.resolve(),
  lightSweep() {},
};
sandbox.globalThis = sandbox;

const code = fs.readFileSync(APP, "utf8");
vm.createContext(sandbox);
vm.runInContext(code, sandbox, { filename: "app.js" });

/* ---------------- 断言工具 ---------------- */
let pass = 0, fail = 0;
function check(name, cond, detail) {
  if (cond) { pass++; console.log(`PASS  ${name}`); }
  else { fail++; console.log(`FAIL  ${name}${detail ? "  -- " + detail : ""}`); }
}

/* ---------------- 取被测内部函数 ----------------
 * app.js 是脚本（非模块），顶层 function/let 都在 vm 上下文的全局里。
 * 用 runInContext 取出来，驱动真实的 render()。 */
const grab = (expr) => vm.runInContext(expr, sandbox);

function row(sym, score) {
  return {
    symbol: sym, score, max_score: 18, extra_score: 0,
    percentage_24h: 1.5, deriv_status: "ok", oi_chg_1d: 0.12,
    oi_chg_3d: 0.25, ls_top: 0.9, funding_rate: 0.0001,
    market_scope: "现货域", has_futures: true, conditions: "趋势",
    extra_conditions: "无", c_trend: 1, breakout: 0,
    market_cap: 5e7, spot_quote_volume: 1e6,
  };
}

function stateWith(rows, status) {
  return {
    status: status || "success", rows, mode: "real", min_score: 5,
    started_at: Date.now(), finished_at: Date.now(),
    round: null, round_quality: "unknown", paused: [],
  };
}

const results = () => BY_ID["results"];
const cards = () => results().querySelectorAll(".card");

function cardSymbols() {
  return cards().map((c) => c.dataset.symbol);
}

/* 找到某张卡片的展开按钮（卡片里 class 含 expand-btn 的按钮） */
function expandBtnOf(card) {
  const walk = (n) => {
    for (const c of n.children) {
      if (c.tagName === "BUTTON" && c.classList.contains("expand-btn")) return c;
      const hit = walk(c);
      if (hit) return hit;
    }
    return null;
  };
  return walk(card);
}
function detailsOf(card) {
  const walk = (n) => {
    for (const c of n.children) {
      if (c.tagName === "SECTION" && c.classList.contains("details")) return c;
      const hit = walk(c);
      if (hit) return hit;
    }
    return null;
  };
  return walk(card);
}

/* ---------------- 场景 ---------------- */
console.log("=== B：内容指纹，数据没变就不重建 DOM ===");
{
  const rows = [row("AAA/USDT", 6), row("BBB/USDT", 7)];
  grab("render")(stateWith(rows));
  const before = cards().map((c) => c._uid);
  check("首轮渲染出 2 张卡片", before.length === 2, `got ${before.length}`);

  // 模拟 12 秒轮询：同样的数据再来一次（新对象，值相同）
  grab("render")(stateWith([row("AAA/USDT", 6), row("BBB/USDT", 7)]));
  const after = cards().map((c) => c._uid);
  check("B1 数据未变 → 卡片节点身份完全不变（未重建）",
    JSON.stringify(before) === JSON.stringify(after),
    `before=${before} after=${after}`);
}

{
  const rows = [row("AAA/USDT", 6), row("BBB/USDT", 7)];
  grab("render")(stateWith(rows));
  const before = cards().map((c) => c._uid);
  grab("render")(stateWith([row("AAA/USDT", 9), row("BBB/USDT", 7)]));  // 分数变了
  const after = cards().map((c) => c._uid);
  check("B2 数据变了 → 重建（节点身份改变）",
    JSON.stringify(before) !== JSON.stringify(after));
  check("B2 重建后数量正确", cards().length === 2);
}

console.log("\n=== A：展开状态被记住，轮询不缩回 ===");
{
  grab("render")(stateWith([row("AAA/USDT", 6), row("BBB/USDT", 7)]));
  const c0 = cards()[0];
  const btn = expandBtnOf(c0);
  const det = detailsOf(c0);
  check("A0 初始为收起", !det.classList.contains("open"));

  btn.fire("click");
  check("A0 点击后展开", det.classList.contains("open"));
  check("A0 按钮文案变「收起详情」", btn.textContent.includes("收起详情"),
    `text=${btn.textContent}`);

  // 关键回归：模拟轮询 → 重建 → 展开态必须还在
  grab("render")(stateWith([row("AAA/USDT", 6), row("BBB/USDT", 7)]));
  const c0b = cards()[0];
  const detB = detailsOf(c0b);
  check("A1 轮询后仍然展开（bug 已修）", detB.classList.contains("open"));

  // 再点一次应收起
  expandBtnOf(c0b).fire("click");
  check("A2 再点变回收起", !detB.classList.contains("open"));

  grab("render")(stateWith([row("AAA/USDT", 6), row("BBB/USDT", 7)]));
  check("A2 收起状态也被记住", !detailsOf(cards()[0]).classList.contains("open"));
}

{
  // 数据变化导致强制重建时，A 也要生效
  grab("render")(stateWith([row("AAA/USDT", 6), row("BBB/USDT", 7)]));
  expandBtnOf(cards()[0]).fire("click");
  check("A3 前置：已展开", detailsOf(cards()[0]).classList.contains("open"));

  grab("render")(stateWith([row("AAA/USDT", 12), row("BBB/USDT", 7)]));
  check("A3 重建后已展开的卡片仍展开",
    detailsOf(cards()[0]).classList.contains("open"));
  check("A3 未展开的卡片仍收起",
    !detailsOf(cards()[1]).classList.contains("open"));
}

console.log("\n=== C：过滤逻辑统一后行为不变 ===");
{
  grab("render")(stateWith([row("AAA/USDT", 6), row("BBB/USDT", 7)]));
  grab("currentFilter = 'AAA'");
  grab("applyFilter()");
  check("C1 过滤只留 AAA", cards()[0].hidden === false && cards()[1].hidden === true,
    `hidden=${cards().map((c) => c.hidden)}`);

  grab("currentFilter = ''");
  grab("applyFilter()");
  check("C2 清空过滤恢复全部", cards().every((c) => c.hidden === false));

  // 过滤后重建，过滤词仍然生效（applyFilter 在 renderRows 末尾调用）
  grab("currentFilter = 'BBB'");
  grab("render")(stateWith([row("AAA/USDT", 9), row("BBB/USDT", 7)]));
  check("C3 重建后过滤词仍生效",
    cards()[0].hidden === true && cards()[1].hidden === false,
    `hidden=${cards().map((c) => c.hidden)}`);
  grab("currentFilter = ''");
  grab("applyFilter()");
}

console.log("\n=== D：空结果与重置 ===");
{
  grab("render")(stateWith([row("AAA/USDT", 6)]));
  check("D1 先有 1 张", cards().length === 1);
  grab("render")(stateWith([], "empty"));
  check("D2 空结果清空容器", cards().length === 0);
  grab("render")(stateWith([row("CCC/USDT", 8)]));
  check("D3 空之后再来结果能正常渲染", cards().length === 1
    && cardSymbols()[0] === "CCC/USDT");
}

console.log("\n=== E：指纹只覆盖 rows，状态行仍每次更新 ===");
{
  const s1 = stateWith([row("AAA/USDT", 6)]);
  s1.status = "running";
  grab("render")(s1);
  const runningText = BY_ID["status-line"].textContent;
  check("E1 running 时状态行显示进行中", runningText.includes("正在查看市场线索"),
    `text=${runningText}`);

  const s2 = stateWith([row("AAA/USDT", 6)]);
  s2.status = "success";
  grab("render")(s2);
  const doneText = BY_ID["status-line"].textContent;
  check("E2 数据未变但状态行照常更新为完成",
    doneText.includes("查询完成"), `text=${doneText}`);
}

console.log("\n=== F：整批结果收起/展开 ===");
{
  const resultsEl = results();
  const btn = BY_ID["collapse-btn"];
  const note = BY_ID["collapsed-note"];

  grab("render")(stateWith([row("AAA/USDT", 6), row("BBB/USDT", 7)]));
  check("F0 有结果时显示折叠开关", btn.hidden === false);
  check("F0 初始为展开（无 collapsed 类）",
    !resultsEl.classList.contains("collapsed"));
  check("F0 初始文案为「全部收起」",
    btn.querySelector(".collapse-label").textContent === "全部收起",
    `label=${btn.querySelector(".collapse-label").textContent}`);

  btn.fire("click");
  check("F1 点击后结果区折叠（collapsed 类）",
    resultsEl.classList.contains("collapsed"));
  check("F1 文案变「全部展开」",
    btn.querySelector(".collapse-label").textContent === "全部展开");
  check("F1 aria-expanded=false", btn.getAttribute("aria-expanded") === "false");
  check("F1 收起提示行出现", note.hidden === false);
  check("F1 只是隐藏，卡片节点没被销毁", cards().length === 2);

  // 关键回归：收起后轮询（数据没变）不能被冲回展开
  grab("render")(stateWith([row("AAA/USDT", 6), row("BBB/USDT", 7)]));
  check("F2 轮询后仍保持收起",
    resultsEl.classList.contains("collapsed"));

  // 数据变了强制重建也要保持收起
  grab("render")(stateWith([row("AAA/USDT", 9), row("BBB/USDT", 7)]));
  check("F2 数据变化重建后仍保持收起",
    resultsEl.classList.contains("collapsed"));

  btn.fire("click");
  check("F3 再点恢复展开", !resultsEl.classList.contains("collapsed"));
  check("F3 文案回到「全部收起」",
    btn.querySelector(".collapse-label").textContent === "全部收起");
  check("F3 收起提示行收起来", note.hidden === true);

  // 卡片级展开态与整批折叠互不干扰。
  // 注意 openSymbols 是跨用例共享的 Set，进本节前先清干净，
  // 否则会继承 A 组残留的展开态，点一下反而把它关掉。
  grab("openSymbols.clear()");
  grab("render")(stateWith([row("AAA/USDT", 6), row("BBB/USDT", 7)]));
  check("F4 前置：卡片初始收起",
    !detailsOf(cards()[0]).classList.contains("open"));
  expandBtnOf(cards()[0]).fire("click");
  check("F4 前置：点开后展开",
    detailsOf(cards()[0]).classList.contains("open"));

  btn.fire("click");   // 整批收起
  check("F4 整批收起不影响卡片级展开态",
    detailsOf(cards()[0]).classList.contains("open"));
  btn.fire("click");   // 整批展开
  check("F4 整批收起再展开后，卡片级展开态仍在",
    detailsOf(cards()[0]).classList.contains("open"));

  // 零结果时不显示折叠开关
  grab("render")(stateWith([], "empty"));
  check("F5 零结果时隐藏折叠开关", btn.hidden === true);
  check("F5 零结果时结果区不残留 collapsed",
    !resultsEl.classList.contains("collapsed"));
}

console.log(`\n${pass} 项通过，${fail} 项失败`);
process.exit(fail ? 1 : 0);
