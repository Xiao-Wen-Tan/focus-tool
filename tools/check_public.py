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
  - any file whose content contains one of your task names (4+ characters),
    read from the live state file

It reads task names from the state file only in memory, to compare against
other files' content. It never prints a task name: only the path of the
offending file is printed, so this script is itself safe to run and to show
to anyone.

Usage:
    python tools/check_public.py              # checks tracked files + staged files
    python tools/check_public.py --staged      # checks only staged files (used by the pre-commit hook)
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


def load_task_names(state_path: Path) -> list[str]:
    """Reads task and done-task names from a state file, in memory only.

    Returns an empty list if the state file does not exist or cannot be
    parsed; a missing state file is not itself an error, since a fresh clone
    or a test environment may not have one.
    """
    try:
        doc = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []

    names: list[str] = []
    plan = doc.get("plan") or {}
    for task in plan.get("tasks") or []:
        name = task.get("name")
        if isinstance(name, str):
            names.append(name)
    for task in doc.get("done") or []:
        name = task.get("name")
        if isinstance(name, str):
            names.append(name)

    # Only names long enough to be meaningful; short names cause false positives
    # (and this script never prints the name either way, only the path).
    return sorted({n for n in names if len(n) >= MIN_NAME_LEN}, key=len, reverse=True)


def content_reason(repo_root: Path, path: str, task_names: list[str]) -> str | None:
    if not task_names:
        return None
    full = repo_root / path
    try:
        text = full.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None  # binary or unreadable; not a text leak we can detect this way

    for name in task_names:
        if name in text:
            return "contains a task name from the live state file"
    return None


def check(repo_root: Path, state_path: Path, staged_only: bool) -> list[tuple[str, str]]:
    if staged_only:
        candidates = git_staged_files(repo_root)
    else:
        candidates = sorted(set(git_tracked_files(repo_root)) | set(git_staged_files(repo_root)))

    task_names = load_task_names(state_path)

    offenses: list[tuple[str, str]] = []
    for path in candidates:
        reason = path_reason(path)
        if reason is None:
            reason = content_reason(repo_root, path, task_names)
        if reason is not None:
            offenses.append((path, reason))
    return offenses


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--staged",
        action="store_true",
        help="check only staged files (used by the pre-commit hook), instead of tracked + staged",
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

    offenses = check(repo_root, state_path, args.staged)

    if offenses:
        print("check_public.py: these files must not be committed to a public repo:", file=sys.stderr)
        for path, reason in offenses:
            print(f"  {path}  ({reason})", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
