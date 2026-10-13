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
each computer, in %APPDATA%\\FocusTool\\local.json. state.json is version 4: every
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
import queue
import re
import shutil
import sys
import threading
import time
import winsound
from datetime import date, datetime, timedelta, timezone
from datetime import datetime as _real_datetime  # for parsing only; see the note below
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from PySide6.QtCore import (QAbstractNativeEventFilter, QDate, QEasingCurve, QEvent, QItemSelectionModel, QPoint,
                            QPropertyAnimation, QRect, QRectF, QSize, Qt, QTimer, Property, Signal)
from PySide6.QtGui import QAction, QColor, QCursor, QFont, QFontMetrics, QIcon, QPainter, QPen, QTextCharFormat
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QButtonGroup, QCalendarWidget, QCheckBox, QComboBox,
                               QDialog, QFrame, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMenu, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox,
                               QSystemTrayIcon, QVBoxLayout, QWidget)

import focus_core as fc
import gh_store
import planner

# The tests replace this module's `datetime` with a clock that only has now(), so this file only ever
# calls datetime.now(). Dates come from `date`, change stamps from fc.now_iso(), and text is parsed with
# _real_datetime.

APP_NAME = "FocusTool"
CHECK_EVERY_MS = 250
AUTOSAVE_EVERY_MS = 30_000
SYNC_CHECK_MS = 2000      # how often to look for changes the other computer saved to Box
CELEBRATE_MS = 3000
TIME_PRESETS = (5, 10, 15, 25, 30, 45, 60, 90)
MINI_SIZE = 64            # the shrunk widget is a circle this many pixels wide
DETAILS_DEFAULT_H = 140   # starting height of the details box in the task panel
DETAILS_MIN_H = 60
NEWER_FILE_MESSAGE = ("Your list was saved by a newer Focus Tool. Please update this app. "
                      "Changes made here are not saved until you do.")
SYNC_POLL_S = 15.0        # how often GitHub mode looks for changes
SYNC_DEBOUNCE_S = 2.0     # GitHub mode pushes this long after a local change
AMBER = "#B7791F"
DUE_RED = "#B5523B"  # a task with a hard deadline
NOTES_SAVE_MS = 800  # folder notes are saved this long after the last key press
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


RECENT_S = 5.0  # a file saved this recently is compared by its text too (see file_changed)


def file_changed(path: Path, sig: tuple[int, int] | None, text: str | None) -> bool:
    """True if `path` differs from the copy last read or written (`sig`, `text`). Two saves in the same
    file-time tick with the same size give the same sig, so a file saved in the last few seconds is also
    compared by its text. These files are small, so that read is cheap."""
    now_sig = _file_sig(path)
    if now_sig != sig:
        return True
    if now_sig is None or text is None or time.time() - now_sig[0] / 1e9 > RECENT_S:
        return False
    try:
        return path.read_text(encoding="utf-8") != text
    except OSError:
        return False


def _read_json(path: Path) -> dict:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _write_json(path: Path, d: dict) -> None:
    _write_atomic(path, json.dumps(d, indent=2))


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
        merged.folder_notes, merged.progress = have.folder_notes, have.progress
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


# ---------------------------------------------------------------- GitHub sync (no Qt here)

def make_sync_engine(cfg: dict) -> gh_store.SyncEngine:
    """The GitHub sync engine for cfg = {owner, repo, branch, token}. Its cache, which also keeps edits
    not yet pushed, is %APPDATA%\\FocusTool\\github-cache.json. The tests replace this function."""
    store = gh_store.GitHubStore(cfg["owner"], cfg["repo"], cfg["token"], branch=cfg.get("branch") or "main")
    return gh_store.SyncEngine(store, local_dir() / "github-cache.json")


def load_token() -> str | None:
    """The saved GitHub token, or None (also when the Credential Manager cannot be used)."""
    try:
        return gh_store.load_token() or None
    except Exception:
        return None


def _short(ex) -> str:
    text = str(getattr(ex, "message", "") or ex).strip().splitlines()
    text = text[0] if text else type(ex).__name__
    return text if len(text) <= 70 else text[:67] + "..."


def _comparable(doc: dict) -> str:
    """A doc as text, leaving out what does not count as a change (current_index, geometry, timer marks)."""
    d = copy.deepcopy(doc)
    d.pop("current_index", None)
    d.pop("geometry", None)
    for k in ("fired", "checkpoints"):
        (d.get("timer") or {}).pop(k, None)
    return fc.canonical(d)


def same_doc(a: dict | None, b: dict | None) -> bool:
    if a is None or b is None:
        return a is b
    return _comparable(a) == _comparable(b)


def _write_atomic(path: Path, text: str, tries: int = 4) -> None:
    """Write a temp file, then swap it in. Windows refuses the swap while another program (often a virus
    scanner) has the file open for a moment, so a refused swap is tried again a few times."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    for attempt in range(tries):
        try:
            tmp.replace(path)
            return
        except PermissionError:
            if attempt == tries - 1:
                raise
            time.sleep(0.05)


def read_sync_cache(path: Path) -> dict:
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def store_local_doc(cache_path: Path, doc: dict, unsynced: bool) -> None:
    """Keep this computer's list in the sync cache, so edits made offline survive a restart. Only the
    sync worker (or code holding its lock) calls this, because SyncEngine rewrites the same file."""
    cache = read_sync_cache(cache_path)
    cache["local_doc"], cache["local_unsynced"] = doc, bool(unsynced)
    try:
        _write_atomic(Path(cache_path), json.dumps(cache, ensure_ascii=False))
    except OSError:
        pass


def run_job(job: dict) -> dict:
    """Do one piece of GitHub work (network, so never on the Qt thread except in tests) and return a
    result for FocusWidget._apply_job_result. Kinds are "sync" (sync job["doc"], then read
    day-plan.json), "put_plan" (upload job["plan"]) and "forget" (delete job["paths"], used after a
    Disconnect). A job whose job["cancel"] event is set does nothing more and its result is ignored."""
    kind, engine, cancel = job["kind"], job.get("engine"), job.get("cancel")
    out = {"kind": kind, "engine": engine}
    if kind == "forget":
        latest = job.get("latest")
        if latest is not None and latest[0] != job.get("session"):
            out["skipped"] = True  # a newer GitHub session uses these file names now; leave its files alone
            return out
        for path in job["paths"]:
            try:
                Path(path).unlink()
            except OSError:
                pass
        return out
    if cancel is not None and cancel.is_set():
        out["cancelled"] = True
        return out
    if kind == "put_plan":
        try:
            gh_store.put_day_plan(engine.store, job["plan"])
            out["error"] = None
        except Exception as ex:
            out["error"] = _short(ex)
        return out
    doc = job["doc"]
    store_local_doc(engine.cache_path, doc, True)
    try:
        res = engine.sync(doc)
    except fc.NewerVersionError:
        out["newer"] = True
        return out
    except Exception as ex:  # a bug or an odd answer must never stop the app
        res = gh_store.SyncResult(doc, False, False, "error", _short(ex), None)
    if cancel is not None and cancel.is_set():
        out["cancelled"] = True  # disconnected meanwhile; the "forget" job after this one cleans up
        return out
    out["result"] = res
    store_local_doc(engine.cache_path, res.doc, res.status != "ok")
    if res.status == "ok":
        try:
            out["plan"] = gh_store.fetch_day_plan(engine.store)
        except Exception:
            pass  # the list synced; the plan is read again next time
    return out


# ---------------------------------------------------------------- day plan and working hours (no Qt here)

def parse_date(text) -> date | None:
    """'2026-10-16' as a date; None for anything else (a device may have saved something odd)."""
    try:
        return date.fromisoformat(text) if isinstance(text, str) else None
    except ValueError:
        return None


def local_to_utc(naive: datetime, tz: str) -> datetime:
    """A naive local time in the planner's time zone, as an aware UTC time."""
    try:
        return naive.replace(tzinfo=ZoneInfo(tz)).astimezone(timezone.utc)
    except Exception:
        return naive.astimezone(timezone.utc)


def made_time(plan: dict) -> str:
    """When a day plan was made, as local 'HH:MM' in its time zone ('' if unknown)."""
    try:
        made = _real_datetime.strptime(plan["generated_at"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        return made.astimezone(ZoneInfo(plan.get("tz") or "America/Chicago")).strftime("%H:%M")
    except Exception:
        return ""


_SPAN_RE = re.compile(r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})")


def parse_windows(text: str) -> list[str]:
    """'13:30-18:00, 9:00-12:00' as ['09:00-12:00', '13:30-18:00']. Empty text means no working time.
    Raises ValueError with a plain message for anything else."""
    spans = []
    for part in [p.strip() for p in text.split(",") if p.strip()]:
        m = _SPAN_RE.fullmatch(part)
        if not m:
            raise ValueError(f"'{part}' should look like 09:00-12:00.")
        h1, m1, h2, m2 = (int(x) for x in m.groups())
        if max(h1, h2) > 23 or max(m1, m2) > 59:
            raise ValueError(f"'{part}' is not a time of day.")
        a, b = h1 * 60 + m1, h2 * 60 + m2
        if a >= b:
            raise ValueError(f"In '{part}' the start must be before the end.")
        spans.append((a, b, f"{h1:02d}:{m1:02d}-{h2:02d}:{m2:02d}"))
    spans.sort()
    for x, y in zip(spans, spans[1:]):
        if y[0] < x[1]:
            raise ValueError(f"'{x[2]}' and '{y[2]}' overlap.")
    return [s[2] for s in spans]


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
    QComboBox, QSpinBox, QFrame#card QComboBox, QFrame#card QSpinBox {{
        background: {c['background']}; border: 1px solid {c['border']}; border-radius: 8px; padding: 3px 8px; }}
    QComboBox:disabled, QPushButton:disabled {{ color: {c['text_dim']}; }}
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
    QPushButton#date, QFrame#card QPushButton#date {{ background: {c['background']}; color: {c['text']};
        border: 1px solid {c['border']}; border-radius: 8px; padding: 3px 10px; }}
    QPushButton#date:hover, QFrame#card QPushButton#date:hover {{ border-color: {c['disk_remaining']}; }}
    QPushButton#date:disabled, QFrame#card QPushButton#date:disabled {{ color: {c['text_dim']}; }}
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
    """'2026-10-16' as 'Fri 10/16' (English day names whatever the Windows language is). Text that is
    not a date comes back as it is."""
    d = parse_date(iso)
    return f"{DAY_LABELS[d.weekday()]} {d.month}/{d.day}" if d else str(iso)


def parse_time_text(text: str) -> str | None:
    """A time typed by hand as "HH:MM" (24-hour): "17:00", "5pm", "5:30 pm", "9am" and "0930" all work.
    Empty text gives None; text that is not a time raises ValueError."""
    t = (text or "").strip().lower().replace(".", "")
    if not t:
        return None
    m = re.fullmatch(r"(\d{1,2})(?::?(\d{2}))?\s*(am|pm|a|p)?", t)
    if not m:
        raise ValueError(text)
    hh, mm, half = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if half:
        if not 1 <= hh <= 12:
            raise ValueError(text)
        hh = hh % 12 + (12 if half.startswith("p") else 0)
    if hh > 23 or mm > 59:
        raise ValueError(text)
    return f"{hh:02d}:{mm:02d}"


def due_text(t: fc.Task) -> str:
    """How the list shows a task's dates: "📅 Due Fri 10/16 5:00pm" for a hard deadline, "🎯 Aim Mon 10/19"
    for an aim-to-finish date, both when it has both. "" for no dates."""
    due = ""
    if t.deadline:
        hm = fc.fmt_hm(t.deadline_time)
        due = f"📅 Due {short_date(t.deadline)}" + (f" {hm}" if hm else "")
    if t.target:
        return f"{due}, 🎯 aim {short_date(t.target)}" if due else f"🎯 Aim {short_date(t.target)}"
    return due


