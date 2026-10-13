"""The widget's clickable buttons and mouse clicks, run without a screen (Qt offscreen mode)."""
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QCursor, QMouseEvent
from PySide6.QtWidgets import QApplication, QPushButton

import focus_core as fc


@pytest.fixture
def widget(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))  # keep the real data folders untouched
    monkeypatch.setenv("FOCUSTOOL_DATA", str(tmp_path / "shared"))
    app = QApplication.instance() or QApplication([])
    import focus_tool as ft
    w = ft.FocusWidget(ft.load_settings())
    w.plan.tasks = [fc.Task("A", 10), fc.Task("B", 5), fc.Task("C", 5)]
    yield w
    w.panel.close()
    w.plan_window.close()
    w.close()


T0 = datetime(2026, 10, 5, 9, 0)


@pytest.fixture
def clock(monkeypatch):
    """Sets the time the widget reads, so the minutes spent on each task can be checked exactly."""
    import focus_tool as ft

    class Clock:
        t = T0

        @classmethod
        def now(cls):
            return cls.t

    monkeypatch.setattr(ft, "datetime", Clock)
    return Clock


def _button(row, text):
    return next(b for b in row.findChildren(QPushButton) if b.text() == text)


def test_hover_done_button_finishes_the_top_task(widget):
    _button(widget.btn_row, "✓").click()
    assert [t.name for t in widget.plan.tasks] == ["B", "C"]
    assert [d.name for d in widget.completed] == ["A"]


def test_time_up_done_button_finishes_the_running_task(widget):
    widget.start_task(widget.plan.tasks[1])
    _button(widget.ask_row, "✓ Done").click()
    assert [t.name for t in widget.plan.tasks] == ["A", "C"]
    assert not widget.timer.running


# ---------- v1.5: one click on the small circle ----------

def _mouse(w, kind, at: QPoint, button=Qt.LeftButton, buttons=Qt.LeftButton):
    """Hand one mouse event at screen position `at` to the widget's own handler."""
    ev = QMouseEvent(kind, QPointF(w.mapFromGlobal(at)), QPointF(at), button, buttons, Qt.NoModifier)
    {QEvent.MouseButtonPress: w.mousePressEvent, QEvent.MouseMove: w.mouseMoveEvent,
     QEvent.MouseButtonRelease: w.mouseReleaseEvent, QEvent.MouseButtonDblClick: w.mouseDoubleClickEvent}[kind](ev)


def _click(w, at: QPoint):
    _mouse(w, QEvent.MouseButtonPress, at)
    _mouse(w, QEvent.MouseButtonRelease, at, buttons=Qt.NoButton)


def test_one_click_on_the_small_circle_expands_it(widget):
    widget.set_mini(True)
    _click(widget, widget.geometry().center())
    assert not widget.mini
    assert widget.width() == int(widget.s["widget_width"])


def test_dragging_the_small_circle_moves_it_without_expanding(widget):
    widget.set_mini(True)
    start = widget.geometry().center()
    end = start + QPoint(40, 25)
    _mouse(widget, QEvent.MouseButtonPress, start)
    _mouse(widget, QEvent.MouseMove, end, button=Qt.NoButton)
    _mouse(widget, QEvent.MouseButtonRelease, end, buttons=Qt.NoButton)
    assert widget.mini
    assert widget.geometry().center() == end


def test_bottom_buttons_show_as_soon_as_the_circle_expands(widget):
    widget.set_mini(True)
    at = widget.geometry().center()
    QCursor.setPos(at)  # offscreen mode: a virtual cursor, the real mouse does not move
    _click(widget, at)
    assert not widget.btn_row.isHidden()


def test_second_click_of_a_double_click_does_not_also_open_the_panel(widget):
    widget.set_mini(True)
    at = widget.geometry().center()
    _click(widget, at)
    _mouse(widget, QEvent.MouseButtonDblClick, at)
    assert not widget.mini
    assert not widget.panel.isVisible()


def test_double_click_on_the_full_widget_still_opens_the_panel(widget):
    _mouse(widget, QEvent.MouseButtonDblClick, widget.geometry().center())
    assert widget.panel.isVisible()


# ---------- starting another task while one is paused or running ----------

def test_a_paused_task_does_not_block_starting_another(widget, clock):
    a, b = widget.plan.tasks[:2]
    widget.start_task(a)
    clock.t = T0 + timedelta(minutes=2)
    widget.toggle_run()  # pause A after 2 minutes
    clock.t = T0 + timedelta(minutes=5)
    widget.start_task(b)
    assert widget.active is b and widget.timer.task == "B"
    assert widget.timer.running and not widget.timer.paused
    assert a.spent_s == 120
    assert fc.load_state(widget.state_path).plan.tasks[0].spent_s == 120  # kept in the shared file too


def test_starting_another_task_pauses_the_running_one_and_keeps_its_time(widget, clock):
    a, b = widget.plan.tasks[:2]
    widget.start_task(a)
    clock.t = T0 + timedelta(minutes=4)
    widget.start_task(b)
    assert widget.active is b and not widget.timer.paused
    assert a.spent_s == 240


