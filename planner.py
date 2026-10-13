"""Day planner for the Focus Tool. Pure logic (standard library plus focus_core), no Qt.

It turns the task list in state.json into a plan for one day (day-plan.json). The plan has time blocks
inside the working hours in the `planner` block of state.json, an outlook of dated work reserved for later
days, and warnings about dates that are at risk.

How a day is planned (contracts.md section 4):
1. Work out the minutes left per task (estimate minus time already used) and skip tasks with none left.
2. Urgent work. Dated tasks whose finish-by day (the date minus 2 working days) is already past go first.
3. Hard pass. Deadline tasks are reserved as late as possible before their finish-by day.
   A deadline with a time of day (deadline_time) is passed from that minute, and on the deadline day
   itself the task only gets time before it.
4. Soft pass. Tasks with a target date ("aim to finish by") are reserved the same way in what is left.
5. Ranked pass. Undated tasks (and repeating tasks active that day) fill the plan day in list order.
   A repeating task is always planned as a repeat, never as dated work. If it has a deadline, that is the
   last day it repeats, so it gets its block only on days it is active up to and including the deadline.
6. Ahead pass. Any free time left on the plan day pulls dated work forward from its latest reserved day.

Command line, as used by the GitHub Actions job and for testing:
    python planner.py --state state.json --out day-plan.json --mode auto|now|tomorrow [--now ISO-UTC]
                      [--by actions|desktop]
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import os
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import focus_core as fc  # noqa: E402

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover (Python 3.8 and older is not supported)
    ZoneInfo = None

PLAN_VERSION = 1
EVENING_HOUR = 19  # from 7 pm on, "auto" and "now" plan tomorrow
MAX_DAYS = 366  # how far ahead overflowing work may be placed
_KIND_RANK = {"urgent": 0, "dated": 1, "ranked": 2, "ahead": 3}  # which kind a merged block keeps


# ---------------------------------------------------------------- small helpers

def _hhmm(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def _minutes(s: str) -> int:
    hh, mm = s.strip().split(":")
    return int(hh) * 60 + int(mm)


def _windows(cfg: dict, day: date) -> list:
    """Working-hour intervals of `day` as [start, end] minutes from midnight, sorted."""
    out = []
    for w in (cfg.get("windows") or {}).get(fc.WEEKDAY_KEYS[day.weekday()], []):
        a, b = w.split("-")
        s, e = _minutes(a), _minutes(b)
        if e > s:
            out.append([s, e])
    return _normalize(out)


def _normalize(ivs: list) -> list:
    out = []
    for s, e in sorted(ivs):
        if e <= s:
            continue
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return out


def _fmt_day(d: date) -> str:
    return f"{fc.WEEKDAY_KEYS[d.weekday()].capitalize()} {d:%m/%d}"


def _iso_ms(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _ceil5(dt: datetime) -> datetime:
    """`dt` rounded up to the next whole 5 minutes (14:12 -> 14:15, 14:15:00 stays)."""
    base = dt.replace(minute=0, second=0, microsecond=0)
    secs = (dt - base).total_seconds()
    return base + timedelta(minutes=5 * math.ceil(secs / 300))


def _cfg_with_defaults(cfg: Optional[dict]) -> dict:
    out = copy.deepcopy(fc.DEFAULT_PLANNER)
    out.update(copy.deepcopy(cfg or {}))
    return out


def _timer_elapsed_s(timer, now_local: datetime) -> float:
    """Seconds used on the timer's task so far, without changing `timer`."""
    if timer is None:
        return 0.0
    d = timer if isinstance(timer, dict) else timer.to_dict()
    t = fc.TimerState.from_dict(d)
    if not t.running:
        return 0.0
    t.tick(now_local.replace(tzinfo=None))
    return max(0.0, float(t.elapsed_s))


def _is_timer_task(task, timer) -> bool:
    if timer is None:
        return False
    d = timer if isinstance(timer, dict) else timer.to_dict()
    if not d.get("running"):
        return False
    if d.get("task_id"):
        return d["task_id"] == task.id
    return d.get("task") == task.name


