"""Desktop features of state.json version 3: ids, dates, repeats, folders, and no thought dump.

Run without a screen (Qt offscreen mode). Each test gets empty stand-ins for this computer's own folder
(%APPDATA%\\FocusTool) and the shared Box folder, so the real data is never touched.
"""
import json
import os
import sys
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from PySide6.QtCore import QDate, QEvent, Qt
from PySide6.QtGui import QColor, QKeyEvent
from PySide6.QtWidgets import QApplication, QPushButton

import focus_core as fc


@pytest.fixture
def shared(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("FOCUSTOOL_DATA", str(tmp_path / "shared"))
    return tmp_path / "shared"


@pytest.fixture
def make(shared):
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


T0 = datetime(2026, 10, 12, 9, 0)  # a Monday


@pytest.fixture
def clock(monkeypatch):
    import focus_tool as ft

    class Clock:
        t = T0

        @classmethod
        def now(cls):
            return cls.t

    monkeypatch.setattr(ft, "datetime", Clock)
    return Clock


def _texts(lst):
    return [lst.item(i).text() for i in range(lst.count())]


def _saved(shared: Path) -> dict:
    return json.loads((shared / "state.json").read_text(encoding="utf-8"))


def _with_tasks(w, *tasks):
    w.plan.tasks = list(tasks)
    w.changed()
    w.panel.show()
    w.panel.refresh()
    return w.panel


# ---------- the thought dump is gone ----------

def test_no_thought_button_and_t_key_does_nothing(make, shared):
    import focus_tool as ft
    w = make()
    w.plan.tasks = [fc.Task("Alpha", 10)]
    assert all(b.text() != "💭" for b in w.btn_row.findChildren(QPushButton))
    assert not hasattr(ft, "DumpDialog") and not hasattr(w, "dump_thought") and not hasattr(w.panel, "dumps")
    w.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_T, Qt.NoModifier, "t"))
    assert [t.name for t in w.plan.tasks] == ["Alpha"]
    assert not (shared / "dump.txt").exists()


# ---------- order, ids and stamps ----------

def test_new_task_goes_to_the_front_with_an_id(make, shared):
    w = make()
    p = _with_tasks(w, fc.Task("Alpha", 10))
    p.new_task()
    p.ed_name.setText("Beta")
    p.add_task()
    assert [t.name for t in w.plan.tasks] == ["Beta", "Alpha"]
    saved = _saved(shared)
    assert saved["version"] == 3
    assert [t["name"] for t in saved["plan"]["tasks"]] == ["Beta", "Alpha"]
    assert saved["plan"]["tasks"][0]["id"] == w.plan.tasks[0].id
    assert saved["plan"]["tasks"][0]["updated_at"] != fc.EPOCH
    assert saved["order_updated_at"] != fc.EPOCH


def test_put_back_goes_to_the_front(make):
    w = make()
    p = _with_tasks(w, fc.Task("Alpha", 10), fc.Task("Beta", 5))
    w.done(w.plan.tasks[1])
    p.refresh()
    p.done_list.setCurrentRow(0)
    p.put_back()
    assert [t.name for t in w.plan.tasks] == ["Beta", "Alpha"]


def test_done_keeps_the_task_id_and_removal_leaves_a_tombstone(make, shared):
    w = make()
    a, b = fc.Task("Alpha", 10), fc.Task("Beta", 5)
    p = _with_tasks(w, a, b)
    w.done(a)
    assert w.completed[-1].id == a.id
    p.list.setCurrentRow(0)
    p.remove_selected()
    assert b.id in _saved(shared)["deleted"]


def test_timer_saves_the_task_id(make, shared, clock):
    w = make()
    a = fc.Task("Alpha", 10)
    _with_tasks(w, fc.Task("Beta", 5), a)
    w.start_task(a)
    timer = _saved(shared)["timer"]
    assert timer["task_id"] == a.id and timer["updated_at"] != fc.EPOCH
    again = make()
    assert again.active is not None and again.active.id == a.id


# ---------- dates ----------

def _set_date(check, edit, d: date):
    check.setChecked(True)
    edit.setDate(QDate(d.year, d.month, d.day))


