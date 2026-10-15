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
from PySide6.QtCore import QDate, QEvent, QPoint, Qt
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
    """Row texts, without the empty row that holds the open task's editor."""
    import focus_tool as ft
    return [lst.item(i).text() for i in range(lst.count()) if lst.item(i).data(Qt.UserRole) != ft.EDITOR_ROW]


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

def test_new_task_goes_to_the_end_with_an_id(make, shared):
    w = make()
    p = _with_tasks(w, fc.Task("Alpha", 10))
    p.ed_add.setText("Beta")
    p.add_task()
    assert [t.name for t in w.plan.tasks] == ["Alpha", "Beta"]
    saved = _saved(shared)
    assert saved["version"] == 5
    assert [t["name"] for t in saved["plan"]["tasks"]] == ["Alpha", "Beta"]
    assert saved["plan"]["tasks"][1]["id"] == w.plan.tasks[1].id
    assert saved["plan"]["tasks"][1]["priority"] == "med"
    assert saved["plan"]["tasks"][1]["updated_at"] != fc.EPOCH
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

def _select(p, name):
    """Open the task called `name`, as a click on its row does."""
    p.list.setCurrentRow(next(i for i in range(p.list.count()) if name in p.list.item(i).text()))


def test_sort_by_date_or_in_my_order(make, clock):
    import focus_tool as ft
    w = make()
    p = _with_tasks(w, fc.Task("Alpha", 10), fc.Task("Beta", 5))
    p.sort_combo.setCurrentIndex(p.sort_combo.findText("By date"))
    p.list.setCurrentRow(1)
    p.ed_deadline.pick(date(2026, 10, 16))
    assert w.plan.tasks[1].deadline == "2026-10-16"
    assert _texts(p.list) == ["📅 Due Fri 10/16 · 5m · Beta", "──  no date, by tag and your order  ──", "10m · Alpha"]
    assert p.list.dragDropMode() == ft.QAbstractItemView.NoDragDrop
    assert p.selected_task().name == "Beta"  # still open after it moved
    p.sort_combo.setCurrentIndex(p.sort_combo.findText("My order"))
    assert _texts(p.list) == ["10m · Alpha", "📅 Due Fri 10/16 · 5m · Beta"]
    assert p.list.dragDropMode() == ft.QAbstractItemView.InternalMove
    assert w.ui["panel_sort"] == "order"


def test_my_order_moves_dated_tasks_too(make, clock):
    w = make()
    p = _with_tasks(w, fc.Task("Alpha", 10), fc.Task("Beta", 5, deadline="2026-10-16"))
    p.sort_combo.setCurrentIndex(0)
    p.list.setCurrentRow(1)
    p.move_selected(-1)
    assert [t.name for t in w.plan.tasks] == ["Beta", "Alpha"]
    p.sort_combo.setCurrentIndex(1)
    p.list.setCurrentRow(0)
    assert not p.b_up.isEnabled()  # By date sets the order, so the arrows rest
    p.move_selected(1)
    assert [t.name for t in w.plan.tasks] == ["Beta", "Alpha"]


def test_editor_date_round_trip(make, shared, clock):
    w = make()
    p = _with_tasks(w, fc.Task("Alpha", 10), fc.Task("Beta", 5))
    p.list.setCurrentRow(0)
    p.ed_deadline.pick(date(2026, 10, 16))
    p.ed_target.pick(date(2026, 10, 14))
    alpha = next(t for t in w.plan.tasks if t.name == "Alpha")
    assert (alpha.deadline, alpha.target) == ("2026-10-16", "2026-10-14")
    _select(p, "Beta")
    assert p.selected_task().name == "Beta" and p.ed_deadline.value() is None
    _select(p, "Alpha")
    assert p.ed_deadline.value() == date(2026, 10, 16) and p.ed_target.value() == date(2026, 10, 14)
    assert p.ed_deadline.text() == "📅 Fri 10/16/2026  ▾"
    saved = next(t for t in _saved(shared)["plan"]["tasks"] if t["name"] == "Alpha")
    assert (saved["deadline"], saved["target"]) == ("2026-10-16", "2026-10-14")
    p.ed_deadline.pick(None)
    assert alpha.deadline is None and alpha.target == "2026-10-14"


