"""Focus Tool v2, a small always-on-top time-visibility widget for Windows.

Run from source:  python focus_tool.py
Build an exe:     .\\build.ps1

All the timer/plan logic lives in focus_core.py (tested). This file is only
the window, the task panel, the tray icon, the hotkey, sounds and file plumbing.

The task list, completed list, timer, log and settings live in
focus-tool\\data inside Box, so every computer that runs the app from Box shares
them. Each computer re-reads that folder every few seconds to pick up the other
computer's changes, and writes it only when you change something, so an idle
computer never writes an old list over a newer one. The window position stays on
each computer, in %APPDATA%\\FocusTool\\local.json. state.json is version 3: every
task has an id, and each change carries a time stamp so two copies can be merged.

plan.tasks holds the tasks still to do, in the order you chose. Tasks with
a deadline or an "aim to finish by" date show first, earliest date first; the rest
follow your order. A repeating task stays in the list and sleeps after Done until
its next day. FocusWidget.active is the task the timer runs on. You pick which task
to start; starting another one pauses the task on the timer and keeps its time in
Task.spent_s, so it continues where it stopped when you start it again. Done moves
a task to the completed list (kept in state.json) and logs it. Nothing starts on
its own; when time is up the timer keeps counting and the widget asks for more time.
"""
from __future__ import annotations

import copy
import csv
import ctypes
import ctypes.wintypes as wt
import json
import os
import shutil
import sys
import time
import winsound
from datetime import date, datetime
from pathlib import Path

from PySide6.QtCore import (QAbstractNativeEventFilter, QDate, QEasingCurve, QPoint, QPropertyAnimation, QRect,
                            QRectF, Qt, QTimer, Property, Signal)
from PySide6.QtGui import QAction, QColor, QCursor, QFont, QFontMetrics, QIcon, QPainter, QPen
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox, QDateEdit,
                               QDialog, QFrame, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMenu, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea,
                               QSpinBox, QSystemTrayIcon, QVBoxLayout, QWidget)

import focus_core as fc

# The tests replace this module's `datetime` with a clock that only has now(), so this file only ever
# calls datetime.now(). Dates come from `date`, and change stamps from fc.now_iso().

APP_NAME = "FocusTool"
CHECK_EVERY_MS = 250
AUTOSAVE_EVERY_MS = 30_000
SYNC_CHECK_MS = 2000      # how often to look for changes the other computer saved to Box
CELEBRATE_MS = 3000
TIME_PRESETS = (5, 10, 15, 25, 30, 45, 60, 90)
MINI_SIZE = 64            # the shrunk widget is a circle this many pixels wide
DETAILS_DEFAULT_H = 140   # starting height of the details box in the task panel
DETAILS_MIN_H = 60
NEWER_FILE_MESSAGE = "Your list was saved by a newer Focus Tool. Please update this app."
REPEAT_CHOICES = ("Never", "Every day", "Weekdays", "Chosen days", "Every N days")
REPEAT_KINDS = (None, "daily", "weekdays", "days", "every")
DAY_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
NEW_FOLDER = "\x00new folder"        # item data of the editor's "New folder..." choice
NEW_SUBFOLDER = "\x00new subfolder"  # item data of the editor's "New subfolder..." choice


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
    """The folder with the tasks, completed list, timer, log and settings, one copy that Box syncs to every computer.
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

    The first computer copies its list over; a later one adds only the tasks, completed tasks and log
    rows the shared folder does not have yet. The old files are left as they are, as a backup.
    Returns the old window position. Raises ValueError if the shared list exists but cannot be read
    yet (Box still syncing it) or was saved by a newer app, so nothing is written and the merge runs
    again at the next start.
    """
    old_path = local / "state.json"
    if not old_path.exists():
        return None
    old = fc.load_state(old_path)
    old_geometry = old.geometry
    new_path = shared / "state.json"
    prev = None
    if new_path.exists():
        have = fc.parse_state(new_path.read_text(encoding="utf-8"))
        prev = fc.state_to_doc(have)
        merged = fc.merge_states(have, old)
        # merge_states builds a plain State; keep the shared file's tombstones, folders and planner block.
        merged.deleted, merged.order_updated_at, merged.planner = have.deleted, have.order_updated_at, have.planner
        merged.folders, merged.folders_updated_at = have.folders, have.folders_updated_at
    else:
        merged = old
    merged.geometry = None
    doc = fc.stamp_changes(prev, fc.state_to_doc(merged), fc.now_iso())
    fc.save_state_obj(new_path, fc.doc_to_state(doc))
    src, dst = local / "log.csv", shared / "log.csv"
    if src.exists():
        have_log = dst.read_text(encoding="utf-8") if dst.exists() else ""
        dst.write_text(fc.merge_lines(have_log, src.read_text(encoding="utf-8")), encoding="utf-8")
    return old_geometry


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
    QComboBox, QDateEdit, QSpinBox, QFrame#card QComboBox, QFrame#card QDateEdit, QFrame#card QSpinBox {{
        background: {c['background']}; border: 1px solid {c['border']}; border-radius: 8px; padding: 3px 8px; }}
    QComboBox:disabled, QDateEdit:disabled, QFrame#card QDateEdit:disabled {{ color: {c['text_dim']}; }}
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


