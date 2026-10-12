// Pure logic for the phone web app. No DOM here, so Node tests can run it.
// This is a port of focus_core.py (the desktop's reference code). The shared files in
// tests/vectors/ are the spec for both languages; when in doubt, Python is right.
//
// Dates are plain "YYYY-MM-DD" strings. "Naive" times are local wall-clock times in the
// planner's time zone written without a zone, like Python's datetime.isoformat().

export const STATE_VERSION = 3;
export const EPOCH = "1970-01-01T00:00:00.000Z";
export const NS = "212d6585-c191-5e24-9304-4a619852c6c3"; // same fixed namespace as focus_core.NS
export const WEEKDAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
export const TOMBSTONE_DAYS = 60;
export const DEFAULT_CHECKPOINTS = [0.5, 0.9, 1.0];
const WORKDAY = ["09:00-12:00", "13:30-18:00"];
const WEEKEND = ["10:00-12:30"];
export const DEFAULT_PLANNER = Object.freeze({
  tz: "America/Chicago",
  buffer_business_days: 2,
  windows: Object.fromEntries(WEEKDAY_KEYS.map((k) => [k, k === "sat" || k === "sun" ? [...WEEKEND] : [...WORKDAY]])),
  updated_at: EPOCH,
});

const TASK_FIELDS = ["name", "estimate_min", "details", "spent_s", "deadline", "target", "folder", "repeat", "last_done"];
const TASK_DEFAULTS = { details: "", spent_s: 0, deadline: null, target: null, folder: null, repeat: null, last_done: null };
const DONE_FIELDS = ["name", "estimate_min", "actual_min", "finished_at", "details", "deadline", "target", "folder"];

export class VersionError extends Error {
  constructor(version) {
    super(version < STATE_VERSION
      ? "This list was saved by an older Focus Tool. Open the desktop app once so it can update the file."
      : "This list was saved by a newer Focus Tool. Reload this page to get the newest version.");
    this.version = version;
  }
}

// ---------------------------------------------------------------- small helpers

export function clone(x) {
  return x === undefined ? undefined : JSON.parse(JSON.stringify(x));
}

function sortKeysDeep(x) {
  if (Array.isArray(x)) return x.map(sortKeysDeep);
  if (x && typeof x === "object") {
    const out = {};
    for (const k of Object.keys(x).sort()) out[k] = sortKeysDeep(x[k]);
    return out;
  }
  return x;
}

/** Stable JSON text used to break ties. Same as focus_core.canonical: sorted keys, no spaces,
 * non-ASCII kept, and whole numbers written without ".0" (JavaScript does that by itself). */
export function canonical(x) {
  return JSON.stringify(sortKeysDeep(x));
}

/** Deep equality of JSON values, ignoring key order (like Python's == on dicts and lists). */
export function deepEqual(a, b) {
  if (a === b) return true;
  if (a === null || b === null || typeof a !== "object" || typeof b !== "object") return false;
  if (Array.isArray(a) !== Array.isArray(b)) return false;
  if (Array.isArray(a)) return a.length === b.length && a.every((v, i) => deepEqual(v, b[i]));
  const ka = Object.keys(a), kb = Object.keys(b);
  if (ka.length !== kb.length) return false;
  return ka.every((k) => Object.prototype.hasOwnProperty.call(b, k) && deepEqual(a[k], b[k]));
}

/** Python's round(): halves go to the even neighbour (round(2.5) == 2). */
export function roundHalfEven(x) {
  const f = Math.floor(x);
  const diff = x - f;
  if (diff > 0.5) return f + 1;
  if (diff < 0.5) return f;
  return f % 2 === 0 ? f : f + 1;
}

function cmpStr(a, b) {
  return a < b ? -1 : a > b ? 1 : 0;
}

function cmpTuple(a, b) {
  for (let i = 0; i < Math.min(a.length, b.length); i++) {
    const x = a[i], y = b[i];
    if (Array.isArray(x)) {
      const c = cmpTuple(x, y);
      if (c) return c;
    } else if (x < y) return -1;
    else if (x > y) return 1;
  }
  return a.length - b.length;
}

export function nowIso(d = new Date()) {
  return d.toISOString();
}

