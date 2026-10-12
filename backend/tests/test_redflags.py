"""Independent fictional reports and counterexamples, not clinical validation.

The audit phrases are fixed inputs supplied before these rules existed. Further
examples use colloquial accounts, spelling slips and resolved/negated reports.
They intentionally do not construct their text from safety module constants.
"""

from datetime import datetime, timezone

import pytest

from backend.safety import LIMITATION, check_conversation_urgent, check_urgent
from backend.schemas import Intake, Message, Visit


AUDIT_REPORTS = [
    ("我不想活了，已经想好怎么做了", "crisis_support", "self_harm"),
    ("我一次吃了20片安眠药，现在很难受", "poisoning", "medical"),
    ("刚才眼前一黑晕倒了，现在意识有点模糊", "emergency_call", "medical"),
    ("刚才全身抽搐了两分钟，现在很累", "seizure", "medical"),
    ("咳出来全是血，一口一口的", "severe_haemoptysis", "medical"),
    ("伤口血流不止，止不住", "severe_bleeding", "medical"),
    ("怀孕32周，今天阴道出血还肚子疼", "pregnancy_bleeding", "medical"),
    ("两个月大的宝宝发烧39度5，一直昏睡不吃奶", "infant_warning", "medical"),
    ("摔到头了，之后吐了两次，越来越困", "head_injury", "medical"),
]


@pytest.mark.parametrize("text,source,kind", AUDIT_REPORTS)
@pytest.mark.parametrize("entry", ["chat", "chief_complaint", "notes"])
def test_fixed_independent_audit_reports_reach_the_shared_guard(text, source, kind, entry):
    if entry == "chat":
        risk = check_urgent(text)
    else:
        risk = check_urgent("请帮我整理要带给医生的资料", Intake(**{entry: text}))
    assert risk.level == "emergency", (text, entry, risk)
    assert risk.kind == kind
    assert source in risk.source_ids
    assert LIMITATION in risk.message


@pytest.mark.parametrize("text,source", [
    ("我不想再活下去了", "crisis_support"),
    ("我准备结束自己的生命", "crisis_support"),
    ("我有自伤念头，怕控制不住自己", "crisis_support"),
    ("我打算寻短见，没人知道", "crisis_support"),
    ("我刚刚吞了十几片安眠药", "poisoning"),
    ("一口气吃了二十片止痛药，害怕了", "poisoning"),
    ("药片吃多了，我把今天的药重复吃了", "poisoning"),
    ("刚才昏过去了，还没有醒过来", "emergency_call"),
    ("我晕过去了，现在很难受", "emergency_call"),
    ("家人没反应，喊不醒", "emergency_call"),
    ("刚才跑步时晕倒，现在完全恢复了", "emergency_call"),
    ("我突然晕到地上，身边没人", "emergency_call"),
    ("今天第一次抽风，四肢抽蓄，现在醒了", "seizure"),
    ("家人刚才抽搐，清醒了一会儿又抽搐", "seizure"),
    ("咳出的血不只是血丝，是满口血", "severe_haemoptysis"),
    ("伤口压住了还在流血，衣服都湿了", "severe_bleeding"),
    ("怀孕八个月，下面流血，肚子一直痛", "pregnancy_bleeding"),
    ("刚满月的宝宝高烧，昏睡，喂不进去", "infant_warning"),
    ("2个月宝宝体温39.5，睡着叫不醒", "infant_warning"),
    ("脑袋撞了门框，接连吐，越来越困", "head_injury"),
    ("头部受伤后，反复呕吐", "head_injury"),
])
def test_colloquial_reports_and_spelling_slips_are_not_left_unknown(text, source):
    risk = check_urgent(text)
    assert risk.level == "emergency", (text, risk)
    assert source in risk.source_ids


@pytest.mark.parametrize("text", [
    "我不想死，也不打算伤害自己，只是最近睡不好。",
    "我没有自杀想法，没有自伤念头。",
    "我不想活得这么累了，准备请两天假休息。",
    "我没有服药过量，遵照医生安排每次吃一片。",
    "我正在按处方吃药，每天一片安眠药。",
    "我没有晕倒，只是站起时有一点头晕。",
    "无晕倒，无意识模糊，只是想整理咳嗽的资料。",
    "未抽搐，未服药过量，吃了一片医生开的药。",
    "我没有全身抽搐，只是腿抽筋。",
    "我眼皮抽搐了几下，最近睡得少。",
    "我眼睑抽搐，没有其他不舒服。",
    "伤口没有出血，血已经止住了。",
    "我没咳血，没有咳出来全是血的情况。",
    "怀孕32周，没有阴道出血，肚子也不疼。",
    "两个月宝宝没有发烧，没有昏睡，吃奶正常。",
    "我没有摔到头，晚上困了，想睡觉。",
    "两个月宝宝白天睡觉，醒来吃奶正常。",
    "宝宝出生39天，精神好，吃奶正常。",
    "十年前我不想活了，已经得到帮助；现在只是整理当年的病史。",
    "去年我一次吃了20片安眠药，当时已经就医。",
    "十年前晕倒，意识模糊，当时已就医。现在完全正常。",
    "上周抽搐了两分钟，当时已就医。现在只是归档病史。",
    "前年怀孕时阴道出血肚子疼，当时已处理。",
    "小时候摔到头，之后吐了两次，已经就医。",
    "小说里的角色不想活了，已经想好怎么做了。",
    "我刚刚看了小说，角色准备自杀，这个情节是什么意思？",
    "我想知道自杀念头是什么意思。",
    "请解释为什么过量服药需要立即求助。",
    "一般如何识别抽搐和晕倒？",
    "科普：怀孕32周阴道出血还肚子疼是什么情况？",
    "如果有人摔到头越来越困，一般应该怎么办？",
])
def test_denials_past_events_education_and_ordinary_tiredness_do_not_escalate(text):
    risk = check_urgent(text)
    assert risk.level != "emergency", (text, risk)