def test_dated_task_moves_to_the_by_date_card(make, clock):
    w = make()
    p = _with_tasks(w, fc.Task("Alpha", 10), fc.Task("Beta", 5))
    assert not p.dated_card.isVisibleTo(p)
    p.list.setCurrentRow(1)
    _set_date(p.ed_deadline_on, p.ed_deadline, date(2026, 10, 16))
    assert w.plan.tasks[1].deadline == "2026-10-16"
    assert p.dated_card.isVisibleTo(p)
    assert _texts(p.dated_list) == ["📅 Fri 10/16 · 5m · Beta"]
    assert _texts(p.list) == ["10m · Alpha"]
    assert p.dated_list.dragDropMode() != p.list.dragDropMode()


def test_editor_date_round_trip(make, shared, clock):
    w = make()
    p = _with_tasks(w, fc.Task("Alpha", 10), fc.Task("Beta", 5))
    p.list.setCurrentRow(0)
    _set_date(p.ed_deadline_on, p.ed_deadline, date(2026, 10, 16))
    _set_date(p.ed_target_on, p.ed_target, date(2026, 10, 14))
    alpha = next(t for t in w.plan.tasks if t.name == "Alpha")
    assert (alpha.deadline, alpha.target) == ("2026-10-16", "2026-10-14")
    p.list.setCurrentRow(0)  # Beta: selecting it clears the dated list's selection
    assert p.dated_list.currentRow() == -1 and not p.ed_deadline_on.isChecked()
    p.dated_list.setCurrentRow(0)
    assert p.list.currentRow() == -1
    assert p.ed_deadline_on.isChecked() and p.ed_deadline.date() == QDate(2026, 10, 16)
    assert p.ed_target_on.isChecked() and p.ed_target.date() == QDate(2026, 10, 14)
    saved = next(t for t in _saved(shared)["plan"]["tasks"] if t["name"] == "Alpha")
    assert (saved["deadline"], saved["target"]) == ("2026-10-16", "2026-10-14")
    p.ed_deadline_on.setChecked(False)
    assert alpha.deadline is None and alpha.target == "2026-10-14"


def test_up_next_puts_dated_tasks_first(make):
    w = make()
    _with_tasks(w, fc.Task("Alpha", 10), fc.Task("Beta", 5, deadline="2026-10-20"))
    assert w.current_task().name == "Beta"


# ---------- repeats ----------

def test_repeat_editor_sets_every_n_days_from_today_and_disables_dates(make, clock):
    w = make()
    p = _with_tasks(w, fc.Task("Walk", 10, deadline="2026-10-20"))
    p.dated_list.setCurrentRow(0)
    p.ed_repeat.setCurrentIndex(p.ed_repeat.findText("Every N days"))
    p.ed_every.setValue(3)
    walk = w.plan.tasks[0]
    assert walk.repeat == {"kind": "every", "every": 3, "anchor": "2026-10-12"}
    assert walk.deadline is None and not p.ed_deadline_on.isEnabled()
    assert _texts(p.list) == ["🔁 10m · Walk"]


def test_repeat_done_keeps_the_task_asleep_and_logs_it(make, shared, clock):
    w = make()
    walk = fc.Task("Walk", 10, repeat={"kind": "daily"})
    _with_tasks(w, walk, fc.Task("Alpha", 20))
    w.start_task(walk)
    clock.t = T0 + timedelta(minutes=5)
    w.done()
    assert walk in w.plan.tasks and walk.last_done == "2026-10-12"
    assert (w.completed[-1].name, w.completed[-1].actual_min) == ("Walk", 5)
    assert "Walk" in (shared / "log.csv").read_text(encoding="utf-8")
    assert w.current_task().name == "Alpha"  # Up next skips the sleeping repeat
    assert _saved(shared)["plan"]["tasks"][0]["last_done"] == "2026-10-12"


def test_editing_keeps_a_repeat_rule_this_app_does_not_know(make, clock):
    w = make()
    rule = {"kind": "monthly", "day": 5}
    p = _with_tasks(w, fc.Task("Walk", 10, repeat=dict(rule)))
    p.list.setCurrentRow(0)
    p.ed_details.setPlainText("Around the lake")
    assert w.plan.tasks[0].details == "Around the lake" and w.plan.tasks[0].repeat == rule


def test_an_asleep_repeat_cannot_start(make, clock):
    w = make()
    walk = fc.Task("Walk", 10, repeat={"kind": "daily"}, last_done="2026-10-12")
    p = _with_tasks(w, walk)
    w.start_task(walk)
    assert not w.timer.running
    p.list.setCurrentRow(0)
    assert not p.btn_toggle.isEnabled()
    assert p.list.item(0).foreground().color() == QColor(w.s["colors"]["text_dim"])


