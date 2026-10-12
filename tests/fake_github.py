"""A tiny fake GitHub REST API, for tests and for manual use against the desktop client.

Runnable standalone:
    python tests/fake_github.py --port 8765 --token SECRET --owner me --repo focus-data [--seed state.json]

Importable:
    from tests.fake_github import start_fake
    fake = start_fake(token="SECRET", owner="me", repo="focus-data")
    fake.url            # "http://127.0.0.1:<port>"
    fake.files          # {"state.json": {"text": "...", "sha": "..."}, ...}
    fake.dispatches     # [{"workflow": "replan.yml", "ref": "main", "inputs": {...}}, ...]
    fake.set_rate_limit(reset_at=...)   # next requests answer 403 rate-limited
    fake.clear_rate_limit()
    fake.force_oversized("state.json") # GET answers 200 with no usable content, like a >1 MB file
    fake.clear_oversized("state.json")
    fake.stop()

Implements just enough of the real GitHub Contents, repo-check and workflow-dispatch endpoints for
the desktop client (gh_store.py) and the web client to be tested against: real git blob SHA, ETag and
304 on a matching If-None-Match, 409 on a stale sha, 422 when a sha is required but missing, 401 on a
wrong token, 404 for a missing file, and CORS (including OPTIONS preflight).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs


def blob_sha(data: bytes) -> str:
    """The real git blob SHA-1: sha1("blob " + len + NUL + data)."""
    header = f"blob {len(data)}\0".encode("ascii")
    return hashlib.sha1(header + data).hexdigest()


class FakeGitHub:
    """One fake repo, served over real HTTP on localhost. Thread-safe enough for sequential test use."""

    def __init__(self, token, owner, repo, port=0, seed_text=None, seed_path="state.json"):
        self.token = token
        self.owner = owner
        self.repo = repo
        self.files = {}  # path -> {"text": str, "sha": str}
        self.dispatches = []  # list of {"workflow":..., "ref":..., "inputs":...}
        self._rate_limit = None  # None, or {"reset_at": epoch_seconds}
        self._oversized = {}  # path -> size, forced by force_oversized
        if seed_text is not None:
            self._put(seed_path, seed_text)
        handler = _make_handler(self)
        self._server = HTTPServer(("127.0.0.1", port), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://127.0.0.1:{port}"

    def set_rate_limit(self, reset_at=None):
        """From the next request on, answer 403 with x-ratelimit-remaining: 0 (until clear_rate_limit)."""
        self._rate_limit = {"reset_at": reset_at}

    def clear_rate_limit(self):
        self._rate_limit = None

    def force_oversized(self, path, size=2_000_000):
        """Make GET of `path` answer 200 with no usable base64 content, the way the real
        GitHub contents API behaves for a file over about 1 MB (no "content", "encoding": "none",
        just a "size"). The file must already exist. Call clear_oversized(path) to undo."""
        self._oversized[path] = size

    def clear_oversized(self, path):
        self._oversized.pop(path, None)

    def stop(self):
        self._server.shutdown()
        self._server.server_close()

    def _put(self, path, text):
        data = text.encode("utf-8")
        sha = blob_sha(data)
        self.files[path] = {"text": text, "sha": sha}
        return sha


def _make_handler(fake: FakeGitHub):
    contents_prefix = f"/repos/{fake.owner}/{fake.repo}/contents/"
    repo_path = f"/repos/{fake.owner}/{fake.repo}"
    dispatch_prefix = f"/repos/{fake.owner}/{fake.repo}/actions/workflows/"

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # quiet: do not spam test output
            pass

        def _cors_headers(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers",
                              "Authorization, Content-Type, If-None-Match, X-GitHub-Api-Version, Accept")
            self.send_header("Access-Control-Allow-Methods", "GET, PUT, POST, OPTIONS")
            self.send_header("Access-Control-Expose-Headers", "ETag")

        def _send_json(self, status, body=None, extra_headers=None):
            payload = b"" if body is None else json.dumps(body).encode("utf-8")
            self.send_response(status)
            self._cors_headers()
            if payload:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            for k, v in (extra_headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if payload:
                self.wfile.write(payload)

        def _rate_limited_response(self):
            headers = {"x-ratelimit-remaining": "0"}
            if fake._rate_limit.get("reset_at") is not None:
                headers["x-ratelimit-reset"] = str(int(fake._rate_limit["reset_at"]))
            self._send_json(403, {"message": "rate limit exceeded"}, headers)

        def _check_auth(self) -> bool:
            expected = f"Bearer {fake.token}"
            got = self.headers.get("Authorization", "")
            if got != expected:
                self._send_json(401, {"message": "Bad credentials"})
                return False
            return True

        def do_OPTIONS(self):
            self.send_response(204)
            self._cors_headers()
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            if fake._rate_limit is not None:
                self._rate_limited_response()
                return
            parsed = urlparse(self.path)
            path = parsed.path
            if path == repo_path:
                if not self._check_auth():
                    return
                self._send_json(200, {"full_name": f"{fake.owner}/{fake.repo}"})
                return
            if path.startswith(contents_prefix):
                if not self._check_auth():
                    return
                file_path = path[len(contents_prefix):]
                entry = fake.files.get(file_path)
                if entry is None:
                    self._send_json(404, {"message": "Not Found"})
                    return
                etag = f'"{entry["sha"]}"'
                if self.headers.get("If-None-Match") == etag:
                    self.send_response(304)
                    self._cors_headers()
                    self.send_header("ETag", etag)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if file_path in fake._oversized:
                    self._send_json(200, {
                        "name": file_path.rsplit("/", 1)[-1],
                        "path": file_path,
                        "sha": entry["sha"],
                        "size": fake._oversized[file_path],
                        "encoding": "none",
                    }, {"ETag": etag})
                    return
                raw = entry["text"].encode("utf-8")
                b64 = base64.b64encode(raw).decode("ascii")
                # GitHub wraps the base64 content; wrap it here too so clients must tolerate newlines.
                wrapped = "\n".join(b64[i:i + 60] for i in range(0, len(b64), 60))
                self._send_json(200, {
                    "name": file_path.rsplit("/", 1)[-1],
                    "path": file_path,
                    "sha": entry["sha"],
                    "content": wrapped,
                    "encoding": "base64",
                }, {"ETag": etag})
                return
            self._send_json(404, {"message": "Not Found"})

        def do_PUT(self):
            if fake._rate_limit is not None:
                self._rate_limited_response()
                return
            parsed = urlparse(self.path)
            path = parsed.path
            if not path.startswith(contents_prefix):
                self._send_json(404, {"message": "Not Found"})
                return
            if not self._check_auth():
                return
            file_path = path[len(contents_prefix):]
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw_body.decode("utf-8")) if raw_body else {}
            except json.JSONDecodeError:
                self._send_json(400, {"message": "bad json"})
                return
            content_b64 = body.get("content", "")
            try:
                data = base64.b64decode(content_b64)
            except Exception:
                self._send_json(400, {"message": "bad content"})
                return
            existing = fake.files.get(file_path)
            given_sha = body.get("sha")
            if existing is not None:
                if given_sha is None:
                    self._send_json(422, {"message": "sha required to update an existing file"})
                    return
                if given_sha != existing["sha"]:
                    self._send_json(409, {"message": "sha does not match current content"})
                    return
                status = 200
            else:
                status = 201
            text = data.decode("utf-8")
            new_sha = fake._put(file_path, text)
            self._send_json(status, {
                "content": {"sha": new_sha, "path": file_path},
                "commit": {"message": body.get("message", "")},
            })

        def do_POST(self):
            if fake._rate_limit is not None:
                self._rate_limited_response()
                return
            parsed = urlparse(self.path)
            path = parsed.path
            if path.startswith(dispatch_prefix) and path.endswith("/dispatches"):
                if not self._check_auth():
                    return
                workflow = path[len(dispatch_prefix):-len("/dispatches")]
                length = int(self.headers.get("Content-Length", "0"))
                raw_body = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw_body.decode("utf-8")) if raw_body else {}
                except json.JSONDecodeError:
                    body = {}
                fake.dispatches.append({
                    "workflow": workflow,
                    "ref": body.get("ref"),
                    "inputs": body.get("inputs") or {},
                })
                self.send_response(204)
                self._cors_headers()
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._send_json(404, {"message": "Not Found"})

    return Handler


def start_fake(token="test-token", owner="me", repo="focus-data", port=0, seed=None):
    """Start a fake GitHub server in a background thread on a (default: random) port.

    `seed`, if given, is a path to a JSON file whose content becomes the initial state.json.
    Returns a FakeGitHub handle; call .stop() when done.
    """
    seed_text = None
    if seed is not None:
        with open(seed, "r", encoding="utf-8") as f:
            seed_text = f.read()
    return FakeGitHub(token=token, owner=owner, repo=repo, port=port, seed_text=seed_text)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run a fake GitHub server for manual testing.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--token", default="test-token")
    parser.add_argument("--owner", default="me")
    parser.add_argument("--repo", default="focus-data")
    parser.add_argument("--seed", default=None)
    args = parser.parse_args(argv)
    fake = start_fake(token=args.token, owner=args.owner, repo=args.repo, port=args.port, seed=args.seed)
    print(f"Fake GitHub serving {args.owner}/{args.repo} at {fake.url} (token {args.token!r}). Ctrl+C to stop.")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        fake.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
