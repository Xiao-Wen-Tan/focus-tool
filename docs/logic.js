// What the buttons of the phone app do to the list, kept apart from the page code so Node can test it.
// Each action changes the document it is given (the app passes a fresh copy, then stamps and saves it).
// The rules mirror the desktop panel in focus_tool.py: start, pause, Done, +5, Put back, reorder.
import {
  EPOCH, WEEKDAY_KEYS, newId, clone, zonedParts, displaySections, reorderRanked, isActive, isDated,
  nextActiveDay, nextBlock, finishTask, timerElapsedS, timerView, timerTick, timerStart, timerPause,
  timerResume, timerAddMinutes, timerSkip, roundHalfEven, addFolder, weekday, taskDate, deepEqual,
  emptyDoc, normalizeDoc, VersionError, STATE_VERSION,
} from "./core.js";

export const TIME_PRESETS = [5, 10, 15, 25, 30, 45, 60, 90];
export const DEFAULT_MINUTES = 45; // settings.default.json default_block_minutes
export const ADD_MINUTES_STEP = 5; // settings.default.json add_minutes_step
export const CELEBRATE_MS = 3000;
const DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

// ---------------------------------------------------------------- time

/** Local time in the list's time zone (planner.tz): {day, hhmm, naive, minutes}. */
export function localNow(doc, date = new Date()) {
  return zonedParts(date, (doc && doc.planner && doc.planner.tz) || "America/Chicago");
}

export function findTask(doc, id) {
  return doc.plan.tasks.find((t) => t.id === id) || null;
}

/** The task on the timer, if the timer is running and the task is still on the list. */
export function timerTask(doc) {
  const tm = doc.timer;
  if (!tm || !tm.running) return null;
  return (tm.task_id && findTask(doc, tm.task_id)) || null;
}

// ---------------------------------------------------------------- labels