export function newId() {
  if (globalThis.crypto && typeof globalThis.crypto.randomUUID === "function") {
    return globalThis.crypto.randomUUID().replace(/-/g, "");
  }
  let s = "";
  for (let i = 0; i < 32; i++) s += Math.floor(Math.random() * 16).toString(16);
  return s;
}

// ---------------------------------------------------------------- uuid5 (SHA-1), sync and dependency free

function sha1(bytes) {
  const ml = bytes.length * 8;
  const withPad = new Uint8Array(((bytes.length + 9 + 63) >> 6) << 6);
  withPad.set(bytes);
  withPad[bytes.length] = 0x80;
  const dv = new DataView(withPad.buffer);
  dv.setUint32(withPad.length - 8, Math.floor(ml / 2 ** 32));
  dv.setUint32(withPad.length - 4, ml >>> 0);
  let h0 = 0x67452301, h1 = 0xefcdab89, h2 = 0x98badcfe, h3 = 0x10325476, h4 = 0xc3d2e1f0;
  const w = new Uint32Array(80);
  for (let off = 0; off < withPad.length; off += 64) {
    for (let i = 0; i < 16; i++) w[i] = dv.getUint32(off + i * 4);
    for (let i = 16; i < 80; i++) {
      const v = w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16];
      w[i] = (v << 1) | (v >>> 31);
    }
    let a = h0, b = h1, c = h2, d = h3, e = h4;
    for (let i = 0; i < 80; i++) {
      let f, k;
      if (i < 20) { f = (b & c) | (~b & d); k = 0x5a827999; }
      else if (i < 40) { f = b ^ c ^ d; k = 0x6ed9eba1; }
      else if (i < 60) { f = (b & c) | (b & d) | (c & d); k = 0x8f1bbcdc; }
      else { f = b ^ c ^ d; k = 0xca62c1d6; }
      const t = (((a << 5) | (a >>> 27)) + f + e + k + w[i]) >>> 0;
      e = d; d = c; c = (b << 30) | (b >>> 2); b = a; a = t;
    }
    h0 = (h0 + a) >>> 0; h1 = (h1 + b) >>> 0; h2 = (h2 + c) >>> 0; h3 = (h3 + d) >>> 0; h4 = (h4 + e) >>> 0;
  }
  const out = new Uint8Array(20);
  const odv = new DataView(out.buffer);
  [h0, h1, h2, h3, h4].forEach((h, i) => odv.setUint32(i * 4, h));
  return out;
}

/** Python's uuid.uuid5(namespace, name).hex */
export function uuid5Hex(namespace, name) {
  const ns = namespace.replace(/-/g, "");
  const nsBytes = new Uint8Array(16);
  for (let i = 0; i < 16; i++) nsBytes[i] = parseInt(ns.slice(i * 2, i * 2 + 2), 16);
  const nameBytes = new TextEncoder().encode(name);
  const all = new Uint8Array(16 + nameBytes.length);
  all.set(nsBytes);
  all.set(nameBytes, 16);
  const h = sha1(all).slice(0, 16);
  h[6] = (h[6] & 0x0f) | 0x50;
  h[8] = (h[8] & 0x3f) | 0x80;
  return Array.from(h, (b) => b.toString(16).padStart(2, "0")).join("");
}

// ---------------------------------------------------------------- dates (strings "YYYY-MM-DD")

function dayToUtc(day) {
  const [y, m, d] = day.split("-").map(Number);
  return Date.UTC(y, m - 1, d);
}

function utcToDay(ms) {
  return new Date(ms).toISOString().slice(0, 10);
}

export function addDays(day, n) {
  return utcToDay(dayToUtc(day) + n * 86400000);
}

export function daysBetween(a, b) {
  return Math.round((dayToUtc(b) - dayToUtc(a)) / 86400000);
}

/** 0 = Monday ... 6 = Sunday, like Python's date.weekday(). */
export function weekday(day) {
  return (new Date(dayToUtc(day)).getUTCDay() + 6) % 7;
}

export function weekdayKey(day) {
  return WEEKDAY_KEYS[weekday(day)];
}

/** `day` minus `bufferDays` working days (Mon to Fri): Fri -> Wed, Mon -> Thu, Sat or Sun -> Thu. */
export function finishBy(day, bufferDays = 2) {
  let left = Math.trunc(bufferDays);
  let d = day;
  while (left > 0) {
    d = addDays(d, -1);
    if (weekday(d) < 5) left -= 1;
  }
  return d;
}

