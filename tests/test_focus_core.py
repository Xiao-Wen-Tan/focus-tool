"""Tests for focus_core, the pure-logic half of the Focus Tool (no Qt here)."""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import focus_core as fc  # noqa: E402


# ---------- parse_plan ----------

def test_parse_line_with_minutes_suffix():
    plan = fc.parse_plan("Draft intro section 45m", default_minutes=45)
    assert [(t.name, t.estimate_min) for t in plan.tasks] == [("Draft intro section", 45)]


def test_parse_line_without_duration_uses_default():
    plan = fc.parse_plan("Grade quiz", default_minutes=45)
    assert plan.tasks[0].estimate_min == 45


def test_parse_accepts_min_and_hours_suffixes():
    plan = fc.parse_plan("Reply to dean 10min\nRead paper 1.5h", default_minutes=45)
    assert [t.estimate_min for t in plan.tasks] == [10, 90]


def test_parse_ignores_blank_lines():
    plan = fc.parse_plan("\nA 5m\n\n  \nB\n", default_minutes=45)
    assert [t.name for t in plan.tasks] == ["A", "B"]








# ---------- TimerState ----------

T0 = datetime(2026, 9, 21, 10, 0, 0)


def test_timer_remaining_and_elapsed_after_tick():
    t = fc.TimerState()
    t.start("Draft", estimate_min=10, now=T0)
    t.tick(T0 + timedelta(minutes=3))
    assert t.elapsed_s == 180
    assert t.remaining_s == 420
    assert t.overtime_s == 0
    assert abs(t.fraction_remaining - 0.7) < 1e-9


def test_timer_overtime_counts_up_past_zero():
    t = fc.TimerState()
    t.start("Draft", estimate_min=10, now=T0)
    t.tick(T0 + timedelta(minutes=12))
    assert t.remaining_s == 0
    assert t.overtime_s == 120
    assert t.fraction_remaining == 0.0


def test_timer_add_minutes_during_overtime_returns_positive_remaining():
    t = fc.TimerState()
    t.start("Draft", estimate_min=10, now=T0)
    t.tick(T0 + timedelta(minutes=12))
    t.add_minutes(5)
    t.tick(T0 + timedelta(minutes=12))
    assert t.remaining_s == 180
    assert t.overtime_s == 0


def test_timer_checkpoints_fire_once_each():
    t = fc.TimerState(checkpoints=(0.5, 0.9, 1.0))
    t.start("Draft", estimate_min=10, now=T0)
    fired = []
    for m in [1, 5, 5.5, 9, 9.5, 10, 11]:
        fired += t.tick(T0 + timedelta(minutes=m))
    assert fired == [0.5, 0.9, 1.0]


def test_timer_pause_freezes_elapsed():
    t = fc.TimerState()
    t.start("Draft", estimate_min=10, now=T0)
    t.tick(T0 + timedelta(minutes=2))
    t.pause(T0 + timedelta(minutes=2))
    t.tick(T0 + timedelta(minutes=6))
    assert t.elapsed_s == 120
    t.resume(T0 + timedelta(minutes=6))
    t.tick(T0 + timedelta(minutes=7))
    assert t.elapsed_s == 180


def test_timer_done_returns_estimate_and_actual_minutes():
    t = fc.TimerState()
    t.start("Draft", estimate_min=10, now=T0)
    t.tick(T0 + timedelta(minutes=13))
    rec = t.done(T0 + timedelta(minutes=13))
    assert rec.task == "Draft"
    assert rec.estimate_min == 10
    assert rec.actual_min == 13
    assert not t.running


# ---------- state round trip ----------

def test_state_round_trip_preserves_plan_index_and_running_timer(tmp_path):
    plan = fc.parse_plan("A 10m\nB 20m", default_minutes=45)
    t = fc.TimerState()
    t.start("A", estimate_min=10, now=T0)
    t.add_minutes(5)
    path = tmp_path / "state.json"
    fc.save_state(path, plan=plan, current_index=0, timer=t, geometry={"x": 10, "y": 20, "w": 300, "h": 120})
    loaded = fc.load_state(path)
    assert [x.name for x in loaded.plan.tasks] == ["A", "B"]
    assert loaded.current_index == 0
    assert loaded.timer.running
    assert loaded.timer.task == "A"
    assert loaded.timer.estimate_min == 15
    assert loaded.geometry == {"x": 10, "y": 20, "w": 300, "h": 120}
    loaded.timer.tick(T0 + timedelta(minutes=1))
    assert loaded.timer.elapsed_s == 60


def test_load_state_missing_file_returns_empty_state(tmp_path):
    loaded = fc.load_state(tmp_path / "nope.json")
    assert loaded.plan.tasks == []
    assert loaded.current_index == 0
    assert not loaded.timer.running


# ---------- v1.1: details, reordering ----------

