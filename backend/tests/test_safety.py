"""Fictional Chinese-context warning regressions; not clinical validation."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.knowledge import Knowledge
from backend.safety import LIMITATION, check_urgent, safe_assistant_output
from backend.schemas import Intake, Source


@pytest.mark.parametrize("text,source", [
    ("现在胸口疼，冒冷汗，怎么办？", "emergency_chest"),
    ("我现在胸痛。还在冒冷汗。", "emergency_chest"),
    ("我没有发烧，但现在胸痛冒冷汗。", "emergency_chest"),
    ("无胸痛，但我现在严重呼吸困难。", "emergency_breathing"),
    ("我现在无法呼吸。", "emergency_breathing"),
    ("现在喘不过气，怎么处理？", "emergency_breathing"),
    ("我没有胸痛，而且现在喘不上气。", "emergency_breathing"),
    ("家人现在没有反应。", "emergency_call"),
    ("家人现在没有任何反应。", "emergency_call"),
    ("我的家人现在叫不醒，意识不清。", "emergency_call"),
    ("我刚刚突然左侧无力，而且说话不清。", "emergency_stroke"),
    ("今天早上，左侧无力并说话不清，十分钟后恢复，目前没有症状。", "emergency_stroke"),
    ("2小时前我突然右臂无力，现在已经恢复。", "emergency_stroke"),
    ("24小时前我突然右侧麻木，现在已经恢复。", "emergency_stroke"),
    ("我突然口齿不清。", "emergency_stroke"),
    ("我现在嘴角歪了。", "emergency_stroke"),
    ("十年前胸痛冒冷汗，但现在胸痛并且喘不过气。", "emergency_chest"),
    ("去年有过一次胸痛，现在胸痛冒冷汗。", "emergency_chest"),
    ("我现在喉咙肿起来了，同时呼吸困难。", "emergency_allergy"),
    ("现在喉咙肿。呼吸困难。", "emergency_allergy"),
    ("我突然出现极剧烈头痛。", "headache"),
    ("刚刚头痛，痛得受不了。", "headache"),
    ("我现在头痛伴左侧无力。", "headache"),
])
def test_current_or_recent_warning_reports(text, source):
    risk = check_urgent(text)
    assert risk.level == "emergency", (text, risk)
    assert source in risk.source_ids
    assert "120" in risk.message
    assert LIMITATION in risk.message


@pytest.mark.parametrize("text", [
    "我没有胸痛，也没有冷汗，现在只是轻微咽部不适。",
    "我没有左侧无力，也没有说话不清，现在只是咳嗽。",
    "没有意识不清，也没有喉咙肿胀。",
    "无明显胸痛，无冷汗。",
    "我现在没有呼吸困难。",
    "十年前我曾经胸痛冒冷汗，当时已就医。现在没有症状。",
    "十年前我突然胸痛冒冷汗，当时已就医。现在没有症状。",
    "十年前，我突然左侧无力，而且说话不清，当时已经就医。",
    "去年突然极剧烈头痛，现在只是补充过去的经历。",
    "上个月胸痛，现在只是咳嗽，没有冷汗。",
    "科普问题：胸痛伴冷汗为什么需要急救？我本人没有这些症状。",
    "胸痛伴冷汗是什么情况？",
    "故事里的角色突然左侧无力，现在喘不过气，这个情节是什么意思？",
    "假设如果有人突然剧烈头痛，一般应该怎么做？",
    "如何识别突然说话不清的危险信号？",
    "科普胸痛冒冷汗，我现在没有胸痛，也没有冷汗。",
    "我现在剧烈头痛，但不是突然开始的。",
])
def test_denial_history_and_education_are_not_current_red_alert(text):
    risk = check_urgent(text)
    assert risk.level != "emergency", (text, risk)
    assert LIMITATION in risk.message


def test_form_history_is_not_treated_as_current_symptoms():
    intake = Intake(
        chief_complaint="轻微咳嗽",
        medical_history="去年曾胸痛冒冷汗；家人病史有突然左侧无力",
        allergies="过去有过喉咙肿胀和呼吸困难",
        medications="既往住院时意识不清",
    )
    assert check_urgent("我想整理就诊材料", intake).level == "unknown"


def test_current_form_complaint_is_checked_even_if_question_is_general():
    intake = Intake(chief_complaint="现在胸痛伴冷汗")
    assert check_urgent("看医生带什么？", intake).level == "emergency"


@pytest.mark.parametrize("age_group,severity", [("儿童", "轻"), ("孕产期", "中"), ("成人", "重")])
def test_special_groups_and_reported_severity_need_professional_review(age_group, severity):
    risk = check_urgent("轻微咳嗽", Intake(age_group=age_group, severity=severity))
    assert risk.level == "prompt"
    assert LIMITATION in risk.message


def test_unknown_does_not_claim_exclusion_or_safety():
    risk = check_urgent("症状还没有填")
    assert risk.level == "unknown"
    assert "不能排除疾病" in risk.message
    assert "未命中规则不代表安全" in risk.message


@pytest.mark.parametrize("text", [
    "你患有肺炎", "诊断为偏头痛", "你确诊为高血压", "口服500毫克",
    "服用两片布洛芬", "注射5mL", "阿莫西林", "每天500mg",
])
def test_generated_diagnosis_and_medication_instruction_rejected(text):
    with pytest.raises(ValueError):
        safe_assistant_output(text)


def test_preparation_instruction_stays_in_scope():
    text = "请记录实际症状，带上药盒；不要自行改变医生安排。"
    assert safe_assistant_output(text) == text


def test_full_curated_catalog_loads_with_optional_source_review_date():
    root = Path(__file__).resolve().parents[2]
    knowledge = Knowledge(root / "knowledge" / "articles.json")
    assert knowledge.load_error == ""
    assert len(knowledge.all()) >= 13
    assert any(source.source_reviewed_at for source in knowledge.all())
    assert {source.id for source in knowledge.search("咳嗽两天")} == {"cough"}


def test_source_still_forbids_unrecognized_fields():
    with pytest.raises(ValidationError):
        Source(id="fictional", title="虚构", publisher="虚构", url="https://example.invalid", reviewed_at="2026-10-12", content="虚构", diagnosis="肺炎")


@pytest.mark.parametrize("text,allowed,denied", [
    ("我没有咳嗽，也没有发热。", "", "咳嗽"),
    ("没有发热，但咳嗽两天。", "咳嗽", "发热"),
    ("我现在左侧无力。", "左侧无力", ""),
])
def test_minimal_model_payload_preserves_symptom_assertion_scope(text, allowed, denied):
    from backend.model import minimal_symptom_payload

    root = Path(__file__).resolve().parents[2]
    knowledge = Knowledge(root / "knowledge" / "articles.json")
    payload = minimal_symptom_payload(text, knowledge.search(text), knowledge)
    if allowed:
        assert allowed in payload
    else:
        assert payload == ""
    if denied:
        assert denied not in payload


def test_minimal_model_payload_does_not_mistake_inability_for_denial():
    from backend.model import minimal_symptom_payload

    root = Path(__file__).resolve().parents[2]
    knowledge = Knowledge(root / "knowledge" / "articles.json")
    # Exercise the matcher with an explicitly approved alias, independently of
    # the current fixed catalog's limited synonym coverage.
    source = knowledge.by_ids(["emergency_breathing"])[0]
    source = source.model_copy(update={"keywords": source.keywords + ["无法呼吸"]})
    payload = minimal_symptom_payload("我现在无法呼吸。", [source], knowledge)
    assert "无法呼吸" in payload