def remaining_minutes(task, timer, now_local: datetime) -> int:
    """Whole minutes still to do on `task`: its estimate minus the time already used, rounded up.
    Time on a running (or paused) timer counts when the timer belongs to this task."""
    secs = task.estimate_min * 60 - float(task.spent_s or 0.0)
    if _is_timer_task(task, timer):
        secs -= _timer_elapsed_s(timer, now_local)
    secs = round(max(0.0, secs), 3)
    return int(math.ceil(secs / 60))


# ---------------------------------------------------------------- the calendar of free time

class _Calendar:
    """Free time per day. Days are built when first needed. Every day after the plan day starts with
    the minutes of repeating tasks active that day already taken from the start of its windows."""

    def __init__(self, cfg: dict, plan_day: date, start_min: Optional[int], repeats: list):
        self.cfg = cfg
        self.plan_day = plan_day
        self.start_min = start_min
        self.repeats = repeats  # (task, minutes) in rank order
        self.free = {}
        self.last_day = plan_day + timedelta(days=MAX_DAYS)

    def day(self, d: date) -> list:
        if d not in self.free:
            ivs = _windows(self.cfg, d)
            if d == self.plan_day and self.start_min is not None:
                ivs = [[max(s, self.start_min), e] for s, e in ivs if e > self.start_min]
            self.free[d] = ivs
            if d > self.plan_day:
                for task, mins in self.repeats:
                    if fc.is_active(task, d):
                        self.take_asap(d, mins)
        return self.free[d]

    def total(self, d: date, until: Optional[int] = None) -> int:
        """Free minutes on day `d`, only those before minute `until` when it is given."""
        end = 24 * 60 if until is None else until
        return sum(max(0, min(e, end) - s) for s, e in self.day(d))

    def take_asap(self, d: date, need: int, until: Optional[int] = None) -> list:
        """Take up to `need` minutes from the start of day `d`, none after minute `until` when it is given.
        Returns the [start, end] pieces taken."""
        end = 24 * 60 if until is None else until
        got, ivs = [], self.day(d)
        while need > 0 and ivs and ivs[0][0] < end:
            s, e = ivs[0]
            n = min(need, min(e, end) - s)
            got.append((s, s + n))
            need -= n
            if s + n >= e:
                ivs.pop(0)
            else:
                ivs[0][0] = s + n
        return got

    def take_alap(self, d: date, need: int, until: Optional[int] = None) -> list:
        """Take up to `need` minutes from the end of day `d` (from minute `until` back, when it is given)."""
        end = 24 * 60 if until is None else until
        got, ivs = [], self.day(d)
        i = len(ivs) - 1
        while need > 0 and i >= 0:
            s, e = ivs[i]
            top = min(e, end)
            if top <= s:
                i -= 1
                continue
            n = min(need, top - s)
            got.append((top - n, top))
            need -= n
            if top < e:  # the part after `until` stays free
                ivs.insert(i + 1, [top, e])
            if top - n <= s:
                ivs.pop(i)
            else:
                ivs[i][1] = top - n
            i -= 1
        return got

    def release(self, d: date, s: int, e: int) -> None:
        self.free[d] = _normalize(self.day(d) + [[s, e]])


# ---------------------------------------------------------------- planning one day

class _Job:
    """One task's planning data and its reserved time slots."""

    def __init__(self, task, index: int, minutes: int, buffer_days: int):
        self.task, self.index, self.minutes = task, index, minutes
        # A repeating task's deadline only says when it stops repeating, so it is planned as undated work.
        self.deadline = date.fromisoformat(task.deadline) if task.deadline and not task.repeat else None
        self.target = date.fromisoformat(task.target) if task.target and not task.repeat else None
        if self.deadline and self.target and self.target > self.deadline:
            self.target = None  # a target later than the deadline adds nothing
        self.cut = fc.parse_hm(task.deadline_time) if self.deadline else None  # minute the deadline falls at
        self.fb_dead = fc.finish_by(self.deadline, buffer_days) if self.deadline else None
        self.fb_tgt = fc.finish_by(self.target, buffer_days) if self.target else None
        self.slots = []  # one dict per slot with day, s, e, kind and date_kind

    @property
    def dated(self) -> bool:
        return bool(self.deadline or self.target)

    @property
    def min_fb(self) -> date:
        return min(d for d in (self.fb_dead, self.fb_tgt) if d)

    @property
    def placed(self) -> int:
        return sum(x["e"] - x["s"] for x in self.slots)

    def until(self, d: date) -> Optional[int]:
        """The minute work must stop on day `d`: the deadline time on the deadline day, else None."""
        return self.cut if d == self.deadline else None

    @property
    def main_kind(self) -> str:
        return "deadline" if self.deadline else "target"

    def add(self, d: date, pieces: list, kind: str, date_kind: Optional[str]) -> int:
        for s, e in pieces:
            self.slots.append({"day": d, "s": s, "e": e, "kind": kind, "date_kind": date_kind})
        return sum(e - s for s, e in pieces)