def test_calendar_opens_on_today_with_today_marked(make, clock):
    w = make()
    p = _with_tasks(w, fc.Task("Alpha", 10))
    p.list.setCurrentRow(0)
    assert p.ed_deadline.isEnabled() and p.ed_target.isEnabled()  # one click opens the calendar
    p.ed_deadline.open_calendar()
    from PySide6.QtWidgets import QCalendarWidget
    cal = p.ed_deadline.popup.findChild(QCalendarWidget)
    assert cal.selectedDate() == QDate(2026, 10, 12)
    assert cal.dateTextFormat(QDate(2026, 10, 12)).fontWeight() > 400
    today = next(b for b in p.ed_deadline.popup.findChildren(QPushButton) if b.text().startswith("Today"))
    today.click()
    assert w.plan.tasks[0].deadline == "2026-10-12" and p.ed_deadline.popup is None


def test_up_next_puts_dated_tasks_first(make):
    w = make()
    _with_tasks(w, fc.Task("Alpha", 10), fc.Task("Beta", 5, deadline="2026-10-20"))
    assert w.current_task().name == "Beta"


# ---------- repeats ----------

def test_repeat_editor_sets_every_n_days_from_today_and_disables_dates(make, clock):
    w = make()
    p = _with_tasks(w, fc.Task("Walk", 10, deadline="2026-10-20", target="2026-10-18"))
    p.list.setCurrentRow(0)
    p.ed_repeat.setCurrentIndex(p.ed_repeat.findText("Every N days"))
    p.ed_every.setValue(3)
    walk = w.plan.tasks[0]
    assert walk.repeat == {"kind": "every", "every": 3, "anchor": "2026-10-12"}
    assert walk.deadline == "2026-10-20" and p.ed_deadline.isEnabled()  # repeats until its deadline
    assert walk.target is None and not p.ed_target.isEnabled()
    assert _texts(p.list) == ["📅 Due Mon 10/12 · 🔁 until Tue 10/20 · 10m · Walk"]  # due today, the anchor


def test_a_repeat_with_a_deadline_leaves_the_list_the_day_after(make, shared, clock):
    w = make()
    walk = fc.Task("Walk", 10, repeat={"kind": "daily"}, deadline="2026-10-12")
    _with_tasks(w, walk, fc.Task("Alpha", 5))
    assert walk in w.plan.tasks
    clock.t = T0 + timedelta(days=1)
    w.on_tick()
    assert [t.name for t in w.plan.tasks] == ["Alpha"]
    assert walk.id in _saved(shared)["deleted"]


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
    assert saved["version"] == 5
    assert saved["plan"]["tasks"][0]["id"] == uuid.uuid5(fc.NS, "task:Alpha#0").hex == w.plan.tasks[0].id
    assert saved["order_updated_at"] != fc.EPOCH and saved["timer"]["updated_at"] != fc.EPOCH


def test_a_newer_version_file_is_never_overwritten(shared, make):
    shared.mkdir(parents=True)
    text = json.dumps({"version": 6, "plan": {"tasks": []}})
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


# ---------- the All tasks window: add line, unfolding editor, several at once, wheel, full screen ----------

def test_enter_in_the_add_line_adds_to_the_folder_shown_and_opens_the_task(make):
    w = make()
    p = _with_tasks(w, *_folder_tasks())
    p.filter_combo.setCurrentIndex(p.filter_combo.findData("Puzzle"))
    p.ed_add.setText("Omega")
    p.ed_add.returnPressed.emit()
    t = w.plan.tasks[-1]
    assert (t.name, t.folder, t.estimate_min) == ("Omega", "Puzzle", int(w.s["default_block_minutes"]))
    assert p.selected_task() is t and p.editor.isVisibleTo(p) and p.ed_name.text() == "Omega"
    assert p.ed_add.text() == ""


