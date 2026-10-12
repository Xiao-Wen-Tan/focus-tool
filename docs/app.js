// The phone web app: five tabs (Now, List, Done, Plan, Settings) over the same list the desktop uses.
// Page code only; the rules live in core.js (shared with the desktop) and logic.js (button actions).
import {
  canonical, clone, mergeDocs, stampChanges, nowIso, displaySections, inFolder,
  folderTree, isActive, renameFolder, deleteFolder, addFolder, timerView, WEEKDAY_KEYS, addDays,
} from "./core.js";
import { GitHubStore, SyncEngine, fetchDayPlan } from "./store.js";
import {
  TIME_PRESETS, DEFAULT_MINUTES, ADD_MINUTES_STEP, CELEBRATE_MS, localNow, findTask, timerTask, mmss, mins,
  dateLabel, rowLabel, repeatLabel, sleepText, mascot, upNext, addTask, updateTask, startTask, toggleRun,
  addMinutes, finishTaskNow, removeTask, putBack, clearDone, moveRanked, moveRankedToTop, setWindows,
  windowsText, timelineSegments, minToHm, stampToLocalHm, parseApiOverride, readLocalDoc, replanBlockMessage,
  syncPauseUntil, changedFields,
} from "./logic.js";

const APP_VERSION = "2.0.0";
const KEY_DOC = "focus.doc";
const KEY_SETTINGS = "focus.settings";
const KEY_UI = "focus.ui";
const KEY_PLAN = "focus.dayplan";
const POLL_MS = 15000;
const EDIT_SYNC_MS = 1500;
const DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

// ---------------------------------------------------------------- storage helpers (never throw)

function load(key, fallback = null) {
  try {
    const t = localStorage.getItem(key);
    return t ? JSON.parse(t) : fallback;
  } catch {
    return fallback;
  }
}

function save(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch { /* private mode or full: keep going in memory */ }
}

function safeStorage() {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------- app state

const app = {
  doc: null, // the list as shown
  lastSaved: null, // the version the next change is compared with (for change stamps)
  settings: Object.assign({ owner: "", repo: "focus-data", branch: "main", token: "" }, load(KEY_SETTINGS, {})),
  ui: Object.assign({ tab: "now", folder: null }, load(KEY_UI, {})),
  engine: null,
  store: null,
  dayPlan: load(KEY_PLAN, null),
  sync: { status: "idle", message: "", at: null },
  blocked: null, // a message when the file on GitHub cannot be used (wrong version); "Sync now" retries
  localBlock: null, // a message when the list kept on this phone came from a newer app; only an update clears it
  syncing: false,
  syncPromise: null,
  syncAgain: false,
  syncTimer: null,
  pauseUntil: 0, // after "rate limited": no automatic sync before this time (ms)
  celebrateUntil: 0,
  asking: false, // "time is up" message showing
  planBusy: "",
  planNote: "", // why the last Re-plan now did not ask GitHub
  api: null,
};

function blockedText() {
  return app.localBlock || app.blocked;
}

function loadDoc() {
  let text = null;
  try {
    text = localStorage.getItem(KEY_DOC);
  } catch { /* storage blocked */ }
  const r = readLocalDoc(text);
  if (r.backup) {
    // Keep the saved text before anything can replace it. An existing backup is never overwritten.
    try {
      if (!localStorage.getItem(r.backup.key)) localStorage.setItem(r.backup.key, r.backup.text);
    } catch { /* storage blocked: the block below still stops any write */ }
  }
  app.localBlock = r.block;
  return r.doc;
}

function now() {
  return localNow(app.doc);
}

function connected() {
  const s = app.settings;
  return Boolean(s.owner && s.repo && s.token);
}

function makeEngine() {
  app.store = null;
  app.engine = null;
  if (!connected()) return;
  const s = app.settings;
  app.store = new GitHubStore({ owner: s.owner.trim(), repo: s.repo.trim(), token: s.token.trim(), branch: s.branch || "main", api: app.api || undefined });
  app.engine = new SyncEngine(app.store, safeStorage());
}

// ---------------------------------------------------------------- changing the list

/** Apply `fn` to a copy of the list, stamp what changed, keep it on the phone, and send it soon. */
function commit(fn) {
  if (blockedText()) {
    toast(blockedText());
    return undefined;
  }
  const next = clone(app.doc);
  const result = fn(next);
  if (result === false || result === null) return result;
  app.doc = stampChanges(app.lastSaved, next, nowIso());
  app.lastSaved = app.doc;
  save(KEY_DOC, app.doc);
  if (app.engine) app.engine.setDirty(true);
  scheduleSync(EDIT_SYNC_MS);
  render();
  return result;
}

function scheduleSync(ms) {
  if (!app.engine) return;
  clearTimeout(app.syncTimer);
  app.syncTimer = setTimeout(() => runSync(), ms);
}

/** One sync with GitHub. Always resolves to a result {status, message, ...}.
 * If a sync is already running, this waits for it and asks for one more afterwards.
 * `force` (a button press) skips the wait after GitHub said "rate limited"; automatic syncs respect it. */
function runSync({ force = false } = {}) {
  if (app.syncPromise) {
    app.syncAgain = true;
    return app.syncPromise;
  }
  if (!app.engine) return Promise.resolve({ status: "none", message: "Not connected to GitHub." });
  if (blockedText()) return Promise.resolve({ status: "version", message: blockedText() });
  if (!force && Date.now() < app.pauseUntil) {
    // Keep exactly one automatic retry, at the time GitHub gave us.
    clearTimeout(app.syncTimer);
    app.syncTimer = setTimeout(() => runSync(), Math.min(app.pauseUntil - Date.now() + 50, 3600000));
    return Promise.resolve({ status: "rate_limited", message: "Waiting, because GitHub asked us to slow down.", retryAfterS: (app.pauseUntil - Date.now()) / 1000 });
  }
  if (typeof navigator !== "undefined" && navigator.onLine === false) {
    app.sync = { status: "offline", message: "Offline. Your changes are kept on this phone.", at: app.sync.at };
    renderStatus();
    return Promise.resolve({ status: "offline", message: app.sync.message });
  }
  app.syncPromise = doSync().finally(() => {
    app.syncPromise = null;
    if (app.syncAgain) {
      app.syncAgain = false;
      scheduleSync(300);
    }
  });
  return app.syncPromise;
}

async function doSync() {
  app.syncing = true;
  renderStatus();
  const sent = canonical(app.doc);
  let r;
  try {
    r = await app.engine.sync(app.doc);
  } catch (ex) {
    r = { status: "error", message: (ex && ex.message) || String(ex), doc: app.doc, remoteChanged: false };
  }
  app.syncing = false;
  app.pauseUntil = syncPauseUntil(r, Date.now());
  if (r.status === "ok") {
    const before = canonical(app.doc);
    if (before === sent) {
      app.doc = r.doc;
    } else {
      app.doc = mergeDocs(app.doc, r.doc); // edits made while this sync was running
      app.engine.setDirty(true);
      app.syncAgain = true;
    }
    app.lastSaved = app.doc;
    save(KEY_DOC, app.doc);
    app.sync = { status: "ok", message: r.pushed ? "Saved to GitHub." : "Up to date.", at: new Date() };
    if (canonical(app.doc) !== before) render();
  } else {
    if (r.status === "version") app.blocked = r.message;
    app.sync = { status: r.status, message: r.message, at: app.sync.at };
    if (r.status === "rate_limited") scheduleSync(Math.min(app.pauseUntil - Date.now(), 3600000));
  }
  renderStatus();
  if (blockedText()) render();
  return r;
}

async function refreshDayPlan() {
  if (!app.store || blockedText()) return;
  try {
    const p = await fetchDayPlan(app.store);
    if (canonical(p) !== canonical(app.dayPlan)) {
      app.dayPlan = p;
      save(KEY_PLAN, p);
      render();
    }
  } catch { /* keep the last plan we had */ }
}

// ---------------------------------------------------------------- tiny DOM helper

function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, String(v));
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

