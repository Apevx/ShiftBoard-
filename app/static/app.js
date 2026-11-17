"use strict";
/* Shiftboard front end — vanilla JS, no build step. */

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const SHARE = (location.pathname.match(/^\/s\/([^/]+)/) || [])[1] || null;
if (SHARE) document.body.classList.add("share");

const MONTHS = ["January","February","March","April","May","June","July","August","September","October","November","December"];
const DOW = ["Sun","Mon","Tue","Wed","Thu","Fri","Sat"];
const SWATCHES = ["#3b82f6","#1e3a8a","#8b5cf6","#ec4899","#ef4444","#f97316","#f59e0b","#eab308","#16a34a","#14b8a6","#0ea5e9","#64748b"];

const ls = {
  get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} },
};

let cur = new Date(); cur = new Date(cur.getFullYear(), cur.getMonth(), 1);
let S = { shifts: [], byId: {}, assign: {}, oncall: [], notes: {}, title: "" };
let mode = { type: "view" };       // view | shift(id) | erase | oncall(first)
let stack = false;                 // allow two shifts on one day
let weekStartSun = ls.get("weekStartSun", false);
let pattern = [];
let openDay = null;

/* ---------- utils ---------- */
const pad = n => String(n).padStart(2, "0");
const ymd = d => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const parseYmd = s => { const [y, m, d] = s.split("-").map(Number); return new Date(y, m - 1, d); };
const addDays = (d, n) => { const x = new Date(d); x.setDate(x.getDate() + n); return x; };
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const todayStr = () => ymd(new Date());
function fgFor(hex) {
  const n = parseInt(hex.slice(1), 16), r = n >> 16, g = (n >> 8) & 255, b = n & 255;
  const L = x => { x /= 255; return x <= .03928 ? x / 12.92 : ((x + .055) / 1.055) ** 2.4; };
  return (0.2126 * L(r) + 0.7152 * L(g) + 0.0722 * L(b)) > 0.45 ? "#0f172a" : "#ffffff";
}
const cvars = hex => `--c:${hex};--fg:${fgFor(hex)}`;
const fmtDay = s => { const d = parseYmd(s); return `${DOW[d.getDay()]} ${d.getDate()} ${MONTHS[d.getMonth()].slice(0, 3)}`; };
const fmtHours = h => (Math.round(h * 10) / 10).toString();
function fmtDT(s) { const [d, t] = s.split("T"); return `${fmtDay(d)} ${t}`; }
function shiftTimes(s) { return s.all_day ? "All day" : `${s.start_time}–${s.end_time} · ${fmtHours(s.hours)}h`; }

function toast(msg) {
  const t = $("#toast"); t.textContent = msg; t.classList.add("show");
  clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.remove("show"), 2200);
}

async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: { "Content-Type": "application/json" } };
  if (opts.body !== undefined) init.body = JSON.stringify(opts.body);
  const r = await fetch(path, init);
  if (r.status === 401 && !SHARE && path !== "/api/login") { showLogin(); throw new Error("auth"); }
  if (!r.ok) {
    let msg = r.statusText;
    try { const j = await r.json(); msg = Array.isArray(j.detail) ? j.detail.map(e => e.msg).join(", ") : (j.detail || msg); } catch {}
    toast(msg); throw new Error(msg);
  }
  return r.json();
}

/* ---------- data ---------- */
function gridRange() {
  const first = new Date(cur.getFullYear(), cur.getMonth(), 1);
  const offset = (first.getDay() - (weekStartSun ? 0 : 1) + 7) % 7;
  const start = addDays(first, -offset);
  return { start, end: addDays(start, 41) };
}