def test_the_editor_unfolds_under_its_task_and_a_second_click_folds_it(make):
    import focus_tool as ft
    w = make()
    p = _with_tasks(w, fc.Task("Alpha", 10), fc.Task("Beta", 5), fc.Task("Gamma", 5))
    p.list.setCurrentRow(1)
    assert p.list.item(2).data(Qt.UserRole) == ft.EDITOR_ROW and p.editor.isVisibleTo(p)
    assert p.list.itemWidget(p.list.item(2)).isAncestorOf(p.editor)
    p.list.setCurrentRow(0)  # opening another task moves the editor under it
    assert p.list.item(1).data(Qt.UserRole) == ft.EDITOR_ROW and p.ed_name.text() == "Alpha"
    assert sum(p.list.item(i).data(Qt.UserRole) == ft.EDITOR_ROW for i in range(p.list.count())) == 1
    p._picks_at_press = p._picks  # a click on the open task that did not change the selection
    p._item_clicked(p.list.item(0))
    assert p.selected_task() is None and not p.editor.isVisibleTo(p)
    assert all(p.list.item(i).data(Qt.UserRole) != ft.EDITOR_ROW for i in range(p.list.count()))


def test_a_sync_while_editing_keeps_the_editor_and_its_text(make):
    w = make()
    p = _with_tasks(w, fc.Task("Alpha", 10))
    p.list.setCurrentRow(0)
    p.ed_details.setPlainText("first step")
    p.refresh()
    assert p.editor.isVisibleTo(p) and p.ed_details.toPlainText() == "first step"
    assert w.plan.tasks[0].details == "first step"


def test_pick_several_tasks_and_give_them_a_date_and_a_folder(make, clock):
    w = make()
    w.folders = ["Puzzle"]
    walk = fc.Task("Walk", 10, repeat={"kind": "daily"})
    p = _with_tasks(w, fc.Task("Alpha", 10), fc.Task("Beta", 5), walk)
    rows = [p.list.item(i) for i in range(3)]  # picked one by one, as Ctrl+click does
    for item in rows:
        item.setSelected(True)
    assert p.bulk.isVisibleTo(p) and not p.editor.isVisibleTo(p)
    assert p.bulk_heading.text().startswith("3 tasks picked")
    p.bulk_target.pick(date(2026, 10, 14))
    assert [t.target for t in w.plan.tasks] == ["2026-10-14", "2026-10-14", None]  # a repeat has no aim date
    p.bulk_deadline.pick(date(2026, 10, 20))
    assert all(t.deadline == "2026-10-20" for t in w.plan.tasks)
    assert p.bulk_deadline.text() == "Set...  ▾"
    assert len(p.selected_ids()) == 3  # still picked after the list was rebuilt
    p.bulk_folder.setCurrentIndex(p.bulk_folder.findData("Puzzle"))
    assert all(t.folder == "Puzzle" for t in w.plan.tasks) and p.bulk_folder.currentIndex() == 0
    p.bulk_deadline.pick(None)
    assert all(t.deadline is None for t in w.plan.tasks)
    p.fold()
    assert not p.bulk.isVisibleTo(p)


def test_the_wheel_does_not_change_a_box_you_have_not_clicked(make):
    from PySide6.QtCore import QPoint, QPointF
    from PySide6.QtGui import QWheelEvent
    w = make()
    w.folders = ["Puzzle", "Other"]
    p = _with_tasks(w, fc.Task("Alpha", 10))
    p.list.setCurrentRow(0)
    before = (p.ed_folder.currentIndex(), p.ed_repeat.currentIndex(), p.filter_combo.currentIndex())
    for box in (p.ed_folder, p.ed_repeat, p.filter_combo):
        ev = QWheelEvent(QPointF(5, 5), QPointF(5, 5), QPoint(0, 0), QPoint(0, -120), Qt.NoButton,
                         Qt.NoModifier, Qt.NoScrollPhase, False)
        QApplication.sendEvent(box, ev)
    assert (p.ed_folder.currentIndex(), p.ed_repeat.currentIndex(), p.filter_combo.currentIndex()) == before
    assert w.plan.tasks[0].folder is None


