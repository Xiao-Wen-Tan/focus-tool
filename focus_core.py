"""Pure logic for the Focus Tool. No Qt, no Windows calls.

What lives here:
- parse_plan: turns the pasted list into tasks (name, estimate, details).
- TimerState: the countdown for the current task (remaining, elapsed, overtime, checkpoints).
- list_end / save_state / load_state: finish-time projection and JSON persistence (state.json version 3).
- migrate_doc / merge_docs / stamp_changes: read older files, and combine two copies of the list
  (phone and desktop) so that neither side loses an edit.
- display_sections / reorder_ranked / finish_by / next_block: the dated and ranked parts of the list.
- is_active / complete_repeat: repeating tasks.  in_folder / folder_tree / rename_folder / delete_folder: folders.
- merge_states / merge_lines: fold one computer's old list into the list shared through Box.
"""
from __future__ import annotations

import copy
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, List, Optional

STATE_VERSION = 3  # 3 adds ids, dates, folders, repeats and change stamps; versions 1 and 2 still load
EPOCH = "1970-01-01T00:00:00.000Z"
NS = uuid.UUID("212d6585-c191-5e24-9304-4a619852c6c3")  # fixed namespace for ids made from names (migration)
WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_WORKDAY = ["09:00-12:00", "13:30-18:00"]
_WEEKEND = ["10:00-12:30"]
DEFAULT_PLANNER = {
    "tz": "America/Chicago",
    "buffer_business_days": 2,
    "windows": {k: list(_WORKDAY if k not in ("sat", "sun") else _WEEKEND) for k in WEEKDAY_KEYS},
    "updated_at": EPOCH,
}
TOMBSTONE_DAYS = 60


class NewerVersionError(ValueError):
    """The state file was written by a newer version of the app. Never replace it with an empty list."""


