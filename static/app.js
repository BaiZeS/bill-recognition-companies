"use strict";

const state = {
  tasks: [],
  currentId: null,
  result: null,
  zoom: 1,
  pollTimer: null,
  prevStatus: {},
  view: "restore",
  extractTab: "result",
  rules: null,
  extract: { loading: false, error: "", groups: null, model: "" },
  taskMeta: { filename: "", output_name: "" },
};

const LINKS = {}; // id -> [id]（kv ↔ 来源行）
const EXTRACT_LINKS = {}; // 结果行 key ↔ 来源元素 id（双向）
const KV_LINE = {}; // kv.id -> 来源文本行 id
let idIndex = {}; // 元素 id -> {text, page}
let fieldRegistry = [];

const $ = (sel) => document.querySelector(sel);
const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

function init() {
  $("#uploadBtn").addEventListener("click", () => $("#fileInput").click());
  $("#fileInput").addEventListener("change", (e) => {
    if (e.target.files.length) uploadFiles(e.target.files);
    e.target.value = "";
  });
  $("#saveBtn").addEventListener("click", save);
  $("#downloadBtn").addEventListener("click", () => {
    if (state.currentId) window.location.href = `/api/tasks/${state.currentId}/download`;
  });
  $("#ghostRange").addEventListener("input", (e) => {
    document.documentElement.style.setProperty("--ghost", e.target.value / 100);
  });
  $("#zoomIn").addEventListener("click", () => setZoom(state.zoom + 0.1));
  $("#zoomOut").addEventListener("click", () => setZoom(state.zoom - 0.1));
  $("#zoomFit").addEventListener("click", () => setZoom(1));

  $("#viewTabs").addEventListener("click", (e) => {
    const btn = e.target.closest(".tab");
    if (btn && !btn.disabled) switchView(btn.dataset.view);
  });
  $("#extractTabs").addEventListener("click", (e) => {
    const btn = e.target.closest(".tab");
    if (btn) switchExtractTab(btn.dataset.tab);
  });
  $("#extractBtn").addEventListener("click", runExtract);
  $("#extractDownloadBtn").addEventListener("click", downloadExtracted);

  renderEmptyPanes();

  let resizeTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      if (state.result) {
        renderPreview();
        if (state.view === "restore") renderSheets();
      }
    }, 150);
  });

  pollTasks();
}

function showToast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(t._timer);
  t._timer = setTimeout(() => (t.hidden = true), 3000);
}

async function uploadFiles(files) {
  const fd = new FormData();
  for (const f of files) fd.append("files", f);
  fd.append("dewm", $("#dewmCheck").checked ? "true" : "false");
  fd.append("deseal", $("#desealCheck").checked ? "true" : "false");
  const resp = await fetch("/api/upload", { method: "POST", body: fd });
  if (!resp.ok) {
    showToast(`上传失败：${(await resp.json().catch(() => ({}))).detail || resp.status}`);
    return;
  }
  const data = await resp.json();
  if (data.tasks.length) {
    state.currentId = data.tasks[0].id;
  }
  pollTasks();
}

async function pollTasks() {
  clearTimeout(state.pollTimer);
  state.pollTimer = null;
  const resp = await fetch("/api/tasks");
  if (!resp.ok) return;
  state.tasks = (await resp.json()).tasks;
  renderTaskList();

  const cur = state.tasks.find((t) => t.id === state.currentId);
  if (cur) {
    const prev = state.prevStatus[cur.id];
    if (cur.status === "done" && prev !== "done") {
      openTask(cur.id);
    } else if (cur.status !== "done") {
      renderWorkingPanes(cur);
    }
  } else if (state.currentId) {
    state.currentId = null;
    state.result = null;
    renderEmptyPanes();
  }

  state.prevStatus = Object.fromEntries(state.tasks.map((t) => [t.id, t.status]));
  if (state.tasks.some((t) => t.status === "pending" || t.status === "processing")) {
    state.pollTimer = setTimeout(pollTasks, 2000);
  }
}

function statusText(t) {
  switch (t.status) {
    case "pending":
      return "待处理";
    case "processing":
      return t.page_count ? `识别中 ${t.pages_done}/${t.page_count} 页` : "识别中";
    case "done":
      return "完成";
    case "failed":
      return "失败";
    default:
      return t.status;
  }
}