@pytest.mark.parametrize("text,source", [
    ("刚才晕倒了，现在完全恢复，没有胸痛也没有呼吸困难", "emergency_call"),
    ("我有癫痫，刚才像平时一样抽搐30秒，现在完全清醒，按医生的急救计划处理了", "seizure"),
    ("咳嗽，痰里一点咳血的血丝，没有其他不舒服", "severe_haemoptysis"),
    ("怀孕8周，阴道少量出血，肚子有一点点疼", "pregnancy_bleeding"),
    ("2个月宝宝发烧38度，精神不错，吃奶正常", "infant_warning"),
    ("4个月宝宝发烧39度，精神不错，吃奶正常", "infant_warning"),
    ("摔到头了，吐了一次，现在完全清醒", "head_injury"),
])
def test_professional_assessment_is_distinct_from_unconfirmed_emergency(text, source):
    risk = check_urgent(text)
    assert risk.level == "prompt", (text, risk)
    assert risk.kind == "medical"
    assert source in risk.source_ids


def test_self_harm_gets_support_and_an_immediate_safety_question():
    risk = check_urgent("我不想活了，已经想好怎么做了")
    assert risk.kind == "self_harm"
    assert "陪" in risk.message
    assert "拉开距离" in risk.message
    assert "已经伤害" in risk.message
    assert "你现在能联系到谁" in risk.message
    assert "命中了" not in risk.message


def test_history_fields_do_not_create_current_red_flags():
    intake = Intake(chief_complaint="轻微咳嗽", medical_history="抽搐、晕倒、头部受伤、曾有自伤念头",
                    medications="曾服药过量", allergies="有过严重反应")
    assert check_urgent("我在整理既往病历", intake).level == "unknown"


def test_separate_form_notes_can_supply_associated_infant_warning_signs():
    risk = check_urgent("想给儿科医生准备资料", Intake(chief_complaint="两个月宝宝发烧39度5", notes="一直昏睡不吃奶"))
    assert risk.level == "emergency"
    assert "infant_warning" in risk.source_ids


def test_explicit_current_report_is_not_hidden_by_educational_context():
    risk = check_urgent("我刚看过抽搐的科普，但我现在全身抽搐了")
    assert risk.level == "emergency"
    assert "seizure" in risk.source_ids


@pytest.mark.parametrize("text", ["持续高热三天不退", "最近发烧39度，3天不退", "发烧三天了，一直不退"])
def test_persistent_fever_gets_professional_review(text):
    risk = check_urgent(text)
    assert risk.level == "prompt"
    assert "fever" in risk.source_ids


def _visit_with_report(text):
    stamp = datetime.now(timezone.utc).isoformat()
    risk = check_urgent(text)
    return Visit(id="fictional-redflag", title="虚构", created_at=stamp, updated_at=stamp, risk=risk,
                 messages=[Message(id="fictional-report", role="user", content=text, created_at=stamp, risk=risk)])


def test_new_self_harm_report_replaces_prior_medical_emergency_message():
    visit = _visit_with_report("我现在胸痛冒冷汗")
    risk = check_conversation_urgent("我不想活了，已经想好怎么做了", visit)
    assert risk.level == "emergency"
    assert risk.kind == "self_harm"
    assert "你现在能联系到谁" in risk.message
    assert "crisis_support" in risk.source_ids


@pytest.mark.parametrize("prior,continuation,source", [
    ("我怀孕32周，阴道出血", "还肚子疼", "pregnancy_bleeding"),
    ("刚刚摔到头了", "越来越困", "head_injury"),
    ("两个月宝宝发烧39度5", "还昏睡不吃奶", "infant_warning"),
])
def test_explicit_linked_warning_continuations_get_recent_report_context(prior, continuation, source):
    risk = check_conversation_urgent(continuation, _visit_with_report(prior))
    assert risk.level == "emergency"
    assert source in risk.source_ids
