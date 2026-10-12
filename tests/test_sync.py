"""One task list shared between computers through the Box folder, run without a screen (Qt offscreen mode).

Each test gets an empty stand-in for this computer's own folder (%APPDATA%\\FocusTool) and for the
shared Box folder (focus-tool\\data). Two widgets made in one test act as two computers.
"""
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from PySide6.QtWidgets import QApplication

import focus_core as fc


@pytest.fixture
def folders(tmp_path, monkeypatch):
    """(this computer's own folder, the shared Box folder), both away from the real ones."""
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("FOCUSTOOL_DATA", str(tmp_path / "box" / "data"))
    return tmp_path / "appdata" / "FocusTool", tmp_path / "box" / "data"


@pytest.fixture
def make(folders):
    QApplication.instance() or QApplication([])
    import focus_tool as ft
    made = []

    def _make():
        w = ft.FocusWidget(ft.load_settings())
        made.append(w)
        return w

    yield _make
    for w in made:
        w.panel.close()
        w.close()


def _old_local_data(local: Path, tasks: str, done: list, dumps: str = "", log: str = ""):
    """The files the earlier version kept on one computer only."""
    local.mkdir(parents=True, exist_ok=True)
    fc.save_state(local / "state.json", fc.parse_plan(tasks, 45), 0, fc.TimerState(),
                  geometry={"x": 40, "y": 50, "w": 64, "h": 64, "mini": True}, done=done)
    if dumps:
        (local / "dump.txt").write_text(dumps, encoding="utf-8")
    if log:
        (local / "log.csv").write_text(log, encoding="utf-8")


# ---------- day to day ----------

def test_tasks_go_to_the_shared_folder_and_window_position_stays_on_this_computer(folders, make):
    local, shared = folders
    w = make()
    w.plan.tasks.append(fc.Task("Write intro", 45))
    w.changed()
    saved = fc.load_state(shared / "state.json")
    assert [t.name for t in saved.plan.tasks] == ["Write intro"]
    assert saved.geometry is None
    assert {"x", "y", "w", "h"} <= set(json.loads((local / "local.json").read_text(encoding="utf-8"))["geometry"])


def test_a_change_saved_by_the_other_computer_shows_up(make):
    here, there = make(), make()
    there.plan.tasks.append(fc.Task("From the laptop", 10))
    there.changed()
    there.start_task(there.plan.tasks[0])
    here.check_shared()
    assert [t.name for t in here.plan.tasks] == ["From the laptop"]
    assert here.timer.running and here.active is here.plan.tasks[0]


def test_an_idle_computer_never_writes_over_the_other_computers_changes(folders, make):
    _, shared = folders
    idle, busy = make(), make()
    busy.plan.tasks.append(fc.Task("New", 10))
    busy.changed()
    saved = (shared / "state.json").read_text(encoding="utf-8")
    idle.save()  # the 30-second autosave, and what Quit and Windows shutdown call
    idle.set_mini(True)
    assert (shared / "state.json").read_text(encoding="utf-8") == saved


def test_a_half_synced_shared_file_is_ignored(folders, make):
    _, shared = folders
    w = make()
    w.plan.tasks.append(fc.Task("Keep me", 10))
    w.changed()
    good = (shared / "state.json").read_text(encoding="utf-8")
    (shared / "state.json").write_text(good[: len(good) // 2], encoding="utf-8")
    w.check_shared()
    assert [t.name for t in w.plan.tasks] == ["Keep me"]


def test_settings_are_shared_and_start_from_this_computers_old_settings(folders):
    local, shared = folders
    local.mkdir(parents=True)
    (local / "settings.json").write_text(json.dumps({"default_block_minutes": 30}), encoding="utf-8")
    import focus_tool as ft
    assert ft.load_settings()["default_block_minutes"] == 30
    assert json.loads((shared / "settings.json").read_text(encoding="utf-8"))["default_block_minutes"] == 30


# ---------- first run of this version on each computer ----------

def test_first_computer_moves_its_list_into_the_shared_folder(folders, make):
    local, shared = folders
    _old_local_data(local, "Old task 10m", [fc.DoneTask("Finished", 5, 5, "2026-10-01 09:00")],
                    dumps="2026-10-01 09:05  an idea\n")
    before = (local / "state.json").read_text(encoding="utf-8")
    w = make()
    assert [t.name for t in w.plan.tasks] == ["Old task"]
    assert [d.name for d in w.completed] == ["Finished"]
    assert [d.text for d in fc.read_dumps(shared / "dump.txt")] == ["an idea"]
    assert w.mini  # the window state moved to this computer's own file
    assert (local / "state.json").read_text(encoding="utf-8") == before  # the old file stays as a backup


def test_second_computer_adds_its_own_list_to_the_shared_one_once(folders, make):
    local, shared = folders
    shared.mkdir(parents=True)
    header = "finished_at,task,estimate_min,actual_min\n"
    fc.save_state(shared / "state.json", fc.parse_plan("A 10m\nB 20m", 45), 0, fc.TimerState(),
                  done=[fc.DoneTask("D1", 5, 5, "2026-10-01 09:00")])
    (shared / "log.csv").write_text(header + "2026-10-01T09:00,D1,5,5\n", encoding="utf-8")
    _old_local_data(local, "B 20m\nC 5m",
                    [fc.DoneTask("D1", 5, 5, "2026-10-01 09:00"), fc.DoneTask("D2", 5, 7, "2026-10-01 11:00")],
                    log=header + "2026-10-01T09:00,D1,5,5\n2026-10-01T11:00,D2,5,7\n")
    w = make()
    assert [t.name for t in w.plan.tasks] == ["A", "B", "C"]
    assert [d.name for d in w.completed] == ["D1", "D2"]
    assert (shared / "log.csv").read_text(encoding="utf-8").splitlines()[1:] == \
           ["2026-10-01T09:00,D1,5,5", "2026-10-01T11:00,D2,5,7"]
    # Deleting C on purpose sticks: the old list is not merged in a second time.
    w.plan.tasks = [t for t in w.plan.tasks if t.name != "C"]
    w.changed()
    assert [t.name for t in make().plan.tasks] == ["A", "B"]


def test_an_unreadable_shared_file_is_not_overwritten_by_the_merge(folders, make):
    local, shared = folders
    shared.mkdir(parents=True)
    (shared / "state.json").write_text('{"version": 2, "pla', encoding="utf-8")
    _old_local_data(local, "C 5m", [])
    make()
    assert (shared / "state.json").read_text(encoding="utf-8") == '{"version": 2, "pla'