def test_full_screen_keys(make):
    w = make()
    p = _with_tasks(w, fc.Task("Alpha", 10))
    p.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_F11, Qt.NoModifier))
    assert p.isFullScreen() and w.ui["panel_full"] is True
    p.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
    assert not p.isFullScreen() and w.ui["panel_full"] is False
    p.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_F11, Qt.NoModifier))
    assert p.isFullScreen()
    p.close()
    w.open_panel()
    assert p.isFullScreen()  # it opens the way it was left


def test_completed_is_folded_until_opened(make):
    w = make()
    p = _with_tasks(w, fc.Task("Alpha", 10), fc.Task("Beta", 5))
    w.done(w.plan.tasks[0])
    p.refresh()
    assert not p.done_box.isVisibleTo(p) and p.b_completed.text() == "▸ ✅ Completed (1)"
    p.b_completed.click()
    assert p.done_box.isVisibleTo(p) and p.b_completed.text() == "▾ ✅ Completed (1)"
    assert w.ui["done_open"] is True


def test_no_separate_add_card_or_by_date_card(make):
    w = make()
    p = w.panel
    assert not hasattr(p, "dated_list") and not hasattr(p, "dated_card") and not hasattr(p, "b_new")
    from PySide6.QtWidgets import QLabel
    old = ("Add a task", "📅 By date", "Your list, in order")
    assert not any(lab.text() in old for lab in p.findChildren(QLabel))


# ---------- hard deadline and aim date look different; deadline time ----------

def test_list_labels_tell_a_hard_deadline_from_an_aim_date(make, clock):
    import focus_tool as ft
    w = make()
    p = _with_tasks(w, fc.Task("Grant", 30, deadline="2026-10-16", deadline_time="17:00"),
                    fc.Task("Paper", 20, target="2026-10-19"),
                    fc.Task("Both", 10, deadline="2026-10-16", target="2026-10-14"),
                    fc.Task("Walk", 5, repeat={"kind": "daily"}, deadline="2026-10-30"))
    rows = {p.list.item(i).text(): p.list.item(i) for i in range(p.list.count())}
    assert set(rows) == {"📅 Due Fri 10/16 5:00pm · 30m · Grant", "🎯 Aim Mon 10/19 · 20m · Paper",
                         "📅 Due Fri 10/16, 🎯 aim Wed 10/14 · 10m · Both",
                         "📅 Due Mon 10/12 · 🔁 until Fri 10/30 · 5m · Walk"}
    red = QColor(ft.DUE_RED)
    assert rows["📅 Due Fri 10/16 5:00pm · 30m · Grant"].foreground().color() == red
    assert rows["📅 Due Fri 10/16, 🎯 aim Wed 10/14 · 10m · Both"].foreground().color() == red
    assert rows["🎯 Aim Mon 10/19 · 20m · Paper"].foreground().color() != red
    assert rows["📅 Due Mon 10/12 · 🔁 until Fri 10/30 · 5m · Walk"].foreground().color() == red  # due today
    assert "Deadline Fri 10/16 at 5:00pm" in rows["📅 Due Fri 10/16 5:00pm · 30m · Grant"].toolTip()


@pytest.mark.parametrize("typed, saved, shown", [("17:00", "17:00", "5:00pm"), ("5pm", "17:00", "5:00pm"),
                                                 ("9:30 am", "09:30", "9:30am"), ("12am", "00:00", "12:00am"),
                                                 ("0930", "09:30", "9:30am"), ("", None, "")])
def test_parse_time_text(typed, saved, shown):
    import focus_tool as ft
    assert ft.parse_time_text(typed) == saved
    assert fc.fmt_hm(saved) == shown