def _qdate(d: date) -> QDate:
    return QDate(d.year, d.month, d.day)


def _iso(q: QDate) -> str:
    return f"{q.year():04d}-{q.month():02d}-{q.day():02d}"


def short_date(iso: str) -> str:
    """'2026-10-16' as 'Fri 10/16' (English day names whatever the Windows language is)."""
    d = date.fromisoformat(iso)
    return f"{DAY_LABELS[d.weekday()]} {d.month}/{d.day}"


def first_date(t: fc.Task) -> str | None:
    """The earlier of a task's deadline and aim-to-finish date; the dated list is sorted by it."""
    dates = [x for x in (t.deadline, t.target) if x]
    return min(dates) if dates else None


def clean_folder(text: str | None) -> str | None:
    """A folder path typed by hand, tidied, so 'Paper / Theory/' becomes 'Paper/Theory'. Empty text gives None."""
    parts = [p.strip() for p in (text or "").split("/") if p.strip()]
    return "/".join(parts) or None


def folder_with_parents(path: str) -> list[str]:
    """'A/B/C' gives ['A', 'A/B', 'A/B/C']."""
    parts = path.split("/")
    return ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]


class FoldersDialog(QDialog):
    """Rename or delete folders. Tasks in a deleted folder stay on the list without a folder."""

    def __init__(self, widget: "FocusWidget"):
        super().__init__(widget.panel if widget.panel.isVisible() else None)
        self.w = widget
        self.setWindowTitle("Folders")
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        self.setStyleSheet(panel_stylesheet(widget.s["colors"]))
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        hint = QLabel("Renaming a folder also renames its subfolders. Deleting one keeps its tasks on the list.")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        self.list = QListWidget()
        self.list.setMinimumSize(320, 200)
        lay.addWidget(self.list)
        row = QHBoxLayout()
        for label, fn in (("Rename...", self.rename), ("Delete", self.delete)):
            b = QPushButton(label)
            b.clicked.connect(lambda _=False, fn=fn: fn())
            row.addWidget(b)
        row.addStretch()
        b_close = QPushButton("Close")
        b_close.clicked.connect(self.accept)
        row.addWidget(b_close)
        lay.addLayout(row)
        self.fill()

    def fill(self):
        self.list.clear()
        for path, depth in fc.folder_tree(self.w.folders, self.w.plan.tasks):
            item = QListWidgetItem("    " * depth + path.split("/")[-1])
            item.setData(Qt.UserRole, path)
            item.setToolTip(path)
            self.list.addItem(item)

    def _selected(self) -> str | None:
        item = self.list.currentItem()
        return item.data(Qt.UserRole) if item is not None else None

    def rename(self):
        path = self._selected()
        if path is None:
            return
        text, ok = QInputDialog.getText(self, "Rename folder", "New name (a / makes it a subfolder):",
                                        QLineEdit.Normal, path)
        new = clean_folder(text) if ok else None
        if new and new != path:
            self.w.rename_folder(path, new)
            self.fill()

    def delete(self):
        path = self._selected()
        if path is None:
            return
        answer = QMessageBox.question(self, "Delete folder", f"Delete the folder '{path}' and its subfolders? "
                                      "Their tasks stay on the list without a folder.")
        if answer == QMessageBox.Yes:
            self.w.delete_folder(path)
            self.fill()


# ---------------------------------------------------------------- task panel

_KEEP = object()  # passed to TaskPanel.refresh to keep the current selection


