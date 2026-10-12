// GitHub client and sync for the phone web app. Same shape as the desktop's gh_store.py.
// The list lives in a PRIVATE repository as state.json; this file reads and writes it through the
// GitHub REST "contents" API with a fine-grained token kept only in this browser's localStorage.
import { mergeDocs, normalizeDoc, canonical, VersionError } from "./core.js";

export class GitHubError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}
export class Conflict extends GitHubError {}
export class Offline extends GitHubError {}
export class AuthError extends GitHubError {}
export class RateLimited extends GitHubError {
  constructor(status, message, resetAt) {
    super(status, message);
    this.resetAt = resetAt; // seconds since 1970, or null
  }
}

// ---------------------------------------------------------------- UTF-8 safe base64

export function encodeBase64Utf8(text) {
  const bytes = new TextEncoder().encode(text);
  let bin = "";
  for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(bin);
}

export function decodeBase64Utf8(b64) {
  const bin = atob(String(b64).replace(/\s+/g, ""));
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new TextDecoder("utf-8").decode(bytes);
}

/** The text written to state.json: like Python's json.dumps(doc, indent=2, ensure_ascii=False). */
export function docText(doc) {
  return JSON.stringify(doc, null, 2);
}

// ---------------------------------------------------------------- the REST client

export class GitHubStore {
  constructor({ owner, repo, token, branch = "main", api = "https://api.github.com", fetchImpl = null, timeoutMs = 15000 }) {
    this.owner = owner;
    this.repo = repo;
    this.token = token;
    this.branch = branch || "main";
    this.api = String(api || "https://api.github.com").replace(/\/+$/, "");
    this.fetch = fetchImpl || ((...a) => globalThis.fetch(...a));
    this.timeoutMs = timeoutMs;
  }

