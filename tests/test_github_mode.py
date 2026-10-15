"""The desktop's GitHub sync mode, the Day plan window, Up next from the plan, and working hours.

Run without a screen (Qt offscreen mode) against tests/fake_github.py on localhost, never the real network.
gh_store's token functions are replaced, so the real Windows Credential Manager is never touched. Each
"computer" gets its own %APPDATA% folder; all of them share one stand-in for the Box data folder.
"""
import json
import os
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication

import focus_core as fc
import gh_store
from fake_github import start_fake

T0 = datetime(2026, 10, 12, 9, 0)  # a Monday


@pytest.fixture
def fake():
    f = start_fake(token="test-token", owner="me", repo="focus-data")
    yield f
    f.stop()


@pytest.fixture
def env(tmp_path, monkeypatch, fake):
    QApplication.instance() or QApplication([])
    import focus_tool as ft
    box = tmp_path / "box" / "data"
    monkeypatch.setenv("FOCUSTOOL_DATA", str(box))
    monkeypatch.setattr(ft.FocusWidget, "AUTO_SYNC", False)
    tokens = {"t": "test-token"}
    monkeypatch.setattr(gh_store, "load_token", lambda: tokens["t"])
    monkeypatch.setattr(gh_store, "save_token", lambda t: tokens.__setitem__("t", t))
    monkeypatch.setattr(gh_store, "delete_token", lambda: tokens.__setitem__("t", None))

    def engine(cfg):
        store = gh_store.GitHubStore(cfg["owner"], cfg["repo"], cfg["token"], branch=cfg.get("branch", "main"),
                                     api=fake.url, timeout=3)
        return gh_store.SyncEngine(store, ft.local_dir() / "github-cache.json", sleep=lambda s: None)

    monkeypatch.setattr(ft, "make_sync_engine", engine)
    made = []

    def computer(name, github=True):
        appdata = tmp_path / name
        monkeypatch.setenv("APPDATA", str(appdata))
        local = appdata / "FocusTool"
        local.mkdir(parents=True, exist_ok=True)
        lj = local / "local.json"
        if github and not lj.exists():
            lj.write_text(json.dumps({"github": {"owner": "me", "repo": "focus-data", "branch": "main"}}),
                          encoding="utf-8")
        w = ft.FocusWidget(ft.load_settings())
        made.append(w)
        return w

    yield SimpleNamespace(ft=ft, computer=computer, tokens=tokens, box=box, fake=fake, tmp=tmp_path)
    for w in made:
        w.stop_sync()
        w.panel.close()
        w.plan_window.close()
        w.close()


@pytest.fixture
def clock(monkeypatch):
    import focus_tool as ft

    class Clock:
        t = T0

        @classmethod
        def now(cls):
            return cls.t

    monkeypatch.setattr(ft, "datetime", Clock)
    return Clock


def names(w):
    return [t.name for t in w.plan.tasks]


def remote(fake) -> dict:
    return json.loads(fake.files["state.json"]["text"])


def remote_names(fake):
    return [t["name"] for t in remote(fake)["plan"]["tasks"]]


def _texts(lst):
    return [lst.item(i).text() for i in range(lst.count())]


def add(w, name, minutes=10, **kw):
    t = fc.Task(name, minutes, **kw)
    w.plan.tasks.insert(0, t)
    w.changed()
    return t


# ---------- GitHub mode ----------

def test_two_computers_converge(env):
    a, b = env.computer("a"), env.computer("b")
    assert a.mode == "github" and b.mode == "github"
    add(a, "Alpha")
    assert a.sync_now().status == "ok"
    assert remote_names(env.fake) == ["Alpha"]
    b.sync_now()
    assert names(b) == ["Alpha"]
    assert a.sync_status.startswith("Synced ")