const SVG_NS = "http://www.w3.org/2000/svg";
function s(tag, attrs = {}) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, String(v));
  return el;
}

function btn(label, onclick, cls = "", extra = {}) {
  return h("button", { type: "button", class: `btn ${cls}`.trim(), onclick, ...extra }, label);
}

function card(title, ...kids) {
  return h("section", { class: "card" }, title ? h("h2", { class: "card-title" }, title) : null, ...kids);
}

let toastTimer = null;
function toast(text) {
  const el = document.getElementById("toast");
  if (!el) return;
  el.textContent = text;
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 3500);
}

// ---------------------------------------------------------------- shell: tabs and status

const TABS = [
  ["now", "Now", "⏱"],
  ["list", "List", "☰"],
  ["done", "Done", "✓"],
  ["plan", "Plan", "🗓"],
  ["settings", "Settings", "⚙"],
];

function setTab(tab) {
  app.ui.tab = tab;
  save(KEY_UI, app.ui);
  render();
  const main = document.getElementById("main");
  if (main) main.scrollTop = 0;
  window.scrollTo(0, 0);
  if (tab === "plan") refreshDayPlan();
}

function renderTabs() {
  const bar = document.getElementById("tabs");
  if (!bar) return;
  bar.replaceChildren(...TABS.map(([id, label, icon]) => h("button", {
    type: "button", class: `tab${app.ui.tab === id ? " active" : ""}`, "aria-current": app.ui.tab === id ? "page" : null,
    onclick: () => setTab(id),
  }, h("span", { class: "tab-icon", "aria-hidden": "true" }, icon), h("span", { class: "tab-label" }, label))));
}

function statusText() {
  if (blockedText()) return { cls: "bad", text: blockedText() };
  if (!connected()) return { cls: "dim", text: "On this phone only. Connect GitHub in Settings to share with the desktop." };
  if (app.syncing) return { cls: "dim", text: "Syncing..." };
  const dirty = app.engine && app.engine.dirty;
  const st = app.sync.status;
  if (st === "offline") return { cls: "warn", text: "Offline. Your changes are kept on this phone." };
  if (st === "auth") return { cls: "bad", text: app.sync.message };
  if (st === "rate_limited") return { cls: "warn", text: "GitHub asked us to wait. Will try again later." };
  if (st === "error") return { cls: "warn", text: app.sync.message };
  if (dirty) return { cls: "dim", text: "Changes waiting to be sent." };
  if (app.sync.at) return { cls: "ok", text: `Synced at ${localNow(app.doc, app.sync.at).hhmm}` };
  return { cls: "dim", text: "Connecting..." };
}

function renderStatus() {
  for (const el of document.querySelectorAll("[data-status]")) {
    const st = statusText();
    el.className = `status ${st.cls}`;
    el.textContent = st.text;
  }
}

function render() {
  renderTabs();
  const main = document.getElementById("main");
  if (!main) return;
  const views = { now: viewNow, list: viewList, done: viewDone, plan: viewPlan, settings: viewSettings };
  const view = views[app.ui.tab] || viewNow;
  let content;
  try {
    content = view();
  } catch (ex) {
    content = card("Something went wrong", h("p", { class: "hint" }, String((ex && ex.message) || ex)));
    console.error(ex);
  }
  main.replaceChildren(h("div", { class: "status-wrap" }, h("p", { "data-status": "", class: "status" })), content);
  renderStatus();
}

// ---------------------------------------------------------------- Now tab

const DISK = { size: 240, r: 110 };

function piePath(cx, cy, r, frac) {
  if (frac <= 0) return "";
  if (frac >= 0.9999) return `M ${cx} ${cy - r} A ${r} ${r} 0 1 1 ${cx - 0.01} ${cy - r} Z`;
  // From 12 o'clock, clockwise, like the desktop disk (drawPie from 90 degrees with a negative span).
  const a = 2 * Math.PI * frac;
  const x = cx + r * Math.sin(a), y = cy - r * Math.cos(a);
  return `M ${cx} ${cy} L ${cx} ${cy - r} A ${r} ${r} 0 ${frac > 0.5 ? 1 : 0} 1 ${x} ${y} Z`;
}