@pytest.mark.parametrize("typed", ["25:00", "13pm", "noon", "5:75", "0pm"])
def test_parse_time_text_refuses_what_is_not_a_time(typed):
    import focus_tool as ft
    with pytest.raises(ValueError):
        ft.parse_time_text(typed)


def test_editor_sets_and_clears_the_deadline_time(make, shared, clock):
    w = make()
    p = _with_tasks(w, fc.Task("Alpha", 10))
    p.list.setCurrentRow(0)
    assert not p.ed_deadline_time.isEnabled()  # no deadline yet, so no time
    p.ed_deadline.pick(date(2026, 10, 16))
    assert p.ed_deadline_time.isEnabled()
    p.ed_deadline_time.setText("5pm")
    p.ed_deadline_time.editingFinished.emit()
    alpha = w.plan.tasks[0]
    assert alpha.deadline_time == "17:00" and p.ed_deadline_time.text() == "5:00pm"
    assert _saved(shared)["plan"]["tasks"][0]["deadline_time"] == "17:00"
    assert _texts(p.list)[0] == "📅 Due Fri 10/16 5:00pm · 10m · Alpha"
    p.ed_deadline_time.setText("later")  # not a time: marked, and the saved time stays
    p.ed_deadline_time.editingFinished.emit()
    assert alpha.deadline_time == "17:00" and "Not a time" in p.ed_deadline_time.toolTip()
    p.ed_deadline.pick(None)  # no deadline: the time goes too
    assert alpha.deadline is None and alpha.deadline_time is None and p.ed_deadline_time.text() == ""


def test_repeating_task_has_no_deadline_time(make, clock):
    import focus_tool as ft
    w = make()
    p = _with_tasks(w, fc.Task("Walk", 5, deadline="2026-10-30", deadline_time="08:00"))
    p.list.setCurrentRow(0)
    assert p.ed_deadline_time.text() == "8:00am"
    p.ed_repeat.setCurrentIndex(ft.REPEAT_KINDS.index("daily"))
    t = w.plan.tasks[0]
    assert t.repeat == {"kind": "daily"} and t.deadline == "2026-10-30" and t.deadline_time is None
    assert not p.ed_deadline_time.isEnabled()


def test_day_plan_blocks_show_due_and_aim(make, clock):
    w = make()
    w.plan.tasks = [fc.Task("Grant", 30, id="g"), fc.Task("Paper", 20, id="p")]
    plan = {"date": "2026-10-12", "blocks": [
        {"task_id": "g", "name": "Grant", "start": "09:00", "end": "09:30", "minutes": 30, "kind": "dated",
         "date": "2026-10-16", "date_kind": "deadline", "time": "17:00"},
        {"task_id": "p", "name": "Paper", "start": "09:30", "end": "09:50", "minutes": 20, "kind": "dated",
         "date": "2026-10-19", "date_kind": "target", "time": None}], "warnings": []}
    w.shown_plan = lambda: plan
    w.plan_window.refresh_plan()
    texts = [w.plan_window.plan_list.item(i).text() for i in range(w.plan_window.plan_list.count())]
    assert texts == ["09:00-09:30  Grant  (30m)  · 📅 Due Fri 10/16 5:00pm",
                     "09:30-09:50  Paper  (20m)  · 🎯 Aim Mon 10/19"]


# ---------- the All tasks window minimizes ----------

def test_no_own_full_screen_or_minimize_buttons(make):
    w = make()
    p = _with_tasks(w, fc.Task("Alpha", 10))
    assert not any(b.text() in ("⛶", "–") for b in p.findChildren(QPushButton))  # Windows' own buttons do it


def test_minimize_and_restore(make):
    w = make()
    p = _with_tasks(w, fc.Task("Alpha", 10))
    flags = p.windowFlags()
    assert flags & Qt.WindowType_Mask == Qt.Window  # a normal window, so it has a taskbar button
    p.minimize()
    assert p.isMinimized()
    w.open_panel()  # Enter or the list button on the widget
    assert not p.isMinimized() and p.isVisible()