def now_iso() -> str:
    """UTC time as 'YYYY-MM-DDTHH:MM:SS.mmmZ', the same text JavaScript's toISOString() gives."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_id() -> str:
    return uuid.uuid4().hex


# ---------------------------------------------------------------- plan parsing

_DURATION_RE = re.compile(r"\s+(\d+(?:\.\d+)?)\s*(m|min|mins|h|hr|hrs)\s*$", re.IGNORECASE)


@dataclass
class Task:
    name: str
    estimate_min: int
    details: str = ""
    spent_s: float = 0.0  # time used before another task was started over it; picked up again when it restarts
    deadline: Optional[str] = None  # "YYYY-MM-DD", a hard date
    target: Optional[str] = None  # "YYYY-MM-DD", the date you aim to finish by
    folder: Optional[str] = None  # "/"-separated path such as "Garden/Seeds"
    repeat: Optional[dict] = None  # see is_active
    last_done: Optional[str] = None  # "YYYY-MM-DD" a repeating task was last done
    id: str = field(default_factory=new_id)
    updated_at: str = EPOCH


@dataclass
class Plan:
    tasks: List[Task] = field(default_factory=list)


def _parse_task_line(line: str, default_minutes: int) -> Task:
    m = _DURATION_RE.search(line)
    if not m:
        return Task(line.strip(), default_minutes)
    value = float(m.group(1))
    unit = m.group(2).lower()
    minutes = value * 60 if unit.startswith("h") else value
    return Task(line[: m.start()].strip(), int(round(minutes)))


def parse_plan(text: str, default_minutes: int) -> Plan:
    plan = Plan()
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        is_detail = raw[:1] in (" ", "\t") or line.startswith("- ")
        if is_detail and plan.tasks:
            body = line[2:].strip() if line.startswith("- ") else line
            t = plan.tasks[-1]
            t.details = f"{t.details}\n{body}" if t.details else body
            continue
        plan.tasks.append(_parse_task_line(line, default_minutes))
    return plan


def move_task(plan: Plan, src: int, dst: int) -> None:
    """Move tasks[src] so it ends up at position dst. Out-of-range indexes are ignored."""
    n = len(plan.tasks)
    if not (0 <= src < n and 0 <= dst < n) or src == dst:
        return
    plan.tasks.insert(dst, plan.tasks.pop(src))


# ---------------------------------------------------------------- timer

@dataclass
class BlockRecord:
    task: str
    estimate_min: int
    actual_min: int
    finished_at: datetime


class TimerState:
    """Countdown for one task.

    Elapsed time is accumulated from wall-clock timestamps passed to tick(),
    so the state can be saved and restored without losing time.
    """

    def __init__(self, checkpoints: Iterable[float] = (0.5, 0.9, 1.0)):
        self.checkpoints = tuple(sorted(checkpoints))
        self.task: Optional[str] = None
        self.task_id: Optional[str] = None
        self.updated_at: str = EPOCH
        self.estimate_min: int = 0
        self.running: bool = False
        self.paused: bool = False
        self._started_at: Optional[datetime] = None  # when the current running stretch began
        self._banked_s: float = 0.0  # elapsed seconds accumulated before the current stretch
        self._last_now: Optional[datetime] = None
        self._fired: set = set()

    # -- lifecycle
    def start(self, task: str, estimate_min: int, now: datetime, spent_s: float = 0.0,
              task_id: Optional[str] = None) -> None:
        """Start the countdown. `spent_s` is time already used on this task, so it continues where it stopped."""
        self.task = task
        self.task_id = task_id
        self.estimate_min = int(estimate_min)
        self.running = True
        self.paused = False
        self._started_at = now
        self._banked_s = float(spent_s)
        self._last_now = now
        self._fired = set()
        if spent_s:
            self.tick(now)  # checkpoints already passed in the earlier stretch do not pulse or chime again

    def pause(self, now: datetime) -> None:
        if self.running and not self.paused:
            self._banked_s += (now - self._started_at).total_seconds()
            self._started_at = None
            self.paused = True
            self._last_now = now

    def resume(self, now: datetime) -> None:
        if self.running and self.paused:
            self._started_at = now
            self.paused = False
            self._last_now = now

    def add_minutes(self, minutes: int) -> None:
        self.estimate_min += int(minutes)
        # Checkpoints below the new fraction may legitimately fire again later.
        self._fired = {c for c in self._fired if c <= self._fraction_elapsed()}

    def skip(self) -> None:
        self.running = False
        self.paused = False
        self._started_at = None

    def done(self, now: datetime) -> BlockRecord:
        self.tick(now)
        rec = BlockRecord(self.task or "", self.estimate_min, int(round(self.elapsed_s / 60)), now)
        self.skip()
        return rec

    # -- time
    def tick(self, now: datetime) -> List[float]:
        """Advance to `now`; return the checkpoint fractions newly crossed."""
        if not self.running:
            return []
        self._last_now = now
        fired = []
        frac = self._fraction_elapsed()
        for c in self.checkpoints:
            if c not in self._fired and frac >= c:
                self._fired.add(c)
                fired.append(c)
        return fired

    @property
    def elapsed_s(self) -> int:
        if not self.running:
            return 0
        live = 0.0
        if self._started_at is not None and self._last_now is not None:
            live = (self._last_now - self._started_at).total_seconds()
        return int(round(self._banked_s + live))

    @property
    def estimate_s(self) -> int:
        return self.estimate_min * 60

    @property
    def remaining_s(self) -> int:
        return max(0, self.estimate_s - self.elapsed_s)

    @property
    def overtime_s(self) -> int:
        return max(0, self.elapsed_s - self.estimate_s)

    @property
    def fraction_remaining(self) -> float:
        if self.estimate_s <= 0:
            return 0.0
        return max(0.0, min(1.0, self.remaining_s / self.estimate_s))

    def _fraction_elapsed(self) -> float:
        if self.estimate_s <= 0:
            return 1.0
        return self.elapsed_s / self.estimate_s

    # -- persistence
    def to_dict(self) -> dict:
        return {
            "task": self.task,
            "task_id": self.task_id,
            "estimate_min": self.estimate_min,
            "running": self.running,
            "paused": self.paused,
            "started_at": self._started_at.isoformat() if self._started_at else None,
            "banked_s": self._banked_s,
            "fired": sorted(self._fired),
            "checkpoints": list(self.checkpoints),
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TimerState":
        t = cls(checkpoints=d.get("checkpoints", (0.5, 0.9, 1.0)))
        t.task = d.get("task")
        t.task_id = d.get("task_id")
        t.updated_at = d.get("updated_at") or EPOCH
        t.estimate_min = int(d.get("estimate_min", 0))
        t.running = bool(d.get("running", False))
        t.paused = bool(d.get("paused", False))
        t._started_at = datetime.fromisoformat(d["started_at"]) if d.get("started_at") else None
        t._banked_s = float(d.get("banked_s", 0.0))
        t._last_now = t._started_at
        t._fired = set(d.get("fired", []))
        return t


# ---------------------------------------------------------------- projection

def list_end(tasks: List[Task], active: Optional[Task], timer: TimerState, now: datetime) -> datetime:
    """Wall-clock time when the list would end at the current estimates. The running task is matched by identity."""
    running = timer.running and active is not None
    remaining = timer.remaining_s if running else 0
    remaining += sum(max(0.0, t.estimate_min * 60 - t.spent_s) for t in tasks if not (running and t is active))
    return now + timedelta(seconds=remaining)


def find_active(tasks: List[Task], index: int, name: Optional[str], task_id: Optional[str] = None) -> Optional[Task]:
    """The task a restored timer belongs to: the task with `task_id` if there is one, else the saved index if
    the name still matches, else the first name match."""
    if task_id:
        hit = next((t for t in tasks if t.id == task_id), None)
        if hit is not None:
            return hit
    if 0 <= index < len(tasks) and tasks[index].name == name:
        return tasks[index]
    return next((t for t in tasks if t.name == name), None)


# ---------------------------------------------------------------- persistence

@dataclass
class DoneTask:
    """A finished task, kept so the completed list survives restarts."""
    name: str
    estimate_min: int
    actual_min: int
    finished_at: str  # "YYYY-MM-DD HH:MM", local time
    details: str = ""
    id: str = field(default_factory=new_id)
    deadline: Optional[str] = None
    target: Optional[str] = None
    folder: Optional[str] = None
    updated_at: str = EPOCH


def done_to_task(d: DoneTask) -> Task:
    """Put a completed task back on the list (with a new id) with its estimate, details, dates and folder."""
    return Task(d.name, d.estimate_min, d.details, deadline=d.deadline, target=d.target, folder=d.folder)


@dataclass
class State:
    plan: Plan
    current_index: int
    timer: TimerState
    geometry: Optional[dict] = None
    done: List[DoneTask] = field(default_factory=list)
    deleted: dict = field(default_factory=dict)  # id -> when it was deleted (tombstones)
    order_updated_at: str = EPOCH
    planner: Optional[dict] = None  # None means DEFAULT_PLANNER
    folders: list = field(default_factory=list)
    folders_updated_at: str = EPOCH


_TASK_FIELDS = ("name", "estimate_min", "details", "spent_s", "deadline", "target", "folder", "repeat", "last_done")
_DONE_FIELDS = ("name", "estimate_min", "actual_min", "finished_at", "details", "deadline", "target", "folder")


def task_to_dict(t: Task) -> dict:
    d = {"id": t.id}
    d.update({k: copy.deepcopy(getattr(t, k)) for k in _TASK_FIELDS})
    d["updated_at"] = t.updated_at
    return d


def task_from_dict(x: dict) -> Task:
    return Task(x["name"], int(x["estimate_min"]), x.get("details", ""), float(x.get("spent_s", 0.0)),
                deadline=x.get("deadline"), target=x.get("target"), folder=x.get("folder"),
                repeat=copy.deepcopy(x.get("repeat")), last_done=x.get("last_done"),
                id=x.get("id") or new_id(), updated_at=x.get("updated_at") or EPOCH)


def _done_to_dict(d: DoneTask) -> dict:
    out = {"id": d.id}
    out.update({k: getattr(d, k) for k in _DONE_FIELDS})
    out["updated_at"] = d.updated_at
    return out


def _done_from_dict(x: dict) -> DoneTask:
    return DoneTask(x["name"], int(x["estimate_min"]), int(x["actual_min"]), x.get("finished_at", ""),
                    x.get("details", ""), id=x.get("id") or new_id(), deadline=x.get("deadline"),
                    target=x.get("target"), folder=x.get("folder"), updated_at=x.get("updated_at") or EPOCH)


def state_to_doc(st: State) -> dict:
    """The state.json version 3 document for a State."""
    return {
        "version": STATE_VERSION,
        "plan": {"tasks": [task_to_dict(t) for t in st.plan.tasks]},
        "order_updated_at": st.order_updated_at,
        "folders": list(st.folders),
        "folders_updated_at": st.folders_updated_at,
        "current_index": st.current_index,
        "timer": st.timer.to_dict(),
        "done": [_done_to_dict(d) for d in st.done],
        "deleted": dict(st.deleted),
        "planner": copy.deepcopy(st.planner if st.planner is not None else DEFAULT_PLANNER),
        "geometry": st.geometry,
    }


def doc_to_state(d: dict) -> State:
    """A State from a state.json document of any version up to 3. Raises ValueError for a newer version."""
    d = migrate_doc(d)
    return State(
        plan=Plan([task_from_dict(x) for x in d["plan"]["tasks"]]),
        current_index=int(d["current_index"]),
        timer=TimerState.from_dict(d["timer"]),
        geometry=d["geometry"],
        done=[_done_from_dict(x) for x in d["done"]],
        deleted=dict(d["deleted"]),
        order_updated_at=d["order_updated_at"],
        planner=d["planner"],
        folders=list(d["folders"]),
        folders_updated_at=d["folders_updated_at"],
    )


def _migrated_id(kind: str, key: str) -> str:
    return uuid.uuid5(NS, f"{kind}:{key}").hex


def _fill_task(x: dict, nth: int) -> dict:
    out = {"id": x.get("id") or _migrated_id("task", f"{x['name']}#{nth}"),
           "name": x["name"], "estimate_min": x["estimate_min"]}
    for k in _TASK_FIELDS[2:]:
        out[k] = copy.deepcopy(x.get(k, Task.__dataclass_fields__[k].default))
    out["updated_at"] = x.get("updated_at") or EPOCH
    return out


def _fill_done(x: dict) -> dict:
    out = {"id": x.get("id") or _migrated_id("done", f"{x['name']}#{x.get('finished_at', '')}")}
    for k in _DONE_FIELDS:
        out[k] = x.get(k, "" if k in ("details", "finished_at") else None)
    out["updated_at"] = x.get("updated_at") or EPOCH
    return out


def migrate_doc(d: dict) -> dict:
    """A complete version 3 document from a version 1, 2 or 3 one, with every missing field filled in.

    Ids made here depend only on the old content, so two computers that migrate the same file agree:
    task id = uuid5(NS, "task:" + name + "#" + n) where n counts earlier tasks with the same name (0, 1, ...),
    done id = uuid5(NS, "done:" + name + "#" + finished_at). The timer's task_id is the task at the saved
    index if its name matches the timer's task, else the first task with that name. Stamps start at EPOCH.
    Raises NewerVersionError if the version is newer than 3, ValueError if it is not a number.
    """
    if not isinstance(d, dict):
        raise ValueError("state document is not an object")
    try:
        version = int(d.get("version", 1))
    except (TypeError, ValueError) as ex:
        raise ValueError(f"state version is not a number: {d.get('version')!r}") from ex
    if version > STATE_VERSION:
        raise NewerVersionError(f"state version {version} is newer than this app understands ({STATE_VERSION})")
    seen: dict = {}
    tasks = []
    for x in d.get("plan", {}).get("tasks", []):
        nth = seen.get(x["name"], 0)
        seen[x["name"]] = nth + 1
        tasks.append(_fill_task(x, nth))
    old_timer = d.get("timer") or {}
    timer = TimerState().to_dict()
    timer.update(copy.deepcopy(old_timer))
    timer["updated_at"] = timer.get("updated_at") or EPOCH
    current_index = int(d.get("current_index", 0))
    if version < STATE_VERSION or "task_id" not in old_timer:
        timer["task_id"] = None
        name = timer.get("task")
        if name is not None:
            if 0 <= current_index < len(tasks) and tasks[current_index]["name"] == name:
                timer["task_id"] = tasks[current_index]["id"]
            else:
                timer["task_id"] = next((t["id"] for t in tasks if t["name"] == name), None)
    planner = copy.deepcopy(d.get("planner")) if d.get("planner") else copy.deepcopy(DEFAULT_PLANNER)
    planner.setdefault("updated_at", EPOCH)
    return {
        "version": STATE_VERSION,
        "plan": {"tasks": tasks},
        "order_updated_at": d.get("order_updated_at") or EPOCH,
        "folders": list(d.get("folders") or []),
        "folders_updated_at": d.get("folders_updated_at") or EPOCH,
        "current_index": current_index,
        "timer": timer,
        "done": [_fill_done(x) for x in d.get("done", [])],
        "deleted": dict(d.get("deleted") or {}),
        "planner": planner,
        "geometry": d.get("geometry"),
    }


def _write_text(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def save_state_obj(path: Path, st: State) -> str:
    """Write the whole State as version 3 in one step (temp file, then rename) and return the text written."""
    text = json.dumps(state_to_doc(st), indent=2, ensure_ascii=False)
    _write_text(path, text)
    return text


def save_state(path: Path, plan: Plan, current_index: int, timer: TimerState, geometry: Optional[dict] = None,
               done: Optional[List[DoneTask]] = None) -> str:
    """Write the state file in one step and return the text written. Fields not passed get their defaults;
    use save_state_obj to keep tombstones, stamps, folders and the planner block."""
    return save_state_obj(path, State(plan, current_index, timer, geometry, list(done or [])))


def parse_state(text: str) -> State:
    """Read the saved JSON text. Raises ValueError if it is not a complete state file (say, half synced)
    or NewerVersionError if it was written by a newer version."""
    try:
        return doc_to_state(json.loads(text))
    except NewerVersionError:
        raise
    except (AttributeError, KeyError, TypeError, ValueError) as ex:
        raise ValueError(f"not a complete state file: {ex}") from ex


def load_state(path: Path) -> State:
    """The saved State; an empty one if the file is missing or incomplete. A file from a newer version
    raises NewerVersionError, so the caller never saves an empty list over it."""
    path = Path(path)
    try:
        return parse_state(path.read_text(encoding="utf-8"))
    except NewerVersionError:
        raise
    except (OSError, ValueError):
        return State(Plan(), 0, TimerState())


# ---------------------------------------------------------------- merge and stamp (same rules in docs/core.js)

def _plain_numbers(x):
    """Whole-number floats as ints, so 0.0 here and 0 in JavaScript give the same canonical text."""
    if isinstance(x, float) and x.is_integer():
        return int(x)
    if isinstance(x, dict):
        return {k: _plain_numbers(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_plain_numbers(v) for v in x]
    return x


def canonical(x) -> str:
    """Stable JSON text used to break ties: sorted keys, no spaces, non-ASCII kept, 1.0 written as 1."""
    return json.dumps(_plain_numbers(x), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _newer(x: dict, y: dict, stamp: str = "updated_at") -> dict:
    """The one of two versions with the larger stamp; equal stamps: the larger canonical text."""
    kx, ky = (x.get(stamp) or EPOCH, canonical(x)), (y.get(stamp) or EPOCH, canonical(y))
    return x if kx >= ky else y


def _union_by_id(xs: list, ys: list) -> dict:
    out: dict = {}
    for item in list(xs) + list(ys):
        have = out.get(item["id"])
        out[item["id"]] = item if have is None else _newer(have, item)
    return out


def _with_parents(path: str) -> List[str]:
    parts = path.split("/")
    return ["/".join(parts[: i + 1]) for i in range(len(parts))]


def _idle_timer(timer: dict) -> dict:
    timer = dict(timer)
    timer.update(running=False, paused=False, started_at=None, banked_s=0, task=None, task_id=None)
    return timer


def merge_docs(a: dict, b: dict) -> dict:
    """Combine two copies of the list. merge_docs(a, b) == merge_docs(b, a), and merging a result with
    itself changes nothing. Deletions always win over edits; otherwise the newer stamp wins per item."""
    a, b = migrate_doc(a), migrate_doc(b)
    deleted = dict(a["deleted"])
    for k, v in b["deleted"].items():
        deleted[k] = max(v, deleted.get(k, v))
    done_by_id = _union_by_id(a["done"], b["done"])
    done = sorted((d for i, d in done_by_id.items() if i not in deleted), key=lambda d: (d["finished_at"], d["id"]))
    done_ids = {d["id"] for d in done}
    tasks_by_id = {i: t for i, t in _union_by_id(a["plan"]["tasks"], b["plan"]["tasks"]).items()
                   if i not in deleted and i not in done_ids}
    a_ids = [t["id"] for t in a["plan"]["tasks"]]
    b_ids = [t["id"] for t in b["plan"]["tasks"]]
    if (a["order_updated_at"], canonical(a_ids)) >= (b["order_updated_at"], canonical(b_ids)):
        win, lose = a_ids, b_ids
    else:
        win, lose = b_ids, a_ids
    win_set = set(win)
    order = [i for i in lose if i in tasks_by_id and i not in win_set] + [i for i in win if i in tasks_by_id]
    tasks = [tasks_by_id[i] for i in dict.fromkeys(order)]
    timer = copy.deepcopy(_newer(a["timer"], b["timer"]))
    if timer.get("running") and timer.get("task_id") not in tasks_by_id:
        timer = _idle_timer(timer)
    planner = copy.deepcopy(_newer(a["planner"], b["planner"]))
    fa = {"folders": a["folders"], "folders_updated_at": a["folders_updated_at"]}
    fb = {"folders": b["folders"], "folders_updated_at": b["folders_updated_at"]}
    folders = list(_newer(fa, fb, "folders_updated_at")["folders"])
    used = sorted({p for t in tasks if t.get("folder") for p in _with_parents(t["folder"])} - set(folders))
    folders += used
    ids = [t["id"] for t in tasks]
    return {
        "version": STATE_VERSION,
        "plan": {"tasks": copy.deepcopy(tasks)},
        "order_updated_at": max(a["order_updated_at"], b["order_updated_at"]),
        "folders": folders,
        "folders_updated_at": max(a["folders_updated_at"], b["folders_updated_at"]),
        "current_index": ids.index(timer["task_id"]) if timer.get("task_id") in ids else 0,
        "timer": timer,
        "done": copy.deepcopy(done),
        "deleted": deleted,
        "planner": planner,
        "geometry": None,
    }


_TIMER_IGNORED = ("fired", "checkpoints", "updated_at")


def _without(d: dict, keys: tuple) -> dict:
    return {k: v for k, v in d.items() if k not in keys}


def _iso_minus_days(stamp: str, days: int) -> str:
    t = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S.%fZ") - timedelta(days=days)
    return f"{t:%Y-%m-%dT%H:%M:%S}.{t.microsecond // 1000:03d}Z"


def stamp_changes(prev: Optional[dict], cur: dict, now: str) -> dict:
    """`cur` (a complete copy is returned) with change stamps set to `now` wherever it differs from `prev`,
    the last version saved or synced. prev None: only fill in missing fields."""
    cur = migrate_doc(cur)
    if prev is None:
        return cur
    prev = migrate_doc(prev)
    prev_tasks = {t["id"]: t for t in prev["plan"]["tasks"]}
    prev_done = {d["id"]: d for d in prev["done"]}
    cur_task_ids = [t["id"] for t in cur["plan"]["tasks"]]
    cur_done_ids = {d["id"] for d in cur["done"]}
    for t in cur["plan"]["tasks"]:
        old = prev_tasks.get(t["id"])
        if old is None or any(t[k] != old[k] for k in _TASK_FIELDS):
            t["updated_at"] = now
    for d in cur["done"]:
        old = prev_done.get(d["id"])
        if old is None or any(d[k] != old[k] for k in _DONE_FIELDS):
            d["updated_at"] = now
    cur_set = set(cur_task_ids)
    both = [i for i in cur_task_ids if i in prev_tasks]
    if both != [i for i in prev_tasks if i in cur_set] or len(both) != len(cur_task_ids):
        cur["order_updated_at"] = now  # reordered, or a task was added
    for i in list(prev_tasks) + list(prev_done):
        if i not in cur_set and i not in cur_done_ids:
            cur["deleted"][i] = now
    if _without(cur["timer"], _TIMER_IGNORED) != _without(prev["timer"], _TIMER_IGNORED):
        cur["timer"]["updated_at"] = now
    if _without(cur["planner"], ("updated_at",)) != _without(prev["planner"], ("updated_at",)):
        cur["planner"]["updated_at"] = now
    if cur["folders"] != prev["folders"]:
        cur["folders_updated_at"] = now
    cutoff = _iso_minus_days(now, TOMBSTONE_DAYS)
    cur["deleted"] = {k: v for k, v in cur["deleted"].items() if v >= cutoff}
    return cur


# ---------------------------------------------------------------- dated and ranked sections

def _is_dated(t: Task) -> bool:
    return bool(t.deadline or t.target)


def display_sections(tasks: List[Task]) -> tuple:
    """(dated, ranked). Dated: tasks with a deadline or target, earliest date first, a deadline before a
    target-only task on the same date, then list order. Ranked: all other tasks (repeats too) in list order."""
    dated = [(min(x for x in (t.deadline, t.target) if x), t.deadline is None, i, t)
             for i, t in enumerate(tasks) if _is_dated(t)]
    return [x[3] for x in sorted(dated, key=lambda x: x[:3])], [t for t in tasks if not _is_dated(t)]


def reorder_ranked(tasks: List[Task], ranked_ids: list) -> List[Task]:
    """A new list where the ranked tasks follow `ranked_ids`, in the slots ranked tasks used; dated tasks
    keep their slots. Ranked tasks missing from `ranked_ids` follow in their old order; unknown ids are ignored."""
    ranked = [t for t in tasks if not _is_dated(t)]
    by_id = {t.id: t for t in ranked}
    picked = [by_id[i] for i in dict.fromkeys(ranked_ids) if i in by_id]
    picked_ids = {t.id for t in picked}
    queue = iter(picked + [t for t in ranked if t.id not in picked_ids])
    return [t if _is_dated(t) else next(queue) for t in tasks]


def finish_by(d: date, buffer_days: int = 2) -> date:
    """`d` minus `buffer_days` working days (Mon to Fri): Fri -> Wed, Mon -> Thu, Sat or Sun -> Thu."""
    left = int(buffer_days)
    while left > 0:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            left -= 1
    return d


def next_block(day_plan: Optional[dict], now: datetime, live_ids: set) -> Optional[dict]:
    """The block of today's plan to work on: the first block of a live task that ends after `now`
    (the current block if `now` is inside it, else the next one). None if the plan is not for today."""
    if not day_plan or day_plan.get("date") != now.date().isoformat():
        return None
    for b in day_plan.get("blocks", []):
        hh, mm = (int(x) for x in b["end"].split(":"))
        end = datetime(now.year, now.month, now.day) + timedelta(hours=hh, minutes=mm)
        if b.get("task_id") in live_ids and end > now:
            return b
    return None


# ---------------------------------------------------------------- repeating tasks

def _rule_matches(rule: dict, day: date) -> bool:
    kind = rule.get("kind")
    if kind == "daily":
        return True
    if kind == "weekdays":
        return day.weekday() < 5
    if kind == "days":
        return WEEKDAY_KEYS[day.weekday()] in rule.get("days", [])
    if kind == "every":
        gap = (day - date.fromisoformat(rule["anchor"])).days
        return gap >= 0 and gap % max(1, int(rule.get("every", 1))) == 0
    return False


def is_active(task: Task, day: date) -> bool:
    """A repeating task is active on days its rule matches, unless it was already done that day.
    "every" counts from its anchor date on the calendar, so a skipped day does not shift it.
    A task that does not repeat is always active."""
    if not task.repeat:
        return True
    return _rule_matches(task.repeat, day) and task.last_done != day.isoformat()


def next_active_day(task: Task, after: date) -> Optional[date]:
    """The first day after `after` on which a repeating task is active; None for a task that does not repeat."""
    if not task.repeat:
        return None
    rule = task.repeat
    if rule.get("kind") == "every":
        anchor, step = date.fromisoformat(rule["anchor"]), max(1, int(rule.get("every", 1)))
        gap = (after + timedelta(days=1) - anchor).days
        d = anchor if gap <= 0 else anchor + timedelta(days=-(-gap // step) * step)
        return d if is_active(task, d) else d + timedelta(days=step)
    for n in range(1, 16):
        d = after + timedelta(days=n)
        if is_active(task, d):
            return d
    return None


def complete_repeat(task: Task, now: datetime, actual_min: int) -> DoneTask:
    """Done on a repeating task: log that day's copy and put the task to sleep until its next day.
    The copy's id is the same on every device for the same task and day."""
    day = now.date().isoformat()
    rec = DoneTask(task.name, task.estimate_min, int(actual_min), f"{now:%Y-%m-%d %H:%M}", task.details,
                   id=uuid.uuid5(NS, f"rep:{task.id}#{day}").hex, folder=task.folder)
    task.last_done = day
    task.spent_s = 0.0
    return rec