def first_date(t: fc.Task) -> str | None:
    """The earlier of a task's deadline and aim-to-finish date; the dated list is sorted by it."""
    dates = [x for x in (t.deadline, t.target) if x]
    return min(dates) if dates else None


def clean_folder(text: str | None) -> str | None:
    """A folder path typed by hand, tidied, so 'Puzzle / Tiles/' becomes 'Puzzle/Tiles'. Empty text gives None."""
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


class WorkingHoursDialog(QDialog):
    """The hours the planner may fill, one line per day, such as '09:00-12:00, 13:30-18:00'."""

    def __init__(self, widget: "FocusWidget"):
        super().__init__(widget.panel if widget.panel.isVisible() else None)
        self.w = widget
        self.setWindowTitle("Working hours")
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        self.setStyleSheet(panel_stylesheet(widget.s["colors"]))
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        hint = QLabel("The planner fills these hours. Separate two spans with a comma. Leave a day empty for "
                      "no work that day.")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        windows = (widget.planner or fc.DEFAULT_PLANNER).get("windows", {})
        self.edits: dict[str, QLineEdit] = {}
        for key, label in zip(fc.WEEKDAY_KEYS, DAY_LABELS):
            row = QHBoxLayout()
            lab = QLabel(label)
            lab.setMinimumWidth(40)
            row.addWidget(lab)
            edit = QLineEdit(", ".join(windows.get(key, [])))
            edit.setPlaceholderText("No work")
            edit.setMinimumWidth(260)
            self.edits[key] = edit
            row.addWidget(edit)
            lay.addLayout(row)
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setStyleSheet("color: #B5523B;")
        lay.addWidget(self.error)
        brow = QHBoxLayout()
        brow.addStretch()
        b_cancel = QPushButton("Cancel")
        b_cancel.clicked.connect(self.reject)
        b_save = QPushButton("Save")
        b_save.setObjectName("primary")
        b_save.clicked.connect(self._save)
        brow.addWidget(b_cancel)
        brow.addWidget(b_save)
        lay.addLayout(brow)

    def apply(self) -> str | None:
        """Check every line and save them. Returns the problem as text, or None when saved."""
        windows = {}
        for key, label in zip(fc.WEEKDAY_KEYS, DAY_LABELS):
            try:
                windows[key] = parse_windows(self.edits[key].text())
            except ValueError as ex:
                message = f"Check {label}. {ex}"
                self.error.setText(message)
                return message
        self.error.clear()
        self.w.set_working_hours(windows)
        return None

    def _save(self):
        if self.apply() is None:
            self.accept()