// ---------------------------------------------------------------- local time in the planner's zone

/** Wall-clock parts of `date` in time zone `tz` (falls back to the device's zone). */
export function zonedParts(date = new Date(), tz = DEFAULT_PLANNER.tz) {
  let p = null;
  try {
    const fmt = new Intl.DateTimeFormat("en-US", {
      timeZone: tz, hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit",
    });
    p = {};
    for (const part of fmt.formatToParts(date)) p[part.type] = part.value;
  } catch {
    p = null;
  }
  const pad = (n, w = 2) => String(n).padStart(w, "0");
  const parts = p
    ? { y: +p.year, mo: +p.month, d: +p.day, h: +p.hour % 24, mi: +p.minute, s: +p.second }
    : { y: date.getFullYear(), mo: date.getMonth() + 1, d: date.getDate(), h: date.getHours(), mi: date.getMinutes(), s: date.getSeconds() };
  const ms = date.getMilliseconds();
  const day = `${parts.y}-${pad(parts.mo)}-${pad(parts.d)}`;
  return {
    day,
    hhmm: `${pad(parts.h)}:${pad(parts.mi)}`,
    // Python's datetime.isoformat() with microseconds; read back by datetime.fromisoformat on the desktop.
    naive: `${day}T${pad(parts.h)}:${pad(parts.mi)}:${pad(parts.s)}.${pad(ms * 1000, 6)}`,
    minutes: parts.h * 60 + parts.mi + (parts.s + ms / 1000) / 60,
  };
}

/** Milliseconds on a fake clock where a naive local time is read as if it were UTC. Differences
 * between two such values are wall-clock differences, the same as subtracting Python naive datetimes. */
export function naiveMs(text) {
  const m = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,6}))?)?/.exec(text || "");
  if (!m) return NaN;
  const frac = m[7] ? Number((m[7] + "000000").slice(0, 6)) / 1000 : 0;
  return Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +(m[6] || 0)) + frac;
}

// ---------------------------------------------------------------- the document

function defaultTimer() {
  return {
    task: null, task_id: null, estimate_min: 0, running: false, paused: false, started_at: null,
    banked_s: 0, fired: [], checkpoints: [...DEFAULT_CHECKPOINTS], updated_at: EPOCH,
  };
}

function fillTask(x) {
  const out = { id: x.id || newId(), name: x.name, estimate_min: x.estimate_min };
  for (const k of TASK_FIELDS.slice(2)) out[k] = x[k] === undefined ? clone(TASK_DEFAULTS[k]) : clone(x[k]);
  out.updated_at = x.updated_at || EPOCH;
  return out;
}

function fillDone(x) {
  const out = { id: x.id || newId() };
  for (const k of DONE_FIELDS) {
    out[k] = x[k] === undefined ? (k === "details" || k === "finished_at" ? "" : null) : clone(x[k]);
  }
  out.updated_at = x.updated_at || EPOCH;
  return out;
}

/** An empty version 3 document. */
export function emptyDoc() {
  return normalizeDoc({ version: STATE_VERSION });
}

/** A complete version 3 document with every missing field filled in (a new copy).
 * Unlike Python's migrate_doc, older versions are refused: the desktop updates them. */
export function normalizeDoc(d) {
  if (!d || typeof d !== "object") throw new Error("The list file is not a JSON object.");
  const version = d.version === undefined ? 1 : Number(d.version);
  if (d.version === null || typeof d.version === "boolean" || !Number.isFinite(version)) {
    throw new Error(`The list file has an unreadable version (${d.version}).`);
  }
  if (Math.trunc(version) !== STATE_VERSION) throw new VersionError(Math.trunc(version));
  const timer = Object.assign(defaultTimer(), clone(d.timer || {}));
  timer.updated_at = timer.updated_at || EPOCH;
  const planner = d.planner ? clone(d.planner) : clone(DEFAULT_PLANNER);
  if (planner.updated_at === undefined) planner.updated_at = EPOCH;
  return {
    version: STATE_VERSION,
    plan: { tasks: ((d.plan && d.plan.tasks) || []).map(fillTask) },
    order_updated_at: d.order_updated_at || EPOCH,
    folders: [...(d.folders || [])],
    folders_updated_at: d.folders_updated_at || EPOCH,
    current_index: Math.trunc(Number(d.current_index || 0)),
    timer,
    done: (d.done || []).map(fillDone),
    deleted: { ...(d.deleted || {}) },
    planner,
    geometry: d.geometry === undefined ? null : clone(d.geometry),
  };
}