def test_a_paused_task_continues_where_it_stopped_and_done_counts_both_stretches(widget, clock):
    a, b = widget.plan.tasks[:2]  # A is 10 minutes
    widget.start_task(a)
    clock.t = T0 + timedelta(minutes=2)
    widget.start_task(b)
    clock.t = T0 + timedelta(minutes=5)
    widget.start_task(a)  # B pauses after 3 minutes, A picks up at 8 minutes left
    assert b.spent_s == 180 and a.spent_s == 0
    clock.t = T0 + timedelta(minutes=6)
    widget.timer.tick(clock.t)
    assert widget.timer.remaining_s == 7 * 60
    widget.done()
    assert (widget.completed[-1].name, widget.completed[-1].actual_min) == ("A", 3)
    widget.done(b)  # never restarted: logs the 3 minutes it already had
    assert (widget.completed[-1].name, widget.completed[-1].actual_min) == ("B", 3)


def test_panel_start_button_starts_the_selected_task_while_another_is_paused(widget, clock):
    widget.start_task(widget.plan.tasks[0])
    clock.t = T0 + timedelta(minutes=2)
    widget.toggle_run()  # A paused after 2 minutes
    widget.panel.show()
    widget.panel.list.setCurrentRow(1)
    assert widget.panel.btn_toggle.text() == "▶ Start selected"
    widget.panel.btn_toggle.click()
    assert widget.timer.task == "B" and not widget.timer.paused
    assert widget.panel.list.item(0).text() == "⏸ 10m · A   (2 min used)"
    widget.panel.list.setCurrentRow(1)
    assert widget.panel.btn_toggle.text() == "⏸ Pause"  # the selected task is the one on the timer


# ---------- the Day plan window has its own button ----------

def _key(w, key, text):
    from PySide6.QtGui import QKeyEvent
    w.keyPressEvent(QKeyEvent(QEvent.KeyPress, key, Qt.NoModifier, text))


def test_bottom_row_has_a_day_plan_button_next_to_the_panel_button(widget):
    labels = [b.text() for b in widget.btn_row.findChildren(QPushButton)]
    assert labels == ["▶", f"+{widget.s['add_minutes_step']}", "✓", "☰", "📅", "–", "✕"]
    assert "Day plan" in _button(widget.btn_row, "📅").toolTip()


def test_day_plan_button_opens_and_closes_the_day_plan_window(widget):
    assert not widget.plan_window.isVisible()
    _button(widget.btn_row, "📅").click()
    assert widget.plan_window.isVisible()
    assert widget.plan_window.windowTitle() == "Day plan"
    assert not widget.panel.isVisible()  # only the plan window opens
    _button(widget.btn_row, "📅").click()
    assert not widget.plan_window.isVisible()


def test_p_key_toggles_the_day_plan_window_and_d_is_still_done(widget):
    _key(widget, Qt.Key_P, "p")
    assert widget.plan_window.isVisible()
    _key(widget, Qt.Key_P, "p")
    assert not widget.plan_window.isVisible()
    _key(widget, Qt.Key_D, "d")
    assert [d.name for d in widget.completed] == ["A"]
    assert not widget.plan_window.isVisible()


def test_day_plan_window_shows_no_plan_yet_and_a_replan_button(widget):
    widget.toggle_day_plan()
    pw = widget.plan_window
    assert pw.plan_heading.text().startswith("No plan yet")
    assert pw.plan_list.count() == 0
    assert pw.b_replan.text() == "Re-plan now"


def test_task_panel_no_longer_has_the_plan_card(widget):
    p = widget.panel
    assert not hasattr(p, "plan_card") and not hasattr(p, "plan_list")
    assert "Re-plan now" not in [b.text() for b in p.findChildren(QPushButton)]
    from PySide6.QtWidgets import QLabel
    assert not any("Day plan" in lab.text() for lab in p.findChildren(QLabel))


def test_day_plan_window_size_and_position_are_kept_on_this_computer(widget, tmp_path):
    import json
    import focus_tool as ft
    widget.toggle_day_plan()
    widget.plan_window.resize(420, 333)
    widget.plan_window.move(150, 120)
    widget.save()
    saved = json.loads(widget.local_path.read_text(encoding="utf-8"))["geometry"]
    assert (saved["plan_w"], saved["plan_h"]) == (420, 333)
    assert (saved["plan_x"], saved["plan_y"]) == (150, 120)
    widget.plan_window.close()
    again = ft.FocusWidget(ft.load_settings())
    try:
        assert (again.plan_window.width(), again.plan_window.height()) == (420, 333)
        assert (again.plan_window.x(), again.plan_window.y()) == (150, 120)
    finally:
        again.panel.close()
        again.plan_window.close()
        again.close()


def test_tray_menu_day_plan_item_opens_the_window(widget, monkeypatch):
    from types import SimpleNamespace
    import focus_tool as ft
    fake_user32 = SimpleNamespace(RegisterHotKey=lambda *a: 1, UnregisterHotKey=lambda *a: 1)
    monkeypatch.setattr(ft, "ctypes", SimpleNamespace(windll=SimpleNamespace(user32=fake_user32)))
    tray = ft.TrayController(QApplication.instance(), widget, widget.s)
    try:
        act = next(a for a in tray.tray.contextMenu().actions() if a.text() == "Day plan...")
        act.trigger()
        assert widget.plan_window.isVisible()
        act.trigger()
        assert widget.plan_window.isVisible()  # the tray item always brings the window up
    finally:
        QApplication.instance().removeNativeEventFilter(tray.filter)
        tray.tray.hide()