async function load() {
  const { start, end } = gridRange();
  const q = `?start=${ymd(start)}&end=${ymd(end)}`;
  const data = await api(SHARE ? `/api/public/${SHARE}/state${q}` : `/api/state${q}`);
  S = { ...S, ...data, notes: data.notes || {} };
  S.byId = Object.fromEntries(S.shifts.map(s => [s.id, s]));
  S.assign = {};
  for (const a of data.assignments) (S.assign[a.day] ||= []).push(a.shift_id);
  render();
}

const sortIds = ids => [...ids].sort((a, b) => (S.byId[a]?.sort ?? 0) - (S.byId[b]?.sort ?? 0));
const activeShifts = () => S.shifts.filter(s => !s.archived);
function oncallOn(day) {
  const ds = `${day}T00:00`, de = `${ymd(addDays(parseYmd(day), 1))}T00:00`;
  return S.oncall.filter(o => o.start < de && o.end > ds).map(o => ({ o, s: o.start > ds, e: o.end < de }));
}
const overlapHours = (aS, aE, bS, bE) => Math.max(0, (Math.min(aE, bE) - Math.max(aS, bS)) / 36e5);

/* ---------- render ---------- */
function render() {
  document.title = S.title || "Shiftboard";
  $("#title").textContent = S.title || "Shiftboard";
  $("#monthLabel").textContent = `${MONTHS[cur.getMonth()]} ${cur.getFullYear()}`;
  const order = weekStartSun ? [0,1,2,3,4,5,6] : [1,2,3,4,5,6,0];
  $("#weekdays").innerHTML = order.map(i => `<div>${DOW[i]}</div>`).join("");

  const { start } = gridRange();
  const today = todayStr(), month = cur.getMonth();
  const first = mode.type === "oncall" ? mode.first : null;
  let html = "";
  for (let i = 0; i < 42; i++) {
    const d = addDays(start, i), day = ymd(d);
    const ids = sortIds(S.assign[day] || []);
    const cls = ["cell"];
    if (d.getMonth() !== month) cls.push("other");
    if (day === today) cls.push("today");
    if (first === day) cls.push("sel");
    const chips = ids.map(id => S.byId[id]).filter(Boolean)
      .map(s => `<span class="chip" style="${cvars(s.color)}">${esc(s.code)}</span>`).join("");
    const bars = oncallOn(day).slice(0, 3)
      .map(({ o, s, e }) => `<div class="ocbar${s ? " s" : ""}${e ? " e" : ""}" style="--c:${o.color}" title="${esc(o.label)}"></div>`).join("");
    html += `<div class="${cls.join(" ")}" data-day="${day}">
      <div class="num">${d.getDate()}</div>
      <div class="chips${ids.length > 1 ? " two" : ""}">${chips}</div>
      <div class="ocbars">${bars}</div>
      ${S.notes[day] ? '<span class="notedot"></span>' : ""}
    </div>`;
  }
  $("#grid").innerHTML = html;
  renderSummary();
  if (!SHARE) renderPalette();
}

function renderSummary() {
  const y = cur.getFullYear(), m = cur.getMonth();
  const days = new Date(y, m + 1, 0).getDate();
  let hours = 0, worked = 0; const counts = {};
  for (let d = 1; d <= days; d++) {
    for (const id of S.assign[ymd(new Date(y, m, d))] || []) {
      const s = S.byId[id]; if (!s) continue;
      counts[id] = (counts[id] || 0) + 1;
      if (!s.all_day) { hours += s.hours; worked++; }
    }
  }
  const mS = new Date(y, m, 1), mE = new Date(y, m + 1, 1);
  const ocH = S.oncall.reduce((t, o) => t + overlapHours(new Date(o.start), new Date(o.end), mS, mE), 0);
  const minis = sortIds(Object.keys(counts).map(Number)).map(id => {
    const s = S.byId[id]; return `<span class="mini" style="background:${s.color};color:${fgFor(s.color)}">${esc(s.code)}×${counts[id]}</span>`;
  }).join("");
  const parts = [];
  if (worked) parts.push(`<b>${worked}</b> shifts · <b>${fmtHours(hours)}</b>h`);
  if (ocH) parts.push(`<b>${fmtHours(ocH)}</b>h on call`);
  $("#summary").innerHTML = parts.length || minis ? `${parts.join(" · ")}<div>${minis}</div>` : "No shifts this month";
}

