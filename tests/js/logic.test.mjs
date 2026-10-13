// docs/logic.js: what the phone's buttons do to the list. Made-up task names only.
import { test } from "node:test";
import assert from "node:assert/strict";
import * as core from "../../docs/core.js";
import * as L from "../../docs/logic.js";

const at = (naive) => ({ day: naive.slice(0, 10), naive, hhmm: naive.slice(11, 16) });
const task = (id, name, extra = {}) => ({ id, name, estimate_min: 10, ...extra });
const doc = (tasks, extra = {}) => core.normalizeDoc({ version: 3, plan: { tasks }, ...extra });
const names = (d) => d.plan.tasks.map((t) => t.name);

test("the page module links (all imports exist) and does nothing without a DOM", async () => {
  const m = await import("../../docs/app.js");
  assert.ok(m._app);
});

test("addTask puts the new task at the front; repeat keeps the deadline but clears the target; folder is registered", () => {
  const d = doc([task("a", "Alpha")]);
  const t = L.addTask(d, { name: "  Write intro ", estimate_min: 25, folder: "Puzzle/Tiles", deadline: "2026-10-16" });
  assert.deepEqual(names(d), ["Write intro", "Alpha"]);
  assert.equal(t.estimate_min, 25);
  assert.deepEqual(d.folders, ["Puzzle/Tiles", "Puzzle"]);
  const r = L.addTask(d, { name: "Walk", repeat: { kind: "daily" }, deadline: "2026-10-16", target: "2026-10-15" });
  assert.equal(r.deadline, "2026-10-16");
  assert.equal(r.target, null);
  const u = L.updateTask(d, "a", { repeat: { kind: "weekdays" }, deadline: "2026-11-02", target: "2026-10-30" });
  assert.equal(u.deadline, "2026-11-02");
  assert.equal(u.target, null);
  assert.equal(L.addTask(d, { name: "   " }), null);
});

test("start, switch, pause, +5 and Done follow the desktop rules", () => {
  const d = doc([task("a", "Alpha"), task("b", "Beta")]);
  assert.ok(L.startTask(d, "a", at("2026-10-13T09:00:00.000000")));
  assert.equal(d.timer.task_id, "a");
  // Starting another task pauses the first and keeps its time on the task.
  assert.ok(L.startTask(d, "b", at("2026-10-13T09:03:00.000000")));
  assert.equal(L.findTask(d, "a").spent_s, 180);
  assert.equal(d.timer.task_id, "b");
  assert.equal(L.rowLabel(d, L.findTask(d, "a")), "⏸ 10m · Alpha   (3 min used)");
  assert.equal(L.rowLabel(d, L.findTask(d, "b")), "▶ 10m · Beta");
  L.toggleRun(d, at("2026-10-13T09:05:00.000000"));
  assert.equal(d.timer.paused, true);
  assert.equal(L.rowLabel(d, L.findTask(d, "b")), "⏸ 10m · Beta");
  L.toggleRun(d, at("2026-10-13T09:10:00.000000"));
  L.addMinutes(d, 5, at("2026-10-13T09:10:00.000000"));
  assert.equal(L.findTask(d, "b").estimate_min, 15);
  // Back to Alpha: it continues from 3 minutes.
  L.startTask(d, "a", at("2026-10-13T09:11:00.000000"));
  assert.equal(d.timer.banked_s, 180);
  const rec = L.finishTaskNow(d, "a", at("2026-10-13T09:18:30.000000"));
  assert.equal(rec.id, "a");
  assert.equal(rec.actual_min, 10); // 180 s + 450 s = 630 s = 10.5 min, rounded half to even like Python
  assert.equal(rec.finished_at, "2026-10-13 09:18");
  assert.equal(d.timer.running, false);
  assert.deepEqual(names(d), ["Beta"]);
  assert.equal(d.done.length, 1);
});

