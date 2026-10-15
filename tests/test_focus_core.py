"""Tests for focus_core, the pure-logic half of the Focus Tool (no Qt here)."""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import focus_core as fc  # noqa: E402


# ---------- parse_plan ----------

def test_parse_line_with_minutes_suffix():
    plan = fc.parse_plan("Plan trip itinerary 45m", default_minutes=45)
    assert [(t.name, t.estimate_min) for t in plan.tasks] == [("Plan trip itinerary", 45)]


def test_parse_line_without_duration_uses_default():
    plan = fc.parse_plan("Water plants", default_minutes=45)
    assert plan.tasks[0].estimate_min == 45


def test_parse_accepts_min_and_hours_suffixes():
    plan = fc.parse_plan("Call plumber 10min\nRead novel 1.5h", default_minutes=45)
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
    text = "Plan trip 45m\n  check the bus times first\n  then book a seat\nEmail 5m"
    plan = fc.parse_plan(text, default_minutes=45)
    assert [t.name for t in plan.tasks] == ["Plan trip", "Email"]
    assert plan.tasks[0].details == "check the bus times first\nthen book a seat"
    assert plan.tasks[1].details == ""


def test_parse_dash_lines_become_details_too():
    plan = fc.parse_plan("Plan trip 45m\n- open the map\nEmail", default_minutes=45)
    assert plan.tasks[0].details == "open the map"


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


# ---------- v2: ids, stamps and state version 3 ----------

import json  # noqa: E402
import re  # noqa: E402
import uuid  # noqa: E402
from datetime import date  # noqa: E402


def _v2_doc():
    """A version 2 state file as the earlier desktop app wrote it."""
    return {
        "version": 2,
        "plan": {"tasks": [{"name": "Alpha", "estimate_min": 10, "details": "", "spent_s": 0.0},
                           {"name": "Beta", "estimate_min": 20, "details": "x", "spent_s": 30.0},
                           {"name": "Alpha", "estimate_min": 5, "details": "", "spent_s": 0.0}]},
        "current_index": 2,
        "timer": {"task": "Alpha", "estimate_min": 5, "running": True, "paused": True, "started_at": None,
                  "banked_s": 12.0, "fired": [], "checkpoints": [0.5, 0.9, 1.0]},
        "geometry": None,
        "done": [{"name": "Gamma", "estimate_min": 5, "actual_min": 6, "finished_at": "2026-10-01 09:00",
                  "details": ""}],
    }


def test_now_iso_has_millisecond_utc_format():
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", fc.now_iso())


def test_new_task_gets_a_hex_id_and_epoch_stamp():
    a, b = fc.Task("Alpha", 10), fc.Task("Alpha", 10)
    assert re.fullmatch(r"[0-9a-f]{32}", a.id) and a.id != b.id
    assert a.updated_at == fc.EPOCH and a.deadline is None and a.repeat is None


def test_migrate_gives_deterministic_ids_and_distinct_ids_for_duplicate_names():
    d1, d2 = fc.migrate_doc(_v2_doc()), fc.migrate_doc(_v2_doc())
    ids = [t["id"] for t in d1["plan"]["tasks"]]
    assert ids == [t["id"] for t in d2["plan"]["tasks"]]
    assert len(set(ids)) == 3
    assert ids[0] == uuid.uuid5(fc.NS, "task:Alpha#0").hex
    assert ids[2] == uuid.uuid5(fc.NS, "task:Alpha#1").hex
    assert d1["done"][0]["id"] == uuid.uuid5(fc.NS, "done:Gamma#2026-10-01 09:00").hex
    assert d1["version"] == 5 and d1["folders"] == [] and d1["deleted"] == {}
    assert d1["planner"] == fc.DEFAULT_PLANNER and d1["planner"] is not fc.DEFAULT_PLANNER
    assert d1["order_updated_at"] == fc.EPOCH and d1["plan"]["tasks"][1]["updated_at"] == fc.EPOCH
    assert d1["plan"]["tasks"][1]["deadline"] is None and d1["plan"]["tasks"][1]["spent_s"] == 30.0


