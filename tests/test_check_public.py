"""Tests for tools/check_public.py.

These build a throwaway git repo under a pytest tmp_path, with a fake state
file (made-up task names only, never real ones from data/state.json), and run
the script against it. They never touch the real data/ folder in this
project.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "tools" / "check_public.py"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    return root


@pytest.fixture
def fake_state(tmp_path: Path) -> Path:
    state_path = tmp_path / "fake-state.json"
    state_path.write_text(
        json.dumps(
            {
                "version": 3,
                "plan": {"tasks": [{"id": "a1", "name": "Alpha", "estimate_min": 25}]},
                "done": [{"id": "d1", "name": "Write intro", "actual_min": 40}],
            }
        ),
        encoding="utf-8",
    )
    return state_path


def run_check(repo: Path, state: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo-root",
            str(repo),
            "--state",
            str(state),
            *extra,
        ],
        capture_output=True,
        text=True,
    )


def test_clean_repo_passes(repo: Path, fake_state: Path):
    (repo / "README.md").write_text("Nothing sensitive here.\n", encoding="utf-8")
    _git(repo, "add", "README.md")

    result = run_check(repo, fake_state, "--staged")

    assert result.returncode == 0
    assert result.stdout.strip() == ""
    assert result.stderr.strip() == ""


def test_path_under_data_is_rejected(repo: Path, fake_state: Path):
    (repo / "data").mkdir()
    (repo / "data" / "state.json").write_text("{}", encoding="utf-8")
    _git(repo, "add", "-f", "data/state.json")

    result = run_check(repo, fake_state, "--staged")

    assert result.returncode == 1
    assert "data/state.json" in result.stderr


def test_disallowed_json_is_rejected(repo: Path, fake_state: Path):
    (repo / "weird-config.json").write_text(
        json.dumps({"hello": "world"}), encoding="utf-8"
    )
    _git(repo, "add", "weird-config.json")

    result = run_check(repo, fake_state, "--staged")

    assert result.returncode == 1
    assert "weird-config.json" in result.stderr


def test_allowlisted_json_is_accepted(repo: Path, fake_state: Path):
    (repo / "settings.default.json").write_text(
        json.dumps({"opacity": 0.9}), encoding="utf-8"
    )
    _git(repo, "add", "settings.default.json")

    result = run_check(repo, fake_state, "--staged")

    assert result.returncode == 0


def test_file_containing_a_task_name_is_rejected_without_printing_the_name(
    repo: Path, fake_state: Path
):
    leaky = repo / "notes.txt"
    leaky.write_text("Reminder: Write intro by Friday.\n", encoding="utf-8")
    _git(repo, "add", "notes.txt")

    result = run_check(repo, fake_state, "--staged")

    assert result.returncode == 1
    assert "notes.txt" in result.stderr
    # The whole point of this script is to never print the task name itself.
    assert "Write intro" not in result.stdout
    assert "Write intro" not in result.stderr


def test_short_task_names_are_ignored(repo: Path, fake_state: Path):
    # "Alpha" (5 chars) is a task name in the fake state file, but a file that
    # merely contains the word "Al" (2 chars) should not trip the check: only
    # names of 4+ characters are matched, to avoid flooding on short words.
    harmless = repo / "misc.txt"
    harmless.write_text("Al went to the store.\n", encoding="utf-8")
    _git(repo, "add", "misc.txt")

    result = run_check(repo, fake_state, "--staged")

    assert result.returncode == 0


def test_tracked_file_is_checked_without_staged_flag(repo: Path, fake_state: Path):
    (repo / "data").mkdir()
    (repo / "data" / "notes.json").write_text("{}", encoding="utf-8")
    _git(repo, "add", "-f", "data/notes.json")
    _git(repo, "commit", "-q", "-m", "add tracked file")

    # Not using --staged: should still catch the already-committed (tracked) file.
    result = run_check(repo, fake_state)

    assert result.returncode == 1
    assert "data/notes.json" in result.stderr


def test_missing_state_file_does_not_crash(repo: Path, tmp_path: Path):
    missing_state = tmp_path / "does-not-exist.json"
    (repo / "notes.txt").write_text("Just some ordinary notes.\n", encoding="utf-8")
    _git(repo, "add", "notes.txt")

    result = run_check(repo, missing_state, "--staged")

    assert result.returncode == 0


def test_staged_content_is_checked_even_if_disk_copy_is_cleaned_afterwards(
    repo: Path, fake_state: Path
):
    # A file is staged while it still contains a task name, then the
    # working-tree copy is rewritten (e.g. by the author scrubbing it) before
    # the commit runs. The check must still block it, because what matters
    # is what is actually staged for commit, not what is currently on disk.
    leaky = repo / "draft.txt"
    leaky.write_text("Plan: Write intro this afternoon.\n", encoding="utf-8")
    _git(repo, "add", "draft.txt")

    # Rewrite the working-tree copy without the task name, but do not re-add it.
    leaky.write_text("Nothing to see here.\n", encoding="utf-8")

    result = run_check(repo, fake_state, "--staged")

    assert result.returncode == 1
    assert "draft.txt" in result.stderr
    assert "Write intro" not in result.stdout
    assert "Write intro" not in result.stderr


# ---------- --history: every blob and commit message reachable from any ref ----------

@pytest.fixture
def fake_state_with_folders(tmp_path: Path) -> Path:
    state_path = tmp_path / "fake-state-folders.json"
    state_path.write_text(
        json.dumps(
            {
                "version": 3,
                "plan": {"tasks": [{"id": "a1", "name": "Alpha", "folder": "Garden/Seeds"}]},
                "folders": ["Garden", "Garden/Seeds"],
                "done": [{"id": "d1", "name": "Write intro"}],
            }
        ),
        encoding="utf-8",
    )
    return state_path


def _commit_file(repo: Path, name: str, text: str, message: str) -> None:
    (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", message)


def test_history_clean_repo_passes(repo: Path, fake_state_with_folders: Path):
    _commit_file(repo, "README.md", "Nothing sensitive here.\n", "first")

    result = run_check(repo, fake_state_with_folders, "--history")

    assert result.returncode == 0, result.stderr


def test_history_finds_a_task_name_in_a_deleted_file(repo: Path, fake_state_with_folders: Path):
    _commit_file(repo, "notes.txt", "Remember to Write intro.\n", "add notes")
    _git(repo, "rm", "-q", "notes.txt")
    _git(repo, "commit", "-q", "-m", "remove notes")

    # The working tree is clean now, so the normal check passes...
    assert run_check(repo, fake_state_with_folders).returncode == 0
    # ...but the name is still in the history.
    result = run_check(repo, fake_state_with_folders, "--history")

    assert result.returncode == 1
    assert "notes.txt" in result.stderr
    assert "Write intro" not in result.stdout + result.stderr


def test_history_finds_a_name_on_another_branch(repo: Path, fake_state_with_folders: Path):
    _commit_file(repo, "README.md", "Hello.\n", "first")
    _git(repo, "checkout", "-q", "-b", "side")
    _commit_file(repo, "side.txt", "Alpha is next.\n", "side work")
    _git(repo, "checkout", "-q", "-")

    result = run_check(repo, fake_state_with_folders, "--history")

    assert result.returncode == 1
    assert "side.txt" in result.stderr
    assert "Alpha" not in result.stdout + result.stderr


def test_history_finds_a_folder_name(repo: Path, fake_state_with_folders: Path):
    _commit_file(repo, "folders.txt", "Filed under Garden/Seeds.\n", "folders")

    result = run_check(repo, fake_state_with_folders, "--history")

    assert result.returncode == 1
    assert "folders.txt" in result.stderr
    assert "Garden" not in result.stdout + result.stderr


def test_history_finds_a_name_in_a_commit_message_and_prints_only_the_id(
    repo: Path, fake_state_with_folders: Path
):
    _commit_file(repo, "README.md", "Hello.\n", "Finished Write intro today")
    commit_id = _git(repo, "rev-parse", "HEAD").stdout.strip()

    result = run_check(repo, fake_state_with_folders, "--history")

    assert result.returncode == 1
    assert commit_id in result.stderr
    assert "Write intro" not in result.stdout + result.stderr
    assert "Finished" not in result.stdout + result.stderr


def test_history_flags_a_data_path_in_an_old_commit(repo: Path, fake_state_with_folders: Path):
    (repo / "data").mkdir()
    (repo / "data" / "state.json").write_text("{}", encoding="utf-8")
    _git(repo, "add", "-f", "data/state.json")
    _git(repo, "commit", "-q", "-m", "oops")
    _git(repo, "rm", "-q", "--cached", "data/state.json")
    _git(repo, "commit", "-q", "-m", "undo")

    result = run_check(repo, fake_state_with_folders, "--history")

    assert result.returncode == 1
    assert "data/state.json" in result.stderr


def test_history_works_with_a_separate_git_dir(tmp_path: Path, fake_state_with_folders: Path):
    work = tmp_path / "work"
    gitdir = tmp_path / "meta.git"
    work.mkdir()
    _git(work, "init", "-q", f"--separate-git-dir={gitdir}")
    _git(work, "config", "user.email", "test@example.com")
    _git(work, "config", "user.name", "Test")
    _commit_file(work, "notes.txt", "Write intro\n", "add")

    result = run_check(work, fake_state_with_folders, "--history")

    assert result.returncode == 1
    assert "notes.txt" in result.stderr


def test_install_hooks_copies_the_hook_into_the_git_hooks_dir(tmp_path: Path):
    work = tmp_path / "work"
    gitdir = tmp_path / "meta.git"
    work.mkdir()
    _git(work, "init", "-q", f"--separate-git-dir={gitdir}")
    installer = SCRIPT.parent / "install_hooks.py"

    result = subprocess.run(
        [sys.executable, str(installer), "--repo-root", str(work)],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    hook = gitdir / "hooks" / "pre-commit"
    assert hook.exists()
    assert "check_public.py" in hook.read_text(encoding="utf-8")
