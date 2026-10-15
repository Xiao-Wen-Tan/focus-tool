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


# ---------- the worked example

def test_worked_example_exact():
    # A meeting at 14:00, a form due today, a report to aim for by Friday (Low), and two undated tasks.
    st = state_of(T("Meeting", 60, deadline="2026-10-13", deadline_time="14:00"),
                  T("Form", 30, deadline="2026-10-13"),
                  T("Report", 120, target="2026-10-16", priority="low"),
                  T("Read", 60),
                  T("Email", 60, priority="high"))
    p = planner.plan_day(st, TUE, None, CFG)
    assert p["date"] == "2026-10-13"
    assert p["start"] == "09:00"
    assert p["capacity_min"] == 450 and p["free_min"] == 120
    assert rows(p) == [("09:00", "09:30", "Form", "dated"), ("09:30", "11:30", "Report", "ahead"),
                       ("11:30", "12:00", "Email", "ranked"), ("13:30", "14:00", "Email", "ranked"),
                       ("14:00", "15:00", "Meeting", "fixed"), ("15:00", "16:00", "Read", "ranked")]
    meeting = p["blocks"][4]
    assert meeting["date"] == "2026-10-13" and meeting["date_kind"] == "deadline" and meeting["time"] == "14:00"
    assert p["blocks"][1]["date_kind"] == "target" and p["blocks"][1]["date"] == "2026-10-16"
    assert p["blocks"][2]["date"] is None and p["blocks"][2]["date_kind"] is None
    assert [b["left_after_min"] for b in p["blocks"]] == [0, 0, 30, 0, 0, 0]
    assert p["outlook"] == [] and p["warnings"] == []
    assert p["version"] == 1 and p["tz"] == "America/Chicago"


# ---------- urgent and overdue

def test_overdue_and_urgent_tasks_come_first_with_warnings():
    st = state_of(T("Rank", 60),
                  T("Late target", 60, target="2026-10-09"),
                  T("Soon", 60, deadline="2026-10-14"),
                  T("Past", 60, deadline="2026-10-12"))
    p = planner.plan_day(st, TUE, None, CFG)
    assert rows(p) == [("09:00", "10:00", "Past", "urgent"), ("10:00", "11:00", "Late target", "urgent"),
                       ("11:00", "12:00", "Rank", "ranked")]
    assert kinds(p) == [("Late target", "overdue"), ("Past", "overdue")]
    assert p["warnings"][0]["message"] == "its date Mon 10/12 has passed"
    assert p["blocks"][1]["date_kind"] == "target"
    assert outlook(p) == [("soon", "2026-10-14", 60)]  # a hard date tomorrow waits for tomorrow


def test_aim_date_inside_the_buffer_is_urgent():
    st = state_of(T("Rank", 60, priority="high"), T("Soon", 60, target="2026-10-14"))
    p = planner.plan_day(st, TUE, None, CFG)
    assert rows(p) == [("09:00", "10:00", "Soon", "urgent"), ("10:00", "11:00", "Rank", "ranked")]
    assert kinds(p) == [("Soon", "in_buffer")]
    assert p["warnings"][0]["message"] == "inside the 2 working days before its aim date Wed 10/14"


def test_urgent_work_overflows_into_later_days():
    st = state_of(T("Big", 600, target="2026-10-14"))
    p = planner.plan_day(st, TUE, None, CFG)
    assert rows(p) == [("09:00", "12:00", "Big", "urgent"), ("13:30", "18:00", "Big", "urgent")]
    assert outlook(p) == [("big", "2026-10-14", 150)]


# ---------- hard dates

def test_hard_date_is_booked_on_its_day_not_before():
    st = state_of(T("Aim", 450, target="2026-10-16"), T("Hard", 450, deadline="2026-10-16"))
    p = planner.plan_day(st, TUE, None, CFG)
    assert {b["name"] for b in p["blocks"]} == {"Aim"}
    assert outlook(p) == [("hard", "2026-10-16", 450)]
    assert p["warnings"] == []


def test_hard_date_task_too_big_for_its_day_warns():
    st = state_of(T("Huge", 600, deadline="2026-10-14"))
    p = planner.plan_day(st, TUE, None, CFG)
    assert p["blocks"] == []
    assert outlook(p) == [("huge", "2026-10-14", 450)]
    assert kinds(p) == [("Huge", "deadline_missed")]
    assert p["warnings"][0]["message"] == "needs 150 min more than fits on its date Wed 10/14"
    assert p["warnings"][0]["minutes"] == 150


def test_hard_dates_on_the_same_day_go_by_priority_then_list_order():
    st = state_of(T("A", 30, deadline="2026-10-13"), T("B", 30, deadline="2026-10-13", priority="high"),
                  T("C", 30, deadline="2026-10-13"))
    p = planner.plan_day(st, TUE, None, CFG)
    assert [r[2] for r in rows(p)] == ["B", "A", "C"]