def test_migrate_resolves_timer_task_id_by_saved_index_then_name():
    doc = fc.migrate_doc(_v2_doc())
    assert doc["timer"]["task_id"] == doc["plan"]["tasks"][2]["id"]  # index 2 is the second "Alpha"
    d = _v2_doc()
    d["current_index"] = 1
    doc = fc.migrate_doc(d)
    assert doc["timer"]["task_id"] == doc["plan"]["tasks"][0]["id"]  # index points at Beta, so first Alpha
    assert doc["timer"]["updated_at"] == fc.EPOCH


def test_migrate_v1_without_done_list():
    d = _v2_doc()
    d["version"] = 1
    del d["done"]
    doc = fc.migrate_doc(d)
    assert doc["version"] == 5 and doc["done"] == []


def test_version_above_5_is_rejected():
    d = fc.migrate_doc(_v2_doc())
    d["version"] = 6
    with pytest.raises(ValueError):
        fc.doc_to_state(d)
    with pytest.raises(ValueError):
        fc.parse_state(json.dumps(d))


def test_v2_file_loads_and_saves_as_v3_round_trip(tmp_path):
    p = tmp_path / "state.json"
    p.write_text(json.dumps(_v2_doc()), encoding="utf-8")
    st = fc.load_state(p)
    assert [t.name for t in st.plan.tasks] == ["Alpha", "Beta", "Alpha"]
    assert st.timer.task_id == st.plan.tasks[2].id
    text = fc.save_state_obj(tmp_path / "out.json", st)
    doc = json.loads(text)
    assert doc["version"] == 5
    assert fc.state_to_doc(fc.doc_to_state(doc)) == doc


def test_v3_round_trip_keeps_every_field(tmp_path):
    t = fc.Task("Alpha", 30, "d", 5.0, deadline="2026-10-16", target="2026-10-15", folder="Puzzle/Tiles",
                updated_at="2026-10-12T10:00:00.000Z")
    r = fc.Task("Daily walk", 15, repeat={"kind": "every", "every": 2, "anchor": "2026-10-12"},
                last_done="2026-10-12")
    timer = fc.TimerState()
    timer.start("Alpha", 30, T0, task_id=t.id)
    timer.updated_at = "2026-10-12T10:00:01.000Z"
    st = fc.State(fc.Plan([t, r]), 0, timer, done=[fc.DoneTask("Beta", 5, 6, "2026-10-12 09:00", folder="Puzzle")],
                  deleted={"abc": "2026-10-12T09:00:00.000Z"}, order_updated_at="2026-10-12T08:00:00.000Z",
                  folders=["Puzzle", "Puzzle/Tiles"], folders_updated_at="2026-10-12T07:00:00.000Z")
    fc.save_state_obj(tmp_path / "s.json", st)
    back = fc.load_state(tmp_path / "s.json")
    assert fc.state_to_doc(back) == fc.state_to_doc(st)
    assert back.plan.tasks[0].deadline == "2026-10-16" and back.plan.tasks[1].repeat["every"] == 2
    assert back.timer.task_id == t.id and back.timer.updated_at == "2026-10-12T10:00:01.000Z"
    assert back.done[0].folder == "Puzzle" and back.deleted == {"abc": "2026-10-12T09:00:00.000Z"}
    assert back.planner == fc.DEFAULT_PLANNER
    assert json.loads((tmp_path / "s.json").read_text(encoding="utf-8"))["version"] == 5


def test_old_timer_dict_without_new_fields_loads():
    t = fc.TimerState.from_dict({"task": "A", "estimate_min": 5, "running": False})
    assert t.task_id is None and t.updated_at == fc.EPOCH
    assert t.to_dict()["task_id"] is None and t.to_dict()["updated_at"] == fc.EPOCH


def test_find_active_matches_by_id_first():
    tasks = [fc.Task("A", 5), fc.Task("A", 5)]
    assert fc.find_active(tasks, 0, "A", task_id=tasks[1].id) is tasks[1]
    assert fc.find_active(tasks, 0, "A", task_id="missing") is tasks[0]


