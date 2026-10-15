"""Tests for gh_store.py (the desktop GitHub client), run against the fake GitHub server
in tests/fake_github.py. Uses only made-up task names.
"""
import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import focus_core as fc  # noqa: E402
import gh_store as gh  # noqa: E402
from tests.fake_github import start_fake  # noqa: E402


# ---------- fixtures ----------

@pytest.fixture
def fake():
    server = start_fake(token="test-token", owner="me", repo="focus-data")
    yield server
    server.stop()


def make_store(fake, token="test-token", **kw):
    return gh.GitHubStore(owner="me", repo="focus-data", token=token, api=fake.url, **kw)


def doc_with_task(name, estimate=10):
    task = fc.task_to_dict(fc.Task(name=name, estimate_min=estimate))
    return fc.stamp_changes(None, {"version": 3, "plan": {"tasks": [task]}}, fc.now_iso())


# ---------- GitHubStore: get_file / 304 ----------

def test_get_missing_file_returns_text_none():
    server = start_fake(token="t", owner="me", repo="focus-data")
    try:
        store = make_store(server, token="t")
        remote = store.get_file("state.json")
        assert remote.text is None
        assert remote.not_modified is False
    finally:
        server.stop()


def test_get_then_304_with_matching_etag(fake):
    store = make_store(fake)
    store.put_file("state.json", '{"a": 1}', None, "seed")
    first = store.get_file("state.json")
    assert first.etag is not None
    second = store.get_file("state.json", etag=first.etag)
    assert second.not_modified is True


# ---------- GitHubStore: put_file ----------

def test_put_new_file_then_update_with_sha(fake):
    store = make_store(fake)
    sha1 = store.put_file("state.json", '{"n": 1}', None, "first write")
    assert sha1 == gh_store_blob_sha('{"n": 1}')
    sha2 = store.put_file("state.json", '{"n": 2}', sha1, "second write")
    assert sha2 != sha1
    remote = store.get_file("state.json")
    assert json.loads(remote.text) == {"n": 2}


def test_stale_sha_raises_conflict(fake):
    store = make_store(fake)
    sha1 = store.put_file("state.json", '{"n": 1}', None, "first write")
    store.put_file("state.json", '{"n": 2}', sha1, "second write")
    with pytest.raises(gh.Conflict):
        store.put_file("state.json", '{"n": 3}', sha1, "stale write")


def test_missing_sha_on_existing_file_raises_conflict(fake):
    store = make_store(fake)
    store.put_file("state.json", '{"n": 1}', None, "first write")
    with pytest.raises(gh.Conflict):
        store.put_file("state.json", '{"n": 2}', None, "no sha given")


def test_wrong_token_raises_auth_error(fake):
    store = make_store(fake, token="wrong-token")
    with pytest.raises(gh.AuthError):
        store.get_file("state.json")


def test_offline_when_port_closed():
    server = start_fake(token="t", owner="me", repo="focus-data")
    url = server.url
    server.stop()
    store = gh.GitHubStore(owner="me", repo="focus-data", token="t", api=url, timeout=2)
    with pytest.raises(gh.Offline):
        store.get_file("state.json")


def test_rate_limit_raises_rate_limited_with_retry_after(fake):
    store = make_store(fake)
    fake.set_rate_limit(reset_at=9999999999)
    with pytest.raises(gh.RateLimited) as exc:
        store.get_file("state.json")
    assert exc.value.retry_after_s is not None
    assert exc.value.retry_after_s > 0


def test_utf8_emoji_round_trip(fake):
    store = make_store(fake)
    text = json.dumps({"name": "\U0001F310 Gamma \U0001F527"}, ensure_ascii=False)
    sha = store.put_file("state.json", text, None, "emoji")
    remote = store.get_file("state.json")
    assert remote.text == text
    assert remote.sha == sha


def test_dispatch_records_workflow_ref_and_inputs(fake):
    store = make_store(fake)
    store.dispatch(workflow="replan.yml", ref="main", inputs={"mode": "now"})
    assert fake.dispatches == [{"workflow": "replan.yml", "ref": "main", "inputs": {"mode": "now"}}]


def test_check_reports_auth_and_success(fake):
    store = make_store(fake, token="wrong")
    assert "me/focus-data" in store.check()
    good = make_store(fake)
    assert good.check() == "Connected to me/focus-data."


def test_check_warns_when_the_repo_is_public(fake):
    fake.private = False
    text = make_store(fake).check()
    assert text.startswith("Connected to me/focus-data.")
    assert "This repository is public, so anyone can see your list." in text


# ---------- fetch/put day plan ----------

def test_fetch_day_plan_none_when_missing(fake):
    store = make_store(fake)
    assert gh.fetch_day_plan(store) is None


def test_put_then_fetch_day_plan(fake):
    store = make_store(fake)
    plan = {"version": 1, "date": "2026-10-13", "blocks": []}
    gh.put_day_plan(store, plan)
    assert gh.fetch_day_plan(store) == plan
    plan2 = {"version": 1, "date": "2026-10-14", "blocks": []}
    gh.put_day_plan(store, plan2)
    assert gh.fetch_day_plan(store) == plan2


