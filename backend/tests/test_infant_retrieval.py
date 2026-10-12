"""Independent wording checks for age-appropriate editorial evidence.

Retrieving warning material does not diagnose the child or assign visit risk.
No model, network service, or real patient records are used here.
"""
from pathlib import Path

import pytest

from backend.knowledge import Knowledge


@pytest.fixture
def library():
    return Knowledge(Path(__file__).resolve().parents[2] / "knowledge" / "articles.json")


@pytest.mark.parametrize("query", [
    "婴儿 发烧 昏睡",
    "婴儿发热",
    "小月龄婴儿",
    "新生儿",
    "新生儿高烧",
    "刚满一个月的婴儿温度变高了",
    "婴儿很困，不肯吃奶",
    "小月龄发烧需要记录什么？",
    "婴儿高热有哪些需要求助的表现？",
    "新生儿体温升高",
])
def test_infant_wording_reaches_specific_source_without_adult_fever(library, query):
    candidates = {item.id for item in library.candidates(query)}
    results = {item.id for item in library.search(query)}
    assert "infant_warning" in candidates
    assert "infant_warning" in results
    assert "fever" not in candidates
    assert "fever" not in results


@pytest.mark.parametrize("query", ["成人发烧", "成年人的发热怎么记录？", "我30岁，发烧了", "成年人高热"])
def test_adult_fever_does_not_pull_in_infant_advice(library, query):
    results = {item.id for item in library.search(query)}
    assert "fever" in results
    assert "infant_warning" not in results
    assert "child_fever" not in results


def test_age_only_search_remains_conditional_source_information(library):
    result = library.search("小月龄婴儿")
    infant = next(item for item in result if item.id == "infant_warning")
    original = library.by_ids(["infant_warning"])[0]
    assert infant == original
    assert "体温达到" in infant.content
    assert "诊断" not in infant.content
    assert infant.url == "https://www.nhs.uk/symptoms/fever-in-children/"
    assert len(library.all()) == 46


def test_missing_infant_source_is_not_replaced_with_adult_fever(tmp_path, library):
    import json

    path = tmp_path / "without-infant.json"
    items = [item.model_dump(mode="json") for item in library.all() if item.id != "infant_warning"]
    path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    incomplete = Knowledge(path)
    assert incomplete.search("新生儿发热") == []