def test_done_to_task_keeps_dates_and_folder_with_a_new_id():
    d = fc.DoneTask("A", 10, 12, "2026-09-21 10:12", deadline="2026-10-16", folder="Puzzle")
    t = fc.done_to_task(d)
    assert (t.deadline, t.folder) == ("2026-10-16", "Puzzle") and t.id != d.id


# ---------- dates and sections ----------

def test_finish_by_counts_back_business_days():
    assert fc.finish_by(date(2026, 10, 16)) == date(2026, 10, 14)  # Fri -> Wed
    assert fc.finish_by(date(2026, 10, 19)) == date(2026, 10, 15)  # Mon -> Thu
    assert fc.finish_by(date(2026, 10, 17)) == date(2026, 10, 15)  # Sat -> Thu
    assert fc.finish_by(date(2026, 10, 18)) == date(2026, 10, 15)  # Sun -> Thu
    assert fc.finish_by(date(2026, 10, 16), 0) == date(2026, 10, 16)


def test_display_sections_sorts_dated_by_earliest_date_then_deadline_first():
    a = fc.Task("A", 5)
    b = fc.Task("B", 5, target="2026-10-20")
    c = fc.Task("C", 5, deadline="2026-10-20")
    d = fc.Task("D", 5, deadline="2026-10-25", target="2026-10-14")
    e = fc.Task("E", 5, repeat={"kind": "daily"})
    dated, ranked = fc.display_sections([a, b, c, d, e])
    assert [t.name for t in dated] == ["D", "C", "B"]
    assert [t.name for t in ranked] == ["A", "E"]


def test_reorder_ranked_keeps_dated_slots():
    a, b, c, d = fc.Task("A", 5), fc.Task("B", 5, deadline="2026-10-16"), fc.Task("C", 5), fc.Task("D", 5)
    out = fc.reorder_ranked([a, b, c, d], [d.id, a.id, c.id])
    assert [t.name for t in out] == ["D", "B", "A", "C"]


def test_next_block_returns_current_or_next_live_block():
    plan = {"date": "2026-10-13", "blocks": [
        {"task_id": "x", "start": "09:00", "end": "09:30"},
        {"task_id": "y", "start": "09:30", "end": "10:00"},
        {"task_id": "z", "start": "10:00", "end": "11:00"}]}
    assert fc.next_block(plan, datetime(2026, 10, 13, 9, 10), {"x", "y", "z"})["task_id"] == "x"
    assert fc.next_block(plan, datetime(2026, 10, 13, 9, 10), {"z"})["task_id"] == "z"
    assert fc.next_block(plan, datetime(2026, 10, 13, 9, 30), {"x", "y"})["task_id"] == "y"
    assert fc.next_block(plan, datetime(2026, 10, 13, 11, 0), {"x", "y", "z"}) is None
    assert fc.next_block(plan, datetime(2026, 10, 14, 9, 0), {"x"}) is None
    assert fc.next_block(None, datetime(2026, 10, 13, 9, 0), {"x"}) is None


# ---------- repeating tasks ----------

def test_is_active_for_each_repeat_kind():
    mon, tue, sat = date(2026, 10, 12), date(2026, 10, 13), date(2026, 10, 17)
    assert fc.is_active(fc.Task("A", 5), sat)
    assert fc.is_active(fc.Task("A", 5, repeat={"kind": "daily"}), sat)
    wk = fc.Task("A", 5, repeat={"kind": "weekdays"})
    assert fc.is_active(wk, mon) and not fc.is_active(wk, sat)
    days = fc.Task("A", 5, repeat={"kind": "days", "days": ["mon", "thu"]})
    assert fc.is_active(days, mon) and not fc.is_active(days, tue) and fc.is_active(days, date(2026, 10, 15))


def test_every_two_days_is_calendar_based_and_skipping_does_not_shift_it():
    t = fc.Task("A", 5, repeat={"kind": "every", "every": 2, "anchor": "2026-10-12"})
    assert [fc.is_active(t, date(2026, 10, d)) for d in range(11, 17)] == [False, True, False, True, False, True]
    t.last_done = "2026-10-12"  # 10/14 is then skipped (never done); 10/16 is still a day
    assert not fc.is_active(t, date(2026, 10, 12))
    assert fc.is_active(t, date(2026, 10, 16))