export function mmss(seconds) {
  const s = Math.max(0, Math.trunc(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

export function mins(seconds) {
  const m = Math.floor(Math.trunc(seconds) / 60);
  return m < 90 ? `${m} min` : `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`;
}

/** "Fri 10/16" */
export function dateLabel(day) {
  if (!day) return "";
  const [, m, d] = day.split("-").map(Number);
  return `${DAY_NAMES[weekday(day)]} ${m}/${d}`;
}

/** "15m · name", plus the time already used, like task_label in focus_tool.py. */
export function taskLabel(t) {
  let used = "";
  if (t.spent_s) used = `   (${t.spent_s < 60 ? "under 1 min" : mins(t.spent_s)} used)`;
  return `${t.estimate_min}m · ${t.name}${used}`;
}

/** The full row text: timer mark, repeat mark, date prefix, then taskLabel. */
export function rowLabel(doc, t) {
  let pre = "";
  const tm = doc.timer;
  if (tm && tm.running && tm.task_id === t.id) pre += tm.paused ? "⏸ " : "▶ ";
  else if (t.spent_s) pre += "⏸ ";
  if (t.repeat) pre += "🔁 ";
  if (isDated(t)) pre += `📅 ${dateLabel(taskDate(t))} · `;
  return pre + taskLabel(t);
}

export function repeatLabel(rule) {
  if (!rule) return "Never";
  if (rule.kind === "daily") return "Every day";
  if (rule.kind === "weekdays") return "Weekdays";
  if (rule.kind === "days") {
    const names = WEEKDAY_KEYS.map((k, i) => (rule.days || []).includes(k) ? DAY_NAMES[i] : null).filter(Boolean);
    return names.length ? names.join(", ") : "Chosen days";
  }
  if (rule.kind === "every") return `Every ${rule.every} days`;
  return "Repeats";
}

/** A short "asleep until" text for a repeating task that is not active today, else "". */
export function sleepText(t, today) {
  if (!t.repeat || isActive(t, today)) return "";
  const next = nextActiveDay(t, today);
  return next ? `Sleeping until ${dateLabel(next)}` : "Sleeping";
}

/** The emoji in the middle of the disk, with the same rules as FocusWidget._mascot. */
export function mascot(doc, nowNaive, celebrating) {
  if (celebrating) return "🎉";
  const tm = doc.timer;
  if (!tm || !tm.running) return doc.plan.tasks.length ? "🌱" : "🥳";
  if (tm.paused) return "😴";
  const v = timerView(tm, nowNaive);
  if (v.overtime > 0) return "😅";
  return v.fraction > 0.5 ? "🙂" : v.fraction > 0.1 ? "😌" : "👀";
}

// ---------------------------------------------------------------- what to do next

/** Tasks in the order the List tab shows them: "By date" first, then "Your order". */
export function displayOrder(doc) {
  const [dated, ranked] = displaySections(doc.plan.tasks);
  return [...dated, ...ranked];
}

/** {task, block}: the plan's block for now if its task is live, else the first active task in display order. */
export function upNext(doc, dayPlan, now) {
  const live = new Set(doc.plan.tasks.filter((t) => isActive(t, now.day)).map((t) => t.id));
  const b = nextBlock(dayPlan, now.naive, live);
  if (b) return { task: findTask(doc, b.task_id), block: b };
  const t = displayOrder(doc).find((x) => isActive(x, now.day));
  return { task: t || null, block: null };
}

// ---------------------------------------------------------------- actions (each mutates `doc`)

function blankTask() {
  return {
    id: newId(), name: "", estimate_min: DEFAULT_MINUTES, details: "", spent_s: 0, deadline: null, target: null,
    folder: null, repeat: null, last_done: null, updated_at: EPOCH,
  };
}

function cleanFields(f) {
  const out = {};
  if (f.name !== undefined) out.name = String(f.name).trim();
  if (f.estimate_min !== undefined) out.estimate_min = Math.max(1, Math.trunc(Number(f.estimate_min) || DEFAULT_MINUTES));
  if (f.details !== undefined) out.details = String(f.details);
  for (const k of ["deadline", "target", "folder"]) if (f[k] !== undefined) out[k] = f[k] || null;
  if (f.repeat !== undefined) out.repeat = f.repeat ? clone(f.repeat) : null;
  if (out.repeat) {
    out.deadline = null; // a repeating task has no dates (contract section 1)
    out.target = null;
  }
  return out;
}

/** New task at the FRONT of the list. Returns it, or null when the name is empty. */
export function addTask(doc, fields) {
  const f = cleanFields(fields);
  if (!f.name) return null;
  const t = Object.assign(blankTask(), f);
  doc.plan.tasks.unshift(t);
  if (t.folder && !doc.folders.includes(t.folder)) addFolder(doc, t.folder);
  return t;
}

export function updateTask(doc, id, fields) {
  const t = findTask(doc, id);
  if (!t) return null;
  const f = cleanFields(fields);
  if (f.name === "") delete f.name;
  if (f.repeat && t.repeat && t.repeat.kind !== f.repeat.kind) t.last_done = null;
  Object.assign(t, f);
  if (t.folder && !doc.folders.includes(t.folder)) addFolder(doc, t.folder);
  const tm = doc.timer;
  if (tm.running && tm.task_id === t.id) {
    tm.task = t.name;
    tm.estimate_min = t.estimate_min;
  }
  return t;
}

/** Put a task on the timer. A task already on it is paused and keeps its time. Returns false for a
 * task that cannot start (missing, or a repeat asleep today). */
export function startTask(doc, id, now) {
  const t = findTask(doc, id);
  if (!t || !isActive(t, now.day)) return false;
  const tm = doc.timer;
  if (tm.running && tm.task_id === t.id) {
    if (tm.paused) timerResume(tm, now.naive);
    return true;
  }
  const cur = timerTask(doc);
  if (tm.running && cur) {
    timerPause(tm, now.naive);
    cur.spent_s = timerElapsedS(tm, now.naive);
  }
  timerStart(tm, t.name, t.estimate_min, now.naive, t.spent_s || 0, t.id);
  t.spent_s = 0;
  return true;
}

/** Pause if running, resume if paused. */
export function toggleRun(doc, now) {
  const tm = doc.timer;
  if (!tm.running) return false;
  if (tm.paused) timerResume(tm, now.naive);
  else timerPause(tm, now.naive);
  return true;
}

export function addMinutes(doc, minutes, now) {
  const tm = doc.timer;
  if (!tm.running) return false;
  timerAddMinutes(tm, minutes, now.naive);
  const t = timerTask(doc);
  if (t) t.estimate_min = tm.estimate_min;
  return true;
}

/** Finish a task: log it in the done list (same id as the task, so other devices drop it too).
 * A repeating task stays on the list and sleeps until its next day. Returns the done entry. */
export function finishTaskNow(doc, id, now) {
  const t = findTask(doc, id);
  if (!t) return null;
  if (t.repeat && !isActive(t, now.day)) return null;
  const tm = doc.timer;
  let actual;
  if (tm.running && tm.task_id === t.id) {
    timerTick(tm, now.naive);
    actual = roundHalfEven(timerElapsedS(tm, now.naive) / 60);
    timerSkip(tm);
  } else {
    actual = roundHalfEven((t.spent_s || 0) / 60);
  }
  const rec = finishTask(t, now.naive, actual);
  doc.done = doc.done.filter((d) => d.id !== rec.id);
  doc.done.push(rec);
  if (!t.repeat) doc.plan.tasks = doc.plan.tasks.filter((x) => x.id !== t.id);
  return rec;
}

/** Remove a task without finishing it. */
export function removeTask(doc, id) {
  const tm = doc.timer;
  if (tm.running && tm.task_id === id) timerSkip(tm);
  const before = doc.plan.tasks.length;
  doc.plan.tasks = doc.plan.tasks.filter((t) => t.id !== id);
  return doc.plan.tasks.length !== before;
}

/** Put a finished task back at the FRONT of the list, with a new id. */
export function putBack(doc, doneId) {
  const d = doc.done.find((x) => x.id === doneId);
  if (!d) return null;
  const t = Object.assign(blankTask(), {
    name: d.name, estimate_min: d.estimate_min, details: d.details || "",
    deadline: d.deadline || null, target: d.target || null, folder: d.folder || null,
  });
  doc.plan.tasks.unshift(t);
  doc.done = doc.done.filter((x) => x.id !== doneId);
  return t;
}

export function clearDone(doc) {
  doc.done = [];
}

function rankedIds(doc) {
  return doc.plan.tasks.filter((t) => !isDated(t)).map((t) => t.id);
}

/** Move a ranked task one place up (-1) or down (+1) among `visibleIds` (the ranked tasks the
 * current folder filter shows). It swaps places with its visible neighbour. */
export function moveRanked(doc, id, delta, visibleIds = null) {
  const ids = rankedIds(doc);
  const shown = (visibleIds || ids).filter((i) => ids.includes(i));
  const k = shown.indexOf(id);
  const j = k + delta;
  if (k < 0 || j < 0 || j >= shown.length) return false;
  const a = ids.indexOf(id), b = ids.indexOf(shown[j]);
  [ids[a], ids[b]] = [ids[b], ids[a]];
  doc.plan.tasks = reorderRanked(doc.plan.tasks, ids);
  return true;
}

export function moveRankedToTop(doc, id) {
  const ids = rankedIds(doc);
  if (!ids.includes(id) || ids[0] === id) return false;
  doc.plan.tasks = reorderRanked(doc.plan.tasks, [id, ...ids.filter((i) => i !== id)]);
  return true;
}

// ---------------------------------------------------------------- working hours

const RANGE_RE = /^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$/;

/** "09:00-12:00, 13:30-18:00" -> ["09:00-12:00", "13:30-18:00"]. Empty text means no work that day.
 * Throws an Error with a plain sentence if the text cannot be read. */
export function parseWindows(text) {
  const parts = String(text || "").split(/[,;]/).map((s) => s.trim()).filter(Boolean);
  const out = [];
  let last = -1;
  for (const p of parts) {
    const m = RANGE_RE.exec(p);
    if (!m) throw new Error(`"${p}" is not a time range. Write it like 09:00-12:00.`);
    const [h1, m1, h2, m2] = m.slice(1).map(Number);
    if (h1 > 23 || h2 > 24 || m1 > 59 || m2 > 59 || (h2 === 24 && m2 > 0)) throw new Error(`"${p}" has a time that does not exist.`);
    const s = h1 * 60 + m1, e = h2 * 60 + m2;
    if (e <= s) throw new Error(`In "${p}" the end is not after the start.`);
    if (s < last) throw new Error("Put the time ranges in order, without overlaps.");
    last = e;
    const pad = (n) => String(n).padStart(2, "0");
    out.push(`${pad(h1)}:${pad(m1)}-${pad(h2)}:${pad(m2)}`);
  }
  return out;
}

export function windowsText(list) {
  return (list || []).join(", ");
}

/** Replace the planner's working hours ({mon: "09:00-12:00, ...", ...}). Throws on bad text. */
export function setWindows(doc, textByKey) {
  const windows = {};
  for (const k of WEEKDAY_KEYS) {
    try {
      windows[k] = parseWindows(textByKey[k]);
    } catch (ex) {
      throw new Error(`On ${DAY_NAMES[WEEKDAY_KEYS.indexOf(k)]}, ${ex.message.charAt(0).toLowerCase()}${ex.message.slice(1)}`);
    }
  }
  doc.planner.windows = windows;
}

// ---------------------------------------------------------------- the plan timeline

export function hmToMin(hm) {
  const [h, m] = String(hm).split(":").map(Number);
  return h * 60 + m;
}

export function minToHm(n) {
  return `${String(Math.floor(n / 60)).padStart(2, "0")}:${String(n % 60).padStart(2, "0")}`;
}

/** Sections of the day to draw: the working windows of the plan's weekday, widened to hold any block
 * that falls outside them. [{start, end, blocks: [{...block, top, len}]}], minutes from midnight. */
export function timelineSegments(dayPlan, planner) {
  if (!dayPlan || !dayPlan.date) return [];
  const key = WEEKDAY_KEYS[weekday(dayPlan.date)];
  const ranges = [];
  for (const w of ((planner && planner.windows) || {})[key] || []) {
    const [a, b] = w.split("-");
    ranges.push([hmToMin(a), hmToMin(b)]);
  }
  for (const b of dayPlan.blocks || []) ranges.push([hmToMin(b.start), hmToMin(b.end)]);
  ranges.sort((x, y) => x[0] - y[0] || x[1] - y[1]);
  const segs = [];
  for (const [s, e] of ranges) {
    const last = segs[segs.length - 1];
    if (last && s <= last.end) last.end = Math.max(last.end, e);
    else segs.push({ start: s, end: e, blocks: [] });
  }
  for (const b of dayPlan.blocks || []) {
    const s = hmToMin(b.start), e = hmToMin(b.end);
    const seg = segs.find((g) => s >= g.start && e <= g.end);
    if (seg) seg.blocks.push({ ...b, top: s - seg.start, len: e - s });
  }
  return segs;
}

/** "19:07" for a generated_at stamp, in the list's time zone. */
export function stampToLocalHm(stamp, tz) {
  const ms = Date.parse(stamp || "");
  if (!Number.isFinite(ms)) return "";
  return zonedParts(new Date(ms), tz).hhmm;
}

// ---------------------------------------------------------------- safety helpers for the page

/** The ?api= test-server override from a URL query string. Only a local server is accepted
 * (http://127.0.0.1:<port> or http://localhost:<port>), so a crafted link cannot send the token elsewhere.
 * Returns the origin, or null. */
export function parseApiOverride(search) {
  let v;
  try {
    v = new URLSearchParams(search || "").get("api");
  } catch {
    return null;
  }
  if (!v) return null;
  const m = /^http:\/\/(127\.0\.0\.1|localhost):(\d{1,5})\/?$/.exec(v.trim());
  if (!m) return null;
  const port = Number(m[2]);
  if (port < 1 || port > 65535) return null;
  return `http://${m[1]}:${port}`;
}

/** Read the list kept on this phone (the raw text of localStorage "focus.doc").
 * Returns {doc, block, backup}: `doc` to show; `block` a message when the saved list must not be changed
 * (it came from a newer app); `backup` = {key, text} to store before anything can replace the saved text. */
export function readLocalDoc(text) {
  if (!text) return { doc: emptyDoc(), block: null, backup: null };
  let raw;
  try {
    raw = JSON.parse(text);
  } catch {
    return { doc: emptyDoc(), block: null, backup: { key: "focus.doc.backup-unreadable", text } };
  }
  try {
    return { doc: normalizeDoc(raw), block: null, backup: null };
  } catch (ex) {
    if (ex instanceof VersionError && ex.version > STATE_VERSION) {
      return {
        doc: emptyDoc(),
        block: "The list on this phone was saved by a newer Focus Tool. Reload this page to update the app. Nothing will be changed until then.",
        backup: { key: "focus.doc.backup-newer", text },
      };
    }
    return { doc: emptyDoc(), block: null, backup: { key: "focus.doc.backup-unreadable", text } };
  }
}

/** Why Re-plan now should not ask GitHub to plan, given the result of the sync just before it.
 * "" means the sync worked and the plan can be requested. */
export function replanBlockMessage(result) {
  if (result && result.status === "ok") return "";
  const st = result ? result.status : "error";
  if (st === "offline") return "You are offline, so the plan cannot be made now. Try again when you are back online.";
  if (st === "auth") return "GitHub did not accept the token. Check it in Settings, then try again.";
  if (st === "rate_limited") return "GitHub asked us to wait for a while. Try re-planning later.";
  if (st === "version") return (result && result.message) || "The list on GitHub was saved by a different app version.";
  if (st === "none") return "Connect GitHub in Settings first.";
  return "Your list could not be saved to GitHub, so the plan was not requested. Try again in a moment.";
}

/** When the next automatic sync may run after a sync result (milliseconds since 1970). */
export function syncPauseUntil(result, nowMs) {
  if (result && result.status === "rate_limited") return nowMs + Math.max(1, Number(result.retryAfterS) || 60) * 1000;
  return 0;
}

/** The editor fields the user actually changed: compares the form with the values when the sheet opened,
 * so a change made on another device meanwhile (say a rename) is not undone by Save. */
export function changedFields(original, form) {
  const out = {};
  for (const k of ["name", "estimate_min", "details", "deadline", "target", "folder", "repeat"]) {
    const a = original[k] === undefined ? null : original[k];
    const b = form[k] === undefined ? null : form[k];
    if (!deepEqual(a, b)) out[k] = b;
  }
  if (out.repeat) {
    out.deadline = null; // choosing a repeat removes the dates
    out.target = null;
  }
  return out;
}
