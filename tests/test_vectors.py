"""Shared test vectors in tests/vectors/. The JavaScript port (tests/js/core.test.mjs) reads the same files,
so these files are the spec for both languages."""
import json
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import focus_core as fc  # noqa: E402

VEC = Path(__file__).resolve().parent / "vectors"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _cases(name: str, key: str = "cases"):
    data = _load(VEC / name)
    return [pytest.param(c, id=c["description"]) for c in data[key]]


def _files(sub: str):
    return [pytest.param(p, id=p.stem) for p in sorted((VEC / sub).glob("*.json"))]


def _d(s: str) -> date:
    return date.fromisoformat(s)


def test_vector_folders_exist():
    assert len(list((VEC / "merge").glob("*.json"))) >= 14
    assert len(list((VEC / "stamp").glob("*.json"))) >= 8


@pytest.mark.parametrize("path", _files("merge"))
def test_merge_vector(path):
    v = _load(path)
    assert v["description"]
    ab, ba = fc.merge_docs(v["a"], v["b"]), fc.merge_docs(v["b"], v["a"])
    assert ab == v["expected"]
    assert ba == v["expected"]
    assert fc.merge_docs(ab, ab) == ab  # idempotent


@pytest.mark.parametrize("path", _files("stamp"))
def test_stamp_vector(path):
    v = _load(path)
    assert v["description"]
    assert fc.stamp_changes(v["prev"], v["cur"], v["now"]) == v["expected"]


@pytest.mark.parametrize("case", _cases("sections.json"))
def test_sections_vector(case):
    tasks = [fc.task_from_dict(t) for t in case["tasks"]]
    dated, ranked = fc.display_sections(tasks)
    assert [t.id for t in dated] == case["expected"]["dated"]
    assert [t.id for t in ranked] == case["expected"]["ranked"]


@pytest.mark.parametrize("case", _cases("reorder.json"))
def test_reorder_vector(case):
    tasks = [fc.task_from_dict(t) for t in case["tasks"]]
    assert [t.id for t in fc.reorder_ranked(tasks, case["ranked_ids"])] == case["expected"]


@pytest.mark.parametrize("case", _cases("finish_by.json"))
def test_finish_by_vector(case):
    assert fc.finish_by(_d(case["date"]), case["buffer_days"]).isoformat() == case["expected"]


@pytest.mark.parametrize("case", _cases("next_block.json"))
def test_next_block_vector(case):
    now = datetime.fromisoformat(case["now"])
    assert fc.next_block(case["day_plan"], now, set(case["live_ids"])) == case["expected"]


@pytest.mark.parametrize("case", _cases("repeat.json"))
def test_repeat_vector(case):
    assert fc.is_active(fc.task_from_dict(case["task"]), _d(case["day"])) is case["expected"]


@pytest.mark.parametrize("case", _cases("folders.json"))
def test_in_folder_vector(case):
    assert fc.in_folder(fc.task_from_dict(case["task"]), case["path"]) is case["expected"]


@pytest.mark.parametrize("case", _cases("folders.json", "tree_cases"))
def test_folder_tree_vector(case):
    tasks = [fc.task_from_dict(t) for t in case["tasks"]]
    assert [list(x) for x in fc.folder_tree(case["folders"], tasks)] == case["expected"]