function renderPalette() {
  const tools = [`<button class="tool${mode.type === "view" ? " on" : ""}" data-tool="view">👆 View</button>`];
  for (const s of activeShifts()) {
    const on = mode.type === "shift" && mode.id === s.id;
    tools.push(`<button class="tool${on ? " on" : ""}" data-tool="shift" data-id="${s.id}"><span class="sw" style="${cvars(s.color)}">${esc(s.code)}</span>${esc(s.name)}</button>`);
  }
  tools.push(`<button class="tool${mode.type === "erase" ? " on" : ""}" data-tool="erase">⌫ Erase</button>`);
  tools.push(`<button class="tool${mode.type === "oncall" ? " on" : ""}" data-tool="oncall">📟 On call</button>`);
  tools.push(`<button class="tool toggle${stack ? " on" : ""}" data-tool="stack" title="Allow two shifts on one day">2× Stack</button>`);
  $("#palette").innerHTML = tools.join("");
  const hints = {
    view: "Tap a day to see details",
    shift: `Tap days to ${stack ? "add" : "set"} ${S.byId[mode.id]?.name || ""} · tap again to remove`,
    erase: "Tap days to clear their shifts",
    oncall: mode.first ? "Now tap the last day of the on-call period" : "Tap the first day of the on-call period",
  };
  $("#hint").textContent = hints[mode.type];
}

/* ---------- interactions ---------- */
$("#palette").addEventListener("click", e => {
  const b = e.target.closest("button[data-tool]"); if (!b) return;
  const t = b.dataset.tool;
  if (t === "stack") stack = !stack;
  else if (t === "shift") mode = (mode.type === "shift" && mode.id === +b.dataset.id) ? { type: "view" } : { type: "shift", id: +b.dataset.id };
  else mode = (mode.type === t && t !== "view") ? { type: "view" } : { type: t, first: null };
  render();
});

$("#grid").addEventListener("click", e => {
  const cell = e.target.closest(".cell"); if (!cell) return;
  onDayTap(cell.dataset.day);
});

async function setDay(day, shiftId, action) {
  const before = [...(S.assign[day] || [])];
  let next = before;
  if (action === "clear") next = [];
  else if (action === "replace") next = [shiftId];
  else if (action === "add") next = before.includes(shiftId) ? before : [...before, shiftId];
  else if (action === "remove") next = before.filter(x => x !== shiftId);
  S.assign[day] = next; render();                  // optimistic
  try {
    const r = await api("/api/assign", { method: "POST", body: { day, shift_id: shiftId, action } });
    S.assign[day] = r.shift_ids;
  } catch { S.assign[day] = before; }
  render();
  if (openDay === day) renderDay();
}

function onDayTap(day) {
  if (SHARE || mode.type === "view") return showDay(day);
  if (mode.type === "shift") {
    const has = (S.assign[day] || []).includes(mode.id);
    return setDay(day, mode.id, has ? "remove" : (stack ? "add" : "replace"));
  }
  if (mode.type === "erase") return (S.assign[day] || []).length && setDay(day, null, "clear");
  if (mode.type === "oncall") {
    if (!mode.first) { mode.first = day; return render(); }
    const [a, b] = [mode.first, day].sort();
    mode.first = null; render();
    openOncall(null, a, b);
  }
}