function renderTaskList() {
  const list = $("#taskList");
  list.innerHTML = "";
  $("#taskEmpty").hidden = state.tasks.length > 0;
  for (const t of state.tasks) {
    const li = document.createElement("li");
    li.className = "task-item" + (t.id === state.currentId ? " selected" : "");
    li.tabIndex = 0;

    const dot = document.createElement("span");
    dot.className = `dot dot-${t.status}`;

    const meta = document.createElement("div");
    meta.className = "task-meta";
    const name = document.createElement("div");
    name.className = "task-name";
    name.textContent = t.filename;
    name.title = t.filename;
    const st = document.createElement("div");
    st.className = "task-status";
    st.textContent = t.status === "failed" ? `失败：${t.error || ""}` : statusText(t);
    if (t.status === "failed") st.title = t.error || "";
    meta.append(name, st);
    li.append(dot, meta);

    if (t.status === "done" || t.status === "failed") {
      const del = document.createElement("button");
      del.className = "task-del";
      del.textContent = "删除";
      del.addEventListener("click", (e) => {
        e.stopPropagation();
        deleteTask(t);
      });
      li.append(del);
    }

    const select = () => {
      state.currentId = t.id;
      renderTaskList();
      openTask(t.id);
    };
    li.addEventListener("click", select);
    li.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        select();
      }
    });
    list.append(li);
  }
}

async function deleteTask(t) {
  if (!window.confirm(`确定删除任务「${t.filename}」吗？`)) return;
  const resp = await fetch(`/api/tasks/${t.id}`, { method: "DELETE" });
  if (!resp.ok) {
    showToast(`删除失败：${(await resp.json().catch(() => ({}))).detail || resp.status}`);
    return;
  }
  if (state.currentId === t.id) {
    state.currentId = null;
    state.result = null;
    renderEmptyPanes();
  }
  pollTasks();
}

async function openTask(id) {
  state.currentId = id;
  const resp = await fetch(`/api/tasks/${id}`);
  if (!resp.ok) return;
  const t = await resp.json();
  if (t.status !== "done") {
    renderWorkingPanes(t);
    return;
  }
  const r = await fetch(`/api/tasks/${id}/result`);
  if (!r.ok) {
    renderWorkingPanes(t);
    return;
  }
  state.result = await r.json();
  state.zoom = 1;
  state.taskMeta = { filename: t.filename, output_name: t.output_name };
  resetExtractState();
  buildLinks();
  buildIdIndex();
  setExtractTabEnabled(true);
  // 先切回复原视图使容器可见，renderSheets 才能量到正确宽度
  switchView("restore");
  renderPreview();
  renderSheets();
}

function resetExtractState() {
  state.extract = { loading: false, error: "", groups: null, model: "", dirty: true };
  for (const k of Object.keys(EXTRACT_LINKS)) delete EXTRACT_LINKS[k];
}

function resetPanesChrome() {
  state.view = "restore";
  state.extractTab = "result";
  setExtractTabEnabled(false);
  $("#restorePane").hidden = false;
  $("#extractPane").hidden = true;
  $("#toolbar").hidden = true;
  $("#rulesPanel").hidden = true;
  resetExtractState();
  for (const btn of document.querySelectorAll("#viewTabs .tab")) {
    btn.classList.toggle("active", btn.dataset.view === "restore");
  }
}

function renderWorkingPanes(t) {
  resetPanesChrome();
  state.result = null;
  const msg =
    t.status === "failed"
      ? `识别失败：${t.error || "未知错误"}`
      : t.status === "pending"
        ? "待处理，排队等待识别"
        : statusText(t);
  const cls = t.status === "failed" ? "placeholder placeholder-error" : "placeholder";
  $("#previewBody").innerHTML = `<div class="${cls}"></div>`;
  $("#previewBody").firstChild.textContent = msg;
  $("#formBody").innerHTML = `<div class="${cls}"></div>`;
  $("#formBody").firstChild.textContent = msg;
}

