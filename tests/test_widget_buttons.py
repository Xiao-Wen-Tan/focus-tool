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
