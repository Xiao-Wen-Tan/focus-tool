"""Tests for planner.py, the day planner. Every test fixes the dates and the clock explicitly.

Plan day in most tests is Tuesday 2026-10-13. Weekday hours are 09:00-12:00 and 13:30-18:00 (450 min),
weekend hours 10:00-12:30 (150 min).
"""
import copy
import json
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import focus_core as fc  # noqa: E402
import planner  # noqa: E402

TUE = date(2026, 10, 13)
CFG = copy.deepcopy(fc.DEFAULT_PLANNER)


def T(name, est, **kw):
    """A task with a readable id, so tests can refer to it."""
    return fc.Task(name, est, id=kw.pop("id", name.lower().replace(" ", "-")), **kw)


def state_of(*tasks, timer=None):
    return fc.State(plan=fc.Plan(list(tasks)), current_index=0, timer=timer or fc.TimerState(),
                    planner=copy.deepcopy(CFG))


def rows(plan):
    return [(b["start"], b["end"], b["name"], b["kind"]) for b in plan["blocks"]]


def kinds(plan):
    return sorted((w["name"], w["kind"]) for w in plan["warnings"])


def outlook(plan):
    return [(o["task_id"], o["day"], o["minutes"]) for o in plan["outlook"]]


def utc(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc)


# ---------- the worked example from the contract

def test_worked_example_exact():
    st = state_of(T("D", 600, deadline="2026-10-16"), T("R1", 5), T("R2", 45))
    p = planner.plan_day(st, TUE, None, CFG)
    assert p["date"] == "2026-10-13"
    assert p["start"] == "09:00"
    assert p["capacity_min"] == 450 and p["free_min"] == 0
    assert rows(p) == [("09:00", "09:05", "R1", "ranked"), ("09:05", "09:50", "R2", "ranked"),
                       ("09:50", "12:00", "D", "ahead"), ("13:30", "18:00", "D", "dated")]
    assert [b["minutes"] for b in p["blocks"]] == [5, 45, 130, 270]
    assert sum(b["minutes"] for b in p["blocks"]) == 450
    assert [b["left_after_min"] for b in p["blocks"]] == [0, 0, 470, 200]
    d_block = p["blocks"][3]
    assert d_block["date"] == "2026-10-16" and d_block["date_kind"] == "deadline" and d_block["repeat"] is False
    assert p["blocks"][0]["date"] is None and p["blocks"][0]["date_kind"] is None
    assert outlook(p) == [("d", "2026-10-14", 200)]
    assert p["warnings"] == []
    assert p["version"] == 1 and p["tz"] == "America/Chicago"


# ---------- urgent and overdue

def test_urgent_and_overdue_tasks_come_first_with_warnings():
    st = state_of(T("Rank", 60),
                  T("Late target", 60, target="2026-10-09"),
                  T("Soon", 60, deadline="2026-10-14"),
                  T("Past", 60, deadline="2026-10-12"))
    p = planner.plan_day(st, TUE, None, CFG)
    assert rows(p)[:4] == [("09:00", "10:00", "Past", "urgent"), ("10:00", "11:00", "Soon", "urgent"),
                           ("11:00", "12:00", "Late target", "urgent"), ("13:30", "14:30", "Rank", "ranked")]
    assert kinds(p) == [("Late target", "overdue"), ("Past", "overdue"), ("Soon", "in_buffer")]
    assert p["blocks"][2]["date_kind"] == "target"


def test_urgent_work_overflows_into_later_days():
    st = state_of(T("Big", 600, deadline="2026-10-14"))
    p = planner.plan_day(st, TUE, None, CFG)
    assert rows(p) == [("09:00", "12:00", "Big", "urgent"), ("13:30", "18:00", "Big", "urgent")]
    assert outlook(p) == [("big", "2026-10-14", 150)]


# ---------- dated work

def test_deadline_beats_target():
    # Both finish-by dates are Wed 10/14. The deadline task gets Wednesday, the target task Tuesday.
    st = state_of(T("Aim", 450, target="2026-10-16"), T("Hard", 450, deadline="2026-10-16"))
    p = planner.plan_day(st, TUE, None, CFG)
    assert {b["name"] for b in p["blocks"]} == {"Aim"}
    assert outlook(p) == [("hard", "2026-10-14", 450)]
    assert p["warnings"] == []


