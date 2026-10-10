/* 观潮 · 个人选币工作台 —— 前端交互（V2）
 * 原则：只渲染 /api/state 的真实状态；外部字段一律 textContent；不编造进度。
 * 场景（ocean.js）与请求逻辑相互独立：动画不延迟业务动作，业务不感知渲染。
 */
"use strict";

const $ = (sel) => document.querySelector(sel);

const els = {
  demoBanner: $("#demo-banner"),
  stage: $("#stage"),
  stageNote: $(".stage-note"),
  panel: $("#panel"),
  scanBtnStage: $("#scan-btn-stage"),
  scanBtn: $("#scan-btn"),
  statusLine: $("#status-line"),
  roundLine: $("#round-line"),
  lastLine: $("#last-line"),
  progress: $("#progress"),
  pauseBadges: $("#pause-badges"),
  noticeArea: $("#notice-area"),
  resultsTitle: $("#results-title"),
  resultsBadge: $("#results-badge"),
  resultsCount: $("#results-count"),
  filter: $("#filter"),
  results: $("#results"),
  emptyState: $("#empty-state"),
  idleState: $("#idle-state"),
  motionToggle: $("#motion-toggle"),
  toast: $("#toast"),
};

const DERIV_LABELS = {
  ok: "正常", partial: "部分缺失", failed: "获取失败",
  paused: "限流暂停", no_futures: "无合约",
};
const DERIV_DOTS = {
  ok: "dot-ok", partial: "dot-partial", failed: "dot-failed",
  paused: "dot-paused", no_futures: "dot-none",
};
const DOMAIN_LABELS = { spot: "现货域", futures: "合约域" };

let TERMS = {};
let currentFilter = "";
let elapsedTimer = null;
let pollTimer = null;
let toastTimer = null;
let lastState = null;
let panelMode = false;      // false = 入场舞台；true = 观测面板
let stageHidden = false;

/* —— 轮询重渲染的两道防护（A + B）——
 *
 * 背景：空闲时每 12 秒拉一次 /api/state，而 render() 只要 status==="success"
 * 就调 renderRows()；renderRows() 会 `textContent = ""` 清空容器再重建全部卡片。
 * 后果：已经点开的"展开详情"被折叠回去，滚动位置、文字选中、按钮焦点一并丢失。
 *
 * B（主）：内容指纹 — 数据没变就一个 DOM 节点都不动，问题从根上消失。
 * A（兜底）：展开状态 — 万一数据真的变了要重建，也按 symbol 记住哪些是展开的。
 */
let lastRowsKey = null;          // B：上一次真正渲染过的内容指纹
const openSymbols = new Set();   // A：哪些卡片处于展开状态（按 symbol，不用位置）
let lastRenderedRows = [];       // 最近一次真正渲染进 DOM 的行（供调试/未来复用）

/* B 用：把影响展示的字段摘出来做指纹。
 * 只取卡片可见字段，不取整个 row —— 否则后端加个无关字段就会让指纹变化、
 * 白白重建 DOM，B 就失效了。字段有增减时记得同步这里。 */
const ROWS_KEY_FIELDS = [
  "symbol", "score", "extra_score", "percentage_24h",
  "deriv_status", "oi_chg_1d", "oi_chg_3d", "ls_top", "ls_global",
  "funding_rate", "market_scope", "okx_contract", "has_futures",
  "conditions", "extra_conditions", "c_trend", "breakout",
];

function rowsFingerprint(rows) {
  if (!Array.isArray(rows)) return "";
  return JSON.stringify(rows.map((row) =>
    ROWS_KEY_FIELDS.map((k) => (row && row[k] !== undefined ? row[k] : null))));
}

/* ---------- 格式化 ---------- */

function isNum(v) { return typeof v === "number" && Number.isFinite(v); }