test("Done on a task that is not on the timer logs its used time", () => {
  const d = doc([task("a", "Alpha", { spent_s: 150 })]);
  const rec = L.finishTaskNow(d, "a", at("2026-10-13T10:00:00.000000"));
  assert.equal(rec.actual_min, 2);
});

test("Done on a repeat logs that day's copy, keeps the task asleep, and refuses a second Done", () => {
  const d = doc([task("w", "Walk", { repeat: { kind: "weekdays" } })]);
  const rec = L.finishTaskNow(d, "w", at("2026-10-13T08:00:00.000000"));
  assert.equal(rec.id, core.uuid5Hex(core.NS, "rep:w#2026-10-13"));
  assert.deepEqual(names(d), ["Walk"]);
  assert.equal(L.findTask(d, "w").last_done, "2026-10-13");
  assert.equal(L.sleepText(L.findTask(d, "w"), "2026-10-13"), "Sleeping until Wed 10/14");
  assert.equal(L.finishTaskNow(d, "w", at("2026-10-13T09:00:00.000000")), null);
  assert.equal(L.startTask(d, "w", at("2026-10-13T09:00:00.000000")), false);
  assert.equal(L.startTask(d, "w", at("2026-10-14T09:00:00.000000")), true);
});

test("stamping after Done keeps the task out of the tombstones (done id = task id)", () => {
  const prev = doc([task("a", "Alpha"), task("b", "Beta")]);
  const cur = core.clone(prev);
  L.finishTaskNow(cur, "a", at("2026-10-13T10:00:00.000000"));
  const stamped = core.stampChanges(prev, cur, "2026-10-13T15:00:00.000Z");
  assert.deepEqual(stamped.deleted, {});
  assert.equal(stamped.done[0].updated_at, "2026-10-13T15:00:00.000Z");
});

test("put back goes to the front with a new id; clear empties the done list", () => {
  const d = doc([task("a", "Alpha")]);
  d.done.push({ id: "x", name: "Beta", estimate_min: 30, actual_min: 20, finished_at: "2026-10-12 15:02", details: "", deadline: "2026-10-20", target: null, folder: "Home", updated_at: core.EPOCH });
  const t = L.putBack(d, "x");
  assert.notEqual(t.id, "x");
  assert.deepEqual(names(d), ["Beta", "Alpha"]);
  assert.equal(t.deadline, "2026-10-20");
  assert.equal(d.done.length, 0);
  d.done.push({ id: "y" });
  L.clearDone(d);
  assert.deepEqual(d.done, []);
});

test("moving in Your order skips dated tasks and respects the folder filter", () => {
  const d = doc([
    task("a", "Alpha", { folder: "P" }), task("b", "Beta", { deadline: "2026-10-16" }),
    task("c", "Gamma"), task("e", "Epsilon", { folder: "P" }),
  ]);
  assert.ok(L.moveRanked(d, "c", -1));
  assert.deepEqual(names(d), ["Gamma", "Beta", "Alpha", "Epsilon"]);
  // With folder P shown, Epsilon's visible neighbour above is Alpha.
  assert.ok(L.moveRanked(d, "e", -1, ["a", "e"]));
  assert.deepEqual(names(d), ["Gamma", "Beta", "Epsilon", "Alpha"]);
  assert.equal(L.moveRanked(d, "c", -1), false);
  assert.ok(L.moveRankedToTop(d, "a"));
  assert.deepEqual(names(d), ["Alpha", "Beta", "Gamma", "Epsilon"]);
});