function renderEmptyPanes() {
  resetPanesChrome();
  for (const sel of ["#previewBody", "#formBody"]) {
    const body = $(sel);
    body.innerHTML = "";
    const div = document.createElement("div");
    div.className = "placeholder";
    div.textContent = "上传票据开始识别";
    const btn = document.createElement("button");
    btn.className = "btn btn-primary";
    btn.textContent = "上传文件";
    btn.addEventListener("click", () => $("#fileInput").click());
    div.append(btn);
    body.append(div);
  }
}

function buildLinks() {
  for (const k of Object.keys(LINKS)) delete LINKS[k];
  for (const k of Object.keys(KV_LINE)) delete KV_LINE[k];
  for (const page of state.result) {
    for (const kv of page.kv) {
      (LINKS[kv.id] ??= []).push(kv.line);
      (LINKS[kv.line] ??= []).push(kv.id);
      KV_LINE[kv.id] = kv.line;
    }
  }
}

function buildIdIndex() {
  idIndex = {};
  state.result.forEach((page, pi) => {
    for (const l of page.text_lines) idIndex[l.id] = { text: l.text, page: pi };
    for (const t of page.tables)
      for (const r of t.rows) for (const c of r) idIndex[c.id] = { text: c.text, page: pi };
    for (const k of page.kv) idIndex[k.id] = { text: `${k.key}：${k.value}`, page: pi };
  });
}

/* ---------- 左栏：原文预览 + overlay ---------- */

function renderPreview() {
  const body = $("#previewBody");
  body.innerHTML = "";
  state.result.forEach((page, i) => {
    const label = document.createElement("div");
    label.className = "page-label";
    label.textContent = `第 ${i + 1} 页`;
    const wrap = document.createElement("div");
    wrap.className = "page-wrap";
    const img = document.createElement("img");
    img.src = `/api/tasks/${state.currentId}/pages/${i}`;
    img.alt = `第 ${i + 1} 页原文`;
    const overlay = document.createElement("div");
    overlay.className = "overlay";
    wrap.append(img, overlay);
    body.append(label, wrap);
    if (img.complete) {
      buildOverlays(page, overlay, img.clientWidth);
    } else {
      img.addEventListener("load", () => buildOverlays(page, overlay, img.clientWidth));
    }
  });
}

function buildOverlays(page, overlay, displayWidth) {
  overlay.innerHTML = "";
  if (!displayWidth) return;
  const scale = displayWidth / page.size[0];
  // z 序：text_lines → table cells → kv（kv 与来源文本行 box 重合，放最上层）
  const items = [];
  for (const l of page.text_lines) items.push(l);
  for (const t of page.tables) for (const r of t.rows) for (const c of r) items.push(c);
  for (const k of page.kv) items.push(k);
  for (const el of items) {
    const [x1, y1, x2, y2] = el.box;
    const div = document.createElement("div");
    div.className = "hl";
    div.dataset.hl = el.id;
    div.style.left = `${x1 * scale}px`;
    div.style.top = `${y1 * scale}px`;
    div.style.width = `${(x2 - x1) * scale}px`;
    div.style.height = `${(y2 - y1) * scale}px`;
    div.title = el.text || `${el.key}：${el.value}`;
    div.addEventListener("mouseenter", () => setActive(el.id, true));
    div.addEventListener("mouseleave", () => setActive(el.id, false));
    div.addEventListener("click", () => {
      const input = document.querySelector(`#formBody [data-hl="${CSS.escape(el.id)}"]`);
      if (input) input.focus();
    });
    overlay.append(div);
  }
}

/* ---------- 右栏：复原视图 ---------- */

const MIN_FS = 5;
const FIT_FLOOR = 4; // 极窄框物理装不下 MIN_FS 时允许继续收缩，避免文字被边框裁切
const FIT_STEP = 0.85;
const FIT_MAX_ITER = 8;

// 竖排判定：box 高大于宽且 ≥2 码点（单字横竖渲染无差别，避免误伤近方形 box）
function isVertical(box, text) {
  const [x1, y1, x2, y2] = box;
  return y2 - y1 > x2 - x1 && [...(text || "")].length >= 2;
}

