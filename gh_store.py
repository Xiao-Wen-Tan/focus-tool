"""Desktop GitHub client for the Focus Tool. It reads and writes state.json and day-plan.json in the
private focus-data repo through the GitHub REST API, and syncs a local copy against it.

Stdlib only (urllib.request, ssl, base64, json), so this module always imports even on a machine
without pywin32. The Windows Credential Manager functions (`load_token`, `save_token`, `delete_token`)
import `win32cred` lazily, inside each function, for the same reason.

What lives here:
- GitHubStore: one authenticated connection to one repo (get_file, put_file, dispatch, check).
- SyncEngine: merges a local document with the remote one through focus_core.merge_docs and pushes
  the result back, retrying on a conflicting write.
- fetch_day_plan / put_day_plan: one-shot reads and writes of day-plan.json.
- load_token / save_token / delete_token: the fine-grained GitHub token in Windows Credential Manager.
"""
from __future__ import annotations

import base64
import json
import os
import ssl
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import focus_core as fc

CRED_TARGET = "FocusTool:github"
STATE_PATH = "state.json"
DAY_PLAN_PATH = "day-plan.json"
_CONFLICT_BACKOFF_S = (1, 2, 4)  # sleep before retries 2, 3, 4 (3 backoffs for 4 attempts)
_MAX_PUT_ATTEMPTS = 4


class GitHubError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class Conflict(GitHubError):
    pass


class Offline(GitHubError):
    def __init__(self, message: str):
        super().__init__(0, message)


class AuthError(GitHubError):
    pass


class RateLimited(GitHubError):
    def __init__(self, status: int, message: str, reset_at: Optional[float] = None,
                 retry_after_s: Optional[float] = None):
        super().__init__(status, message)
        self.reset_at = reset_at
        if retry_after_s is None and reset_at is not None:
            retry_after_s = max(0.0, reset_at - time.time())
        self.retry_after_s = retry_after_s


@dataclass
class Remote:
    text: Optional[str]
    sha: Optional[str]
    etag: Optional[str]
    not_modified: bool