test("in My order every task moves, dated or not, among the tasks shown", () => {
  const d = doc([
    task("a", "Alpha", { folder: "P" }), task("b", "Beta", { deadline: "2026-10-16" }),
    task("c", "Gamma"), task("e", "Epsilon", { folder: "P" }),
  ]);
  assert.ok(L.moveTask(d, "c", -1));
  assert.deepEqual(names(d), ["Alpha", "Gamma", "Beta", "Epsilon"]);
  assert.ok(L.moveTask(d, "b", -1));  // a dated task moves too
  assert.deepEqual(names(d), ["Alpha", "Beta", "Gamma", "Epsilon"]);
  // With folder P shown, Epsilon's visible neighbour above is Alpha; the hidden tasks keep their places.
  assert.ok(L.moveTask(d, "e", -1, ["a", "e"]));
  assert.deepEqual(names(d), ["Epsilon", "Beta", "Gamma", "Alpha"]);
  assert.equal(L.moveTask(d, "e", -1), false);
  assert.ok(L.moveTaskToTop(d, "b"));
  assert.deepEqual(names(d), ["Beta", "Epsilon", "Gamma", "Alpha"]);
  assert.equal(L.moveTaskToTop(d, "b"), false);
});

test("removing the task on the timer stops the timer", () => {
  const d = doc([task("a", "Alpha")]);
  L.startTask(d, "a", at("2026-10-13T09:00:00.000000"));
  assert.ok(L.removeTask(d, "a"));
  assert.equal(d.timer.running, false);
  assert.deepEqual(d.plan.tasks, []);
});

test("updateTask renames the timer's task too", () => {
  const d = doc([task("a", "Alpha")]);
  L.startTask(d, "a", at("2026-10-13T09:00:00.000000"));
  L.updateTask(d, "a", { name: "Alpha two", estimate_min: 30 });
  assert.equal(d.timer.task, "Alpha two");
  assert.equal(d.timer.estimate_min, 30);
});

test("mascot follows FocusWidget._mascot", () => {
  const d = doc([]);
  assert.equal(L.mascot(d, "2026-10-13T09:00:00", false), "🥳");
  assert.equal(L.mascot(d, "2026-10-13T09:00:00", true), "🎉");
  d.plan.tasks.push(core.normalizeDoc({ version: 3, plan: { tasks: [task("a", "Alpha")] } }).plan.tasks[0]);
  assert.equal(L.mascot(d, "2026-10-13T09:00:00", false), "🌱");
  L.startTask(d, "a", at("2026-10-13T09:00:00.000000"));
  assert.equal(L.mascot(d, "2026-10-13T09:04:00.000000", false), "🙂");
  assert.equal(L.mascot(d, "2026-10-13T09:06:00.000000", false), "😌");
  assert.equal(L.mascot(d, "2026-10-13T09:09:30.000000", false), "👀");
  assert.equal(L.mascot(d, "2026-10-13T09:10:30.000000", false), "😅");
  L.toggleRun(d, at("2026-10-13T09:11:00.000000"));
  assert.equal(L.mascot(d, "2026-10-13T09:12:00.000000", false), "😴");
});