// 字宽估算（em）：CJK/全角≈1.0，大写≈0.72，小写/数字≈0.56，标点空格≈0.4
function charWidthUnits(text) {
  let u = 0;
  for (const ch of text || "") {
    const cp = ch.codePointAt(0);
    if (cp > 0x2e7f) u += 1.0;
    else if (ch >= "A" && ch <= "Z") u += 0.72;
    else if (/[a-z0-9]/.test(ch)) u += 0.56;
    else u += 0.4;
  }
  return u;
}

// 初始字号 = 高/宽双约束解析解，多数情况一次到位，fitField 只作兜底
function initialFontSize(text, box, scale, vert) {
  const [x1, y1, x2, y2] = box;
  const w = (x2 - x1) * scale;
  const h = (y2 - y1) * scale;
  if (vert) {
    const n = Math.max(1, [...(text || "")].length);
    // 列宽≤box 宽（0.8 字形余量）；整列高 n*fs ≤ box 高减 border2+padding4
    return Math.max(MIN_FS, Math.min(w * 0.8, (h - 6) / n));
  }
  const units = charWidthUnits(text);
  const byWidth = units > 0 ? (w - 6) / units : Infinity;
  // 高度项预留 5px：CJK 墨迹在 input 内偏上(墨迹顶≈中线-0.53fs)，h*0.9 时小框墨迹会顶出内容盒(h-2)
  // 被边框裁切；h-5 保证墨迹与上下边框各留约 1-2px。h*0.9 仅作大框上限
  return Math.max(MIN_FS, Math.min(h * 0.9, h - 5, byWidth));
}

// 收缩字号直到文本不溢出；OR 条件对 scroll 度量的物理/逻辑两种语义都鲁棒
function fitField(input) {
  const maxW = input.clientWidth;
  const maxH = input.clientHeight;
  if (!maxW || !maxH) return;
  let fs = parseFloat(input.style.fontSize);
  for (let i = 0; i < FIT_MAX_ITER; i++) {
    if (input.scrollWidth <= maxW + 1 && input.scrollHeight <= maxH + 1) return;
    fs *= FIT_STEP;
    if (fs <= FIT_FLOOR) {
      input.style.fontSize = `${FIT_FLOOR}px`;
      return;
    }
    input.style.fontSize = `${fs}px`;
  }
}

function fitSheetFields(sheet) {
  for (const input of sheet.querySelectorAll("input, .vert")) fitField(input);
}

function renderSheets() {
  const body = $("#formBody");
  body.innerHTML = "";
  fieldRegistry = [];
  const sheets = document.createElement("div");
  sheets.className = "sheets";
  body.append(sheets);
  const avail = sheets.clientWidth || body.clientWidth - 32;

  state.result.forEach((page, i) => {
    const fitScale = avail / page.size[0];
    const scale = fitScale * state.zoom;
    const sheet = document.createElement("div");
    sheet.className = "sheet";
    sheet.style.width = `${page.size[0] * scale}px`;
    sheet.style.height = `${page.size[1] * scale}px`;

    const ghost = document.createElement("img");
    ghost.className = "ghost";
    ghost.src = `/api/tasks/${state.currentId}/pages/${i}`;
    ghost.alt = "";
    sheet.append(ghost);

    const kvLineIds = new Set(page.kv.map((k) => k.line));

    for (const t of page.tables) {
      for (const r of t.rows) {
        for (const c of r) {
          sheet.append(makeField(c.id, c.box, scale, c.text));
        }
      }
    }
    for (const kv of page.kv) {
      const [x1, y1, x2, y2] = kv.box;
      // 方向用整体 box 判定一次：按高切分后的子 box 宽高比可能反转，不能在子 box 上重判
      const vert = isVertical(kv.box, kv.key + kv.value);
      // 按估算字宽比例切分（值为数字时比字符数比例给 key 更多空间）
      const keyU = charWidthUnits(kv.key);
      const totalU = Math.max(0.001, keyU + charWidthUnits(kv.value));
      if (vert) {
        const keyH = ((y2 - y1) * keyU) / totalU;
        sheet.append(makeField(kv.id, [x1, y1, x2, y1 + keyH], scale, kv.key, "key", { vert: true }));
        sheet.append(makeField(kv.id, [x1, y1 + keyH, x2, y2], scale, kv.value, "value", { vert: true }));
      } else {
        const w = (x2 - x1) * scale;
        const keyW = (w * keyU) / totalU;
        sheet.append(makeField(kv.id, kv.box, scale, kv.key, "key", { widthOverride: keyW, vert: false }));
        const valueX1 = x1 + ((x2 - x1) * keyU) / totalU;
        sheet.append(makeField(kv.id, [valueX1, y1, x2, y2], scale, kv.value, "value", { vert: false }));
      }
    }
    for (const l of page.text_lines) {
      if (kvLineIds.has(l.id)) continue; // kv 已按 key/value 渲染，避免重复
      sheet.append(makeField(l.id, l.box, scale, l.text));
    }
    sheets.append(sheet);
    fitSheetFields(sheet);
  });
}