# ---------- SyncEngine ----------

def test_sync_first_time_uploads_local_doc_when_remote_missing(fake, tmp_path):
    store = make_store(fake)
    engine = gh.SyncEngine(store, tmp_path / "cache.json")
    local = doc_with_task("Alpha")
    result = engine.sync(local)
    assert result.status == "ok"
    assert result.pushed is True
    remote = store.get_file("state.json")
    assert json.loads(remote.text)["plan"]["tasks"][0]["name"] == "Alpha"


def test_sync_merges_remote_change_into_local(fake, tmp_path):
    store = make_store(fake)
    engine = gh.SyncEngine(store, tmp_path / "cache.json")
    local = doc_with_task("Alpha")
    first = engine.sync(local)
    assert first.status == "ok"

    # Someone else (the phone) pushes a change directly, bypassing this engine's cache.
    other_store = make_store(fake)
    remote_doc = fc.merge_docs(first.doc, first.doc)
    beta_task = fc.task_to_dict(fc.Task(name="Beta", estimate_min=5))
    remote_doc["plan"]["tasks"].insert(0, beta_task)
    remote_doc["order_updated_at"] = fc.now_iso()
    remote_text = json.dumps(remote_doc, indent=2, ensure_ascii=False)
    remote = other_store.get_file("state.json")
    other_store.put_file("state.json", remote_text, remote.sha, "phone edit")

    second = engine.sync(first.doc)
    assert second.status == "ok"
    names = {t["name"] for t in second.doc["plan"]["tasks"]}
    assert names == {"Alpha", "Beta"}
    assert second.remote_changed is True


def test_sync_concurrent_edits_both_survive_after_conflict(fake, tmp_path):
    store_a = make_store(fake)
    cache_a = tmp_path / "a.json"
    engine_a = gh.SyncEngine(store_a, cache_a)
    local_a = doc_with_task("Alpha")
    engine_a.sync(local_a)

    store_b = make_store(fake)
    cache_b = tmp_path / "b.json"
    engine_b = gh.SyncEngine(store_b, cache_b)
    remote_now = store_b.get_file("state.json")
    doc_b = json.loads(remote_now.text)
    engine_b.sync(doc_b)  # B catches up first, nothing new to push

    # Both sides now edit independently from the same base, and both try to sync.
    doc_a2 = json.loads(json.dumps(doc_b))
    doc_a2["plan"]["tasks"].insert(0, fc.task_to_dict(fc.Task(name="FromA", estimate_min=5)))
    doc_a2["order_updated_at"] = fc.now_iso()

    doc_b2 = json.loads(json.dumps(doc_b))
    doc_b2["plan"]["tasks"].insert(0, fc.task_to_dict(fc.Task(name="FromB", estimate_min=5)))
    doc_b2["order_updated_at"] = fc.now_iso()

    result_a = engine_a.sync(doc_a2)
    assert result_a.status == "ok"
    result_b = engine_b.sync(doc_b2)
    assert result_b.status == "ok"

    final = store_a.get_file("state.json")
    final_doc = json.loads(final.text)
    names = {t["name"] for t in final_doc["plan"]["tasks"]}
    assert "FromA" in names
    assert "FromB" in names
    assert "Alpha" in names


def test_sync_gives_up_after_four_conflicts(fake, tmp_path, monkeypatch):
    store = make_store(fake)
    sleeps = []
    engine = gh.SyncEngine(store, tmp_path / "cache.json", sleep=sleeps.append)

    original_put_file = gh.GitHubStore.put_file

    def always_conflict(self, path, text, sha, message):
        raise gh.Conflict(409, "forced conflict")

    monkeypatch.setattr(gh.GitHubStore, "put_file", always_conflict)
    local = doc_with_task("Alpha")
    result = engine.sync(local)
    assert result.status == "error"
    assert len(sleeps) == 3  # 3 backoffs for 4 attempts
    assert sleeps == [1, 2, 4]


def test_sync_auth_error_status(fake, tmp_path):
    store = make_store(fake, token="wrong")
    engine = gh.SyncEngine(store, tmp_path / "cache.json")
    result = engine.sync(doc_with_task("Alpha"))
    assert result.status == "auth"


def test_sync_offline_status(tmp_path):
    server = start_fake(token="t", owner="me", repo="focus-data")
    url = server.url
    server.stop()
    store = gh.GitHubStore(owner="me", repo="focus-data", token="t", api=url, timeout=2)
    engine = gh.SyncEngine(store, tmp_path / "cache.json")
    result = engine.sync(doc_with_task("Alpha"))
    assert result.status == "offline"


def test_sync_rate_limited_status(fake, tmp_path):
    store = make_store(fake)
    fake.set_rate_limit(reset_at=9999999999)
    engine = gh.SyncEngine(store, tmp_path / "cache.json")
    result = engine.sync(doc_with_task("Alpha"))
    assert result.status == "rate_limited"
    assert result.retry_after_s is not None


# ---------- token storage (fake win32cred) ----------

