"""Pure logic for the Focus Tool. No Qt, no Windows calls.

Three things live here:
- parse_plan: turns the pasted list into tasks (name, estimate, details).
- TimerState: the countdown for the current task (remaining, elapsed, overtime, checkpoints).
- list_end / save_state / load_state: finish-time projection and JSON persistence.
- merge_states / merge_lines: fold one computer's old list into the list shared through Box.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, List, Optional

STATE_VERSION = 2  # 2 adds the persisted 'done' list; version 1 files still load

# ---------------------------------------------------------------- plan parsing

_DURATION_RE = re.compile(r"\s+(\d+(?:\.\d+)?)\s*(m|min|mins|h|hr|hrs)\s*$", re.IGNORECASE)


@dataclass
class Task:
    name: str
    estimate_min: int
    details: str = ""
    spent_s: float = 0.0  # time used before another task was started over it; picked up again when it restarts


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
        self.estimate_min: int = 0
        self.running: bool = False
        self.paused: bool = False
        self._started_at: Optional[datetime] = None  # when the current running stretch began
        self._banked_s: float = 0.0  # elapsed seconds accumulated before the current stretch
        self._last_now: Optional[datetime] = None
        self._fired: set = set()

    # -- lifecycle
    def start(self, task: str, estimate_min: int, now: datetime, spent_s: float = 0.0) -> None:
        """Start the countdown. `spent_s` is time already used on this task, so it continues where it stopped."""
        self.task = task
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
            "estimate_min": self.estimate_min,
            "running": self.running,
            "paused": self.paused,
            "started_at": self._started_at.isoformat() if self._started_at else None,
            "banked_s": self._banked_s,
            "fired": sorted(self._fired),
            "checkpoints": list(self.checkpoints),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TimerState":
        t = cls(checkpoints=d.get("checkpoints", (0.5, 0.9, 1.0)))
        t.task = d.get("task")
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


def find_active(tasks: List[Task], index: int, name: Optional[str]) -> Optional[Task]:
    """The task a restored timer belongs to: the saved index if the name still matches, else the first name match."""
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
    finished_at: str  # "YYYY-MM-DD HH:MM"
    details: str = ""


def done_to_task(d: DoneTask) -> Task:
    """Put a completed task back on the list with its original estimate and details."""
    return Task(d.name, d.estimate_min, d.details)


@dataclass
class State:
    plan: Plan
    current_index: int
    timer: TimerState
    geometry: Optional[dict] = None
    done: List[DoneTask] = field(default_factory=list)


def _plan_to_dict(plan: Plan) -> dict:
    return {
        "tasks": [{"name": t.name, "estimate_min": t.estimate_min, "details": t.details, "spent_s": t.spent_s}
                  for t in plan.tasks],
    }


def _plan_from_dict(d: dict) -> Plan:
    plan = Plan()
    plan.tasks = [Task(x["name"], int(x["estimate_min"]), x.get("details", ""), float(x.get("spent_s", 0.0)))
                  for x in d.get("tasks", [])]
    return plan


def save_state(path: Path, plan: Plan, current_index: int, timer: TimerState, geometry: Optional[dict] = None,
               done: Optional[List[DoneTask]] = None) -> str:
    """Write the state file in one step (temp file, then rename) and return the text written."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": STATE_VERSION,
        "plan": _plan_to_dict(plan),
        "current_index": current_index,
        "timer": timer.to_dict(),
        "geometry": geometry,
        "done": [asdict(d) for d in (done or [])],
    }
    text = json.dumps(payload, indent=2)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return text


def parse_state(text: str) -> State:
    """Read the saved JSON text. Raises ValueError if it is not a complete state file (say, half synced)."""
    try:
        d = json.loads(text)
        return State(
            plan=_plan_from_dict(d.get("plan", {})),
            current_index=int(d.get("current_index", 0)),
            timer=TimerState.from_dict(d.get("timer", {})),
            geometry=d.get("geometry"),
            done=[DoneTask(x["name"], int(x["estimate_min"]), int(x["actual_min"]), x.get("finished_at", ""),
                           x.get("details", "")) for x in d.get("done", [])],
        )
    except (AttributeError, KeyError, TypeError, ValueError) as ex:
        raise ValueError(f"not a complete state file: {ex}") from ex


def load_state(path: Path) -> State:
    path = Path(path)
    try:
        return parse_state(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return State(Plan(), 0, TimerState())


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
    """`have` plus the lines of `extra` it does not contain yet, in order. Used for dump.txt and log.csv."""
    lines = have.splitlines()
    seen = set(lines)
    for line in extra.splitlines():
        if line.strip() and line not in seen:
            lines.append(line)
            seen.add(line)
    return "".join(f"{line}\n" for line in lines)


# ---------------------------------------------------------------- thought dumps

@dataclass
class Dump:
    when: str
    text: str


def read_dumps(path: Path) -> List[Dump]:
    path = Path(path)
    if not path.exists():
        return []
    out: List[Dump] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        when, _, text = line.partition("  ")
        out.append(Dump(when.strip(), text.strip()))
    return out


def remove_dump(path: Path, index: int) -> None:
    dumps = read_dumps(path)
    if not (0 <= index < len(dumps)):
        return
    del dumps[index]
    Path(path).write_text("".join(f"{d.when}  {d.text}\n" for d in dumps), encoding="utf-8")