def _place_alap(cal: _Calendar, job: _Job, need: int, last: date, kind: str, date_kind: str) -> int:
    """Reserve up to `need` minutes as late as possible, from the end of `last` back to the plan day."""
    got, d = 0, last
    while got < need and d >= cal.plan_day:
        got += job.add(d, cal.take_alap(d, need - got, job.until(d)), kind, date_kind)
        d -= timedelta(days=1)
    return got


def _place_asap(cal: _Calendar, job: _Job, need: int, first: date, last: date, kind: str,
                date_kind: Optional[str]) -> int:
    """Reserve up to `need` minutes as early as possible, from `first` to `last` (both included)."""
    got, d = 0, max(first, cal.plan_day)
    last = min(last, cal.last_day)
    while got < need and d <= last:
        got += job.add(d, cal.take_asap(d, need - got, job.until(d)), kind, date_kind)
        d += timedelta(days=1)
    return got


def _fmt_when(job: _Job, d: date, kind: str) -> str:
    """The day for a warning, with the deadline time when it has one: "Fri 10/16 5:00pm"."""
    if kind == "deadline" and job.cut is not None:
        return f"{_fmt_day(d)} {fc.fmt_hm(job.task.deadline_time)}"
    return _fmt_day(d)


def _warn(job: _Job, kind: str, message: str, minutes: Optional[int] = None) -> dict:
    w = {"task_id": job.task.id, "name": job.task.name, "kind": kind, "message": message}
    if minutes is not None:
        w["minutes"] = minutes
    return w


