"""Fixed natural-language contrasts from the independent review.

Inputs are literal fictional reports, not text assembled from implementation
patterns. These tests establish product behavior for a finite set, not clinical
sensitivity or specificity.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.agent import Executor
from backend.knowledge import Knowledge
from backend.model import ModelService
from backend.safety import LIMITATION, check_conversation_urgent, check_urgent
from backend.schemas import Intake, Message, Visit


REWORDING_PAIRS = [
    ("右边手脚突然无力", "右边手脚突然没力气", "emergency", "medical", "emergency_stroke"),
    ("右边手脚突然无力", "右边手脚突然使不上劲", "emergency", "medical", "emergency_stroke"),
    ("右边手脚突然无力", "一侧手脚无力", "emergency", "medical", "emergency_stroke"),
    ("我突然口齿不清", "说话也不利索", "emergency", "medical", "emergency_stroke"),
    ("我突然口齿不清", "说话含糊", "emergency", "medical", "emergency_stroke"),
    ("喉舌肿胀，呼吸困难", "嘴唇肿了，呼吸也困难", "emergency", "medical", "emergency_allergy"),
    ("我咯血，只有一点血丝", "咳嗽时痰里带一点血丝", "prompt", "medical", "severe_haemoptysis"),
    ("持续高热四天", "发烧四天了还没退", "prompt", "medical", "fever"),
    ("怀孕12周，阴道出血", "怀孕12周，有一点点出血", "prompt", "medical", "pregnancy_bleeding"),
    ("活着没有意思", "活着没意思", "prompt", "self_harm", "crisis_support"),
    ("不想再坚持下去了", "不想再撑下去了", "prompt", "self_harm", "crisis_support"),
]


def shared_entry(text, entry):
    if entry == "chat":
        return check_urgent(text)
    return check_urgent("请帮我整理就诊材料", Intake(**{entry: text}))


@pytest.mark.parametrize("first,second,level,kind,source", REWORDING_PAIRS)
@pytest.mark.parametrize("entry", ["chat", "chief_complaint", "notes"])
def test_literal_rewording_pairs_keep_the_same_supported_warning(first, second, level, kind, source, entry):
    results = [shared_entry(text, entry) for text in (first, second)]
    for risk in results:
        assert (risk.level, risk.kind) == (level, kind), (entry, first, second, risk)
        assert source in risk.source_ids
        assert LIMITATION in risk.message


QUOTED_OR_HYPOTHETICAL = [
    "如果一个人突然抽搐，旁人该怎么处理？",
    "如果旁边有人抽搐，我该做什么？",
    "遇到有人抽搐该怎么急救？",
    "看到新闻里说有人服药过量被送医",
    "我在书上看到过量服药很危险",
    "群里有人说他孩子抽搐了",
    "群里有人说他孩子今天抽搐了",
    "刚才刷到一条视频，有人晕倒了没人管",
    "我转发一篇报道，有人抽搐后晕倒了",
    "听说有人服药过量，我想了解求助方式",
    "小说里的角色说活着没意思",
    "如果一个人不想再撑下去了，该怎么帮他？",
    "书里写：活着没意思，我已经准备好了。",
]


@pytest.mark.parametrize("text", QUOTED_OR_HYPOTHETICAL)
@pytest.mark.parametrize("entry", ["chat", "chief_complaint", "notes"])
def test_quoted_news_reports_and_hypothetical_instructions_do_not_become_current_events(text, entry):
    risk = shared_entry(text, entry)
    assert risk.level == "unknown", (entry, text, risk)
    assert risk.kind != "self_harm"


@pytest.mark.parametrize("text,source", [
    ("我朋友今天抽搐了，我该劝他做什么", "seizure"),
    ("我刚看过抽搐的新闻，但我朋友今天全身抽搐了", "seizure"),
    ("我刚才遇到有人晕倒了，现在叫不醒", "emergency_call"),
    ("群里有人说他孩子正在抽搐还叫不醒", "seizure"),
    ("群里朋友求助：我妈妈现在晕倒了，没有醒过来", "emergency_call"),
    ("我读了关于抽搐的书，但我现在全身抽搐了", "seizure"),
    ("我看到新闻里有人服药过量，但我刚才一次吃了20片安眠药", "poisoning"),
])
def test_a_real_current_request_for_someone_else_is_not_suppressed_as_a_rumor(text, source):
    risk = check_urgent(text)
    assert risk.level == "emergency"
    assert source in risk.source_ids


@pytest.mark.parametrize("text", [
    "去年右边手脚没力气，已经就医，现在没有不适",
    "我没有一侧手脚无力，也没有说话不利索",
    "我没有觉得活着没意思，也没有自伤想法",
    "我不想活得这么累，打算请假休息",
    "这次游戏我已经准备好了",
    "我已经准备好了",
    "我准备好去看医生了",
    "我右手累了，写字时间太久",
    "我从小右侧没力气，这次只是整理原有病史",
    "我自幼口齿不清，这次只是记录原有情况",
    "我天生一侧手脚无力，想整理既往就诊资料",
    "孕妇，手指擦伤有一点点出血，已经止住了",
    "怀孕12周，牙龈刷牙时有一点点出血",
    "我没有发烧四天还没退，只是今天轻微咳嗽",
])
def test_denial_history_ordinary_preparation_and_known_nonobstetric_bleeding_are_not_reclassified(text):
    risk = check_urgent(text)
    assert risk.level != "emergency", (text, risk)
    assert risk.kind != "self_harm", (text, risk)
    assert "pregnancy_bleeding" not in risk.source_ids, (text, risk)


def test_indirect_support_does_not_assert_a_suicide_plan_or_an_unverified_hotline():
    risk = check_urgent("活着没意思")
    assert risk.level == "prompt" and risk.kind == "self_harm"
    assert "信任的人" in risk.message
    assert "是否安全" in risk.message
    assert "不会仅凭这句话断定" in risk.message
    assert "120" not in risk.message and "12356" not in risk.message


def test_a_new_report_cannot_be_hidden_by_a_lifelong_history():
    assert check_urgent('我从小右手没力气，但今天左边手脚突然无力').level == 'emergency'


@pytest.mark.parametrize("text", [
    "我一侧手脚没有力气", "右侧手脚使不出劲", "嘴唇肿起来了，呼吸同样困难",
])
def test_positive_inability_and_breathing_synonyms_are_not_mistaken_for_negation(text):
    assert check_urgent(text).level == "emergency"


@pytest.mark.parametrize("text", ["发热已经四天了还没退", "发烧4天了没退", "连续高烧五天"])
def test_multiple_day_fever_needs_professional_review_without_invented_emergency(text):
    risk = check_urgent(text)
    assert risk.level == "prompt"
    assert "fever" in risk.source_ids


@pytest.mark.parametrize("text,expected", [
    ("怀孕12周，阴道有一点点出血", "prompt"),
    ("怀孕12周，有一点点出血", "prompt"),
    ("怀孕12周，阴道大量出血，肚子剧烈疼痛", "emergency"),
])
def test_pregnancy_bleeding_preserves_site_and_severity_uncertainty(text, expected):
    risk = check_urgent(text)
    assert risk.level == expected
    assert "pregnancy_bleeding" in risk.source_ids
    if "阴道" not in text:
        assert "部位未明确" in risk.reasons[0]


def prior_visit(text, minutes=0, override_risk=None):
    stamp = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    risk = override_risk or check_urgent(text)
    return Visit(id="fictional-rewording", title="虚构", created_at=stamp, updated_at=stamp, risk=risk,
                 messages=[Message(id="fictional-report", role="user", content=text, created_at=stamp, risk=risk)])


def test_preparation_after_recent_crisis_changes_the_context_but_is_not_a_standalone_trigger():
    assert check_urgent("我已经准备好了").level == "unknown"
    risk = check_conversation_urgent("我已经准备好了", prior_visit("活着没意思"))
    assert (risk.level, risk.kind) == ("emergency", "self_harm")
    assert "crisis_support" in risk.source_ids


@pytest.mark.parametrize("previous,minutes", [
    ("准备看医生要带什么", 0),
    ("小说里的人说活着没意思", 0),
    ("活着没意思", 31),
])
def test_preparation_cannot_borrow_stale_or_quoted_crisis_context(previous, minutes):
    risk = check_conversation_urgent("我已经准备好了", prior_visit(previous, minutes))
    assert risk.level != "emergency"


def test_explicit_action_preparation_in_same_current_crisis_report_is_emergency():
    risk = check_urgent("活着没意思，我已经准备好了")
    assert (risk.level, risk.kind) == ("emergency", "self_harm")


@pytest.mark.parametrize("text", ["我已经准备好了去看医生", "我已经准备好考试了", "我已经准备好去旅游了"])
def test_named_ordinary_preparation_is_not_reinterpreted_even_after_distress(text):
    visit = prior_visit("活着没意思")
    assert check_urgent(text).level == 'unknown'
    risk = check_conversation_urgent(text, visit)
    assert risk is visit.risk  # Retain prior support; no new action inferred.
    assert risk.level == 'prompt', (text, risk)


def test_new_indirect_support_must_not_downgrade_an_existing_medical_emergency():
    risk = check_conversation_urgent("活着没意思", prior_visit("胸口疼，冒冷汗"))
    assert risk.level == "emergency"
    assert risk.kind == "self_harm"
    assert set(risk.source_ids) >= {"emergency_chest", "crisis_support"}
    assert "急救" in risk.message


@pytest.mark.parametrize("entry", ["chat", "chief_complaint", "notes"])
@pytest.mark.parametrize("text,expected,kind", [
    ("右边手脚突然没力气", "emergency", "medical"),
    ("嘴唇肿了，呼吸也困难", "emergency", "medical"),
    ("活着没意思", "prompt", "self_harm"),
    ("如果一个人突然抽搐，旁人该怎么处理？", "unknown", "unknown"),
])
def test_allowlisted_safety_tool_has_the_same_three_entry_guard(text, expected, kind, entry):
    root = Path(__file__).resolve().parents[2]
    executor = Executor(Knowledge(root / "knowledge/articles.json"), ModelService())
    intake = Intake(**{entry: text}) if entry != "chat" else Intake()
    query = text if entry == "chat" else "请帮我整理就诊材料"
    risk = asyncio.run(executor.execute("check_urgent_signs", text=query, intake=intake))
    assert (risk.level, risk.kind) == (expected, kind)