def test_concurrent_edits_both_survive(env):
    a, b = env.computer("a"), env.computer("b")
    add(a, "Alpha")
    a.sync_now()
    b.sync_now()
    a.plan.tasks[0].name = "Alpha 2"
    a.changed()
    add(b, "Beta")
    a.sync_now()
    b.sync_now()
    a.sync_now()
    assert sorted(names(a)) == sorted(names(b)) == ["Alpha 2", "Beta"]


def test_a_delete_sticks(env):
    a, b = env.computer("a"), env.computer("b")
    add(a, "Beta")
    add(a, "Alpha")
    a.sync_now()
    b.sync_now()
    a.plan.tasks = [t for t in a.plan.tasks if t.name != "Alpha"]
    a.changed()
    a.sync_now()
    b.sync_now()
    b.sync_now()
    a.sync_now()
    assert names(a) == names(b) == ["Beta"] == remote_names(env.fake)


def test_adopted_remote_data_is_not_stamped_as_a_local_edit(env):
    a, b = env.computer("a"), env.computer("b")
    add(a, "Alpha")
    a.sync_now()
    stamp = remote(env.fake)["plan"]["tasks"][0]["updated_at"]
    b.sync_now()
    add(b, "Beta")
    b.sync_now()
    alpha = next(t for t in remote(env.fake)["plan"]["tasks"] if t["name"] == "Alpha")
    assert alpha["updated_at"] == stamp


def test_box_mirror_is_written_but_not_read(env):
    a = env.computer("a")
    add(a, "Alpha")
    a.sync_now()
    assert [t.name for t in fc.load_state(env.box / "state.json").plan.tasks] == ["Alpha"]
    fc.save_state_obj(env.box / "state.json", fc.State(fc.Plan([fc.Task("Intruder", 5)]), 0, fc.TimerState()))
    a.check_shared()
    a.sync_now()
    assert names(a) == ["Alpha"] and remote_names(env.fake) == ["Alpha"]


def test_first_github_start_imports_the_box_file_once(env):
    env.box.mkdir(parents=True)
    fc.save_state_obj(env.box / "state.json", fc.State(fc.Plan([fc.Task("Alpha", 10)]), 0, fc.TimerState()))
    a = env.computer("a")
    a.sync_now()
    assert remote_names(env.fake) == ["Alpha"]
    assert remote(env.fake)["order_updated_at"] != fc.EPOCH
    local = json.loads((env.tmp / "a" / "FocusTool" / "local.json").read_text(encoding="utf-8"))
    assert local["github_imported"] is True
    fc.save_state_obj(env.box / "state.json",
                      fc.State(fc.Plan([fc.Task("Alpha", 10), fc.Task("Gamma", 5)]), 0, fc.TimerState()))
    a.stop_sync()
    again = env.computer("a")
    again.sync_now()
    assert names(again) == ["Alpha"]


def test_offline_edit_is_kept_and_pushed_later(env):
    a = env.computer("a")
    a.sync_now()
    dead = start_fake()
    dead_url = dead.url
    dead.stop()
    live_url = a._engine.store.api
    a._engine.store.api = dead_url
    add(a, "Alpha")
    assert a.sync_now().status == "offline"
    assert a.sync_status == "Offline, will retry"
    assert "Alpha" not in json.dumps(remote(env.fake))
    a.stop_sync()
    again = env.computer("a")  # a restart while offline keeps the edit
    assert names(again) == ["Alpha"]
    again._engine.store.api = live_url
    assert again.sync_now().status == "ok"
    assert remote_names(env.fake) == ["Alpha"]


def test_a_rejected_token_says_so(env):
    env.tokens["t"] = "wrong-token"
    a = env.computer("a")
    assert a.sync_now().status == "auth"
    assert a.sync_status == "GitHub token rejected"
    a.panel.show()
    assert a.panel.sync_label.text() == "GitHub token rejected"


def test_rate_limit_says_when_it_retries(env, clock):
    a = env.computer("a")
    env.fake.set_rate_limit(reset_at=time.time() + 600)
    assert a.sync_now().status == "rate_limited"
    assert a.sync_status.startswith("GitHub busy, retrying at ")