def test_task_with_both_dates_keeps_its_deadline_slots():
    # Task Both has deadline Mon 10/19 (finish by Thu 10/15) and target Thu 10/15 (finish by Tue 10/13).
    # Task Other has only a target (finish by Thu 10/15). It may not take the slots Both needs.
    st = state_of(T("Both", 900, deadline="2026-10-19", target="2026-10-15"),
                  T("Other", 450, target="2026-10-19"))
    p = planner.plan_day(st, TUE, None, CFG)
    assert rows(p) == [("09:00", "12:00", "Other", "dated"), ("13:30", "18:00", "Other", "dated")]
    assert outlook(p) == [("both", "2026-10-14", 450), ("both", "2026-10-15", 450)]
    assert kinds(p) == [("Both", "target_missed")]


def test_target_later_than_deadline_is_ignored():
    st = state_of(T("X", 30, deadline="2026-10-16", target="2026-10-30"))
    p = planner.plan_day(st, TUE, None, CFG)
    assert all(b["date_kind"] == "deadline" for b in p["blocks"])
    assert p["warnings"] == []


def test_weekend_capacity_is_150_minutes():
    p = planner.plan_day(state_of(T("A", 200)), date(2026, 10, 17), None, CFG)
    assert p["capacity_min"] == 150 and p["start"] == "10:00"
    assert rows(p) == [("10:00", "12:30", "A", "ranked")]
    assert p["blocks"][0]["left_after_min"] == 50


def test_task_longer_than_a_day_splits_across_days():
    # 1000 min with finish by Thu 10/15. Thu 450 and Wed 450 are reserved backwards and 100 on Tue,
    # then the rest of Tuesday pulls 350 minutes forward from Thursday.
    st = state_of(T("Long", 1000, deadline="2026-10-19"))
    p = planner.plan_day(st, TUE, None, CFG)
    assert sum(b["minutes"] for b in p["blocks"]) == 450
    assert outlook(p) == [("long", "2026-10-14", 450), ("long", "2026-10-15", 100)]


def test_deadline_missed_and_buffer_warnings():
    # Deadline Thu 10/15, finish by Tue 10/13. 1500 min but only 1350 fit by Thursday.
    st = state_of(T("Huge", 1500, deadline="2026-10-15"))
    p = planner.plan_day(st, TUE, None, CFG)
    assert kinds(p) == [("Huge", "deadline_missed"), ("Huge", "uses_buffer")]
    miss = [w for w in p["warnings"] if w["kind"] == "deadline_missed"][0]
    assert miss["message"] == "needs 150 min more than fits before Thu 10/15"
    assert miss["task_id"] == "huge"
    assert outlook(p) == [("huge", "2026-10-14", 450), ("huge", "2026-10-15", 450)]


def test_target_missed_puts_the_rest_after_the_finish_by_day():
    st = state_of(T("Aim", 600, target="2026-10-15"))
    p = planner.plan_day(st, TUE, None, CFG)
    assert kinds(p) == [("Aim", "target_missed")]
    assert "150 min" in p["warnings"][0]["message"]
    assert outlook(p) == [("aim", "2026-10-14", 150)]


# ---------- remaining time

def test_spent_time_and_running_timer_reduce_remaining_minutes():
    now = datetime(2026, 10, 13, 10, 0)
    timer = fc.TimerState()
    timer.start("B", 30, now - timedelta(minutes=10), spent_s=300, task_id="b")
    a = T("A", 60, spent_s=600)
    b = T("B", 30)
    assert planner.remaining_minutes(a, timer, now) == 50
    assert planner.remaining_minutes(b, timer, now) == 15
    timer.pause(now - timedelta(minutes=5))  # paused after 5 more minutes, so 10 used in total
    assert planner.remaining_minutes(b, timer, now) == 20
    assert planner.remaining_minutes(T("C", 1, spent_s=61), None, now) == 0
    assert planner.remaining_minutes(T("E", 10, spent_s=30), None, now) == 10  # partial minutes round up


def test_finished_tasks_are_skipped():
    st = state_of(T("Used up", 10, spent_s=900), T("Next", 15))
    p = planner.plan_day(st, date(2026, 10, 17), None, CFG)
    assert [b["name"] for b in p["blocks"]] == ["Next"]


# ---------- repeating tasks

def test_repeats_planned_in_rank_order_only_when_active():
    st = state_of(T("Walk", 20, repeat={"kind": "daily"}),
                  T("Mondays", 30, repeat={"kind": "days", "days": ["mon"]}),
                  T("Plain", 10),
                  T("Done today", 15, repeat={"kind": "weekdays"}, last_done="2026-10-13"),
                  T("Odd days", 25, repeat={"kind": "every", "every": 2, "anchor": "2026-10-11"}))
    p = planner.plan_day(st, TUE, None, CFG)
    assert rows(p) == [("09:00", "09:20", "Walk", "ranked"), ("09:20", "09:30", "Plain", "ranked"),
                       ("09:30", "09:55", "Odd days", "ranked")]
    assert [b["repeat"] for b in p["blocks"]] == [True, False, True]