def test_minimized_from_full_screen_comes_back_full_screen(make):
    w = make()
    p = _with_tasks(w, fc.Task("Alpha", 10))
    p.toggle_full_screen()
    p.minimize()
    assert p.isMinimized()
    w.open_panel()
    assert not p.isMinimized() and p.isFullScreen()


# ---------- folder notes ----------

def _show_folder(p, path):
    p.filter_combo.setCurrentIndex(p.filter_combo.findData(path))


def test_notes_box_shows_only_for_one_folder(make):
    w = make()
    p = _with_tasks(w, fc.Task("Draft", 10, folder="Grant"))
    assert not p.notes_box.isVisibleTo(p)
    _show_folder(p, "Grant")
    assert p.notes_box.isVisibleTo(p) and p.b_notes.text() == "▾ 📝 Notes for Grant"
    _show_folder(p, "")
    assert not p.notes_box.isVisibleTo(p)


def test_folder_summary_is_saved_and_stays_with_its_folder(make, shared, clock):
    w = make()
    w.folders = ["Grant", "Home"]
    p = _with_tasks(w, fc.Task("Draft", 10, folder="Grant"))
    _show_folder(p, "Grant")
    p.notes_text.setPlainText("Waiting on letters")
    assert p._notes_timer.isActive()
    p.flush_notes()
    assert w.folder_notes["Grant"]["text"] == "Waiting on letters"
    saved = _saved(shared)["folder_notes"]["Grant"]
    assert saved["text"] == "Waiting on letters" and saved["updated_at"] != fc.EPOCH
    _show_folder(p, "Home")
    assert p.notes_text.toPlainText() == ""
    p.notes_text.setPlainText("Paint the fence")
    _show_folder(p, "Grant")  # changing the folder saves what was typed first
    assert w.folder_notes["Home"]["text"] == "Paint the fence"
    assert p.notes_text.toPlainText() == "Waiting on letters"


def test_typing_in_the_notes_survives_a_refresh(make):
    w = make()
    p = _with_tasks(w, fc.Task("Draft", 10, folder="Grant"))
    _show_folder(p, "Grant")
    p.notes_text.setPlainText("half typed")
    p.refresh()  # a sync arriving while typing
    assert p.notes_text.toPlainText() == "half typed"


def test_progress_notes_add_edit_delete(make, shared, clock):
    w = make()
    p = _with_tasks(w, fc.Task("Draft", 10, folder="Grant"))
    _show_folder(p, "Grant")
    p.notes_add.setText("Budget sent")
    p.add_progress()
    clock.t = T0 + timedelta(days=1)
    p.notes_add.setText("Letters asked for")
    p.add_progress()
    texts = [p.notes_log.item(i).text() for i in range(p.notes_log.count())]
    assert texts == ["Tue 10/13   Letters asked for", "Mon 10/12   Budget sent"]  # newest first
    assert [(x["at"], x["folder"]) for x in _saved(shared)["progress"]] == [("2026-10-12 09:00", "Grant"),
                                                                            ("2026-10-13 09:00", "Grant")]
    assert p.b_notes.text() == "▾ 📝 Notes for Grant (2 progress)"
    p.notes_log.setCurrentRow(1)
    p.edit_progress("Budget sent to Sam")
    assert p.notes_log.item(1).text() == "Mon 10/12   Budget sent to Sam"
    gone = next(x["id"] for x in w.progress if x["text"] == "Letters asked for")
    p.notes_log.setCurrentRow(0)
    p.delete_progress()
    assert [x["text"] for x in w.progress] == ["Budget sent to Sam"]
    assert gone in _saved(shared)["deleted"]


def test_renaming_a_folder_moves_its_notes(make):
    w = make()
    w.folders = ["Grant"]
    p = _with_tasks(w, fc.Task("Draft", 10, folder="Grant"))
    _show_folder(p, "Grant")
    p.notes_text.setPlainText("Waiting on letters")
    p.notes_add.setText("Budget sent")
    p.add_progress()
    w.rename_folder("Grant", "NSF grant")
    _show_folder(p, "NSF grant")
    assert p.notes_text.toPlainText() == "Waiting on letters"
    assert p.notes_log.count() == 1