/* month navigation */
const shiftMonth = n => { cur = new Date(cur.getFullYear(), cur.getMonth() + n, 1); load(); };
$("#prevBtn").onclick = () => shiftMonth(-1);
$("#nextBtn").onclick = () => shiftMonth(1);
$("#todayBtn").onclick = () => { const t = new Date(); cur = new Date(t.getFullYear(), t.getMonth(), 1); load(); };
document.addEventListener("keydown", e => {
  if (document.querySelector("dialog[open]") || /INPUT|TEXTAREA/.test(e.target.tagName)) return;
  if (e.key === "ArrowLeft") shiftMonth(-1);
  if (e.key === "ArrowRight") shiftMonth(1);
  if (e.key === "Escape") { mode = { type: "view" }; render(); }
});
let tx = null, ty = null;
$("main").addEventListener("touchstart", e => { tx = e.touches[0].clientX; ty = e.touches[0].clientY; }, { passive: true });
$("main").addEventListener("touchend", e => {
  if (tx === null) return;
  const dx = e.changedTouches[0].clientX - tx, dy = e.changedTouches[0].clientY - ty;
  if (Math.abs(dx) > 70 && Math.abs(dx) > Math.abs(dy) * 1.5) shiftMonth(dx < 0 ? 1 : -1);
  tx = null;
});

/* ---------- day sheet ---------- */
function showDay(day) { openDay = day; renderDay(); $("#dayDlg").showModal(); }
function renderDay() {
  const day = openDay;
  $("#dayTitle").textContent = fmtDay(day);
  const ids = sortIds(S.assign[day] || []);
  $("#dayShifts").innerHTML = ids.length ? ids.map(id => {
    const s = S.byId[id]; if (!s) return "";
    return `<div class="item"><span class="sw" style="${cvars(s.color)}">${esc(s.code)}</span>
      <div class="grow"><div class="t">${esc(s.name)}</div><div class="s">${shiftTimes(s)}</div></div>
      ${SHARE ? "" : `<button data-rm="${id}" aria-label="Remove">✕</button>`}</div>`;
  }).join("") : `<div class="muted">${SHARE ? "Not working" : "No shift"}</div>`;

  $("#dayPicker").innerHTML = activeShifts().map(s =>
    `<button class="${ids.includes(s.id) ? "on" : ""}" style="${cvars(s.color)}" data-id="${s.id}" title="${esc(s.name)}">${esc(s.code)} · ${esc(s.name)}</button>`
  ).join("");

  const ocs = oncallOn(day).map(x => x.o);
  $("#dayOncall").innerHTML = ocs.length ? ocs.map(o => `
    <div class="item" ${SHARE ? "" : `data-oc="${o.id}" style="cursor:pointer"`}>
      <span class="oc-sw" style="--c:${o.color}"></span>
      <div class="grow"><div class="t">${esc(o.label)}</div><div class="s">${fmtDT(o.start)} → ${fmtDT(o.end)}</div>
      ${o.note ? `<div class="s">${esc(o.note)}</div>` : ""}</div>
      ${SHARE ? "" : "<span class=\"muted\">›</span>"}
    </div>`).join("") : '<div class="muted">None</div>';
  $("#dayNote").value = S.notes[day] || "";
}
$("#dayShifts").addEventListener("click", e => {
  const b = e.target.closest("[data-rm]"); if (b) setDay(openDay, +b.dataset.rm, "remove");
});
$("#dayPicker").addEventListener("click", e => {
  const b = e.target.closest("button[data-id]"); if (!b) return;
  const id = +b.dataset.id, has = (S.assign[openDay] || []).includes(id);
  setDay(openDay, id, has ? "remove" : (stack ? "add" : "replace"));
});
$("#dayOncall").addEventListener("click", e => {
  const it = e.target.closest("[data-oc]"); if (!it) return;
  const o = S.oncall.find(x => x.id === +it.dataset.oc);
  $("#dayDlg").close(); openOncall(o);
});
$("#dayAddOncall").onclick = () => { const d = openDay; $("#dayDlg").close(); openOncall(null, d, d); };
$("#dayNote").addEventListener("change", async e => {
  const text = e.target.value, day = openDay;
  await api(`/api/notes/${day}`, { method: "PUT", body: { text } });
  if (text.trim()) S.notes[day] = text; else delete S.notes[day];
  render();
});
$("#dayDlg").addEventListener("close", () => {
  const ta = $("#dayNote");
  if (openDay && ta.value !== (S.notes[openDay] || "")) ta.dispatchEvent(new Event("change"));
  openDay = null;
});

