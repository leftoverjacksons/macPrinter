"use strict";
// macPrinter dashboard. State arrives via server-sent events (/api/events).

let S = null;              // latest state from the server
let tab = "session";
let preview = null;        // {token, placements, overflow, sig} while the preview is open
let history = [];

const $ = (id) => document.getElementById(id);
// Replace markup only when it changed. State updates arrive several times a second while checks
// run; rebuilding unchanged buttons would swallow clicks/taps that land mid-rebuild.
function setHTML(el, html) { if (el._html !== html) { el.innerHTML = html; el._html = html; } }
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtTime = (t) => new Date(t * 1000).toLocaleString([], { dateStyle: "short", timeStyle: "short" });

function toast(msg, err = false) {
  const t = $("toast");
  t.textContent = msg; t.className = err ? "err" : ""; t.hidden = false;
  clearTimeout(toast.h); toast.h = setTimeout(() => (t.hidden = true), err ? 6000 : 3500);
}

async function api(method, url, body) {
  const r = await fetch(url, { method, headers: body ? { "Content-Type": "application/json" } : {}, body: body ? JSON.stringify(body) : undefined });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) { toast(data.error || data.detail || `${r.status} ${r.statusText}`, true); throw new Error(data.error); }
  return data;
}

// ---------------------------------------------------------------- tabs
document.querySelectorAll("#tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
function showTab(name) {
  tab = name;
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab").forEach((s) => (s.hidden = s.id !== `tab-${name}`));
  if (name === "history") loadHistory();
  if (name === "settings") fillSettings();
  if (name === "sheet") loadTemplates();
  render();
}

// ---------------------------------------------------------------- render
function render() {
  if (!S) return;
  renderChips();
  $("sim-banner").hidden = S.detector !== "sim";
  $("detector-error").hidden = !S.detector_error;
  $("detector-error").textContent = `Detector error: ${S.detector_error}`;
  if (tab === "session") renderSession();
  if (tab === "sheet") renderSheet();
}

function renderChips() {
  const sh = S.sheet;
  const sess = S.session;
  setHTML($("chips"), [
    `<span class="chip ${S.detector === "sim" ? "sim" : ""}">Detector: ${esc(S.detector)}</span>`,
    `<span class="chip">Sheet #${sh.id} · ${esc(sh.template)} · ${sh.free}/${sh.capacity} free</span>`,
    sess ? `<span class="chip on">Session #${sess.id} · ${S.queue.length} queued</span>` : `<span class="chip">No session</span>`,
  ].join(""));
}

function renderSession() {
  const inPreview = preview !== null;
  $("session-main").hidden = inPreview;
  $("preview").hidden = !inPreview;
  $("sim-panel").hidden = S.detector !== "sim";

  const sess = S.session;
  const n = S.queue.length;
  setHTML($("session-bar"), inPreview
    ? `<b>Session #${sess ? sess.id : ""}</b><span class="muted">Review the sheet before recording it.</span>`
    : sess
    ? `<b>Session #${sess.id}</b><span class="muted">started ${fmtTime(sess.started_at)}</span><span class="spacer"></span>
       <button class="btn danger" onclick="cancelSession()">Cancel session</button>
       <button class="btn primary big" onclick="openPreview()" ${n ? "" : "disabled"}>Finish &amp; preview (${n})</button>`
    : `<span class="muted">Start a session, then plug in dongles one after another. Each one is checked and queued automatically.</span>
       <span class="spacer"></span><button class="btn primary big" onclick="startSession()">Start session</button>`);
  if (inPreview) { renderPreviewState(); return; }

  const dets = S.detections;
  $("det-count").textContent = dets.length ? `(${dets.filter((d) => d.present).length} plugged in)` : "";
  setHTML($("detections"), dets.length ? dets.map(detCard).join("")
    : `<div class="card empty">Plug in a dongle${S.detector === "sim" ? " (or use the simulator above)" : ""}.</div>`);

  $("queue-count").textContent = `(${n})`;
  setHTML($("queue"), !sess ? `<div class="empty">No active session.</div>`
    : n ? S.queue.map((q, i) => `<div class="row"><span class="muted">${i + 1}.</span><span class="mac">${esc(q.mac)}</span>
        ${q.reprint ? `<span class="badge b-reprint">reprint</span>` : ""}
        ${q.override && !q.reprint ? `<span class="badge b-override" title="${esc(q.override)}">override</span>` : ""}
        <span class="spacer"></span><button class="btn small" onclick="removeQueued('${esc(q.mac)}')">Remove</button></div>`).join("")
    : `<div class="empty">Nothing queued yet.</div>`);
  const free = S.sheet.free;
  $("queue-foot").textContent = sess
    ? (n > free ? `Sheet has ${free} free labels — ${n - free} will wait for a new sheet.` : `Sheet has ${free} free labels.`)
    : "";
}

const ICON = { pass: "✓", warn: "!", fail: "✕", running: "…", pending: "·", skip: "–" };
const CHECK_NAMES = { mac: "MAC", link: "Link", internet: "Internet" };

function detCard(d) {
  const i = d.info;
  const statusLabel = d.present ? d.status : "unplugged";
  const checks = Object.entries(d.checks).map(([k, c]) =>
    `<span class="ic ${c.status}">${ICON[c.status] || ""}</span><span>${CHECK_NAMES[k] || k}</span><span>${esc(c.status === "skip" ? (c.detail || "check disabled") : c.detail)}</span>`).join("");
  const acts = [];
  if (S.session && d.status === "pass") acts.push(`<button class="btn primary small" onclick="detAct('${d.key}','queue')">Add to queue</button>`);
  if (S.session && d.status === "fail" && d.can_override) acts.push(`<button class="btn small" onclick="detAct('${d.key}','queue')">${d.reprint ? "Queue as reprint" : "Queue anyway"}</button>`);
  if (d.present && ["fail", "pass"].includes(d.status)) acts.push(`<button class="btn small" onclick="detAct('${d.key}','retry')">Retry checks</button>`);
  acts.push(`<button class="btn small" onclick="detAct('${d.key}','dismiss')">Dismiss</button>`);
  let sim = "";
  if (S.sim && S.sim[d.key]) {
    const s = S.sim[d.key];
    sim = `<div class="simctl">Simulator:
      <button class="btn small" onclick="simSet('${d.key}',{cable:${!s.cable}})">${s.cable ? "Pull cable" : "Plug cable"}</button>
      <button class="btn small" onclick="simSet('${d.key}',{internet:${!s.internet}})">${s.internet ? "Break internet" : "Fix internet"}</button>
      <button class="btn small" onclick="simUnplug('${d.key}')">Unplug USB</button></div>`;
  }
  return `<div class="card det s-${d.status} ${d.present ? "" : "gone"}">
    <div class="det-head"><span class="mac">${esc(i.mac)}</span>
      <span class="badge ${d.present ? "b-" + d.status : "b-gone"}">${esc(statusLabel)}</span></div>
    <div class="meta">${esc(i.iface)} · ${esc(i.vid_pid)} · ${esc(i.driver)}${i.serial ? " · s/n " + esc(i.serial) : ""}</div>
    <div class="checks">${checks}</div>
    ${d.message ? `<div class="det-msg">${esc(d.message)}</div>` : ""}
    <div class="det-actions">${acts.join("")}</div>${sim}</div>`;
}

// ---------------------------------------------------------------- actions
async function startSession() { await api("POST", "/api/session/start"); }
async function cancelSession() {
  if (confirm("Cancel this session? Queued dongles are dropped (nothing is printed).")) await api("POST", "/api/session/cancel");
}
async function removeQueued(mac) { await api("DELETE", `/api/queue/${encodeURIComponent(mac)}`); }
async function detAct(key, action) { await api("POST", `/api/detections/${key}/${action}`); }
async function simSet(key, v) { await api("POST", `/api/sim/${key}/set`, v); }
async function simUnplug(key) { await api("POST", `/api/sim/${key}/unplug`); }
$("sim-plug").addEventListener("click", async () => {
  const req = {
    mac: $("sim-mac").value.trim(), permanent: $("sim-perm").checked,
    cable: $("sim-cable").checked, internet: $("sim-net").checked,
  };
  $("sim-mac").value = "";
  await api("POST", "/api/sim/plug", req);
});

// ---------------------------------------------------------------- preview
const queueSig = () => S ? JSON.stringify([S.sheet.id, S.sheet.cells.length, S.queue.map((q) => q.mac)]) : "";

async function openPreview() {
  const p = await api("GET", "/api/preview");
  preview = { ...p, sig: queueSig() };
  loadPdf();
  render();
}
function loadPdf() {
  const url = `/api/preview.pdf?outlines=${$("pv-outlines").checked ? 1 : 0}&t=${Date.now()}`;
  $("pv-pdf").src = url;
  $("pv-download").href = url;
}
$("pv-outlines").addEventListener("change", loadPdf);
$("pv-back").addEventListener("click", () => { preview = null; render(); });
$("pv-refresh").addEventListener("click", openPreview);
$("pv-commit").addEventListener("click", async () => {
  const r = await api("POST", "/api/commit", { token: preview.token });
  toast(`Job #${r.job_id}: ${r.placed} label(s) recorded.` + (r.remaining ? ` ${r.remaining} still queued — load a new sheet.` : " Session complete."));
  preview = null;
  render();
});

function renderPreviewState() {
  $("pv-stale").hidden = preview.sig === queueSig();
  $("pv-overflow").hidden = !preview.overflow;
  $("pv-overflow").textContent = `${preview.overflow} queued dongle(s) don't fit on this sheet. They stay queued; load a new sheet after marking this one printed.`;
  $("pv-commit").disabled = !preview.placements.length;
  const newCells = new Map(preview.placements.map((p) => [`${p.row},${p.col}`, p.mac]));
  sizeGrid($("pv-grid"), S.sheet.cols, 64);
  setHTML($("pv-grid"), gridHtml(S.sheet, newCells, false));
}

function gridHtml(sheet, newCells, clickable) {
  const cells = new Map(sheet.cells.map((c) => [`${c.row},${c.col}`, c]));
  let h = "";
  for (let r = 0; r < sheet.rows; r++) for (let c = 0; c < sheet.cols; c++) {
    const k = `${r},${c}`, cell = cells.get(k), nw = newCells.get(k);
    const cls = nw ? "new" : cell ? cell.state : "free";
    const text = nw || (cell && cell.mac) || "";
    const title = `row ${r + 1}, col ${c + 1}${text ? " — " + text : ""}${cell ? " (" + cell.state + ")" : ""}`;
    h += `<div class="cell ${cls}" title="${esc(title)}" ${clickable ? `onclick="cycleCell(${r},${c})"` : ""}>${esc(clickable ? text : "")}</div>`;
  }
  return h;
}

// ---------------------------------------------------------------- sheet tab
function renderSheet() {
  const sh = S.sheet;
  const used = sh.cells.filter((c) => c.state === "used").length, voids = sh.cells.length - used;
  setHTML($("sheet-summary"), `<b>Sheet #${sh.id}</b> · ${esc(sh.template)} · loaded ${fmtTime(sh.created_at)} ·
    <b>${sh.free}</b> free · ${used} used · ${voids} void`);
  const g = $("sheet-grid");
  sizeGrid(g, sh.cols, 150);
  setHTML(g, gridHtml(sh, new Map(), true));
}

// Columns shrink to fit narrow screens but never grow past cellPx; width tracks the column count.
function sizeGrid(el, cols, cellPx) {
  el.style.gridTemplateColumns = `repeat(${cols}, minmax(0, ${cellPx}px))`;
  el.style.maxWidth = `${cols * (cellPx + 2) + 12}px`;
}

async function cycleCell(r, c) {
  const cell = S.sheet.cells.find((x) => x.row === r && x.col === c);
  const next = !cell ? "used" : cell.state === "used" ? "void" : "free";
  if (cell && cell.mac && !confirm(`Row ${r + 1}, col ${c + 1} holds the printed label ${cell.mac}. Change it to ${next}?`)) return;
  await api("POST", "/api/sheet/cell", { row: r, col: c, state: next });
}
$("new-sheet").addEventListener("click", async () => {
  const tpl = $("tpl-select").value;
  if (!confirm(`Load a new, empty ${tpl} sheet? The current sheet is retired and its remaining labels are no longer tracked.`)) return;
  await api("POST", "/api/sheet/new", { template: tpl });
  toast(`New ${tpl} sheet loaded.`);
});
async function loadTemplates() {
  const list = await api("GET", "/api/templates");
  const cur = S ? S.settings.template : "";
  $("tpl-select").innerHTML = list.map((t) => `<option ${t === cur ? "selected" : ""}>${esc(t)}</option>`).join("");
}

// ---------------------------------------------------------------- history
async function loadHistory() { history = await api("GET", "/api/history"); renderHistory(); }
function renderHistory() {
  const f = $("hist-filter").value.trim().toUpperCase().replace(/[:.]/g, "-");
  const rows = history.filter((h) => !f || h.mac.includes(f));
  setHTML($("hist-body"), rows.length ? rows.map((h) => `<tr>
    <td>${fmtTime(h.created_at)}</td><td class="mac">${esc(h.mac)}</td><td>#${h.sheet_id}</td>
    <td>${h.row + 1}</td><td>${h.col + 1}</td><td><a href="/api/jobs/${h.job_id}.pdf" target="_blank">#${h.job_id}</a></td>
    <td>${h.reprint ? `<span class="badge b-reprint">reprint</span>` : ""}</td>
    <td><button class="btn small" onclick="reprint('${esc(h.mac)}')">Reprint</button></td></tr>`).join("")
    : `<tr><td colspan="8" class="muted">No labels yet.</td></tr>`);
}
$("hist-filter").addEventListener("input", renderHistory);
async function reprint(mac) { await api("POST", `/api/history/${encodeURIComponent(mac)}/reprint`); toast(`${mac} added to the queue as a reprint.`); }
$("hist-csv").addEventListener("click", () => {
  const lines = [["timestamp", "mac", "sheet_id", "row", "col", "job_id", "reprint"].join(",")].concat(
    history.map((h) => [new Date(h.created_at * 1000).toISOString(), h.mac, h.sheet_id, h.row + 1, h.col + 1, h.job_id, h.reprint].join(",")));
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([lines.join("\n") + "\n"], { type: "text/csv" }));
  a.download = "macprinter-labels.csv"; a.click();
});

// ---------------------------------------------------------------- settings
function fillSettings() {
  if (!S) return;
  const f = $("settings-form");
  for (const [k, v] of Object.entries(S.settings)) {
    const el = f.elements[k];
    if (!el) continue;
    if (el.type === "checkbox") el.checked = !!v; else el.value = v;
  }
}
$("settings-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target, out = {};
  for (const el of f.elements) {
    if (!el.name) continue;
    out[el.name] = el.type === "checkbox" ? el.checked : el.type === "number" ? Number(el.value) : el.value;
  }
  await api("PUT", "/api/settings", out);
  toast("Settings saved. New checks use them from the next plug-in.");
});

// ---------------------------------------------------------------- live updates
function connect() {
  const es = new EventSource("/api/events");
  es.onmessage = (e) => { S = JSON.parse(e.data); render(); };
  es.onerror = () => { es.close(); setTimeout(connect, 2000); };
}
connect();