  headers(extra = {}) {
    return {
      Authorization: `Bearer ${this.token}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      ...extra,
    };
  }

  repoUrl() {
    return `${this.api}/repos/${encodeURIComponent(this.owner)}/${encodeURIComponent(this.repo)}`;
  }

  contentsUrl(path) {
    const p = path.split("/").map(encodeURIComponent).join("/");
    return `${this.repoUrl()}/contents/${p}`;
  }

  async request(url, init = {}) {
    const ctl = typeof AbortController === "function" ? new AbortController() : null;
    const timer = ctl ? setTimeout(() => ctl.abort(), this.timeoutMs) : null;
    let res;
    try {
      res = await this.fetch(url, { cache: "no-store", ...init, signal: ctl ? ctl.signal : undefined });
    } catch (ex) {
      throw new Offline(0, `Could not reach GitHub (${(ex && ex.message) || ex}).`);
    } finally {
      if (timer) clearTimeout(timer);
    }
    return res;
  }

  async fail(res) {
    let message = `GitHub answered ${res.status}.`;
    try {
      const body = await res.json();
      if (body && body.message) message = `GitHub answered ${res.status}: ${body.message}`;
    } catch { /* no JSON body */ }
    const h = (k) => (res.headers && res.headers.get ? res.headers.get(k) : null);
    if (res.status === 401) throw new AuthError(401, "GitHub did not accept the token. Check it in Settings.");
    if ((res.status === 403 || res.status === 429) && (h("x-ratelimit-remaining") === "0" || h("retry-after"))) {
      let reset = null;
      if (h("retry-after")) reset = Date.now() / 1000 + Number(h("retry-after"));
      else if (h("x-ratelimit-reset")) reset = Number(h("x-ratelimit-reset"));
      throw new RateLimited(res.status, "GitHub asked us to slow down for a while.", reset);
    }
    if (res.status === 409) throw new Conflict(409, message);
    if (res.status === 422 && /sha/i.test(message)) throw new Conflict(422, message);
    throw new GitHubError(res.status, message);
  }

  /** {text, sha, etag, notModified}. 304 gives notModified; 404 gives text null. */
  async getFile(path, etag = null) {
    const extra = etag ? { "If-None-Match": etag } : {};
    const res = await this.request(`${this.contentsUrl(path)}?ref=${encodeURIComponent(this.branch)}`,
      { method: "GET", headers: this.headers(extra) });
    const newEtag = (res.headers && res.headers.get && res.headers.get("etag")) || null;
    if (res.status === 304) return { text: null, sha: null, etag: etag, notModified: true };
    if (res.status === 404) return { text: null, sha: null, etag: null, notModified: false };
    if (!res.ok) await this.fail(res);
    const body = await res.json();
    return { text: decodeBase64Utf8(body.content || ""), sha: body.sha || null, etag: newEtag, notModified: false };
  }

  /** Write `text` to `path`; returns the new content sha. A stale sha raises Conflict. */
  async putFile(path, text, sha, message) {
    const body = { message, content: encodeBase64Utf8(text), branch: this.branch };
    if (sha) body.sha = sha;
    const res = await this.request(this.contentsUrl(path), {
      method: "PUT", headers: this.headers({ "Content-Type": "application/json" }), body: JSON.stringify(body),
    });
    if (!res.ok) await this.fail(res);
    const out = await res.json();
    return (out && out.content && out.content.sha) || null;
  }

  /** Start the re-plan workflow in the data repository. GitHub answers 204. */
  async dispatch(workflow = "replan.yml", ref = "main", inputs = null) {
    const body = { ref };
    if (inputs) body.inputs = inputs;
    const res = await this.request(`${this.repoUrl()}/actions/workflows/${encodeURIComponent(workflow)}/dispatches`, {
      method: "POST", headers: this.headers({ "Content-Type": "application/json" }), body: JSON.stringify(body),
    });
    if (res.status !== 204 && !res.ok) await this.fail(res);
  }

  /** A friendly sentence about whether the repository can be reached with this token. */
  async check() {
    let res;
    try {
      res = await this.request(this.repoUrl(), { method: "GET", headers: this.headers() });
    } catch (ex) {
      return "Could not reach GitHub. Check your connection.";
    }
    if (res.status === 401) return "GitHub did not accept the token. Make a new one and paste it again.";
    if (res.status === 404) return `Repository ${this.owner}/${this.repo} was not found, or the token cannot see it.`;
    if (!res.ok) return `GitHub answered ${res.status}.`;
    let priv = "";
    try {
      const body = await res.json();
      priv = body && body.private === false ? " Warning: this repository is public, so your list is visible to anyone." : "";
    } catch { /* ignore */ }
    return `Connected to ${this.owner}/${this.repo}.${priv}`;
  }
}

// ---------------------------------------------------------------- sync

const SYNC_KEY = "focus.sync";

function readJson(storage, key) {
  try {
    const t = storage && storage.getItem(key);
    return t ? JSON.parse(t) : null;
  } catch {
    return null;
  }
}

function writeJson(storage, key, value) {
  try {
    if (storage) storage.setItem(key, JSON.stringify(value));
  } catch { /* storage full or blocked: keep going in memory */ }
}

const sleepMs = (ms) => new Promise((r) => setTimeout(r, ms));

/** Keeps the phone's copy and the state.json on GitHub in step.
 * Cache in localStorage "focus.sync": {etag, sha, last_remote_doc, dirty}. The full local doc itself is
 * kept by the app under "focus.doc"; together they are the offline queue (no operation log). */
export class SyncEngine {
  constructor(store, storage, { sleep = sleepMs, path = "state.json", message = "phone: update list" } = {}) {
    this.store = store;
    this.storage = storage;
    this.sleep = sleep;
    this.path = path;
    this.message = message;
    this.cache = Object.assign({ etag: null, sha: null, last_remote_doc: null, dirty: false }, readJson(storage, SYNC_KEY) || {});
  }

  saveCache() {
    writeJson(this.storage, SYNC_KEY, this.cache);
  }

  setDirty(on) {
    this.cache.dirty = Boolean(on);
    this.saveCache();
  }

  get dirty() {
    return Boolean(this.cache.dirty);
  }

  /** Read GitHub's copy. Returns {doc, sha, changed}; doc null when the file does not exist yet. */
  async fetchRemote(useEtag) {
    const r = await this.store.getFile(this.path, useEtag ? this.cache.etag : null);
    if (r.notModified && this.cache.last_remote_doc) {
      return { doc: this.cache.last_remote_doc, sha: this.cache.sha, changed: false };
    }
    if (r.notModified) return this.fetchRemote(false); // cache lost its doc: fetch the full file
    if (r.text == null) {
      this.cache.etag = null;
      this.cache.sha = null;
      this.cache.last_remote_doc = null;
      return { doc: null, sha: null, changed: false };
    }
    let parsed;
    try {
      parsed = JSON.parse(r.text);
    } catch {
      throw new GitHubError(0, "state.json on GitHub is not valid JSON. Fix it on the desktop first.");
    }
    const doc = normalizeDoc(parsed); // refuses versions other than 3 (VersionError) before anything is written
    const changed = !this.cache.last_remote_doc || canonical(doc) !== canonical(this.cache.last_remote_doc);
    this.cache.etag = r.etag;
    this.cache.sha = r.sha;
    this.cache.last_remote_doc = doc;
    return { doc, sha: r.sha, changed };
  }

  /** GET (If-None-Match) -> merge(local, remote) -> PUT if the merge differs from GitHub's copy.
   * A conflict means someone else wrote first: fetch again, merge again, up to 4 tries (1, 2, 4 s apart).
   * Returns {doc, remoteChanged, pushed, status, message, retryAfterS};
   * status is ok | offline | auth | rate_limited | version | error. */
  async sync(localDoc) {
    let remoteChanged = false;
    let useEtag = true;
    for (let attempt = 0; attempt < 4; attempt++) {
      try {
        const remote = await this.fetchRemote(useEtag);
        remoteChanged = remoteChanged || remote.changed;
        const merged = remote.doc ? mergeDocs(localDoc, remote.doc) : normalizeDoc(localDoc);
        if (remote.doc && canonical(merged) === canonical(remote.doc)) {
          this.cache.dirty = false;
          this.saveCache();
          return { doc: merged, remoteChanged, pushed: false, status: "ok", message: "Up to date.", retryAfterS: null };
        }
        const sha = await this.store.putFile(this.path, docText(merged), remote.sha, this.message);
        this.cache.sha = sha;
        this.cache.etag = null; // the file changed, so the old ETag no longer applies
        this.cache.last_remote_doc = merged;
        this.cache.dirty = false;
        this.saveCache();
        return { doc: merged, remoteChanged, pushed: true, status: "ok", message: "Saved to GitHub.", retryAfterS: null };
      } catch (ex) {
        if (ex instanceof Conflict && attempt < 3) {
          useEtag = false;
          await this.sleep(1000 * 2 ** attempt);
          continue;
        }
        this.saveCache();
        return this.failure(localDoc, ex, remoteChanged);
      }
    }
    /* c8 ignore next */
    return this.failure(localDoc, new GitHubError(0, "Gave up after 4 tries."), remoteChanged);
  }

  failure(localDoc, ex, remoteChanged) {
    const base = { doc: localDoc, remoteChanged, pushed: false, retryAfterS: null };
    if (ex instanceof VersionError) return { ...base, status: "version", message: ex.message };
    if (ex instanceof Offline) return { ...base, status: "offline", message: "Offline. Your changes are kept on this phone." };
    if (ex instanceof AuthError) return { ...base, status: "auth", message: ex.message };
    if (ex instanceof RateLimited) {
      const wait = ex.resetAt ? Math.max(1, ex.resetAt - Date.now() / 1000) : 60;
      return { ...base, status: "rate_limited", message: ex.message, retryAfterS: wait };
    }
    if (ex instanceof Conflict) return { ...base, status: "error", message: "Someone else kept saving at the same time. Will try again." };
    return { ...base, status: "error", message: (ex && ex.message) || String(ex) };
  }
}

/** The day plan written by the planner, or null if there is none yet. */
export async function fetchDayPlan(store) {
  const r = await store.getFile("day-plan.json");
  if (!r.text) return null;
  try {
    return JSON.parse(r.text);
  } catch {
    return null;
  }
}