/* ---------- on-call editor ---------- */
let editingOc = null;
function openOncall(o, firstDay, lastDay) {
  editingOc = o;
  const f = $("#ocForm"), last = ls.get("ocDefaults", { label: "On call", st: "17:00", et: "08:00", color: "#f59e0b" });
  $("#ocTitle").textContent = o ? "Edit on call" : "New on call";
  $("#ocDelete").hidden = !o;
  if (o) {
    [f.sd.value, f.st.value] = o.start.split("T");
    [f.ed.value, f.et.value] = o.end.split("T");
    f.label.value = o.label; f.color.value = o.color; f.note.value = o.note || "";
  } else {
    f.label.value = last.label; f.color.value = last.color; f.note.value = "";
    f.sd.value = firstDay; f.st.value = last.st; f.et.value = last.et;
    // overnight default: if end time is earlier than start, finish the morning after the last day
    f.ed.value = last.et <= last.st ? ymd(addDays(parseYmd(lastDay), 1)) : lastDay;
  }
  $("#ocDlg").showModal();
}
$("#ocForm").addEventListener("submit", async e => {
  e.preventDefault();
  const f = e.target;
  const body = { label: f.label.value.trim() || "On call", start: `${f.sd.value}T${f.st.value}`, end: `${f.ed.value}T${f.et.value}`,
    color: f.color.value, note: f.note.value.trim() || null };
  if (body.end <= body.start) return toast("End must be after start");
  await api(editingOc ? `/api/oncall/${editingOc.id}` : "/api/oncall", { method: editingOc ? "PUT" : "POST", body });
  ls.set("ocDefaults", { label: body.label, st: f.st.value, et: f.et.value, color: body.color });
  $("#ocDlg").close(); toast("On call saved"); load();
});
$("#ocDelete").onclick = async () => {
  if (!editingOc || !confirm("Delete this on-call period?")) return;
  await api(`/api/oncall/${editingOc.id}`, { method: "DELETE" });
  $("#ocDlg").close(); load();
};

/* ---------- settings ---------- */
$("#settingsBtn").onclick = () => { renderSettings(); $("#setDlg").showModal(); };
$("#tabs").addEventListener("click", e => {
  const b = e.target.closest("button[data-tab]"); if (!b) return;
  $$("#tabs button").forEach(x => x.classList.toggle("on", x === b));
  $$("#setDlg [data-pane]").forEach(p => p.hidden = p.dataset.pane !== b.dataset.tab);
});

function renderSettings() {
  const list = activeShifts();
  $("#shiftList").innerHTML = list.map((s, i) => `
    <div class="item"><span class="sw" style="${cvars(s.color)}">${esc(s.code)}</span>
      <div class="grow"><div class="t">${esc(s.name)}</div><div class="s">${shiftTimes(s)}</div></div>
      <button data-up="${i}" ${i ? "" : "disabled"} aria-label="Move up">▲</button>
      <button data-down="${i}" ${i < list.length - 1 ? "" : "disabled"} aria-label="Move down">▼</button>
      <button data-edit="${s.id}" aria-label="Edit">✎</button></div>`).join("") || '<p class="muted">No shifts yet.</p>';

  $("#patPicker").innerHTML = list.map(s => `<button style="${cvars(s.color)}" data-id="${s.id}">${esc(s.code)}</button>`).join("")
    + `<button class="off" data-id="">Off</button>`;
  renderPattern();
  if (!$("#patStart").value) {
    $("#patStart").value = todayStr();
    $("#patEnd").value = ymd(addDays(new Date(), 182));
  }

  const base = (S.public_url || location.origin);
  const page = `${base}/s/${S.share_token}`, ics = `${page}/calendar.ics`;
  $("#shareUrl").value = page; $("#icsUrl").value = ics;
  $("#webcalLink").href = ics.replace(/^https?:/, "webcal:");
  const local = /^(https?:\/\/)(localhost|127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|[^/]*\.local\b)/.test(base);
  $("#shareWarn").textContent = local
    ? "These links use a local address, so they only work on your home network. Google Calendar and people outside need a public URL — see README (PUBLIC_URL / Cloudflare Tunnel / Tailscale Funnel)."
    : "";
  $("#genForm").title.value = S.title || "";
  $("#weekStartSun").checked = weekStartSun;
  $("#logoutBtn").hidden = !S.auth_required;
}