// ---------------------------------------------------------------- merge and stamp (same rules as focus_core.py)

function stampOf(x, key) {
  return x[key] || EPOCH;
}

/** The one of two versions with the larger stamp; equal stamps: the larger canonical text. */
function newer(x, y, key = "updated_at") {
  return cmpTuple([stampOf(x, key), canonical(x)], [stampOf(y, key), canonical(y)]) >= 0 ? x : y;
}

function unionById(xs, ys) {
  const out = new Map();
  for (const item of [...xs, ...ys]) {
    const have = out.get(item.id);
    out.set(item.id, have === undefined ? item : newer(have, item));
  }
  return out;
}

function withParents(path) {
  const parts = path.split("/");
  return parts.map((_, i) => parts.slice(0, i + 1).join("/"));
}

function idleTimer(timer) {
  return { ...timer, running: false, paused: false, started_at: null, banked_s: 0, task: null, task_id: null };
}

/** Combine two copies of the list. mergeDocs(a, b) equals mergeDocs(b, a), and merging a result with
 * itself changes nothing. Deletions win over edits; otherwise the newer stamp wins per item. */
export function mergeDocs(a0, b0) {
  const a = normalizeDoc(a0), b = normalizeDoc(b0);
  const deleted = { ...a.deleted };
  for (const [k, v] of Object.entries(b.deleted)) {
    const have = deleted[k] === undefined ? v : deleted[k];
    deleted[k] = v > have ? v : have;
  }
  const doneById = unionById(a.done, b.done);
  const done = [...doneById.entries()].filter(([i]) => !(i in deleted)).map(([, d]) => d)
    .sort((x, y) => cmpTuple([x.finished_at, x.id], [y.finished_at, y.id]));
  const doneIds = new Set(done.map((d) => d.id));
  const tasksById = new Map([...unionById(a.plan.tasks, b.plan.tasks).entries()]
    .filter(([i]) => !(i in deleted) && !doneIds.has(i)));
  const aIds = a.plan.tasks.map((t) => t.id), bIds = b.plan.tasks.map((t) => t.id);
  const aWins = cmpTuple([a.order_updated_at, canonical(aIds)], [b.order_updated_at, canonical(bIds)]) >= 0;
  const [win, lose] = aWins ? [aIds, bIds] : [bIds, aIds];
  const winSet = new Set(win);
  const order = [...lose.filter((i) => tasksById.has(i) && !winSet.has(i)), ...win.filter((i) => tasksById.has(i))];
  const tasks = [...new Set(order)].map((i) => tasksById.get(i));
  let timer = clone(newer(a.timer, b.timer));
  if (timer.running && !tasksById.has(timer.task_id)) timer = idleTimer(timer);
  const planner = clone(newer(a.planner, b.planner));
  const fa = { folders: a.folders, folders_updated_at: a.folders_updated_at };
  const fb = { folders: b.folders, folders_updated_at: b.folders_updated_at };
  const folders = [...newer(fa, fb, "folders_updated_at").folders];
  const have = new Set(folders);
  const used = new Set();
  for (const t of tasks) if (t.folder) for (const p of withParents(t.folder)) if (!have.has(p)) used.add(p);
  folders.push(...[...used].sort());
  const ids = tasks.map((t) => t.id);
  const ci = timer.task_id != null ? ids.indexOf(timer.task_id) : -1;
  return {
    version: STATE_VERSION,
    plan: { tasks: clone(tasks) },
    order_updated_at: a.order_updated_at > b.order_updated_at ? a.order_updated_at : b.order_updated_at,
    folders,
    folders_updated_at: a.folders_updated_at > b.folders_updated_at ? a.folders_updated_at : b.folders_updated_at,
    current_index: ci >= 0 ? ci : 0,
    timer,
    done: clone(done),
    deleted,
    planner,
    geometry: null,
  };
}