# ---------- ranking: drag sign, row arrows, open on release ----------

def _row_point(p, row, part):
    """A point on a task row: the 'up' or 'down' arrow, the drag 'sign', or the 'text'."""
    r = p.list.visualItemRect(p.list.item(row))
    if part in ("up", "down"):
        return p.list.arrow_rect(r, -1 if part == "up" else 1).center()
    return QPoint(r.left() + 10, r.center().y()) if part == "sign" else r.center()


def test_row_arrows_move_without_opening(make):
    from PySide6.QtTest import QTest
    w = make()
    p = _with_tasks(w, fc.Task("Alpha", 10), fc.Task("Beta", 5, deadline="2026-10-16"), fc.Task("Gamma", 5))
    p.sort_combo.setCurrentIndex(0)
    assert p.list.reorderable
    QTest.mouseClick(p.list.viewport(), Qt.LeftButton, Qt.NoModifier, _row_point(p, 2, "up"))
    assert [t.name for t in w.plan.tasks] == ["Alpha", "Gamma", "Beta"]
    QTest.mouseClick(p.list.viewport(), Qt.LeftButton, Qt.NoModifier, _row_point(p, 0, "down"))
    assert [t.name for t in w.plan.tasks] == ["Gamma", "Alpha", "Beta"]
    assert p.selected_ids() == [] and p._editor_item is None  # nothing opened


def test_task_opens_on_release_not_on_press(make):
    from PySide6.QtTest import QTest
    w = make()
    p = _with_tasks(w, fc.Task("Alpha", 10), fc.Task("Beta", 5))
    at = _row_point(p, 1, "text")
    QTest.mousePress(p.list.viewport(), Qt.LeftButton, Qt.NoModifier, at)
    assert p._editor_item is None  # still folded while the button is down, so a drag can start
    QTest.mouseRelease(p.list.viewport(), Qt.LeftButton, Qt.NoModifier, at)
    assert p.selected_task().name == "Beta" and p._editor_item is not None


def test_by_date_has_no_row_arrows(make):
    from PySide6.QtTest import QTest
    w = make()
    p = _with_tasks(w, fc.Task("Alpha", 10), fc.Task("Beta", 5))
    p.sort_combo.setCurrentIndex(1)
    assert not p.list.reorderable
    QTest.mouseClick(p.list.viewport(), Qt.LeftButton, Qt.NoModifier, _row_point(p, 1, "up"))
    assert [t.name for t in w.plan.tasks] == ["Alpha", "Beta"]


def test_editing_dates_or_folder_keeps_the_list_where_it_was(make, clock):
    w = make()
    p = _with_tasks(w, *[fc.Task(f"Task {i}", 10) for i in range(30)])
    p.resize(560, 600)
    QApplication.processEvents()
    p.list.setCurrentRow(20)
    p.list.scrollToItem(p.list.item(20))
    QApplication.processEvents()
    bar = p.list.verticalScrollBar()
    at = bar.value()
    assert at > 0
    p.ed_deadline.pick(date(2026, 10, 16))
    QApplication.processEvents()
    assert w.plan.tasks[20].deadline == "2026-10-16"
    assert bar.value() == at  # no jump to the top
    w.add_folder("Grant")
    p._fill_folder_combos()
    p.ed_folder.setCurrentIndex(p.ed_folder.findData("Grant"))
    QApplication.processEvents()
    assert w.plan.tasks[20].folder == "Grant"
    assert bar.value() == at