class TaskPanel(QWidget):
    """One card to add or edit a task, the dated list, the ranked list, and the completed list."""

    def __init__(self, widget: "FocusWidget"):
        super().__init__(None, Qt.Tool | Qt.WindowStaysOnTopHint)
        self.w = widget
        self.setWindowTitle("Focus Tool")
        self.setWindowIcon(QIcon(str(resource_dir() / "assets" / "icon.ico")))
        self.setStyleSheet(panel_stylesheet(widget.s["colors"]))
        self.resize(int(widget.ui.get("panel_w", 560)), int(widget.ui.get("panel_h", 860)))
        self._updating = False
        self._editing = False  # False: the task card adds a new task. True: it edits the selected one.
        self._folder_key: tuple | None = None  # the folder tree the two folder combos were last filled with
        self._last_folder: str | None = None  # the editor's folder before "New folder..." was picked
        self._editor_anchor: str | None = None  # start date of an "Every N days" repeat being edited
        self._kept_repeat: dict | None = None  # a repeat rule this app does not know; kept unless changed here

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
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("color: #B5523B; font-weight: 600;")
        self.status_label.hide()
        root.addWidget(self.status_label)

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

        # each date has a check box that turns it on; unchecked means no date
        self.ed_deadline_on, self.ed_deadline = self._date_row(tc, "Deadline", "A hard date the task must be done by")
        self.ed_target_on, self.ed_target = self._date_row(tc, "Aim to finish by",
                                                           "The date you expect to finish. Not a hard limit.")
        frow = QHBoxLayout()
        frow.addWidget(self._row_label("Folder"))
        self.ed_folder = QComboBox()
        self.ed_folder.currentIndexChanged.connect(self._editor_folder_changed)
        frow.addWidget(self.ed_folder, 1)
        tc.addLayout(frow)
        rrow = QHBoxLayout()
        rrow.addWidget(self._row_label("Repeat"))
        self.ed_repeat = QComboBox()
        self.ed_repeat.addItems(list(REPEAT_CHOICES))
        self.ed_repeat.currentIndexChanged.connect(self._repeat_changed)
        rrow.addWidget(self.ed_repeat)
        self.ed_every = QSpinBox()
        self.ed_every.setRange(1, 365)
        self.ed_every.setValue(2)
        self.ed_every.setPrefix("every ")
        self.ed_every.setSuffix(" days")
        self.ed_every.valueChanged.connect(self._apply_editor)
        rrow.addWidget(self.ed_every)
        rrow.addStretch()
        tc.addLayout(rrow)
        self.days_row = QWidget()
        drow = QHBoxLayout(self.days_row)
        drow.setContentsMargins(0, 0, 0, 0)
        drow.setSpacing(6)
        self.ed_days: list[QCheckBox] = []
        for label in DAY_LABELS:
            cb = QCheckBox(label)
            cb.toggled.connect(self._apply_editor)
            self.ed_days.append(cb)
            drow.addWidget(cb)
        drow.addStretch()
        tc.addWidget(self.days_row)

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

        # -- which folder to show
        show = QHBoxLayout()
        show.addWidget(QLabel("Show"))
        self.filter_combo = QComboBox()
        self.filter_combo.setToolTip("Show one folder and its subfolders, or all tasks")
        self.filter_combo.currentIndexChanged.connect(self._filter_changed)
        show.addWidget(self.filter_combo, 1)
        b_folders = QPushButton("Folders...")
        b_folders.setToolTip("Rename or delete folders")
        b_folders.clicked.connect(lambda: self.w.open_folders())
        show.addWidget(b_folders)
        root.addLayout(show)

        # -- tasks with a date, earliest first
        self.dated_card, dl = card("📅 By date", "Tasks with a deadline or a date to aim for, earliest first. "
                                                "Their dates set the order, so they cannot be dragged.")
        self.dated_list = QListWidget()
        self.dated_list.setMinimumHeight(80)
        self.dated_list.currentRowChanged.connect(lambda r: self._selection_changed(self.dated_list, r))
        dl.addWidget(self.dated_list)
        root.addWidget(self.dated_card)

        # -- the ranked list
        list_card, lst = card("Your list, in order", "Click a task to edit it above, Start it, or mark it Done. "
                                                      "Drag to reorder, or use the arrows.")
        self.list = QListWidget()
        self.list.setDragDropMode(QAbstractItemView.InternalMove)
        self.list.setDefaultDropAction(Qt.MoveAction)
        self.list.setMinimumHeight(150)
        self.list.currentRowChanged.connect(lambda r: self._selection_changed(self.list, r))
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
        drow2 = QHBoxLayout()
        b_back = QPushButton("↩ Put back in list")
        b_back.clicked.connect(self.put_back)
        drow2.addWidget(b_back)
        drow2.addStretch()
        b_clear = QPushButton("Clear completed")
        b_clear.setObjectName("quiet")
        b_clear.setToolTip("Empties this card. The record stays in log.csv.")
        b_clear.clicked.connect(self.clear_done)
        drow2.addWidget(b_clear)
        dn.addLayout(drow2)
        root.addWidget(done_card)
        root.addStretch()
        self._fill_folder_combos()
        self._set_mode(editing=False, clear=True)
        self.dated_card.setVisible(False)
        self.show_status(widget.status_message)

    # -- small builders
    @staticmethod
    def _row_label(text: str) -> QLabel:
        lab = QLabel(text)
        lab.setMinimumWidth(110)
        return lab

    def _date_row(self, lay: QVBoxLayout, text: str, tip: str) -> tuple[QCheckBox, QDateEdit]:
        row = QHBoxLayout()
        on = QCheckBox(text)
        on.setToolTip(tip)
        on.setMinimumWidth(110)
        edit = QDateEdit()
        edit.setCalendarPopup(True)
        edit.setDisplayFormat("ddd M/d/yyyy")
        edit.setDate(_qdate(self.w.today()))
        edit.setEnabled(False)
        on.toggled.connect(self._date_toggled)
        edit.dateChanged.connect(self._apply_editor)
        row.addWidget(on)
        row.addWidget(edit)
        row.addStretch()
        lay.addLayout(row)
        return on, edit

    def show_status(self, message: str):
        """A warning under the header, such as a list saved by a newer version of the app."""
        self.status_label.setText(message)
        self.status_label.setVisible(bool(message))

    # -- refresh from model
    def showEvent(self, e):
        self.refresh()
        super().showEvent(e)

    def resizeEvent(self, e):
        self.w.ui["panel_w"], self.w.ui["panel_h"] = self.width(), self.height()
        super().resizeEvent(e)

    def _details_resized(self, h: int):
        self.w.ui["details_h"] = h

    def _label(self, t: fc.Task, dated: bool = False, show_folder: bool = False) -> str:
        if t is self.w.active and self.w.timer.running:
            text = ("⏸ " if self.w.timer.paused else "▶ ") + task_label(t)
        else:
            text = ("⏸ " if t.spent_s else "") + task_label(t)  # paused to start another task
        if dated and first_date(t):
            text = f"📅 {short_date(first_date(t))} · {text}"
        elif t.repeat:
            text = "🔁 " + text
        if show_folder and t.folder:
            text += f"   📁 {t.folder}"
        return text

    def filter_path(self) -> str | None:
        """The folder the lists are filtered to, or None for all tasks."""
        return self.filter_combo.currentData() or None

    def _task_by_id(self, task_id) -> fc.Task | None:
        return next((t for t in self.w.plan.tasks if t.id == task_id), None)

    def selected_task(self) -> fc.Task | None:
        for lst in (self.dated_list, self.list):
            item = lst.currentItem()
            if item is not None and lst.currentRow() >= 0:
                return self._task_by_id(item.data(Qt.UserRole))
        return None

    def _selection_changed(self, lst: QListWidget, row: int):
        """Selecting in one list clears the other, so only one task is ever selected."""
        if self._updating:
            return
        if row >= 0:
            other = self.dated_list if lst is self.list else self.list
            self._updating = True
            other.setCurrentRow(-1)
            self._updating = False
        self._load_editor()

    def refresh(self, select=_KEEP):
        """Rebuild the lists. `select` is the id of the task to select (None for none); by default the
        selection stays on the same task."""
        t = self.selected_task()
        sel = (t.id if t is not None else None) if select is _KEEP else select
        self._updating = True
        self._fill_folder_combos()
        path = self.filter_path()
        dim = QColor(self.w.s["colors"]["text_dim"])
        dated, ranked = fc.display_sections(self.w.plan.tasks)
        for lst, tasks, is_dated in ((self.dated_list, dated, True), (self.list, ranked, False)):
            lst.clear()
            for t in tasks:
                if not fc.in_folder(t, path):
                    continue
                item = QListWidgetItem(self._label(t, is_dated, show_folder=path is None))
                item.setData(Qt.UserRole, t.id)
                tips = [t.details] if t.details.strip() else []
                if t.deadline:
                    tips.append(f"Deadline {short_date(t.deadline)}")
                if t.target:
                    tips.append(f"Aim to finish by {short_date(t.target)}")
                if self.w.is_asleep(t):
                    item.setForeground(dim)
                    tips.append(self._rest_text(t))
                if tips:
                    item.setToolTip("\n".join(tips))
                lst.addItem(item)
            lst.setCurrentRow(-1)
            if sel is not None:
                for i in range(lst.count()):
                    if lst.item(i).data(Qt.UserRole) == sel:
                        lst.setCurrentRow(i)
        self.dated_card.setVisible(self.dated_list.count() > 0)
        self.done_list.clear()
        for d in reversed(self.w.completed):
            when = d.finished_at[11:16] if len(d.finished_at) >= 16 else d.finished_at
            actual = f"{d.actual_min}m actual" if d.actual_min else "not timed"
            self.done_list.addItem(QListWidgetItem(f"{when}   {d.name}   ({d.estimate_min}m planned, {actual})"))
        self._updating = False
        self.show_status(self.w.status_message)
        self._load_editor()
        self.update_toggle()

    def _rest_text(self, t: fc.Task) -> str:
        nxt = fc.next_active_day(t, self.w.today())
        return f"Resting until {short_date(nxt.isoformat())}" if nxt else "Resting, since no days are chosen"

    def _fill_folder_combos(self):
        """Fill the filter and the editor's folder choice with the folder tree, when it changed."""
        tree = fc.folder_tree(self.w.folders, self.w.plan.tasks)
        if tuple(tree) == self._folder_key:
            return
        self._folder_key = tuple(tree)
        was_updating, self._updating = self._updating, True
        keep_filter = self.filter_combo.currentData() or ""
        keep_folder = self.ed_folder.currentData()
        self.filter_combo.clear()
        self.filter_combo.addItem("All folders", "")
        self.ed_folder.clear()
        self.ed_folder.addItem("No folder", "")
        for p, depth in tree:
            label = "    " * depth + p.split("/")[-1]
            self.filter_combo.addItem(label, p)
            self.ed_folder.addItem(label, p)
        self.ed_folder.addItem("New folder...", NEW_FOLDER)
        self.ed_folder.addItem("New subfolder...", NEW_SUBFOLDER)
        self.filter_combo.setCurrentIndex(max(0, self.filter_combo.findData(keep_filter)))
        self.ed_folder.setCurrentIndex(max(0, self.ed_folder.findData(keep_folder or "")))
        self._updating = was_updating

    def _set_folder_choice(self, path: str | None):
        was_updating, self._updating = self._updating, True
        self.ed_folder.setCurrentIndex(max(0, self.ed_folder.findData(path or "")))
        self._updating = was_updating
        self._last_folder = path

    def _folder_choice(self) -> str | None:
        data = self.ed_folder.currentData()
        if data in (NEW_FOLDER, NEW_SUBFOLDER):
            return self._last_folder
        return data or None

    def _filter_changed(self, *_):
        if self._updating:
            return
        self.refresh()
        if not self._editing:
            self._set_folder_choice(self.filter_path())  # a new task goes into the folder being shown

    def _on_timer_selected(self) -> bool:
        """True when Start should pause or resume the task on the timer: it is selected, or nothing is."""
        t = self.selected_task()
        return self.w.timer.running and (t is None or t is self.w.active)

    def update_toggle(self):
        timer = self.w.timer
        t = self.selected_task()
        self.btn_toggle.setEnabled(True)
        if self._on_timer_selected():
            self.btn_toggle.setText("⏸ Pause" if not timer.paused else "▶ Resume")
            self.btn_toggle.setToolTip(f"Timer is on: {timer.task}")
        elif t is not None:
            self.btn_toggle.setText("▶ Start selected")
            if self.w.is_asleep(t):
                self.btn_toggle.setEnabled(False)
                self.btn_toggle.setToolTip(f"Done for now. {self._rest_text(t)}.")
            else:
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
            self._show_fields(None, None, self.filter_path(), None)
            self._updating = False

    def _show_fields(self, deadline, target, folder, repeat):
        """Put a task's dates, folder and repeat rule into the editor (call with _updating set)."""
        today = self.w.today()
        for on, edit, value in ((self.ed_deadline_on, self.ed_deadline, deadline),
                                (self.ed_target_on, self.ed_target, target)):
            on.setChecked(bool(value))
            edit.setDate(_qdate(date.fromisoformat(value) if value else today))
        self._set_folder_choice(folder)
        rule = repeat or {}
        kind = rule.get("kind")
        self.ed_repeat.setCurrentIndex(REPEAT_KINDS.index(kind) if kind in REPEAT_KINDS else 0)
        days = rule.get("days", [])
        for key, cb in zip(fc.WEEKDAY_KEYS, self.ed_days):
            cb.setChecked(key in days)
        self.ed_every.setValue(int(rule.get("every", 2)))
        self._editor_anchor = rule.get("anchor")
        self._kept_repeat = copy.deepcopy(repeat) if repeat and kind not in REPEAT_KINDS else None
        self._sync_field_states()

    def _sync_field_states(self):
        """A repeating task cannot have dates; show the day boxes or the day count only for their rule."""
        kind = REPEAT_KINDS[self.ed_repeat.currentIndex()]
        for on, edit in ((self.ed_deadline_on, self.ed_deadline), (self.ed_target_on, self.ed_target)):
            on.setEnabled(kind is None)
            edit.setEnabled(kind is None and on.isChecked())
        self.days_row.setVisible(kind == "days")
        self.ed_every.setVisible(kind == "every")

    def _editor_values(self) -> dict:
        """Dates, folder and repeat rule as set in the editor."""
        kind = REPEAT_KINDS[self.ed_repeat.currentIndex()]
        if kind is None:
            repeat = copy.deepcopy(self._kept_repeat)
        elif kind == "days":
            repeat = {"kind": "days", "days": [k for k, cb in zip(fc.WEEKDAY_KEYS, self.ed_days) if cb.isChecked()]}
        elif kind == "every":
            repeat = {"kind": "every", "every": self.ed_every.value(),
                      "anchor": self._editor_anchor or self.w.today().isoformat()}
        else:
            repeat = {"kind": kind}
        dated = kind is None and repeat is None
        return {"deadline": _iso(self.ed_deadline.date()) if dated and self.ed_deadline_on.isChecked() else None,
                "target": _iso(self.ed_target.date()) if dated and self.ed_target_on.isChecked() else None,
                "folder": self._folder_choice(), "repeat": repeat}

    def _date_toggled(self, *_):
        if self._updating:
            return
        self._sync_field_states()
        self._apply_editor()

    def _repeat_changed(self, index: int):
        if self._updating:
            return
        kind = REPEAT_KINDS[index]
        self._kept_repeat = None  # a choice made here replaces a rule this app did not know
        self._updating = True
        if kind is not None:
            self.ed_deadline_on.setChecked(False)
            self.ed_target_on.setChecked(False)
        if kind == "days" and not any(cb.isChecked() for cb in self.ed_days):
            self.ed_days[self.w.today().weekday()].setChecked(True)
        if kind == "every" and not self._editor_anchor:
            self._editor_anchor = self.w.today().isoformat()  # counting starts today
        self._updating = False
        self._sync_field_states()
        self._apply_editor()

    def _editor_folder_changed(self, index: int):
        if self._updating:
            return
        data = self.ed_folder.itemData(index)
        if data in (NEW_FOLDER, NEW_SUBFOLDER):
            parent = self._last_folder if data == NEW_SUBFOLDER else None
            if parent:
                text, ok = QInputDialog.getText(self, "New subfolder", f"Name of the new subfolder in {parent}:")
            else:
                text, ok = QInputDialog.getText(self, "New folder", "Name of the new folder:")
            path = clean_folder(f"{parent}/{text}" if parent else text) if ok and clean_folder(text) else None
            if path:
                self.w.add_folder(path)
                self._fill_folder_combos()
            self._set_folder_choice(path or self._last_folder)
        else:
            self._last_folder = data or None
        self._apply_editor()

    def _load_editor(self, *_):
        if self._updating:
            return
        self.update_toggle()
        t = self.selected_task()
        if t is None:
            # Leaving edit mode clears the card; staying in add mode keeps whatever is being typed.
            self._set_mode(editing=False, clear=self._editing)
            return
        self._updating = True
        self.ed_name.setText(t.name)
        self.ed_time.set_value(t.estimate_min)
        self.ed_details.setPlainText(t.details)
        self._show_fields(t.deadline, t.target, t.folder, t.repeat)
        self._updating = False
        self._set_mode(editing=True, clear=False)

    def _item_for(self, task_id) -> tuple[QListWidget, QListWidgetItem] | None:
        for lst in (self.dated_list, self.list):
            for i in range(lst.count()):
                if lst.item(i).data(Qt.UserRole) == task_id:
                    return lst, lst.item(i)
        return None

    def _apply_editor(self, *_):
        if self._updating or not self._editing:
            return
        t = self.selected_task()
        if t is None:
            return
        v = self._editor_values()
        before = (t.deadline, t.target, t.folder, t.repeat)
        t.name = self.ed_name.text().strip() or t.name
        t.estimate_min = self.ed_time.value()
        t.details = self.ed_details.toPlainText()
        t.deadline, t.target, t.folder, t.repeat = v["deadline"], v["target"], v["folder"], v["repeat"]
        if t is self.w.active and self.w.timer.running:
            self.w.timer.task = t.name
            self.w.timer.estimate_min = t.estimate_min
        if (t.deadline, t.target, t.folder, t.repeat) != before:
            self.refresh()  # the task may move to the other list, or out of the folder shown
        else:
            found = self._item_for(t.id)
            if found is not None:
                lst, item = found
                self._updating = True
                item.setText(self._label(t, lst is self.dated_list, show_folder=self.filter_path() is None))
                item.setToolTip(t.details)
                self._updating = False
        self.w.changed()

    def _enter_in_name(self):
        if not self._editing:
            self.add_task()

    def new_task(self):
        self._updating = True
        self.list.setCurrentRow(-1)
        self.dated_list.setCurrentRow(-1)
        self._updating = False
        self._set_mode(editing=False, clear=True)
        self.update_toggle()
        self.ed_name.setFocus()

    # -- add / reorder / remove
    def add_task(self):
        name = self.ed_name.text().strip()
        if not name:
            self.ed_name.setFocus()
            return
        v = self._editor_values()
        self.w.plan.tasks.insert(0, fc.Task(name, self.ed_time.value(), self.ed_details.toPlainText().strip(),
                                            deadline=v["deadline"], target=v["target"], folder=v["folder"],
                                            repeat=v["repeat"]))
        self._set_mode(editing=False, clear=True)
        self.refresh(None)
        self.ed_name.setFocus()
        self.w.changed()

    def _shown_ranked_ids(self) -> list:
        return [self.list.item(i).data(Qt.UserRole) for i in range(self.list.count())]

    def _apply_ranked_order(self, shown_ids: list) -> None:
        """Reorder the ranked tasks shown in the list. Tasks hidden by the folder filter, and dated tasks,
        keep their places."""
        _, ranked = fc.display_sections(self.w.plan.tasks)
        shown = set(shown_ids)
        if sorted(t.id for t in ranked if t.id in shown) != sorted(shown_ids):
            return  # the list no longer matches the tasks (changed meanwhile); leave the order alone
        new = iter(shown_ids)
        self.w.plan.tasks = fc.reorder_ranked(self.w.plan.tasks, [next(new) if t.id in shown else t.id
                                                                  for t in ranked])

    def _rows_moved(self, *_):
        if self._updating:
            return
        self._apply_ranked_order(self._shown_ranked_ids())
        self.refresh()
        self.w.changed()

    def move_selected(self, delta: int):
        row = self.list.currentRow()
        dst = row + delta
        if self.list.currentItem() is None or not (0 <= row < self.list.count() and 0 <= dst < self.list.count()):
            return
        ids = self._shown_ranked_ids()
        ids.insert(dst, ids.pop(row))
        self._apply_ranked_order(ids)
        self.refresh()
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
        self.refresh(None)
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
        """Finish the selected task. With nothing selected, finish the task on the timer (or the next one)."""
        t = self.selected_task()
        self.w.done(t)
        self.refresh(t.id if t is not None and t in self.w.plan.tasks else None)

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
        t = fc.done_to_task(self.w.completed[i])
        self.w.plan.tasks.insert(0, t)
        del self.w.completed[i]
        self.refresh(t.id)
        self.w.changed()

    def clear_done(self):
        self.w.completed.clear()
        self.refresh()
        self.w.changed()


