// docs/store.js against a mocked fetch (an in-memory GitHub "contents" API). No network.
import { test } from "node:test";
import assert from "node:assert/strict";
import * as core from "../../docs/core.js";
import {
  GitHubStore, SyncEngine, encodeBase64Utf8, decodeBase64Utf8, docText, fetchDayPlan,
} from "../../docs/store.js";

class MemoryStorage {
  constructor() { this.m = new Map(); }
  getItem(k) { return this.m.has(k) ? this.m.get(k) : null; }
  setItem(k, v) { this.m.set(k, String(v)); }
  removeItem(k) { this.m.delete(k); }
}

function response(status, body = null, headers = {}) {
  const h = new Map(Object.entries(headers).map(([k, v]) => [k.toLowerCase(), String(v)]));
  return {
    status,
    ok: status >= 200 && status < 300,
    headers: { get: (k) => (h.has(k.toLowerCase()) ? h.get(k.toLowerCase()) : null) },
    json: async () => (body == null ? Promise.reject(new Error("no body")) : body),
  };
}

/** A fake of the parts of the GitHub API the app uses. `beforePut` lets a test change the file
 * between our GET and our PUT, like another device saving at the same moment. */
function fakeGitHub({ token = "tok", files = {} } = {}) {
  const repo = { files: new Map(), n: 0, calls: [], beforePut: null, offline: false, dispatched: [] };
  const put = (path, text) => {
    repo.n += 1;
    const sha = `sha${repo.n}`;
    repo.files.set(path, { text, sha });
    return sha;
  };
  for (const [p, t] of Object.entries(files)) put(p, t);
  repo.put = put;
  repo.fetch = async (url, init = {}) => {
    const headers = init.headers || {};
    repo.calls.push({ url, method: init.method, headers, body: init.body, cache: init.cache });
    if (repo.offline) throw new TypeError("Failed to fetch");
    if (headers.Authorization !== `Bearer ${token}`) return response(401, { message: "Bad credentials" });
    const u = new URL(url);
    const m = /^\/repos\/([^/]+)\/([^/]+)(\/contents\/(.+)|\/actions\/workflows\/([^/]+)\/dispatches)?$/.exec(u.pathname);
    if (!m) return response(404, { message: "Not Found" });
    if (!m[3]) return response(200, { full_name: `${m[1]}/${m[2]}`, private: true });
    if (m[5]) {
      repo.dispatched.push({ workflow: m[5], body: JSON.parse(init.body) });
      return response(204);
    }
    const path = decodeURIComponent(m[4]);
    const f = repo.files.get(path);
    if (init.method === "GET") {
      if (!f) return response(404, { message: "Not Found" });
      const etag = `"${f.sha}"`;
      if (headers["If-None-Match"] === etag) return response(304, null, { ETag: etag });
      // GitHub wraps base64 at 60 characters; the decoder has to cope with the newlines.
      const b64 = encodeBase64Utf8(f.text).replace(/(.{60})/g, "$1\n");
      return response(200, { sha: f.sha, content: b64, encoding: "base64" }, { ETag: etag });
    }
    if (init.method === "PUT") {
      if (repo.beforePut) {
        const fn = repo.beforePut;
        repo.beforePut = null;
        fn();
      }
      const body = JSON.parse(init.body);
      const cur = repo.files.get(path);
      if ((cur && body.sha !== cur.sha) || (!cur && body.sha)) return response(409, { message: "is at x but expected y" });
      const sha = put(path, decodeBase64Utf8(body.content));
      return response(200, { content: { sha } });
    }
    return response(405, { message: "no" });
  };
  return repo;
}

const task = (id, name, extra = {}) => ({ id, name, estimate_min: 10, ...extra });
const doc = (tasks, extra = {}) => core.normalizeDoc({ version: 3, plan: { tasks }, ...extra });
const STAMP1 = "2026-10-12T10:00:00.000Z";
const STAMP2 = "2026-10-12T11:00:00.000Z";

function setup(opts = {}) {
  const gh = fakeGitHub({ files: opts.files });
  const store = new GitHubStore({ owner: "me", repo: "focus-data", token: opts.token ?? "tok", fetchImpl: gh.fetch, api: "https://api.example.test" });
  const storage = new MemoryStorage();
  const sleeps = [];
  const engine = new SyncEngine(store, storage, { sleep: async (ms) => { sleeps.push(ms); } });
  return { gh, store, storage, engine, sleeps };
}