def test_background_thread_syncs(env, monkeypatch):
    monkeypatch.setattr(env.ft.FocusWidget, "AUTO_SYNC", True)
    a = env.computer("a")
    add(a, "Alpha")
    a.kick_sync()
    end = time.monotonic() + 10
    while time.monotonic() < end and not a.sync_status.startswith("Synced "):
        time.sleep(0.05)
        a.drain_sync_results()  # what on_tick does in the app
    assert remote_names(env.fake) == ["Alpha"]
    assert a.sync_status.startswith("Synced ")


def test_github_dialog_saves_and_disconnect_returns_to_box_mode(env):
    env.tokens["t"] = None
    a = env.computer("a", github=False)
    assert a.mode == "box"
    dlg = env.ft.GitHubDialog(a)
    dlg.owner.setText("me")
    dlg.repo.setText("focus-data")
    dlg.token.setText("test-token")
    assert dlg.save()
    assert a.mode == "github" and env.tokens["t"] == "test-token"
    add(a, "Alpha")
    a.sync_now()
    assert remote_names(env.fake) == ["Alpha"]
    a.disconnect_github()
    assert a.mode == "box" and env.tokens["t"] is None
    local = json.loads((env.tmp / "a" / "FocusTool" / "local.json").read_text(encoding="utf-8"))
    assert "github" not in local
    add(a, "Beta")
    assert [t.name for t in fc.load_state(env.box / "state.json").plan.tasks] == ["Beta", "Alpha"]
    assert remote_names(env.fake) == ["Alpha"]


# ---------- day plan ----------

def _plan(day, blocks, made="2026-10-12T00:07:00.000Z"):
    return {"version": 1, "date": day, "mode": "now", "generated_at": made, "generated_by": "actions",
            "tz": "America/Chicago", "start": "09:00", "capacity_min": 450, "free_min": 0, "blocks": blocks,
            "outlook": [], "warnings": [{"task_id": "x", "name": "Alpha", "kind": "deadline_missed",
                                         "message": "needs 30 min more than fits before Thu 10/15"}]}


def _block(t, start, end, minutes):
    return {"task_id": t.id, "name": t.name, "start": start, "end": end, "minutes": minutes, "kind": "ranked",
            "date": None, "date_kind": None, "left_after_min": 0, "repeat": False}


def _write_plan(env, plan):
    env.box.mkdir(parents=True, exist_ok=True)
    (env.box / "day-plan.json").write_text(json.dumps(plan), encoding="utf-8")


def test_replan_now_writes_the_plan_and_uploads_it(env, clock):
    a = env.computer("a")
    add(a, "Alpha", 30)
    a.sync_now()
    a.replan_now()
    plan = json.loads((env.box / "day-plan.json").read_text(encoding="utf-8"))
    assert plan["date"] == "2026-10-12" and plan["generated_by"] == "desktop"
    assert [(b["name"], b["start"], b["end"]) for b in plan["blocks"]] == [("Alpha", "09:00", "09:30")]
    a.sync_now()
    assert json.loads(env.fake.files["day-plan.json"]["text"])["generated_at"] == plan["generated_at"]


def test_github_mode_reads_the_plan_from_github_and_mirrors_it(env, clock):
    a = env.computer("a")
    alpha = add(a, "Alpha", 30)
    env.fake._put("day-plan.json", json.dumps(_plan("2026-10-12", [_block(alpha, "09:00", "09:30", 30)])))
    a.sync_now()
    assert a.day_plan["date"] == "2026-10-12"
    assert json.loads((env.box / "day-plan.json").read_text(encoding="utf-8"))["date"] == "2026-10-12"