def test_hard_date_wins_over_an_aim_date():
    # Both has a hard date Mon 10/19 and an aim date Thu 10/15: it is booked on Monday only.
    st = state_of(T("Both", 900, deadline="2026-10-19", target="2026-10-15"),
                  T("Other", 450, target="2026-10-19"))
    p = planner.plan_day(st, TUE, None, CFG)
    assert rows(p) == [("09:00", "12:00", "Other", "ahead"), ("13:30", "18:00", "Other", "ahead")]
    assert outlook(p) == [("both", "2026-10-19", 450)]
    assert kinds(p) == [("Both", "deadline_missed")]


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
    st = state_of(T("Long", 1000, target="2026-10-19"))
    p = planner.plan_day(st, TUE, None, CFG)
    assert sum(b["minutes"] for b in p["blocks"]) == 450
    assert outlook(p) == [("long", "2026-10-14", 450), ("long", "2026-10-15", 100)]


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

def test_repeats_planned_before_undated_tasks_only_when_active():
    st = state_of(T("Walk", 20, repeat={"kind": "daily"}),
                  T("Mondays", 30, repeat={"kind": "days", "days": ["mon"]}),
                  T("Plain", 10, priority="high"),
                  T("Done today", 15, repeat={"kind": "weekdays"}, last_done="2026-10-13"),
                  T("Odd days", 25, repeat={"kind": "every", "every": 2, "anchor": "2026-10-11"}))
    p = planner.plan_day(st, TUE, None, CFG)
    assert rows(p) == [("09:00", "09:20", "Walk", "ranked"), ("09:20", "09:45", "Odd days", "ranked"),
                       ("09:45", "09:55", "Plain", "ranked")]
    assert [b["repeat"] for b in p["blocks"]] == [True, True, False]


def test_repeats_on_later_days_reduce_capacity_for_dated_work():
    st = state_of(T("Walk", 30, repeat={"kind": "daily"}), T("D", 450, target="2026-10-16"))
    p = planner.plan_day(st, TUE, None, CFG)
    assert rows(p) == [("09:00", "09:30", "Walk", "ranked"), ("09:30", "12:00", "D", "ahead"),
                       ("13:30", "18:00", "D", "dated")]
    assert outlook(p) == [("d", "2026-10-14", 30)]
    assert p["outlook"] and all(o["task_id"] != "walk" for o in p["outlook"])


def test_repeat_with_deadline_is_planned_as_a_repeat_on_and_before_its_deadline():
    # The deadline is the plan day itself. As dated work it would be urgent with an "in buffer" warning;
    # as a repeat it is an ordinary ranked block with no date and no warning.
    st = state_of(T("Walk", 20, repeat={"kind": "daily"}, deadline="2026-10-13"), T("Plain", 10))
    p = planner.plan_day(st, TUE, None, CFG)
    assert rows(p) == [("09:00", "09:20", "Walk", "ranked"), ("09:20", "09:30", "Plain", "ranked")]
    walk = p["blocks"][0]
    assert walk["repeat"] is True and walk["date"] is None and walk["date_kind"] is None
    assert p["warnings"] == [] and p["outlook"] == []


def test_repeat_with_deadline_is_not_planned_after_its_deadline():
    st = state_of(T("Walk", 20, repeat={"kind": "daily"}, deadline="2026-10-12"), T("Plain", 10))
    p = planner.plan_day(st, TUE, None, CFG)
    assert rows(p) == [("09:00", "09:10", "Plain", "ranked")]
    assert p["warnings"] == []


def test_repeat_with_deadline_holds_time_on_later_days_only_up_to_its_deadline():
    walk = T("Walk", 30, repeat={"kind": "daily"}, deadline="2026-10-15")
    cal = planner._Calendar(CFG, TUE, None, [(walk, 30)])
    assert cal.total(date(2026, 10, 14)) == 420  # Wednesday: 450 minutes less the walk
    assert cal.total(date(2026, 10, 15)) == 420  # Thursday, the deadline: still held
    assert cal.total(date(2026, 10, 16)) == 450  # Friday: past the deadline, the full day is free


def test_repeat_with_deadline_does_not_reserve_dated_slots():
    st = state_of(T("Walk", 30, repeat={"kind": "daily"}, deadline="2026-10-20"),
                  T("D", 450, deadline="2026-10-16"))
    p = planner.plan_day(st, TUE, None, CFG)
    assert all(b["kind"] == "ranked" for b in p["blocks"] if b["task_id"] == "walk")
    assert all(o["task_id"] != "walk" for o in p["outlook"])


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


# ---------- fixed time (a hard date with a time of day)

def test_fixed_time_is_booked_at_exactly_its_time():
    st = state_of(T("Plain", 600), T("Meeting", 60, deadline="2026-10-13", deadline_time="14:00"))
    p = planner.plan_day(st, TUE, None, CFG)
    assert rows(p) == [("09:00", "12:00", "Plain", "ranked"), ("13:30", "14:00", "Plain", "ranked"),
                       ("14:00", "15:00", "Meeting", "fixed"), ("15:00", "18:00", "Plain", "ranked")]
    assert p["blocks"][2]["time"] == "14:00" and p["blocks"][2]["date_kind"] == "deadline"
    assert p["warnings"] == []