# ---------- folders ----------

def _folder_tasks():
    return (fc.Task("Alpha", 10, folder="Puzzle"), fc.Task("Gamma", 10, folder="Other"),
            fc.Task("Beta", 10, folder="Puzzle/Tiles"), fc.Task("Delta", 10))


def test_folder_filter_shows_subfolders(make):
    w = make()
    p = _with_tasks(w, *_folder_tasks())
    assert "10m · Beta   📁 Puzzle/Tiles" in _texts(p.list)
    p.filter_combo.setCurrentIndex(p.filter_combo.findData("Puzzle"))
    assert _texts(p.list) == ["10m · Alpha", "10m · Beta"]
    p.filter_combo.setCurrentIndex(0)
    assert p.list.count() == 4


def test_moving_inside_a_filter_keeps_the_hidden_tasks_in_place(make):
    w = make()
    p = _with_tasks(w, *_folder_tasks())
    p.filter_combo.setCurrentIndex(p.filter_combo.findData("Puzzle"))
    p.list.setCurrentRow(1)
    p.move_selected(-1)
    assert [t.name for t in w.plan.tasks] == ["Beta", "Gamma", "Alpha", "Delta"]


def test_rename_folder_updates_labels(make, shared):
    w = make()
    w.folders = ["Puzzle", "Puzzle/Tiles"]
    p = _with_tasks(w, *_folder_tasks())
    w.rename_folder("Puzzle", "Tinker")
    assert "10m · Beta   📁 Tinker/Tiles" in _texts(p.list)
    assert "Tinker" in _saved(shared)["folders"] and "Puzzle" not in _saved(shared)["folders"]
    w.delete_folder("Tinker")
    assert all(t.folder in (None, "Other") for t in w.plan.tasks)


def test_new_folder_from_the_editor(make, monkeypatch):
    import focus_tool as ft
    w = make()
    p = _with_tasks(w, fc.Task("Alpha", 10))
    p.list.setCurrentRow(0)
    monkeypatch.setattr(ft.QInputDialog, "getText", staticmethod(lambda *a, **k: ("Puzzle", True)))
    p.ed_folder.setCurrentIndex(p.ed_folder.findData(ft.NEW_FOLDER))
    assert w.plan.tasks[0].folder == "Puzzle" and "Puzzle" in w.folders
    monkeypatch.setattr(ft.QInputDialog, "getText", staticmethod(lambda *a, **k: ("Tiles", True)))
    p.ed_folder.setCurrentIndex(p.ed_folder.findData(ft.NEW_SUBFOLDER))
    assert w.plan.tasks[0].folder == "Puzzle/Tiles" and "Puzzle/Tiles" in w.folders


# ---------- the shared file ----------

def test_a_version_2_box_file_is_migrated_and_saved_as_version_3(shared, make):
    shared.mkdir(parents=True)
    (shared / "state.json").write_text(json.dumps({
        "version": 2, "plan": {"tasks": [{"name": "Alpha", "estimate_min": 10, "details": "", "spent_s": 0.0}]},
        "current_index": 0, "timer": {"task": None, "estimate_min": 0, "running": False, "paused": False,
                                      "started_at": None, "banked_s": 0.0, "fired": [],
                                      "checkpoints": [0.5, 0.9, 1.0]},
        "done": []}), encoding="utf-8")
    w = make()
    saved = _saved(shared)
    assert saved["version"] == 3
    assert saved["plan"]["tasks"][0]["id"] == uuid.uuid5(fc.NS, "task:Alpha#0").hex == w.plan.tasks[0].id
    assert saved["order_updated_at"] != fc.EPOCH and saved["timer"]["updated_at"] != fc.EPOCH


def test_a_newer_version_file_is_never_overwritten(shared, make):
    shared.mkdir(parents=True)
    text = json.dumps({"version": 4, "plan": {"tasks": []}})
    (shared / "state.json").write_text(text, encoding="utf-8")
    w = make()
    w.plan.tasks.append(fc.Task("Alpha", 10))
    w.changed()
    w.check_shared()
    w.save()
    assert (shared / "state.json").read_text(encoding="utf-8") == text
    assert "newer Focus Tool" in w.status_message
    w.panel.show()
    assert "newer Focus Tool" in w.panel.status_label.text() and w.panel.status_label.isVisibleTo(w.panel)
