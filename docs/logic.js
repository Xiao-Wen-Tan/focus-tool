// What the buttons of the phone app do to the list, kept apart from the page code so Node can test it.
// Each action changes the document it is given (the app passes a fresh copy, then stamps and saves it).
// The rules mirror the desktop panel in focus_tool.py: start, pause, Done, +5, Put back, reorder.
import {
  EPOCH, WEEKDAY_KEYS, newId, clone, zonedParts, displaySections, reorderRanked, isActive, isDated,
  nextActiveDay, nextDueDay, expiredRepeats, nextBlock, finishTask, timerElapsedS, timerView, timerTick, timerStart, timerPause,
  timerResume, timerAddMinutes, timerSkip, roundHalfEven, addFolder, weekday, taskDate, deepEqual,
  emptyDoc, normalizeDoc, VersionError, STATE_VERSION, parseHm, PRIORITIES, priorityRank, byPriority,
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

/** A deadline time for people to read: "17:00" -> "5:00pm" (like focus_core.fmt_hm); "" when not a time. */
export function fmtHm(s) {
  const m = parseHm(s);
  if (m == null) return "";
  const hh = Math.floor(m / 60), mm = m % 60;
  return `${hh % 12 || 12}:${String(mm).padStart(2, "0")}${hh < 12 ? "am" : "pm"}`;
}

/** How a task's dates show, like due_text in focus_tool.py: "📅 Due Fri 10/16 5:00pm" for a hard deadline,
 * "🎯 Aim Mon 10/19" for an aim-to-finish date, both when it has both. "" for no dates. */
export function dueText(t) {
  let due = "";
  if (t.deadline) {
    const hm = fmtHm(t.deadline_time);
    due = `📅 Due ${dateLabel(t.deadline)}${hm ? ` ${hm}` : ""}`;
  }
  if (t.target) return due ? `${due}, 🎯 aim ${dateLabel(t.target)}` : `🎯 Aim ${dateLabel(t.target)}`;
  return due;
}

/** The words for a repeating task: "📅 Due Wed 10/14 · 🔁 until Fri 10/30" (the next day it comes up, then the
 * last day it repeats), "📅 Due Wed 10/14 · 🔁" without a last day, or just "🔁" when no day is left.
 * Like repeat_text in focus_tool.py. */
export function repeatDueText(t, today) {
  const due = nextDueDay(t, today);
  const until = t.deadline ? `🔁 until ${dateLabel(t.deadline)}` : "🔁";
  return due ? `📅 Due ${dateLabel(due)} · ${until}` : until;
}

/** The full row text: timer mark, date prefix, then taskLabel. A repeating task shows the next day it comes
 * up as its due date, and its own deadline as the last day it repeats. */
export function rowLabel(doc, t, today = localNow(doc).day) {
  let pre = "";
  const tm = doc.timer;
  if (tm && tm.running && tm.task_id === t.id) pre += tm.paused ? "⏸ " : "▶ ";
  else if (t.spent_s) pre += "⏸ ";
  if (t.repeat) pre += `${repeatDueText(t, today)} · `;
  else if (isDated(t)) pre += `${dueText(t)} · `;
  return pre + taskLabel(t);
}

/** The words for a plan block's date: "📅 Due Fri 10/16 5:00pm" or "🎯 Aim Mon 10/19"; "" for none. */
export function blockDateText(b) {
  if (!b || !b.date) return "";
  if (b.date_kind === "deadline") {
    const hm = fmtHm(b.time);
    return `📅 Due ${dateLabel(b.date)}${hm ? ` ${hm}` : ""}`;
  }
  return b.date_kind === "target" ? `🎯 Aim ${dateLabel(b.date)}` : "";
}

/** A time typed by hand as "HH:MM", or null for empty text. The phone's time box already gives "HH:MM";
 * anything else that is not a real time also gives null. */
export function cleanTime(text) {
  const t = String(text || "").trim();
  return parseHm(t) == null ? null : t;
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

/** My order as the List tab shows it: High, then Medium, then Low, your order inside each tag. */
export function myOrder(tasks) {
  return byPriority(tasks);
}

/** Tasks in the order the List tab's By date view shows them: dated tasks (repeats by the next day they come
 * up) first, then the rest by tag and your order. */
export function displayOrder(doc, today) {
  const [dated, ranked] = displaySections(doc.plan.tasks, today);
  return [...dated, ...ranked];
}

/** {task, block}: the plan's block for now if its task is live, else the first active task in display order. */
export function upNext(doc, dayPlan, now) {
  const live = new Set(doc.plan.tasks.filter((t) => isActive(t, now.day)).map((t) => t.id));
  const b = nextBlock(dayPlan, now.naive, live);
  if (b) return { task: findTask(doc, b.task_id), block: b };
  const t = displayOrder(doc, now.day).find((x) => isActive(x, now.day));
  return { task: t || null, block: null };
}

// ---------------------------------------------------------------- actions (each mutates `doc`)

function blankTask() {
  return {
    id: newId(), name: "", estimate_min: DEFAULT_MINUTES, details: "", spent_s: 0, deadline: null, target: null,
    folder: null, repeat: null, last_done: null, updated_at: EPOCH, deadline_time: null, priority: "med",
  };
}

/** The tag's short name for a row: "High", "Med" or "Low". */
export function priorityLabel(t) {
  return { high: "High", low: "Low" }[t.priority] || "Med";
}

function cleanFields(f) {
  const out = {};
  if (f.name !== undefined) out.name = String(f.name).trim();
  if (f.estimate_min !== undefined) out.estimate_min = Math.max(1, Math.trunc(Number(f.estimate_min) || DEFAULT_MINUTES));
  if (f.details !== undefined) out.details = String(f.details);
  for (const k of ["deadline", "target", "folder"]) if (f[k] !== undefined) out[k] = f[k] || null;
  if (f.repeat !== undefined) out.repeat = f.repeat ? clone(f.repeat) : null;
  if (f.deadline_time !== undefined) out.deadline_time = cleanTime(f.deadline_time);
  if (f.priority !== undefined) out.priority = PRIORITIES.includes(f.priority) ? f.priority : "med";
  // A repeating task may keep a deadline, the last day it repeats (contract section 1). It never has a target.
  if (out.repeat) out.target = null;
  return out;
}

/** New task at the END of the list, so tasks entered earlier stay above it inside their tag. Returns it, or
 * null when the name is empty. */
export function addTask(doc, fields) {
  const f = cleanFields(fields);
  if (!f.name) return null;
  const t = Object.assign(blankTask(), f);
  if (!t.deadline || t.repeat) t.deadline_time = null; // the time belongs to a hard deadline only
  doc.plan.tasks.push(t);
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
  if (!t.deadline || t.repeat) t.deadline_time = null; // the time belongs to a hard deadline only
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

/** Remove every repeating task whose deadline has passed (it repeated until that day). Uses removeTask,
 * so the next save records each one as deleted and other devices drop it too. Returns the removed ids,
 * or false when there was nothing to remove (so the app does not save an unchanged list). */
export function removeExpiredRepeats(doc, today) {
  const ids = expiredRepeats(doc.plan.tasks, today).map((t) => t.id);
  for (const id of ids) removeTask(doc, id);
  return ids.length ? ids : false;
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

/** The ids in `ids` whose task has the same priority tag as task `id`: a task moves only inside its tag. */
function sameTag(doc, id, ids) {
  const byId = new Map(doc.plan.tasks.map((t) => [t.id, t]));
  const t = byId.get(id);
  return t ? ids.filter((i) => byId.has(i) && priorityRank(byId.get(i)) === priorityRank(t)) : [];
}

/** Move a ranked task one place up (-1) or down (+1) among `visibleIds` (the ranked tasks the
 * current folder filter shows) that share its tag. It swaps places with its visible neighbour. */
export function moveRanked(doc, id, delta, visibleIds = null) {
  const ids = rankedIds(doc);
  const shown = sameTag(doc, id, (visibleIds || ids).filter((i) => ids.includes(i)));
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

/** My order: move any task, dated or not, one place up (-1) or down (+1) among `visibleIds` (the tasks the
 * current folder filter shows) that share its tag. It swaps places with its visible neighbour; hidden tasks keep
 * their places. */
export function moveTask(doc, id, delta, visibleIds = null) {
  const tasks = doc.plan.tasks;
  const ids = tasks.map((t) => t.id);
  const shown = sameTag(doc, id, (visibleIds || ids).filter((i) => ids.includes(i)));
  const k = shown.indexOf(id);
  const j = k + delta;
  if (k < 0 || j < 0 || j >= shown.length) return false;
  const a = ids.indexOf(id), b = ids.indexOf(shown[j]);
  [tasks[a], tasks[b]] = [tasks[b], tasks[a]];
  return true;
}

export function moveTaskToTop(doc, id) {
  const k = doc.plan.tasks.findIndex((t) => t.id === id);
  if (k <= 0) return false;
  doc.plan.tasks.unshift(...doc.plan.tasks.splice(k, 1));
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

/** Whether automatic syncs stop after a sync result. GitHub rejecting the token ("auth") pauses them,
 * like the desktop does; any other result (from a button press, the only kind that runs while paused) lifts it. */
export function authPauseAfterSync(result) {
  return Boolean(result && result.status === "auth");
}

/** Saving Settings lifts the pause only when the token was changed. */
export function authPauseAfterSave(paused, oldToken, newToken) {
  return Boolean(paused) && String(oldToken || "").trim() === String(newToken || "").trim();
}

/** May a sync run now? While paused for the token, only a button press (`force`) syncs. */
export function mayAutoSync(authPaused, force) {
  return Boolean(force) || !authPaused;
}

/** The editor fields the user actually changed: compares the form with the values when the sheet opened,
 * so a change made on another device meanwhile (say a rename) is not undone by Save. */
export function changedFields(original, form) {
  const out = {};
  for (const k of ["name", "estimate_min", "details", "deadline", "target", "folder", "repeat", "deadline_time", "priority"]) {
    const a = original[k] === undefined ? null : original[k];
    const b = form[k] === undefined ? null : form[k];
    if (!deepEqual(a, b)) out[k] = b;
  }
  if (out.repeat) out.target = null; // choosing a repeat removes the "aim to finish by" date, not the deadline
  return out;
}

// ---------------------------------------------------------------- folder notes

/** The summary text of folder `path` ("" when it has none). */
export function folderNotesText(doc, path) {
  const n = (doc.folder_notes || {})[path];
  return n ? n.text : "";
}

/** Set the summary text of folder `path`. Returns false when nothing changed (so commit skips it). */
export function setFolderNotes(doc, path, text) {
  if (!path) return false;
  const notes = doc.folder_notes || (doc.folder_notes = {});
  const have = notes[path];
  if ((have ? have.text : "") === text) return false;
  if (have) have.text = text;
  else notes[path] = { text, updated_at: EPOCH };
  return true;
}

/** The progress notes of folder `path`, newest first. */
export function progressFor(doc, path) {
  return (doc.progress || []).filter((x) => x.folder === path)
    .sort((a, b) => (a.at === b.at ? (a.id < b.id ? 1 : -1) : a.at < b.at ? 1 : -1));
}

/** Add a progress note to folder `path`, dated `now` (localNow). Returns it, or null for empty text. */
export function addProgress(doc, path, text, now) {
  const t = String(text || "").trim();
  if (!t || !path) return null;
  const x = { id: newId(), folder: path, at: `${now.day} ${now.hhmm}`, text: t, updated_at: EPOCH };
  (doc.progress || (doc.progress = [])).push(x);
  return x;
}

/** Change a progress note's text. Returns false when there is nothing to change. */
export function editProgress(doc, id, text) {
  const x = (doc.progress || []).find((n) => n.id === id);
  const t = String(text || "").trim();
  if (!x || !t || t === x.text) return false;
  x.text = t;
  return true;
}

/** Delete a progress note (the save leaves a tombstone so other devices drop it too). */
export function deleteProgress(doc, id) {
  const before = (doc.progress || []).length;
  doc.progress = (doc.progress || []).filter((n) => n.id !== id);
  return doc.progress.length !== before;
}