function without(d, keys) {
  const out = {};
  for (const [k, v] of Object.entries(d)) if (!keys.includes(k)) out[k] = v;
  return out;
}

export function isoMinusDays(stamp, days) {
  return new Date(Date.parse(stamp) - days * 86400000).toISOString();
}

/** `cur` (a complete copy is returned) with change stamps set to `now` wherever it differs from
 * `prev`, the last version saved or synced. prev null: only fill in missing fields. */
export function stampChanges(prev, cur0, now) {
  const cur = normalizeDoc(cur0);
  if (prev == null) return cur;
  const p = normalizeDoc(prev);
  const prevTasks = new Map(p.plan.tasks.map((t) => [t.id, t]));
  const prevDone = new Map(p.done.map((d) => [d.id, d]));
  const curTaskIds = cur.plan.tasks.map((t) => t.id);
  const curDoneIds = new Set(cur.done.map((d) => d.id));
  for (const t of cur.plan.tasks) {
    const old = prevTasks.get(t.id);
    if (!old || TASK_FIELDS.some((k) => !deepEqual(t[k], old[k]))) t.updated_at = now;
  }
  for (const d of cur.done) {
    const old = prevDone.get(d.id);
    if (!old || DONE_FIELDS.some((k) => !deepEqual(d[k], old[k]))) d.updated_at = now;
  }
  const curSet = new Set(curTaskIds);
  const both = curTaskIds.filter((i) => prevTasks.has(i));
  const prevOrder = [...prevTasks.keys()].filter((i) => curSet.has(i));
  if (!deepEqual(both, prevOrder) || both.length !== curTaskIds.length) cur.order_updated_at = now;
  for (const i of [...prevTasks.keys(), ...prevDone.keys()]) {
    if (!curSet.has(i) && !curDoneIds.has(i)) cur.deleted[i] = now;
  }
  const ignored = ["fired", "checkpoints", "updated_at"];
  if (!deepEqual(without(cur.timer, ignored), without(p.timer, ignored))) cur.timer.updated_at = now;
  if (!deepEqual(without(cur.planner, ["updated_at"]), without(p.planner, ["updated_at"]))) cur.planner.updated_at = now;
  if (!deepEqual(cur.folders, p.folders)) cur.folders_updated_at = now;
  const cutoff = isoMinusDays(now, TOMBSTONE_DAYS);
  cur.deleted = Object.fromEntries(Object.entries(cur.deleted).filter(([, v]) => v >= cutoff));
  return cur;
}

// ---------------------------------------------------------------- dated and ranked sections

export function isDated(t) {
  return Boolean(t.deadline || t.target);
}

/** The earlier of a task's deadline and target, or null. */
export function taskDate(t) {
  const ds = [t.deadline, t.target].filter(Boolean);
  return ds.length ? ds.sort()[0] : null;
}

/** [dated, ranked]. Dated: earliest date first, a deadline before a target-only task on the same
 * date, then list order. Ranked: every other task (repeats too) in list order. */
export function displaySections(tasks) {
  const dated = tasks.map((t, i) => [t, i]).filter(([t]) => isDated(t))
    .map(([t, i]) => [[taskDate(t), t.deadline ? 0 : 1, i], t])
    .sort((x, y) => cmpTuple(x[0], y[0])).map((x) => x[1]);
  return [dated, tasks.filter((t) => !isDated(t))];
}

/** A new list where the ranked tasks follow `rankedIds`, in the slots ranked tasks used; dated tasks
 * keep their slots. Ranked tasks missing from rankedIds follow in their old order. */
export function reorderRanked(tasks, rankedIds) {
  const ranked = tasks.filter((t) => !isDated(t));
  const byId = new Map(ranked.map((t) => [t.id, t]));
  const picked = [...new Set(rankedIds)].filter((i) => byId.has(i)).map((i) => byId.get(i));
  const pickedIds = new Set(picked.map((t) => t.id));
  const queue = [...picked, ...ranked.filter((t) => !pickedIds.has(t.id))];
  let n = 0;
  return tasks.map((t) => (isDated(t) ? t : queue[n++]));
}

/** The block of today's plan to work on: the first block of a live task that ends after `nowNaive`
 * (the current block if now is inside it, else the next one). null if the plan is not for today. */
