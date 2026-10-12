"""Fictional conversation regressions; no clinical or external model claims."""

from pathlib import Path

import pytest

from backend.conversation import render_reply
from backend.knowledge import Knowledge
from backend.planning import FOLLOWUP, collection_for, conversation_context, conversation_paused, suggest_candidates
from backend.safety import check_urgent, safe_assistant_output
from backend.schemas import Intake, Message, Visit


ROOT = Path(__file__).resolve().parents[1]


def record(intake=None):
    return Visit(id="fictional-conversation", title="虚构对话", created_at="2026-10-12T00:00:00+00:00", updated_at="2026-10-12T00:00:00+00:00", intake=intake or Intake(), risk=check_urgent(""))


def turn(visit, text, sources=None, treatment_request=False):
    candidates = suggest_candidates(visit, text)
    reply = render_reply(visit, text, visit.risk, sources or [], candidates, treatment_request)
    safe_assistant_output(reply.content)
    # Match the real API: persist pending proposals by field, followed by both
    # messages, while leaving the form untouched until explicit confirmation.
    by_field = {item.field: item for item in visit.intake_candidates}
    by_field.update({item.field: item for item in candidates})
    visit.intake_candidates = list(by_field.values())
    visit.messages.append(Message(id=f"u-{len(visit.messages)}", role="user", content=text, created_at=visit.created_at, risk=visit.risk))
    visit.messages.append(Message(id=f"a-{len(visit.messages)}", role="assistant", content=reply.content, created_at=visit.created_at, risk=visit.risk, followup_field=reply.next_field, suggested_questions=reply.suggested_questions))
    return reply, candidates


def test_seven_turn_conversation_advances_without_confirming_form():
    visit = record()
    inputs = [
        ("头疼想睡觉", "chief_complaint", "onset"),
        ("昨晚开始的", "onset", "severity"),
        ("轻微", "severity", "age_group"),
        ("成人", "age_group", "medications"),
        ("没有用药", "medications", "allergies"),
        ("不知道", "allergies", "medical_history"),
        ("没有既往病史", "medical_history", None),
    ]
    for text, field, next_field in inputs:
        reply, candidates = turn(visit, text)
        assert field in {item.field for item in candidates}
        assert reply.next_field == next_field
        assert len(reply.suggested_questions) == (1 if next_field else 0)
        assert collection_for(visit.intake, visit.risk).completion == 0
        assert visit.intake == Intake()
        assert "确认后我们继续" not in reply.content
        assert "信息不足以判断紧急程度" not in reply.content
    assert conversation_context(visit)["chief_complaint"] == "头疼想睡觉"


def test_first_reply_acknowledges_exact_words_without_inventing_severity():
    reply, _ = turn(record(), "头疼想睡觉")
    assert "头疼想睡觉" in reply.content
    assert "从什么时候开始" in reply.content
    assert len(reply.content) < 120
    assert "轻度头痛" not in reply.content
    assert "休息就好" not in reply.content
    assert "没事" not in reply.content


def test_one_turn_can_answer_multiple_fields_without_seven_repetitions():
    visit = record()
    reply, candidates = turn(visit, "我头疼，昨晚开始，轻微，成人，目前没有用药，没有已知过敏，没有既往病史")
    assert {item.field for item in candidates} == {"chief_complaint", "onset", "severity", "age_group", "medications", "allergies", "medical_history"}
    assert reply.next_field is None
    assert "写入资料前再逐项核对" in reply.content
    assert collection_for(visit.intake, visit.risk).completion == 0


def test_explicit_followup_field_targets_short_answer_even_with_form_gaps():
    visit = record()
    visit.messages.append(Message(id="a", role="assistant", content="有没有过敏？", created_at=visit.created_at, risk=visit.risk, followup_field="allergies"))
    _, candidates = turn(visit, "不知道")
    assert [(item.field, item.value) for item in candidates] == [("allergies", "不知道")]


def test_old_canonical_question_compatibility_and_confirmed_field_skip():
    visit = record()
    visit.messages.append(Message(id="a", role="assistant", content=FOLLOWUP["chief_complaint"], created_at=visit.created_at, risk=visit.risk, suggested_questions=[FOLLOWUP["chief_complaint"]]))
    visit.intake = Intake(chief_complaint="我头疼")
    reply, candidates = turn(visit, "三天前")
    assert [(item.field, item.value) for item in candidates] == [("onset", "三天前")]
    assert reply.next_field == "severity"