$("#shiftList").addEventListener("click", async e => {
  const b = e.target.closest("button"); if (!b) return;
  if (b.dataset.edit) return openShift(S.byId[+b.dataset.edit]);
  const ids = activeShifts().map(s => s.id);
  const i = +(b.dataset.up ?? b.dataset.down), j = b.dataset.up !== undefined ? i - 1 : i + 1;
  if (j < 0 || j >= ids.length) return;
  [ids[i], ids[j]] = [ids[j], ids[i]];
  await api("/api/shifts-order", { method: "PUT", body: { ids } });
  await load(); renderSettings();
});
$("#newShift").onclick = () => openShift(null);

/* shift editor */
let editingShift = null;
function updateShiftHours() {
  const f = $("#shiftForm");
  $(".times", f).hidden = f.all_day.checked;
  let txt = "";
  if (!f.all_day.checked && f.start_time.value && f.end_time.value) {
    const [a, b] = [f.start_time.value, f.end_time.value].map(t => { const [h, m] = t.split(":").map(Number); return h * 60 + m; });
    const mins = ((b - a) % 1440 + 1440) % 1440 || 1440;
    txt = `${fmtHours(mins / 60)} hours${b <= a ? " (finishes next day)" : ""}`;
  }
  $("#shiftHours").textContent = txt;
}
function openShift(s) {
  editingShift = s;
  const f = $("#shiftForm");
  $("#shiftTitle").textContent = s ? "Edit shift" : "New shift";
  $("#shiftDelete").hidden = !s;
  f.name.value = s?.name || ""; f.code.value = s?.code || "";
  f.all_day.checked = !!s?.all_day;
  f.start_time.value = s?.start_time || "07:00"; f.end_time.value = s?.end_time || "15:00";
  f.color.value = s?.color || SWATCHES[S.shifts.length % SWATCHES.length];
  $("#swatches").innerHTML = SWATCHES.map(c => `<button type="button" style="--c:${c}" data-c="${c}" aria-label="${c}"></button>`).join("");
  updateShiftHours();
  $("#shiftDlg").showModal();
}
$("#swatches").addEventListener("click", e => { const b = e.target.closest("[data-c]"); if (b) $("#shiftForm").color.value = b.dataset.c; });
["all_day", "start_time", "end_time"].forEach(n => $("#shiftForm")[n].addEventListener("input", updateShiftHours));
$("#shiftForm").addEventListener("submit", async e => {
  e.preventDefault();
  const f = e.target;
  const body = { name: f.name.value.trim(), code: f.code.value.trim(), color: f.color.value, all_day: f.all_day.checked,
    start_time: f.all_day.checked ? null : f.start_time.value, end_time: f.all_day.checked ? null : f.end_time.value };
  await api(editingShift ? `/api/shifts/${editingShift.id}` : "/api/shifts", { method: editingShift ? "PUT" : "POST", body });
  $("#shiftDlg").close(); await load(); renderSettings();
});
$("#shiftDelete").onclick = async () => {
  if (!editingShift || !confirm(`Delete "${editingShift.name}"?`)) return;
  const r = await api(`/api/shifts/${editingShift.id}`, { method: "DELETE" });
  if (r.archived) toast(`Hidden from palette (kept on ${r.used} past days)`);
  if (mode.type === "shift" && mode.id === editingShift.id) mode = { type: "view" };
  $("#shiftDlg").close(); await load(); renderSettings();
};

