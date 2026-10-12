"""Focus Tool v1.5: a small always-on-top time-visibility widget for Windows.

Run from source:  python focus_tool.py
Build an exe:     .\\build.ps1

All the timer/plan logic lives in focus_core.py (tested). This file is only
the window, the task panel, the tray icon, the hotkey, sounds and file plumbing.

Data (v1.5): the task list, completed list, timer, thoughts, log and settings live
in focus-tool\\data inside Box, so every computer that runs the app from Box shares
them. Each computer re-reads that folder every few seconds to pick up the other
computer's changes, and writes it only when you change something, so an idle
computer never writes an old list over a newer one. The window position stays on
each computer, in %APPDATA%\\FocusTool\\local.json.

Model: plan.tasks holds the tasks still to do, in the order you chose.
FocusWidget.active is the task the timer runs on; it can sit anywhere in the
list (v1.4 dropped pinning it to the top). You pick which task to start;
starting another one pauses the task on the timer and keeps its time in
Task.spent_s, so it continues where it stopped when you start it again. Done
moves a task to the completed list (kept in state.json) and logs it. Nothing
starts on its own; when time is up the timer keeps counting and the widget
asks for more time.
"""
from __future__ import annotations

import csv
import ctypes
import ctypes.wintypes as wt
import json
import os
import shutil
import sys
import time
import winsound
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import (QAbstractNativeEventFilter, QEasingCurve, QPoint, QPropertyAnimation, QRect, QRectF,
                            Qt, QTimer, Property, Signal)
from PySide6.QtGui import QAction, QColor, QCursor, QFont, QFontMetrics, QIcon, QPainter, QPen
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QButtonGroup, QDialog, QFrame,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu, QPlainTextEdit,
                               QPushButton, QScrollArea, QSystemTrayIcon, QVBoxLayout, QWidget)

import focus_core as fc

APP_NAME = "FocusTool"
CHECK_EVERY_MS = 250
AUTOSAVE_EVERY_MS = 30_000
SYNC_CHECK_MS = 2000      # how often to look for changes the other computer saved to Box
CELEBRATE_MS = 3000
TIME_PRESETS = (5, 10, 15, 25, 30, 45, 60, 90)
MINI_SIZE = 64            # the shrunk widget is a circle this many pixels wide
DETAILS_DEFAULT_H = 140   # starting height of the details box in the task panel
DETAILS_MIN_H = 60


# ---------------------------------------------------------------- paths & settings

def resource_dir() -> Path:
    """Folder holding assets and settings.default.json (source tree or PyInstaller bundle)."""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def app_home() -> Path:
    """The focus-tool folder in Box: where focus_tool.py sits, or the folder above build\\ for the exe."""
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        return exe_dir.parent if exe_dir.name.lower() == "build" else exe_dir
    return Path(__file__).resolve().parent


def shared_dir() -> Path:
    """Tasks, completed list, timer, thoughts, log and settings: one copy that Box syncs to every computer.
    FOCUSTOOL_DATA points it somewhere else (the tests use this)."""
    d = Path(os.environ.get("FOCUSTOOL_DATA") or app_home() / "data")
    d.mkdir(parents=True, exist_ok=True)
    return d


def local_dir() -> Path:
    """This computer only: window position (local.json), and the files of versions before 1.5, kept as a backup."""
    d = Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _file_sig(path: Path) -> tuple[int, int] | None:
    """(modified time, size) of a file, or None if it is missing. A change means someone saved it."""
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


def _read_json(path: Path) -> dict:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _write_json(path: Path, d: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=2), encoding="utf-8")
    tmp.replace(path)


def merge_old_local_data(local: Path, shared: Path) -> dict | None:
    """Fold this computer's files from before v1.5 into the shared Box folder. Runs once per computer.

    The first computer copies its list over; a later one adds only the tasks, completed tasks, thoughts
    and log rows the shared folder does not have yet. The old files are left as they are, as a backup.
    Returns the old window position. Raises ValueError if the shared list exists but cannot be read
    yet (Box still syncing it), so nothing is written and the merge runs again at the next start.
    """
    old_path = local / "state.json"
    if not old_path.exists():
        return None
    old = fc.load_state(old_path)
    new_path = shared / "state.json"
    merged = fc.merge_states(fc.parse_state(new_path.read_text(encoding="utf-8")), old) if new_path.exists() else old
    fc.save_state(new_path, merged.plan, merged.current_index, merged.timer, done=merged.done)
    for name in ("dump.txt", "log.csv"):
        src, dst = local / name, shared / name
        if src.exists():
            have = dst.read_text(encoding="utf-8") if dst.exists() else ""
            dst.write_text(fc.merge_lines(have, src.read_text(encoding="utf-8")), encoding="utf-8")
    return old.geometry