def plan_day(state, day: date, start: Optional[time], cfg: dict, now_local: Optional[datetime] = None) -> dict:
    """Plan `day` from `start` (None = the day's first working hour). `now_local` (naive local time) is
    used for the running timer; it defaults to `day` at `start`."""
    cfg = _cfg_with_defaults(cfg)
    buffer_days = int(cfg.get("buffer_business_days", 2))
    start_min = start.hour * 60 + start.minute if start is not None else None
    if now_local is None:
        now_local = datetime.combine(day, start or time(0))
    timer = state.timer

    jobs = []
    for i, t in enumerate(state.plan.tasks):
        if t.repeat and not fc.is_active(t, day):
            continue  # asleep today, or past its deadline
        m = remaining_minutes(t, timer, now_local)
        if m > 0:
            jobs.append(_Job(t, i, m, buffer_days))
    dated = [j for j in jobs if j.dated]
    ranked = [j for j in jobs if not j.dated]
    repeats = [(t, t.estimate_min) for t in state.plan.tasks
               if t.repeat and t.estimate_min > 0]

    cal = _Calendar(cfg, day, start_min, repeats)
    capacity = cal.total(day)
    first_free = cal.day(day)[0][0] if cal.day(day) else (start_min or 0)
    warnings = []

    # 2. Urgent work comes first, because its finish-by day is already past.
    urgent = [j for j in dated if j.min_fb < day]
    for j in urgent:
        opts = [(fb, d, k) for fb, d, k in ((j.fb_dead, j.deadline, "deadline"), (j.fb_tgt, j.target, "target"))
                if fb]
        j.trigger = min(opts, key=lambda o: (o[0], o[2] != "deadline"))
    urgent.sort(key=lambda j: (j.deadline is None, j.trigger[1], j.index))
    for j in urgent:
        _, when, k = j.trigger
        _place_asap(cal, j, j.minutes, day, cal.last_day, "urgent", k)
        passed_today = (k == "deadline" and when == day and j.cut is not None and start_min is not None
                        and start_min >= j.cut)
        if when < day or passed_today:
            warnings.append(_warn(j, "overdue", f"its {k} {_fmt_when(j, when, k)} has passed"))
        else:
            warnings.append(_warn(j, "in_buffer",
                                  f"inside the {buffer_days} working days before its {k} {_fmt_when(j, when, k)}"))
    rest = [j for j in dated if j not in urgent]

    # 3. Hard pass. Deadline tasks go latest finish-by day first, each as late as possible.
    hard = sorted([j for j in rest if j.deadline], key=lambda j: (j.fb_dead, -j.index), reverse=True)
    for j in hard:
        got = _place_alap(cal, j, j.minutes, j.fb_dead, "dated", "deadline")
        if got < j.minutes:
            more = _place_asap(cal, j, j.minutes - got, j.fb_dead + timedelta(days=1), j.deadline,
                               "dated", "deadline")
            if more:
                warnings.append(_warn(j, "uses_buffer",
                                      f"uses the {buffer_days} buffer days before its deadline "
                                      f"{_fmt_day(j.deadline)}"))
            short = j.minutes - got - more
            if short > 0:
                warnings.append(_warn(j, "deadline_missed",
                                      f"needs {short} min more than fits before "
                                      f"{_fmt_when(j, j.deadline, 'deadline')}", short))

    # 4. Soft pass. Target dates go latest finish-by day first. A task that also has a deadline gives its
    # deadline slots back first, so moving it earlier can only help; what does not fit before the target
    # goes back as early as possible after it, which those freed slots always cover.
    soft = sorted([j for j in rest if j.target], key=lambda j: (j.fb_tgt, -j.index), reverse=True)
    for j in soft:
        need = j.minutes
        if j.deadline:
            for x in j.slots:
                cal.release(x["day"], x["s"], x["e"])
            need = j.placed
            j.slots = []
        got = _place_alap(cal, j, need, j.fb_tgt, "dated", "target")
        short = need - got
        if short > 0:
            warnings.append(_warn(j, "target_missed",
                                  f"needs {short} min more than fits {buffer_days} working days before "
                                  f"its target {_fmt_day(j.target)}", short))
            last = j.deadline if j.deadline else cal.last_day
            _place_asap(cal, j, short, j.fb_tgt + timedelta(days=1), last, "dated",
                        "deadline" if j.deadline else "target")

    # 5. Ranked pass. Undated tasks fill the plan day in list order.
    for j in ranked:
        j.add(day, cal.take_asap(day, j.minutes), "ranked", None)

    # 6. Ahead pass. Free time left today pulls dated work forward from its latest reserved day.
    for j in sorted(dated, key=lambda j: (j.min_fb, j.deadline is None, j.index)):
        while cal.total(day, j.until(day)) > 0:
            future = [x for x in j.slots if x["day"] > day]
            if not future:
                break
            latest = max(x["day"] for x in future)
            seg = min((x for x in future if x["day"] == latest), key=lambda x: x["s"])
            n = min(seg["e"] - seg["s"], cal.total(day, j.until(day)))
            cal.release(latest, seg["s"], seg["s"] + n)
            seg["s"] += n
            if seg["s"] >= seg["e"]:
                j.slots.remove(seg)
            j.add(day, cal.take_asap(day, n, j.until(day)), "ahead", j.main_kind)

    # 7. Blocks for the plan day, merged when the same task runs on; outlook for later days.
    raw = []
    for j in jobs:
        for x in j.slots:
            if x["day"] == day:
                raw.append((x["s"], x["e"], j, x["kind"], x["date_kind"]))
    raw.sort(key=lambda r: r[0])
    merged = []
    for s, e, j, kind, dk in raw:
        if merged and merged[-1][2] is j and merged[-1][1] == s:
            prev = merged[-1]
            if _KIND_RANK[kind] < _KIND_RANK[prev[3]]:
                prev[3], prev[4] = kind, dk
            prev[1] = e
        else:
            merged.append([s, e, j, kind, dk])
    done_so_far = {}
    blocks = []
    for s, e, j, kind, dk in merged:
        done_so_far[j.task.id] = done_so_far.get(j.task.id, 0) + (e - s)
        date_text, time_text = None, None
        if dk == "deadline":
            date_text = j.task.deadline
            time_text = j.task.deadline_time if j.cut is not None else None
        elif dk == "target":
            date_text = j.task.target
        blocks.append({"task_id": j.task.id, "name": j.task.name, "start": _hhmm(s), "end": _hhmm(e),
                       "minutes": e - s, "kind": kind, "date": date_text, "date_kind": dk, "time": time_text,
                       "left_after_min": max(0, j.minutes - done_so_far[j.task.id]),
                       "repeat": bool(j.task.repeat)})

    per_day = {}
    for j in jobs:
        for x in j.slots:
            if x["day"] > day:
                key = (x["day"], j.task.id)
                first = per_day.get(key, (x["s"], 0))
                per_day[key] = (min(first[0], x["s"]), first[1] + x["e"] - x["s"])
    outlook = [{"task_id": tid, "day": d.isoformat(), "minutes": mins}
               for (d, tid), (first, mins) in sorted(per_day.items(), key=lambda kv: (kv[0][0], kv[1][0]))]

    return {
        "version": PLAN_VERSION, "date": day.isoformat(), "mode": None, "generated_at": None,
        "generated_by": None, "tz": cfg.get("tz"), "start": _hhmm(first_free),
        "capacity_min": capacity, "free_min": cal.total(day),
        "blocks": blocks, "outlook": outlook, "warnings": warnings,
    }


