#!/usr/bin/env python3
"""Safety check for the public focus-tool repo.

This repo will be published as a PUBLIC GitHub repo. Your real tasks live in
data/state.json (Box, local copy) and in the private focus-data repo, never in
this one. This script fails (exit 1) if any tracked or staged file in this repo
looks like it would leak that data:

  - anything under data/
  - any state*.json or day-plan*.json file anywhere
  - any other JSON file that is not on the small allowlist of JSON this repo is
    allowed to ship (settings.default.json, docs/manifest.webmanifest,
    docs/package.json, tests/vectors/**)
  - any file whose content contains one of your task names or folder names
    (4+ characters), read from the live state file

With --history it instead scans the whole git history: every file version
(blob) reachable from any branch or tag, every path those versions were ever
stored under, and every commit message. A name that was committed once and
deleted later is still public once the history is pushed, so this is the check
to run before the first push.

It reads task and folder names from the state file only in memory, to compare
against other files' content. It never prints a name. Only the path of the
offending file (or, for a commit message, the commit id) is printed, so this
script is itself safe to run and to show to anyone.

Usage:
    python tools/check_public.py              # checks tracked files + staged files
    python tools/check_public.py --staged      # checks only staged files (used by the pre-commit hook)
    python tools/check_public.py --history     # checks every file version and commit message in the history
    python tools/check_public.py --state PATH  # use a different state file (tests use a fake one)
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

# JSON files this public repo is allowed to ship. Everything else ending in
# .json is treated as suspicious, since real task/plan data is JSON.
ALLOWED_JSON_PATHS = {
    "settings.default.json",
    "docs/manifest.webmanifest",
    "docs/package.json",
}
ALLOWED_JSON_PREFIXES = ("tests/vectors/",)

MIN_NAME_LEN = 4


def _norm(path: str) -> str:
    return path.replace("\\", "/")


def _run_git(args: list[str], repo_root: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def git_tracked_files(repo_root: Path) -> list[str]:
    out = _run_git(["ls-files"], repo_root)
    return [_norm(p) for p in out.splitlines() if p]


def git_staged_files(repo_root: Path) -> list[str]:
    # ACMR: added, copied, modified, renamed (not deleted, which has no content to check).
    out = _run_git(["diff", "--cached", "--name-only", "--diff-filter=ACMR"], repo_root)
    return [_norm(p) for p in out.splitlines() if p]


def read_staged_blob(repo_root: Path, path: str) -> str | None:
    """Reads a staged file's content straight from the git index (stage 0),
    not from the working tree. A file can be staged with a task name in it
    and then have its working-tree copy edited or cleaned up afterwards; the
    commit that is about to happen uses the staged content, so that is what
    must be checked.
    """
    result = subprocess.run(
        ["git", "show", f":{path}"],
        cwd=repo_root,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return None  # not in the index (e.g. a deleted path), or git could not read it
    try:
        return result.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return None  # binary; not a text leak we can detect this way


def path_reason(path: str) -> str | None:
    """Returns why a path alone is banned, or None if the path is fine."""
    if path == "data" or path.startswith("data/"):
        return "is under data/"

    name = path.rsplit("/", 1)[-1]
    if re.fullmatch(r"state.*\.json", name, re.IGNORECASE):
        return "is a state*.json file"
    if re.fullmatch(r"day-plan.*\.json", name, re.IGNORECASE):
        return "is a day-plan*.json file"

    if name.endswith(".json"):
        if path in ALLOWED_JSON_PATHS:
            return None
        if any(path.startswith(prefix) for prefix in ALLOWED_JSON_PREFIXES):
            return None
        return "is a JSON file outside the public allowlist"

    return None


def _read_state(state_path: Path) -> dict:
    try:
        doc = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _long_enough(names) -> list[str]:
    # Only names long enough to be meaningful; short names cause false positives
    # (and this script never prints the name either way, only the path).
    return sorted({n for n in names if len(n) >= MIN_NAME_LEN}, key=len, reverse=True)


def _all_task_entries(doc: dict) -> list:
    plan = doc.get("plan") or {}
    return list(plan.get("tasks") or []) + list(doc.get("done") or [])


def load_task_names(state_path: Path) -> list[str]:
    """Reads task and done-task names from a state file, in memory only.

    Returns an empty list if the state file does not exist or cannot be
    parsed; a missing state file is not itself an error, since a fresh clone
    or a test environment may not have one.
    """
    doc = _read_state(state_path)
    names = [t.get("name") for t in _all_task_entries(doc) if isinstance(t, dict)]
    return _long_enough(n for n in names if isinstance(n, str))


def load_folder_names(state_path: Path) -> list[str]:
    """Reads folder names from a state file, in memory only.

    A folder such as "Garden/Seeds" gives both the full path and each part
    ("Garden", "Seeds"), since either could appear on its own.
    """
    doc = _read_state(state_path)
    raw = [f for f in (doc.get("folders") or []) if isinstance(f, str)]
    raw += [t.get("folder") for t in _all_task_entries(doc)
            if isinstance(t, dict) and isinstance(t.get("folder"), str)]
    names: list[str] = []
    for folder in raw:
        names.append(folder)
        names.extend(part.strip() for part in folder.split("/"))
    return _long_enough(names)


def _text_contains_a_name(text: str | None, task_names: list[str],
                          folder_names: list[str] | None = None) -> str | None:
    if not text:
        return None
    for name in task_names:
        if name in text:
            return "contains a task name from the live state file"
    for name in folder_names or []:
        if name in text:
            return "contains a folder name from the live state file"
    return None


def content_reason(repo_root: Path, path: str, task_names: list[str], from_index: bool,
                   folder_names: list[str] | None = None) -> str | None:
    if not task_names and not folder_names:
        return None

    if from_index:
        # The file is staged: check the content that would actually be
        # committed, not whatever happens to be on disk right now.
        text = read_staged_blob(repo_root, path)
    else:
        try:
            text = (repo_root / path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            text = None  # binary or unreadable; not a text leak we can detect this way

    return _text_contains_a_name(text, task_names, folder_names)


def check(repo_root: Path, state_path: Path, staged_only: bool) -> list[tuple[str, str]]:
    staged = set(git_staged_files(repo_root))
    if staged_only:
        candidates = sorted(staged)
    else:
        candidates = sorted(set(git_tracked_files(repo_root)) | staged)

    task_names = load_task_names(state_path)
    folder_names = load_folder_names(state_path)

    offenses: list[tuple[str, str]] = []
    for path in candidates:
        reason = path_reason(path)
        if reason is None:
            # Staged files are checked against the index (what will actually
            # be committed); tracked-but-not-staged files are checked against
            # the working tree, since that is the only copy of them that exists.
            reason = content_reason(repo_root, path, task_names, from_index=path in staged,
                                    folder_names=folder_names)
        if reason is not None:
            offenses.append((path, reason))
    return offenses


def _git_bytes(args: list[str], repo_root: Path, stdin: bytes | None = None) -> bytes:
    result = subprocess.run(
        ["git", *args], cwd=repo_root, input=stdin, capture_output=True, check=True
    )
    return result.stdout


def _history_objects(repo_root: Path) -> dict[str, set[str]]:
    """Every object reachable from any ref, mapped to the paths it was stored under."""
    out = _git_bytes(["rev-list", "--all", "--objects"], repo_root).decode("utf-8", "replace")
    objects: dict[str, set[str]] = {}
    for line in out.splitlines():
        sha, _, path = line.partition(" ")
        if sha:
            objects.setdefault(sha, set())
            if path:
                objects[sha].add(_norm(path))
    return objects


def _blob_texts(repo_root: Path, shas: list[str]):
    """Yields (sha, text) for each blob among `shas`, all read with one `git cat-file --batch`."""
    if not shas:
        return
    data = _git_bytes(["cat-file", "--batch"], repo_root, stdin=("\n".join(shas) + "\n").encode())
    pos = 0
    while pos < len(data):
        end = data.index(b"\n", pos)
        header = data[pos:end].decode("utf-8", "replace").split()
        pos = end + 1
        if len(header) < 3:
            continue  # "<sha> missing" has no content after it
        sha, kind, size = header[0], header[1], int(header[2])
        body = data[pos:pos + size]
        pos += size + 1  # the content is followed by one newline
        if kind == "blob":
            yield sha, body.decode("utf-8", "ignore")


def _commit_messages(repo_root: Path):
    """Yields (commit id, message) for every commit reachable from any ref."""
    out = _git_bytes(["log", "--all", "--format=%H%x00%B%x00"], repo_root).decode("utf-8", "replace")
    parts = out.split("\x00")
    for i in range(0, len(parts) - 1, 2):
        sha = parts[i].strip()
        if sha:
            yield sha, parts[i + 1]


def check_history(repo_root: Path, state_path: Path) -> list[tuple[str, str]]:
    """Scans every blob, every path and every commit message reachable from any ref."""
    if not _git_bytes(["for-each-ref", "--count=1"], repo_root).strip():
        return []  # no commits yet, so there is no history to scan

    task_names = load_task_names(state_path)
    folder_names = load_folder_names(state_path)
    offenses: set[tuple[str, str]] = set()

    objects = _history_objects(repo_root)
    for paths in objects.values():
        for path in paths:
            reason = path_reason(path)
            if reason is not None:
                offenses.add((path, f"{reason}, somewhere in the history"))

    if task_names or folder_names:
        for sha, text in _blob_texts(repo_root, sorted(objects)):
            reason = _text_contains_a_name(text, task_names, folder_names)
            if reason is not None:
                for path in sorted(objects.get(sha) or {f"(blob {sha})"}):
                    offenses.add((path, f"{reason}, in file version {sha[:10]}"))
        for sha, message in _commit_messages(repo_root):
            reason = _text_contains_a_name(message, task_names, folder_names)
            if reason is not None:
                offenses.add((f"commit {sha}", f"its message {reason}"))

    return sorted(offenses)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--staged",
        action="store_true",
        help="check only staged files (used by the pre-commit hook), instead of tracked + staged",
    )
    parser.add_argument(
        "--history",
        action="store_true",
        help="scan every file version and commit message reachable from any branch or tag",
    )
    parser.add_argument(
        "--state",
        default="data/state.json",
        help="path to the state file to read task names from, relative to --repo-root "
        "(default: data/state.json; tests point this at a temporary fake file)",
    )
    parser.add_argument(
        "--repo-root",
        default=None,
        help="repo root to run git and resolve paths against (default: discovered with git rev-parse)",
    )
    args = parser.parse_args(argv)

    if args.repo_root:
        repo_root = Path(args.repo_root).resolve()
    else:
        repo_root = Path(
            _run_git(["rev-parse", "--show-toplevel"], Path.cwd()).strip()
        ).resolve()

    state_path = Path(args.state)
    if not state_path.is_absolute():
        state_path = repo_root / state_path

    if args.history:
        offenses = check_history(repo_root, state_path)
        heading = "check_public.py: the git history has these problems (rewrite it before pushing):"
    else:
        offenses = check(repo_root, state_path, args.staged)
        heading = "check_public.py: these files must not be committed to a public repo:"

    if offenses:
        print(heading, file=sys.stderr)
        for path, reason in offenses:
            print(f"  {path}  ({reason})", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