def test_enum_unknown_is_not_guessed_but_conversation_can_continue():
    visit = record()
    turn(visit, "我头疼，昨天开始")
    reply, candidates = turn(visit, "不知道")
    assert not any(item.field == "severity" for item in candidates)
    assert reply.next_field == "age_group"
    assert "不需要猜" in reply.content
    reply, _ = turn(visit, "成人")
    assert reply.next_field == "medications"
    assert visit.intake.severity == "未填写"


def test_pause_persists_through_greeting_and_continued_symptom_until_explicit_resume():
    visit = record()
    turn(visit, "我咳嗽")
    for text in ["不想填，别问了", "谢谢", "你好", "我这两天继续咳嗽"]:
        reply, _ = turn(visit, text)
        assert reply.next_field is None
        assert reply.suggested_questions == []
    assert conversation_paused(visit)
    reply, candidates = turn(visit, "继续问我吧")
    assert candidates == []
    # The volunteered "这两天继续咳嗽" supplied an exact onset proposal while
    # collection was paused, so resuming should not repeat that answer.
    assert reply.next_field == "severity"
    assert not conversation_paused(visit)


def test_repetition_complaint_acknowledges_previous_report_without_asking_again():
    visit = record()
    turn(visit, "头疼想睡觉")
    reply, candidates = turn(visit, "刚说过，别重复问")
    assert "头疼想睡觉" in reply.content
    assert reply.next_field is None
    assert candidates == []


@pytest.mark.parametrize("text", ["这家医院地址在哪", "医院预约多少钱", "你是谁", "你能做什么", "你是不是机器人", "我昨天被老板骂了该怎么办？", "我昨天被老板骂了", "可以运动吗", "明天会下雨吗"])
def test_questions_and_nonmedical_dates_are_not_intake_candidates(text):
    visit = record(Intake(chief_complaint="我头疼"))
    reply, candidates = turn(visit, text)
    assert candidates == []
    assert reply.next_field is None or "老板" in text
    assert not any(item.field == "onset" for item in visit.intake_candidates)


def test_medication_question_does_not_file_question_as_medication():
    visit = record(Intake(chief_complaint="我头疼", onset="昨天", severity="轻", age_group="成人"))
    reply, candidates = turn(visit, "你能做什么")
    assert candidates == []
    assert "我是知禾" in reply.content
    assert reply.suggested_questions == []
    reply, candidates = turn(visit, "这个药怎么吃？", treatment_request=True)
    assert candidates == []
    assert reply.next_field is None
    assert "剂量" in reply.content


def test_knowledge_question_uses_short_existing_sentences_and_source_without_form_question():
    visit = record()
    turn(visit, "头疼想睡觉")
    knowledge = Knowledge(ROOT / "knowledge" / "articles.json")
    sources = knowledge.search("头疼需要去医院吗")
    reply, _ = turn(visit, "那需要去医院吗", sources)
    assert sources[0].title in reply.content
    assert "出处" in reply.content
    assert reply.next_field is None
    assert "年龄" not in reply.content
    assert len(reply.content) < 260
    assert "轻微" not in reply.content


def test_cause_question_does_not_invent_an_explanation():
    knowledge = Knowledge(ROOT / "knowledge" / "articles.json")
    reply, _ = turn(record(), "为什么头疼", knowledge.search("头疼"))
    assert "不能只凭症状判断原因" in reply.content
    assert "熬夜导致" not in reply.content
    assert reply.next_field is None


def test_medication_report_is_acknowledged_without_echoing_prescription():
    visit = record(Intake(chief_complaint="我头疼", onset="昨天", severity="轻", age_group="成人"))
    reply, candidates = turn(visit, "我在服用阿莫西林")
    assert any(item.field == "medications" for item in candidates)
    assert "阿莫西林" not in reply.content
    assert reply.next_field == "allergies"
    assert visit.intake.medications == ""


def test_emergency_reply_does_not_ask_for_more_fields():
    visit = record()
    visit.risk = check_urgent("我现在胸痛冒冷汗")
    reply = render_reply(visit, "谢谢", visit.risk, [], [])
    assert "120" in reply.content
    assert reply.next_field is None
    assert reply.suggested_questions == []