def finish_task(task: Task, now: datetime, actual_min: int) -> DoneTask:
    """The done entry for finishing `task`. It keeps the task's id, so a merge drops the task from the list
    even if another device edited it meanwhile. A repeating task goes through complete_repeat instead."""
    if task.repeat:
        return complete_repeat(task, now, actual_min)
    return DoneTask(task.name, task.estimate_min, int(actual_min), f"{now:%Y-%m-%d %H:%M}", task.details,
                    id=task.id, deadline=task.deadline, target=task.target, folder=task.folder)


# ---------------------------------------------------------------- folders

def _under(folder: Optional[str], path: str) -> bool:
    return folder is not None and (folder == path or folder.startswith(path + "/"))


def in_folder(task: Task, path: Optional[str]) -> bool:
    """True if `task` is in folder `path` or one of its subfolders. path None means all tasks."""
    return path is None or _under(task.folder, path)


def folder_tree(folders: list, tasks: List[Task]) -> list:
    """Every known folder path with its depth (0 = top level), parents included, each folder followed by its
    subfolders: [("Puzzle", 0), ("Puzzle/Tiles", 1), ("Puzzle B", 0)]."""
    paths = {p for f in list(folders) + [t.folder for t in tasks if t.folder] for p in _with_parents(f)}
    return [(p, p.count("/")) for p in sorted(paths, key=lambda p: p.split("/"))]


