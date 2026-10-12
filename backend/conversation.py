"""Small local conversation policy; proposals never become saved intake facts."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .planning import (
    CONVERSATION_ORDER, PAUSE_CONVERSATION, SOCIAL_MESSAGE, SYMPTOM,
    _focused_field, conversation_context, conversation_paused,
)
from .safety import safe_assistant_output
from .schemas import IntakeCandidate, Risk, Source, Visit


QUESTIONS = {
    "chief_complaint": "你现在最想说的是哪里不舒服？",
    "onset": "这种不舒服大概从什么时候开始的？",
    "severity": "你自己感觉不适程度是轻、中，还是重？",
    "age_group": "方便说一下，你属于成人、儿童、老年人，还是孕产期？",
    "medications": "目前有没有在用药物或补充剂？没有也可以直接说。",
    "allergies": "有没有已知的过敏？不清楚也可以说不清楚。",
    "medical_history": "以前有没有相关的病史或就诊经历？",
}
QUESTION_MESSAGE = re.compile(r"[?？]|怎么|为什么|如何|怎么办|危险吗|严重吗|需要.{0,6}(?:就医|看医生)|能不能|是否|介绍|科普|解释|哪些|什么|[吗么][。！!\s]*$")
INSTITUTION_MESSAGE = re.compile(r"(?:医院|门诊|诊所).{0,14}(?:地址|在哪|哪里|位置|电话|收费|多少钱|营业|挂号|预约)|医保|报销|保险|挂号费|预约号|医生排班")
THANKS = re.compile(r"^(?:谢谢(?:你|您)?|感谢(?:你|您)?|多谢|辛苦了)[\s！!。,.，？?]*$")
UNKNOWN_ANSWER = re.compile(r"^(?:不知道|不清楚|记不清|不记得|不确定|暂时不知道)[\s！!。,.，？?]*$")


@dataclass
class ConversationReply:
    content: str
    next_field: str | None = None
    suggested_questions: list[str] = field(default_factory=list)


def _quoted_report(text: str, limit: int = 100) -> str:
    """Quote the actual report, while keeping treatment text out of replies."""
    raw = text.strip()
    try:
        safe_assistant_output(raw)
    except ValueError:
        # These are exact symptom words from the user's report, not inferred
        # diagnoses, severity, causes, or a paraphrase of their medication.
        raw = "、".join(dict.fromkeys(match.group() for match in SYMPTOM.finditer(raw)))
    if not raw:
        return ""
    return raw if len(raw) <= limit else raw[:limit] + "…"


def _spoken_unknowns(visit: Visit) -> set[str]:
    """Remember an explicit '不知道' for enum fields without inventing a value."""
    skipped: set[str] = set()
    active: str | None = None
    for message in visit.messages:
        if message.role == "assistant":
            active = getattr(message, "followup_field", None)
        elif active in {"age_group", "severity"} and UNKNOWN_ANSWER.fullmatch(message.content):
            skipped.add(active)
    return skipped


def _source_excerpt(source: Source, text: str) -> str:
    """Choose at most two existing editorial sentences, without new advice."""
    sentences = [part.strip() for part in re.findall(r"[^。！？\n]+[。！？]?", source.content) if part.strip()]
    if not sentences:
        return ""
    if re.search(r"危险|急救|紧急|什么时候.{0,8}(?:就医|看医生)|需要.{0,8}(?:就医|看医生)", text):
        relevant = [part for part in sentences if re.search(r"急救|立即|及时|应联系|需要评估|需要.*专业|需要.*评估", part)]
    elif re.search(r"怎么(?:办|缓解)|如何缓解|能做什么", text):
        relevant = [part for part in sentences if re.search(r"记录|准备|休息|饮水|联系|评估|求助", part)]
    elif re.search(r"空腹|禁食|抽血", text):
        relevant = [part for part in sentences if re.search(r"空腹|禁食|通知", part)]
    else:
        relevant = sentences
    chosen = (relevant or sentences)[:2]
    # Sentence boundaries preserve conditions on warnings and restrictions.
    # Do not truncate a sentence into an unconditional clinical instruction.
    return "".join(chosen)


def render_reply(
    visit: Visit,
    text: str,
    risk: Risk,
    sources: list[Source],
    candidates: list[IntakeCandidate],
    treatment_request: bool = False,
) -> ConversationReply:
    """Answer the current turn and ask one question using conversation context.

    The caller still owns emergency priority, validation, source cards, candidate
    persistence, and confirmed-only collection state. This function performs no
    I/O and does not update any part of ``visit``.
    """
    text = text.strip()
    if risk.level == "emergency":
        return ConversationReply(risk.message)

    context = conversation_context(visit, candidates)
    skipped = _spoken_unknowns(visit)
    focused = _focused_field(visit)
    if focused in {"severity", "age_group"} and UNKNOWN_ANSWER.fullmatch(text):
        skipped.add(focused)
    next_field = next((key for key in CONVERSATION_ORDER if key not in context and key not in skipped), None)

    if PAUSE_CONVERSATION.search(text):
        if re.search(r"说过|问过|重复", text):
            report = _quoted_report(context.get("chief_complaint", ""))
            content = f"我看到你前面说过“{report}”。" if report else "你已经说过的内容可以接着用。"
            content += "先按已有的话聊，你想补充时再说。"
        else:
            content = "好，先停下这些问题。你可以直接说想聊的内容，也可以让我把已经说过的话整理出来。"
        return ConversationReply(content)

    if THANKS.fullmatch(text):
        return ConversationReply("不客气。需要时可以继续补充，或让我把刚才的话整理成就诊摘要。")

    if re.search(r"你是谁|你(?:能|会|可以)做什么|你(?:是不是|是)机器人|你有什么功能", text):
        return ConversationReply("我是知禾，一个帮你整理就诊信息的助手。可以接着聊你描述的不适，查阅带来源的健康资料，或整理给医生看的摘要和问题清单。")

    paused = conversation_paused(visit, text)
    if SOCIAL_MESSAGE.fullmatch(text):
        content = "你好，我是知禾。" if not context else "你好，我们可以接着聊。"
        if paused:
            return ConversationReply(content + "你可以直接说想聊什么。")
        question = QUESTIONS[next_field] if next_field else "你还想补充什么，或想让我整理成摘要？"
        return ConversationReply(content + question, next_field, [question] if next_field else [])

    if INSTITUTION_MESSAGE.search(text):
        return ConversationReply("我这里没有这家医院的地址、预约或收费资料。需要到该医院的官方网站或官方电话核实；如果你已经有医院通知，可以贴出其中的安排，我帮你整理。")

    if treatment_request:
        content = "我不能仅凭这段描述确定是什么病，也不能给你开药、决定剂量或停药。可以把具体症状和已有用药整理给医生或药师核对。"
        return ConversationReply(content)
    elif QUESTION_MESSAGE.search(text):
        if sources:
            source = sources[0]
            content = _source_excerpt(source, text)
            if re.search(r"为什么|原因|怎么回事", text):
                content = "不能只凭症状判断原因。资料里可以核对的是：" + content
            content += f"\n\n出处：{source.publisher}《{source.title}》。这是一般资料，不能据此确定你的具体情况。"
            return ConversationReply(content)
        else:
            return ConversationReply("这个问题我还没有对应的已核对资料，不能凭猜测回答。你可以换个具体问法，或把问题留给医生核对。")
    elif candidates:
        fields = {item.field for item in candidates}
        if "chief_complaint" in fields:
            report = _quoted_report(next(item.value for item in reversed(candidates) if item.field == "chief_complaint"))
            content = f"你说的是“{report}”，我听到了。" if report else "你提到的不舒服，我听到了。"
        elif "onset" in fields:
            report = _quoted_report(next(item.value for item in reversed(candidates) if item.field == "onset"), 70)
            content = f"明白，时间是“{report}”。" if report else "开始时间这项信息收到。"
        elif "severity" in fields:
            wording = next(item.evidence for item in reversed(candidates) if item.field == "severity")
            report = _quoted_report(wording, 40)
            content = f"明白，你自己感觉“{report}”。" if report else "你对程度的描述收到。"
        elif "age_group" in fields:
            content = "年龄阶段收到。"
        else:
            content = "这项信息收到。"
    elif UNKNOWN_ANSWER.fullmatch(text) and focused:
        content = "没关系，不确定的地方就留作不确定，不需要猜。"
    elif re.fullmatch(r"(?:继续(?:问|聊|填|补充)?|接着(?:问|聊)|可以问|开始填写)[。！!\s]*", text):
        content = "好，我们接着聊。"
    elif context:
        content = "我在听，你可以按自己的话说，不需要照着表格回答。"
    else:
        content = "我在听。你可以说说现在最困扰你的感受，也可以直接问一个具体问题。"

    if risk.level == "prompt":
        content = risk.message.split("。")[0] + "。\n\n" + content
    if paused:
        return ConversationReply(content)
    if next_field:
        question = QUESTIONS[next_field]
        return ConversationReply(content + "\n\n" + question, next_field, [question])
    if candidates:
        content += "\n\n你已经提到的基本情况都接上了。需要时可以让我整理成就诊摘要；写入资料前再逐项核对。"
    return ConversationReply(content)