# ---------------------------------------------------------------- from a state.json document

def _tz(name: str):
    if ZoneInfo is None:
        raise RuntimeError("zoneinfo is not available; Python 3.9 or newer is needed")
    return ZoneInfo(name)


def plan_from_doc(doc: dict, now_utc: datetime, mode: str, existing_plan: Optional[dict] = None,
                  by: str = "desktop") -> Optional[dict]:
    """The day plan for a state.json document, or None when "auto" decides to skip.
    "auto": before 7 pm local, or when `existing_plan` is already for tomorrow, skip; else plan tomorrow.
    "now": before 7 pm plan the rest of today from now (rounded up to 5 min); else plan tomorrow.
    "tomorrow": plan tomorrow."""
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    state = fc.doc_to_state(doc)
    cfg = _cfg_with_defaults(state.planner)
    now_local = now_utc.astimezone(_tz(cfg["tz"])).replace(tzinfo=None)
    today = now_local.date()
    tomorrow = today + timedelta(days=1)
    evening = now_local.hour >= EVENING_HOUR

    if mode == "auto":
        if not evening:
            return None
        if existing_plan and existing_plan.get("date") == tomorrow.isoformat():
            return None
        day, start = tomorrow, None
    elif mode == "now":
        if evening:
            day, start = tomorrow, None
        else:
            day, start = today, _ceil5(now_local).time()
    elif mode == "tomorrow":
        day, start = tomorrow, None
    else:
        raise ValueError(f"unknown mode {mode!r}")

    plan = plan_day(state, day, start, cfg, now_local=now_local)
    plan["mode"] = mode
    plan["generated_at"] = _iso_ms(now_utc)
    plan["generated_by"] = by
    return plan


# ---------------------------------------------------------------- command line

def _parse_now(s: str) -> datetime:
    s = s.strip()
    if s.endswith("Z") or s.endswith("z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _read_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Plan one day of the Focus Tool list.")
    ap.add_argument("--state", required=True, help="state.json to read")
    ap.add_argument("--out", required=True, help="day-plan.json to write (also read as the existing plan)")
    ap.add_argument("--mode", choices=("auto", "now", "tomorrow"), default="auto")
    ap.add_argument("--now", help="the current time in UTC, for example 2026-10-13T00:07:00Z")
    ap.add_argument("--by", choices=("actions", "desktop"), default="desktop")
    args = ap.parse_args(argv)

    now_utc = _parse_now(args.now) if args.now else datetime.now(timezone.utc)
    doc = json.loads(Path(args.state).read_text(encoding="utf-8"))
    out = Path(args.out)
    existing = _read_json(out) if out.exists() else None
    plan = plan_from_doc(doc, now_utc, args.mode, existing, by=args.by)
    if plan is None:
        print("planner: nothing to do now; the plan file was left as it is")
        return 0
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, out)
    print(f"planner: wrote the plan for {plan['date']} ({plan['mode']}) to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