function arcPath(cx, cy, r, frac) {
  if (frac <= 0) return "";
  const f = Math.min(frac, 0.9999);
  const a = 2 * Math.PI * f;
  const x = cx + r * Math.sin(a), y = cy - r * Math.cos(a);
  return `M ${cx} ${cy - r} A ${r} ${r} 0 ${f > 0.5 ? 1 : 0} 1 ${x} ${y}`;
}

function buildDisk() {
  const { size, r } = DISK;
  const c = size / 2;
  const svg = s("svg", { viewBox: `0 0 ${size} ${size}`, class: "disk", role: "img", "aria-label": "Time left" });
  svg.append(
    s("circle", { cx: c, cy: c, r, class: "disk-track" }),
    s("circle", { cx: c, cy: c, r, class: "disk-over", "data-k": "over" }),
    s("path", { d: "", class: "disk-left", "data-k": "left" }),
    s("path", { d: "", class: "disk-ring", "data-k": "ring" }),
    s("circle", { cx: c, cy: c, r: r * 0.42, class: "disk-hole" }),
  );
  const t = s("text", { x: c, y: c, class: "disk-mascot", "text-anchor": "middle", "dominant-baseline": "central", "data-k": "mascot" });
  svg.append(t);
  return svg;
}

function updateDisk(svg, doc, n) {
  const { size, r } = DISK;
  const c = size / 2;
  const tm = doc.timer;
  const get = (k) => svg.querySelector(`[data-k="${k}"]`);
  let left = "", ring = "", over = false;
  if (tm.running) {
    const v = timerView(tm, n.naive);
    if (v.overtime > 0) {
      over = true;
      ring = arcPath(c, c, r - 6, Math.min(1, v.overtime / Math.max(1, v.estimate)));
    } else {
      left = piePath(c, c, r, v.fraction);
    }
  }
  get("over").style.display = over ? "" : "none";
  get("left").setAttribute("d", left);
  get("ring").setAttribute("d", ring);
  get("mascot").textContent = mascot(doc, n.naive, Date.now() < app.celebrateUntil);
}

function nowLines(doc, n) {
  const tm = doc.timer;
  const running = timerTask(doc);
  const lines = [];
  if (tm.running) {
    const v = timerView(tm, n.naive);
    if (app.asking) lines.push("Time is up. Add more time, or press Done.");
    else if (v.overtime > 0) lines.push(`Over by ${mmss(v.overtime)}   ·   on this: ${mins(v.elapsed)}`);
    else lines.push(`${mmss(v.remaining)} left   ·   on this: ${mins(v.elapsed)}${tm.paused ? "   (paused)" : ""}`);
    if (!running) lines.push("This task is no longer on the list.");
  }
  return lines;
}

function viewNow() {
  const doc = app.doc;
  const n = now();
  const tm = doc.timer;
  const wrap = h("div", { class: "now" });
  const disk = buildDisk();
  updateDisk(disk, doc, n);
  wrap.append(h("div", { class: "disk-wrap" }, disk));
  const title = h("h1", { class: "now-title", "data-k": "title" });
  const detail = h("p", { class: "now-detail" });
  const timeLine = h("p", { class: "now-time", "data-k": "time", "aria-live": "polite" });
  const buttons = h("div", { class: "now-buttons" });
  if (tm.running) {
    const t = timerTask(doc);
    title.textContent = tm.task || (t && t.name) || "";
    if (t && t.details.trim()) detail.textContent = t.details.trim().split("\n")[0];
    buttons.append(
      btn(tm.paused ? "▶ Resume" : "⏸ Pause", () => { app.asking = false; commit((d) => toggleRun(d, now())); }, "primary big"),
      btn("✓ Done", () => doDone(tm.task_id), "big"),
      btn(`+${ADD_MINUTES_STEP}`, () => { app.asking = false; commit((d) => addMinutes(d, ADD_MINUTES_STEP, now())); }, "big"),
    );
  } else {
    const next = upNext(doc, app.dayPlan, n);
    if (next.task) {
      title.textContent = `Up next: ${next.task.name}`;
      if (next.task.details.trim()) detail.textContent = next.task.details.trim().split("\n")[0];
      timeLine.textContent = next.block
        ? `In the plan ${next.block.start}-${next.block.end}. ${next.task.estimate_min} min planned.`
        : `${next.task.estimate_min} min planned. Press Start when ready.`;
      buttons.append(btn("▶ Start", () => doStart(next.task.id), "primary big"),
        btn("Choose another", () => setTab("list"), "big quiet"));
    } else {
      title.textContent = doc.done.length ? "All done, the list is empty" : "Nothing on the list yet";
      timeLine.textContent = "Add a task in the List tab.";
      buttons.append(btn("+ Add a task", () => openEditor(null), "primary big"));
    }
  }
  wrap.append(title, detail, timeLine, buttons);
  wrap._tick = () => {
    const nn = now();
    updateDisk(disk, app.doc, nn);
    if (app.doc.timer.running) timeLine.textContent = nowLines(app.doc, nn).join("\n");
  };
  wrap._tick();
  return wrap;
}

function doStart(id) {
  app.asking = false;
  const t = findTask(app.doc, id);
  if (!t) return;
  if (!isActive(t, now().day)) {
    toast(`${t.name} is a repeating task that is done for today.`);
    return;
  }
  commit((d) => startTask(d, id, now()));
  setTab("now");
}

function doDone(id) {
  app.asking = false;
  const rec = commit((d) => finishTaskNow(d, id, now()));
  if (rec) {
    app.celebrateUntil = Date.now() + CELEBRATE_MS;
    toast(`Finished ${rec.name}`);
    render();
  }
}