def test_parse_indented_lines_become_details_of_previous_task():
    text = "Draft intro 45m\n  read reviewer 2 first\n  then fix figure 1\nEmail 5m"
    plan = fc.parse_plan(text, default_minutes=45)
    assert [t.name for t in plan.tasks] == ["Draft intro", "Email"]
    assert plan.tasks[0].details == "read reviewer 2 first\nthen fix figure 1"
    assert plan.tasks[1].details == ""


def test_parse_dash_lines_become_details_too():
    plan = fc.parse_plan("Draft intro 45m\n- open the doc\nEmail", default_minutes=45)
    assert plan.tasks[0].details == "open the doc"


def test_details_survive_state_round_trip(tmp_path):
    plan = fc.parse_plan("A 10m\n  note for A", default_minutes=45)
    fc.save_state(tmp_path / "s.json", plan=plan, current_index=0, timer=fc.TimerState())
    assert fc.load_state(tmp_path / "s.json").plan.tasks[0].details == "note for A"


def test_move_task_reorders_in_place():
    plan = fc.parse_plan("A\nB\nC", default_minutes=45)
    fc.move_task(plan, 2, 0)
    assert [t.name for t in plan.tasks] == ["C", "A", "B"]
    fc.move_task(plan, 0, 2)
    assert [t.name for t in plan.tasks] == ["A", "B", "C"]


def test_move_task_out_of_range_is_ignored():
    plan = fc.parse_plan("A\nB", default_minutes=45)
    fc.move_task(plan, 1, 5)
    fc.move_task(plan, -1, 0)
    assert [t.name for t in plan.tasks] == ["A", "B"]


def test_next_prefix_is_just_a_task_now():
    plan = fc.parse_plan("next: Meeting 2:00pm", default_minutes=45)
    assert [t.name for t in plan.tasks] == ["next: Meeting 2:00pm"]
    assert not hasattr(plan, "next_event_at")


# ---------- dump file access ----------

def test_read_dumps_parses_timestamp_and_text(tmp_path):
    p = tmp_path / "dump.txt"
    p.write_text("2026-09-21 14:10  call the dean\n2026-09-21 14:12  buy milk\n", encoding="utf-8")
    dumps = fc.read_dumps(p)
    assert [(d.when, d.text) for d in dumps] == [("2026-09-21 14:10", "call the dean"), ("2026-09-21 14:12", "buy milk")]


def test_read_dumps_missing_file_is_empty(tmp_path):
    assert fc.read_dumps(tmp_path / "none.txt") == []


def test_remove_dump_rewrites_file_without_that_line(tmp_path):
    p = tmp_path / "dump.txt"
    p.write_text("2026-09-21 14:10  a\n2026-09-21 14:12  b\n2026-09-21 14:13  c\n", encoding="utf-8")
    fc.remove_dump(p, 1)
    assert [d.text for d in fc.read_dumps(p)] == ["a", "c"]


# ---------- v1.3: completed tasks persist, time-up never auto-completes ----------

def test_done_list_round_trips_through_state_file(tmp_path):
    plan = fc.parse_plan("B 20m", default_minutes=45)
    done = [fc.DoneTask("A", 10, 12, "2026-09-21 10:12", "note for A")]
    fc.save_state(tmp_path / "s.json", plan=plan, current_index=0, timer=fc.TimerState(), done=done)
    loaded = fc.load_state(tmp_path / "s.json")
    assert [(d.name, d.estimate_min, d.actual_min, d.finished_at, d.details) for d in loaded.done] == \
           [("A", 10, 12, "2026-09-21 10:12", "note for A")]


def test_load_state_without_done_field_gives_empty_done(tmp_path):
    plan = fc.parse_plan("A 10m", default_minutes=45)
    fc.save_state(tmp_path / "s.json", plan=plan, current_index=0, timer=fc.TimerState())
    assert fc.load_state(tmp_path / "s.json").done == []


def test_done_to_task_restores_name_estimate_and_details():
    d = fc.DoneTask("A", 10, 12, "2026-09-21 10:12", "note")
    t = fc.done_to_task(d)
    assert (t.name, t.estimate_min, t.details) == ("A", 10, "note")


def test_timer_keeps_running_after_time_is_up():
    t = fc.TimerState()
    t.start("Draft", estimate_min=10, now=T0)
    t.tick(T0 + timedelta(minutes=15))
    assert t.running
    assert t.task == "Draft"
    assert t.overtime_s == 300


# ---------- running task anywhere in the list (v1.4) ----------

def test_list_end_counts_running_task_by_remaining_time_wherever_it_sits():
    now = datetime(2026, 9, 22, 10, 0)
    a, b, c = fc.Task("A", 30), fc.Task("B", 20), fc.Task("C", 10)
    timer = fc.TimerState()
    timer.start("B", 20, now)
    timer.tick(now + timedelta(minutes=5))  # B has 15 min left
    end = fc.list_end([a, b, c], b, timer, now)
    assert end == now + timedelta(minutes=30 + 15 + 10)