test("base64 round trip keeps UTF-8 text (accents, CJK, emoji)", () => {
  const s = "Café 中文 🔁 Write intro";
  assert.equal(decodeBase64Utf8(encodeBase64Utf8(s)), s);
  assert.equal(decodeBase64Utf8(encodeBase64Utf8(s).replace(/(.{4})/g, "$1\n")), s);
  const big = "é".repeat(100000);
  assert.equal(decodeBase64Utf8(encodeBase64Utf8(big)), big);
});

test("requests send the GitHub headers and no-store, and use ?ref=branch", async () => {
  const { gh, store } = setup({ files: { "state.json": docText(doc([])) } });
  await store.getFile("state.json");
  const c = gh.calls[0];
  assert.equal(c.cache, "no-store");
  assert.equal(c.headers.Accept, "application/vnd.github+json");
  assert.equal(c.headers["X-GitHub-Api-Version"], "2022-11-28");
  assert.match(c.url, /\/repos\/me\/focus-data\/contents\/state\.json\?ref=main$/);
});

test("getFile: ETag then 304 not modified, 404 gives text null", async () => {
  const { store } = setup({ files: { "state.json": "{\"a\":1}" } });
  const r1 = await store.getFile("state.json");
  assert.equal(r1.text, "{\"a\":1}");
  assert.equal(r1.etag, "\"sha1\"");
  const r2 = await store.getFile("state.json", r1.etag);
  assert.equal(r2.notModified, true);
  const r3 = await store.getFile("missing.json");
  assert.equal(r3.text, null);
  assert.equal(r3.notModified, false);
});

test("putFile sends the sha and branch; a stale sha is a Conflict", async () => {
  const { gh, store } = setup({ files: { "state.json": "{}" } });
  const sha = await store.putFile("state.json", "{\"b\":2}", "sha1", "msg");
  assert.equal(sha, "sha2");
  const body = JSON.parse(gh.calls.at(-1).body);
  assert.equal(body.sha, "sha1");
  assert.equal(body.branch, "main");
  assert.equal(body.message, "msg");
  await assert.rejects(store.putFile("state.json", "{}", "sha1", "msg"), (e) => e.constructor.name === "Conflict");
});

test("check, auth error and dispatch", async () => {
  const ok = setup();
  assert.match(await ok.store.check(), /Connected to me\/focus-data/);
  await ok.store.dispatch("replan.yml", "main", { mode: "now" });
  assert.deepEqual(ok.gh.dispatched, [{ workflow: "replan.yml", body: { ref: "main", inputs: { mode: "now" } } }]);
  const bad = setup({ token: "wrong" });
  assert.match(await bad.store.check(), /did not accept the token/);
  const r = await bad.engine.sync(doc([task("a", "Alpha")]));
  assert.equal(r.status, "auth");
});

test("rate limit gives status rate_limited with a wait time", async () => {
  const { store, engine } = setup();
  store.fetch = async () => response(403, { message: "API rate limit exceeded" }, { "x-ratelimit-remaining": "0", "x-ratelimit-reset": String(Math.floor(Date.now() / 1000) + 120) });
  const r = await engine.sync(doc([]));
  assert.equal(r.status, "rate_limited");
  assert.ok(r.retryAfterS > 100 && r.retryAfterS <= 121);
});

test("sync: first upload when the file does not exist yet", async () => {
  const { gh, engine, storage } = setup();
  const local = doc([task("a", "Alpha", { updated_at: STAMP1 })], { order_updated_at: STAMP1 });
  const r = await engine.sync(local);
  assert.equal(r.status, "ok");
  assert.equal(r.pushed, true);
  const saved = JSON.parse(gh.files.get("state.json").text);
  assert.deepEqual(saved, r.doc);
  assert.equal(gh.files.get("state.json").text, JSON.stringify(r.doc, null, 2));
  assert.equal(JSON.parse(storage.getItem("focus.sync")).sha, "sha1");
});

test("sync: no change means a 304 and no PUT", async () => {
  const { gh, engine } = setup();
  const local = doc([task("a", "Alpha", { updated_at: STAMP1 })], { order_updated_at: STAMP1 });
  const r1 = await engine.sync(local);
  const r2 = await engine.sync(r1.doc); // etag is cleared after our own PUT, so this one is a full GET
  assert.equal(r2.pushed, false);
  const n = gh.calls.length;
  const r3 = await engine.sync(r2.doc);
  assert.equal(r3.status, "ok");
  assert.equal(r3.pushed, false);
  assert.equal(r3.remoteChanged, false);
  assert.equal(gh.calls.length, n + 1);
  assert.equal(gh.calls.at(-1).headers["If-None-Match"], "\"sha1\"");
});