def test_day_plan_window_shows_todays_or_tomorrows_plan(env, clock):
    a = env.computer("a", github=False)
    alpha = add(a, "Alpha", 30)
    a.toggle_day_plan()
    _write_plan(env, _plan("2026-10-12", [_block(alpha, "09:00", "09:30", 30)]))
    a.check_shared()
    p = a.plan_window
    assert "Mon 10/12" in p.plan_heading.text() and "made 19:07" in p.plan_heading.text()
    assert _texts(p.plan_list) == ["09:00-09:30  Alpha  (30m)"]
    assert "needs 30 min more" in p.plan_warn.text() and p.plan_warn.isVisibleTo(p)
    # (two blocks, so the file's size changes too and the change is seen even within one file-time tick)
    _write_plan(env, _plan("2026-10-13", [_block(alpha, "10:00", "10:15", 15), _block(alpha, "11:00", "11:15", 15)],
                           made="2026-10-13T00:07:01.000Z"))
    a.check_shared()
    assert "Tue 10/13" in p.plan_heading.text()
    _write_plan(env, _plan("2026-10-11", [], made="2026-10-11T00:07:02.000Z"))
    a.check_shared()
    assert p.plan_heading.text().startswith("No plan yet") and p.plan_list.count() == 0


def test_up_next_follows_the_current_block(env, clock):
    a = env.computer("a", github=False)
    gamma, beta, alpha = add(a, "Gamma"), add(a, "Beta"), add(a, "Alpha")
    _write_plan(env, _plan("2026-10-12", [_block(beta, "09:00", "10:00", 60), _block(gamma, "10:00", "11:00", 60)]))
    a.check_shared()
    clock.t = T0 + timedelta(minutes=30)
    assert a.current_task() is beta
    clock.t = T0 + timedelta(minutes=90)
    assert a.current_task() is gamma
    clock.t = T0 + timedelta(minutes=150)
    assert a.current_task() is alpha


# ---------- working hours ----------

def test_working_hours_dialog_validates_and_saves(env):
    a = env.computer("a", github=False)
    dlg = env.ft.WorkingHoursDialog(a)
    assert dlg.edits["mon"].text() == "09:00-12:00, 13:30-18:00"
    dlg.edits["mon"].setText("9-12")
    assert dlg.apply() is not None
    assert a.planner["windows"]["mon"] == ["09:00-12:00", "13:30-18:00"]
    dlg.edits["mon"].setText("12:00-09:00")
    assert dlg.apply() is not None
    dlg.edits["mon"].setText("13:00-17:00, 8:30-12:00")
    dlg.edits["sat"].setText("")
    assert dlg.apply() is None
    saved = json.loads((env.box / "state.json").read_text(encoding="utf-8"))["planner"]
    assert saved["windows"]["mon"] == ["08:30-12:00", "13:00-17:00"] and saved["windows"]["sat"] == []
    assert saved["updated_at"] != fc.EPOCH


# ---------- Box mode follow-ups ----------

def test_box_write_merges_a_change_saved_meanwhile(env):
    here, there = env.computer("a", github=False), env.computer("b", github=False)
    add(here, "Alpha")
    there.check_shared()
    add(there, "Beta")
    here.plan.tasks.append(fc.Task("Gamma", 5))
    here.changed()  # here has not looked at the file since there saved Beta
    assert sorted(t.name for t in fc.load_state(env.box / "state.json").plan.tasks) == ["Alpha", "Beta", "Gamma"]
    assert sorted(names(here)) == ["Alpha", "Beta", "Gamma"]


def test_box_write_never_replaces_a_newer_file_saved_meanwhile(env):
    here = env.computer("a", github=False)
    add(here, "Alpha")
    text = json.dumps({"version": 6, "plan": {"tasks": []}})
    (env.box / "state.json").write_text(text, encoding="utf-8")
    add(here, "Beta")
    assert (env.box / "state.json").read_text(encoding="utf-8") == text
    assert "not saved" in here.status_message