def test_repeat_days_are_pills_that_show_the_choice(make, clock):
    import focus_tool as ft
    w = make()
    p = _with_tasks(w, fc.Task("Walk", 10))
    p.list.setCurrentRow(0)
    p.ed_repeat.setCurrentIndex(ft.REPEAT_KINDS.index("days"))
    assert p.ed_days[0].isChecked()  # today, Monday, is picked to start with
    wed = p.ed_days[2]
    assert isinstance(wed, QPushButton) and wed.isCheckable() and wed.objectName() == "pill"
    wed.click()
    assert w.plan.tasks[0].repeat == {"kind": "days", "days": ["mon", "wed"]}
    assert p.ed_days[2].isChecked() and not p.ed_days[1].isChecked()


def test_by_date_lists_repeats_by_their_next_day(make, clock):
    w = make()  # today is Mon 10/12
    p = _with_tasks(w, fc.Task("Grant", 30, deadline="2026-10-14"),
                    fc.Task("Gym", 30, repeat={"kind": "days", "days": ["wed"]}),
                    fc.Task("Walk", 5, repeat={"kind": "daily"}),
                    fc.Task("Read", 20))
    p.sort_combo.setCurrentIndex(1)
    assert _texts(p.list) == ["📅 Due Mon 10/12 · 🔁 · 5m · Walk", "📅 Due Wed 10/14 · 30m · Grant",
                              "📅 Due Wed 10/14 · 🔁 · 30m · Gym", "──  no date, by tag and your order  ──", "20m · Read"]


# ---------- priority tags and the No folder filter ----------

def test_my_order_groups_high_med_low_and_shows_the_tag(make):
    w = make()
    p = _with_tasks(w, fc.Task("Alpha", 10, priority="low"), fc.Task("Beta", 10), fc.Task("Gamma", 10, priority="high"),
                    fc.Task("Delta", 10))
    p.sort_combo.setCurrentIndex(0)
    assert _texts(p.list) == ["High · 10m · Gamma", "10m · Beta", "10m · Delta", "Low · 10m · Alpha"]


def test_editor_priority_buttons_set_the_tag(make, shared):
    w = make()
    p = _with_tasks(w, fc.Task("Alpha", 10), fc.Task("Beta", 10))
    p.sort_combo.setCurrentIndex(0)
    p.list.setCurrentRow(1)
    assert p.selected_task().name == "Beta"
    checked = [b.text() for b in p.ed_priority.buttons() if b.isChecked()]
    assert checked == ["Med"]
    high = next(b for b in p.ed_priority.buttons() if b.text() == "High")
    high.click()
    assert w.plan.tasks[1].priority == "high"
    assert _texts(p.list)[0] == "High · 10m · Beta"  # it moved to the top group
    assert _saved(shared)["plan"]["tasks"][1]["priority"] == "high"


def test_row_arrows_move_only_inside_the_tag(make):
    w = make()
    p = _with_tasks(w, fc.Task("Alpha", 10), fc.Task("Beta", 10, priority="high"), fc.Task("Gamma", 10))
    p.sort_combo.setCurrentIndex(0)
    ids = {t.name: t.id for t in w.plan.tasks}
    p.move_task(ids["Alpha"], -1)  # Alpha is first among the Medium tasks; Beta (High) is above it
    assert [t.name for t in w.plan.tasks] == ["Alpha", "Beta", "Gamma"]
    p.move_task(ids["Gamma"], -1)
    assert [t.name for t in w.plan.tasks] == ["Gamma", "Beta", "Alpha"]
    assert _texts(p.list) == ["High · 10m · Beta", "10m · Gamma", "10m · Alpha"]


def test_no_folder_filter_shows_tasks_without_a_folder(make):
    w = make()
    p = _with_tasks(w, *_folder_tasks())
    p.filter_combo.setCurrentIndex(p.filter_combo.findData(fc.NO_FOLDER))
    assert p.filter_combo.currentText() == "No folder"
    assert _texts(p.list) == ["10m · Delta"]
    assert not p.notes_box.isVisibleTo(p)  # no folder, so no folder notes
    p.ed_add.setText("Omega")
    p.ed_add.returnPressed.emit()
    assert w.plan.tasks[-1].folder is None
    assert _texts(p.list) == ["10m · Delta", f"{w.s['default_block_minutes']}m · Omega"]