# ---------------------------------------------------------------- the widget

class FocusWidget(QWidget):
    status_changed = Signal(str)  # a warning for the tray tooltip, such as a list saved by a newer app

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
        self._last_doc: dict | None = None  # the list as last read or written; changes are stamped against it
        self._stamp_import = False  # True after reading a file older than version 3 (see _stamped_doc)
        self._blocked = False  # True once the shared file turned out to be from a newer app, so it is never written
        self.status_message = ""  # a warning for the panel header and the tray tooltip
        got = self._read_shared()
        self._adopt(got[0] if got else fc.State(fc.Plan(), 0, fc.TimerState()))
        self._last_doc = self._snapshot()
        if got and got[1]:
            self._mark_imported()
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
        self.save()  # writes the shared file only if reading it needed an upgrade to version 3

    # -- pulse property for the animation
    def get_pulse(self) -> float:
        return self._pulse

    def set_pulse(self, v: float) -> None:
        self._pulse = v
        self.update()

    pulse = Property(float, get_pulse, set_pulse)

    # -- model helpers
    def today(self) -> date:
        return datetime.now().date()

    def is_asleep(self, t: fc.Task) -> bool:
        """A repeating task that is done for today, or not due today. It cannot be started."""
        return bool(t.repeat) and not fc.is_active(t, self.today())

    def display_order(self) -> list[fc.Task]:
        """Tasks in the order the panel lists them, with dated tasks by date first and then the rest in your order."""
        dated, ranked = fc.display_sections(self.plan.tasks)
        return dated + ranked

    def current_task(self) -> fc.Task | None:
        """The task on the timer, or when idle the first task that can be started (shown as 'Up next')."""
        if self.timer.running and self.active is not None:
            return self.active
        return next((t for t in self.display_order() if not self.is_asleep(t)), None)

    def changed(self):
        """Call after any change to the tasks or timer: write it to the shared folder and repaint."""
        self._shared_dirty = True
        self.save()
        self.update()
        self._refresh_toggle_labels()

    def _set_status(self, message: str):
        self.status_message = message
        self.status_changed.emit(message)
        if getattr(self, "panel", None) is not None:
            self.panel.show_status(message)

    # -- folders
    def state_obj(self) -> fc.State:
        """The live list as one State (the same task objects, not copies)."""
        idx = next((i for i, t in enumerate(self.plan.tasks) if t is self.active), 0)
        return fc.State(self.plan, idx, self.timer, None, self.completed, self.deleted, self.order_updated_at,
                        self.planner, self.folders, self.folders_updated_at)

    def add_folder(self, path: str):
        """Add a folder (and its parent folders) to the known folders."""
        missing = [p for p in folder_with_parents(path) if p not in self.folders]
        if missing:
            self.folders = self.folders + missing
            self.changed()

    def rename_folder(self, old: str, new: str):
        st = self.state_obj()
        fc.rename_folder(st, old, new)
        self.folders = st.folders
        self.changed()
        if self.panel.isVisible():
            self.panel.refresh()

    def delete_folder(self, path: str):
        st = self.state_obj()
        fc.delete_folder(st, path)
        self.folders = st.folders
        self.changed()
        if self.panel.isVisible():
            self.panel.refresh()

    def open_folders(self):
        FoldersDialog(self).exec()

    # -- the list shared through Box
    def _adopt(self, st: fc.State):
        """Take the whole list from a loaded state (Box mode replaces everything)."""
        self.plan, self.completed, self.timer = st.plan, st.done, st.timer
        self.deleted, self.order_updated_at = dict(st.deleted), st.order_updated_at
        self.planner = st.planner if st.planner is not None else copy.deepcopy(fc.DEFAULT_PLANNER)
        self.folders, self.folders_updated_at = list(st.folders), st.folders_updated_at
        self.timer.checkpoints = tuple(sorted(self.s["checkpoints"]))
        self.active: fc.Task | None = None
        if self.timer.running:
            self.active = fc.find_active(self.plan.tasks, st.current_index, self.timer.task, self.timer.task_id)
            if self.active is None:
                self.timer.skip()
        self.timer.tick(datetime.now())  # catch up the clock; checkpoints already passed do not pulse or chime

    def _snapshot(self) -> dict:
        """The live list as a version 3 document (a copy). It is what the next save compares against."""
        return self.build_doc()

    def _mark_imported(self):
        """The shared file was older than version 3. Save it once as version 3 with fresh order, folder and
        timer stamps, so a later merge never has to break a tie between two unstamped copies."""
        self._stamp_import = True
        self._shared_dirty = True

    def _read_shared(self) -> tuple[fc.State, bool] | None:
        """(the shared list as saved, True if the file was older than version 3), or None while it is
        missing, only half synced, or saved by a newer app (then nothing is ever written to it again)."""
        sig = _file_sig(self.state_path)
        try:
            text = self.state_path.read_text(encoding="utf-8")
            raw = json.loads(text)
            st = fc.doc_to_state(raw)
            old = int(raw.get("version", 1)) < fc.STATE_VERSION
        except fc.NewerVersionError:
            self._blocked = True
            self._set_status(NEWER_FILE_MESSAGE)
            return None
        except (OSError, ValueError, AttributeError, KeyError, TypeError):
            return None
        self._shared_sig, self._shared_text = sig, text
        return st, old

    def check_shared(self):
        """Every few seconds: show changes the other computer saved, or retry a write Box blocked."""
        if self._blocked:
            return
        if self._shared_dirty:
            self._save_shared()
            return
        if QApplication.activeModalWidget() or QApplication.activePopupWidget():
            return  # a dialog or menu is holding tasks from the current list; look again once it closes
        if _file_sig(self.state_path) == self._shared_sig:
            return
        before = self._shared_text
        got = self._read_shared()
        if got is not None and self._shared_text != before:
            self._apply_state(*got)

    def _apply_state(self, st: fc.State, old_file: bool = False):
        """Show a list the other computer saved."""
        was = (self.timer.running, self.timer.task_id, self.timer.task)
        self._adopt(st)
        self._last_doc = self._snapshot()
        if old_file:
            self._mark_imported()
        same_task = was == (self.timer.running, self.timer.task_id, self.timer.task)
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

    # -- building and writing the shared document (one place each)
    def build_doc(self) -> dict:
        """The whole live list as a state.json version 3 document, not stamped yet."""
        doc = fc.state_to_doc(self.state_obj())
        doc["geometry"] = None  # the window position stays on each computer
        return doc

    def _stamped_doc(self) -> dict:
        """build_doc() with change stamps set wherever it differs from the list as last read or written."""
        now = fc.now_iso()
        doc = fc.stamp_changes(self._last_doc, self.build_doc(), now)
        if self._stamp_import:
            doc["order_updated_at"] = doc["folders_updated_at"] = doc["timer"]["updated_at"] = now
        return doc

    def _take_stamps(self, doc: dict):
        """Copy the stamps of a written document back onto the live list, and remember it for the next save."""
        tasks = {x["id"]: x["updated_at"] for x in doc["plan"]["tasks"]}
        for t in self.plan.tasks:
            t.updated_at = tasks.get(t.id, t.updated_at)
        done = {x["id"]: x["updated_at"] for x in doc["done"]}
        for d in self.completed:
            d.updated_at = done.get(d.id, d.updated_at)
        self.timer.updated_at = doc["timer"]["updated_at"]
        self.deleted, self.order_updated_at = dict(doc["deleted"]), doc["order_updated_at"]
        self.planner, self.folders_updated_at = copy.deepcopy(doc["planner"]), doc["folders_updated_at"]
        self._last_doc = doc
        self._stamp_import = False

    def _save_shared(self):
        """Write the shared file (Box mode). Stamps are set right before writing; an unchanged list is not
        written; a file saved by a newer app is never written."""
        if self._blocked:
            return
        doc = self._stamped_doc()
        if doc == self._last_doc and self._shared_text is not None:
            self._shared_dirty = False
            return
        try:
            text = fc.save_state_obj(self.state_path, fc.doc_to_state(doc))
        except OSError:
            return  # Box can hold the file for a moment while it uploads; check_shared tries again
        self._take_stamps(doc)
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
        """Start the only task, or pop up the list so you pick which one to start. Sleeping repeats are left out."""
        tasks = [t for t in self.display_order() if not self.is_asleep(t)]
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
        if self.is_asleep(task):
            return  # a repeat that is done for today, or not due today, waits for its next day
        if self.timer.running and self.active is not None:
            self.timer.pause(now)
            self.active.spent_s = self.timer.elapsed_s
        self.timer.start(task.name, task.estimate_min, now, spent_s=task.spent_s, task_id=task.id)
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
        """Finish `task` (default: the task on the timer, or Up next). Logs it and adds it to Completed.
        A repeating task stays on the list and sleeps until its next day."""
        task = task if task is not None else self.current_task()
        if task is None:
            return
        if self.is_asleep(task) and not (task is self.active and self.timer.running):
            return  # already done for today
        now = datetime.now()
        if task is self.active and self.timer.running:
            rec = self.timer.done(now)
            self.active = None
            self._set_asking(False)
        else:
            rec = fc.BlockRecord(task.name, task.estimate_min, int(round(task.spent_s / 60)), now)
        self._log(rec)
        entry = fc.finish_task(task, now, rec.actual_min)
        self.completed[:] = [d for d in self.completed if d.id != entry.id] + [entry]
        if not task.repeat:
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
            return "🥳" if self.current_task() is None else "🌱"
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
            title = ("All done for today" if self.plan.tasks else
                     "All done, the list is empty" if self.completed else "Double-click to add tasks")
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
            todo = [t for t in self.plan.tasks if not self.is_asleep(t) or t is self.active]
            fin = fc.list_end(todo, self.active, self.timer, now)
            left = len(todo) - (1 if self.timer.running else 0)
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
        menu.addAction(QAction("Folders...", menu, triggered=widget.open_folders))
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
        widget.status_changed.connect(self.show_status)
        self.show_status(widget.status_message)

        mods, key = parse_hotkey(settings.get("hotkey_show_hide", "ctrl+alt+f"))
        self.filter = HotkeyFilter(self.toggle)
        app.installNativeEventFilter(self.filter)
        if key and not ctypes.windll.user32.RegisterHotKey(None, HOTKEY_ID, mods | 0x4000, key):
            self.tray.showMessage("Focus Tool", "Could not register the show/hide hotkey (already in use).", icon, 5000)

    def show_status(self, message: str):
        """Put a warning (such as a list saved by a newer app) in the tray tooltip, and pop it up once."""
        self.tray.setToolTip(f"Focus Tool\n{message}" if message else "Focus Tool")
        if message:
            self.tray.showMessage("Focus Tool", message, QSystemTrayIcon.Warning, 8000)

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
