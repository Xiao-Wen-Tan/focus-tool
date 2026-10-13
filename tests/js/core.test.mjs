// Runs every shared vector in tests/vectors/ against docs/core.js (the Python runner is tests/test_vectors.py),
// plus a few checks of the parts that have no vectors (uuid5, timer math, finishTask, version refusal).
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import * as core from "../../docs/core.js";

const VEC = join(dirname(fileURLToPath(import.meta.url)), "..", "vectors");
const load = (p) => JSON.parse(readFileSync(p, "utf-8"));
const files = (sub) => readdirSync(join(VEC, sub)).filter((f) => f.endsWith(".json")).sort().map((f) => join(VEC, sub, f));

test("vector folders exist", () => {
  assert.ok(files("merge").length >= 14);
  assert.ok(files("stamp").length >= 8);
});

for (const p of files("merge")) {
  test(`merge vector ${p.split(/[\\/]/).pop()}`, () => {
    const v = load(p);
    assert.ok(v.description);
    const ab = core.mergeDocs(v.a, v.b);
    const ba = core.mergeDocs(v.b, v.a);
    assert.deepEqual(ab, v.expected);
    assert.deepEqual(ba, v.expected);
    assert.deepEqual(core.mergeDocs(ab, ab), ab);
    assert.equal(core.canonical(ab), core.canonical(v.expected));
  });
}

for (const p of files("stamp")) {
  test(`stamp vector ${p.split(/[\\/]/).pop()}`, () => {
    const v = load(p);
    assert.ok(v.description);
    const before = JSON.stringify(v.cur);
    assert.deepEqual(core.stampChanges(v.prev, v.cur, v.now), v.expected);
    assert.equal(JSON.stringify(v.cur), before, "input not mutated");
  });
}

const cases = (name, key = "cases") => load(join(VEC, name))[key];

for (const c of cases("sections.json")) {
  test(`sections: ${c.description}`, () => {
    const [dated, ranked] = core.displaySections(c.tasks);
    assert.deepEqual(dated.map((t) => t.id), c.expected.dated);
    assert.deepEqual(ranked.map((t) => t.id), c.expected.ranked);
  });
}

for (const c of cases("reorder.json")) {
  test(`reorder: ${c.description}`, () => {
    assert.deepEqual(core.reorderRanked(c.tasks, c.ranked_ids).map((t) => t.id), c.expected);
  });
}

for (const c of cases("finish_by.json")) {
  test(`finish_by: ${c.description}`, () => {
    assert.equal(core.finishBy(c.date, c.buffer_days), c.expected);
  });
}

for (const c of cases("next_block.json")) {
  test(`next_block: ${c.description}`, () => {
    assert.deepEqual(core.nextBlock(c.day_plan, c.now, new Set(c.live_ids)), c.expected);
  });
}

for (const c of cases("repeat.json")) {
  test(`repeat: ${c.description}`, () => {
    assert.equal(core.isActive(c.task, c.day), c.expected);
  });
}

for (const c of cases("repeat.json", "next_cases")) {
  test(`next_active_day: ${c.description}`, () => {
    assert.equal(core.nextActiveDay(c.task, c.after), c.expected);
  });
}

for (const c of cases("repeat.json", "expired_cases")) {
  test(`expired_repeats: ${c.description}`, () => {
    assert.deepEqual(core.expiredRepeats(c.tasks, c.today).map((t) => t.id), c.expected);
  });
}

for (const c of cases("folders.json")) {
  test(`in_folder: ${c.description}`, () => {
    assert.equal(core.inFolder(c.task, c.path), c.expected);
  });
}

for (const c of cases("folders.json", "tree_cases")) {
  test(`folder_tree: ${c.description}`, () => {
    assert.deepEqual(core.folderTree(c.folders, c.tasks), c.expected);
  });
}

// ---------------------------------------------------------------- parts without shared vectors