/* rotation builder */
function renderPattern() {
  $("#patSeq").innerHTML = pattern.length ? pattern.map(id => {
    const s = id === null ? null : S.byId[id];
    return s ? `<span class="p" style="${cvars(s.color)}">${esc(s.code)}</span>` : `<span class="p off">–</span>`;
  }).join("") : '<span class="muted">Tap shifts below to add days</span>';
  $("#patLen").textContent = pattern.length ? `(${pattern.length}-day cycle)` : "";
}
$("#patPicker").addEventListener("click", e => {
  const b = e.target.closest("button"); if (!b) return;
  pattern.push(b.dataset.id ? +b.dataset.id : null); renderPattern();
});
$("#patUndo").onclick = () => { pattern.pop(); renderPattern(); };
$("#patClear").onclick = () => { pattern = []; renderPattern(); };
$("#patApply").onclick = async () => {
  if (!pattern.length) return toast("Add at least one day to the pattern");
  const start = $("#patStart").value, end = $("#patEnd").value;
  if (!start || !end || end < start) return toast("Check the date range");
  const r = await api("/api/pattern", { method: "POST", body: { start, end, pattern, replace: $("#patReplace").checked } });
  toast(`Rotation applied — ${r.assigned} shifts`);
  $("#setDlg").close(); cur = new Date(parseYmd(start).getFullYear(), parseYmd(start).getMonth(), 1); load();
};
$("#clearRange").onclick = async () => {
  const start = $("#patStart").value, end = $("#patEnd").value;
  if (!start || !end || end < start) return toast("Check the date range");
  if (!confirm(`Remove every shift from ${fmtDay(start)} to ${fmtDay(end)}?`)) return;
  const r = await api("/api/clear", { method: "POST", body: { start, end } });
  toast(`Removed ${r.removed} shifts`); load();
};

/* share + general */
$$("[data-copy]").forEach(b => b.onclick = async () => {
  const v = $("#" + b.dataset.copy).value;
  try { await navigator.clipboard.writeText(v); toast("Copied"); }
  catch { $("#" + b.dataset.copy).select(); document.execCommand("copy"); toast("Copied"); }
});
$("#rotateShare").onclick = async () => {
  if (!confirm("Reset share links? Anyone using the old links or calendar subscription will lose access.")) return;
  const r = await api("/api/share/rotate", { method: "POST" });
  S.share_token = r.share_token; renderSettings(); toast("New links created");
};
$("#genForm").addEventListener("submit", async e => {
  e.preventDefault();
  await api("/api/settings", { method: "PUT", body: { title: e.target.title.value } });
  if ($("#weekStartSun").checked !== weekStartSun) { weekStartSun = $("#weekStartSun").checked; ls.set("weekStartSun", weekStartSun); }
  toast("Saved"); load();
});
$("#logoutBtn").onclick = async () => { await api("/api/logout", { method: "POST" }); location.reload(); };

/* ---------- login ---------- */
function showLogin() { $("#login").hidden = false; $("input[name=password]", $("#login")).focus(); }
$("#loginForm").addEventListener("submit", async e => {
  e.preventDefault();
  try {
    await api("/api/login", { method: "POST", body: { password: e.target.password.value } });
    $("#login").hidden = true; e.target.reset(); load();
  } catch {}
});

/* ---------- boot ---------- */
if (SHARE) {
  const ics = `${location.origin}/s/${SHARE}/calendar.ics`;
  $("#subscribeTop").href = ics.replace(/^https?:/, "webcal:");
}
load().catch(() => {});