/** Once a second: move the disk and the clock, and notice when time is up. */
function tick() {
  if (document.hidden) return;
  const doc = app.doc;
  if (doc && doc.timer.running) {
    // Show "time is up" when the countdown reaches zero while the app is open. Tracked in memory only,
    // so the list itself does not change every second.
    const tm = doc.timer;
    const v = timerView(tm, now().naive);
    const key = `${tm.task_id}|${tm.estimate_min}`;
    const up = v.estimate > 0 && v.remaining === 0;
    if (app.upKey === key && !app.wasUp && up && !tm.paused) {
      app.asking = true;
      try { if (navigator.vibrate) navigator.vibrate([200, 100, 200]); } catch { /* not supported */ }
    }
    app.upKey = key;
    app.wasUp = up;
  }
  const main = document.getElementById("main");
  const view = main && main.querySelector(".now");
  if (view && view._tick) view._tick();
}

// ---------------------------------------------------------------- List tab

function folderOptions(selected, { all = true, none = false, extra = [] } = {}) {
  const opts = [];
  if (all) opts.push(h("option", { value: "", selected: selected == null }, "All folders"));
  if (none) opts.push(h("option", { value: "", selected: selected == null }, "No folder"));
  for (const [path, depth] of folderTree(app.doc.folders, app.doc.plan.tasks)) {
    const name = path.split("/").pop();
    opts.push(h("option", { value: path, selected: path === selected }, `${"   ".repeat(depth)}${name}`));
  }
  for (const [value, label] of extra) opts.push(h("option", { value }, label));
  return opts;
}

function taskRow(t, { ranked, visibleIds, today }) {
  const asleep = Boolean(t.repeat && !isActive(t, today));
  const sub = [];
  if (asleep) sub.push(sleepText(t, today));
  else if (t.repeat) sub.push(t.repeat.kind === "days" ? `Repeats on ${repeatLabel(t.repeat)}` : `Repeats ${repeatLabel(t.repeat).toLowerCase()}`);
  if (t.deadline && t.target) sub.push(`Deadline ${dateLabel(t.deadline)}, aim ${dateLabel(t.target)}`);
  else if (t.deadline) sub.push("Deadline");
  else if (t.target) sub.push("Aim to finish");
  if (t.folder && app.ui.folder == null) sub.push(t.folder);
  if (t.details.trim()) sub.push(t.details.trim().split("\n")[0]);
  const main = h("button", { type: "button", class: "row-main", onclick: () => openEditor(t.id) },
    h("span", { class: "row-label" }, rowLabel(app.doc, t)),
    sub.length ? h("span", { class: "row-sub" }, sub.join("  ·  ")) : null);
  const tools = ranked ? h("div", { class: "row-tools" },
    btn("↑", () => commit((d) => moveRanked(d, t.id, -1, visibleIds)), "icon", { "aria-label": `Move ${t.name} up` }),
    btn("↓", () => commit((d) => moveRanked(d, t.id, 1, visibleIds)), "icon", { "aria-label": `Move ${t.name} down` }),
    btn("⤒", () => commit((d) => moveRankedToTop(d, t.id)), "icon", { "aria-label": `Move ${t.name} to the top` }),
  ) : null;
  return h("li", { class: `row${asleep ? " asleep" : ""}${app.doc.timer.running && app.doc.timer.task_id === t.id ? " on-timer" : ""}` }, main, tools);
}

function viewList() {
  const doc = app.doc;
  const today = now().day;
  const filter = app.ui.folder && folderTree(doc.folders, doc.plan.tasks).some(([p]) => p === app.ui.folder) ? app.ui.folder : null;
  const [dated, ranked] = displaySections(doc.plan.tasks);
  const shownDated = dated.filter((t) => inFolder(t, filter));
  const shownRanked = ranked.filter((t) => inFolder(t, filter));
  const visibleIds = shownRanked.map((t) => t.id);
  const select = h("select", { class: "folder-filter", "aria-label": "Show folder", onchange: (e) => {
    app.ui.folder = e.target.value || null;
    save(KEY_UI, app.ui);
    render();
  } }, ...folderOptions(filter));
  const head = h("div", { class: "list-head" }, select, btn("+ Add", () => openEditor(null), "primary"));
  const out = h("div", { class: "list" }, head);
  if (shownDated.length) {
    out.append(card("📅 By date", h("ul", { class: "rows" }, shownDated.map((t) => taskRow(t, { ranked: false, today })))));
  }
  const hint = h("p", { class: "hint" }, "Tap a task to edit, start or finish it. New tasks go to the top.");
  out.append(card("Your order", shownRanked.length
    ? h("ul", { class: "rows" }, shownRanked.map((t) => taskRow(t, { ranked: true, visibleIds, today })))
    : h("p", { class: "empty" }, filter ? "No tasks in this folder." : "No tasks yet. Tap + Add."), hint));
  return out;
}

// ---------------------------------------------------------------- the editor sheet

function closeSheet() {
  const root = document.getElementById("sheet");
  if (root) {
    root.replaceChildren();
    root.hidden = true;
  }
  document.body.classList.remove("sheet-open");
}