export function nextBlock(dayPlan, nowNaive, liveIds) {
  const live = liveIds instanceof Set ? liveIds : new Set(liveIds);
  if (!dayPlan || dayPlan.date !== nowNaive.slice(0, 10)) return null;
  const now = naiveMs(nowNaive);
  for (const b of dayPlan.blocks || []) {
    const end = naiveMs(`${dayPlan.date}T${b.end}:00`);
    if (live.has(b.task_id) && end > now) return b;
  }
  return null;
}

// ---------------------------------------------------------------- repeating tasks

function ruleMatches(rule, day) {
  const kind = rule.kind;
  if (kind === "daily") return true;
  if (kind === "weekdays") return weekday(day) < 5;
  if (kind === "days") return (rule.days || []).includes(weekdayKey(day));
  if (kind === "every") {
    const gap = daysBetween(rule.anchor, day);
    return gap >= 0 && gap % Math.max(1, Math.trunc(Number(rule.every || 1))) === 0;
  }
  return false;
}

/** A repeating task is active on days its rule matches, unless it was already done that day.
 * A task that does not repeat is always active. */
export function isActive(task, day) {
  if (!task.repeat) return true;
  return ruleMatches(task.repeat, day) && task.last_done !== day;
}

/** The first day after `after` on which a repeating task is active; null for other tasks. */
export function nextActiveDay(task, after) {
  if (!task.repeat) return null;
  const rule = task.repeat;
  if (rule.kind === "every") {
    const step = Math.max(1, Math.trunc(Number(rule.every || 1)));
    const gap = daysBetween(rule.anchor, addDays(after, 1));
    const d = gap <= 0 ? rule.anchor : addDays(rule.anchor, Math.ceil(gap / step) * step);
    return isActive(task, d) ? d : addDays(d, step);
  }
  for (let n = 1; n < 16; n++) {
    const d = addDays(after, n);
    if (isActive(task, d)) return d;
  }
  return null;
}

/** Done on a repeating task: the done entry for that day, and the task goes to sleep (mutates task).
 * `nowNaive` is local time; the entry's id is the same on every device for the same task and day. */
export function completeRepeat(task, nowNaive, actualMin) {
  const day = nowNaive.slice(0, 10);
  const rec = {
    id: uuid5Hex(NS, `rep:${task.id}#${day}`), name: task.name, estimate_min: task.estimate_min,
    actual_min: Math.trunc(actualMin), finished_at: `${day} ${nowNaive.slice(11, 16)}`, details: task.details || "",
    deadline: null, target: null, folder: task.folder ?? null, updated_at: EPOCH,
  };
  task.last_done = day;
  task.spent_s = 0;
  return rec;
}

/** The done entry for finishing `task` (like focus_core.finish_task). It keeps the task's id, so a merge
 * drops the task from the list even if another device edited it meanwhile. A repeating task goes through
 * completeRepeat instead, so it stays on the list, asleep until its next day (mutates task). */
export function finishTask(task, nowNaive, actualMin) {
  if (task.repeat) return completeRepeat(task, nowNaive, actualMin);
  return {
    id: task.id, name: task.name, estimate_min: task.estimate_min, actual_min: Math.trunc(actualMin),
    finished_at: `${nowNaive.slice(0, 10)} ${nowNaive.slice(11, 16)}`, details: task.details || "",
    deadline: task.deadline ?? null, target: task.target ?? null, folder: task.folder ?? null, updated_at: EPOCH,
  };
}

// ---------------------------------------------------------------- folders

function under(folder, path) {
  return folder != null && (folder === path || folder.startsWith(path + "/"));
}

/** True if `task` is in folder `path` or one of its subfolders. path null means all tasks. */
export function inFolder(task, path) {
  return path == null || under(task.folder, path);
}

/** Every known folder path with its depth, parents included, each folder followed by its subfolders. */
export function folderTree(folders, tasks) {
  const paths = new Set();
  for (const f of [...folders, ...tasks.map((t) => t.folder).filter(Boolean)]) for (const p of withParents(f)) paths.add(p);
  return [...paths].sort((x, y) => cmpTuple(x.split("/"), y.split("/")))
    .map((p) => [p, p.split("/").length - 1]);
}