function makeInputField(value) {
  const input = document.createElement("input");
  input.type = "text";
  input.value = value;
  return input;
}

// 竖排字段逐字块级堆叠：不依赖浏览器/字体的竖排度量
// （writing-mode 在 input 内不生效，且缺竖排度量的 CJK 字体在 Chromium 下会叠字）
function makeVertField(value) {
  const div = document.createElement("div");
  div.className = "vert";
  div.contentEditable = "true";
  for (const ch of [...(value || "")]) {
    const s = document.createElement("span");
    s.textContent = ch;
    div.append(s);
  }
  div.addEventListener("keydown", (e) => {
    if (e.key === "Enter") e.preventDefault();
  });
  return div;
}

const fieldValue = (el) => (el.tagName === "INPUT" ? el.value : el.textContent);

function makeField(id, box, scale, value, part, opts = {}) {
  const [x1, y1, x2, y2] = box;
  const vert = opts.vert ?? isVertical(box, value);
  const el = vert ? makeVertField(value) : makeInputField(value);
  el.dataset.hl = id;
  if (part) el.dataset.part = part;
  el.style.left = `${x1 * scale}px`;
  el.style.top = `${y1 * scale}px`;
  el.style.width = `${opts.widthOverride ?? (x2 - x1) * scale}px`;
  el.style.height = `${(y2 - y1) * scale}px`;
  el.style.fontSize = `${initialFontSize(value, box, scale, vert)}px`;
  if (!vert) el.style.lineHeight = `${(y2 - y1) * scale}px`;
  el.addEventListener("mouseenter", () => setActive(id, true));
  el.addEventListener("mouseleave", () => setActive(id, false));
  el.addEventListener("input", () => fitField(el));
  fieldRegistry.push({ id, part: part || "text", el });
  return el;
}

/* ---------- 双向联动 ---------- */

function setActive(id, on) {
  const ids = [id, ...(LINKS[id] || []), ...(EXTRACT_LINKS[id] || [])];
  for (const i of new Set(ids)) {
    for (const el of document.querySelectorAll(`[data-hl="${CSS.escape(i)}"]`)) {
      el.classList.toggle("on", on);
      if (on) {
        el.scrollIntoView({ block: "nearest", behavior: reduceMotion ? "auto" : "smooth" });
      }
    }
  }
}

/* ---------- 保存 / 下载 ---------- */

function collectCorrections() {
  const doc = structuredClone(state.result);
  const idMap = new Map();
  for (const page of doc) {
    for (const t of page.tables) for (const r of t.rows) for (const c of r) idMap.set(c.id, c);
    for (const l of page.text_lines) idMap.set(l.id, l);
    for (const k of page.kv) idMap.set(k.id, k);
  }
  for (const { id, part, el } of fieldRegistry) {
    const obj = idMap.get(id);
    if (!obj) continue;
    if (part === "key") obj.key = fieldValue(el);
    else if (part === "value") obj.value = fieldValue(el);
    else obj.text = fieldValue(el);
  }
  // kv 修改后同步来源文本行，保持 JSON 内两处一致
  for (const page of doc) {
    for (const k of page.kv) {
      const line = idMap.get(k.line);
      if (line) line.text = `${k.key}：${k.value}`;
    }
  }
  return doc;
}

async function save() {
  if (!state.currentId || !state.result) return;
  const doc = collectCorrections();
  const resp = await fetch(`/api/tasks/${state.currentId}/save`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(doc),
  });
  if (!resp.ok) {
    showToast(`保存失败：${(await resp.json().catch(() => ({}))).detail || resp.status}`);
    return;
  }
  const data = await resp.json();
  state.result = doc;
  showToast(`已保存：${data.saved}`);
}