function openEditor(id) {
  const root = document.getElementById("sheet");
  if (!root) return;
  const existing = id ? findTask(app.doc, id) : null;
  if (id && !existing) return;
  const today = now().day;
  const t = existing ? clone(existing) : {
    name: "", estimate_min: DEFAULT_MINUTES, details: "", deadline: null, target: null,
    folder: app.ui.folder || null, repeat: null,
  };
  const form = { ...t, repeat: t.repeat ? clone(t.repeat) : null };
  const opened = clone({ name: t.name, estimate_min: t.estimate_min, details: t.details, deadline: t.deadline,
    target: t.target, folder: t.folder, repeat: t.repeat }); // to save only what the user changed

  const name = h("input", { type: "text", class: "input", placeholder: "What is the task?", value: form.name, enterkeyhint: "done", "aria-label": "Task name" });
  name.addEventListener("input", () => { form.name = name.value; });

  const pills = h("div", { class: "pills", role: "group", "aria-label": "Minutes" });
  const drawPills = () => {
    const values = TIME_PRESETS.includes(form.estimate_min) ? TIME_PRESETS : [...TIME_PRESETS, form.estimate_min].sort((a, b) => a - b);
    pills.replaceChildren(...values.map((m) => h("button", {
      type: "button", class: `pill${m === form.estimate_min ? " on" : ""}`, "aria-pressed": m === form.estimate_min ? "true" : "false",
      onclick: () => { form.estimate_min = m; drawPills(); },
    }, `${m}m`)));
  };
  drawPills();

  const details = h("textarea", { class: "input", rows: "4", placeholder: "Details (optional). The first step, a link, what done looks like.", "aria-label": "Details" });
  details.value = form.details || "";
  details.addEventListener("input", () => { form.details = details.value; });

  // dates
  const dateRow = (label, key) => {
    const input = h("input", { type: "date", class: "input date", value: form[key] || "", "aria-label": label });
    input.addEventListener("change", () => { form[key] = input.value || null; });
    const clear = btn("Clear", () => { input.value = ""; form[key] = null; }, "quiet small");
    return h("label", { class: "field" }, h("span", { class: "field-label" }, label), h("span", { class: "field-row" }, input, clear));
  };
  const dates = h("div", { class: "dates" }, dateRow("Deadline", "deadline"), dateRow("Aim to finish by", "target"));
  const datesNote = h("p", { class: "hint" }, "Repeating tasks have no dates. They follow your order.");

  // folder
  const folderSel = h("select", { class: "input", "aria-label": "Folder" });
  const newFolderBox = h("div", { class: "field-row", hidden: true });
  const newFolderInput = h("input", { type: "text", class: "input", placeholder: "Folder name" });
  let newParent = null;
  const drawFolders = () => {
    folderSel.replaceChildren(...folderOptions(form.folder, {
      all: false, none: true,
      extra: [["__new__", "New folder..."], ...(form.folder ? [["__sub__", `New subfolder in ${form.folder.split("/").pop()}...`]] : [])],
    }));
  };
  drawFolders();
  folderSel.addEventListener("change", () => {
    const v = folderSel.value;
    if (v === "__new__" || v === "__sub__") {
      newParent = v === "__sub__" ? form.folder : null;
      newFolderBox.hidden = false;
      newFolderInput.placeholder = newParent ? `Subfolder of ${newParent}` : "Folder name";
      newFolderInput.focus();
      drawFolders();
      return;
    }
    form.folder = v || null;
    newFolderBox.hidden = true;
    drawFolders();
  });
  newFolderBox.append(newFolderInput, btn("Add folder", () => {
    const nameText = newFolderInput.value.split("/").map((x) => x.trim()).filter(Boolean).join("/");
    if (!nameText) return;
    const path = newParent ? `${newParent}/${nameText}` : nameText;
    commit((d) => { addFolder(d, path); });
    form.folder = path;
    newFolderInput.value = "";
    newFolderBox.hidden = true;
    drawFolders();
  }, "small"));

  // repeat
  const kinds = [["", "Never"], ["daily", "Every day"], ["weekdays", "Weekdays"], ["days", "Chosen days"], ["every", "Every N days"]];
  const repeatSel = h("select", { class: "input", "aria-label": "Repeat" },
    ...kinds.map(([v, l]) => h("option", { value: v, selected: (form.repeat ? form.repeat.kind : "") === v }, l)));
  const repeatExtra = h("div", { class: "repeat-extra" });
  const drawRepeat = () => {
    repeatExtra.replaceChildren();
    const r = form.repeat;
    dates.hidden = Boolean(r);
    datesNote.hidden = !r;
    if (r && r.kind === "days") {
      repeatExtra.append(h("div", { class: "pills", role: "group", "aria-label": "Days" }, WEEKDAY_KEYS.map((k, i) => {
        const on = (r.days || []).includes(k);
        return h("button", { type: "button", class: `pill${on ? " on" : ""}`, "aria-pressed": on ? "true" : "false", onclick: () => {
          const set = new Set(r.days || []);
          if (set.has(k)) set.delete(k); else set.add(k);
          r.days = WEEKDAY_KEYS.filter((x) => set.has(x));
          drawRepeat();
        } }, DAY_NAMES[i]);
      })));
    } else if (r && r.kind === "every") {
      const num = h("input", { type: "number", class: "input num", min: "1", max: "365", inputmode: "numeric", value: String(r.every || 2), "aria-label": "Every how many days" });
      num.addEventListener("input", () => { r.every = Math.max(1, Math.min(365, Math.trunc(Number(num.value) || 1))); });
      repeatExtra.append(h("label", { class: "field-row" }, h("span", {}, "Every"), num, h("span", {}, `days, counting from ${dateLabel(r.anchor)}`)));
    }
  };
  repeatSel.addEventListener("change", () => {
    const k = repeatSel.value;
    if (!k) form.repeat = null;
    else if (k === "days") form.repeat = { kind: "days", days: [WEEKDAY_KEYS[(new Date(`${today}T12:00:00Z`).getUTCDay() + 6) % 7]] };
    else if (k === "every") form.repeat = { kind: "every", every: 2, anchor: today };
    else form.repeat = { kind: k };
    drawRepeat();
  });
  drawRepeat();

  const field = (label, ...kids) => h("label", { class: "field" }, h("span", { class: "field-label" }, label), ...kids);
  const asleep = existing && existing.repeat && !isActive(existing, today);

  const collect = () => {
    const f = {
      name: form.name, estimate_min: form.estimate_min, details: form.details, folder: form.folder,
      repeat: form.repeat, deadline: form.repeat ? null : form.deadline, target: form.repeat ? null : form.target,
    };
    if (f.repeat && f.repeat.kind === "days" && !(f.repeat.days || []).length) {
      toast("Pick at least one day for the repeat.");
      return null;
    }
    if (!String(f.name || "").trim()) {
      toast("Give the task a name first.");
      name.focus();
      return null;
    }
    return f;
  };
  const saveExisting = () => {
    const f = collect();
    if (!f) return false;
    if (!findTask(app.doc, id)) {
      toast("This task was removed on another device.");
      closeSheet();
      return false;
    }
    const changed = changedFields(opened, f);
    if (Object.keys(changed).length) commit((d) => updateTask(d, id, changed));
    return true;
  };

  const actions = h("div", { class: "sheet-actions" });
  if (existing) {
    actions.append(
      btn("Save", () => { if (saveExisting()) closeSheet(); }, "primary"),
      btn("▶ Start", () => { if (saveExisting()) { closeSheet(); doStart(id); } }, "", { disabled: asleep || null }),
      btn("✓ Done", () => { if (saveExisting()) { closeSheet(); doDone(id); } }, "", { disabled: asleep || null }),
      btn("Delete", () => {
        if (!window.confirm(`Delete "${existing.name}" without finishing it?`)) return;
        closeSheet();
        commit((d) => removeTask(d, id));
      }, "danger"),
    );
  } else {
    actions.append(btn("+ Add to list", () => {
      const f = collect();
      if (!f) return;
      commit((d) => addTask(d, f));
      closeSheet();
    }, "primary"));
  }
  actions.append(btn("Cancel", closeSheet, "quiet"));

  const sheet = h("div", { class: "sheet", role: "dialog", "aria-modal": "true", "aria-label": existing ? "Edit task" : "Add a task" },
    h("div", { class: "sheet-grip", "aria-hidden": "true" }),
    h("h2", { class: "card-title" }, existing ? "Edit task" : "Add a task"),
    asleep ? h("p", { class: "hint" }, `${sleepText(existing, today)}. Done for today.`) : null,
    name, pills, details,
    field("Repeat", repeatSel), repeatExtra,
    dates, datesNote,
    field("Folder", folderSel), newFolderBox,
    actions);
  root.replaceChildren(h("div", { class: "sheet-backdrop", onclick: closeSheet }), sheet);
  root.hidden = false;
  document.body.classList.add("sheet-open");
  if (!existing) setTimeout(() => name.focus(), 50);
}

