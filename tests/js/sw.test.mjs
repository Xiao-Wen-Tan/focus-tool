// docs/sw.js in a sandbox with fake caches and fetch: network first, cache fallback, other origins untouched.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const SRC = readFileSync(join(dirname(fileURLToPath(import.meta.url)), "..", "..", "docs", "sw.js"), "utf-8");
const ORIGIN = "https://me.github.io";
const BASE = `${ORIGIN}/focus-tool/`;

function sandbox() {
  const store = new Map(); // url without query -> body
  const listeners = {};
  const fetchCalls = [];
  let online = true;
  const res = (url, body) => ({ ok: true, type: "basic", redirected: false, url, body, clone() { return res(url, body); } });
  const cache = {
    put: async (url, r) => { store.set(String(url), r.body); },
    match: async (req, opts) => {
      const u = new URL(typeof req === "string" ? req : req.url);
      if (opts && opts.ignoreSearch) u.search = "";
      return store.has(u.href) ? res(u.href, store.get(u.href)) : undefined;
    },
    addAll: async (reqs) => {
      for (const r of reqs) {
        fetchCalls.push({ url: r.url, cache: r.cache, install: true });
        store.set(r.url, `installed ${r.url}`);
      }
    },
  };
  const ctx = {
    self: {
      location: new URL(BASE),
      addEventListener: (t, fn) => { listeners[t] = fn; },
      skipWaiting: async () => {},
      clients: { claim: async () => {} },
    },
    caches: {
      open: async () => cache,
      keys: async () => ["focus-shell", "focus-v2.0.0"],
      delete: async (k) => { store.set(`deleted:${k}`, 1); return true; },
    },
    fetch: async (url, init) => {
      fetchCalls.push({ url, cache: init && init.cache });
      if (!online) throw new TypeError("Failed to fetch");
      return res(url, `fresh ${url}`);
    },
    Request: class {
      constructor(url, init) {
        this.url = new URL(url, BASE).href;
        this.cache = init && init.cache;
      }
    },
    Response: { redirect: (u) => ({ redirectTo: u }) },
    URL,
  };
  vm.runInNewContext(SRC, ctx);
  const fire = async (type, request) => {
    let p = null;
    const ev = { request, respondWith: (x) => { p = x; }, waitUntil: (x) => { p = x; } };
    listeners[type](ev);
    return p ? await p : undefined;
  };
  return { store, fetchCalls, fire, setOnline: (v) => { online = v; } };
}

const get = (url, extra = {}) => ({ url, method: "GET", mode: "cors", ...extra });

test("install precaches the app shell with cache: reload", async () => {
  const sb = sandbox();
  await sb.fire("install", null);
  const inst = sb.fetchCalls.filter((c) => c.install);
  assert.ok(inst.length >= 10);
  assert.ok(inst.every((c) => c.cache === "reload"));
  assert.ok(inst.some((c) => c.url === `${BASE}app.js`));
});

test("online: fresh copy from the network (no-store) and the cache is updated", async () => {
  const sb = sandbox();
  const r = await sb.fire("fetch", get(`${BASE}app.js`));
  assert.equal(r.body, `fresh ${BASE}app.js`);
  assert.equal(sb.fetchCalls[0].cache, "no-store");
  assert.equal(sb.store.get(`${BASE}app.js`), `fresh ${BASE}app.js`);
});

test("offline: the cached copy is served, query ignored; nothing cached means an error", async () => {
  const sb = sandbox();
  await sb.fire("fetch", get(`${BASE}?api=http://127.0.0.1:8765`, { mode: "navigate" }));
  assert.ok(sb.store.has(BASE)); // stored without the query
  sb.setOnline(false);
  const r = await sb.fire("fetch", get(`${BASE}?api=http://127.0.0.1:9999`, { mode: "navigate" }));
  assert.equal(r.body, `fresh ${BASE}?api=http://127.0.0.1:8765`);
  await assert.rejects(sb.fire("fetch", get(`${BASE}never-cached.js`)));
});

test("other origins and non-GET requests are left alone", async () => {
  const sb = sandbox();
  assert.equal(await sb.fire("fetch", get("https://api.github.com/repos/me/focus-data/contents/state.json")), undefined);
  assert.equal(await sb.fire("fetch", { url: `${BASE}app.js`, method: "PUT" }), undefined);
  assert.equal(sb.fetchCalls.length, 0);
});

test("activate removes caches from older versions", async () => {
  const sb = sandbox();
  await sb.fire("activate", null);
  assert.ok(sb.store.has("deleted:focus-v2.0.0"));
  assert.ok(!sb.store.has("deleted:focus-shell"));
});