test("labels: minutes, mm:ss, dates", () => {
  assert.equal(L.mmss(754), "12:34");
  assert.equal(L.mins(600), "10 min");
  assert.equal(L.mins(100 * 60), "1h 40m");
  assert.equal(L.dateLabel("2026-10-16"), "Fri 10/16");
  assert.equal(L.rowLabel(doc([]), { name: "Alpha", estimate_min: 45, spent_s: 0, deadline: null, target: "2026-10-16", repeat: null }), "🎯 Aim Fri 10/16 · 45m · Alpha");
  assert.equal(L.rowLabel(doc([]), { name: "Grant", estimate_min: 30, spent_s: 0, deadline: "2026-10-16", deadline_time: "17:00", target: null, repeat: null }), "📅 Due Fri 10/16 5:00pm · 30m · Grant");
  assert.equal(L.rowLabel(doc([]), { name: "Both", estimate_min: 10, spent_s: 0, deadline: "2026-10-16", target: "2026-10-14", repeat: null }), "📅 Due Fri 10/16, 🎯 aim Wed 10/14 · 10m · Both");
  assert.equal(L.rowLabel(doc([]), { name: "Walk", estimate_min: 10, spent_s: 30, deadline: null, target: null, repeat: { kind: "daily" } }, "2026-10-13"), "⏸ 📅 Due Tue 10/13 · 🔁 · 10m · Walk   (under 1 min used)");
  assert.equal(L.repeatLabel({ kind: "days", days: ["mon", "thu"] }), "Mon, Thu");
  assert.equal(L.rowLabel(doc([]), { name: "Walk", estimate_min: 10, spent_s: 0, deadline: "2026-10-16", target: null, repeat: { kind: "daily" } }, "2026-10-13"), "📅 Due Tue 10/13 · 🔁 until Fri 10/16 · 10m · Walk");
  // Done for today: the next day it comes up is its due date. No day left: only the repeat mark.
  assert.equal(L.rowLabel(doc([]), { name: "Walk", estimate_min: 10, spent_s: 0, deadline: null, target: null, repeat: { kind: "daily" }, last_done: "2026-10-13" }, "2026-10-13"), "📅 Due Wed 10/14 · 🔁 · 10m · Walk");
  assert.equal(L.rowLabel(doc([]), { name: "Walk", estimate_min: 10, spent_s: 0, deadline: "2026-10-13", target: null, repeat: { kind: "daily" }, last_done: "2026-10-13" }, "2026-10-13"), "🔁 until Tue 10/13 · 10m · Walk");
});

test("removeExpiredRepeats drops repeats past their deadline, and the save records them as deleted", () => {
  const d = doc([
    task("old", "Old walk", { repeat: { kind: "daily" }, deadline: "2026-10-12" }),
    task("today", "Walk today", { repeat: { kind: "daily" }, deadline: "2026-10-13" }),
    task("plain", "Plain", { deadline: "2026-10-01" }),
    task("bad", "Bad date", { repeat: { kind: "daily" }, deadline: "2026-02-30" }),
  ]);
  const prev = core.clone(d);
  assert.deepEqual(L.removeExpiredRepeats(d, "2026-10-13"), ["old"]);
  assert.deepEqual(names(d), ["Walk today", "Plain", "Bad date"]);
  assert.equal(L.removeExpiredRepeats(d, "2026-10-13"), false);
  const saved = core.stampChanges(prev, d, "2026-10-13T12:00:00.000Z");
  assert.equal(saved.deleted.old, "2026-10-13T12:00:00.000Z");
});

test("an expired repeat on the timer is removed and the timer stops", () => {
  const d = doc([task("w", "Walk", { repeat: { kind: "daily" }, deadline: "2026-10-12" })]);
  assert.ok(L.startTask(d, "w", at("2026-10-12T09:00:00.000000")));
  L.removeExpiredRepeats(d, "2026-10-13");
  assert.equal(d.timer.running, false);
  assert.deepEqual(names(d), []);
});

test("upNext prefers the plan's block for now, else the first active task in display order", () => {
  const d = doc([task("a", "Alpha"), task("w", "Walk", { repeat: { kind: "daily" }, last_done: "2026-10-13" }), task("b", "Beta", { deadline: "2026-10-20" })]);
  const n = at("2026-10-13T09:10:00.000000");
  assert.equal(L.upNext(d, null, n).task.name, "Beta");
  const plan = { date: "2026-10-13", blocks: [{ task_id: "w", start: "09:00", end: "09:30" }, { task_id: "a", start: "09:30", end: "10:00" }] };
  const r = L.upNext(d, plan, n);
  assert.equal(r.task.name, "Alpha"); // Walk is asleep today, so its block is skipped
  assert.equal(r.block.start, "09:30");
});