def test_last_done_blocks_only_that_day():
    t = fc.Task("A", 5, repeat={"kind": "daily"}, last_done="2026-10-12")
    assert not fc.is_active(t, date(2026, 10, 12)) and fc.is_active(t, date(2026, 10, 13))


def test_next_active_day():
    t = fc.Task("A", 5, repeat={"kind": "days", "days": ["mon"]})
    assert fc.next_active_day(t, date(2026, 10, 12)) == date(2026, 10, 19)
    e = fc.Task("A", 5, repeat={"kind": "every", "every": 3, "anchor": "2026-10-12"})
    assert fc.next_active_day(e, date(2026, 10, 12)) == date(2026, 10, 15)
    assert fc.next_active_day(e, date(2026, 10, 1)) == date(2026, 10, 12)
    assert fc.next_active_day(fc.Task("A", 5), date(2026, 10, 12)) is None


def test_complete_repeat_logs_that_days_copy_and_sleeps_the_task():
    t = fc.Task("Walk", 15, "outside", 120.0, folder="Health", repeat={"kind": "daily"})
    now = datetime(2026, 10, 12, 18, 5)
    d = fc.complete_repeat(t, now, 14)
    assert d.id == uuid.uuid5(fc.NS, "rep:" + t.id + "#2026-10-12").hex
    assert (d.name, d.estimate_min, d.actual_min, d.finished_at, d.details, d.folder) == \
           ("Walk", 15, 14, "2026-10-12 18:05", "outside", "Health")
    assert t.last_done == "2026-10-12" and t.spent_s == 0
    assert not fc.is_active(t, now.date())


# ---------- folders ----------

def test_in_folder_matches_the_folder_and_its_subfolders():
    t = fc.Task("A", 5, folder="Puzzle/Tiles")
    assert fc.in_folder(t, None) and fc.in_folder(t, "Puzzle") and fc.in_folder(t, "Puzzle/Tiles")
    assert not fc.in_folder(t, "Puz") and not fc.in_folder(t, "Puzzle/Tiles/Deep")
    assert not fc.in_folder(fc.Task("B", 5), "Puzzle") and fc.in_folder(fc.Task("B", 5), None)


def test_folder_tree_includes_parents_and_sorts_children_under_parents():
    tasks = [fc.Task("A", 5, folder="Tennis/Fall/Week 1")]
    assert fc.folder_tree(["Puzzle/Tiles", "Puzzle B", "Puzzle"], tasks) == [
        ("Puzzle", 0), ("Puzzle/Tiles", 1), ("Puzzle B", 0), ("Tennis", 0), ("Tennis/Fall", 1), ("Tennis/Fall/Week 1", 2)]


def _folder_state():
    tasks = [fc.Task("A", 5, folder="Puzzle"), fc.Task("B", 5, folder="Puzzle/Tiles"), fc.Task("C", 5, folder="Puzzles"),
             fc.Task("D", 5)]
    done = [fc.DoneTask("E", 5, 5, "2026-10-01 09:00", folder="Puzzle/Tiles")]
    return fc.State(fc.Plan(tasks), 0, fc.TimerState(), done=done, folders=["Puzzle", "Puzzle/Tiles", "Puzzles"])


def test_rename_folder_moves_subfolders_tasks_and_done():
    st = _folder_state()
    fc.rename_folder(st, "Puzzle", "Crafts/Puzzle")
    assert st.folders == ["Crafts/Puzzle", "Crafts/Puzzle/Tiles", "Puzzles", "Crafts"]
    assert [t.folder for t in st.plan.tasks] == ["Crafts/Puzzle", "Crafts/Puzzle/Tiles", "Puzzles", None]
    assert st.done[0].folder == "Crafts/Puzzle/Tiles"


def test_delete_folder_removes_subfolders_and_unfiles_their_tasks():
    st = _folder_state()
    fc.delete_folder(st, "Puzzle")
    assert st.folders == ["Puzzles"]
    assert [t.folder for t in st.plan.tasks] == [None, None, "Puzzles", None]


# ---------- tools/migrate_state.py ----------

