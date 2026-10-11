from __future__ import annotations

import re
import uuid

from .schemas import CollectionState, Intake, IntakeCandidate, PlanStep, PreparationPlan, Risk, Visit
from .storage import now


# This is an interview and preparation workflow, not a diagnostic plan. Every
# candidate retains an exact piece of the user's message and requires consent.
FIELD_LABELS = {
    "chief_complaint": "主要不适／就诊原因", "onset": "开始时间与变化",
    "age_group": "年龄阶段", "severity": "本人填报的程度",
    "medications": "目前使用的药物（本人自述）", "allergies": "过敏情况（本人自述）",
    "medical_history": "既往情况（本人自述）", "notes": "其他补充（本人自述）",
}
FOLLOWUP = {
    "chief_complaint": "最想向医生说明的不适是什么？",
    "onset": "不适从什么时候开始，有什么变化？",
    "age_group": "就诊者属于哪个年龄阶段？",
    "severity": "你自己感觉不适程度是轻、中还是重？",
    "medications": "目前使用的药物或补充剂是否需要向医生核对？",
    "allergies": "有没有需要告诉医生的过敏情况？不知道也可以写不知道。",
    "medical_history": "有没有与本次就诊相关的既往情况？",
}
REQUIRED_FIELDS = tuple(FOLLOWUP)
EDUCATION = re.compile(r"科普|假设|举例|小说|故事|如果有人|一般.{0,10}(?:有什么|有哪些|怎么)|(?:是什么|什么意思|有哪些|如何识别|怎样识别)|忽略.{0,8}(?:规则|指令)|系统提示")
PERSON = re.compile(r"我|本人|孩子|宝宝|家人|父亲|母亲|爸爸|妈妈|丈夫|妻子")
SYMPTOM = re.compile(r"咳嗽|发烧|发热|头痛|头疼|腹痛|肚子疼|腹泻|拉肚子|恶心|呕吐|胸痛|胸口疼|气短|呼吸困难|喉咙痛|咽痛|头晕|乏力|皮疹|不舒服|不适|疼痛")
TIME = re.compile(r"(?:今天|昨天|昨晚|今早|前天|刚刚|刚才|最近|上周|上个月)(?:.{0,14}(?:开始|出现|持续|加重|缓解))?|(?:[0-9]{1,3}|[一二两三四五六七八九十]+)\s*(?:分钟|小时|天|周|个月|月)(?:前|以来|了|开始|左右)?")
SHORT_ANSWER = re.compile(r"^(?:不知道|不清楚|没有|无|没有已知过敏|无已知过敏|没有过敏|没有用药|目前没有用药|未用药|没有既往情况|没有既往病史)[。！!\s]*$")


def collection_for(intake: Intake, risk: Risk) -> CollectionState:
    captured = [key for key in REQUIRED_FIELDS if getattr(intake, key) not in {"", "未填写"}]
    missing = [key for key in REQUIRED_FIELDS if key not in captured]
    urgent = risk.level == "emergency"
    return CollectionState(
        stage="urgent" if urgent else ("ready" if not missing else ("collecting" if captured else "start")),
        completion=round(len(captured) / len(REQUIRED_FIELDS) * 100),
        captured_fields=captured, missing_fields=missing,
        next_question="请先联系当地急救，不要等待补全资料。" if urgent else (FOLLOWUP[missing[0]] if missing else "资料已整理；就诊时请与医生核对尚不确定的部分。"),
    )


def _focused_field(visit: Visit) -> str | None:
    last = next((message for message in reversed(visit.messages) if message.role == "assistant"), None)
    if last:
        for question in last.suggested_questions:
            for key, expected in FOLLOWUP.items():
                # Confirmation can advance the form without creating another
                # assistant message. Never let that older question keep the
                # conversation pinned to an already confirmed field.
                if question == expected and getattr(visit.intake, key) in {"", "未填写"}:
                    return key
    # A populated form already identifies a useful next question, even when the
    # visitor started by filling it rather than speaking in chat.
    return next((key for key in REQUIRED_FIELDS if getattr(visit.intake, key) in {"", "未填写"}), None)