class GitHubDialog(QDialog):
    """Connect to the private GitHub repo that holds the list, so the phone and this computer share it."""
    checked = Signal(str)  # the Test button's answer, sent from its thread

    def __init__(self, widget: "FocusWidget"):
        super().__init__(widget.panel if widget.panel.isVisible() else None)
        self.w = widget
        self.setWindowTitle("GitHub sync")
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        self.setStyleSheet(panel_stylesheet(widget.s["colors"]))
        cfg = widget._gh_cfg or {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        hint = QLabel("Your list is kept in a private GitHub repository that the phone also reads. The token "
                      "is saved in the Windows Credential Manager, never in a file.")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        self.owner = QLineEdit(cfg.get("owner", ""))
        self.repo = QLineEdit(cfg.get("repo", "") or "focus-data")
        self.branch = QLineEdit(cfg.get("branch", "") or "main")
        self.token = QLineEdit()
        self.token.setEchoMode(QLineEdit.Password)
        self.token.setPlaceholderText("Saved. Leave empty to keep it." if load_token()
                                      else "Fine-grained token with Contents read and write")
        for label, edit in (("Owner", self.owner), ("Repository", self.repo), ("Branch", self.branch),
                            ("Token", self.token)):
            row = QHBoxLayout()
            lab = QLabel(label)
            lab.setMinimumWidth(110)
            row.addWidget(lab)
            edit.setMinimumWidth(280)
            row.addWidget(edit)
            lay.addLayout(row)
        self.result = QLabel(widget.sync_status or ("Not connected. The list is kept in Box only."
                                                    if widget.mode == "box" else ""))
        self.result.setObjectName("hint")
        self.result.setWordWrap(True)
        lay.addWidget(self.result)
        self.checked.connect(self.result.setText)
        brow = QHBoxLayout()
        for label, fn in (("Test", self.test), ("Disconnect", self.disconnect)):
            b = QPushButton(label)
            b.clicked.connect(lambda _=False, fn=fn: fn())
            brow.addWidget(b)
        brow.addStretch()
        b_close = QPushButton("Close")
        b_close.clicked.connect(self.reject)
        brow.addWidget(b_close)
        b_save = QPushButton("Save")
        b_save.setObjectName("primary")
        b_save.clicked.connect(lambda: self.save())
        brow.addWidget(b_save)
        lay.addLayout(brow)

    def _config(self) -> dict:
        return {"owner": self.owner.text().strip(), "repo": self.repo.text().strip(),
                "branch": self.branch.text().strip() or "main"}

    def test(self):
        cfg, token = self._config(), self.token.text().strip() or load_token()
        if not (cfg["owner"] and cfg["repo"] and token):
            self.result.setText("Fill in the owner, the repository and a token first.")
            return
        self.result.setText("Checking...")
        try:
            store = make_sync_engine({**cfg, "token": token}).store
        except Exception as ex:
            self.result.setText(f"Could not check ({_short(ex)}).")
            return

        def check():  # network, so on its own thread; the answer comes back through the `checked` signal
            try:
                self.checked.emit(store.check())
            except RuntimeError:
                pass  # the dialog was closed meanwhile

        threading.Thread(target=check, daemon=True).start()

    def save(self) -> bool:
        cfg, typed = self._config(), self.token.text().strip()
        token = typed or load_token()
        if not (cfg["owner"] and cfg["repo"] and token):
            self.result.setText("Fill in the owner, the repository and a token first.")
            return False
        if typed:
            try:
                gh_store.save_token(typed)
            except Exception as ex:
                self.result.setText(f"Could not save the token ({_short(ex)}).")
                return False
        self.w.connect_github(cfg, token)
        self.accept()
        return True

    def disconnect(self):
        self.w.disconnect_github()
        self.token.clear()
        self.result.setText("Disconnected. The list is kept in Box only.")


# ---------------------------------------------------------------- task panel

_KEEP = object()  # passed to TaskPanel.refresh to keep the current selection
EDITOR_ROW = "\x00editor"    # item data of the list row that holds the open task's editor
DIVIDER_ROW = "\x00divider"  # item data of the line between dated and other tasks (By date)
SORT_CHOICES = ("My order", "By date")


class DateButton(QPushButton):
    """A date, or no date. Clicking opens a calendar on that date (or on today), with today marked."""
    changed = Signal()  # the date changed
    picked = Signal(object)  # a date (or None for "No date") was picked in the calendar, changed or not

    def __init__(self, today_fn, mark_color: str, empty_text: str = "No date"):
        super().__init__()
        self._today = today_fn
        self._mark = mark_color
        self._empty = empty_text
        self._value: date | None = None
        self.popup: QFrame | None = None
        self.setObjectName("date")
        self.setToolTip("Click to pick a date")
        self.clicked.connect(self.open_calendar)
        self.set_value(None)

    def value(self) -> date | None:
        return self._value

    def set_value(self, d: date | None):
        """Show `d` without telling anyone (for loading a task into the editor)."""
        self._value = d
        self.setText((f"📅 {d:%a} {d.month}/{d.day}/{d.year}" if d else self._empty) + "  ▾")

    def pick(self, d: date | None):
        """Choose `d`, as a click in the calendar does."""
        if self.popup is not None:
            self.popup.close()
            self.popup = None
        if d != self._value:
            self.set_value(d)
            self.changed.emit()
        self.picked.emit(d)

    def open_calendar(self):
        today = self._today()
        pop = QFrame(self.window(), Qt.Popup)
        lay = QVBoxLayout(pop)
        lay.setContentsMargins(8, 8, 8, 8)
        cal = QCalendarWidget()
        cal.setVerticalHeaderFormat(QCalendarWidget.NoVerticalHeader)
        mark = QTextCharFormat()
        mark.setFontWeight(QFont.Bold)
        mark.setFontUnderline(True)
        mark.setBackground(QColor(self._mark))
        mark.setForeground(QColor("#1F6F5C"))
        cal.setDateTextFormat(_qdate(today), mark)
        cal.setSelectedDate(_qdate(self._value or today))
        cal.clicked.connect(lambda q: self.pick(date(q.year(), q.month(), q.day())))
        lay.addWidget(cal)
        row = QHBoxLayout()
        b_today = QPushButton(f"Today ({today.month}/{today.day})")
        b_today.clicked.connect(lambda: self.pick(today))
        row.addWidget(b_today)
        row.addStretch()
        b_none = QPushButton("No date")
        b_none.clicked.connect(lambda: self.pick(None))
        row.addWidget(b_none)
        lay.addLayout(row)
        self.popup = pop
        pop.adjustSize()
        at = self.mapToGlobal(QPoint(0, self.height()))
        screen = QApplication.screenAt(at) or QApplication.primaryScreen()
        room = screen.availableGeometry()
        at.setX(min(at.x(), room.right() - pop.width()))
        if at.y() + pop.height() > room.bottom():
            at.setY(self.mapToGlobal(QPoint(0, 0)).y() - pop.height())  # no room below, so open above
        pop.move(at)
        pop.show()


class TaskList(QListWidget):
    """The task list. Several tasks can be picked with Ctrl or Shift; choosing a row in code picks only it."""

    def setCurrentRow(self, row: int, command=None):
        if command is None:
            command = QItemSelectionModel.ClearAndSelect if row >= 0 else QItemSelectionModel.Clear
        super().setCurrentRow(row, command)


class TaskPanel(QWidget):
    """The All tasks window: a one-line box to add a task, one list (in your order or by date, for all tasks
    or one folder), a folder's notes when one folder is shown, a task's editor that unfolds under it in the
    list, and the completed tasks. It is a normal window with a taskbar button, so it can be minimized."""

    def __init__(self, widget: "FocusWidget"):
        super().__init__(None, Qt.Window | Qt.WindowStaysOnTopHint)
        self.w = widget
        self.setWindowTitle("Focus Tool")
        self.setWindowIcon(QIcon(str(resource_dir() / "assets" / "icon.ico")))
        self.setStyleSheet(panel_stylesheet(widget.s["colors"]))
        self.resize(int(widget.ui.get("panel_w", 560)), int(widget.ui.get("panel_h", 860)))
        self._updating = False
        self._folder_key: tuple | None = None  # the folder tree the two folder combos were last filled with
        self._last_folder: str | None = None  # the editor's folder before "New folder..." was picked
        self._editor_anchor: str | None = None  # start date of an "Every N days" repeat being edited
        self._kept_repeat: dict | None = None  # a repeat rule this app does not know; kept unless changed here
        self._editor_item: QListWidgetItem | None = None  # the list row the editor sits in, under its task
        self._shown_time: str | None = None  # the open task's deadline time as saved (kept while the box is invalid)
        self._picks = 0  # counts selection changes, to tell a click that opens a task from one that folds it
        self._picks_at_press = -1
        c = widget.s["colors"]

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 18)
        root.setSpacing(10)

        # -- header
        head = QHBoxLayout()
        title = QLabel("🌱 Tasks")
        title.setObjectName("title")
        head.addWidget(title)
        self.sync_label = QLabel()  # GitHub mode, such as "Synced 18:42"
        self.sync_label.setObjectName("hint")
        head.addWidget(self.sync_label)
        head.addStretch()
        self.btn_toggle = QPushButton("▶ Start")
        self.btn_toggle.setObjectName("primary")
        self.btn_toggle.clicked.connect(self.toggle_selected)
        head.addWidget(self.btn_toggle)
        b_done = QPushButton("✓ Done")
        b_done.setToolTip("Finish the task selected in the list")
        b_done.clicked.connect(self.done_selected)
        head.addWidget(b_done)
        self.btn_full = QPushButton("⛶")
        self.btn_full.setToolTip("Full screen (F11). Esc leaves it.")
        self.btn_full.clicked.connect(self.toggle_full_screen)
        head.addWidget(self.btn_full)
        self.btn_min = QPushButton("–")
        self.btn_min.setToolTip("Minimize to the taskbar. Enter or ☰ on the widget brings it back.")
        self.btn_min.clicked.connect(self.minimize)
        head.addWidget(self.btn_min)
        root.addLayout(head)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("color: #B5523B; font-weight: 600;")
        self.status_label.hide()
        root.addWidget(self.status_label)

        # -- add a task: type a name, press Enter
        self.ed_add = QLineEdit()
        self.ed_add.setPlaceholderText("+ Add a task: type it and press Enter")
        self.ed_add.returnPressed.connect(self.add_task)
        root.addWidget(self.ed_add)

        # -- what to show, and in which order
        show = QHBoxLayout()
        show.addWidget(QLabel("Show"))
        self.filter_combo = QComboBox()
        self.filter_combo.setToolTip("Show one folder and its subfolders, or all tasks")
        self.filter_combo.currentIndexChanged.connect(self._filter_changed)
        show.addWidget(self.filter_combo, 1)
        show.addWidget(QLabel("Sort"))
        self.sort_combo = QComboBox()
        self.sort_combo.addItems(list(SORT_CHOICES))
        self.sort_combo.setToolTip("My order: the order you set by dragging. "
                                   "By date: dated tasks first, earliest first, then the rest in your order.")
        self.sort_combo.setCurrentIndex(1 if widget.ui.get("panel_sort") == "date" else 0)
        self.sort_combo.currentIndexChanged.connect(self._sort_changed)
        show.addWidget(self.sort_combo)
        b_folders = QPushButton("Folders...")
        b_folders.setToolTip("Rename or delete folders")
        b_folders.clicked.connect(lambda: self.w.open_folders())
        show.addWidget(b_folders)
        root.addLayout(show)

        # -- the shown folder's notes: a summary, and dated progress notes (only when one folder is shown)
        self._notes_path: str | None = None  # the folder whose notes are in the box
        self._notes_timer = QTimer(self)
        self._notes_timer.setSingleShot(True)
        self._notes_timer.setInterval(NOTES_SAVE_MS)
        self._notes_timer.timeout.connect(self.flush_notes)
        self.notes_box = QWidget()
        nb = QVBoxLayout(self.notes_box)
        nb.setContentsMargins(0, 0, 0, 0)
        nb.setSpacing(6)
        self.b_notes = QPushButton()
        self.b_notes.setObjectName("quiet")
        self.b_notes.setStyleSheet("text-align: left;")
        self.b_notes.clicked.connect(self._toggle_notes)
        nb.addWidget(self.b_notes)
        self.notes_body = QWidget()
        nl = QVBoxLayout(self.notes_body)
        nl.setContentsMargins(0, 0, 0, 0)
        nl.setSpacing(6)
        self.notes_text = QPlainTextEdit()
        self.notes_text.setPlaceholderText("Notes for this folder's project: where it stands, what comes next")
        self.notes_text.setFixedHeight(int(self.w.ui.get("notes_h", 90)))
        self.notes_text.textChanged.connect(self._notes_edited)
        nl.addWidget(self.notes_text)
        ngrip = DragHandle(self.notes_text)
        ngrip.moved.connect(lambda h: self.w.ui.__setitem__("notes_h", h))
        nl.addWidget(ngrip)
        self.notes_add = QLineEdit()
        self.notes_add.setPlaceholderText("+ Add a progress note: type it and press Enter (it gets today's date)")
        self.notes_add.returnPressed.connect(self.add_progress)
        nl.addWidget(self.notes_add)
        self.notes_log = QListWidget()
        self.notes_log.setMaximumHeight(110)
        self.notes_log.setToolTip("Progress notes, newest first. Double-click one to edit it.")
        self.notes_log.itemDoubleClicked.connect(lambda *_: self.edit_progress())
        nl.addWidget(self.notes_log)
        lrow = QHBoxLayout()
        lrow.addStretch()
        b_edit = QPushButton("Edit note…")
        b_edit.setObjectName("quiet")
        b_edit.clicked.connect(lambda: self.edit_progress())
        lrow.addWidget(b_edit)
        b_del = QPushButton("Delete note")
        b_del.setObjectName("quiet")
        b_del.clicked.connect(self.delete_progress)
        lrow.addWidget(b_del)
        nl.addLayout(lrow)
        nb.addWidget(self.notes_body)
        self.notes_body.setVisible(bool(widget.ui.get("notes_open", True)))
        self.notes_box.hide()
        root.addWidget(self.notes_box)

        # -- several tasks picked: set their dates or folder together
        self.bulk, bl = card("")
        self.bulk_heading = self.bulk.findChild(QLabel, "heading")
        brow = QHBoxLayout()
        brow.addWidget(QLabel("Deadline"))
        self.bulk_deadline = DateButton(self.w.today, c["disk_remaining"], "Set...")
        self.bulk_deadline.picked.connect(lambda d: self._bulk_date("deadline", d))
        brow.addWidget(self.bulk_deadline)
        brow.addSpacing(10)
        brow.addWidget(QLabel("Aim to finish by"))
        self.bulk_target = DateButton(self.w.today, c["disk_remaining"], "Set...")
        self.bulk_target.picked.connect(lambda d: self._bulk_date("target", d))
        brow.addWidget(self.bulk_target)
        brow.addStretch()
        bl.addLayout(brow)
        brow2 = QHBoxLayout()
        brow2.addWidget(QLabel("Folder"))
        self.bulk_folder = QComboBox()
        self.bulk_folder.currentIndexChanged.connect(self._bulk_folder_changed)
        brow2.addWidget(self.bulk_folder, 1)
        b_unpick = QPushButton("Clear selection")
        b_unpick.setObjectName("quiet")
        b_unpick.clicked.connect(self.fold)
        brow2.addWidget(b_unpick)
        bl.addLayout(brow2)
        self.bulk.hide()
        root.addWidget(self.bulk)

        # -- the one list
        self.list = TaskList()
        self.list.setDefaultDropAction(Qt.MoveAction)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.list.verticalScrollBar().setSingleStep(20)
        self.list.setMinimumHeight(150)
        self.list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.list.setToolTip("Click a task to open it, click it again to fold it. Ctrl+click or Shift+click "
                             "to pick several and give them a date or folder together. "
                             "With Sort set to My order, drag tasks to reorder them.")
        self.list.itemSelectionChanged.connect(self._selection_changed)
        self.list.itemClicked.connect(self._item_clicked)
        self.list.viewport().installEventFilter(self)
        self.list.model().rowsMoved.connect(self._rows_moved)
        root.addWidget(self.list, 1)

        # -- the editor; it lives in the list, in a row right under the selected task
        self.editor, tc = card("")
        self.editor.findChild(QLabel, "heading").hide()
        self.editor.hide()
        self.ed_name = QLineEdit()
        self.ed_name.setPlaceholderText("What is the task?")
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
        mark = c["disk_remaining"]
        self.ed_deadline_time = QLineEdit()
        self.ed_deadline_time.setPlaceholderText("time (optional)")
        self.ed_deadline_time.setFixedWidth(120)
        self.ed_deadline_time.setToolTip("The time of day the deadline falls at, such as 5pm or 17:00. "
                                         "Leave it empty for the end of the day.")
        self.ed_deadline_time.editingFinished.connect(self._deadline_time_done)
        self.ed_deadline = self._date_row(tc, "Deadline", "A hard date the task must be done by", mark,
                                          extra=self.ed_deadline_time)
        self.ed_deadline.changed.connect(lambda *_: self._sync_field_states())
        self.ed_target = self._date_row(tc, "Aim to finish by", "The date you expect to finish. Not a hard limit.",
                                        mark)
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
        arow = QHBoxLayout()
        self.b_up = QPushButton("↑")
        self.b_up.clicked.connect(lambda: self.move_selected(-1))
        arow.addWidget(self.b_up)
        self.b_down = QPushButton("↓")
        self.b_down.clicked.connect(lambda: self.move_selected(1))
        arow.addWidget(self.b_down)
        b_remove = QPushButton("Remove")
        b_remove.setToolTip("Remove this task without finishing it")
        b_remove.clicked.connect(self.remove_selected)
        arow.addWidget(b_remove)
        arow.addStretch()
        b_fold = QPushButton("Fold ▴")
        b_fold.setObjectName("quiet")
        b_fold.clicked.connect(self.fold)
        arow.addWidget(b_fold)
        tc.addLayout(arow)

        # -- completed, folded away until opened
        self.b_completed = QPushButton()
        self.b_completed.setObjectName("quiet")
        self.b_completed.setStyleSheet("text-align: left;")
        self.b_completed.clicked.connect(self._toggle_completed)
        root.addWidget(self.b_completed)
        self.done_box = QWidget()
        dn = QVBoxLayout(self.done_box)
        dn.setContentsMargins(0, 0, 0, 0)
        hint = QLabel("Kept across restarts. Newest first.")
        hint.setObjectName("hint")
        dn.addWidget(hint)
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
        b_clear.setToolTip("Empties this list. The record stays in log.csv.")
        b_clear.clicked.connect(self.clear_done)
        drow2.addWidget(b_clear)
        dn.addLayout(drow2)
        self.done_box.setVisible(bool(widget.ui.get("done_open", False)))
        root.addWidget(self.done_box)

        # The mouse wheel scrolls the window; it changes a box's value only after you click into the box.
        self._wheel_guarded = (self.filter_combo, self.sort_combo, self.bulk_folder, self.ed_folder, self.ed_repeat, self.ed_every)
        for box in self._wheel_guarded:
            box.setFocusPolicy(Qt.StrongFocus)
            box.installEventFilter(self)
        self._fill_folder_combos()
        self.show_status(widget.status_message)

    # -- small builders
    @staticmethod
    def _row_label(text: str) -> QLabel:
        lab = QLabel(text)
        lab.setMinimumWidth(110)
        return lab

    def _date_row(self, lay: QVBoxLayout, text: str, tip: str, mark: str, extra: QWidget | None = None) -> DateButton:
        row = QHBoxLayout()
        lab = self._row_label(text)
        lab.setToolTip(tip)
        row.addWidget(lab)
        button = DateButton(self.w.today, mark)
        button.changed.connect(self._apply_editor)
        row.addWidget(button)
        if extra is not None:
            row.addWidget(extra)
        row.addStretch()
        lay.addLayout(row)
        return button

    def show_status(self, message: str):
        """A warning under the header, such as a list saved by a newer version of the app."""
        self.status_label.setText(message)
        self.status_label.setVisible(bool(message))

    def show_sync(self, text: str):
        self.sync_label.setText(text)

    # -- window events
    def showEvent(self, e):
        self.refresh()
        super().showEvent(e)

    def resizeEvent(self, e):
        if not self.isFullScreen() and not self.isMinimized():
            self.w.ui["panel_w"], self.w.ui["panel_h"] = self.width(), self.height()
        super().resizeEvent(e)

    def hideEvent(self, e):
        self.flush_notes()
        super().hideEvent(e)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_F11:
            self.toggle_full_screen()
        elif e.key() == Qt.Key_Escape and self.isFullScreen():
            self.toggle_full_screen()
        else:
            super().keyPressEvent(e)

    def eventFilter(self, obj, e):
        if e.type() == QEvent.Wheel and obj in self._wheel_guarded and not obj.hasFocus():
            if self.editor.isAncestorOf(obj):
                bar = self.list.verticalScrollBar()
                bar.setValue(bar.value() - e.angleDelta().y() // 2)
            return True
        if e.type() == QEvent.MouseButtonPress and obj is self.list.viewport():
            self._picks_at_press = self._picks
        return super().eventFilter(obj, e)

    def minimize(self):
        """Down to the taskbar. Full screen is kept for when it comes back."""
        self.setWindowState(self.windowState() | Qt.WindowMinimized)

    def restore(self):
        """Back from the taskbar, as it was (full screen too)."""
        self.setWindowState((self.windowState() & ~Qt.WindowMinimized) | Qt.WindowActive)

    def toggle_full_screen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()
        self.w.ui["panel_full"] = self.isFullScreen()
        self.btn_full.setToolTip("Leave full screen (F11 or Esc)" if self.isFullScreen()
                                 else "Full screen (F11). Esc leaves it.")

    def _details_resized(self, h: int):
        self.w.ui["details_h"] = h
        self._fit_editor()

    def _toggle_completed(self):
        self.done_box.setVisible(not self.done_box.isVisible())
        self.w.ui["done_open"] = self.done_box.isVisible()
        self._completed_label()

    def _completed_label(self):
        arrow = "▾" if self.done_box.isVisible() else "▸"
        self.b_completed.setText(f"{arrow} ✅ Completed ({len(self.w.completed)})")

    # -- what the list shows
    def _label(self, t: fc.Task, show_folder: bool = False) -> str:
        if t is self.w.active and self.w.timer.running:
            text = ("⏸ " if self.w.timer.paused else "▶ ") + task_label(t)
        else:
            text = ("⏸ " if t.spent_s else "") + task_label(t)  # paused to start another task
        if t.repeat and t.deadline:
            text = f"🔁 until {short_date(t.deadline)} · {text}"
        elif first_date(t):
            text = f"{due_text(t)} · {text}"
        elif t.repeat:
            text = "🔁 " + text
        if show_folder and t.folder:
            text += f"   📁 {t.folder}"
        return text

    def filter_path(self) -> str | None:
        """The folder the list is filtered to, or None for all tasks."""
        return self.filter_combo.currentData() or None

    def by_date(self) -> bool:
        return self.sort_combo.currentIndex() == 1

    def _shown_tasks(self) -> list:
        """The tasks in the list, in its order, with DIVIDER_ROW between dated and other tasks (By date)."""
        path = self.filter_path()
        if not self.by_date():
            return [t for t in self.w.plan.tasks if fc.in_folder(t, path)]
        dated, ranked = (([t for t in part if fc.in_folder(t, path)])
                         for part in fc.display_sections(self.w.plan.tasks))
        return dated + ([DIVIDER_ROW] if dated and ranked else []) + ranked

    def _task_by_id(self, task_id) -> fc.Task | None:
        return next((t for t in self.w.plan.tasks if t.id == task_id), None)

    @staticmethod
    def _is_task_row(item: QListWidgetItem | None) -> bool:
        return item is not None and item.data(Qt.UserRole) not in (None, EDITOR_ROW, DIVIDER_ROW)

    def _row_of(self, task_id) -> int:
        for i in range(self.list.count()):
            if self.list.item(i).data(Qt.UserRole) == task_id:
                return i
        return -1

    def selected_ids(self) -> list:
        """Ids of the picked tasks, in list order."""
        rows = sorted(self.list.row(i) for i in self.list.selectedItems() if self._is_task_row(i))
        return [self.list.item(r).data(Qt.UserRole) for r in rows]

    def selected_task(self) -> fc.Task | None:
        """The one picked task, or None when none or several are picked."""
        ids = self.selected_ids()
        return self._task_by_id(ids[0]) if len(ids) == 1 else None

    def refresh(self, select=_KEEP):
        """Rebuild the list. `select` is the id of the task to open (None for none); by default the same
        task stays open."""
        keep = self.selected_ids() if select is _KEEP else ([] if select is None else [select])
        focus = QApplication.focusWidget()
        focus = focus if focus is not None and self.editor.isAncestorOf(focus) else None
        self._updating = True
        self._detach_editor()
        self._fill_folder_combos()
        show_folder = self.filter_path() is None
        dim = QColor(self.w.s["colors"]["text_dim"])
        self.list.clear()
        for t in self._shown_tasks():
            if t is DIVIDER_ROW:
                item = QListWidgetItem("──  no date, in your order  ──")
                item.setData(Qt.UserRole, DIVIDER_ROW)
                item.setFlags(Qt.NoItemFlags)
                item.setTextAlignment(Qt.AlignCenter)
                item.setForeground(dim)
                self.list.addItem(item)
                continue
            item = QListWidgetItem(self._label(t, show_folder))
            item.setData(Qt.UserRole, t.id)
            tips = [t.details] if t.details.strip() else []
            if t.deadline:
                hm = fc.fmt_hm(t.deadline_time) if not t.repeat else ""
                tips.append(f"Deadline {short_date(t.deadline)}" + (f" at {hm}" if hm else ""))
            if t.target:
                tips.append(f"Aim to finish by {short_date(t.target)}")
            if t.deadline and not t.repeat:
                item.setForeground(QColor(DUE_RED))
            if self.w.is_asleep(t):
                item.setForeground(dim)
                tips.append(self._rest_text(t))
            if tips:
                item.setToolTip("\n".join(tips))
            self.list.addItem(item)
        self.list.setDragDropMode(QAbstractItemView.NoDragDrop if self.by_date()
                                  else QAbstractItemView.InternalMove)
        rows = [r for r in (self._row_of(i) for i in keep) if r >= 0]
        for r in rows:
            self.list.item(r).setSelected(True)
        if rows:
            self.list.setCurrentItem(self.list.item(rows[0]), QItemSelectionModel.NoUpdate)
        if len(rows) == 1:
            self._attach_editor(rows[0])
        self._show_bulk(len(rows))
        self.done_list.clear()
        for d in reversed(self.w.completed):
            when = d.finished_at[11:16] if len(d.finished_at) >= 16 else d.finished_at
            actual = f"{d.actual_min}m actual" if d.actual_min else "not timed"
            self.done_list.addItem(QListWidgetItem(f"{when}   {d.name}   ({d.estimate_min}m planned, {actual})"))
        self._completed_label()
        self._show_notes()
        self._updating = False
        self.show_status(self.w.status_message)
        self.show_sync(self.w.sync_status)
        self._load_editor()
        if focus is not None and self.editor.isVisible():
            focus.setFocus()  # a sync while typing in the editor keeps the cursor where it was

    # -- the shown folder's notes
    def _notes_label(self):
        arrow = "▾" if self.notes_body.isVisible() else "▸"
        n = sum(1 for x in self.w.progress if x.get("folder") == self._notes_path)
        self.b_notes.setText(f"{arrow} 📝 Notes for {(self._notes_path or '').split('/')[-1]}"
                             + (f" ({n} progress)" if n else ""))

    def _toggle_notes(self):
        self.notes_body.setVisible(not self.notes_body.isVisible())
        self.w.ui["notes_open"] = self.notes_body.isVisible()
        self._notes_label()

    def _show_notes(self):
        """Show the notes of the folder the list is filtered to; hide the box for All tasks. Text being typed
        is kept when a sync refreshes the window."""
        path = self.filter_path()
        self.notes_box.setVisible(path is not None)
        if path is None:
            self._notes_path = None
            return
        typing = path == self._notes_path and (self.notes_text.hasFocus() or self._notes_timer.isActive())
        self._notes_path = path
        text = self.w.folder_notes.get(path, {}).get("text", "")
        if not typing and self.notes_text.toPlainText() != text:
            was, self._updating = self._updating, True
            self.notes_text.setPlainText(text)
            self._updating = was
        keep = self.notes_log.currentItem().data(Qt.UserRole) if self.notes_log.currentItem() else None
        self.notes_log.clear()
        entries = sorted((x for x in self.w.progress if x.get("folder") == path),
                         key=lambda x: (x.get("at", ""), x.get("id", "")), reverse=True)
        for x in entries:
            at = x.get("at", "")
            item = QListWidgetItem(f"{short_date(at[:10])}   {x.get('text', '')}")
            item.setToolTip(f"{at}\n{x.get('text', '')}")
            item.setData(Qt.UserRole, x.get("id"))
            self.notes_log.addItem(item)
            if x.get("id") == keep:
                self.notes_log.setCurrentItem(item)
        self._notes_label()

    def _notes_edited(self):
        if not self._updating and self._notes_path is not None:
            self._notes_timer.start()

    def flush_notes(self):
        """Save the notes being typed (called a moment after the last key press, and before the folder
        changes or the window hides)."""
        self._notes_timer.stop()
        path = self._notes_path
        if path is None:
            return
        text = self.notes_text.toPlainText()
        have = self.w.folder_notes.get(path)
        if (have or {}).get("text", "") == text:
            return
        if have is None:
            self.w.folder_notes[path] = {"text": text, "updated_at": fc.EPOCH}
        else:
            have["text"] = text
        self.w.changed()

    def add_progress(self):
        text = self.notes_add.text().strip()
        if not text or self._notes_path is None:
            return
        self.w.progress.append({"id": fc.new_id(), "folder": self._notes_path,
                                "at": datetime.now().strftime("%Y-%m-%d %H:%M"), "text": text,
                                "updated_at": fc.EPOCH})
        self.notes_add.clear()
        self._show_notes()
        self.w.changed()

    def _picked_progress(self) -> dict | None:
        item = self.notes_log.currentItem()
        if item is None:
            return None
        return next((x for x in self.w.progress if x.get("id") == item.data(Qt.UserRole)), None)

    def edit_progress(self, new_text: str | None = None):
        """Change the picked progress note. `new_text` skips the dialog (tests)."""
        x = self._picked_progress()
        if x is None:
            return
        if new_text is None:
            new_text, ok = QInputDialog.getText(self, "Edit progress note", f"Note from {x.get('at', '')}:",
                                                text=x.get("text", ""))
            if not ok:
                return
        new_text = new_text.strip()
        if new_text and new_text != x.get("text"):
            x["text"] = new_text
            self._show_notes()
            self.w.changed()

    def delete_progress(self):
        x = self._picked_progress()
        if x is None:
            return
        self.w.progress.remove(x)
        self._show_notes()
        self.w.changed()

    def _rest_text(self, t: fc.Task) -> str:
        try:
            nxt = fc.next_active_day(t, self.w.today())
        except (ValueError, TypeError, KeyError):
            nxt = None  # a rule saved oddly by another device
        return f"Resting until {short_date(nxt.isoformat())}" if nxt else "Resting, since no days are chosen"

    # -- the editor row: unfold under a task, fold away
    def _attach_editor(self, row: int):
        item = QListWidgetItem()
        item.setData(Qt.UserRole, EDITOR_ROW)
        item.setFlags(Qt.NoItemFlags)
        self.list.insertItem(row + 1, item)
        holder = QWidget()
        holder.setAttribute(Qt.WA_NoMousePropagation)  # a click beside the fields must not change the selection
        lay = QVBoxLayout(holder)
        lay.setContentsMargins(16, 2, 4, 8)
        lay.addWidget(self.editor)
        self.editor.show()
        self.list.setItemWidget(item, holder)
        self._editor_item = item
        self._fit_editor()

    def _detach_editor(self):
        """Take the editor out of the list. It is moved to this window first, because removing its row
        deletes the row's widget."""
        if self._editor_item is None:
            return
        self.editor.hide()
        self.editor.setParent(self)
        row = self.list.row(self._editor_item)
        if row >= 0:
            self.list.takeItem(row)
        self._editor_item = None

    def _fit_editor(self):
        if self._editor_item is None:
            return
        holder = self.list.itemWidget(self._editor_item)
        if holder is not None:
            holder.layout().activate()
            self._editor_item.setSizeHint(QSize(0, holder.sizeHint().height()))

    def _selection_changed(self):
        """Picking one task opens its editor under it and folds the one that was open. Picking several shows
        the bar that sets their dates or folder together."""
        if self._updating:
            return
        self._picks += 1
        ids = self.selected_ids()
        self._updating = True
        self._detach_editor()
        if len(ids) == 1:
            self._attach_editor(self._row_of(ids[0]))
        self._updating = False
        self._show_bulk(len(ids))
        self._load_editor()
        if self._editor_item is not None:
            QTimer.singleShot(0, self._show_editor_row)

    def _show_editor_row(self):
        if self._editor_item is not None:
            self.list.scrollToItem(self._editor_item)
            current = self.list.currentItem()
            if current is not None:
                self.list.scrollToItem(current)  # the task's own row stays in view too

    def _item_clicked(self, item: QListWidgetItem):
        """A click on the open task (one that did not just open it) folds it."""
        if self._picks == self._picks_at_press and self.selected_ids() == [item.data(Qt.UserRole)]:
            self.fold()

    def fold(self):
        self.list.clearSelection()

    # -- several tasks at once
    def _show_bulk(self, n: int):
        self.bulk.setVisible(n > 1)
        if n > 1:
            self.bulk_heading.setText(f"{n} tasks picked. Set their dates or folder together.")

    def _picked_tasks(self) -> list:
        return [t for t in (self._task_by_id(i) for i in self.selected_ids()) if t is not None]

    def _bulk_date(self, field: str, d: date | None):
        """Give every picked task this deadline or aim-to-finish date (None clears it). On a repeating task a
        deadline is the last day it repeats; it has no aim-to-finish date, so that one leaves it as it is."""
        for button in (self.bulk_deadline, self.bulk_target):
            button.set_value(None)  # the buttons always read "Set..."
        tasks = [t for t in self._picked_tasks() if field == "deadline" or not t.repeat]
        if not tasks:
            return
        for t in tasks:
            setattr(t, field, d.isoformat() if d else None)
            if field == "deadline" and d is None:
                t.deadline_time = None  # no deadline, so no time of day for it
        self.refresh()
        self.w.changed()

    def _bulk_folder_changed(self, index: int):
        if self._updating or index <= 0:
            return
        data = self.bulk_folder.itemData(index)
        folder = data or None
        if data == NEW_FOLDER:
            text, ok = QInputDialog.getText(self, "New folder", "Name of the new folder:")
            folder = clean_folder(text) if ok else None
            if folder:
                self.w.add_folder(folder)
        self._updating = True
        self.bulk_folder.setCurrentIndex(0)
        self._updating = False
        if data == NEW_FOLDER and not folder:
            return
        for t in self._picked_tasks():
            t.folder = folder
        self.refresh()
        self.w.changed()

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
        self.bulk_folder.clear()
        self.bulk_folder.addItem("Move to folder...", None)
        self.bulk_folder.addItem("No folder", "")
        self.filter_combo.addItem("All tasks", "")
        self.ed_folder.clear()
        self.ed_folder.addItem("No folder", "")
        for p, depth in tree:
            label = "    " * depth + p.split("/")[-1]
            self.filter_combo.addItem(label, p)
            self.ed_folder.addItem(label, p)
            self.bulk_folder.addItem(label, p)
        self.bulk_folder.addItem("New folder...", NEW_FOLDER)
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
        if not self._updating:
            self.flush_notes()
            self.refresh()

    def _sort_changed(self, *_):
        self.w.ui["panel_sort"] = "date" if self.by_date() else "order"
        self.refresh()

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

    # -- the editor's fields
    def _show_fields(self, deadline, target, folder, repeat, deadline_time=None):
        """Put a task's dates, folder and repeat rule into the editor (call with _updating set)."""
        self.ed_deadline.set_value(parse_date(deadline))  # text that is not a date counts as no date
        self.ed_deadline_time.setText(fc.fmt_hm(deadline_time))
        self._time_error(False)
        self.ed_target.set_value(parse_date(target))
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
        """A repeating task has no aim-to-finish date; its deadline is the last day it repeats. Show the day
        boxes or the day count only for their rule.
        Up and down work only in My order, where you set the order."""
        kind = REPEAT_KINDS[self.ed_repeat.currentIndex()]
        self.ed_deadline.setToolTip("Click to pick a date" if kind is None
                                    else "The last day it repeats. After that it leaves the list.")
        self.ed_target.setEnabled(kind is None)
        self.ed_target.setToolTip("Click to pick a date" if kind is None
                                  else "A repeating task has no aim-to-finish date")
        timed = kind is None and self.ed_deadline.value() is not None
        self.ed_deadline_time.setEnabled(timed)  # a repeating task's deadline is only the last day it repeats
        if not timed and self.ed_deadline_time.text():
            self.ed_deadline_time.clear()
        self.days_row.setVisible(kind == "days")
        self.ed_every.setVisible(kind == "every")
        for b, tip in ((self.b_up, "Move up"), (self.b_down, "Move down")):
            b.setEnabled(not self.by_date())
            b.setToolTip(tip if not self.by_date() else "Set Sort to My order to move tasks")
        self._fit_editor()

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
        deadline, target = self.ed_deadline.value(), self.ed_target.value()
        try:
            hm = parse_time_text(self.ed_deadline_time.text())
        except ValueError:
            hm = self._shown_time  # not a time yet: keep the one saved
        return {"deadline": deadline.isoformat() if deadline else None,
                "target": target.isoformat() if target and kind is None and repeat is None else None,
                "folder": self._folder_choice(), "repeat": repeat,
                "deadline_time": hm if deadline and kind is None and repeat is None else None}

    def _time_error(self, bad: bool):
        self.ed_deadline_time.setStyleSheet(f"border: 1px solid {DUE_RED};" if bad else "")
        if bad:
            self.ed_deadline_time.setToolTip("Not a time. Write it like 5pm, 5:30pm or 17:00.")

    def _deadline_time_done(self):
        """The time box lost focus or Enter was pressed: tidy what was typed ("17" -> "5:00pm") and save it."""
        if self._updating:
            return
        try:
            hm = parse_time_text(self.ed_deadline_time.text())
        except ValueError:
            self._time_error(True)
            return
        self._time_error(False)
        self.ed_deadline_time.setToolTip("The time of day the deadline falls at, such as 5pm or 17:00. "
                                         "Leave it empty for the end of the day.")
        self._updating = True
        self.ed_deadline_time.setText(fc.fmt_hm(hm))
        self._updating = False
        self._apply_editor()

    def _repeat_changed(self, index: int):
        if self._updating:
            return
        kind = REPEAT_KINDS[index]
        self._kept_repeat = None  # a choice made here replaces a rule this app did not know
        self._updating = True
        if kind is not None:
            self.ed_target.set_value(None)  # a repeating task keeps its deadline, the last day it repeats
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
            return
        self._updating = True
        if self.ed_name.text() != t.name:  # setting the same text would move the cursor
            self.ed_name.setText(t.name)
        self.ed_time.set_value(t.estimate_min)
        if self.ed_details.toPlainText() != t.details:
            self.ed_details.setPlainText(t.details)
        self._shown_time = t.deadline_time
        self._show_fields(t.deadline, t.target, t.folder, t.repeat, t.deadline_time)
        self._updating = False

    def _apply_editor(self, *_):
        if self._updating:
            return
        t = self.selected_task()
        if t is None:
            return
        v = self._editor_values()
        before = (t.deadline, t.target, t.folder, t.repeat, t.deadline_time)
        t.name = self.ed_name.text().strip() or t.name
        t.estimate_min = self.ed_time.value()
        t.details = self.ed_details.toPlainText()
        t.deadline, t.target, t.folder, t.repeat = v["deadline"], v["target"], v["folder"], v["repeat"]
        t.deadline_time = self._shown_time = v["deadline_time"]
        if t is self.w.active and self.w.timer.running:
            self.w.timer.task = t.name
            self.w.timer.estimate_min = t.estimate_min
        if (t.deadline, t.target, t.folder, t.repeat, t.deadline_time) != before:
            self.refresh()  # the task may move, or leave the folder shown
        else:
            row = self._row_of(t.id)
            if row >= 0:
                item = self.list.item(row)
                self._updating = True
                item.setText(self._label(t, show_folder=self.filter_path() is None))
                item.setToolTip(t.details)
                self._updating = False
        self.w.changed()

    # -- add / reorder / remove
    def add_task(self):
        """Add the task typed in the top box to the front of the list, in the folder shown, and open it."""
        name = self.ed_add.text().strip()
        if not name:
            self.ed_add.setFocus()
            return
        t = fc.Task(name, int(self.w.s["default_block_minutes"]), folder=self.filter_path())
        self.w.plan.tasks.insert(0, t)
        self.ed_add.clear()
        self.refresh(t.id)
        QTimer.singleShot(0, self._show_editor_row)
        self.ed_add.setFocus()  # ready for the next one
        self.w.changed()

    def _shown_ids(self) -> list:
        return [self.list.item(i).data(Qt.UserRole) for i in range(self.list.count())
                if self._is_task_row(self.list.item(i))]

    def _apply_order(self, shown_ids: list) -> None:
        """Put the shown tasks in this order, in the places they held. Tasks hidden by the folder filter keep
        their places."""
        tasks = self.w.plan.tasks
        shown = set(shown_ids)
        if sorted(t.id for t in tasks if t.id in shown) != sorted(shown_ids):
            return  # the list no longer matches the tasks (changed meanwhile); leave the order alone
        by_id = {t.id: t for t in tasks}
        new = iter(shown_ids)
        self.w.plan.tasks = [by_id[next(new)] if t.id in shown else t for t in tasks]

    def _rows_moved(self, *_):
        if self._updating or self.by_date():
            return
        self._apply_order(self._shown_ids())
        self.refresh()
        self.w.changed()

    def move_selected(self, delta: int):
        t = self.selected_task()
        if t is None or self.by_date():
            return
        ids = self._shown_ids()
        row = ids.index(t.id)
        dst = row + delta
        if not 0 <= dst < len(ids):
            return
        ids.insert(dst, ids.pop(row))
        self._apply_order(ids)
        self.refresh()
        self.w.changed()

    def remove_selected(self):
        t = self.selected_task()
        if t is None:
            return
        self.w.remove_task(t)
        self.refresh(None)

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
        self.refresh(t.id if t is not None and any(x.id == t.id for x in self.w.plan.tasks) else None)

    # -- completed
    def _done_index(self) -> int:
        row = self.done_list.currentRow()
        if row < 0:
            return -1
        return len(self.w.completed) - 1 - row  # the list shows newest first

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

class DayPlanWindow(QWidget):
    """The day plan made by the planner (on GitHub every evening, or with Re-plan now), in its own window."""

    def __init__(self, widget: "FocusWidget"):
        super().__init__(None, Qt.Tool | Qt.WindowStaysOnTopHint)
        self.w = widget
        self.setWindowTitle("Day plan")
        self.setWindowIcon(QIcon(str(resource_dir() / "assets" / "icon.ico")))
        self.setStyleSheet(panel_stylesheet(widget.s["colors"]))
        self.resize(int(widget.ui.get("plan_w", 420)), int(widget.ui.get("plan_h", 420)))
        if "plan_x" in widget.ui and "plan_y" in widget.ui:
            at = QRect(int(widget.ui["plan_x"]), int(widget.ui["plan_y"]), self.width(), self.height())
            if any(sc.availableGeometry().intersects(at) for sc in QApplication.screens()):
                self.move(at.topLeft())  # only if that place is still on a screen

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 18)
        root.setSpacing(12)
        title = QLabel("🗓 Day plan")
        title.setObjectName("title")
        root.addWidget(title)

        plan_card, pc = card("Time blocks")
        self.plan_heading = QLabel()
        self.plan_heading.setObjectName("hint")
        self.plan_heading.setWordWrap(True)
        pc.addWidget(self.plan_heading)
        self.plan_warn = QLabel()
        self.plan_warn.setWordWrap(True)
        self.plan_warn.setStyleSheet(f"color: {AMBER};")
        self.plan_warn.hide()
        pc.addWidget(self.plan_warn)
        self.plan_list = QListWidget()
        self.plan_list.setMinimumHeight(90)
        self.plan_list.setToolTip("Double-click a block to start its task")
        self.plan_list.itemDoubleClicked.connect(self._start_plan_item)
        pc.addWidget(self.plan_list, 1)
        prow = QHBoxLayout()
        prow.addStretch()
        self.b_replan = QPushButton("Re-plan now")
        self.b_replan.setToolTip("Plan the rest of today from now. After 7 pm it plans tomorrow.")
        self.b_replan.clicked.connect(lambda: self.w.replan_now())
        prow.addWidget(self.b_replan)
        pc.addLayout(prow)
        root.addWidget(plan_card, 1)

    def refresh_plan(self):
        """Show today's plan, or tomorrow's once it is made."""
        plan = self.w.shown_plan()
        self.plan_list.clear()
        if plan is None:
            self.plan_heading.setText("No plan yet. Re-plan now makes one for the rest of today.")
            self.plan_warn.hide()
            return
        made = made_time(plan)
        self.plan_heading.setText(f"Plan for {short_date(plan.get('date'))}" + (f", made {made}" if made else ""))
        warns = [f"⚠ {x.get('name', '')} · {x.get('message', '')}" for x in plan.get("warnings") or []
                 if isinstance(x, dict)]
        self.plan_warn.setText("\n".join(warns))
        self.plan_warn.setVisible(bool(warns))
        names = {t.id: t.name for t in self.w.plan.tasks}
        for b in plan.get("blocks") or []:
            if not isinstance(b, dict):
                continue
            name = names.get(b.get("task_id"), b.get("name", ""))
            mark = ""
            if b.get("date") and b.get("date_kind") == "deadline":
                hm = fc.fmt_hm(b.get("time"))
                mark = f"  · 📅 Due {short_date(b['date'])}" + (f" {hm}" if hm else "")
            elif b.get("date") and b.get("date_kind") == "target":
                mark = f"  · 🎯 Aim {short_date(b['date'])}"
            item = QListWidgetItem(f"{b.get('start', '')}-{b.get('end', '')}  {name}  ({b.get('minutes', 0)}m){mark}")
            item.setData(Qt.UserRole, b.get("task_id"))
            if b.get("task_id") not in names:
                item.setForeground(QColor(self.w.s["colors"]["text_dim"]))  # finished or removed since
            self.plan_list.addItem(item)

    def _start_plan_item(self, item: QListWidgetItem):
        t = next((t for t in self.w.plan.tasks if t.id == item.data(Qt.UserRole)), None)
        if t is not None:
            self.w.start_task(t)

    def showEvent(self, e):
        self.refresh_plan()
        super().showEvent(e)

    def resizeEvent(self, e):
        self.w.ui["plan_w"], self.w.ui["plan_h"] = self.width(), self.height()
        super().resizeEvent(e)

    def moveEvent(self, e):
        if self.isVisible():
            self.w.ui["plan_x"], self.w.ui["plan_y"] = self.x(), self.y()
        super().moveEvent(e)