def load_settings() -> dict:
    default_path = resource_dir() / "settings.default.json"
    user_path = shared_dir() / "settings.json"
    defaults = json.loads(default_path.read_text(encoding="utf-8"))
    if not user_path.exists():
        old = local_dir() / "settings.json"  # settings from before v1.5 lived on each computer
        shutil.copy(old if old.exists() else default_path, user_path)
    try:
        user = json.loads(user_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return defaults
    merged = {**defaults, **user}
    merged["colors"] = {**defaults["colors"], **user.get("colors", {})}
    return merged


# ---------------------------------------------------------------- global hotkey (Win32)

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN = 0x1, 0x2, 0x4, 0x8
WM_HOTKEY = 0x0312
HOTKEY_ID = 0xF0C5


def parse_hotkey(text: str) -> tuple[int, int]:
    mods, key = 0, 0
    for part in text.lower().split("+"):
        part = part.strip()
        if part == "ctrl":
            mods |= MOD_CONTROL
        elif part == "alt":
            mods |= MOD_ALT
        elif part == "shift":
            mods |= MOD_SHIFT
        elif part == "win":
            mods |= MOD_WIN
        elif len(part) == 1:
            key = ord(part.upper())
    return mods, key


class HotkeyFilter(QAbstractNativeEventFilter):
    def __init__(self, callback):
        super().__init__()
        self.callback = callback

    def nativeEventFilter(self, event_type, message):
        if event_type == "windows_generic_MSG":
            msg = wt.MSG.from_address(int(message))
            if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                self.callback()
                return True, 0
        return False, 0


# ---------------------------------------------------------------- startup shortcut

def startup_shortcut_path() -> Path:
    return Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup" / f"{APP_NAME}.lnk"


def set_start_at_login(enabled: bool) -> None:
    link = startup_shortcut_path()
    if not enabled:
        if link.exists():
            link.unlink()
        return
    import win32com.client  # pywin32, only needed here
    shell = win32com.client.Dispatch("WScript.Shell")
    sc = shell.CreateShortcut(str(link))
    if getattr(sys, "frozen", False):
        sc.TargetPath = sys.executable
        sc.Arguments = ""
        sc.WorkingDirectory = str(Path(sys.executable).parent)
    else:
        sc.TargetPath = str(Path(sys.executable).with_name("pythonw.exe"))
        sc.Arguments = f'"{Path(__file__).resolve()}"'
        sc.WorkingDirectory = str(Path(__file__).resolve().parent)
    sc.IconLocation = str(resource_dir() / "assets" / "icon.ico")
    sc.Save()


# ---------------------------------------------------------------- styling helpers

def panel_stylesheet(c: dict) -> str:
    return f"""
    QWidget {{ background: {c['background']}; color: {c['text']}; font-family: 'Segoe UI'; font-size: 10pt; }}
    QScrollArea {{ border: none; }}
    QFrame#card {{ background: white; border: 1px solid {c['border']}; border-radius: 14px; }}
    QFrame#card QWidget {{ background: transparent; }}
    QLabel#title {{ font-size: 16pt; font-weight: 700; }}
    QLabel#heading {{ font-size: 10.5pt; font-weight: 600; color: {c['text']}; }}
    QLabel#hint {{ color: {c['text_dim']}; font-size: 9pt; }}
    QListWidget, QPlainTextEdit, QLineEdit {{
        background: {c['background']}; border: 1px solid {c['border']}; border-radius: 10px; padding: 6px; }}
    QFrame#card QWidget#grip {{ background: {c['border']}; border-radius: 3px; }}
    QFrame#card QWidget#grip:hover {{ background: {c['button_hover']}; }}
    QListWidget::item {{ padding: 6px 8px; border-radius: 8px; }}
    QListWidget::item:selected {{ background: {c['disk_track']}; color: {c['text']}; }}
    QPushButton {{ background: {c['button']}; color: {c['button_text']}; border: none; border-radius: 10px;
        padding: 6px 12px; }}
    QPushButton:hover {{ background: {c['button_hover']}; }}
    QPushButton#primary {{ background: {c['disk_remaining']}; color: #2F4A44; font-weight: 600; }}
    QPushButton#primary:hover {{ background: #7FCDB6; }}
    QPushButton#pill {{ background: {c['background']}; border: 1px solid {c['border']}; border-radius: 12px;
        padding: 3px 10px; font-size: 9pt; }}
    QPushButton#pill:checked {{ background: {c['disk_remaining']}; border-color: {c['disk_remaining']};
        color: #2F4A44; font-weight: 600; }}
    QPushButton#quiet {{ background: transparent; color: {c['text_dim']}; }}
    QPushButton#quiet:hover {{ background: {c['button']}; }}
    """


def card(title: str, hint: str | None = None) -> tuple[QFrame, QVBoxLayout]:
    f = QFrame()
    f.setObjectName("card")
    lay = QVBoxLayout(f)
    lay.setContentsMargins(14, 12, 14, 12)
    lay.setSpacing(8)
    h = QLabel(title)
    h.setObjectName("heading")
    lay.addWidget(h)
    if hint:
        hl = QLabel(hint)
        hl.setObjectName("hint")
        hl.setWordWrap(True)
        lay.addWidget(hl)
    return f, lay


class TimePicker(QWidget):
    """A row of pill buttons: pick a duration by clicking, no typing."""
    changed = Signal(int)

    def __init__(self, presets=TIME_PRESETS):
        super().__init__()
        self.presets = list(presets)
        self.lay = QHBoxLayout(self)
        self.lay.setContentsMargins(0, 0, 0, 0)
        self.lay.setSpacing(4)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self._extra: QPushButton | None = None
        self.lay.addStretch()  # stays last; pills are inserted before it
        for m in self.presets:
            self._add_pill(m)
        self.group.idClicked.connect(lambda mid: self.changed.emit(mid))

    def _add_pill(self, minutes: int) -> QPushButton:
        b = QPushButton(f"{minutes}m")
        b.setObjectName("pill")
        b.setCheckable(True)
        b.setCursor(Qt.PointingHandCursor)
        self.group.addButton(b, minutes)
        self.lay.insertWidget(self.lay.count() - 1, b)
        return b

    def value(self) -> int:
        return self.group.checkedId() if self.group.checkedId() > 0 else self.presets[0]

    def set_value(self, minutes: int):
        if self._extra is not None and minutes != self.group.id(self._extra):
            self.group.removeButton(self._extra)
            self._extra.deleteLater()
            self._extra = None
        if self.group.button(minutes) is None:
            self._extra = self._add_pill(minutes)
        self.group.button(minutes).setChecked(True)


# ---------------------------------------------------------------- dialogs

class DragHandle(QWidget):
    """A thin bar under a box. Drag it up or down to make the box shorter or taller."""
    moved = Signal(int)  # new height of the target

    def __init__(self, target: QWidget):
        super().__init__()
        self.target = target
        self.setObjectName("grip")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedHeight(6)
        self.setCursor(Qt.SizeVerCursor)
        self.setToolTip("Drag to make the details box taller or shorter")
        self._start: tuple[int, int] | None = None

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._start = (int(e.globalPosition().y()), self.target.height())

    def mouseMoveEvent(self, e):
        if self._start is not None:
            y0, h0 = self._start
            h = max(DETAILS_MIN_H, h0 + int(e.globalPosition().y()) - y0)
            self.target.setFixedHeight(h)
            self.moved.emit(h)

    def mouseReleaseEvent(self, e):
        self._start = None


class DumpDialog(QDialog):
    def __init__(self, parent, colors: dict):
        super().__init__(parent)
        self.setWindowTitle("Dump a thought")
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        self.setStyleSheet(panel_stylesheet(colors))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.addWidget(QLabel("Write it down, then back to the task. Enter to save."))
        self.edit = QLineEdit()
        self.edit.setMinimumWidth(440)
        self.edit.returnPressed.connect(self.accept)
        layout.addWidget(self.edit)

    def text(self) -> str:
        return self.edit.text().strip()


# ---------------------------------------------------------------- task panel

class TaskPanel(QWidget):
    """One card to add or edit a task, the ordered list, the completed list, and dumped thoughts."""

    def __init__(self, widget: "FocusWidget"):
        super().__init__(None, Qt.Tool | Qt.WindowStaysOnTopHint)
        self.w = widget
        self.setWindowTitle("Focus Tool")
        self.setWindowIcon(QIcon(str(resource_dir() / "assets" / "icon.ico")))
        self.setStyleSheet(panel_stylesheet(widget.s["colors"]))
        self.resize(int(widget.ui.get("panel_w", 560)), int(widget.ui.get("panel_h", 860)))
        self._updating = False
        self._editing = False  # False: the task card adds a new task. True: it edits the selected one.

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll)
        body = QWidget()
        scroll.setWidget(body)
        root = QVBoxLayout(body)
        root.setContentsMargins(18, 16, 18, 18)
        root.setSpacing(12)

        # -- header
        head = QHBoxLayout()
        title = QLabel("🌱 Tasks")
        title.setObjectName("title")
        head.addWidget(title)
        head.addStretch()
        self.btn_toggle = QPushButton("▶ Start")
        self.btn_toggle.setObjectName("primary")
        self.btn_toggle.clicked.connect(self.toggle_selected)
        head.addWidget(self.btn_toggle)
        b_done = QPushButton("✓ Done")
        b_done.setToolTip("Finish the task selected in the list")
        b_done.clicked.connect(self.done_selected)
        head.addWidget(b_done)
        root.addLayout(head)

        # -- one card: add a new task, or edit the selected one
        task_card, tc = card("Add a task")
        self.task_heading = task_card.findChild(QLabel, "heading")
        self.task_hint = QLabel()
        self.task_hint.setObjectName("hint")
        self.task_hint.setWordWrap(True)
        tc.addWidget(self.task_hint)
        self.ed_name = QLineEdit()
        self.ed_name.setPlaceholderText("What is the task?")
        self.ed_name.returnPressed.connect(self._enter_in_name)
        self.ed_name.textEdited.connect(self._apply_editor)
        tc.addWidget(self.ed_name)
        self.ed_time = TimePicker()
        self.ed_time.changed.connect(self._apply_editor)
        tc.addWidget(self.ed_time)
        self.ed_details = QPlainTextEdit()
        self.ed_details.setPlaceholderText("Details (optional): the first step, a link, what done looks like")
        self.ed_details.setFixedHeight(max(DETAILS_MIN_H, int(self.w.ui.get("details_h", DETAILS_DEFAULT_H))))
        self.ed_details.textChanged.connect(self._apply_editor)
        tc.addWidget(self.ed_details)
        grip = DragHandle(self.ed_details)
        grip.moved.connect(self._details_resized)
        tc.addWidget(grip)
        trow = QHBoxLayout()
        trow.addStretch()
        self.b_new = QPushButton("+ New task")
        self.b_new.setToolTip("Leave this task and add a different one")
        self.b_new.clicked.connect(self.new_task)
        trow.addWidget(self.b_new)
        self.b_add = QPushButton("+ Add to list")
        self.b_add.setObjectName("primary")
        self.b_add.clicked.connect(self.add_task)
        trow.addWidget(self.b_add)
        tc.addLayout(trow)
        root.addWidget(task_card)

        # -- the list
        list_card, lst = card("Your list, in order", "Click a task to select it: edit it above, Start it, or mark it "
                                                      "Done. Drag to reorder, or use the arrows.")
        self.list = QListWidget()
        self.list.setDragDropMode(QAbstractItemView.InternalMove)
        self.list.setDefaultDropAction(Qt.MoveAction)
        self.list.setMinimumHeight(150)
        self.list.currentRowChanged.connect(self._load_editor)
        self.list.model().rowsMoved.connect(self._rows_moved)
        lst.addWidget(self.list)
        lrow = QHBoxLayout()
        for label, tip, fn in [("↑", "Move up", lambda: self.move_selected(-1)),
                               ("↓", "Move down", lambda: self.move_selected(1)),
                               ("Remove", "Remove the selected task without finishing it", self.remove_selected)]:
            b = QPushButton(label)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            lrow.addWidget(b)
        lrow.addStretch()
        lst.addLayout(lrow)
        root.addWidget(list_card)

        # -- completed
        done_card, dn = card("✅ Completed", "Kept across restarts. Newest first.")
        self.done_list = QListWidget()
        self.done_list.setMinimumHeight(90)
        dn.addWidget(self.done_list)
        drow = QHBoxLayout()
        b_back = QPushButton("↩ Put back in list")
        b_back.clicked.connect(self.put_back)
        drow.addWidget(b_back)
        drow.addStretch()
        b_clear = QPushButton("Clear completed")
        b_clear.setObjectName("quiet")
        b_clear.setToolTip("Empties this card. The record stays in log.csv.")
        b_clear.clicked.connect(self.clear_done)
        drow.addWidget(b_clear)
        dn.addLayout(drow)
        root.addWidget(done_card)

        # -- thoughts
        th_card, th = card("💭 Dumped thoughts", "Everything you typed with the thought button. Turn one into a task, or let it go.")
        self.dumps = QListWidget()
        self.dumps.setMinimumHeight(90)
        th.addWidget(self.dumps)
        throw = QHBoxLayout()
        b1 = QPushButton("→ Make it a task")
        b1.clicked.connect(self.dump_to_task)
        b2 = QPushButton("Delete")
        b2.clicked.connect(self.delete_dump)
        b3 = QPushButton("Open dump.txt")
        b3.setObjectName("quiet")
        b3.clicked.connect(lambda: os.startfile(str(self.w.dump_path)) if self.w.dump_path.exists() else None)
        throw.addWidget(b1)
        throw.addWidget(b2)
        throw.addStretch()
        throw.addWidget(b3)
        th.addLayout(throw)
        root.addWidget(th_card)
        root.addStretch()
        self._set_mode(editing=False, clear=True)

    # -- refresh from model
    def showEvent(self, e):
        self.refresh()
        super().showEvent(e)

    def resizeEvent(self, e):
        self.w.ui["panel_w"], self.w.ui["panel_h"] = self.width(), self.height()
        super().resizeEvent(e)

    def _details_resized(self, h: int):
        self.w.ui["details_h"] = h

    def _label(self, t: fc.Task) -> str:
        if t is self.w.active and self.w.timer.running:
            return ("⏸ " if self.w.timer.paused else "▶ ") + task_label(t)
        return ("⏸ " if t.spent_s else "") + task_label(t)  # paused to start another task

    def selected_task(self) -> fc.Task | None:
        row = self.list.currentRow()
        return self.w.plan.tasks[row] if 0 <= row < len(self.w.plan.tasks) else None

    def refresh(self, select: int | None = None):
        """Rebuild the lists. `select` picks a row (-1 for none); None keeps the current selection."""
        self._updating = True
        cur = select if select is not None else self.list.currentRow()
        self.list.clear()
        for i, t in enumerate(self.w.plan.tasks):
            item = QListWidgetItem(self._label(t))
            item.setData(Qt.UserRole, i)  # position before any drag, used by _rows_moved
            if t.details.strip():
                item.setToolTip(t.details)
            self.list.addItem(item)
        if self.w.plan.tasks and cur >= 0:
            self.list.setCurrentRow(min(cur, len(self.w.plan.tasks) - 1))
        else:
            self.list.setCurrentRow(-1)
        self.done_list.clear()
        for d in reversed(self.w.completed):
            when = d.finished_at[11:16] if len(d.finished_at) >= 16 else d.finished_at
            actual = f"{d.actual_min}m actual" if d.actual_min else "not timed"
            self.done_list.addItem(QListWidgetItem(f"{when}   {d.name}   ({d.estimate_min}m planned, {actual})"))
        self.dumps.clear()
        for d in fc.read_dumps(self.w.dump_path):
            self.dumps.addItem(QListWidgetItem(f"{d.when}   {d.text}"))
        self._updating = False
        self._load_editor(self.list.currentRow())
        self.update_toggle()

    def _on_timer_selected(self) -> bool:
        """True when Start should pause or resume the task on the timer: it is selected, or nothing is."""
        t = self.selected_task()
        return self.w.timer.running and (t is None or t is self.w.active)

    def update_toggle(self):
        timer = self.w.timer
        if self._on_timer_selected():
            self.btn_toggle.setText("⏸ Pause" if not timer.paused else "▶ Resume")
            self.btn_toggle.setToolTip(f"Timer is on: {timer.task}")
        elif self.selected_task() is not None:
            self.btn_toggle.setText("▶ Start selected")
            self.btn_toggle.setToolTip(f"Start the selected task. '{timer.task}' is paused and keeps its time."
                                       if timer.running else "Start the task selected in the list")
        else:
            self.btn_toggle.setText("▶ Start…")
            self.btn_toggle.setToolTip("Choose a task to start")

    # -- the task card: add mode / edit mode
    def _set_mode(self, editing: bool, clear: bool):
        self._editing = editing
        self.task_heading.setText("Selected task" if editing else "Add a task")
        self.task_hint.setText("Changes apply as you type. Press '+ New task' to add a different one."
                               if editing else "Name it, pick how long, jot the first step, then add it to the list.")
        self.b_new.setVisible(editing)
        self.b_add.setVisible(not editing)
        if clear:
            self._updating = True
            self.ed_name.clear()
            self.ed_details.clear()
            self.ed_time.set_value(int(self.w.s["default_block_minutes"]))
            self._updating = False

    def _load_editor(self, row: int):
        if self._updating:
            return
        self.update_toggle()
        t = self.w.plan.tasks[row] if 0 <= row < len(self.w.plan.tasks) else None
        if t is None:
            # Leaving edit mode clears the card; staying in add mode keeps whatever is being typed.
            self._set_mode(editing=False, clear=self._editing)
            return
        self._updating = True
        self.ed_name.setText(t.name)
        self.ed_time.set_value(t.estimate_min)
        self.ed_details.setPlainText(t.details)
        self._updating = False
        self._set_mode(editing=True, clear=False)

    def _apply_editor(self, *_):
        if self._updating or not self._editing:
            return
        row = self.list.currentRow()
        if not (0 <= row < len(self.w.plan.tasks)):
            return
        t = self.w.plan.tasks[row]
        t.name = self.ed_name.text().strip() or t.name
        t.estimate_min = self.ed_time.value()
        t.details = self.ed_details.toPlainText()
        if t is self.w.active and self.w.timer.running:
            self.w.timer.task = t.name
            self.w.timer.estimate_min = t.estimate_min
        self._updating = True
        self.list.item(row).setText(self._label(t))
        self.list.item(row).setToolTip(t.details)
        self._updating = False
        self.w.changed()

    def _enter_in_name(self):
        if not self._editing:
            self.add_task()

    def new_task(self):
        self.list.setCurrentRow(-1)
        self._set_mode(editing=False, clear=True)
        self.ed_name.setFocus()

    # -- add / reorder / remove
    def add_task(self):
        name = self.ed_name.text().strip()
        if not name:
            self.ed_name.setFocus()
            return
        self.w.plan.tasks.append(fc.Task(name, self.ed_time.value(), self.ed_details.toPlainText().strip()))
        self._set_mode(editing=False, clear=True)
        self.refresh(-1)
        self.ed_name.setFocus()
        self.w.changed()

    def _rows_moved(self, *_):
        old = list(self.w.plan.tasks)
        order = [self.list.item(i).data(Qt.UserRole) for i in range(self.list.count())]
        if sorted(order) == list(range(len(old))):
            self.w.plan.tasks = [old[i] for i in order]
        self.refresh(self.list.currentRow())
        self.w.changed()

    def move_selected(self, delta: int):
        row = self.list.currentRow()
        dst = row + delta
        fc.move_task(self.w.plan, row, dst)
        self.refresh(dst if 0 <= dst < len(self.w.plan.tasks) else row)
        self.w.changed()

    def remove_selected(self):
        t = self.selected_task()
        if t is None:
            return
        if t is self.w.active:
            self.w.timer.skip()
            self.w.active = None
            self.w._set_asking(False)
        self.w.plan.tasks.remove(t)
        self.refresh(-1)
        self.w.changed()

    def toggle_selected(self):
        """Pause/resume the task on the timer, or start the selected task (or choose one if none is selected)."""
        if self._on_timer_selected():
            self.w.toggle_run()
            return
        t = self.selected_task()
        if t is not None:
            self.w.start_task(t)
        else:
            self.w.choose_and_start()

    def done_selected(self):
        """Finish the selected task. With nothing selected, finish the task on the timer (or the top one)."""
        row = self.list.currentRow()
        t = self.selected_task()
        self.w.done(t)
        self.refresh(min(row, len(self.w.plan.tasks) - 1) if t is not None else -1)

    # -- completed
    def _done_index(self) -> int:
        row = self.done_list.currentRow()
        if row < 0:
            return -1
        return len(self.w.completed) - 1 - row  # the card shows newest first

    def put_back(self):
        i = self._done_index()
        if i < 0:
            return
        self.w.plan.tasks.append(fc.done_to_task(self.w.completed[i]))
        del self.w.completed[i]
        self.refresh(len(self.w.plan.tasks) - 1)
        self.w.changed()

    def clear_done(self):
        self.w.completed.clear()
        self.refresh()
        self.w.changed()

    # -- thoughts
    def dump_to_task(self):
        row = self.dumps.currentRow()
        dumps = fc.read_dumps(self.w.dump_path)
        if not (0 <= row < len(dumps)):
            return
        self.w.plan.tasks.append(fc.Task(dumps[row].text, int(self.w.s["default_block_minutes"])))
        fc.remove_dump(self.w.dump_path, row)
        self.refresh(len(self.w.plan.tasks) - 1)
        self.w.changed()

    def delete_dump(self):
        row = self.dumps.currentRow()
        if row >= 0:
            fc.remove_dump(self.w.dump_path, row)
            self.refresh()