def suggest_candidates(visit: Visit, text: str) -> list[IntakeCandidate]:
    text = text.strip()
    if not text or EDUCATION.search(text):
        return []
    focused = _focused_field(visit)
    pending = {(item.field, item.value) for item in visit.intake_candidates}
    result: list[IntakeCandidate] = []

    def add(field: str, value: str, evidence: str):
        if not value or evidence not in text or value == getattr(visit.intake, field) or (field, value) in pending:
            return
        # Free text is copied, never rephrased; the two enumerated fields are
        # mapped only from a direct statement or a relevant short answer.
        if field not in {"age_group", "severity"} and value not in text:
            return
        # The candidate transport schema is capped at 2,000 even though the
        # optional notes form accepts more; no candidate may violate its schema.
        maximum = 500 if field == "onset" else 2000
        if len(value) > maximum:
            return
        result.append(IntakeCandidate(id=uuid.uuid4().hex, field=field, value=value, evidence=evidence, created_at=now()))
        pending.add((field, value))

    # Bare symptom reports such as '咳嗽三天' are allowed, but questions about
    # somebody else's possible disease and negated symptoms are not intake facts.
    clauses = [part.strip() for part in re.split(r"[，,。；;\n]", text) if part.strip()]
    for clause in clauses:
        symptom = SYMPTOM.search(clause)
        denied = bool(symptom and re.search(r"(?:没有|没|无|不伴|否认).{0,8}" + re.escape(symptom.group()), clause))
        historic = bool(re.search(r"曾经|以前|小时候|既往|去年|前年|当时", clause) and not re.search(r"现在|目前|这次|今天", clause))
        if symptom and not denied and not historic:
            if (PERSON.search(clause) or focused == "chief_complaint" or clause.startswith(symptom.group())) and not re.search(r"[?？]|怎么|为什么|是什么病", clause):
                add("chief_complaint", clause, clause)
        time_match = TIME.search(clause)
        if time_match and not denied and not historic and (PERSON.search(clause) or SYMPTOM.search(clause) or focused == "onset"):
            add("onset", time_match.group(), time_match.group())

        # Explicit local declarations. Reading a quoted allergy/medicine source
        # is intentionally insufficient to populate any personal field.
        if re.search(r"[?？]|吗|是否|有没有|能不能", clause):
            continue
        for field, pattern in (
            ("allergies", r"(?:我|本人|孩子|宝宝|家人).{0,25}(?:对.{1,35}过敏|没有(?:已知)?过敏|无已知过敏|过敏情况.{0,25}|不清楚.{0,8}过敏)"),
            ("medications", r"(?:我|本人|孩子|宝宝|家人)(?:目前|现在|正在|最近)?(?:在用|使用|服用|吃着|没有用药|未用药|没有使用药物).{0,80}"),
            ("medical_history", r"(?:我|本人|孩子|宝宝|家人).{0,8}(?:既往|以前|曾经|过去|没有既往病史).{0,80}"),
        ):
            match = re.search(pattern, clause)
            if match:
                add(field, match.group(), match.group())

    direct_age = (
        ("孕产期", r"(?:我|本人)(?:现在|目前)?(?:怀孕|孕期|产后)|(?:我|本人)是孕妇"),
        ("儿童", r"(?:我|本人)(?:是)?(?:儿童|小朋友)|(?:我的|本人照护的)(?:孩子|宝宝)"),
        ("老年人", r"(?:我|本人)(?:是|属于)(?:老年人|老人)|(?:我的|本人照护的)(?:父亲|母亲|家人)是老年人"),
        ("成人", r"(?:我|本人)(?:是|属于)(?:成人|成年人)"),
    )
    for value, pattern in direct_age:
        match = re.search(pattern, text)
        if match and not re.search(r"[?？]|吗|是否|有没有|能不能", text):
            add("age_group", value, match.group())
            break
    if focused == "age_group" and text.rstrip("。！! ") in {"成人", "成年人", "儿童", "老年人", "老人", "孕产期", "孕期"}:
        raw = text.rstrip("。！! ")
        add("age_group", {"成年人": "成人", "老人": "老年人", "孕期": "孕产期"}.get(raw, raw), raw)

    for value, wording in (("重", r"严重|重"), ("中", r"中等|中度|中"), ("轻", r"轻微|轻度|轻")):
        explicit = re.search(r"(?:我|本人)(?:觉得|感觉|认为)(?:程度)?(?:是|比较|很)?(" + wording + r")", text)
        short = re.fullmatch(r"(?:程度(?:是|为)?)?(" + wording + r")[。！!\s]*", text) if focused == "severity" else None
        match = explicit or short
        if match and not re.search(r"[?？]|吗|是否|有没有|能不能", text):
            add("severity", value, match.group())
            break
    if focused in {"allergies", "medications", "medical_history"} and (SHORT_ANSWER.fullmatch(text) or (len(text) <= 150 and not re.search(r"[?？]|吗|是否|能不能|有没有|怎么|为什么|能吃|是.{0,4}病", text))):
        # A free-form answer to the one active question retains the whole text;
        # '没有' means only the field asked, not all history or symptoms.
        add(focused, text, text)
    return result[:8]