def test_repeats_on_later_days_reduce_capacity_for_dated_work():
    st = state_of(T("Walk", 30, repeat={"kind": "daily"}), T("D", 450, deadline="2026-10-16"))
    p = planner.plan_day(st, TUE, None, CFG)
    assert rows(p) == [("09:00", "09:30", "Walk", "ranked"), ("09:30", "12:00", "D", "ahead"),
                       ("13:30", "18:00", "D", "dated")]
    assert outlook(p) == [("d", "2026-10-14", 30)]
    assert p["outlook"] and all(o["task_id"] != "walk" for o in p["outlook"])


# ---------- modes

def doc_of(*tasks, timer=None):
    return fc.state_to_doc(state_of(*tasks, timer=timer))


def test_now_mode_starts_at_next_five_minutes():
    p = planner.plan_from_doc(doc_of(T("A", 600)), utc("2026-10-13T19:12"), "now", None)
    assert p["date"] == "2026-10-13" and p["mode"] == "now" and p["start"] == "14:15"
    assert p["capacity_min"] == 225
    assert rows(p) == [("14:15", "18:00", "A", "ranked")]
    assert p["generated_at"] == "2026-10-13T19:12:00.000Z"


def test_now_mode_after_seven_pm_plans_tomorrow():
    p = planner.plan_from_doc(doc_of(T("A", 30)), utc("2026-10-14T00:30"), "now", None)
    assert p["date"] == "2026-10-14" and p["start"] == "09:00"


def test_tomorrow_mode_plans_tomorrow_any_time():
    p = planner.plan_from_doc(doc_of(T("A", 30)), utc("2026-10-13T15:00"), "tomorrow", None)
    assert p["date"] == "2026-10-14" and p["mode"] == "tomorrow"


def test_auto_mode_in_summer_time():
    doc = doc_of(T("A", 30))
    p = planner.plan_from_doc(doc, utc("2026-10-13T00:07"), "auto", None)  # 19:07 CDT on 10/12
    assert p is not None and p["date"] == "2026-10-13" and p["mode"] == "auto"
    assert planner.plan_from_doc(doc, utc("2026-10-13T01:07"), "auto", p) is None  # already planned
    older = dict(p, date="2026-10-12")
    assert planner.plan_from_doc(doc, utc("2026-10-13T01:07"), "auto", older)["date"] == "2026-10-13"


def test_auto_mode_in_winter_time():
    doc = doc_of(T("A", 30))
    assert planner.plan_from_doc(doc, utc("2026-01-13T00:07"), "auto", None) is None  # 18:07 CST
    p = planner.plan_from_doc(doc, utc("2026-01-13T01:07"), "auto", None)  # 19:07 CST
    assert p["date"] == "2026-01-13"


def test_running_timer_counts_in_now_mode():
    timer = fc.TimerState()
    # Started at 13:00 local (naive local time, as state.json stores it); now 14:12 local.
    timer.start("A", 120, datetime(2026, 10, 13, 13, 0), task_id="a")
    p = planner.plan_from_doc(doc_of(T("A", 120), timer=timer), utc("2026-10-13T19:12"), "now", None)
    assert rows(p) == [("14:15", "15:03", "A", "ranked")]


# ---------- command line

def test_cli_writes_the_plan(tmp_path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps(doc_of(T("A", 30))), encoding="utf-8")
    out = tmp_path / "day-plan.json"
    rc = planner.main(["--state", str(state), "--out", str(out), "--mode", "auto",
                       "--now", "2026-10-13T00:07:00Z", "--by", "actions"])
    assert rc == 0
    p = json.loads(out.read_text(encoding="utf-8"))
    assert p["date"] == "2026-10-13" and p["generated_by"] == "actions" and p["mode"] == "auto"
    assert rows(p) == [("09:00", "09:30", "A", "ranked")]


def test_cli_skip_leaves_the_file_untouched(tmp_path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps(doc_of(T("A", 30))), encoding="utf-8")
    out = tmp_path / "day-plan.json"
    text = json.dumps({"version": 1, "date": "2026-10-13", "blocks": []})
    out.write_text(text, encoding="utf-8")
    rc = planner.main(["--state", str(state), "--out", str(out), "--mode", "auto",
                       "--now", "2026-10-13T01:07:00Z"])
    assert rc == 0
    assert out.read_text(encoding="utf-8") == text
    rc = planner.main(["--state", str(state), "--out", str(out), "--mode", "auto",
                       "--now", "2026-10-12T22:00:00Z"])  # 17:00 CDT, too early
    assert rc == 0 and out.read_text(encoding="utf-8") == text
