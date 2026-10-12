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

test("addTask puts the new task at the front; repeat clears dates; folder is registered", () => {
  const d = doc([task("a", "Alpha")]);
  const t = L.addTask(d, { name: "  Write intro ", estimate_min: 25, folder: "Paper/Theory", deadline: "2026-10-16" });
  assert.deepEqual(names(d), ["Write intro", "Alpha"]);
  assert.equal(t.estimate_min, 25);
  assert.deepEqual(d.folders, ["Paper/Theory", "Paper"]);
  const r = L.addTask(d, { name: "Walk", repeat: { kind: "daily" }, deadline: "2026-10-16", target: "2026-10-15" });
  assert.equal(r.deadline, null);
  assert.equal(r.target, null);
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
  assert.equal(L.rowLabel(doc([]), { name: "Alpha", estimate_min: 45, spent_s: 0, deadline: null, target: "2026-10-16", repeat: null }), "📅 Fri 10/16 · 45m · Alpha");
  assert.equal(L.rowLabel(doc([]), { name: "Walk", estimate_min: 10, spent_s: 30, deadline: null, target: null, repeat: { kind: "daily" } }), "⏸ 🔁 10m · Walk   (under 1 min used)");
  assert.equal(L.repeatLabel({ kind: "days", days: ["mon", "thu"] }), "Mon, Thu");
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