// ---------------------------------------------------------------- Done tab

function viewDone() {
  const doc = app.doc;
  const items = [...doc.done].reverse();
  const out = h("div", { class: "done" });
  const list = items.length ? h("ul", { class: "rows" }, items.map((d) => {
    const when = d.finished_at && d.finished_at.length >= 16 ? d.finished_at.slice(11, 16) : d.finished_at;
    const day = d.finished_at ? d.finished_at.slice(0, 10) : "";
    const actual = d.actual_min ? `${d.actual_min}m actual` : "not timed";
    return h("li", { class: "row" },
      h("div", { class: "row-main static" },
        h("span", { class: "row-label" }, d.name),
        h("span", { class: "row-sub" }, `${day === now().day ? "Today" : dateLabel(day)} ${when}  ·  ${d.estimate_min}m planned, ${actual}`)),
      h("div", { class: "row-tools" }, btn("↩ Put back", () => { commit((x) => putBack(x, d.id)); toast(`${d.name} is back on the list`); }, "small")));
  })) : h("p", { class: "empty" }, "Nothing finished yet.");
  out.append(card("✅ Completed", h("p", { class: "hint" }, "Newest first. Put back moves a task to the top of your list."), list,
    items.length ? h("div", { class: "card-foot" }, btn("Clear completed", () => {
      if (window.confirm("Clear the completed list on all devices?")) commit((x) => clearDone(x));
    }, "quiet")) : null));
  return out;
}

// ---------------------------------------------------------------- Plan tab

const PX_PER_MIN = 2;
const KIND_LABEL = { urgent: "Urgent", dated: "Dated", ranked: "Your order", ahead: "Ahead" };