test("sync: remote edits merge in, and the merge is pushed when it differs", async () => {
  const remote = doc([task("b", "Beta", { updated_at: STAMP1 })], { order_updated_at: STAMP1 });
  const { gh, engine } = setup({ files: { "state.json": docText(remote) } });
  const local = doc([task("a", "Alpha", { updated_at: STAMP2 })], { order_updated_at: STAMP2 });
  const r = await engine.sync(local);
  assert.equal(r.status, "ok");
  assert.equal(r.remoteChanged, true);
  assert.equal(r.pushed, true);
  assert.deepEqual(r.doc.plan.tasks.map((t) => t.name), ["Beta", "Alpha"]);
  assert.deepEqual(JSON.parse(gh.files.get("state.json").text), r.doc);
});

test("sync: 409 conflict refetches, merges again and retries with backoff", async () => {
  const start = doc([task("a", "Alpha", { updated_at: STAMP1 })], { order_updated_at: STAMP1 });
  const { gh, engine, sleeps } = setup({ files: { "state.json": docText(start) } });
  const local = core.clone(start);
  local.plan.tasks[0].name = "Alpha edited";
  local.plan.tasks[0].updated_at = STAMP2;
  // Another device adds "Gamma" between our GET and our PUT.
  gh.beforePut = () => {
    const other = core.clone(start);
    other.plan.tasks.unshift(core.normalizeDoc({ version: 3, plan: { tasks: [task("g", "Gamma", { updated_at: STAMP2 })] } }).plan.tasks[0]);
    other.order_updated_at = STAMP2;
    gh.put("state.json", docText(other));
  };
  const r = await engine.sync(local);
  assert.equal(r.status, "ok");
  assert.equal(r.pushed, true);
  assert.deepEqual(sleeps, [1000]);
  const saved = JSON.parse(gh.files.get("state.json").text);
  assert.deepEqual(saved.plan.tasks.map((t) => t.name), ["Gamma", "Alpha edited"]);
  const puts = gh.calls.filter((c) => c.method === "PUT");
  assert.equal(puts.length, 2);
  assert.equal(JSON.parse(puts[1].body).sha, "sha2");
});

test("sync: offline keeps the dirty local doc and reports offline", async () => {
  const { gh, engine, storage } = setup();
  engine.setDirty(true);
  gh.offline = true;
  const local = doc([task("a", "Alpha", { updated_at: STAMP1 })]);
  const r = await engine.sync(local);
  assert.equal(r.status, "offline");
  assert.equal(r.pushed, false);
  assert.deepEqual(r.doc, local);
  assert.equal(JSON.parse(storage.getItem("focus.sync")).dirty, true);
  // A new engine (say, after the app was closed) still knows there are unsent changes.
  const again = new SyncEngine(engine.store, storage);
  assert.equal(again.dirty, true);
  gh.offline = false;
  const r2 = await again.sync(local);
  assert.equal(r2.status, "ok");
  assert.equal(again.dirty, false);
});

test("sync refuses a newer state file and never writes over it", async () => {
  const text = JSON.stringify({ version: 4, plan: { tasks: [] } });
  const { gh, engine } = setup({ files: { "state.json": text } });
  const r = await engine.sync(doc([task("a", "Alpha")]));
  assert.equal(r.status, "version");
  assert.match(r.message, /newer/);
  assert.equal(gh.files.get("state.json").text, text);
  assert.equal(gh.calls.filter((c) => c.method === "PUT").length, 0);
});

test("sync refuses an older (version 2) file and asks to open the desktop app", async () => {
  const text = JSON.stringify({ version: 2, plan: { tasks: [{ name: "Alpha", estimate_min: 5 }] } });
  const { gh, engine } = setup({ files: { "state.json": text } });
  const r = await engine.sync(doc([]));
  assert.equal(r.status, "version");
  assert.match(r.message, /desktop app once/);
  assert.equal(gh.files.get("state.json").text, text);
});

test("fetchDayPlan reads day-plan.json or gives null", async () => {
  const plan = { version: 1, date: "2026-10-13", blocks: [] };
  const { store } = setup({ files: { "day-plan.json": JSON.stringify(plan) } });
  assert.deepEqual(await fetchDayPlan(store), plan);
  const empty = setup();
  assert.equal(await fetchDayPlan(empty.store), null);
});