class FocusWidget(QWidget):
    # Sync status and warnings for the tray, as (tooltip text, True to also pop up a notice)
    status_changed = Signal(str, bool)
    AUTO_SYNC = True  # GitHub mode syncs on a worker thread; tests turn this off and call sync_now()

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
        gh = local.get("github")
        self._gh_cfg: dict | None = ({k: str(gh.get(k) or "") for k in ("owner", "repo", "branch")}
                                    if isinstance(gh, dict) and gh.get("owner") and gh.get("repo") else None)
        self._gh_imported = bool(local.get("github_imported"))  # the Box list was merged into GitHub once
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
        self._box_blocked = False  # the Box file is from a newer app, so Box mode never writes it
        self._gh_blocked = False  # the GitHub list is from a newer app, so GitHub mode stops syncing
        self._auth_blocked = False  # GitHub rejected the token; no automatic syncs until a new one is saved
        self.status_message = ""  # a warning for the panel header and the tray tooltip
        self.mode = "box"  # "github" when local.json names a repo and a token is saved
        self.sync_status = ""  # GitHub mode, such as "Synced 18:42"
        self._engine: gh_store.SyncEngine | None = None
        self._jobs: queue.Queue = queue.Queue()  # work for the sync thread
        self._results: queue.Queue = queue.Queue()  # its answers, applied by on_tick on the Qt thread
        self._job_lock = threading.Lock()  # one GitHub job at a time (they share the cache file)
        self._cancel = threading.Event()  # set when GitHub mode ends; its queued jobs then do nothing
        self._session = [0]  # number of the current GitHub session, shared with the worker (see run_job)
        self.pending_path = self.local_path.parent / "github-pending.json"  # edits not yet pushed
        self._worker: threading.Thread | None = None
        self._job_pending = False  # a sync job is queued or running
        self._sync_due: float | None = None  # time.monotonic() when a change should be pushed
        self._last_sync_at = float("-inf")
        self._sync_not_before = 0.0  # after a rate limit, wait until this time.monotonic()
        self._mirror_text: str | None = None  # what GitHub mode last wrote to the Box copy
        self.day_plan: dict | None = None
        self.plan_path = shared / "day-plan.json"
        self._plan_sig: tuple[int, int] | None = None
        self._plan_text: str | None = None
        self._read_day_plan()
        self._day = self.today()
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
        self.plan_window = DayPlanWindow(self)
        if self._gh_cfg is not None:
            token = load_token()
            if token:
                self._start_github(token)

        self.tick_timer = QTimer(self)
        self.tick_timer.timeout.connect(self.on_tick)
        self.tick_timer.start(CHECK_EVERY_MS)
        self.save_timer = QTimer(self)
        self.save_timer.timeout.connect(self.save)
        self.save_timer.start(AUTOSAVE_EVERY_MS)
        self.sync_timer = QTimer(self)
        self.sync_timer.timeout.connect(self.check_shared)
        self.sync_timer.start(SYNC_CHECK_MS)
        self.drop_ended_repeats()
        self.save()  # writes the shared file only if reading it needed an upgrade (or the GitHub import)

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
        try:
            return bool(t.repeat) and not fc.is_active(t, self.today())
        except (ValueError, TypeError, KeyError):
            return False  # a rule saved oddly by another device, so keep the task usable

    def display_order(self) -> list[fc.Task]:
        """Tasks in the order the panel lists them, with dated tasks by date first and then the rest in your order."""
        dated, ranked = fc.display_sections(self.plan.tasks)
        return dated + ranked

    def current_task(self) -> fc.Task | None:
        """The task on the timer. When idle, 'Up next' is the task of the day plan's current or next block,
        or else the first task that can be started."""
        if self.timer.running and self.active is not None:
            return self.active
        live = [t for t in self.display_order() if not self.is_asleep(t)]
        try:
            block = fc.next_block(self.day_plan, datetime.now(), {t.id for t in live})
        except (KeyError, TypeError, ValueError, AttributeError):
            block = None  # a plan file this app cannot read
        if block is not None:
            return next(t for t in live if t.id == block.get("task_id"))
        return live[0] if live else None

    def changed(self):
        """Call after any change to the tasks or timer: write it to the shared folder and repaint."""
        self._shared_dirty = True
        self.save()
        self.update()
        self._refresh_toggle_labels()
        self._refresh_plan_window()

    def _set_status(self, message: str):
        """A warning that stays, such as a list saved by a newer app (red, under the panel header)."""
        self.status_message = message
        self._emit_status(alert=bool(message))
        if getattr(self, "panel", None) is not None:
            self.panel.show_status(message)

    def _set_sync_status(self, text: str, alert: bool = False):
        """GitHub mode's state ("Synced 18:42", "Offline, will retry", ...) in the panel header and tray."""
        if text == self.sync_status:
            return
        self.sync_status = text
        self._emit_status(alert)
        if getattr(self, "panel", None) is not None:
            self.panel.show_sync(text)

    def _emit_status(self, alert: bool):
        self.status_changed.emit("\n".join(x for x in (self.sync_status, self.status_message) if x), alert)

    @property
    def _blocked(self) -> bool:
        """True when the list of the current mode was saved by a newer app, so nothing may be written."""
        return self._gh_blocked if self.mode == "github" else self._box_blocked

    def _block_newer(self, github: bool = False):
        """The Box file (or with `github`, the GitHub list) was saved by a newer app. Never write it again
        until restart, and say so while that list is the one in use."""
        if github:
            self._gh_blocked = True
        else:
            self._box_blocked = True
        if self._blocked:
            self._set_status(NEWER_FILE_MESSAGE)

    # -- folders
    def state_obj(self) -> fc.State:
        """The live list as one State (the same task objects, not copies)."""
        idx = next((i for i, t in enumerate(self.plan.tasks) if t is self.active), 0)
        return fc.State(self.plan, idx, self.timer, None, self.completed, self.deleted, self.order_updated_at,
                        self.planner, self.folders, self.folders_updated_at, self.folder_notes, self.progress)

    def add_folder(self, path: str):
        """Add a folder (and its parent folders) to the known folders."""
        missing = [p for p in folder_with_parents(path) if p not in self.folders]
        if missing:
            self.folders = self.folders + missing
            self.changed()

    def rename_folder(self, old: str, new: str):
        self.panel.flush_notes()  # notes still being typed belong to the old name
        st = self.state_obj()
        fc.rename_folder(st, old, new)
        self.folders = st.folders
        self.changed()
        if self.panel.isVisible():
            self.panel.refresh()

    def delete_folder(self, path: str):
        self.panel.flush_notes()  # notes still being typed belong to the old name
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
        self.folder_notes, self.progress = st.folder_notes, st.progress
        self.timer.checkpoints = tuple(sorted(self.s["checkpoints"]))
        self.active: fc.Task | None = None
        if self.timer.running:
            self.active = fc.find_active(self.plan.tasks, st.current_index, self.timer.task, self.timer.task_id)
            if self.active is None:
                self.timer.skip()
        self.timer.tick(datetime.now())  # catch up the clock; checkpoints already passed do not pulse or chime

    def _snapshot(self) -> dict:
        """The live list as a version 4 document (a copy). It is what the next save compares against."""
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
            old = int(raw.get("version", 1)) < 3  # before change stamps
        except fc.NewerVersionError:
            self._block_newer()
            return None
        except (OSError, ValueError, AttributeError, KeyError, TypeError):
            return None
        self._shared_sig, self._shared_text = sig, text
        return st, old

    def check_shared(self):
        """Every few seconds in Box mode, show changes the other computer saved, or retry a write Box blocked.
        GitHub mode never reads the Box copy; it syncs from on_tick instead."""
        if self.mode == "github":
            return
        if file_changed(self.plan_path, self._plan_sig, self._plan_text):
            self._read_day_plan()
        if self._blocked:
            return
        if self._shared_dirty:
            self._save_shared()
            return
        if QApplication.activeModalWidget() or QApplication.activePopupWidget():
            return  # a dialog or menu is holding tasks from the current list; look again once it closes
        if not file_changed(self.state_path, self._shared_sig, self._shared_text):
            return
        before = self._shared_text
        got = self._read_shared()
        if got is not None and self._shared_text != before:
            self._apply_state(*got)

    def _apply_state(self, st: fc.State, old_file: bool = False, baseline: dict | None = None):
        """Show a list another computer or the phone saved. `baseline` is that list as a doc; the next save
        compares against it, so what came from elsewhere is never stamped again as a local edit."""
        was = (self.timer.running, self.timer.task_id, self.timer.task)
        self._adopt(st)
        self._last_doc = copy.deepcopy(baseline) if baseline is not None else self._snapshot()
        if old_file:
            self._mark_imported()
        same_task = was == (self.timer.running, self.timer.task_id, self.timer.task)
        # Still over time on the same task: keep this computer's answer. A different task over time: ask.
        self._set_asking(self.timer.running and self.timer.overtime_s > 0 and (self._asking or not same_task))
        self.update()
        self._refresh_toggle_labels()
        if self.panel.isVisible():
            self.panel.refresh()
        self._refresh_plan_window()

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
                ("📅", "Day plan (P)", self.toggle_day_plan),
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
        d = {"geometry": self.geometry_dict(), "merged_into_shared": self._merged}
        if self._gh_cfg is not None:
            d["github"] = self._gh_cfg  # owner, repo, branch; the token is in the Windows Credential Manager
        if self._gh_imported:
            d["github_imported"] = True
        try:
            _write_json(self.local_path, d)
        except OSError:
            pass  # the file is locked for longer than the retries; the next save writes it

    # -- building and writing the shared document (one place each)
    def build_doc(self) -> dict:
        """The whole live list as a state.json version 4 document, not stamped yet."""
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
        for path, n in doc["folder_notes"].items():
            self.folder_notes.setdefault(path, {"text": n["text"]})["updated_at"] = n["updated_at"]
        notes = {x["id"]: x["updated_at"] for x in doc["progress"]}
        for x in self.progress:
            x["updated_at"] = notes.get(x["id"], x.get("updated_at", fc.EPOCH))
        self._last_doc = doc
        self._stamp_import = False

    def _save_shared(self):
        """Write the change (stamped right before writing). Box mode writes the shared file, but never an
        unchanged list and never a file saved by a newer app. If another computer saved the file since this
        one last read or wrote it, the two lists are merged first, so neither loses its edits.
        GitHub mode instead commits the change locally and pushes it a moment later."""
        if self._blocked:
            return
        if self.mode == "github":
            self._commit_github()
            return
        doc = self._stamped_doc()
        if self.state_path.exists() and file_changed(self.state_path, self._shared_sig, self._shared_text):
            try:
                disk = fc.migrate_doc(json.loads(self.state_path.read_text(encoding="utf-8")))
            except fc.NewerVersionError:
                self._block_newer()
                return
            except (OSError, ValueError, AttributeError, KeyError, TypeError):
                disk = None  # half synced; this computer's list is the complete one
            if disk is not None:
                merged = fc.merge_docs(doc, disk)
                if not same_doc(merged, doc):
                    self._apply_state(fc.doc_to_state(merged), baseline=merged)
                doc = merged
        elif doc == self._last_doc and self._shared_text is not None:
            self._shared_dirty = False
            return
        try:
            text = fc.save_state_obj(self.state_path, fc.doc_to_state(doc))
        except OSError:
            return  # Box can hold the file for a moment while it uploads; check_shared tries again
        self._take_stamps(doc)
        self._shared_sig, self._shared_text = _file_sig(self.state_path), text
        self._shared_dirty = False

    # -- GitHub mode
    def _commit_github(self):
        """Take a local change into the baseline, keep it in github-pending.json at once, copy it to Box,
        and push it a moment later."""
        doc = self._stamped_doc()
        if self._stamp_import or not same_doc(doc, self._last_doc):
            self._take_stamps(doc)
            self._write_pending()
            self._write_mirror(doc)
            self._sync_due = time.monotonic() + SYNC_DEBOUNCE_S
        self._shared_dirty = False

    def _write_pending(self):
        """Keep this computer's whole list in %APPDATA%\\FocusTool\\github-pending.json. It is a small local
        file written on the Qt thread, so an edit survives a crash or a quit even while a sync is stuck.
        A start in GitHub mode merges it back in."""
        try:
            _write_atomic(self.pending_path, json.dumps(self._last_doc, ensure_ascii=False))
        except OSError:
            pass

    def _write_mirror(self, doc: dict):
        """GitHub mode keeps data\\state.json in Box as a backup copy. It is written, never read, and never
        written over a file saved by a newer app."""
        if self._box_blocked or self._box_file_is_newer():
            return
        st = fc.doc_to_state(doc)
        text = json.dumps(fc.state_to_doc(st), indent=2, ensure_ascii=False)
        if text == self._mirror_text:
            return
        try:
            fc.save_state_obj(self.state_path, st)
        except OSError:
            return  # Box is holding the file; the next change writes it
        self._mirror_text = text

    def _box_file_is_newer(self) -> bool:
        """True if the Box state.json was saved by a newer app (checked before each mirror write, since
        such a file can appear at any time). Only its version number is looked at."""
        try:
            with self.state_path.open("r", encoding="utf-8") as f:
                head = f.read()  # small file; the key may come anywhere if another app wrote it
        except OSError:
            return False
        m = re.search(r'"version"\s*:\s*(\d+)', head)
        if m and int(m.group(1)) > fc.STATE_VERSION:
            self._box_blocked = True  # stays until restart, as for a newer file found in Box mode
            return True
        return False

    def _saved_github_docs(self, engine) -> list:
        """The lists this computer kept for GitHub mode (pending edits, and the sync cache's copies)."""
        cache = read_sync_cache(engine.cache_path)
        found = []
        for d in (read_sync_cache(self.pending_path), cache.get("local_doc")):
            if isinstance(d, dict) and d:
                found.append(d)
        if not found and isinstance(cache.get("last_remote_doc"), dict):
            found.append(cache["last_remote_doc"])
        return found

    def _start_github(self, token: str, running: bool = False):
        """Switch to GitHub mode. The starting list merges the edits kept for GitHub mode (pending file and
        sync cache). When the app is already running (`running`), the list on screen is merged in too, so
        nothing typed before connecting is lost. On the first GitHub start of this computer, the Box list
        is merged in once."""
        try:
            engine = make_sync_engine({**self._gh_cfg, "token": token})
        except Exception as ex:
            self._set_sync_status(f"Sync error: {_short(ex)}")
            return
        if self._shared_dirty:
            self._save_shared()  # finish the last write in the mode we are leaving
        self._end_session(delete_files=False)
        self._session[0] += 1  # clean-up jobs of earlier sessions no longer touch the files
        self._engine, self.mode = engine, "github"
        self._jobs, self._results, self._cancel = queue.Queue(), queue.Queue(), threading.Event()
        self._job_pending, self._sync_due, self._last_sync_at = False, None, float("-inf")
        self._auth_blocked, self._gh_blocked = False, False
        start = None
        for d in self._saved_github_docs(engine):
            try:
                start = fc.merge_docs(start, d) if start is not None else fc.migrate_doc(d)
            except fc.NewerVersionError:
                self._block_newer(github=True)
                return
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
        if running and start is not None:
            start = fc.merge_docs(start, self._last_doc)
        if not self._gh_imported:
            box = self.build_doc()  # the list read from Box
            start = fc.merge_docs(start, box) if start is not None else fc.migrate_doc(box)
            self._gh_imported = True
            self._stamp_import = True  # fresh order, folder and timer stamps on the merged list
        if start is not None and not same_doc(start, self._last_doc):
            self._apply_state(fc.doc_to_state(start), baseline=start)
        if self.status_message == NEWER_FILE_MESSAGE:
            self._set_status("")  # a newer Box file only matters in Box mode
        self._save_local()
        self._shared_dirty = True
        self._save_shared()  # commits the import stamps, if any
        self._write_pending()
        self._set_sync_status("Connecting to GitHub...")

    def connect_github(self, cfg: dict, token: str):
        """Use GitHub from now on (the GitHub sync dialog's Save, also after a rejected token)."""
        cfg = {k: str(cfg.get(k) or "") for k in ("owner", "repo", "branch")}
        cfg["branch"] = cfg["branch"] or "main"
        if self.mode == "github" and cfg != self._gh_cfg:
            self._end_session(delete_files=True)  # the saved copies of another repo do not apply
        self._gh_cfg = cfg
        self._save_local()
        self._start_github(token, running=True)

    def disconnect_github(self):
        """Go back to Box mode and forget the token and the repo. The list stays as it is and is saved to Box.
        Queued GitHub work is cancelled, a running sync is left to finish on its own with its result ignored,
        and the saved GitHub copies are deleted (again after that sync, so it cannot bring them back)."""
        try:
            gh_store.delete_token()
        except Exception:
            pass
        self._end_session(delete_files=True)
        self._engine, self.mode, self._gh_cfg, self._gh_imported = None, "box", None, False
        self._auth_blocked = False
        self._save_local()
        self._set_sync_status("")
        if self._box_blocked:
            self._set_status(NEWER_FILE_MESSAGE)
        self._shared_sig, self._shared_text = _file_sig(self.state_path), self._mirror_text
        self._mirror_text = None
        if self.panel.isVisible():
            self.panel.refresh()

    def _end_session(self, delete_files: bool):
        """Stop GitHub work without waiting. Cancels queued jobs; the worker finishes a running one,
        then (with `delete_files`) deletes the cache and pending files, then stops."""
        self._cancel.set()
        while True:
            try:
                self._jobs.get_nowait()
            except queue.Empty:
                break
        paths = [self.pending_path] + ([self._engine.cache_path] if self._engine is not None else [])
        if delete_files:
            forget = {"kind": "forget", "paths": paths, "session": self._session[0], "latest": self._session}
            if self._worker is not None:
                self._jobs.put(forget)
            run_job(forget)  # now too, in case no job is running
        self.stop_sync()
        # Detach: the old worker keeps its own queue (with the jobs just queued for it), so ending a
        # session a second time can never take them away again.
        self._jobs, self._results = queue.Queue(), queue.Queue()
        self._job_pending = False

    def stop_sync(self):
        """Stop the sync thread (it finishes the job it is doing first)."""
        if self._worker is not None:
            self._jobs.put(None)
            self._worker = None

    def shutdown(self):
        """On quit, save, keep unpushed edits in github-pending.json, and stop the sync thread. Never waits
        for a sync that is still running."""
        self.save()
        if self.mode == "github":
            self._write_pending()
        self.stop_sync()

    def _ensure_worker(self):
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._work, args=(self._jobs, self._results, self._job_lock),
                                            daemon=True, name="FocusTool sync")
            self._worker.start()

    @staticmethod
    def _work(jobs: queue.Queue, results: queue.Queue, lock: threading.Lock):
        """The sync thread. It touches no Qt object and no live task; it only turns jobs into results."""
        while True:
            job = jobs.get()
            if job is None:
                return
            with lock:
                results.put(run_job(job))

    def _job(self, kind: str, **data) -> dict:
        return {"kind": kind, "engine": self._engine, "cancel": self._cancel, **data}

    def kick_sync(self):
        """Queue a sync of the list as it is now."""
        if self.mode != "github" or self._blocked or self._engine is None:
            return
        if self._shared_dirty:
            self._save_shared()
        if self.AUTO_SYNC:
            self._ensure_worker()
        self._jobs.put(self._job("sync", doc=copy.deepcopy(self._last_doc)))
        self._job_pending, self._sync_due, self._last_sync_at = True, None, time.monotonic()

    def _sync_tick(self):
        """Called by on_tick. Applies finished sync results and starts a sync when one is due. After a
        rejected token it waits until a new token is saved."""
        if self.mode != "github":
            return
        self.drain_sync_results()
        if self._blocked or self._auth_blocked or not self.AUTO_SYNC or self._job_pending:
            return
        due = self._sync_due if self._sync_due is not None else self._last_sync_at + SYNC_POLL_S
        if time.monotonic() >= max(due, self._sync_not_before):
            self.kick_sync()

    def drain_sync_results(self):
        while True:
            try:
                out = self._results.get_nowait()
            except queue.Empty:
                return
            self._apply_job_result(out)

    def sync_now(self) -> gh_store.SyncResult | None:
        """Run the queued GitHub jobs and one sync right here, then apply the results. The app syncs on its
        thread instead; this is for tests and returns the sync's SyncResult."""
        if self.mode != "github" or self._blocked or self._engine is None:
            return None
        with self._job_lock:
            while True:
                try:
                    job = self._jobs.get_nowait()
                except queue.Empty:
                    break
                if job is not None:
                    self._results.put(run_job(job))
            self.drain_sync_results()
            if self._shared_dirty:
                self._save_shared()
            out = run_job(self._job("sync", doc=copy.deepcopy(self._last_doc)))
        out["inline"] = True
        self._last_sync_at = time.monotonic()
        self._apply_job_result(out)
        return out.get("result")

    def _apply_job_result(self, out: dict):
        """On the Qt thread, merge what GitHub had with the list as it is now (it may have changed while
        the sync ran), show the result, copy it to Box, and say how the sync went."""
        if out["kind"] == "sync" and not out.get("inline") and out["engine"] is self._engine:
            self._job_pending = False
        if out["engine"] is not self._engine or self.mode != "github" or out.get("cancelled"):
            return  # from before a disconnect or a switch to another repo
        if out["kind"] == "put_plan":
            if out["error"]:
                self._set_sync_status(f"Sync error: {out['error']}")
            return
        if out.get("newer"):
            self._block_newer(github=True)
            return
        res: gh_store.SyncResult = out["result"]
        if self._shared_dirty:
            self._save_shared()
        try:
            merged = fc.merge_docs(self._last_doc, res.doc)
        except fc.NewerVersionError:
            self._block_newer(github=True)
            return
        if not same_doc(merged, self._last_doc):
            self._apply_state(fc.doc_to_state(merged), baseline=merged)
            self._write_pending()
        if res.status == "ok" and not same_doc(merged, res.doc):
            self._sync_due = time.monotonic()  # edits made during the sync still need pushing
        self._write_mirror(self._last_doc)
        now = datetime.now()
        self._auth_blocked = res.status == "auth"
        if res.status == "ok":
            self._set_sync_status(f"Synced {now:%H:%M}")
        elif res.status == "offline":
            self._set_sync_status("Offline, will retry")
        elif res.status == "auth":
            self._set_sync_status("GitHub token rejected", alert=True)
        elif res.status == "rate_limited":
            wait = float(res.retry_after_s or SYNC_POLL_S)
            self._sync_not_before = time.monotonic() + wait
            self._set_sync_status(f"GitHub busy, retrying at {now + timedelta(seconds=wait):%H:%M}")
        else:
            self._set_sync_status(f"Sync error: {_short(res.message or 'unknown')}")
        if isinstance(out.get("plan"), dict):
            self._set_day_plan(out["plan"], mirror=True)

    # -- day plan
    def _read_day_plan(self):
        """Read data\\day-plan.json (Box mode, and at start). A half-synced file is skipped until it is whole."""
        sig = _file_sig(self.plan_path)
        if sig is None:
            self._plan_sig = self._plan_text = None
            return
        try:
            text = self.plan_path.read_text(encoding="utf-8")
            plan = json.loads(text)
        except (OSError, ValueError):
            return
        self._plan_sig, self._plan_text = sig, text
        if isinstance(plan, dict):
            self._set_day_plan(plan, mirror=False, force=True)

    def _set_day_plan(self, plan: dict, mirror: bool, force: bool = False):
        """Show a day plan. Unless `force`, a plan older than the one shown is ignored (a sync may bring
        back the plan from before a Re-plan that is still uploading). `mirror` writes data\\day-plan.json."""
        old = self.day_plan or {}
        if not force and str(plan.get("generated_at", "")) < str(old.get("generated_at", "")):
            return
        if mirror:
            text = json.dumps(plan, indent=2, ensure_ascii=False)
            try:
                if not self.plan_path.exists() or self.plan_path.read_text(encoding="utf-8") != text:
                    _write_atomic(self.plan_path, text)
                self._plan_sig, self._plan_text = _file_sig(self.plan_path), text
            except OSError:
                pass
        if fc.canonical(plan) != fc.canonical(old):
            self.day_plan = plan
            self.update()
            self._refresh_plan_window()

    def _refresh_plan_window(self):
        """Redraw the Day plan window if it is open (task names and finished tasks change how it looks)."""
        window = getattr(self, "plan_window", None)  # None while the widget is still being built
        if window is not None and window.isVisible():
            window.refresh_plan()

    def toggle_day_plan(self):
        """Open the Day plan window, or close it if it is open."""
        if self.plan_window.isVisible():
            self.plan_window.hide()
        else:
            self.open_day_plan()

    def open_day_plan(self):
        self.plan_window.show()
        self.plan_window.raise_()
        self.plan_window.activateWindow()

    def shown_plan(self) -> dict | None:
        """The plan the Day plan window shows, which is today's if there is one, else tomorrow's."""
        p, today = self.day_plan, self.today()
        if p and p.get("date") in (today.isoformat(), (today + timedelta(days=1)).isoformat()):
            return p
        return None

    def replan_now(self):
        """Plan the rest of today from now (after 7 pm, tomorrow) on this computer, save it to
        data\\day-plan.json, and in GitHub mode upload it too."""
        if self._shared_dirty:
            self._save_shared()
        tz = (self.planner or {}).get("tz") or "America/Chicago"
        # datetime.now() is the PC clock. Reading it as local time in the planner's time zone assumes the
        # PC is set to that zone (America/Chicago by default), as the user's computers are.
        try:
            plan = planner.plan_from_doc(self.build_doc(), local_to_utc(datetime.now(), tz), "now", by="desktop")
        except ZoneInfoNotFoundError:  # the built .exe is missing the tzdata time zone files
            self._set_sync_status(f"Re-plan failed because the time zone data for {tz} is missing. "
                                  "Rebuild or reinstall Focus Tool.", alert=True)
            return
        except Exception as ex:  # for example a date another device saved oddly
            self._set_sync_status(f"Re-plan failed ({_short(ex)})")
            return
        if plan is None:
            return
        self._set_day_plan(plan, mirror=True, force=True)
        if self.mode == "github" and self._engine is not None:
            if self.AUTO_SYNC:
                self._ensure_worker()
            self._jobs.put(self._job("put_plan", plan=plan))

    def set_working_hours(self, windows: dict):
        """New working hours for the planner, such as {"mon": ["09:00-12:00"], ...}."""
        self.planner = {**copy.deepcopy(self.planner or fc.DEFAULT_PLANNER), "windows": copy.deepcopy(windows)}
        self.changed()

    def open_working_hours(self):
        WorkingHoursDialog(self).exec()

    def open_github_dialog(self):
        GitHubDialog(self).exec()

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
        elif t.lower() == "p":
            self.toggle_day_plan()
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
        """Adds a row to log.csv. If the file is locked (say Excel has it open) the row is skipped, so the
        task is still recorded as finished in Completed."""
        try:
            new = not self.log_path.exists()
            with self.log_path.open("a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["finished_at", "task", "estimate_min", "actual_min"])
                w.writerow([rec.finished_at.isoformat(timespec="minutes"), rec.task, rec.estimate_min,
                            rec.actual_min])
        except OSError:
            pass

    def remove_task(self, t: fc.Task):
        """Take a task off the list without finishing it (the other devices get a tombstone)."""
        if t is self.active:
            self.timer.skip()
            self.active = None
            self._set_asking(False)
        self.plan.tasks.remove(t)
        self.changed()

    def drop_ended_repeats(self):
        """A repeating task with a deadline repeats until that day; the day after, it leaves the list."""
        for t in fc.expired_repeats(self.plan.tasks, self.today()):
            self.remove_task(t)

    def open_panel(self):
        if self.ui.get("panel_full") and not self.panel.isVisible():
            self.panel.showFullScreen()
        if self.panel.isMinimized():
            self.panel.restore()
        self.panel.show()
        self.panel.raise_()
        self.panel.activateWindow()

    # -- ticking
    def on_tick(self):
        self._sync_tick()
        if self.today() != self._day:  # past midnight, so repeats wake up and the day plan moves on a day
            self._day = self.today()
            self.drop_ended_repeats()
            self.update()
            if self.panel.isVisible():
                self.panel.refresh()
            self._refresh_plan_window()
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
        menu.addAction(QAction("Day plan...", menu, triggered=widget.open_day_plan))
        menu.addAction(QAction("Shrink / expand", menu, triggered=lambda: widget.set_mini(not widget.mini)))
        menu.addAction(QAction("Start / pause", menu, triggered=widget.toggle_run))
        menu.addAction(QAction("Folders...", menu, triggered=widget.open_folders))
        menu.addAction(QAction("Working hours...", menu, triggered=widget.open_working_hours))
        menu.addAction(QAction("GitHub sync...", menu, triggered=widget.open_github_dialog))
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
        self.show_status("\n".join(x for x in (widget.sync_status, widget.status_message) if x),
                         bool(widget.status_message))

        mods, key = parse_hotkey(settings.get("hotkey_show_hide", "ctrl+alt+f"))
        self.filter = HotkeyFilter(self.toggle)
        app.installNativeEventFilter(self.filter)
        if key and not ctypes.windll.user32.RegisterHotKey(None, HOTKEY_ID, mods | 0x4000, key):
            self.tray.showMessage("Focus Tool", "Could not register the show/hide hotkey (already in use).", icon, 5000)

    def show_status(self, text: str, alert: bool = False):
        """Put the sync status and any warning in the tray tooltip; pop up a notice for a warning."""
        self.tray.setToolTip(f"Focus Tool\n{text}" if text else "Focus Tool")
        if alert and text:
            self.tray.showMessage("Focus Tool", text, QSystemTrayIcon.Warning, 8000)

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
        self.w.shutdown()
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
    app.aboutToQuit.connect(widget.shutdown)
    app.commitDataRequest.connect(lambda *_: widget.save())  # Windows log-off / shutdown
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