class GitHubStore:
    def __init__(self, owner, repo, token, branch="main", api="https://api.github.com",
                 timeout=15, opener=None):
        self.owner = owner
        self.repo = repo
        self.token = token
        self.branch = branch
        self.api = api.rstrip("/")
        self.timeout = timeout
        self._opener = opener
        self._ssl_context = ssl.create_default_context()

    # ---------------------------------------------------------------- low level

    def _headers(self, extra=None):
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "FocusTool",
        }
        if extra:
            headers.update(extra)
        return headers

    def _open(self, req):
        try:
            if self._opener is not None:
                return self._opener.open(req, timeout=self.timeout)
            return urllib.request.urlopen(req, timeout=self.timeout, context=self._ssl_context)
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise Offline(f"could not reach GitHub: {e}") from e

    def _request(self, method, url, headers, data=None):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            resp = self._open(req)
            body = resp.read()
            return resp.status, dict(resp.headers.items()), body
        except urllib.error.HTTPError as e:
            body = e.read()
            self._raise_for_error(e.code, dict(e.headers.items()) if e.headers else {}, body)

    def _raise_for_error(self, status, headers, body):
        message = self._error_message(body)
        if status == 401:
            raise AuthError(status, message)
        if status in (403, 429) and self._is_rate_limit(headers):
            reset_at, retry_after_s = self._rate_limit_timing(headers)
            raise RateLimited(status, message, reset_at=reset_at, retry_after_s=retry_after_s)
        if status in (409, 422):
            raise Conflict(status, message)
        raise GitHubError(status, message)

    @staticmethod
    def _is_rate_limit(headers) -> bool:
        lower = {k.lower(): v for k, v in headers.items()}
        if lower.get("x-ratelimit-remaining") == "0":
            return True
        return "retry-after" in lower

    @staticmethod
    def _rate_limit_timing(headers):
        lower = {k.lower(): v for k, v in headers.items()}
        reset_at = None
        retry_after_s = None
        if "x-ratelimit-reset" in lower:
            try:
                reset_at = float(lower["x-ratelimit-reset"])
            except ValueError:
                reset_at = None
        if "retry-after" in lower:
            try:
                retry_after_s = float(lower["retry-after"])
            except ValueError:
                retry_after_s = None
        return reset_at, retry_after_s

    @staticmethod
    def _error_message(body: bytes) -> str:
        try:
            data = json.loads(body.decode("utf-8")) if body else {}
            return str(data.get("message", "")) or "GitHub error"
        except (json.JSONDecodeError, UnicodeDecodeError):
            return "GitHub error"

    @staticmethod
    def _decode_content(b64_text: str) -> str:
        # GitHub wraps base64 content in newlines; tolerate that (and any other whitespace).
        cleaned = "".join(b64_text.split())
        return base64.b64decode(cleaned).decode("utf-8")

    # ---------------------------------------------------------------- contents API

    def get_file(self, path, etag=None) -> Remote:
        url = f"{self.api}/repos/{self.owner}/{self.repo}/contents/{path}?ref={self.branch}"
        extra = {"If-None-Match": etag} if etag else {}
        req = urllib.request.Request(url, headers=self._headers(extra), method="GET")
        try:
            resp = self._open(req)
            status = resp.status
            headers = dict(resp.headers.items())
            body = resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 304:
                return Remote(text=None, sha=None, etag=etag, not_modified=True)
            if e.code == 404:
                return Remote(text=None, sha=None, etag=None, not_modified=False)
            body = e.read()
            self._raise_for_error(e.code, dict(e.headers.items()) if e.headers else {}, body)
            return  # unreachable, _raise_for_error always raises
        if status == 304:
            return Remote(text=None, sha=None, etag=etag, not_modified=True)
        data = json.loads(body.decode("utf-8"))
        content = data.get("content")
        encoding = data.get("encoding")
        size = data.get("size") or 0
        if (size > 0 and not content) or (encoding not in (None, "base64")):
            # GitHub answers this way for a file over about 1 MB: no base64 content, a
            # non-"base64" encoding (or none at all). Never hand back empty text for that.
            raise GitHubError(
                200, f"GitHub returned no usable content for {path} (the file may be too "
                     f"large for the contents API, which only serves files up to 1 MB this way)")
        text = self._decode_content(content or "")
        lower = {k.lower(): v for k, v in headers.items()}
        new_etag = lower.get("etag", f'"{data.get("sha", "")}"')
        return Remote(text=text, sha=data["sha"], etag=new_etag, not_modified=False)

    def put_file(self, path, text, sha, message) -> str:
        url = f"{self.api}/repos/{self.owner}/{self.repo}/contents/{path}"
        body = {
            "message": message,
            "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
            "branch": self.branch,
        }
        if sha is not None:
            body["sha"] = sha
        data = json.dumps(body).encode("utf-8")
        status, headers, resp_body = self._request(
            "PUT", url, self._headers({"Content-Type": "application/json"}), data=data)
        resp = json.loads(resp_body.decode("utf-8"))
        return resp["content"]["sha"]

    def dispatch(self, workflow="replan.yml", ref="main", inputs=None) -> None:
        url = f"{self.api}/repos/{self.owner}/{self.repo}/actions/workflows/{workflow}/dispatches"
        body = {"ref": ref, "inputs": inputs or {}}
        data = json.dumps(body).encode("utf-8")
        status, headers, resp_body = self._request(
            "POST", url, self._headers({"Content-Type": "application/json"}), data=data)
        if status != 204:
            raise GitHubError(status, "unexpected response to workflow dispatch")

    def check(self) -> str:
        url = f"{self.api}/repos/{self.owner}/{self.repo}"
        try:
            status, headers, body = self._request("GET", url, self._headers())
        except AuthError:
            return f"Bad token, or no access to {self.owner}/{self.repo}."
        except GitHubError as e:
            if e.status == 404:
                return f"Repository {self.owner}/{self.repo} was not found, or you have no access to it."
            return f"GitHub error: {e.message}"
        except Offline as e:
            return f"Could not reach GitHub: {e.message}"
        warning = ""
        try:
            if json.loads(body.decode("utf-8")).get("private") is False:
                warning = " This repository is public, so anyone can see your list."
        except (ValueError, AttributeError):
            pass  # no readable answer about privacy; say nothing rather than guess
        return f"Connected to {self.owner}/{self.repo}.{warning}"


# ---------------------------------------------------------------- sync


@dataclass
class SyncResult:
    doc: dict
    remote_changed: bool
    pushed: bool
    status: str  # "ok" | "offline" | "auth" | "rate_limited" | "error"
    message: str
    retry_after_s: Optional[float]