function viewPlan() {
  const doc = app.doc;
  const n = now();
  const p = app.dayPlan;
  const out = h("div", { class: "plan" });
  const replan = btn("Re-plan now", replanNow, "primary", { disabled: !connected() || Boolean(app.planBusy) || null });
  const head = h("div", { class: "plan-head" }, replan, app.planBusy ? h("p", { class: "hint" }, app.planBusy) : app.planNote ? h("p", { class: "status warn" }, app.planNote) : null);
  if (!connected()) {
    out.append(card("Plan", h("p", { class: "hint" }, "Connect GitHub in Settings to see the day plan the nightly job makes.")));
    return out;
  }
  if (!p || !p.date) {
    out.append(head, card("Plan", h("p", { class: "empty" }, "No plan yet. Tap Re-plan now, or wait for the 7 pm job.")));
    return out;
  }
  const which = p.date === n.day ? "Today" : p.date === addDays(n.day, 1) ? "Tomorrow" : "";
  const gen = stampToLocalHm(p.generated_at, doc.planner.tz);
  const genDay = p.generated_at ? localNow(doc, new Date(p.generated_at)).day : "";
  const sub = [
    gen ? `generated ${genDay && genDay !== n.day ? `${dateLabel(genDay)} ` : ""}${gen}` : "",
    p.generated_by === "actions" ? "by the nightly job" : p.generated_by === "desktop" ? "on the desktop" : "",
  ].filter(Boolean).join(" ");
  const total = (p.blocks || []).reduce((a, b) => a + (b.minutes || 0), 0);
  out.append(head);
  const info = h("p", { class: "hint" }, `${sub}. ${total} min planned${p.free_min ? `, ${p.free_min} min free` : ""}.`);
  const live = new Set(doc.plan.tasks.map((t) => t.id));
  const segs = timelineSegments(p, doc.planner);
  const timeline = h("div", { class: "timeline" });
  segs.forEach((g, gi) => {
    if (gi > 0) timeline.append(h("div", { class: "tl-break" }, `${minToHm(segs[gi - 1].end)} to ${minToHm(g.start)} break`));
    const seg = h("div", { class: "tl-seg", style: { height: `${(g.end - g.start) * PX_PER_MIN}px` } });
    for (let m = Math.ceil(g.start / 60) * 60; m < g.end; m += 60) {
      if (m === g.start) continue;
      seg.append(h("div", { class: "tl-hour", style: { top: `${(m - g.start) * PX_PER_MIN}px` } }, h("span", {}, minToHm(m))));
    }
    seg.append(h("div", { class: "tl-hour first", style: { top: "0px" } }, h("span", {}, minToHm(g.start))));
    for (const b of g.blocks) {
      const ok = live.has(b.task_id);
      const tall = b.len * PX_PER_MIN >= 56;
      const tiny = b.len * PX_PER_MIN < 26;
      const label = `${b.start}-${b.end}  ${b.name}  (${b.minutes}m)`;
      const extra = [KIND_LABEL[b.kind] || b.kind, b.date ? `${b.date_kind === "deadline" ? "due" : "aim"} ${dateLabel(b.date)}` : "", b.left_after_min ? `${b.left_after_min}m left after` : ""].filter(Boolean).join(" · ");
      seg.append(h("button", {
        type: "button", class: `tl-block k-${b.kind}${ok ? "" : " gone"}${tall ? "" : " short"}${tiny ? " tiny" : ""}`,
        style: { top: `${b.top * PX_PER_MIN + 1}px`, height: `${Math.max(b.len * PX_PER_MIN - 2, 8)}px` },
        "aria-label": `${label}. ${extra}. Tap to start.`,
        onclick: () => (ok ? doStart(b.task_id) : toast("That task is no longer on the list.")),
      }, h("span", { class: "tl-name" }, `${b.repeat ? "🔁 " : ""}${b.name}`),
      h("span", { class: "tl-time" }, `${b.start}-${b.end} · ${b.minutes}m`),
      tall ? h("span", { class: "tl-kind" }, extra) : null));
    }
    timeline.append(seg);
  });
  out.append(card(`${which ? `${which}, ` : ""}${dateLabel(p.date)}`, info,
    segs.length ? timeline : h("p", { class: "empty" }, "No working hours on this day."),
    h("div", { class: "legend" }, Object.entries(KIND_LABEL).map(([k, l]) => h("span", { class: `chip k-${k}` }, l)))));
  if ((p.warnings || []).length) {
    out.append(card("Heads up", h("ul", { class: "warnings" }, p.warnings.map((w) => h("li", {}, h("strong", {}, w.name), ` ${w.message}`)))));
  }
  if ((p.outlook || []).length) {
    const names = new Map(doc.plan.tasks.map((t) => [t.id, t.name]));
    out.append(card("Coming days", h("ul", { class: "outlook" }, p.outlook.map((o) =>
      h("li", {}, `${dateLabel(o.day)}  ·  ${names.get(o.task_id) || "A task"}  ·  ${o.minutes}m`)))));
  }
  return out;
}

async function replanNow() {
  if (!app.store || app.planBusy) return;
  const before = app.dayPlan && app.dayPlan.generated_at;
  app.planNote = "";
  const setBusy = (t) => { app.planBusy = t; if (app.ui.tab === "plan") render(); };
  try {
    setBusy("Saving your list...");
    // Wait for a sync that is already running, then run a fresh one that includes every edit so far.
    while (app.syncPromise) {
      try { await app.syncPromise; } catch { /* its result does not matter here */ }
    }
    const r = await runSync({ force: true });
    const why = replanBlockMessage(r);
    if (why) {
      app.planNote = why;
      setBusy("");
      return;
    }
    setBusy("Asking GitHub to re-plan. This usually takes about a minute.");
    await app.store.dispatch("replan.yml", app.settings.branch || "main", { mode: "now" });
    const t0 = Date.now();
    while (Date.now() - t0 < 180000) {
      await new Promise((r) => setTimeout(r, 5000));
      let p = null;
      try { p = await fetchDayPlan(app.store); } catch { p = null; }
      if (p && p.generated_at && p.generated_at !== before) {
        app.dayPlan = p;
        save(KEY_PLAN, p);
        setBusy("");
        toast("New plan is ready.");
        return;
      }
      setBusy(`Waiting for the new plan (${Math.round((Date.now() - t0) / 1000)} s)...`);
    }
    setBusy("");
    toast("No new plan after 3 minutes. Check the Actions page of your data repository.");
  } catch (ex) {
    setBusy("");
    toast(`Could not re-plan. ${(ex && ex.message) || ex}`);
  }
}

// ---------------------------------------------------------------- Settings tab