test("working hours: parse, refuse bad text, write planner.windows", () => {
  assert.deepEqual(L.parseWindows("9:00-12:00, 13:30-18:00"), ["09:00-12:00", "13:30-18:00"]);
  assert.deepEqual(L.parseWindows(""), []);
  assert.throws(() => L.parseWindows("12:00-09:00"), /end is not after/);
  assert.throws(() => L.parseWindows("noon"), /not a time range/);
  assert.throws(() => L.parseWindows("13:00-14:00, 09:00-10:00"), /in order/);
  const d = doc([]);
  const text = Object.fromEntries(core.WEEKDAY_KEYS.map((k) => [k, L.windowsText(d.planner.windows[k])]));
  text.sat = "";
  L.setWindows(d, text);
  assert.deepEqual(d.planner.windows.sat, []);
  assert.deepEqual(d.planner.windows.mon, ["09:00-12:00", "13:30-18:00"]);
  const stamped = core.stampChanges(doc([]), d, "2026-10-13T15:00:00.000Z");
  assert.equal(stamped.planner.updated_at, "2026-10-13T15:00:00.000Z");
  assert.throws(() => L.setWindows(d, { ...text, tue: "x" }), /^Error: On Tue, "x" is not a time range/);
});

test("timeline segments follow the plan day's working windows", () => {
  const plan = { date: "2026-10-13", blocks: [
    { task_id: "a", name: "R1", start: "09:00", end: "09:05", minutes: 5, kind: "ranked" },
    { task_id: "d", name: "D", start: "13:30", end: "18:00", minutes: 270, kind: "dated" }] };
  const segs = L.timelineSegments(plan, core.DEFAULT_PLANNER);
  assert.deepEqual(segs.map((g) => [g.start, g.end, g.blocks.length]), [[540, 720, 1], [810, 1080, 1]]);
  assert.equal(segs[1].blocks[0].top, 0);
  assert.equal(segs[1].blocks[0].len, 270);
  assert.equal(L.stampToLocalHm("2026-10-13T00:07:00.000Z", "America/Chicago"), "19:07");
});

// ---------------------------------------------------------------- fix round 1

test("?api= accepts only a local test server", () => {
  assert.equal(L.parseApiOverride("?api=http://127.0.0.1:8765"), "http://127.0.0.1:8765");
  assert.equal(L.parseApiOverride("?api=http://localhost:8765/"), "http://localhost:8765");
  for (const bad of [
    "?api=https://evil.example", "?api=http://127.0.0.1", "?api=http://127.0.0.1:8765/steal",
    "?api=http://127.0.0.1.evil.example:80", "?api=http://user@127.0.0.1:80", "?api=https://127.0.0.1:8765",
    "?api=http://localhost:0", "?api=http://localhost:70000", "?api=javascript:alert(1)", "", "?x=1",
  ]) assert.equal(L.parseApiOverride(bad), null, bad);
});

test("a newer list kept on the phone is blocked and backed up, never replaced", () => {
  const text = JSON.stringify({ version: 5, plan: { tasks: [{ id: "a", name: "Alpha" }] } });
  const r = L.readLocalDoc(text);
  assert.match(r.block, /newer Focus Tool/);
  assert.deepEqual(r.backup, { key: "focus.doc.backup-newer", text });
  assert.deepEqual(r.doc.plan.tasks, []);
  const ok = L.readLocalDoc(JSON.stringify(doc([task("a", "Alpha")])));
  assert.equal(ok.block, null);
  assert.equal(ok.backup, null);
  assert.deepEqual(names(ok.doc), ["Alpha"]);
  const broken = L.readLocalDoc("{not json");
  assert.equal(broken.block, null);
  assert.equal(broken.backup.key, "focus.doc.backup-unreadable");
  assert.equal(L.readLocalDoc(null).block, null);
});

test("Re-plan now asks GitHub only after a sync that worked", () => {
  assert.equal(L.replanBlockMessage({ status: "ok" }), "");
  assert.match(L.replanBlockMessage({ status: "offline" }), /offline/);
  assert.match(L.replanBlockMessage({ status: "auth" }), /token/);
  assert.match(L.replanBlockMessage({ status: "rate_limited" }), /wait/);
  assert.match(L.replanBlockMessage({ status: "error", message: "boom" }), /could not be saved/);
  assert.match(L.replanBlockMessage(null), /could not be saved/);
});