function completeParents(paths) {
  const out = [...new Set(paths)];
  for (const p of [...out]) for (const parent of withParents(p).slice(0, -1)) if (!out.includes(parent)) out.push(parent);
  return out;
}

/** Rename folder `old` to `neu`, with its subfolders, in doc.folders and on tasks and done tasks (mutates doc). */
export function renameFolder(doc, old, neu) {
  const move = (p) => (under(p, old) ? neu + p.slice(old.length) : p);
  doc.folders = completeParents(doc.folders.map(move));
  for (const t of [...doc.plan.tasks, ...doc.done]) t.folder = move(t.folder ?? null);
}

/** Remove folder `path` and its subfolders; their tasks stay on the list without a folder (mutates doc). */
export function deleteFolder(doc, path) {
  doc.folders = doc.folders.filter((p) => !under(p, path));
  for (const t of doc.plan.tasks) if (under(t.folder, path)) t.folder = null;
}

/** Add a folder path (and its parents) to doc.folders (mutates doc). */
export function addFolder(doc, path) {
  doc.folders = completeParents([...doc.folders, path]);
}

// ---------------------------------------------------------------- timer math (like focus_core.TimerState)

/** Seconds on the timer at local time `nowNaive`. 0 when the timer is not running. */
export function timerElapsedS(timer, nowNaive) {
  if (!timer || !timer.running) return 0;
  let live = 0;
  if (timer.started_at && !timer.paused) {
    const ms = naiveMs(nowNaive) - naiveMs(timer.started_at);
    if (Number.isFinite(ms)) live = ms / 1000;
  }
  return roundHalfEven(Number(timer.banked_s || 0) + live);
}

export function timerView(timer, nowNaive) {
  const elapsed = timerElapsedS(timer, nowNaive);
  const est = Math.trunc(Number(timer.estimate_min || 0)) * 60;
  const remaining = Math.max(0, est - elapsed);
  const overtime = Math.max(0, elapsed - est);
  const fraction = est <= 0 ? 0 : Math.max(0, Math.min(1, remaining / est));
  return { elapsed, estimate: est, remaining, overtime, fraction };
}

function fractionElapsed(timer, nowNaive) {
  const v = timerView(timer, nowNaive);
  return v.estimate <= 0 ? 1 : v.elapsed / v.estimate;
}

/** Checkpoints newly crossed at nowNaive; adds them to timer.fired (mutates timer). */
export function timerTick(timer, nowNaive) {
  if (!timer.running) return [];
  const frac = fractionElapsed(timer, nowNaive);
  const fired = [];
  const have = new Set(timer.fired || []);
  for (const c of [...(timer.checkpoints || DEFAULT_CHECKPOINTS)].sort((x, y) => x - y)) {
    if (!have.has(c) && frac >= c) {
      have.add(c);
      fired.push(c);
    }
  }
  timer.fired = [...have].sort((x, y) => x - y);
  return fired;
}

export function timerStart(timer, task, estimateMin, nowNaive, spentS = 0, taskId = null) {
  Object.assign(timer, {
    task, task_id: taskId, estimate_min: Math.trunc(estimateMin), running: true, paused: false,
    started_at: nowNaive, banked_s: Number(spentS), fired: [],
  });
  if (spentS) timerTick(timer, nowNaive);
}

export function timerPause(timer, nowNaive) {
  if (timer.running && !timer.paused) {
    timer.banked_s = Number(timer.banked_s || 0) + (naiveMs(nowNaive) - naiveMs(timer.started_at)) / 1000;
    timer.started_at = null;
    timer.paused = true;
  }
}

export function timerResume(timer, nowNaive) {
  if (timer.running && timer.paused) {
    timer.started_at = nowNaive;
    timer.paused = false;
  }
}

export function timerAddMinutes(timer, minutes, nowNaive) {
  timer.estimate_min = Math.trunc(Number(timer.estimate_min || 0)) + Math.trunc(minutes);
  const frac = fractionElapsed(timer, nowNaive);
  timer.fired = (timer.fired || []).filter((c) => c <= frac);
}

/** Stop the timer like TimerState.skip(): task, task_id and banked_s stay, as in Python. */
export function timerSkip(timer) {
  timer.running = false;
  timer.paused = false;
  timer.started_at = null;
}