class SyncEngine:
    def __init__(self, store: GitHubStore, cache_path: Path, sleep=time.sleep):
        self.store = store
        self.cache_path = Path(cache_path)
        self._sleep = sleep

    def _load_cache(self) -> dict:
        try:
            return json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {"etag": None, "sha": None, "last_remote_doc": None}

    def _save_cache(self, etag, sha, remote_doc):
        # Write the new content to a temp file next to the real one, then swap it in with a
        # single os.replace. This keeps the write atomic, so a crash or a Box sync mid-write
        # never leaves a half-written or truncated cache file behind.
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"etag": etag, "sha": sha, "last_remote_doc": remote_doc}, ensure_ascii=False)
        fd, tmp_name = tempfile.mkstemp(
            prefix=self.cache_path.name + ".", suffix=".tmp", dir=str(self.cache_path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, self.cache_path)
        except Exception:
            try:
                os.remove(tmp_name)
            except OSError:
                pass
            raise

    def sync(self, local_doc: dict) -> SyncResult:
        cache = self._load_cache()
        try:
            remote = self.store.get_file(STATE_PATH, etag=cache.get("etag"))
        except AuthError as e:
            return SyncResult(local_doc, False, False, "auth", e.message, None)
        except RateLimited as e:
            return SyncResult(local_doc, False, False, "rate_limited", e.message, e.retry_after_s)
        except Offline as e:
            return SyncResult(local_doc, False, False, "offline", e.message, None)
        except GitHubError as e:
            # A 200 with no usable content, or any other GitHub error get_file has no named
            # status for, lands here. Never guess at the remote content, and never push over it.
            return SyncResult(local_doc, False, False, "error", e.message, None)

        # `last_remote_doc` in the cache, and `remote_doc` here, are always the fully migrated
        # (normalized) form of what is on GitHub, never the raw JSON as GitHub sent it. That way
        # comparing "did the remote change" never confuses a normalization difference (for
        # example an older file missing a default field) with a real content change.
        if remote.not_modified:
            remote_doc = cache.get("last_remote_doc")
            sha = cache.get("sha")
            etag = cache.get("etag")
            remote_changed = False
        elif remote.text is None:
            remote_doc = None
            sha = None
            etag = None
            remote_changed = False
        else:
            remote_doc = fc.migrate_doc(json.loads(remote.text))
            sha = remote.sha
            etag = remote.etag
            cached_remote = cache.get("last_remote_doc")
            remote_changed = cached_remote is None or fc.canonical(remote_doc) != fc.canonical(cached_remote)

        if remote_doc is None:
            merged = fc.migrate_doc(local_doc)
        else:
            merged = fc.merge_docs(local_doc, remote_doc)

        needs_push = sha is None or fc.canonical(merged) != fc.canonical(remote_doc or {})
        if not needs_push:
            self._save_cache(etag, sha, remote_doc)
            return SyncResult(merged, remote_changed, False, "ok", "", None)

        text = json.dumps(merged, indent=2, ensure_ascii=False)
        attempt = 0
        while True:
            try:
                new_sha = self.store.put_file(STATE_PATH, text, sha, "sync from desktop")
            except Conflict:
                attempt += 1
                if attempt >= _MAX_PUT_ATTEMPTS:
                    return SyncResult(merged, remote_changed, False, "error",
                                       "too many conflicting writes to state.json", None)
                self._sleep(_CONFLICT_BACKOFF_S[attempt - 1])
                try:
                    fresh = self.store.get_file(STATE_PATH)
                except AuthError as e:
                    return SyncResult(local_doc, remote_changed, False, "auth", e.message, None)
                except RateLimited as e:
                    return SyncResult(local_doc, remote_changed, False, "rate_limited", e.message, e.retry_after_s)
                except Offline as e:
                    return SyncResult(local_doc, remote_changed, False, "offline", e.message, None)
                except GitHubError as e:
                    return SyncResult(local_doc, remote_changed, False, "error", e.message, None)
                if fresh.text is None:
                    sha = None
                    merged = fc.migrate_doc(merged)
                else:
                    remote_doc = fc.migrate_doc(json.loads(fresh.text))
                    sha = fresh.sha
                    etag = fresh.etag
                    remote_changed = True
                    merged = fc.merge_docs(merged, remote_doc)
                text = json.dumps(merged, indent=2, ensure_ascii=False)
                continue
            except AuthError as e:
                return SyncResult(local_doc, remote_changed, False, "auth", e.message, None)
            except RateLimited as e:
                return SyncResult(local_doc, remote_changed, False, "rate_limited", e.message, e.retry_after_s)
            except Offline as e:
                return SyncResult(local_doc, remote_changed, False, "offline", e.message, None)
            self._save_cache(etag, new_sha, merged)
            return SyncResult(merged, remote_changed, True, "ok", "", None)


def fetch_day_plan(store: GitHubStore) -> Optional[dict]:
    remote = store.get_file(DAY_PLAN_PATH)
    if remote.text is None:
        return None
    return json.loads(remote.text)


def put_day_plan(store: GitHubStore, plan: dict) -> None:
    remote = store.get_file(DAY_PLAN_PATH)
    text = json.dumps(plan, indent=2, ensure_ascii=False)
    store.put_file(DAY_PLAN_PATH, text, remote.sha, "update day plan")


# ---------------------------------------------------------------- Windows Credential Manager


def load_token() -> Optional[str]:
    import win32cred
    try:
        cred = win32cred.CredRead(CRED_TARGET, win32cred.CRED_TYPE_GENERIC)
    except Exception:
        return None
    blob = cred.get("CredentialBlob")
    if blob is None:
        return None
    if isinstance(blob, bytes):
        try:
            return blob.decode("utf-16-le").rstrip("\x00")
        except UnicodeDecodeError:
            return blob.decode("utf-8", errors="ignore")
    return blob


def save_token(token: str) -> None:
    import win32cred
    cred = {
        "Type": win32cred.CRED_TYPE_GENERIC,
        "TargetName": CRED_TARGET,
        "UserName": "FocusTool",
        "CredentialBlob": token,
        "Persist": win32cred.CRED_PERSIST_LOCAL_MACHINE,
    }
    win32cred.CredWrite(cred, 0)


def delete_token() -> None:
    import win32cred
    try:
        win32cred.CredDelete(CRED_TARGET, win32cred.CRED_TYPE_GENERIC)
    except Exception:
        pass