function setZoom(z) {
  state.zoom = Math.min(2, Math.max(0.3, Math.round(z * 10) / 10));
  if (state.result) renderSheets();
}

/* ---------- 规则抽取视图 ---------- */

function switchView(view) {
  state.view = view;
  for (const btn of document.querySelectorAll("#viewTabs .tab")) {
    btn.classList.toggle("active", btn.dataset.view === view);
  }
  const restore = view === "restore";
  $("#restorePane").hidden = !restore;
  $("#extractPane").hidden = restore;
  $("#toolbar").hidden = !restore || !state.result;
  if (!restore) switchExtractTab(state.extractTab);
}

function setExtractTabEnabled(enabled) {
  const btn = document.querySelector('#viewTabs .tab[data-view="extract"]');
  btn.disabled = !enabled;
  btn.title = enabled ? "" : "请先上传并完成识别";
}

function switchExtractTab(tab) {
  state.extractTab = tab;
  for (const btn of document.querySelectorAll("#extractTabs .tab")) {
    btn.classList.toggle("active", btn.dataset.tab === tab);
  }
  $("#rulesPanel").hidden = tab !== "rules";
  $("#extractResult").hidden = tab !== "result";
  // 仅在状态变化后重建结果表，避免丢弃用户对抽取值的编辑
  if (tab === "result" && state.extract.dirty) {
    renderExtract();
    state.extract.dirty = false;
  }
  if (tab === "rules") {
    if (state.rules === null) loadRules();
    else renderRulesEditor();
  }
}

async function loadRules() {
  const resp = await fetch("/api/rule-template");
  state.rules = resp.ok ? (await resp.json()).rules : [];
  if (!$("#rulesPanel").hidden) renderRulesEditor();
}

async function runExtract() {
  if (!state.currentId || state.extract.loading) return;
  state.extract.loading = true;
  state.extract.error = "";
  state.extract.dirty = false;
  switchExtractTab("result");
  renderExtract();
  const body = state.rules && state.rules.length ? { rules: state.rules } : {};
  try {
    const resp = await fetch(`/api/tasks/${state.currentId}/extract`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.detail || `HTTP ${resp.status}`);
    state.extract.groups = data.groups;
    state.extract.model = data.model;
    buildExtractLinks();
  } catch (e) {
    state.extract.error = e.message;
    state.extract.groups = null;
  } finally {
    state.extract.loading = false;
    state.extract.dirty = false;
    renderExtract();
  }
}

function buildExtractLinks() {
  for (const k of Object.keys(EXTRACT_LINKS)) delete EXTRACT_LINKS[k];
  if (!state.extract.groups) return;
  for (const g of state.extract.groups) {
    for (const f of g.fields) {
      const rowKey = `g${g.page}-${g.index}:${f.code}`;
      const srcs = [];
      for (const sid of f.source_ids) {
        srcs.push(sid);
        if (KV_LINE[sid]) srcs.push(KV_LINE[sid]);
      }
      EXTRACT_LINKS[rowKey] = [...new Set(srcs)];
      for (const s of srcs) (EXTRACT_LINKS[s] ??= []).push(rowKey);
    }
  }
}

function extractBanner(msg, isError) {
  const d = document.createElement("div");
  d.className = "extract-banner" + (isError ? " extract-banner-error" : "");
  d.textContent = msg;
  return d;
}