def test_adopting_from_box_then_one_edit_keeps_the_other_stamps(env):
    there = env.computer("b", github=False)
    add(there, "Beta")
    add(there, "Alpha")
    here = env.computer("a", github=False)
    add(there, "Gamma")
    here.check_shared()
    before = {t["name"]: t["updated_at"] for t in json.loads(
        (env.box / "state.json").read_text(encoding="utf-8"))["plan"]["tasks"]}
    time.sleep(0.01)
    next(t for t in here.plan.tasks if t.name == "Alpha").estimate_min = 25
    here.changed()
    after = {t["name"]: t["updated_at"] for t in json.loads(
        (env.box / "state.json").read_text(encoding="utf-8"))["plan"]["tasks"]}
    assert after["Beta"] == before["Beta"] and after["Gamma"] == before["Gamma"]
    assert after["Alpha"] != before["Alpha"]


def test_repeats_wake_up_after_midnight(env, clock):
    a = env.computer("a", github=False)
    add(a, "Walk", repeat={"kind": "daily"}, last_done="2026-10-12")
    a.panel.show()
    dim = QColor(a.s["colors"]["text_dim"])
    assert a.panel.list.item(0).foreground().color() == dim
    clock.t = datetime(2026, 10, 13, 0, 1)
    a.on_tick()
    assert a.panel.list.item(0).foreground().color() != dim
    assert a.current_task().name == "Walk"


def test_malformed_dates_from_another_device_do_not_crash(env, clock):
    a = env.computer("a", github=False)
    add(a, "Walk", repeat={"kind": "every", "every": 2, "anchor": "someday"})
    add(a, "Alpha", deadline="soon")
    a.panel.show()
    a.panel.refresh()
    rows = [i for i in range(a.panel.list.count()) if "Alpha" in a.panel.list.item(i).text()]
    assert rows
    a.panel.list.setCurrentRow(rows[0])
    assert a.panel.ed_deadline.value() is None
    assert a.current_task() is not None
    a.replan_now()  # never raises; the planner's complaint goes to the status line


# ---------- fix round 1 ----------

def _make_slow(w):
    """Make w's sync wait until `gate` is set; `entered` is set once a sync has started."""
    gate, entered = threading.Event(), threading.Event()
    real = w._engine.sync

    def slow(doc):
        entered.set()
        gate.wait(10)
        return real(doc)

    w._engine.sync = slow
    return gate, entered


def test_a_rejected_token_stops_polling_until_a_new_token_is_saved(env):
    env.tokens["t"] = "wrong-token"
    a = env.computer("a")
    assert a.sync_now().status == "auth"
    a.AUTO_SYNC = True
    add(a, "Alpha")
    a._sync_due, a._last_sync_at = 0.0, float("-inf")
    a._sync_tick()
    assert not a._job_pending and a._jobs.empty()
    a.AUTO_SYNC = False
    env.tokens["t"] = "test-token"
    a.connect_github({"owner": "me", "repo": "focus-data", "branch": "main"}, "test-token")
    assert names(a) == ["Alpha"]  # the edit made after the 401 is still there
    assert a.sync_now().status == "ok"
    assert remote_names(env.fake) == ["Alpha"]


def test_quitting_while_a_sync_is_stuck_keeps_the_edit(env):
    a = env.computer("a")
    a.sync_now()
    gate, entered = _make_slow(a)
    try:
        a.AUTO_SYNC = True
        a.kick_sync()
        assert entered.wait(5)
        add(a, "Alpha")
        started = time.monotonic()
        a.shutdown()
        assert time.monotonic() - started < 2
        again = env.computer("a")
        assert names(again) == ["Alpha"]
    finally:
        gate.set()


def test_an_edit_made_while_a_sync_runs_is_kept_and_pushed(env):
    a = env.computer("a")
    a.sync_now()
    a.kick_sync()  # no worker in tests, so the job waits in the queue
    add(a, "Beta")
    a._apply_job_result(env.ft.run_job(a._jobs.get_nowait()))
    assert names(a) == ["Beta"]
    a.sync_now()
    assert remote_names(env.fake) == ["Beta"]


