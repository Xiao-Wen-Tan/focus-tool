#!/usr/bin/env python3
"""Installs tools/pre-commit as this repo's git pre-commit hook.

The hook runs tools/check_public.py --staged before every commit, so a commit
that would put your task data into this public repo is refused.

It asks git where the hooks folder is (`git rev-parse --git-path hooks`)
instead of assuming .git/hooks, because this repo keeps its git folder
outside Box (a separate git dir). Run it once per computer, from the repo:

    python tools/install_hooks.py
"""
from __future__ import annotations

import argparse
import stat
import subprocess
import sys
from pathlib import Path

HOOK_SOURCE = Path(__file__).resolve().parent / "pre-commit"


def hooks_dir(repo_root: Path) -> Path:
    out = subprocess.run(
        ["git", "rev-parse", "--git-path", "hooks"],
        cwd=repo_root, capture_output=True, text=True, check=True,
    ).stdout.strip()
    path = Path(out)
    return path if path.is_absolute() else (repo_root / path).resolve()


def install(repo_root: Path) -> Path:
    target_dir = hooks_dir(repo_root)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "pre-commit"
    # Write with Unix line endings so the shell in Git for Windows can run it.
    target.write_bytes(HOOK_SOURCE.read_bytes().replace(b"\r\n", b"\n"))
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=".", help="the repo folder (default: the current folder)")
    args = parser.parse_args(argv)
    target = install(Path(args.repo_root).resolve())
    print(f"Installed the pre-commit hook at {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