def test_token_functions_use_win32cred(monkeypatch):
    store = {}

    fake_win32cred = types.SimpleNamespace(
        CRED_TYPE_GENERIC=1,
        CRED_PERSIST_LOCAL_MACHINE=2,
        CredWrite=lambda cred, flags: store.__setitem__(cred["TargetName"], cred["CredentialBlob"]),
        CredRead=lambda target, cred_type: (
            {"CredentialBlob": store[target]} if target in store else (_ for _ in ()).throw(Exception("not found"))
        ),
        CredDelete=lambda target, cred_type: store.pop(target, None),
    )
    monkeypatch.setitem(sys.modules, "win32cred", fake_win32cred)

    assert gh.load_token() is None
    gh.save_token("secret-token")
    assert gh.load_token() == "secret-token"
    gh.delete_token()
    assert gh.load_token() is None


# ---------- helper ----------

def gh_store_blob_sha(text: str) -> str:
    from tests.fake_github import blob_sha
    return blob_sha(text.encode("utf-8"))


# ---------- fix round 1 ----------

def test_save_cache_writes_atomically_and_leaves_no_temp_file(tmp_path):
    store = gh.GitHubStore(owner="me", repo="focus-data", token="t", api="http://127.0.0.1:1")
    cache_path = tmp_path / "cache.json"
    engine = gh.SyncEngine(store, cache_path)

    engine._save_cache("etag1", "sha1", {"version": 3, "a": 1})
    assert json.loads(cache_path.read_text(encoding="utf-8"))["sha"] == "sha1"
    assert [p for p in tmp_path.iterdir() if p != cache_path] == []

    engine._save_cache("etag2", "sha2", {"version": 3, "a": 2})
    assert json.loads(cache_path.read_text(encoding="utf-8"))["sha"] == "sha2"
    assert [p for p in tmp_path.iterdir() if p != cache_path] == []


def test_save_cache_swaps_in_with_os_replace(tmp_path, monkeypatch):
    calls = []
    real_replace = gh.os.replace

    def spy(src, dst):
        calls.append((src, dst))
        return real_replace(src, dst)

    monkeypatch.setattr(gh.os, "replace", spy)
    store = gh.GitHubStore(owner="me", repo="focus-data", token="t", api="http://127.0.0.1:1")
    engine = gh.SyncEngine(store, tmp_path / "cache.json")
    engine._save_cache("e", "s", {"version": 3})
    assert len(calls) == 1
    assert str(calls[0][1]).endswith("cache.json")


def test_get_file_oversized_raises_plain_github_error(fake):
    store = make_store(fake)
    store.put_file("state.json", '{"n": 1}', None, "seed")
    fake.force_oversized("state.json")
    with pytest.raises(gh.GitHubError) as exc:
        store.get_file("state.json")
    assert not isinstance(exc.value, (gh.Conflict, gh.AuthError, gh.Offline, gh.RateLimited))


def test_sync_reports_error_and_never_pushes_when_remote_content_is_unusable(fake, tmp_path):
    store = make_store(fake)
    store.put_file("state.json", json.dumps(fc.migrate_doc({"version": 3, "plan": {"tasks": []}})),
                    None, "seed")
    fake.force_oversized("state.json")
    before = dict(fake.files["state.json"])
    engine = gh.SyncEngine(store, tmp_path / "cache.json")

    result = engine.sync(doc_with_task("Alpha"))

    assert result.status == "error"
    assert result.pushed is False
    assert fake.files["state.json"] == before


def test_remote_changed_false_when_remote_content_is_unchanged_across_full_fetches(fake, tmp_path):
    store = make_store(fake)
    # A "raw" remote doc missing most of the fields migrate_doc fills in with defaults, so a sloppy
    # comparison (raw vs. migrated) would wrongly see this as "changed" on a later, identical fetch.
    minimal_remote = {"version": 3, "plan": {"tasks": []}}
    store.put_file("state.json", json.dumps(minimal_remote), None, "seed minimal")
    local_doc = fc.migrate_doc(minimal_remote)
    cache_path = tmp_path / "cache.json"
    engine = gh.SyncEngine(store, cache_path)

    first = engine.sync(local_doc)
    assert first.pushed is False  # local already matches the (migrated) remote; nothing to push

    # Force a full 200 GET next time instead of a 304, as if the cached ETag had gone stale.
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    cache["etag"] = '"not-the-real-etag"'
    cache_path.write_text(json.dumps(cache), encoding="utf-8")

    second = engine.sync(local_doc)
    assert second.remote_changed is False


def test_protect_token_round_trip_and_plain_fallback():
    entry = gh.protect_token("secret-token")
    assert "dpapi" in entry and "secret-token" not in json.dumps(entry)
    assert gh.unprotect_token(entry) == "secret-token"
    assert gh.unprotect_token({"plain": "abc"}) == "abc"
    assert gh.unprotect_token({"dpapi": "bm90IGEgYmxvYg=="}) is None  # not this user's data
    assert gh.unprotect_token(None) is None and gh.unprotect_token("x") is None