test("uuid5 matches Python's uuid.uuid5(NS, name).hex", () => {
  // Values printed by Python 3.10: uuid.uuid5(focus_core.NS, name).hex
  assert.equal(core.uuid5Hex(core.NS, "rep:abc123#2026-10-13"), "36f9ef0a84fa50cbadd10f0e940173ff");
  assert.equal(core.uuid5Hex(core.NS, "rep:té中#2026-01-01"), "975e4adecfa4593087bd2e4257cd6503");
});

test("canonical sorts keys, keeps non-ASCII, writes 1.0 as 1", () => {
  assert.equal(core.canonical({ b: 1.0, a: ["é", { d: null, c: 0.5 }] }), '{"a":["é",{"c":0.5,"d":null}],"b":1}');
});

test("roundHalfEven matches Python round", () => {
  assert.deepEqual([0.5, 1.5, 2.5, 3.5, 2.4, 2.6].map(core.roundHalfEven), [0, 2, 2, 4, 2, 3]);
});

test("normalizeDoc refuses version 2 and version 5, fills a v3 doc and gives it the version 4 fields", () => {
  assert.throws(() => core.normalizeDoc({ version: 2 }), core.VersionError);
  assert.throws(() => core.normalizeDoc({ version: 5 }), core.VersionError);
  assert.equal(core.normalizeDoc({ version: 4 }).version, 4);
  assert.throws(() => core.normalizeDoc({}), core.VersionError);
  assert.throws(() => core.normalizeDoc({ version: null }), /unreadable/);
  const d = core.normalizeDoc({ version: 3, plan: { tasks: [{ id: "a", name: "Alpha", estimate_min: 5 }] }, done: [{ id: "x", name: "Beta", estimate_min: 5, actual_min: 4 }] });
  assert.equal(d.version, 4);
  assert.equal(d.plan.tasks[0].deadline_time, null);
  assert.deepEqual([d.folder_notes, d.progress], [{}, []]);
  assert.equal(d.plan.tasks[0].spent_s, 0);
  assert.equal(d.plan.tasks[0].updated_at, core.EPOCH);
  assert.equal(d.done[0].finished_at, "");
  assert.equal(d.planner.tz, "America/Chicago");
  assert.deepEqual(d.planner.windows.sat, ["10:00-12:30"]);
  assert.throws(() => core.mergeDocs({ version: 5 }, core.emptyDoc()), core.VersionError);
});

test("finishTask keeps the task id; a repeat goes through completeRepeat", () => {
  const t = { id: "t1", name: "Alpha", estimate_min: 25, details: "d", spent_s: 0, deadline: "2026-10-16", target: null, folder: "P", repeat: null, last_done: null, deadline_time: "17:00" };
  const d = core.finishTask(t, "2026-10-13T09:05:03.123000", 20);
  assert.deepEqual(d, { id: "t1", name: "Alpha", estimate_min: 25, actual_min: 20, finished_at: "2026-10-13 09:05", details: "d", deadline: "2026-10-16", target: null, folder: "P", updated_at: core.EPOCH, deadline_time: "17:00" });
  const r = { id: "abc123", name: "Walk", estimate_min: 10, details: "", spent_s: 30, deadline: null, target: null, folder: null, repeat: { kind: "daily" }, last_done: null };
  const rd = core.finishTask(r, "2026-10-13T18:30:00.000000", 9);
  assert.equal(rd.id, "36f9ef0a84fa50cbadd10f0e940173ff");
  assert.equal(rd.finished_at, "2026-10-13 18:30");
  assert.equal(r.last_done, "2026-10-13");
  assert.equal(r.spent_s, 0);
  assert.equal(core.isActive(r, "2026-10-13"), false);
  assert.equal(core.nextActiveDay(r, "2026-10-13"), "2026-10-14");
});

test("nextActiveDay for every-N and chosen days", () => {
  const every = { repeat: { kind: "every", every: 3, anchor: "2026-10-12" }, last_done: null };
  assert.equal(core.nextActiveDay(every, "2026-10-12"), "2026-10-15");
  assert.equal(core.nextActiveDay(every, "2026-10-01"), "2026-10-12");
  const days = { repeat: { kind: "days", days: ["mon", "thu"] }, last_done: null };
  assert.equal(core.nextActiveDay(days, "2026-10-12"), "2026-10-15");
  assert.equal(core.nextActiveDay({ repeat: null }, "2026-10-12"), null);
});