function renderExtract() {
  const box = $("#extractResult");
  box.innerHTML = "";
  const ex = state.extract;
  if (ex.loading) {
    box.append(extractBanner("正在按规则抽取，请稍候…"));
    return;
  }
  if (ex.error) {
    box.append(extractBanner(`抽取失败：${ex.error}`, true));
    return;
  }
  if (!ex.groups) {
    box.append(extractBanner("点击「按照规则抽取」开始"));
    return;
  }
  for (const g of ex.groups) {
    const card = document.createElement("div");
    card.className = "extract-card";
    const title = document.createElement("div");
    title.className = "extract-card-title";
    title.textContent = `回单组 ${g.index + 1}（第 ${g.page + 1} 页）`;
    const table = document.createElement("table");
    table.className = "extract-table";
    const thead = document.createElement("thead");
    thead.innerHTML = "<tr><th>必填</th><th>中文名</th><th>字段编码</th><th>抽取值</th><th>来源</th></tr>";
    const tbody = document.createElement("tbody");
    for (const f of g.fields) {
      const rowKey = `g${g.page}-${g.index}:${f.code}`;
      const tr = document.createElement("tr");
      tr.dataset.hl = rowKey;
      if (f.missing_required) tr.classList.add("missing");

      const tdReq = document.createElement("td");
      tdReq.textContent = f.required ? "必填" : "";
      const tdName = document.createElement("td");
      tdName.textContent = f.name;
      const tdCode = document.createElement("td");
      tdCode.className = "code";
      tdCode.textContent = f.code;
      const tdValue = document.createElement("td");
      const input = document.createElement("input");
      input.type = "text";
      input.value = f.value;
      tdValue.append(input);
      const tdSrc = document.createElement("td");
      tdSrc.className = "src";
      tdSrc.textContent = f.source_ids
        .map((sid) => idIndex[sid]?.text || sid)
        .join(" | ");
      tdSrc.title = tdSrc.textContent;

      tr.append(tdReq, tdName, tdCode, tdValue, tdSrc);
      tr.addEventListener("mouseenter", () => setActive(rowKey, true));
      tr.addEventListener("mouseleave", () => setActive(rowKey, false));
      tr.addEventListener("click", (e) => {
        if (e.target.tagName === "INPUT") return;
        const first = (EXTRACT_LINKS[rowKey] || [])[0];
        if (!first) return;
        const el = document.querySelector(
          `#previewBody .hl[data-hl="${CSS.escape(first)}"]`
        );
        if (el) {
          el.scrollIntoView({ block: "center", behavior: reduceMotion ? "auto" : "smooth" });
        }
      });
      tbody.append(tr);
    }
    table.append(thead, tbody);
    card.append(title, table);
    box.append(card);
  }
}

/* ---------- 规则模板编辑器 ---------- */

const REQUIRED_OPTIONS = ["必填", "可选", "系统保留"];

function renderRulesEditor() {
  const panel = $("#rulesPanel");
  panel.innerHTML = "";
  if (!state.rules) {
    panel.textContent = "加载中…";
    return;
  }
  const head = document.createElement("div");
  head.className = "rules-head";
  head.textContent = "抽取规则模板";
  const table = document.createElement("table");
  table.className = "rules-table";
  const thead = document.createElement("thead");
  thead.innerHTML =
    "<tr><th>字段编码</th><th>中文名</th><th>必填</th><th>类型</th><th>长度</th><th>回填策略</th><th></th></tr>";
  const tbody = document.createElement("tbody");
  state.rules.forEach((rule, i) => {
    const tr = document.createElement("tr");
    const fields = [
      ["code", rule.code, "text"],
      ["name", rule.name, "text"],
      ["required", rule.required, "select"],
      ["type", rule.type, "text"],
      ["length", rule.length ?? "", "text"],
      ["strategy", rule.strategy, "text"],
    ];
    for (const [key, val, kind] of fields) {
      const td = document.createElement("td");
      let el;
      if (kind === "select") {
        el = document.createElement("select");
        for (const opt of REQUIRED_OPTIONS) {
          const o = document.createElement("option");
          o.value = opt;
          o.textContent = opt;
          if (opt === val) o.selected = true;
          el.append(o);
        }
      } else {
        el = document.createElement("input");
        el.type = "text";
        el.value = val;
      }
      el.addEventListener("input", () => {
        state.rules[i][key] = el.value;
      });
      el.addEventListener("change", () => {
        state.rules[i][key] = el.value;
      });
      td.append(el);
      tr.append(td);
    }
    const tdDel = document.createElement("td");
    const del = document.createElement("button");
    del.className = "btn btn-ghost";
    del.textContent = "删除";
    del.addEventListener("click", () => {
      state.rules.splice(i, 1);
      renderRulesEditor();
    });
    tdDel.append(del);
    tr.append(tdDel);
    tbody.append(tr);
  });
  table.append(thead, tbody);

  const actions = document.createElement("div");
  actions.className = "rules-actions";
  const addBtn = document.createElement("button");
  addBtn.className = "btn";
  addBtn.textContent = "添加字段";
  addBtn.addEventListener("click", () => {
    state.rules.push({
      code: "",
      name: "",
      required: "可选",
      type: "字符",
      length: null,
      strategy: "",
    });
    renderRulesEditor();
  });
  const resetBtn = document.createElement("button");
  resetBtn.className = "btn";
  resetBtn.textContent = "恢复默认";
  resetBtn.addEventListener("click", async () => {
    await fetch("/api/rule-template", { method: "DELETE" });
    await loadRules();
    showToast("已恢复默认规则模板");
  });
  const saveBtn = document.createElement("button");
  saveBtn.className = "btn btn-primary";
  saveBtn.textContent = "保存规则";
  saveBtn.addEventListener("click", saveRules);
  actions.append(addBtn, resetBtn, saveBtn);

  const cfg = document.createElement("div");
  cfg.className = "rules-config";
  cfg.innerHTML =
    '<div class="rules-head">模型设置</div>' +
    '<div class="tool"><span class="tool-label">模型</span><input id="cfgModel" type="text"></div>' +
    '<div class="tool"><span class="tool-label">端点</span><input id="cfgBaseUrl" type="text"></div>' +
    '<span id="cfgKeyState" class="key-badge"></span>' +
    '<button id="cfgSaveBtn" class="btn">保存模型设置</button>';

  panel.append(head, table, actions, cfg);
  cfg.querySelector("#cfgSaveBtn").addEventListener("click", saveExtractConfig);
  loadExtractConfig();
}