function viewSettings() {
  const doc = app.doc;
  const st = app.settings;
  const out = h("div", { class: "settings" });

  // GitHub
  const owner = h("input", { type: "text", class: "input", value: st.owner, autocapitalize: "off", autocorrect: "off", spellcheck: "false", placeholder: "your GitHub user name" });
  const repo = h("input", { type: "text", class: "input", value: st.repo, autocapitalize: "off", autocorrect: "off", spellcheck: "false", placeholder: "focus-data" });
  const branch = h("input", { type: "text", class: "input", value: st.branch || "main", autocapitalize: "off", autocorrect: "off", spellcheck: "false" });
  const token = h("input", { type: "password", class: "input", value: st.token, autocomplete: "off", placeholder: "github_pat_..." });
  const result = h("p", { class: "hint", "aria-live": "polite" });
  const read = () => ({ owner: owner.value.trim(), repo: repo.value.trim(), branch: branch.value.trim() || "main", token: token.value.trim() });
  const field = (label, input) => h("label", { class: "field" }, h("span", { class: "field-label" }, label), input);
  out.append(card("GitHub sync",
    h("p", { class: "hint" }, "Your list is kept in a private GitHub repository. The token stays in this browser on this phone."),
    field("Owner", owner), field("Repository", repo), field("Branch", branch), field("Token", token),
    h("div", { class: "btn-row" },
      btn("Save", () => {
        const next = read();
        const changedRepo = next.owner !== st.owner || next.repo !== st.repo || next.branch !== st.branch;
        app.settings = next;
        save(KEY_SETTINGS, next);
        if (changedRepo) {
          try { localStorage.removeItem("focus.sync"); } catch { /* ignore */ }
          app.dayPlan = null;
          save(KEY_PLAN, null);
        }
        app.blocked = null; // a newer list kept on this phone (app.localBlock) stays blocked
        makeEngine();
        if (app.engine) app.engine.setDirty(true);
        result.textContent = connected() ? "Saved. Syncing now." : "Saved. Fill in owner, repository and token to sync.";
        runSync({ force: true }).then(() => refreshDayPlan());
      }, "primary"),
      btn("Test connection", async () => {
        const v = read();
        if (!v.owner || !v.repo || !v.token) { result.textContent = "Fill in owner, repository and token first."; return; }
        result.textContent = "Testing...";
        const store = new GitHubStore({ ...v, api: app.api || undefined });
        result.textContent = await store.check();
      }),
      btn("Forget token", () => {
        if (!window.confirm("Remove the token from this phone? The list stays on the phone.")) return;
        app.settings = { ...app.settings, token: "" };
        save(KEY_SETTINGS, app.settings);
        makeEngine();
        render();
      }, "quiet")),
    result,
    app.api ? h("p", { class: "hint" }, `Using the test server at ${app.api}`) : null));

  // Sync status
  out.append(card("Sync status",
    h("p", { "data-status": "", class: "status" }),
    h("div", { class: "btn-row" }, btn("Sync now", () => { app.blocked = null; runSync({ force: true }).then(() => refreshDayPlan()); }, "", { disabled: !connected() || null }))));

  // Working hours
  const inputs = {};
  const rows = WEEKDAY_KEYS.map((k, i) => {
    inputs[k] = h("input", { type: "text", class: "input", value: windowsText(doc.planner.windows[k]), inputmode: "text", autocapitalize: "off", autocorrect: "off", spellcheck: "false", "aria-label": `${DAY_NAMES[i]} hours` });
    return h("label", { class: "hours-row" }, h("span", { class: "hours-day" }, DAY_NAMES[i]), inputs[k]);
  });
  const hoursMsg = h("p", { class: "hint", "aria-live": "polite" });
  out.append(card("Working hours",
    h("p", { class: "hint" }, `The planner fills these hours. Write ranges like 09:00-12:00, 13:30-18:00. Leave a day empty for no work. Times are ${doc.planner.tz} time.`),
    ...rows,
    h("div", { class: "btn-row" }, btn("Save hours", () => {
      const text = Object.fromEntries(WEEKDAY_KEYS.map((k) => [k, inputs[k].value]));
      try {
        const probe = clone(app.doc);
        setWindows(probe, text);
      } catch (ex) {
        hoursMsg.textContent = ex.message;
        return;
      }
      commit((d) => setWindows(d, text));
      toast("Working hours saved. Re-plan to use them today.");
    }, "primary")),
    hoursMsg));

  // Folders
  const tree = folderTree(doc.folders, doc.plan.tasks);
  const folderList = tree.length ? h("ul", { class: "rows" }, tree.map(([path, depth]) => {
    const count = doc.plan.tasks.filter((t) => inFolder(t, path)).length;
    const li = h("li", { class: "row" });
    const showRow = () => li.replaceChildren(
      h("div", { class: "row-main static", style: { paddingLeft: `${12 + depth * 18}px` } },
        h("span", { class: "row-label" }, path.split("/").pop()),
        h("span", { class: "row-sub" }, `${count} task${count === 1 ? "" : "s"}`)),
      h("div", { class: "row-tools" },
        btn("Rename", () => showEdit(), "small"),
        btn("Delete", () => {
          if (!window.confirm(`Delete folder "${path}" and its subfolders? Their tasks stay on the list without a folder.`)) return;
          commit((d) => { deleteFolder(d, path); });
          if (app.ui.folder && (app.ui.folder === path || app.ui.folder.startsWith(`${path}/`))) { app.ui.folder = null; save(KEY_UI, app.ui); }
        }, "small danger")));
    const showEdit = () => {
      const parent = path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "";
      const input = h("input", { type: "text", class: "input", value: path.split("/").pop(), "aria-label": "New folder name" });
      li.replaceChildren(h("div", { class: "row-main static" }, input), h("div", { class: "row-tools" },
        btn("Save", () => {
          const nm = input.value.split("/").map((x) => x.trim()).filter(Boolean).join("/");
          if (!nm) return;
          const neu = parent ? `${parent}/${nm}` : nm;
          if (neu !== path) {
            commit((d) => { renameFolder(d, path, neu); });
            if (app.ui.folder === path) { app.ui.folder = neu; save(KEY_UI, app.ui); }
          } else showRow();
        }, "small primary"),
        btn("Cancel", () => showRow(), "small quiet")));
      input.focus();
    };
    showRow();
    return li;
  })) : h("p", { class: "empty" }, "No folders yet. Make one when you add or edit a task.");
  out.append(card("Folders", folderList));

  out.append(card("About", h("p", { class: "hint" }, `Focus Tool web app ${APP_VERSION}. Your list is also kept on this phone, so it works offline and syncs when you are back online.`)));
  return out;
}

// ---------------------------------------------------------------- start up

function start() {
  app.api = parseApiOverride(location.search);
  app.doc = loadDoc();
  app.lastSaved = app.doc;
  makeEngine();
  render();
  setInterval(tick, 1000);
  setInterval(() => {
    if (!document.hidden && app.engine) {
      runSync();
      if (app.ui.tab === "plan" || app.ui.tab === "now") refreshDayPlan();
    }
  }, POLL_MS);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) {
      render();
      if (app.engine) { runSync(); refreshDayPlan(); }
    }
  });
  window.addEventListener("online", () => { if (app.engine) runSync(); });
  window.addEventListener("offline", () => {
    app.sync = { ...app.sync, status: "offline" };
    renderStatus();
  });
  if (app.engine) {
    runSync().then(() => refreshDayPlan());
  }
  try {
    if ("serviceWorker" in navigator && (location.protocol === "https:" || location.hostname === "localhost" || location.hostname === "127.0.0.1")) {
      navigator.serviceWorker.register("./sw.js").catch(() => { /* offline shell is optional */ });
    }
  } catch { /* ignore */ }
}

if (typeof document !== "undefined") {
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
}

export { app as _app };