test("timer math follows TimerState: start, pause, resume, add minutes, overtime", () => {
  const timer = core.emptyDoc().timer;
  core.timerStart(timer, "Alpha", 10, "2026-10-13T09:00:00.000000", 0, "t1");
  assert.equal(timer.started_at, "2026-10-13T09:00:00.000000");
  assert.equal(core.timerElapsedS(timer, "2026-10-13T09:04:00.000000"), 240);
  core.timerPause(timer, "2026-10-13T09:04:00.000000");
  assert.equal(timer.paused, true);
  assert.equal(timer.banked_s, 240);
  assert.equal(core.timerElapsedS(timer, "2026-10-13T10:00:00.000000"), 240);
  core.timerResume(timer, "2026-10-13T10:00:00.000000");
  let v = core.timerView(timer, "2026-10-13T10:07:00.000000");
  assert.equal(v.elapsed, 660);
  assert.equal(v.remaining, 0);
  assert.equal(v.overtime, 60);
  assert.deepEqual(core.timerTick(timer, "2026-10-13T10:07:00.000000"), [0.5, 0.9, 1.0]);
  core.timerAddMinutes(timer, 5, "2026-10-13T10:07:00.000000");
  assert.equal(timer.estimate_min, 15);
  assert.deepEqual(timer.fired, [0.5]);
  v = core.timerView(timer, "2026-10-13T10:07:00.000000");
  assert.equal(v.remaining, 240);
  assert.ok(Math.abs(v.fraction - 240 / 900) < 1e-9);
  core.timerSkip(timer);
  assert.equal(timer.running, false);
  assert.equal(core.timerElapsedS(timer, "2026-10-13T11:00:00.000000"), 0);
});

test("timer started with time already used skips checkpoints already passed", () => {
  const timer = core.emptyDoc().timer;
  core.timerStart(timer, "Alpha", 10, "2026-10-13T09:00:00.000000", 330, "t1");
  assert.deepEqual(timer.fired, [0.5]);
  assert.equal(core.timerElapsedS(timer, "2026-10-13T09:00:00.000000"), 330);
});

test("zonedParts gives Chicago wall time and a naive ISO string", () => {
  const p = core.zonedParts(new Date(Date.UTC(2026, 9, 13, 14, 5, 3, 123)), "America/Chicago");
  assert.equal(p.day, "2026-10-13");
  assert.equal(p.hhmm, "09:05");
  assert.equal(p.naive, "2026-10-13T09:05:03.123000");
  const w = core.zonedParts(new Date(Date.UTC(2026, 0, 13, 5, 0, 0)), "America/Chicago");
  assert.equal(w.day, "2026-01-12");
  assert.equal(w.hhmm, "23:00");
});

test("rename and delete folder rewrite tasks and the folder list", () => {
  const doc = core.normalizeDoc({ version: 3, folders: ["Puzzle", "Puzzle/Tiles", "Home"], plan: { tasks: [
    { id: "a", name: "Alpha", estimate_min: 5, folder: "Puzzle/Tiles" },
    { id: "b", name: "Beta", estimate_min: 5, folder: "Home" }] } });
  core.renameFolder(doc, "Puzzle", "Easel/Doodle");
  assert.deepEqual(doc.folders, ["Easel/Doodle", "Easel/Doodle/Tiles", "Home", "Easel"]);
  assert.equal(doc.plan.tasks[0].folder, "Easel/Doodle/Tiles");
  core.deleteFolder(doc, "Easel");
  assert.deepEqual(doc.folders, ["Home"]);
  assert.equal(doc.plan.tasks[0].folder, null);
  core.addFolder(doc, "Work/Ideas");
  assert.deepEqual(doc.folders, ["Home", "Work/Ideas", "Work"]);
});