def test_fixed_time_outside_working_hours_is_booked_anyway():
    st = state_of(T("Dinner", 90, deadline="2026-10-13", deadline_time="19:00"), T("Plain", 30))
    p = planner.plan_day(st, TUE, None, CFG)
    assert rows(p) == [("09:00", "09:30", "Plain", "ranked"), ("19:00", "20:30", "Dinner", "fixed")]
    assert p["capacity_min"] == 450 and p["free_min"] == 420
    assert p["warnings"] == []


def test_fixed_time_on_a_later_day_holds_that_time():
    st = state_of(T("Talk", 60, deadline="2026-10-15", deadline_time="10:00"))
    p = planner.plan_day(st, TUE, None, CFG)
    assert p["blocks"] == [] and outlook(p) == [("talk", "2026-10-15", 60)]


def test_two_fixed_times_that_overlap_are_both_kept_with_a_warning():
    st = state_of(T("A", 60, deadline="2026-10-13", deadline_time="14:00"),
                  T("B", 60, deadline="2026-10-13", deadline_time="14:30"))
    p = planner.plan_day(st, TUE, None, CFG)
    assert rows(p) == [("14:00", "15:00", "A", "fixed"), ("14:30", "15:30", "B", "fixed")]
    assert kinds(p) == [("B", "overlap")]
    assert p["warnings"][0]["message"] == "overlaps A at Tue 10/13 2:30pm"


def test_fixed_time_already_over_today_is_overdue():
    st = state_of(T("Form", 60, deadline="2026-10-13", deadline_time="10:00"))
    p = planner.plan_day(st, TUE, time(11, 0), CFG)
    assert rows(p) == [("11:00", "12:00", "Form", "urgent")]
    assert kinds(p) == [("Form", "overdue")]
    assert p["warnings"][0]["message"] == "its date Tue 10/13 10:00am has passed"


def test_fixed_time_under_way_books_what_is_left_of_it():
    st = state_of(T("Form", 60, deadline="2026-10-13", deadline_time="10:00"))
    p = planner.plan_day(st, TUE, time(10, 30), CFG)
    assert rows(p) == [("10:30", "11:00", "Form", "fixed")]
    assert p["warnings"] == []


def test_hard_date_without_time_is_not_overdue_until_the_day_ends():
    st = state_of(T("Form", 60, deadline="2026-10-13"))
    p = planner.plan_day(st, TUE, time(17, 0), CFG)
    assert rows(p) == [("17:00", "18:00", "Form", "dated")]
    assert p["warnings"] == [] and p["blocks"][0]["time"] is None


def test_deadline_time_that_is_not_a_real_time_counts_as_day_end():
    cfg = copy.deepcopy(CFG)
    cfg["buffer_business_days"] = 0
    st = state_of(T("Talk", 300, deadline="2026-10-13", deadline_time="noon"))
    p = planner.plan_day(st, TUE, None, cfg)
    assert sum(b["minutes"] for b in p["blocks"]) == 300
    assert p["warnings"] == [] and p["blocks"][0]["time"] is None


def test_calendar_block_takes_time_out_whether_free_or_not():
    cal = planner._Calendar(CFG, TUE, None, [])
    cal.block(TUE, 11 * 60, 14 * 60)
    assert cal.day(TUE) == [[9 * 60, 11 * 60], [14 * 60, 18 * 60]]
    cal.block(TUE, 19 * 60, 20 * 60)  # outside working hours: nothing changes
    assert cal.total(TUE) == 360


# ---------- priority tags

def test_undated_tasks_go_high_then_medium_then_low_then_list_order():
    st = state_of(T("L", 10, priority="low"), T("M1", 10), T("H", 10, priority="high"), T("M2", 10))
    p = planner.plan_day(st, TUE, None, CFG)
    assert [r[2] for r in rows(p)] == ["H", "M1", "M2", "L"]


def test_aim_date_work_goes_before_undated_work_of_any_tag():
    st = state_of(T("Undated", 400, priority="high"), T("Aim", 100, target="2026-10-30", priority="low"))
    p = planner.plan_day(st, TUE, None, CFG)
    assert rows(p)[0] == ("09:00", "10:40", "Aim", "ahead")
    assert sum(b["minutes"] for b in p["blocks"] if b["name"] == "Undated") == 350


def test_aim_date_work_is_pulled_ahead_by_tag_then_list_order():
    st = state_of(T("A", 300, target="2026-10-30"), T("B", 300, target="2026-10-30", priority="high"))
    p = planner.plan_day(st, TUE, None, CFG)
    assert rows(p) == [("09:00", "12:00", "B", "ahead"), ("13:30", "15:30", "B", "ahead"),
                       ("15:30", "18:00", "A", "ahead")]


def test_unknown_priority_counts_as_medium():
    st = state_of(T("Odd", 10, priority="urgent!"), T("L", 10, priority="low"), T("M", 10))
    p = planner.plan_day(st, TUE, None, CFG)
    assert [r[2] for r in rows(p)] == ["Odd", "M", "L"]


def test_fmt_hm():
    assert [fc.fmt_hm(x) for x in ("00:05", "09:30", "12:00", "17:00", "bad")] == ["12:05am", "9:30am", "12:00pm",
                                                                                   "5:00pm", ""]