def test_disconnect_cancels_queued_and_running_jobs(env):
    a = env.computer("a")
    add(a, "Alpha")
    a.sync_now()
    folder = env.tmp / "a" / "FocusTool"
    assert (folder / "github-cache.json").exists() and (folder / "github-pending.json").exists()
    gate, entered = _make_slow(a)
    try:
        a.AUTO_SYNC = True
        a.kick_sync()
        assert entered.wait(5)
        worker = a._worker
        add(a, "Beta")
        a.kick_sync()  # queued behind the stuck one
        started = time.monotonic()
        a.disconnect_github()
        assert time.monotonic() - started < 2
        assert a.mode == "box"
    finally:
        gate.set()
    worker.join(10)
    a.drain_sync_results()
    assert "Beta" not in remote_names(env.fake)
    assert not (folder / "github-cache.json").exists() and not (folder / "github-pending.json").exists()
    assert a.mode == "box" and a.sync_status == ""


def test_a_newer_box_file_does_not_block_github_mode(env):
    env.box.mkdir(parents=True)
    text = json.dumps({"version": 6, "plan": {"tasks": []}})
    (env.box / "state.json").write_text(text, encoding="utf-8")
    a = env.computer("a")
    add(a, "Alpha")
    assert a.sync_now().status == "ok"
    assert remote_names(env.fake) == ["Alpha"]
    assert (env.box / "state.json").read_text(encoding="utf-8") == text  # the newer file is left alone
    assert a.status_message == ""
    a.disconnect_github()
    assert "newer Focus Tool" in a.status_message


def test_a_same_size_rewrite_in_the_same_file_tick_is_seen(env, clock):
    a = env.computer("a", github=False)
    alpha = add(a, "Alpha", 30)
    _write_plan(env, _plan("2026-10-12", [_block(alpha, "09:00", "09:30", 30)]))
    a.check_shared()
    path = env.box / "day-plan.json"
    st = os.stat(path)
    _write_plan(env, _plan("2026-10-12", [_block(alpha, "10:00", "10:30", 30)]))
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert os.stat(path).st_size == st.st_size
    a.check_shared()
    assert a.day_plan["blocks"][0]["start"] == "10:00"


def test_a_same_size_list_rewrite_in_the_same_file_tick_is_seen(env):
    here, there = env.computer("a", github=False), env.computer("b", github=False)
    add(there, "Alpha")
    here.check_shared()
    path = env.box / "state.json"
    st = os.stat(path)
    there.plan.tasks[0].name = "Omega"
    there.changed()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert os.stat(path).st_size == st.st_size
    here.check_shared()
    assert names(here) == ["Omega"]


def test_a_briefly_locked_file_is_retried(env, monkeypatch):
    calls, real = [], Path.replace

    def flaky(self, target):  # a virus scanner can hold a new file for a moment on Windows
        calls.append(1)
        if len(calls) <= 2:
            raise PermissionError(5, "Access is denied")
        return real(self, target)

    monkeypatch.setattr(Path, "replace", flaky)
    path = env.tmp / "x.json"
    env.ft._write_atomic(path, "hi")
    assert path.read_text(encoding="utf-8") == "hi" and len(calls) == 3


def test_saving_the_window_position_never_raises(env, monkeypatch):
    a = env.computer("a", github=False)

    def locked(self, target):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(Path, "replace", locked)
    a._save_local()  # the position is saved next time; the app must not stop


# ---------- fix round 2 ----------

def test_reconnecting_during_a_stuck_sync_keeps_the_new_files(env):
    a = env.computer("a")
    add(a, "Alpha")
    a.sync_now()
    folder = env.tmp / "a" / "FocusTool"
    gate, entered = _make_slow(a)
    try:
        a.AUTO_SYNC = True
        a.kick_sync()
        assert entered.wait(5)
        worker = a._worker
        a.AUTO_SYNC = False
        a.disconnect_github()
        env.tokens["t"] = "test-token"
        a.connect_github({"owner": "me", "repo": "focus-data", "branch": "main"}, "test-token")
        add(a, "Beta")
    finally:
        gate.set()
    worker.join(10)  # the old session's sync ends, then its clean-up job runs
    assert (folder / "github-pending.json").exists() and (folder / "github-cache.json").exists()
    pending = json.loads((folder / "github-pending.json").read_text(encoding="utf-8"))
    assert "Beta" in [t["name"] for t in pending["plan"]["tasks"]]
    assert a.sync_now().status == "ok"
    assert "Beta" in remote_names(env.fake)