test("after rate_limited the next automatic sync waits retryAfterS", () => {
  assert.equal(L.syncPauseUntil({ status: "rate_limited", retryAfterS: 90 }, 1000), 91000);
  assert.equal(L.syncPauseUntil({ status: "rate_limited" }, 1000), 61000);
  assert.equal(L.syncPauseUntil({ status: "ok" }, 1000), 0);
});

test("editor Save sends only the fields the user changed", () => {
  const opened = { name: "Alpha", estimate_min: 25, details: "", deadline: null, target: null, folder: null, repeat: null };
  assert.deepEqual(L.changedFields(opened, { ...opened }), {});
  assert.deepEqual(L.changedFields(opened, { ...opened, estimate_min: 45 }), { estimate_min: 45 });
  assert.deepEqual(L.changedFields(opened, { ...opened, repeat: { kind: "daily" } }), { repeat: { kind: "daily" }, target: null });
  // Choosing a repeat keeps a deadline the user set; it becomes the last day the task repeats.
  assert.deepEqual(L.changedFields(opened, { ...opened, repeat: { kind: "daily" }, deadline: "2026-10-30" }),
    { deadline: "2026-10-30", repeat: { kind: "daily" }, target: null });
  // A rename made on another device while the sheet was open survives a Save that changed only minutes.
  const d = doc([task("a", "Alpha", { estimate_min: 25 })]);
  L.findTask(d, "a").name = "Alpha renamed on desktop";
  L.updateTask(d, "a", L.changedFields(opened, { ...opened, estimate_min: 45 }));
  assert.equal(L.findTask(d, "a").name, "Alpha renamed on desktop");
  assert.equal(L.findTask(d, "a").estimate_min, 45);
});

test("after GitHub rejects the token, automatic syncs pause until the token is changed", () => {
  // A sync result decides the pause: "auth" pauses, anything else (a button press that worked) lifts it.
  assert.equal(L.authPauseAfterSync({ status: "auth" }), true);
  assert.equal(L.authPauseAfterSync({ status: "ok" }), false);
  assert.equal(L.authPauseAfterSync({ status: "offline" }), false);
  // While paused, only a button press (force) syncs.
  assert.equal(L.mayAutoSync(true, false), false);
  assert.equal(L.mayAutoSync(true, true), true);
  assert.equal(L.mayAutoSync(false, false), true);
  // Saving Settings lifts the pause only when the token changed.
  assert.equal(L.authPauseAfterSave(true, "tok-1", "tok-1"), true);
  assert.equal(L.authPauseAfterSave(true, "tok-1", " tok-1 "), true);
  assert.equal(L.authPauseAfterSave(true, "tok-1", "tok-2"), false);
  assert.equal(L.authPauseAfterSave(false, "tok-1", "tok-1"), false);
});

test("deadline time: shown as 5:00pm, kept only on a hard deadline of a task that does not repeat", () => {
  assert.deepEqual(["00:05", "09:30", "12:00", "17:00", "bad", null].map(L.fmtHm), ["12:05am", "9:30am", "12:00pm", "5:00pm", "", ""]);
  const d = doc([task("a", "Alpha")]);
  const t = L.addTask(d, { name: "Grant", deadline: "2026-10-16", deadline_time: "17:00" });
  assert.equal(t.deadline_time, "17:00");
  assert.equal(L.addTask(d, { name: "Paper", target: "2026-10-16", deadline_time: "17:00" }).deadline_time, null);
  assert.equal(L.addTask(d, { name: "Odd", deadline: "2026-10-16", deadline_time: "5 pm" }).deadline_time, null);
  L.updateTask(d, t.id, { deadline: null });
  assert.equal(t.deadline_time, null); // no deadline, no time
  L.updateTask(d, t.id, { deadline: "2026-10-16", deadline_time: "09:00" });
  L.updateTask(d, t.id, { repeat: { kind: "daily" } });
  assert.equal(t.deadline, "2026-10-16");
  assert.equal(t.deadline_time, null); // a repeating task's deadline is only the last day it repeats
  assert.deepEqual(L.changedFields({ deadline: "2026-10-16", deadline_time: null }, { deadline: "2026-10-16", deadline_time: "17:00" }), { deadline_time: "17:00" });
});