def test_list_end_when_idle_sums_all_estimates():
    now = datetime(2026, 9, 22, 10, 0)
    tasks = [fc.Task("A", 30), fc.Task("B", 20)]
    assert fc.list_end(tasks, None, fc.TimerState(), now) == now + timedelta(minutes=50)


def test_find_active_prefers_saved_index_then_falls_back_to_name():
    tasks = [fc.Task("A", 5), fc.Task("B", 5), fc.Task("B", 5)]
    assert fc.find_active(tasks, 2, "B") is tasks[2]
    assert fc.find_active(tasks, 0, "B") is tasks[1]  # index moved, name still found
    assert fc.find_active(tasks, 0, "Z") is None


# ---------- v1.5: one list shared through Box ----------

def test_parse_state_rejects_a_half_written_file():
    with pytest.raises(ValueError):
        fc.parse_state('{"version": 2, "plan": {"tas')


def test_save_state_returns_the_text_it_wrote(tmp_path):
    p = tmp_path / "s.json"
    text = fc.save_state(p, fc.parse_plan("A 10m", 45), 0, fc.TimerState())
    assert p.read_text(encoding="utf-8") == text


def test_merge_states_adds_only_what_the_shared_list_is_missing():
    shared = fc.State(fc.parse_plan("A 10m\nB 20m", 45), 0, fc.TimerState(),
                      done=[fc.DoneTask("D1", 5, 5, "2026-10-01 09:00")])
    old = fc.State(fc.parse_plan("B 25m\nC 5m", 45), 0, fc.TimerState(),
                   done=[fc.DoneTask("D2", 5, 6, "2026-09-30 17:00"), fc.DoneTask("D1", 5, 5, "2026-10-01 09:00")])
    merged = fc.merge_states(shared, old)
    assert [(t.name, t.estimate_min) for t in merged.plan.tasks] == [("A", 10), ("B", 20), ("C", 5)]
    assert [d.name for d in merged.done] == ["D2", "D1"]  # oldest first, no duplicate


def test_merge_states_keeps_a_running_timer_from_either_side():
    old_timer = fc.TimerState()
    old_timer.start("C", 5, T0)
    old = fc.State(fc.parse_plan("C 5m", 45), 0, old_timer)
    merged = fc.merge_states(fc.State(fc.parse_plan("A 10m", 45), 0, fc.TimerState()), old)
    assert merged.timer.running and merged.timer.task == "C"
    assert fc.find_active(merged.plan.tasks, merged.current_index, "C") is merged.plan.tasks[1]
    shared_timer = fc.TimerState()
    shared_timer.start("A", 10, T0)
    merged = fc.merge_states(fc.State(fc.parse_plan("A 10m", 45), 0, shared_timer), old)
    assert merged.timer.task == "A"  # a timer already running in the shared list wins


def test_merge_lines_appends_only_new_lines():
    have = "finished_at,task\n2026-10-01T09:00,A\n"
    extra = "finished_at,task\n2026-09-30T17:00,B\n2026-10-01T09:00,A\n"
    assert fc.merge_lines(have, extra) == "finished_at,task\n2026-10-01T09:00,A\n2026-09-30T17:00,B\n"
    assert fc.merge_lines("", "x\n") == "x\n"


# ---------- pausing one task to start another ----------

def test_timer_start_continues_from_time_already_spent():
    t = fc.TimerState(checkpoints=(0.5, 0.9, 1.0))
    t.start("Draft", estimate_min=10, now=T0, spent_s=360)  # 6 of 10 minutes used in an earlier stretch
    assert t.tick(T0) == []  # the 50% mark was already passed then, so it does not pulse again
    t.tick(T0 + timedelta(minutes=1))
    assert t.elapsed_s == 420
    assert t.remaining_s == 180
    assert t.tick(T0 + timedelta(minutes=3)) == [0.9]


def test_list_end_counts_only_the_unused_part_of_a_paused_task():
    now = datetime(2026, 10, 5, 9, 0)
    tasks = [fc.Task("A", 30, spent_s=600), fc.Task("B", 20)]  # 10 of A's 30 minutes already used
    assert fc.list_end(tasks, None, fc.TimerState(), now) == now + timedelta(minutes=20 + 20)


def test_time_spent_survives_state_round_trip(tmp_path):
    plan = fc.Plan([fc.Task("A", 15, spent_s=111.5), fc.Task("B", 10)])
    fc.save_state(tmp_path / "s.json", plan=plan, current_index=0, timer=fc.TimerState())
    assert [t.spent_s for t in fc.load_state(tmp_path / "s.json").plan.tasks] == [111.5, 0]