async function saveRules() {
  const codes = state.rules.map((r) => (r.code || "").trim());
  if (codes.some((c) => !c)) {
    showToast("字段编码不能为空");
    return;
  }
  if (new Set(codes).size !== codes.length) {
    showToast("字段编码重复");
    return;
  }
  const rules = state.rules.map((r) => ({
    ...r,
    code: (r.code || "").trim(),
    length: r.length === "" || r.length == null ? null : r.length,
  }));
  const resp = await fetch("/api/rule-template", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ rules }),
  });
  if (!resp.ok) {
    showToast(`保存失败：${(await resp.json().catch(() => ({}))).detail || resp.status}`);
    return;
  }
  showToast("规则模板已保存");
}

async function loadExtractConfig() {
  const resp = await fetch("/api/extract-config");
  if (!resp.ok) return;
  const cfg = await resp.json();
  const model = $("#cfgModel");
  const base = $("#cfgBaseUrl");
  const badge = $("#cfgKeyState");
  if (model) model.value = cfg.model || "";
  if (base) base.value = cfg.base_url || "";
  if (badge) {
    badge.textContent = cfg.configured ? "key 已配置" : "key 未配置";
    badge.classList.toggle("key-badge-bad", !cfg.configured);
  }
}

async function saveExtractConfig() {
  const payload = {
    model: ($("#cfgModel")?.value || "").trim(),
    base_url: ($("#cfgBaseUrl")?.value || "").trim(),
  };
  const resp = await fetch("/api/extract-config", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  showToast(resp.ok ? "模型设置已保存" : "模型设置保存失败");
}

/* ---------- 抽取结果下载 ---------- */

function downloadExtracted() {
  if (!state.extract.groups) {
    showToast("暂无抽取结果");
    return;
  }
  const results = [];
  for (const g of state.extract.groups) {
    const obj = {};
    for (const f of g.fields) {
      const rowKey = `g${g.page}-${g.index}:${f.code}`;
      const input = document.querySelector(
        `#extractBody tr[data-hl="${CSS.escape(rowKey)}"] input`
      );
      obj[f.code] = input ? input.value : f.value;
    }
    results.push(obj);
  }
  const payload = {
    _meta: {
      filename: state.taskMeta.filename,
      model: state.extract.model,
      extracted_at: new Date().toISOString(),
      groups: results.length,
    },
    results,
  };
  const blob = new Blob([JSON.stringify(payload, null, 2)], {
    type: "application/json",
  });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `${state.taskMeta.output_name || "result"}_extracted.json`;
  a.click();
  URL.revokeObjectURL(a.href);
}

document.addEventListener("DOMContentLoaded", init);