def test_a_newer_box_file_saved_during_github_mode_is_not_overwritten(env):
    a = env.computer("a")
    add(a, "Alpha")
    a.sync_now()
    text = json.dumps({"version": 6, "plan": {"tasks": []}})
    (env.box / "state.json").write_text(text, encoding="utf-8")
    add(a, "Beta")
    assert a.sync_now().status == "ok"
    assert (env.box / "state.json").read_text(encoding="utf-8") == text
    assert "Beta" in remote_names(env.fake)


# ---------- final review fixes ----------

def test_replan_without_time_zone_data_says_so_instead_of_crashing(env, clock, monkeypatch):
    import zoneinfo
    import planner

    def no_tz(*a, **k):
        raise zoneinfo.ZoneInfoNotFoundError("No time zone found with key America/Chicago")

    monkeypatch.setattr(planner, "plan_from_doc", no_tz)
    a = env.computer("a", github=False)
    add(a, "Alpha", 30)
    a.replan_now()  # must not raise
    assert "time zone" in a.sync_status.lower()
    assert "reinstall" in a.sync_status.lower() or "rebuild" in a.sync_status.lower()


def test_a_locked_log_does_not_stop_done(env, clock, tmp_path):
    a = env.computer("a", github=False)
    t = add(a, "Alpha", 30)
    locked = tmp_path / "locked-log"
    locked.mkdir()  # opening a folder as a file fails with an OSError, like a log another program holds
    a.log_path = locked
    a.done(t)
    assert "Alpha" not in names(a)
    assert [d.name for d in a.completed] == ["Alpha"]


# ---------- the token's copy in local.json ----------

def _local(env, name):
    return json.loads((env.tmp / name / "FocusTool" / "local.json").read_text(encoding="utf-8"))


def test_token_in_local_json_survives_a_restart_when_the_credential_manager_does_not_keep_it(env, monkeypatch):
    env.tokens["t"] = None

    def refuse(_token):
        raise OSError("A specified logon session does not exist.")

    monkeypatch.setattr(gh_store, "save_token", refuse)
    a = env.computer("a", github=False)
    dlg = env.ft.GitHubDialog(a)
    dlg.owner.setText("me")
    dlg.repo.setText("focus-data")
    dlg.token.setText("test-token")
    assert dlg.save() and a.mode == "github"
    entry = _local(env, "a")["github_token"]
    assert "test-token" not in json.dumps(entry)  # encrypted, not in plain text
    assert gh_store.unprotect_token(entry) == "test-token"
    a.stop_sync()
    a.close()
    again = env.computer("a")  # the exe restarted; the Credential Manager still has nothing
    assert again.mode == "github" and again.saved_token() == "test-token"
    add(again, "Alpha")
    again.sync_now()
    assert remote_names(env.fake) == ["Alpha"]


def test_a_token_in_the_credential_manager_is_copied_into_local_json(env):
    b = env.computer("b")
    assert b.mode == "github"
    assert gh_store.unprotect_token(_local(env, "b")["github_token"]) == "test-token"


def test_a_missing_token_says_so_instead_of_quietly_using_box(env):
    env.tokens["t"] = None
    c = env.computer("c")
    assert c.mode == "box"
    assert "GitHub key missing" in c.sync_status


def test_disconnect_removes_the_token_copy_from_local_json(env):
    d = env.computer("d")
    assert "github_token" in _local(env, "d")
    d.disconnect_github()
    assert "github_token" not in _local(env, "d") and "github" not in _local(env, "d")