def preparation_plan(visit: Visit) -> PreparationPlan:
    collection = collection_for(visit.intake, visit.risk)
    questions = []
    if visit.intake.chief_complaint:
        questions.append("我填报的这些不适，在就诊时还需要补充哪些信息？")
    else:
        questions.append("如何向医生完整说明这次就诊的原因？")
    if not visit.intake.onset:
        questions.append("如果记不清具体开始时间，怎样说明大致时间和变化？")
    if visit.intake.medications:
        questions.append("我目前填报的药物或补充剂资料是否完整，需要核对哪些事项？")
    if visit.intake.allergies:
        questions.append("我填报的过敏情况还需要提供哪些原始记录？")
    if visit.observations:
        questions.append("我记录的症状和测量时间线是否足以说明变化，是否还缺少信息？")
    if visit.intake.age_group in {"儿童", "孕产期"}:
        questions.append("儿童或孕产期的就诊准备和陪同安排，有哪些需要提前核实？")
    questions.append("此次就诊后，哪些情况需要再次联系专业医务人员？")

    steps: list[PlanStep] = []
    if visit.risk.level == "emergency":
        steps.append(PlanStep(id="urgent", title="先处理紧急警示", detail=visit.risk.message, category="information"))
    missing = "、".join(FIELD_LABELS[key] for key in collection.missing_fields)
    steps.append(PlanStep(id="intake", title="核对本人填报", detail="先核对表单和已确认的聊天片段；" + ("仍可补充：" + missing + "。不知道的项目如实说明。" if missing else "资料完整度只表示填写进度，不表示医学上已确认。"), category="information"))
    if visit.observations:
        steps.append(PlanStep(id="timeline", title="带上观察时间线", detail=f"已记录 {len(visit.observations)} 条本人观察。核对测量时间、单位和原始记录，供医生评估；本助手不解释数值含义。", category="documents"))
    steps.extend([
        PlanStep(id="materials", title="整理现有资料", detail="携带已有检查报告、相关就诊记录和药物或补充剂包装／清单；没有的材料不需要凭空补写。", category="documents"),
        PlanStep(id="official", title="通过官方渠道核实安排", detail="向医院官方渠道确认预约时间、地址、证件、陪同安排及检查准备要求；是否空腹等事项以该机构说明为准。", category="appointment"),
        PlanStep(id="discussion", title="带着问题与医生沟通", detail="保存就诊摘要和待提问清单；就诊时先说明最想解决的问题，并请医生核对不确定之处。", category="questions"),
    ])
    goal = "整理本人／照护者的自述、观察和待提问事项，帮助就诊沟通。"
    if visit.intake.chief_complaint:
        goal += " 本人关注（原文，未经医学核实）：" + visit.intake.chief_complaint
    return PreparationPlan(generated_at=now(), source="self_report", goal=goal, questions=questions[:8], steps=steps,
                           notice="这是一份就诊准备计划，不是诊疗方案。未确认的候选信息不作为事实，不提供诊断、处方、剂量或科室判断；紧急警示出现时不要等待完成清单。")