test("plan blocks name their date: due with a time, or aim", () => {
  assert.equal(L.blockDateText({ date: "2026-10-16", date_kind: "deadline", time: "17:00" }), "📅 Due Fri 10/16 5:00pm");
  assert.equal(L.blockDateText({ date: "2026-10-16", date_kind: "deadline", time: null }), "📅 Due Fri 10/16");
  assert.equal(L.blockDateText({ date: "2026-10-19", date_kind: "target" }), "🎯 Aim Mon 10/19");
  assert.equal(L.blockDateText({ date: null, date_kind: null }), "");
});

test("folder notes: summary, and progress notes added, edited, deleted; the save stamps and tombstones them", () => {
  const d = doc([task("a", "Alpha", { folder: "Grant" })], { folders: ["Grant"] });
  assert.equal(L.folderNotesText(d, "Grant"), "");
  assert.equal(L.setFolderNotes(d, "Grant", "Waiting on letters"), true);
  assert.equal(L.setFolderNotes(d, "Grant", "Waiting on letters"), false); // no change, nothing to save
  const n1 = L.addProgress(d, "Grant", " Budget sent ", at("2026-10-12T09:00:00.000000"));
  const n2 = L.addProgress(d, "Grant", "Letters asked for", at("2026-10-13T10:30:00.000000"));
  assert.equal(L.addProgress(d, "Grant", "  ", at("2026-10-13T10:30:00.000000")), null);
  assert.equal(n1.text, "Budget sent");
  assert.deepEqual(L.progressFor(d, "Grant").map((x) => x.at), ["2026-10-13 10:30", "2026-10-12 09:00"]); // newest first
  const saved = core.stampChanges(doc([]), d, "2026-10-13T15:00:00.000Z");
  assert.equal(saved.folder_notes.Grant.updated_at, "2026-10-13T15:00:00.000Z");
  assert.ok(saved.progress.every((x) => x.updated_at === "2026-10-13T15:00:00.000Z"));
  const next = core.clone(saved);
  assert.equal(L.editProgress(next, n1.id, "Budget sent to Sam"), true);
  assert.equal(L.deleteProgress(next, n2.id), true);
  const again = core.stampChanges(saved, next, "2026-10-13T16:00:00.000Z");
  assert.deepEqual(again.progress.map((x) => [x.text, x.updated_at]), [["Budget sent to Sam", "2026-10-13T16:00:00.000Z"]]);
  assert.equal(again.deleted[n2.id], "2026-10-13T16:00:00.000Z");
});

test("renaming a folder moves its notes; deleting it keeps them", () => {
  const d = doc([], { folders: ["Grant", "Grant/Budget"], folder_notes: { Grant: { text: "Summary", updated_at: core.EPOCH } },
    progress: [{ id: "n1", folder: "Grant/Budget", at: "2026-10-12 09:00", text: "Sent", updated_at: core.EPOCH }] });
  core.renameFolder(d, "Grant", "NSF grant");
  assert.equal(d.folder_notes["NSF grant"].text, "Summary");
  assert.equal(d.folder_notes.Grant.text, "");
  assert.equal(d.progress[0].folder, "NSF grant/Budget");
  core.deleteFolder(d, "NSF grant");
  assert.equal(d.folder_notes["NSF grant"].text, "Summary");
});