def _complete_parents(paths: list) -> list:
    out = list(dict.fromkeys(paths))
    for p in list(out):
        for parent in _with_parents(p)[:-1]:
            if parent not in out:
                out.append(parent)
    return out


def rename_folder(state: State, old: str, new: str) -> None:
    """Rename folder `old` to `new`, with its subfolders, in the folder list and on tasks and done tasks."""
    def move(p: Optional[str]) -> Optional[str]:
        return new + p[len(old):] if _under(p, old) else p
    state.folders = _complete_parents([move(p) for p in state.folders])
    for t in list(state.plan.tasks) + list(state.done):
        t.folder = move(t.folder)


def delete_folder(state: State, path: str) -> None:
    """Remove folder `path` and its subfolders; their tasks stay on the list without a folder."""
    state.folders = [p for p in state.folders if not _under(p, path)]
    for t in state.plan.tasks:
        if _under(t.folder, path):
            t.folder = None


# ---------------------------------------------------------------- one-time merge of an older local list

def merge_states(shared: State, old: State) -> State:
    """The shared list plus whatever `old` (one computer's list from before syncing) does not have yet.

    Tasks match by name, completed tasks by name and finish time, so nothing shows up twice. The shared
    timer wins; the old one is kept only when the shared timer is idle.
    """
    names = {t.name for t in shared.plan.tasks}
    tasks = shared.plan.tasks + [t for t in old.plan.tasks if t.name not in names]
    seen = {(d.name, d.finished_at) for d in shared.done}
    done = sorted(shared.done + [d for d in old.done if (d.name, d.finished_at) not in seen],
                  key=lambda d: d.finished_at)
    timer, index = shared.timer, shared.current_index
    if not shared.timer.running and old.timer.running:
        timer = old.timer
        index = next((i for i, t in enumerate(tasks) if t.name == old.timer.task), 0)
    return State(Plan(tasks), index, timer, None, done)


def merge_lines(have: str, extra: str) -> str:
    """`have` plus the lines of `extra` it does not contain yet, in order. Used for log.csv."""
    lines = have.splitlines()
    seen = set(lines)
    for line in extra.splitlines():
        if line.strip() and line not in seen:
            lines.append(line)
            seen.add(line)
    return "".join(f"{line}\n" for line in lines)