# ---------------------------------------------------------------- the widget

class FocusWidget(QWidget):
    def __init__(self, settings: dict):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.s = settings
        self.c = {k: QColor(v) for k, v in settings["colors"].items()}
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowOpacity(float(settings.get("opacity", 0.96)))
        self.resize(int(settings["widget_width"]), int(settings["widget_height"]))
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)

        shared = shared_dir()
        self.state_path = shared / "state.json"
        self.dump_path = shared / "dump.txt"
        self.log_path = shared / "log.csv"
        self.local_path = local_dir() / "local.json"

        local = _read_json(self.local_path)
        geometry = local.get("geometry")
        self._merged = bool(local.get("merged_into_shared"))
        if not self._merged:
            try:
                old_geometry = merge_old_local_data(local_dir(), shared)
                geometry = geometry or old_geometry
                self._merged = True
            except (OSError, ValueError):
                pass  # the shared list is still syncing; the merge runs again at the next start

        self._shared_sig: tuple[int, int] | None = None  # the shared file as this computer last read or wrote it
        self._shared_text: str | None = None
        self._shared_dirty = False  # True while a change has not reached the shared file yet
        self._dump_sig = _file_sig(self.dump_path)
        self._adopt(self._read_shared() or fc.State(fc.Plan(), 0, fc.TimerState()))
        # Panel size, details-box height and shrunk/expanded state ride along in the saved geometry.
        self.ui: dict = {k: v for k, v in (geometry or {}).items() if k not in ("x", "y", "w", "h")}
        self._restore_geometry(geometry)
        self.mini = bool(self.ui.get("mini", False))
        if self.mini:
            self.resize(MINI_SIZE, MINI_SIZE)
        # If the computer was restarted while a task was already over time, ask again.
        self._asking = self.timer.running and self.timer.overtime_s > 0

        self._pulse = 0.0
        self.pulse_anim = QPropertyAnimation(self, b"pulse", self)
        self.pulse_anim.setDuration(2000)
        self.pulse_anim.setKeyValueAt(0.0, 0.0)
        self.pulse_anim.setKeyValueAt(0.5, 1.0)
        self.pulse_anim.setKeyValueAt(1.0, 0.0)
        self.pulse_anim.setEasingCurve(QEasingCurve.InOutSine)
        self._celebrate_until: float = 0.0

        self._drag_offset: QPoint | None = None
        self._press_at = QPoint()
        self._expanded_at = float("-inf")  # when a click last expanded the small circle (time.monotonic)
        self._build_ask_row()
        self._build_buttons()
        self.panel = TaskPanel(self)

        self.tick_timer = QTimer(self)
        self.tick_timer.timeout.connect(self.on_tick)
        self.tick_timer.start(CHECK_EVERY_MS)
        self.save_timer = QTimer(self)
        self.save_timer.timeout.connect(self.save)
        self.save_timer.start(AUTOSAVE_EVERY_MS)
        self.sync_timer = QTimer(self)
        self.sync_timer.timeout.connect(self.check_shared)
        self.sync_timer.start(SYNC_CHECK_MS)
        self._save_local()

    # -- pulse property for the animation
    def get_pulse(self) -> float:
        return self._pulse

    def set_pulse(self, v: float) -> None:
        self._pulse = v
        self.update()

    pulse = Property(float, get_pulse, set_pulse)

    # -- model helpers
    def current_task(self) -> fc.Task | None:
        """The task on the timer, or when idle the top of the list (shown as 'Up next')."""
        if self.timer.running and self.active is not None:
            return self.active
        return self.plan.tasks[0] if self.plan.tasks else None

    def changed(self):
        """Call after any change to the tasks or timer: write it to the shared folder and repaint."""
        self._shared_dirty = True
        self.save()
        self.update()
        self._refresh_toggle_labels()

    # -- the list shared through Box
    def _adopt(self, st: fc.State):
        """Take the list, completed tasks and timer from a loaded state."""
        self.plan, self.completed, self.timer = st.plan, st.done, st.timer
        self.timer.checkpoints = tuple(sorted(self.s["checkpoints"]))
        self.active: fc.Task | None = None
        if self.timer.running:
            self.active = fc.find_active(self.plan.tasks, st.current_index, self.timer.task)
            if self.active is None:
                self.timer.skip()
        self.timer.tick(datetime.now())  # catch up the clock; checkpoints already passed do not pulse or chime

    def _read_shared(self) -> fc.State | None:
        """The shared list as saved, or None while it is missing or only half synced."""
        sig = _file_sig(self.state_path)
        try:
            text = self.state_path.read_text(encoding="utf-8")
            st = fc.parse_state(text)
        except (OSError, ValueError):
            return None
        self._shared_sig, self._shared_text = sig, text
        return st

    def check_shared(self):
        """Every few seconds: show changes the other computer saved, or retry a write Box blocked."""
        if self._shared_dirty:
            self._save_shared()
            return
        if QApplication.activeModalWidget() or QApplication.activePopupWidget():
            return  # a dialog or menu is holding tasks from the current list; look again once it closes
        dump_sig = _file_sig(self.dump_path)
        if dump_sig != self._dump_sig:
            self._dump_sig = dump_sig
            if self.panel.isVisible():
                self.panel.refresh()
        if _file_sig(self.state_path) == self._shared_sig:
            return
        before = self._shared_text
        st = self._read_shared()
        if st is not None and self._shared_text != before:
            self._apply_state(st)

    def _apply_state(self, st: fc.State):
        """Show a list the other computer saved."""
        was = (self.timer.running, self.timer.task)
        self._adopt(st)
        same_task = was == (self.timer.running, self.timer.task)
        # Still over time on the same task: keep this computer's answer. A different task over time: ask.
        self._set_asking(self.timer.running and self.timer.overtime_s > 0 and (self._asking or not same_task))
        self.update()
        self._refresh_toggle_labels()
        if self.panel.isVisible():
            self.panel.refresh()

    def _refresh_toggle_labels(self):
        running = self.timer.running and not self.timer.paused
        self.btn_toggle.setText("⏸" if running else "▶")
        self.btn_toggle.setToolTip("Pause (Space)" if running else "Start / resume (Space)")
        if self.panel.isVisible():
            self.panel.update_toggle()

    # -- buttons (shown on hover)
    def _build_buttons(self):
        self.btn_row = QWidget(self)
        lay = QHBoxLayout(self.btn_row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        c = self.s["colors"]
        spec = [("▶", "Start / resume (Space)", self.toggle_run),
                (f"+{self.s['add_minutes_step']}", "Add minutes (+)", self.add_minutes),
                ("✓", "Done (D)", self.done),
                ("💭", "Dump a thought (T)", self.dump_thought),
                ("☰", "All tasks (Enter)", self.open_panel),
                ("–", "Shrink to a small circle (M). Click the circle to expand", lambda: self.set_mini(True)),
                ("✕", "Hide widget (Esc). Ctrl+Alt+F or the tray icon brings it back", self.hide)]
        self.btn_toggle = None
        for label, tip, fn in spec:
            b = QPushButton(label)
            b.setToolTip(tip)
            b.setFixedHeight(24)
            b.setCursor(Qt.PointingHandCursor)
            b.setFocusPolicy(Qt.NoFocus)
            b.setStyleSheet(f"QPushButton{{background:{c['button']};color:{c['button_text']};border:none;"
                            f"border-radius:8px;padding:0 8px;font-size:11px;}}"
                            f"QPushButton:hover{{background:{c['button_hover']};}}")
            b.clicked.connect(lambda _=False, fn=fn: fn())  # drop the 'checked' flag Qt passes on click
            lay.addWidget(b)
            if self.btn_toggle is None:
                self.btn_toggle = b
        self.btn_row.adjustSize()
        self.btn_row.hide()
        self._place_buttons()

    def _build_ask_row(self):
        """Shown when time is up: the timer keeps counting, nothing completes on its own."""
        self.ask_row = QWidget(self)
        lay = QHBoxLayout(self.ask_row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        c = self.s["colors"]
        step = int(self.s["add_minutes_step"])
        spec = [(f"+{step}", f"Add {step} minutes", lambda: self.add_minutes(step)),
                (f"+{3 * step}", f"Add {3 * step} minutes", lambda: self.add_minutes(3 * step)),
                ("✓ Done", "Finish this task (D)", self.done),
                ("keep going", "Stay on it without a new estimate", self.keep_going)]
        for label, tip, fn in spec:
            b = QPushButton(label)
            b.setToolTip(tip)
            b.setFixedHeight(24)
            b.setCursor(Qt.PointingHandCursor)
            b.setFocusPolicy(Qt.NoFocus)
            primary = label.startswith("+")
            b.setStyleSheet(f"QPushButton{{background:{c['disk_remaining'] if primary else c['button']};"
                            f"color:{'#2F4A44' if primary else c['button_text']};border:none;"
                            f"border-radius:8px;padding:0 7px;font-size:11px;font-weight:{600 if primary else 400};}}"
                            f"QPushButton:hover{{background:{c['button_hover']};}}")
            b.clicked.connect(lambda _=False, fn=fn: fn())  # drop the 'checked' flag Qt passes on click
            lay.addWidget(b)
        self.ask_row.adjustSize()
        self.ask_row.hide()

    def _place_buttons(self):
        self.btn_row.move(self.width() - self.btn_row.width() - 12, self.height() - self.btn_row.height() - 10)
        self.ask_row.move(self.width() - self.ask_row.width() - 12, self.height() - self.ask_row.height() - 10)

    def _show_rows(self, hovering: bool):
        self.ask_row.setVisible(self._asking and not self.mini)
        self.btn_row.setVisible(hovering and not self._asking and not self.mini)

    # -- shrink to a small circle
    def set_mini(self, on: bool):
        """Shrink to a small timer circle, or expand back. The top-right corner stays where it is."""
        if on == self.mini:
            return
        self.mini = on
        self.ui["mini"] = on
        right, top = self.geometry().right(), self.geometry().top()
        w, h = (MINI_SIZE, MINI_SIZE) if on else (int(self.s["widget_width"]), int(self.s["widget_height"]))
        self.resize(w, h)
        self.move(right - w + 1, top)
        self.setToolTip("")
        self._refresh_toggle_labels()
        # Windows sends no "mouse entered" when the window grows around a cursor that is already on it,
        # so show the bottom buttons now instead of waiting for the cursor to leave and come back.
        self._show_rows(not on and QRect(right - w + 1, top, w, h).contains(QCursor.pos()))
        self.save()
        self.update()

    def resizeEvent(self, e):
        self._place_buttons()

    def enterEvent(self, e):
        self._refresh_toggle_labels()
        self._show_rows(True)

    def leaveEvent(self, e):
        self._show_rows(False)

    # -- geometry persistence
    def _restore_geometry(self, g: dict | None):
        if g:
            rect = QRect(g["x"], g["y"], g["w"], g["h"])
            for screen in QApplication.screens():
                if screen.availableGeometry().intersects(rect):
                    self.move(rect.topLeft())
                    return
        screen = QApplication.primaryScreen().availableGeometry()
        self.move(screen.right() - self.width() - 24, screen.top() + 24)

    def geometry_dict(self) -> dict:
        g = self.geometry()
        return {**self.ui, "x": g.x(), "y": g.y(), "w": g.width(), "h": g.height()}

    def save(self):
        """Save this computer's window position, and the shared list only if it has an unwritten change.
        Never rewriting an unchanged list is what stops an idle computer from undoing the other one's edits."""
        self._save_local()
        if self._shared_dirty:
            self._save_shared()

    def _save_local(self):
        _write_json(self.local_path, {"geometry": self.geometry_dict(), "merged_into_shared": self._merged})

    def _save_shared(self):
        idx = next((i for i, t in enumerate(self.plan.tasks) if t is self.active), 0)
        try:
            text = fc.save_state(self.state_path, self.plan, idx, self.timer, done=self.completed)
        except OSError:
            return  # Box can hold the file for a moment while it uploads; check_shared tries again
        self._shared_sig, self._shared_text = _file_sig(self.state_path), text
        self._shared_dirty = False

    # -- dragging
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._press_at = e.globalPosition().toPoint()
            self._drag_offset = self._press_at - self.frameGeometry().topLeft()
            self.setFocus()

    def mouseMoveEvent(self, e):
        if self._drag_offset is not None and e.buttons() & Qt.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, e):
        if self._drag_offset is None:
            return
        self._drag_offset = None
        moved = (e.globalPosition().toPoint() - self._press_at).manhattanLength()
        if self.mini and moved < QApplication.startDragDistance():
            self.set_mini(False)  # a click on the small circle expands it; a drag only moves it
            self._expanded_at = time.monotonic()
        else:
            self.save()

    def mouseDoubleClickEvent(self, e):
        if self.mini:
            self.set_mini(False)
        elif time.monotonic() - self._expanded_at > QApplication.doubleClickInterval() / 1000:
            self.open_panel()
        # otherwise this is the second half of a double-click whose first click expanded the circle

    # -- keyboard
    def keyPressEvent(self, e):
        k, t = e.key(), e.text()
        if t in ("+", "="):
            self.add_minutes()
        elif k == Qt.Key_Space:
            self.toggle_run()
        elif t.lower() == "d":
            self.done()
        elif t.lower() == "t":
            self.dump_thought()
        elif t.lower() == "m":
            self.set_mini(not self.mini)
        elif k in (Qt.Key_Return, Qt.Key_Enter):
            self.open_panel()
        elif k == Qt.Key_Escape:
            self.hide()
        else:
            super().keyPressEvent(e)

    # -- actions
    def toggle_run(self):
        """One button: pause if running, resume if paused, otherwise choose a task to start."""
        now = datetime.now()
        if self.timer.running and not self.timer.paused:
            self.timer.pause(now)
        elif self.timer.running and self.timer.paused:
            self.timer.resume(now)
        else:
            self.choose_and_start()
            return
        self.changed()
        if self.panel.isVisible():
            self.panel.refresh()

    def choose_and_start(self):
        """Start the only task, or pop up the list so you pick which one to start."""
        tasks = self.plan.tasks
        if not tasks:
            self.open_panel()
            return
        if len(tasks) == 1:
            self.start_task(tasks[0])
            return
        c = self.s["colors"]
        menu = QMenu(self)
        menu.setStyleSheet(f"QMenu {{ background: {c['background']}; color: {c['text']}; border: 1px solid {c['border']};"
                           f" padding: 6px; font-family: 'Segoe UI'; font-size: 10pt; }}"
                           f"QMenu::item {{ padding: 6px 14px; border-radius: 8px; }}"
                           f"QMenu::item:selected {{ background: {c['disk_track']}; }}"
                           f"QMenu::item:disabled {{ color: {c['text_dim']}; }}")
        head = menu.addAction("Which task do you want to start?")
        head.setEnabled(False)
        for t in tasks:
            menu.addAction(task_label(t)).triggered.connect(lambda _=False, t=t: self.start_task(t))
        menu.exec(QCursor.pos())

    def start_task(self, task: fc.Task):
        """Put `task` on the timer. A task already on it is paused and keeps its time for when it restarts."""
        now = datetime.now()
        if task is self.active and self.timer.running:
            if self.timer.paused:
                self.toggle_run()
            return
        if self.timer.running and self.active is not None:
            self.timer.pause(now)
            self.active.spent_s = self.timer.elapsed_s
        self.timer.start(task.name, task.estimate_min, now, spent_s=task.spent_s)
        task.spent_s = 0.0  # while on the timer, its time is counted there
        self.active = task
        self._set_asking(False)
        self.changed()
        if self.panel.isVisible():
            self.panel.refresh()

    def add_minutes(self, minutes: int | None = None):
        if self.timer.running:
            self.timer.add_minutes(int(minutes or self.s["add_minutes_step"]))
            if self.active is not None:
                self.active.estimate_min = self.timer.estimate_min
            self._set_asking(False)
            self.changed()

    def keep_going(self):
        """Dismiss the time-up prompt; the timer keeps counting overtime."""
        self._set_asking(False)
        self.update()

    def _set_asking(self, asking: bool):
        self._asking = asking
        self._show_rows(self.underMouse())

    def done(self, task: fc.Task | None = None):
        """Finish `task` (default: the task on the timer, or the top one). Logs it and moves it to Completed."""
        task = task if task is not None else self.current_task()
        if task is None:
            return
        now = datetime.now()
        if task is self.active and self.timer.running:
            rec = self.timer.done(now)
            self.active = None
            self._set_asking(False)
        else:
            rec = fc.BlockRecord(task.name, task.estimate_min, int(round(task.spent_s / 60)), now)
        self._log(rec)
        self.completed.append(fc.DoneTask(task.name, rec.estimate_min, rec.actual_min, f"{now:%Y-%m-%d %H:%M}",
                                          task.details))
        self.plan.tasks = [t for t in self.plan.tasks if t is not task]
        self._celebrate_until = now.timestamp() + CELEBRATE_MS / 1000
        self.changed()
        if self.panel.isVisible():
            self.panel.refresh()

    def _log(self, rec: fc.BlockRecord):
        new = not self.log_path.exists()
        with self.log_path.open("a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["finished_at", "task", "estimate_min", "actual_min"])
            w.writerow([rec.finished_at.isoformat(timespec="minutes"), rec.task, rec.estimate_min, rec.actual_min])

    def dump_thought(self):
        dlg = DumpDialog(self, self.s["colors"])
        if dlg.exec() == QDialog.Accepted and dlg.text():
            with self.dump_path.open("a", encoding="utf-8") as f:
                f.write(f"{datetime.now():%Y-%m-%d %H:%M}  {dlg.text()}\n")
            if self.panel.isVisible():
                self.panel.refresh(self.panel.list.currentRow())
        self.activateWindow()

    def open_panel(self):
        self.panel.show()
        self.panel.raise_()
        self.panel.activateWindow()

    # -- ticking
    def on_tick(self):
        if not self.timer.running or self.timer.paused:
            self.update()
            return
        fired = self.timer.tick(datetime.now())
        for frac in fired:
            self.pulse_anim.stop()
            self.pulse_anim.start()
            if frac >= 1.0:
                self._end_signal()
                self.set_mini(False)  # the time-up question needs the full widget
                self._set_asking(True)
                if not self.isVisible():
                    self.show()
        if self.mini:
            left = (f"over by {_mmss(self.timer.overtime_s)}" if self.timer.overtime_s
                    else f"{_mmss(self.timer.remaining_s)} left")
            self.setToolTip(f"{self.timer.task}\n{left}")
        self.update()

    def _end_signal(self):
        if self.s.get("chime_at_end"):
            wav = resource_dir() / "assets" / "chime.wav"
            if wav.exists():
                winsound.PlaySound(str(wav), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
        if self.s.get("toast_at_end"):
            try:
                from windows_toasts import Toast, WindowsToaster
                toaster = WindowsToaster("Focus Tool")
                t = Toast()
                t.text_fields = [f"Time is up on: {self.timer.task}", "Done, +5, or keep going. Your call."]
                toaster.show_toast(t)
            except Exception:
                pass

    # -- mascot
    def _mascot(self) -> str:
        if datetime.now().timestamp() < self._celebrate_until:
            return "🎉"
        if not self.timer.running:
            return "🥳" if not self.plan.tasks else "🌱"
        if self.timer.paused:
            return "😴"
        if self.timer.overtime_s > 0:
            return "😅"
        f = self.timer.fraction_remaining
        return "🙂" if f > 0.5 else ("😌" if f > 0.1 else "👀")

    # -- painting
    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        w, h = self.width(), self.height()
        if self.mini:
            self._paint_mini(p, w)
            p.end()
            return
        p.setPen(QPen(self.c["border"], 2))
        p.setBrush(self.c["background"])
        p.drawRoundedRect(QRectF(1, 1, w - 2, h - 2), 18, 18)
        p.setPen(Qt.NoPen)

        # disk
        r = min(h - 32, 120) / 2
        cx, cy = 18 + r, h / 2
        disk = QRectF(cx - r, cy - r, 2 * r, 2 * r)
        p.setBrush(self.c["disk_track"])
        p.drawEllipse(disk)
        if self.timer.running:
            frac = self.timer.fraction_remaining
            if self.timer.overtime_s > 0:
                p.setBrush(self.c["disk_overtime"])
                p.drawEllipse(disk)
                ring = QRectF(disk).adjusted(3, 3, -3, -3)
                over = min(1.0, self.timer.overtime_s / max(1, self.timer.estimate_s))
                p.setBrush(Qt.NoBrush)
                p.setPen(QPen(self.c["overtime_ring"], 4, Qt.SolidLine, Qt.RoundCap))
                p.drawArc(ring, 90 * 16, int(-over * 360 * 16))
                p.setPen(Qt.NoPen)
            else:
                color = QColor(self.c["disk_remaining"])
                if self._pulse > 0:
                    color = _blend(color, self.c["disk_pulse"], self._pulse)
                p.setBrush(color)
                p.drawPie(disk, 90 * 16, int(-frac * 360 * 16))
        # mascot hole
        if self.s.get("mascot", True):
            hole = r * 0.42
            p.setBrush(self.c["background"])
            p.drawEllipse(QRectF(cx - hole, cy - hole, 2 * hole, 2 * hole))
            p.setFont(QFont("Segoe UI Emoji", int(hole * 0.9)))
            p.setPen(self.c["text"])
            p.drawText(QRectF(cx - hole, cy - hole, 2 * hole, 2 * hole), Qt.AlignCenter, self._mascot())
            p.setPen(Qt.NoPen)

        # text block
        x0 = int(cx + r + 18)
        avail = w - x0 - 14
        fam = self.s.get("font_family", "Segoe UI")
        big = QFont(fam, 12, QFont.DemiBold)
        small = QFont(fam, 9)
        p.setFont(big)
        p.setPen(self.c["text"])
        task = self.current_task()
        if self.timer.running:
            title = self.timer.task or ""
        elif task:
            title = f"Up next: {task.name}"
        else:
            title = "All done, the list is empty" if self.completed else "Double-click to add tasks"
        p.drawText(x0, 32, QFontMetrics(big).elidedText(title, Qt.ElideRight, avail))

        p.setFont(small)
        y = 54
        for text, dim in self._info_lines():
            p.setPen(self.c["text_dim"] if dim else self.c["text"])
            p.drawText(x0, y, QFontMetrics(small).elidedText(text, Qt.ElideRight, avail))
            y += 19
        p.end()

    def _paint_mini(self, p: QPainter, size: int):
        """The shrunk widget: a round badge with the time-left pie and the mascot."""
        p.setPen(QPen(self.c["border"], 2))
        p.setBrush(self.c["background"])
        p.drawEllipse(QRectF(1, 1, size - 2, size - 2))
        p.setPen(Qt.NoPen)
        disk = QRectF(5, 5, size - 10, size - 10)
        p.setBrush(self.c["disk_track"])
        p.drawEllipse(disk)
        if self.timer.running:
            if self.timer.overtime_s > 0:
                p.setBrush(self.c["disk_overtime"])
                p.drawEllipse(disk)
            else:
                color = QColor(self.c["disk_remaining"])
                if self._pulse > 0:
                    color = _blend(color, self.c["disk_pulse"], self._pulse)
                p.setBrush(color)
                p.drawPie(disk, 90 * 16, int(-self.timer.fraction_remaining * 360 * 16))
        if self.s.get("mascot", True):
            hole = (size - 10) * 0.22
            c = size / 2
            p.setBrush(self.c["background"])
            p.drawEllipse(QRectF(c - hole, c - hole, 2 * hole, 2 * hole))
            p.setFont(QFont("Segoe UI Emoji", max(7, int(hole * 0.85))))
            p.setPen(self.c["text"])
            p.drawText(QRectF(c - hole, c - hole, 2 * hole, 2 * hole), Qt.AlignCenter, self._mascot())

    def _info_lines(self) -> list[tuple[str, bool]]:
        now = datetime.now()
        lines: list[tuple[str, bool]] = []
        task = self.current_task()
        if task and task.details.strip():
            lines.append((task.details.strip().splitlines()[0], True))
        if self.timer.running:
            if self._asking:
                lines.append(("Time is up. Add more time, or press Done.", False))
            elif self.timer.overtime_s > 0:
                lines.append((f"over by {_mmss(self.timer.overtime_s)}   on this: {_mins(self.timer.elapsed_s)}", False))
            else:
                lines.append((f"{_mmss(self.timer.remaining_s)} left   on this: {_mins(self.timer.elapsed_s)}"
                              + ("   (paused)" if self.timer.paused else ""), False))
        elif task:
            lines.append((f"{task.estimate_min} min planned. Press ▶ or Space when ready.", False))
        if task is not None:
            fin = fc.list_end(self.plan.tasks, self.active, self.timer, now)
            left = len(self.plan.tasks) - (1 if self.timer.running else 0)
            more = f" ({left} more after this)" if (self.timer.running and left > 0) else \
                   (f" ({left} tasks)" if not self.timer.running else " (last task)")
            lines.append((f"list ends about {fin:%I:%M %p}".lstrip("0").replace(" 0", " ") + more, True))
        return lines[:4]


def _blend(a: QColor, b: QColor, t: float) -> QColor:
    return QColor(int(a.red() + (b.red() - a.red()) * t),
                  int(a.green() + (b.green() - a.green()) * t),
                  int(a.blue() + (b.blue() - a.blue()) * t))


def _mmss(seconds: int) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


def _mins(seconds: int) -> str:
    m = int(seconds) // 60
    return f"{m} min" if m < 90 else f"{m // 60}h {m % 60:02d}m"


def task_label(t: fc.Task) -> str:
    """'15m · name', plus the time already used when the task was paused to start another one."""
    used = ""
    if t.spent_s:
        used = f"   ({'under 1 min' if t.spent_s < 60 else _mins(t.spent_s)} used)"
    return f"{t.estimate_min}m · {t.name}{used}"


# ---------------------------------------------------------------- tray + app

class TrayController:
    def __init__(self, app: QApplication, widget: FocusWidget, settings: dict):
        self.app, self.w = app, widget
        icon = QIcon(str(resource_dir() / "assets" / "icon.ico"))
        self.tray = QSystemTrayIcon(icon, app)
        self.tray.setToolTip("Focus Tool")
        menu = QMenu()
        self.act_toggle = QAction("Hide widget", menu, triggered=self.toggle)
        menu.addAction(self.act_toggle)
        menu.addAction(QAction("All tasks…", menu, triggered=widget.open_panel))
        menu.addAction(QAction("Shrink / expand", menu, triggered=lambda: widget.set_mini(not widget.mini)))
        menu.addAction(QAction("Start / pause", menu, triggered=widget.toggle_run))
        menu.addSeparator()
        self.act_login = QAction("Start at login", menu, checkable=True)
        self.act_login.setChecked(startup_shortcut_path().exists())
        self.act_login.toggled.connect(self._toggle_login)
        menu.addAction(self.act_login)
        menu.addAction(QAction("Open data folder (in Box)", menu, triggered=lambda: os.startfile(str(shared_dir()))))
        menu.addSeparator()
        menu.addAction(QAction("Quit", menu, triggered=self.quit))
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self.toggle() if reason == QSystemTrayIcon.Trigger else None)
        self.tray.show()

        mods, key = parse_hotkey(settings.get("hotkey_show_hide", "ctrl+alt+f"))
        self.filter = HotkeyFilter(self.toggle)
        app.installNativeEventFilter(self.filter)
        if key and not ctypes.windll.user32.RegisterHotKey(None, HOTKEY_ID, mods | 0x4000, key):
            self.tray.showMessage("Focus Tool", "Could not register the show/hide hotkey (already in use).", icon, 5000)

    def toggle(self):
        if self.w.isVisible():
            self.w.hide()
            self.act_toggle.setText("Show widget")
        else:
            self.w.show()
            self.w.activateWindow()
            self.act_toggle.setText("Hide widget")

    def _toggle_login(self, checked: bool):
        try:
            set_start_at_login(checked)
        except Exception as ex:  # keep the app alive whatever the shell does
            self.tray.showMessage("Focus Tool", f"Could not change the startup shortcut: {ex}",
                                  QSystemTrayIcon.Warning, 6000)
            self.act_login.blockSignals(True)
            self.act_login.setChecked(startup_shortcut_path().exists())
            self.act_login.blockSignals(False)

    def quit(self):
        self.w.save()
        ctypes.windll.user32.UnregisterHotKey(None, HOTKEY_ID)
        self.tray.hide()
        self.app.quit()


def main() -> int:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName(APP_NAME)
    settings = load_settings()
    widget = FocusWidget(settings)
    controller = TrayController(app, widget, settings)  # noqa: F841 (kept alive by reference)
    widget.show()
    app.aboutToQuit.connect(widget.save)
    app.commitDataRequest.connect(lambda *_: widget.save())  # Windows log-off / shutdown
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