def _migrate_tool():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "migrate_state", Path(__file__).resolve().parents[1] / "tools" / "migrate_state.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_migrate_tool_writes_v3_and_never_overwrites(tmp_path):
    src, out = tmp_path / "in.json", tmp_path / "out.json"
    src.write_text(json.dumps(_v2_doc()), encoding="utf-8")
    before = src.read_text(encoding="utf-8")
    tool = _migrate_tool()
    assert tool.main([str(src), str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8")) == fc.migrate_doc(_v2_doc())
    assert src.read_text(encoding="utf-8") == before
    written = out.read_text(encoding="utf-8")
    assert tool.main([str(src), str(out)]) != 0  # OUT exists: refused
    assert out.read_text(encoding="utf-8") == written
    assert tool.main([str(src), str(src)]) != 0


# ---------- fix round 1 ----------

def test_load_state_raises_for_a_newer_version_instead_of_returning_an_empty_state(tmp_path):
    d = fc.migrate_doc(_v2_doc())
    d["version"] = 6
    p = tmp_path / "s.json"
    p.write_text(json.dumps(d), encoding="utf-8")
    with pytest.raises(fc.NewerVersionError):
        fc.load_state(p)
    with pytest.raises(fc.NewerVersionError):
        fc.parse_state(json.dumps(d))
    assert issubclass(fc.NewerVersionError, ValueError)


def test_load_state_still_returns_empty_state_for_a_half_synced_file(tmp_path):
    p = tmp_path / "s.json"
    p.write_text('{"version": 3, "plan": {"tas', encoding="utf-8")
    assert fc.load_state(p).plan.tasks == []


def test_finish_task_reuses_the_task_id_and_copies_fields():
    t = fc.Task("Alpha", 30, "notes", 600.0, deadline="2026-10-16", target="2026-10-15", folder="Puzzle")
    d = fc.finish_task(t, datetime(2026, 10, 12, 15, 2), 40)
    assert d.id == t.id
    assert (d.name, d.estimate_min, d.actual_min, d.finished_at, d.details, d.deadline, d.target, d.folder) == \
           ("Alpha", 30, 40, "2026-10-12 15:02", "notes", "2026-10-16", "2026-10-15", "Puzzle")


def test_finish_task_on_a_repeat_delegates_to_complete_repeat():
    t = fc.Task("Walk", 15, repeat={"kind": "daily"})
    d = fc.finish_task(t, datetime(2026, 10, 12, 18, 5), 14)
    assert d.id == uuid.uuid5(fc.NS, "rep:" + t.id + "#2026-10-12").hex
    assert t.last_done == "2026-10-12"


def test_done_without_finished_at_still_merges():
    a = fc.migrate_doc(_v2_doc())
    del a["done"][0]["finished_at"]
    b = fc.migrate_doc(_v2_doc())
    b["done"] = [{"id": "d2", "name": "Delta", "estimate_min": 5, "actual_min": 5, "finished_at": "2026-10-02 09:00"}]
    m = fc.merge_docs(a, b)
    assert [d["finished_at"] for d in m["done"]] == ["", "2026-10-02 09:00"]


@pytest.mark.parametrize("bad", [None, "three", [3]])
def test_bad_version_raises_value_error(bad):
    d = fc.migrate_doc(_v2_doc())
    d["version"] = bad
    good = fc.migrate_doc(_v2_doc())
    with pytest.raises(ValueError):
        fc.merge_docs(d, good)
    with pytest.raises(ValueError):
        fc.stamp_changes(good, d, "2026-10-12T15:00:00.000Z")


# ---------- deadline time of day ----------

@pytest.mark.parametrize("text, minutes", [("17:00", 1020), ("00:00", 0), ("23:59", 1439), ("24:00", None),
                                           ("9:00", None), ("12:60", None), (None, None), ("", None)])
def test_parse_hm(text, minutes):
    assert fc.parse_hm(text) == minutes


def test_deadline_time_survives_save_finish_and_put_back(tmp_path):
    t = fc.Task("Report", 30, deadline="2026-10-16", deadline_time="17:00")
    st = fc.State(fc.Plan([t]), 0, fc.TimerState())
    path = tmp_path / "state.json"
    fc.save_state_obj(path, st)
    back = fc.load_state(path).plan.tasks[0]
    assert (back.deadline, back.deadline_time) == ("2026-10-16", "17:00")
    done = fc.finish_task(back, datetime(2026, 10, 15, 9, 0), 25)
    assert done.deadline_time == "17:00"
    assert fc.done_to_task(done).deadline_time == "17:00"


# ---------- folder notes ----------

def _notes_state():
    st = fc.State(fc.Plan([fc.Task("Draft", 30, folder="Grant")]), 0, fc.TimerState(), folders=["Grant", "Grant/Budget"])
    st.folder_notes = {"Grant": {"text": "Waiting on letters", "updated_at": fc.EPOCH},
                       "Grant/Budget": {"text": "Sent to Sam", "updated_at": fc.EPOCH}}
    st.progress = [{"id": "n1", "folder": "Grant/Budget", "at": "2026-10-12 09:00", "text": "First draft",
                    "updated_at": fc.EPOCH}]
    return st


def test_notes_survive_a_save_and_load(tmp_path):
    path = tmp_path / "state.json"
    fc.save_state_obj(path, _notes_state())
    back = fc.load_state(path)
    assert back.folder_notes["Grant"]["text"] == "Waiting on letters"
    assert back.progress[0]["text"] == "First draft"


def test_rename_folder_moves_its_notes_and_progress():
    st = _notes_state()
    fc.rename_folder(st, "Grant", "NSF grant")
    assert st.folder_notes["NSF grant"]["text"] == "Waiting on letters"
    assert st.folder_notes["NSF grant/Budget"]["text"] == "Sent to Sam"
    assert st.folder_notes["Grant"]["text"] == "" and st.folder_notes["Grant/Budget"]["text"] == ""
    assert st.progress[0]["folder"] == "NSF grant/Budget"


def test_rename_onto_a_folder_with_notes_keeps_both_texts():
    st = _notes_state()
    st.folder_notes["Other"] = {"text": "Old notes", "updated_at": fc.EPOCH}
    fc.rename_folder(st, "Grant/Budget", "Other")
    assert st.folder_notes["Other"]["text"] == "Old notes\n\nSent to Sam"


def test_delete_folder_keeps_its_notes():
    st = _notes_state()
    fc.delete_folder(st, "Grant")
    assert st.folders == []
    assert st.folder_notes["Grant"]["text"] == "Waiting on letters"
    assert st.progress[0]["folder"] == "Grant/Budget"


def test_merge_states_keeps_the_shared_notes():
    shared = _notes_state()
    out = fc.merge_states(shared, fc.State(fc.Plan(), 0, fc.TimerState()))
    assert out.folder_notes == shared.folder_notes and out.progress == shared.progress


# ---------- priority tags and the No folder filter ----------

def test_priority_defaults_to_med_and_round_trips():
    t = fc.Task("A", 5)
    assert t.priority == "med"
    assert fc.task_from_dict(fc.task_to_dict(fc.Task("B", 5, priority="high"))).priority == "high"
    doc = fc.migrate_doc({"version": 4, "plan": {"tasks": [{"id": "a", "name": "A", "estimate_min": 5}]}})
    assert doc["version"] == 5 and doc["plan"]["tasks"][0]["priority"] == "med"


def test_by_priority_is_high_med_low_and_keeps_list_order_inside_a_tag():
    tasks = [fc.Task("L", 5, priority="low"), fc.Task("M1", 5), fc.Task("H", 5, priority="high"),
             fc.Task("M2", 5), fc.Task("Odd", 5, priority="??")]
    assert [t.name for t in fc.by_priority(tasks)] == ["H", "M1", "M2", "Odd", "L"]
    _, ranked = fc.display_sections(tasks)
    assert [t.name for t in ranked] == ["H", "M1", "M2", "Odd", "L"]


def test_no_folder_filter():
    a, b = fc.Task("A", 5, folder="Home"), fc.Task("B", 5)
    assert [t.name for t in (a, b) if fc.in_folder(t, fc.NO_FOLDER)] == ["B"]
    assert [t.name for t in (a, b) if fc.in_folder(t, None)] == ["A", "B"]