function fmtMoney(v) {
  if (!isNum(v) || v === 0) return null;
  const abs = Math.abs(v);
  if (abs >= 1e12) return (v / 1e12).toFixed(2) + "T";
  if (abs >= 1e9) return (v / 1e9).toFixed(2) + "B";
  if (abs >= 1e6) return (v / 1e6).toFixed(2) + "M";
  if (abs >= 1e3) return (v / 1e3).toFixed(2) + "K";
  return v.toFixed(0);
}

/* oi_chg_* 是小数（0.12 = 12%）→ 乘以 100，与后端 render_table 一致 */
function fmtFractionPct(v, digits = 1) {
  if (!isNum(v)) return null;
  return (v >= 0 ? "+" : "") + (v * 100).toFixed(digits) + "%";
}

/* percentage_24h / box_width_pct 本身已是百分数 → 直接加 %，不再乘 100 */
function fmtRawPct(v, digits = 1) {
  if (!isNum(v)) return null;
  return (v >= 0 ? "+" : "") + v.toFixed(digits) + "%";
}

function fmtRatio(v, digits = 2) { return isNum(v) ? v.toFixed(digits) : null; }

function fmtFunding(v) {
  if (!isNum(v)) return null;
  return (v * 100).toFixed(3) + "%";
}

function fmtTime(ms) {
  if (!isNum(ms)) return null;
  const d = new Date(ms);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getMonth() + 1}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function fmtDuration(ms) {
  if (!isNum(ms) || ms < 0) return "0 秒";
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s} 秒`;
  return `${Math.floor(s / 60)} 分 ${s % 60} 秒`;
}

/* ---------- DOM 小工具 ---------- */

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

function kvRow(label, value, termId) {
  const dt = el("dt", null, label);
  if (termId && TERMS[termId]) {
    dt.classList.add("term-hint");
    dt.title = `${TERMS[termId].zh}：${TERMS[termId].plain}（详见 LEARNING.md）`;
  }
  const dd = el("dd");
  if (value === null || value === undefined || value === "") {
    dd.textContent = "暂无";
    dd.classList.add("na");
  } else {
    dd.textContent = value;
  }
  return [dt, dd];
}

function toast(msg) {
  els.toast.textContent = msg;
  els.toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { els.toast.hidden = true; }, 1800);
}

async function copyText(text, okMsg) {
  try {
    if (navigator.clipboard && window.isSecureContext !== false) {
      await navigator.clipboard.writeText(text);
    } else {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand("copy");
      ta.remove();
      if (!ok) throw new Error("execCommand failed");
    }
    toast(okMsg);
  } catch {
    toast("复制失败，请长按/右键手动复制");
  }
}

function researchPrompt(row) {
  const name = String(row.symbol || "").replace("/USDT", "");
  return [
    `请帮我研究加密货币 ${name}（交易对 ${row.symbol}）。只梳理可核验的公开事实，不预测价格、不给买卖建议：`,
    `1. 最近30天的公开事件：项目公告、交易所上币或下架、新合约上线、代币解锁、合作与治理变动，逐条附来源链接与时间；`,
    `2. 社区讨论：X/Twitter 与论坛的提及度变化、主要讨论主题、代表性账号，注意区分真实讨论与机器人刷屏；`,
    `3. 相反证据：质疑观点、负面消息、团队或大户的抛压迹象、流动性与合规风险；`,
    `4. 最后给出来源清单，标注哪些是单一来源、尚未交叉验证。`,
  ].join("\n");
}

/* ---------- 卡片渲染 ---------- */

function splitConditions(text) {
  if (!text || text === "无") return [];
  return String(text).split(",").map((s) => s.trim()).filter(Boolean);
}

function derivInfo(row) {
  const key = row.has_futures ? String(row.deriv_status || "") : "no_futures";
  return {
    label: DERIV_LABELS[key] || "暂无",
    dot: DERIV_DOTS[key] || "dot-none",
    key,
  };
}

function buildDetailGroup(title, rows) {
  const group = el("div", "detail-group");
  group.appendChild(el("h3", null, title));
  const dl = el("dl", "kv");
  for (const [label, value, termId] of rows) {
    for (const node of kvRow(label, value, termId)) dl.appendChild(node);
  }
  group.appendChild(dl);
  return group;
}

function buildCard(row, index) {
  const card = el("article", "card");
  card.style.setProperty("--i", Math.min(index, 12));
  const symbol = String(row.symbol || "");
  card.dataset.symbol = symbol.toUpperCase();
  const name = symbol.replace("/USDT", "");

  const top = el("div", "card-top");
  const coin = el("div", "coin", name);
  coin.appendChild(el("span", "pair", " /USDT"));
  top.appendChild(coin);
  const badge = el("div", "score-badge");
  badge.appendChild(document.createTextNode(`${row.score ?? "—"}`));
  badge.appendChild(el("small", null, `/ ${row.max_score ?? "—"} 分`));
  top.appendChild(badge);
  card.appendChild(top);
  card.appendChild(el("div", "score-note", "规则得分 · 不是上涨概率"));

  const tags = el("div", "card-tags");
  tags.appendChild(el("span", "pill", row.market_scope || "暂无"));
  const deriv = derivInfo(row);
  const derivPill = el("span", `pill ${deriv.dot}`);
  derivPill.appendChild(el("span", "dot"));
  derivPill.appendChild(document.createTextNode(`合约数据 · ${deriv.label}`));
  if (TERMS.deriv_status) {
    derivPill.title = `${TERMS.deriv_status.plain}（详见 LEARNING.md）`;
  }
  tags.appendChild(derivPill);
  if (isNum(row.extra_score) && row.extra_score > 0) {
    const aux = el("span", "pill pill-aux", `辅助 +${row.extra_score}`);
    if (TERMS.extra_score) aux.title = `${TERMS.extra_score.plain}（详见 LEARNING.md）`;
    tags.appendChild(aux);
  }
  if (row.okx_contract) tags.appendChild(el("span", "pill", "OKX 也有合约"));
  card.appendChild(tags);

  const reasons = el("div", "reasons");
  const hits = splitConditions(row.conditions);
  hits.slice(0, 3).forEach((h) => reasons.appendChild(el("span", "reason-chip", h)));
  if (hits.length > 3) reasons.appendChild(el("span", "reason-more", `+${hits.length - 3}`));
  if (!hits.length) reasons.appendChild(el("span", "reason-more", "无核心命中条件"));
  card.appendChild(reasons);

  const change = el("div", "change24");
  change.appendChild(document.createTextNode("24h 涨跌 "));
  const pct = fmtRawPct(row.percentage_24h);
  if (pct === null) {
    change.appendChild(el("span", null, "暂无"));
  } else {
    change.appendChild(el("span", row.percentage_24h >= 0 ? "up" : "down", pct));
  }
  card.appendChild(change);

  const expandBtn = el("button", "expand-btn", "展开详情");
  expandBtn.type = "button";
  const details = el("section", "details");
  /* A：按 symbol 恢复展开状态。轮询重建 DOM 后，已经点开的卡片不会缩回去。
     用 symbol 而不是列表位置——币的排序会随分数/成交额变化，位置不可靠。 */
  const openKey = symbol.toUpperCase();
  if (openSymbols.has(openKey)) {
    details.classList.add("open");
    expandBtn.setAttribute("aria-expanded", "true");
    expandBtn.textContent = "收起详情";
  } else {
    expandBtn.setAttribute("aria-expanded", "false");
  }
  expandBtn.addEventListener("click", () => {
    const open = details.classList.toggle("open");
    if (open) openSymbols.add(openKey); else openSymbols.delete(openKey);
    expandBtn.setAttribute("aria-expanded", String(open));
    expandBtn.firstChild.textContent = open ? "收起详情" : "展开详情";
  });
  card.appendChild(expandBtn);

  const lsIsFallback = !!(row.ls_source && String(row.ls_source).includes("回退"));
  const lsDetails = [["大户持仓多空比", fmtRatio(row.ls_top), "ls_top"]];
  if (lsIsFallback) {
    // R2：回退口径是全市场账户比，不是大户持仓比——分开展示，不冒充
    lsDetails.push(["全市场账户比（回退，仅供参考）", fmtRatio(row.ls_global), "ls_global"]);
  }
  details.appendChild(buildDetailGroup("合约数据", [
    ["数据状态", deriv.label, "deriv_status"],
    ["OI 1日增速", fmtFractionPct(row.oi_chg_1d), "oi_chg_1d"],
    ["OI 3日增速", fmtFractionPct(row.oi_chg_3d), "oi_chg_3d"],
    ...lsDetails,
    ["资金费率", fmtFunding(row.funding_rate), "funding_rate"],
    ["未平仓合约价值", fmtMoney(row.open_interest_value), "oi"],
    ["OI / 市值", fmtRatio(row.oi_cap_ratio), null],
    ["数据时间", fmtTime(row.deriv_as_of), null],
  ]));
  details.appendChild(buildDetailGroup("市场", [
    ["市值", fmtMoney(row.market_cap), "market_cap"],
    ["现货成交额 24h", fmtMoney(row.spot_quote_volume), "quote_volume"],
    ["合约成交额 24h", fmtMoney(row.futures_quote_volume), null],
    ["成交额 / 市值", fmtRatio(row.volume_cap_ratio), null],
  ]));
  details.appendChild(buildDetailGroup("K 线", [
    ["趋势（收盘 > EMA50）", row.c_trend ? "是" : "否", "c_trend"],
    ["已突破箱体", row.breakout ? "是" : "否", "box"],
    ["箱体宽度", isNum(row.box_width_pct) ? row.box_width_pct.toFixed(1) + "%" : null, null],
    ["量比", fmtRatio(row.volume_ratio), null],
    ["24h 量能比", isNum(row.volume_24h_growth) ? row.volume_24h_growth.toFixed(2) + "x" : null, null],
  ]));

  const condGroup = el("div", "detail-group");
  condGroup.appendChild(el("h3", null, "命中原因"));
  const coreWrap = el("div", "reasons");
  const core = splitConditions(row.conditions);
  if (core.length) {
    core.forEach((h) => coreWrap.appendChild(el("span", "reason-chip", h)));
  } else {
    coreWrap.appendChild(el("span", "reason-more", "无"));
  }
  condGroup.appendChild(coreWrap);
  const auxHits = splitConditions(row.extra_conditions);
  if (auxHits.length) {
    condGroup.appendChild(el("p", "aux-note", `辅助条件（不参与门槛）：${auxHits.join("、")}`));
  }
  details.appendChild(condGroup);
  card.appendChild(details);

  const actions = el("div", "card-actions");
  const copyName = el("button", null, "复制币名");
  copyName.type = "button";
  copyName.addEventListener("click", () => copyText(name, `已复制 ${name}`));
  const copyPrompt = el("button", null, "复制研究提示词");
  copyPrompt.type = "button";
  copyPrompt.addEventListener("click", () => copyText(researchPrompt(row), "已复制研究提示词"));
  actions.appendChild(copyName);
  actions.appendChild(copyPrompt);
  card.appendChild(actions);

  return card;
}

function renderRows(rows) {
  els.results.textContent = "";
  lastRenderedRows = rows;
  rows.forEach((row, i) => {
    els.results.appendChild(buildCard(row, i));
  });
  applyFilter();
}

/* 过滤只保留这一处实现。此前 renderRows 和 input 事件各写了一份判断，
   改一处漏一处的风险很实在（比如以后想改成匹配币名而非交易对）。 */
function applyFilter() {
  const kw = currentFilter.toUpperCase();
  for (const card of els.results.querySelectorAll(".card")) {
    card.hidden = !!(kw && !card.dataset.symbol.includes(kw));
  }
}

/* ---------- 状态渲染 ---------- */

function setNotice(kind, text, timeMs) {
  els.noticeArea.textContent = "";
  if (!text) return;
  const box = el("div", `notice notice-${kind}`);
  box.appendChild(el("span", null, text));
  const t = fmtTime(timeMs);
  if (t) box.appendChild(el("span", "notice-time", t));
  els.noticeArea.appendChild(box);
}

function renderPauses(paused) {
  els.pauseBadges.textContent = "";
  for (const p of paused || []) {
    const domain = DOMAIN_LABELS[p.domain] || p.domain || "数据域";
    const until = fmtTime(p.until_ms) || "稍后";
    const chip = el("span", "pause-chip", `${domain}限流暂停中 · 至 ${until}`);
    if (p.reason) chip.title = p.reason;
    els.pauseBadges.appendChild(chip);
  }
}

function stopElapsed() {
  clearInterval(elapsedTimer);
  elapsedTimer = null;
}

function startElapsed(startedAt) {
  stopElapsed();
  const tick = () => {
    const elNode = els.statusLine.querySelector(".elapsed");
    if (elNode) elNode.textContent = fmtDuration(Date.now() - startedAt);
  };
  tick();
  elapsedTimer = setInterval(tick, 1000);
}

function setButtons(running) {
  for (const btn of [els.scanBtnStage, els.scanBtn]) {
    btn.disabled = running;
    btn.querySelector(".btn-label").textContent = running ? "正在查看市场线索…" : "查询标的";
  }
}

/* 视图切换：入场舞台 ⇄ 观测面板（带动效，不回头） */
function showPanel() {
  if (panelMode) return;
  panelMode = true;
  document.body.classList.add("panel-on");
  els.panel.hidden = false;
  els.stage.classList.add("leaving");
  if (!stageHidden) {
    stageHidden = true;
    setTimeout(() => { els.stage.hidden = true; }, 600);
  }
}

/* R2 整轮摘要：数据完整性与"本轮无候选"必须由后端摘要表达，不能由卡片反推。
   complete=数据完整（无候选是正常结果）；incomplete=本轮不完整，不得当完整推荐。 */
function renderRound(round, quality) {
  const elNode = els.roundLine;
  if (!elNode) return;
  if (!round || quality === "unknown") {
    elNode.hidden = true;
    elNode.textContent = "";
    return;
  }
  const c = round.deriv_counts || {};
  const expected = round.futures_expected;
  const expectedText = (expected === null || expected === undefined) ? "未知" : String(expected);
  if (quality === "complete") {
    elNode.className = "round-line round-ok";
    elNode.textContent = round.has_passing
      ? `数据完整 · 合约数据 ${round.futures_fetched_ok}/${expectedText} · 异常 ${round.errors ?? 0}`
      : `数据完整 · 本轮无候选（过门槛 ${round.chosen_count ?? 0}）`;
  } else {
    elNode.className = "round-line round-warn";
    const domain = round.deriv_domain_state === "no_client"
      ? " · 合约数据本轮不可用" : "";
    elNode.textContent =
      `本轮不完整 · 合约数据 ${round.futures_fetched_ok ?? "?"}/${expectedText}`
      + ` · 暂停 ${c.paused ?? 0} 失败 ${c.failed ?? 0}`
      + ` 陈旧 ${round.stale_rows ?? 0}`
      + ` 跳过 ${round.skipped_short_history ?? 0} 异常 ${round.errors ?? "?"}`
      + `${domain} —— 不作为完整推荐`;
  }
  elNode.hidden = false;
}

function render(state) {
  lastState = state;
  const { status, rows, last, error } = state;
  const demo = state.mode === "demo";
  els.demoBanner.hidden = !demo;
  renderRound(state.round, state.round_quality);

  // 视图归属：纯初始（idle 且无历史）→ 舞台；其余一律进面板
  const shouldBePanel = status !== "idle" || !!last;
  if (shouldBePanel) showPanel();

  const running = status === "running";
  setButtons(running);
  els.progress.hidden = !running;

  const startedText = fmtTime(state.started_at);
  if (running) {
    els.statusLine.textContent = "";
    els.statusLine.appendChild(document.createTextNode(
      `正在查看市场线索 · 开始于 ${startedText || "—"} · 已耗时 `));
    els.statusLine.appendChild(el("span", "elapsed", "0 秒"));
    startElapsed(state.started_at || Date.now());
  } else {
    stopElapsed();
    if (status === "success") {
      els.statusLine.textContent = `查询完成 · 本轮 ${(rows || []).length} 个标的`;
    } else if (status === "empty") {
      els.statusLine.textContent = "查询完成 · 这一轮暂未发现符合条件的标的";
    } else if (status === "failed") {
      els.statusLine.textContent = "本轮查询失败";
    } else {
      els.statusLine.textContent = "待查询 · 点击按钮开始一轮真实扫描";
    }
  }

  const lastTime = (status === "success" || status === "empty")
    ? state.finished_at : (last && last.finished_at);
  els.lastLine.textContent = `上次完成：${fmtTime(lastTime) || "—"}`;

  renderPauses(state.paused);

  const showStale = (status === "running" || status === "failed" || status === "idle") && last;
  if (status === "failed") {
    setNotice("error", error || "未知错误", state.finished_at);
  } else if (status === "running" && last) {
    setNotice("info", "扫描进行中，下方仍是上次成功的结果", last.finished_at);
  } else {
    setNotice(null, null);
  }

  const minScore = isNum(state.min_score) ? ` · 评分 ≥ ${state.min_score}` : "";
  if (status === "success" && Array.isArray(rows)) {
    els.idleState.hidden = true;
    els.emptyState.hidden = true;
    els.resultsTitle.textContent = "入选标的";
    els.resultsBadge.hidden = true;
    els.resultsCount.textContent = `本轮 ${rows.length} 个${minScore}`;
    /* B：数据没变就一个 DOM 节点都不动。
       这样"展开详情"、滚动位置、选中的文字、按钮焦点全部自然保留。
       注意指纹**只覆盖 rows**：状态行/摘要/暂停徽章仍然每次照常更新，
       它们本来就该跟着最新状态走，不在 B 的拦截范围内。 */
    const key = rowsFingerprint(rows);
    if (key !== lastRowsKey || els.results.childElementCount === 0) {
      lastRowsKey = key;
      renderRows(rows);
    }
  } else if (status === "empty") {
    els.idleState.hidden = true;
    els.emptyState.hidden = false;
    $("#empty-sub").textContent = state.round_quality === "incomplete"
      ? "本轮部分数据未取到（见上方摘要）——不能据此认为“没有候选”，建议稍后重试。"
      : "候选池里没有币种达到评分门槛。潮水平静的时候也是常态。";
    els.resultsTitle.textContent = "入选标的";
    els.resultsBadge.hidden = true;
    els.resultsCount.textContent = `本轮 0 个${minScore}`;
    if (els.results.childElementCount) {
      els.results.textContent = "";
      lastRowsKey = null;      // 结果已被清空 → 下次必须重新渲染
    }
  } else if (showStale) {
    els.idleState.hidden = true;
    els.emptyState.hidden = true;
    els.resultsTitle.textContent = "入选标的";
    els.resultsBadge.hidden = false;
    if (last.status === "empty" || !(last.rows || []).length) {
      els.resultsCount.textContent = "上次为零结果";
      if (els.results.childElementCount) {
        els.results.textContent = "";
        lastRowsKey = null;
      }
      els.emptyState.hidden = false;
      $("#empty-sub").textContent =
        `上次查询（${fmtTime(last.finished_at) || "—"}）没有发现符合条件的标的。`;
    } else {
      els.resultsCount.textContent = `上次 ${last.rows.length} 个 · ${fmtTime(last.finished_at) || "—"}`;
      const key = rowsFingerprint(last.rows);
      if (key !== lastRowsKey || els.results.childElementCount === 0) {
        lastRowsKey = key;
        renderRows(last.rows);
      }
    }
  } else {
    els.resultsTitle.textContent = "入选标的";
    els.resultsBadge.hidden = true;
    els.resultsCount.textContent = "";
    if (els.results.childElementCount) {
      els.results.textContent = "";
      lastRowsKey = null;
    }
    els.emptyState.hidden = true;
    els.idleState.hidden = true;
  }

  if (demo) {
    els.stageNote.textContent = "演示模式 · 依次呈现：有结果 → 零结果 → 失败（内置样例）";
  }
}

/* ---------- 轮询与事件 ---------- */

async function fetchState() {
  try {
    const res = await fetch("/api/state", { headers: { "Accept": "application/json" } });
    if (!res.ok) throw new Error(String(res.status));
    const state = await res.json();
    render(state);
    schedulePoll(state.status === "running" ? 1500 : 12000);
  } catch {
    schedulePoll(5000);
  }
}

function schedulePoll(delay) {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(fetchState, delay);
}

async function triggerScan() {
  const firstFromStage = !panelMode;
  setButtons(true);
  if (firstFromStage) {
    // 镜头前推与请求同时起步；动画不阻塞业务
    Ocean.transition().then(showPanel);
  } else {
    Ocean.lightSweep();
  }
  try {
    const res = await fetch("/api/scan", { method: "POST" });
    if (res.status === 409) {
      toast("上一轮还在进行中");
    } else if (!res.ok) {
      toast("启动查询失败，请稍后再试");
    }
  } catch {
    toast("无法连接本地服务");
  } finally {
    setTimeout(fetchState, 400);
  }
}

els.scanBtnStage.addEventListener("click", triggerScan);
els.scanBtn.addEventListener("click", triggerScan);

els.filter.addEventListener("input", () => {
  currentFilter = els.filter.value.trim();
  applyFilter();
});

/* ---------- 场景初始化与动效开关 ---------- */

function setupScene() {
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const saved = localStorage.getItem("chuanmu-motion");
  const motion = saved !== null ? saved === "1" : !reduced;

  const ok = Ocean.init($("#sea"));
  if (ok) {
    document.body.classList.add("sea-on");
  } else {
    // 静帧后备：CSS 海天渐层已就位，明确提示效果未加载
    document.body.classList.add("sea-on");
    console.warn("[观潮] WebGL 不可用，已切换为静态海面背景");
  }
  Ocean.setMotion(motion);
  updateMotionToggle(motion, ok);

  let rafPending = false;
  window.addEventListener("pointermove", (ev) => {
    if (rafPending) return;
    rafPending = true;
    requestAnimationFrame(() => {
      rafPending = false;
      Ocean.pointer((ev.clientX / window.innerWidth) * 2 - 1,
                    (ev.clientY / window.innerHeight) * 2 - 1);
    });
  }, { passive: true });
}

function updateMotionToggle(on, sceneOk) {
  els.motionToggle.textContent = on ? "静态" : "动态";
  els.motionToggle.setAttribute("aria-pressed", String(on));
  els.motionToggle.title = on ? "暂停海面动态（切换为静帧）" : "恢复海面动态";
  if (!sceneOk) els.motionToggle.hidden = true;
}

els.motionToggle.addEventListener("click", () => {
  const on = els.motionToggle.textContent.trim() === "静态";
  Ocean.setMotion(!on);
  localStorage.setItem("chuanmu-motion", on ? "0" : "1");
  updateMotionToggle(!on, true);
});

(async function init() {
  setupScene();
  try {
    const res = await fetch("/api/terms");
    if (res.ok) TERMS = await res.json();
  } catch { TERMS = {}; }
  fetchState();
})();
