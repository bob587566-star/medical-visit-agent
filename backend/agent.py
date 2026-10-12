from __future__ import annotations

import time
import re
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict

from .knowledge import Knowledge
from .model import ModelService, minimal_symptom_payload
from .planning import FIELD_LABELS, FOLLOWUP, collection_for, conversation_context, preparation_plan, suggest_candidates
from .conversation import render_reply
from .safety import check_conversation_urgent, check_urgent, safe_assistant_output
from .schemas import ChecklistItem, CollectionState, ExtractedDetails, Intake, IntakeCandidate, Observation, PreparationPlan, Risk, Source, Summary, TraceStep, Visit
from .storage import now


CHECKLIST = [
    ("details", "记录主要不适、开始时间、变化和想问医生的问题"),
    ("medications", "整理目前使用的药物或补充剂资料，供医生核对"),
    ("allergies", "整理已知过敏情况；不清楚的部分明确告诉医生"),
    ("reports", "携带已有检查报告和相关就诊记录（如有）"),
    ("appointment", "通过医院官方渠道核实预约、地点、所需证件及费用"),
]


def is_treatment_request(text: str) -> bool:
    return bool(re.search(
        r"什么药|哪种药|药.*(?:剂量|多少|怎么吃)|开(?:个|点|一张)?(?:药|处方)|"
        r"(?:开|推荐).{0,15}(?:布洛芬|阿莫西林|对乙酰氨基酚|止痛药|抗生素)|"
        r"诊断|确诊|是什么病|得了什么|能不能停药|"
        r"(?:一次|一天|每天).{0,8}(?:多少|几次|几片|几粒)|多少(?:毫克|微克|mg)|药物剂量",
        text, re.I))


class SearchOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sources: list[Source]


class DetailsOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intake: Intake
    user_reports: list[str]
    missing: list[str]
    extracted: ExtractedDetails | None = None
    mode: Literal["local", "model_assisted", "local_fallback"] = "local"
    observations: list[Observation] = []


class ChecklistOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[ChecklistItem]


class CandidatesOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidates: list[IntakeCandidate]


def ordered_observations(observations: list[Observation]) -> list[Observation]:
    # Normalize only for ordering; the displayed self-report time remains exact.
    def timestamp(item):
        parsed = datetime.fromisoformat(item.recorded_at.replace("Z", "+00:00"))
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()
    return sorted(observations, key=timestamp)


@dataclass
class AgentResult:
    content: str
    risk: Risk
    sources: list[Source]
    trace: list[TraceStep]
    suggested_questions: list[str]
    mode: str
    duration_ms: int
    summary: Summary | None
    checklist: list[ChecklistItem]
    intake_candidates: list[IntakeCandidate] = field(default_factory=list)
    collection: CollectionState = field(default_factory=CollectionState)
    followup_field: str | None = None


class Executor:
    """A fixed, bounded tool plan. Model output cannot choose or invoke tools."""
    ALLOWED = {
        "check_urgent_signs": Risk,
        "search_health_knowledge": SearchOutput,
        "collect_visit_details": DetailsOutput,
        "build_visit_checklist": ChecklistOutput,
        "draft_visit_summary": Summary,
        "suggest_intake_updates": CandidatesOutput,
        "build_preparation_plan": PreparationPlan,
    }

    def __init__(self, knowledge: Knowledge, model: ModelService, retriever=None, safety_review=None):
        self.knowledge = knowledge
        self.model = model
        self.retriever = retriever
        self.safety_review = safety_review

    async def execute(self, tool: str, **kwargs):
        if tool not in self.ALLOWED:
            raise ValueError("tool is not in the bounded allowlist")
        handler = getattr(self, "tool_" + tool)
        raw = await handler(**kwargs)
        # Every tool output is schema-validated before the next step sees it.
        return self.ALLOWED[tool].model_validate(raw.model_dump() if isinstance(raw, BaseModel) else raw)

    async def tool_check_urgent_signs(self, text: str, intake: Intake, visit: Visit | None = None):
        return check_conversation_urgent(text, visit) if visit else check_urgent(text, intake)

    async def tool_search_health_knowledge(self, query: str):
        if self.retriever is None:
            sources = self.knowledge.search(query)
        else:
            result = await self.retriever.search(query)
            sources = result if isinstance(result, list) else result.sources
        return SearchOutput(sources=sources)

    async def tool_suggest_intake_updates(self, visit: Visit, text: str):
        return CandidatesOutput(candidates=suggest_candidates(visit, text))

    async def tool_build_preparation_plan(self, visit: Visit):
        return preparation_plan(visit)

    async def tool_collect_visit_details(self, visit: Visit, text: str, sources: list[Source], use_model: bool):
        values = visit.intake.model_dump()
        missing = [key for key, value in values.items() if not value or value == "未填写"]
        reports = [message.content for message in visit.messages if message.role == "user"]
        if text:
            reports.append(text)
        extracted = None
        mode = "local"
        if use_model:
            # No evidence means no model request; unknown topics fail closed.
            payload = minimal_symptom_payload(text or visit.intake.chief_complaint, sources, self.knowledge) if sources else ""
            extracted = await self.model.extract(payload) if payload else None
            mode = "model_assisted" if extracted else "local_fallback"
        return DetailsOutput(intake=visit.intake, user_reports=reports, missing=missing, extracted=extracted, mode=mode, observations=visit.observations)

    async def tool_build_visit_checklist(self, visit: Visit, risk: Risk):
        existing = {item.id: item.done for item in visit.checklist}
        items = [ChecklistItem(id=key, text=text, done=existing.get(key, False)) for key, text in CHECKLIST]
        if risk.level == "emergency":
            items.insert(0, ChecklistItem(id="urgent", text="立即联系当地急救并请身边的人协助；中国大陆可拨打 120", done=existing.get("urgent", False)))
        if visit.intake.age_group in {"儿童", "孕产期"}:
            items.append(ChecklistItem(id="special", text="向专业医务人员确认儿童或孕产期的专门就诊安排", done=existing.get("special", False)))
        return ChecklistOutput(items=items)

    async def tool_draft_visit_summary(self, details: DetailsOutput, risk: Risk):
        lines = ["就诊准备摘要（仅依据本人／照护者填报和自述，未经医学核实）", ""]
        for key, label in FIELD_LABELS.items():
            value = getattr(details.intake, key)
            lines.append(f"{label}：{value if value and value != '未填写' else '未填写'}")
        lines.extend(["", "对话中的本人／照护者原始自述（以下是原文，未经医学判断）："])
        lines.extend(f"• {report}" for report in details.user_reports)
        if not details.user_reports:
            lines.append("未填写")
        lines.extend(["", "本人观察时间线（原始记录，不作医学解读）："])
        for observation in ordered_observations(details.observations):
            labels = {"symptom": "症状观察", "temperature": "体温记录", "blood_pressure": "血压记录", "blood_glucose": "血糖记录", "note": "补充记录"}
            suffix = "；备注：" + observation.notes if observation.notes else ""
            lines.append(f"• {observation.recorded_at} {labels[observation.kind]}：{observation.value}{(' ' + observation.unit) if observation.unit else ''}{suffix}")
        if not details.observations:
            lines.append("尚无本人观察记录")
        lines.extend(["", "就诊时待核对：缺失信息、已有检查资料，以及本人希望咨询的问题。"])
        if risk.level == "emergency":
            lines.extend(["", "当前规则提示：" + risk.message])
        return Summary(text="\n".join(lines), generated_at=now())

    async def run(self, visit: Visit, text: str = "", use_model: bool = False, purpose: str = "message") -> AgentResult:
        start = time.monotonic()
        trace: list[TraceStep] = []
        risk = await self.execute("check_urgent_signs", text=text, intake=visit.intake, visit=visit)
        # A later greeting or unrelated short reply does not undo a prior
        # urgent warning in this visit. Keep the warning ahead of all I/O.
        if visit.risk.level == "emergency" and risk.kind != 'self_harm':
            risk = visit.risk
        if purpose == 'message' and self.safety_review is not None and risk.level != 'emergency':
            risk = await self.safety_review.review(text, risk)
        trace.append(TraceStep(tool="check_urgent_signs", label="检查当前警示征象", status="done"))
        collection = collection_for(visit.intake, risk)
        if risk.level == "emergency" and purpose == "message":
            # This branch must finish before any external request or model wait.
            return AgentResult(risk.message, risk, self.knowledge.by_ids(risk.source_ids), trace, [], "local_emergency", int((time.monotonic() - start) * 1000), None, visit.checklist, collection=collection)

        if purpose == "message" and re.fullmatch(r"(?:你好|您好|嗨|hello|hi|在吗|谢谢|感谢)[\s！!。,.，？?]*", text.strip(), re.I):
            reply = render_reply(visit, text, risk, [], [])
            return AgentResult(reply.content, risk, [], trace, reply.suggested_questions, "local", int((time.monotonic() - start) * 1000), None, visit.checklist, collection=collection, followup_field=reply.next_field)

        candidates = await self.execute("suggest_intake_updates", visit=visit, text=text)
        trace.append(TraceStep(tool="suggest_intake_updates", label="寻找需本人确认的表单片段", status="done"))

        treatment_request = purpose == "message" and is_treatment_request(text)
        if treatment_request:
            candidates = CandidatesOutput(candidates=[])
            sources = []
            use_model = False
            trace.append(TraceStep(tool="search_health_knowledge", label="诊断与用药请求不进入自动回答", status="skipped"))

        elif purpose == "message" and candidates.candidates and not any(item.field == "chief_complaint" for item in candidates.candidates) and not re.search(r"[?？]|怎么|为什么|如何|怎么办", text):
            # A short answer to the active collection question is workflow
            # input, not a new health question. Do not search unrelated sources.
            sources = []
            use_model = False
            trace.append(TraceStep(tool="search_health_knowledge", label="当前回答用于信息采集，无需知识检索", status="skipped"))

        elif risk.level == "emergency":
            sources = self.knowledge.by_ids(risk.source_ids)
            use_model = False
            trace.append(TraceStep(tool="search_health_knowledge", label="紧急情况下跳过普通知识查询", status="skipped"))
        else:
            query = text or visit.intake.chief_complaint or "就诊准备"
            # Only short, explicit references to this same health conversation
            # may borrow the current reported complaint for retrieval. A new
            # topic, address or prescribing question never borrows old evidence.
            contextual_question = re.fullmatch(
                r"(?:那|那么|这|这样|这种情况|现在)?(?:我)?(?:该|要|应该|需要)?(?:怎么(?:办|处理|做)|注意什么|有什么要注意的|什么时候(?:就医|去医院|看医生)|需要(?:看医生|去医院|就医)(?:吗|么)?|(?:看医生|去医院|就医)(?:吗|么))",
                text.strip().strip("？?。！!"),
            ) if text else None
            if contextual_question:
                chief = conversation_context(visit).get("chief_complaint", "")
                if chief:
                    query = chief + "；" + text
            found = await self.execute("search_health_knowledge", query=query)
            sources = found.sources
            trace.append(TraceStep(tool="search_health_knowledge", label="在本机检索有来源的健康教育资料" if self.retriever is not None else "按人工主题标签检索权威资料", status="done"))
        details = await self.execute("collect_visit_details", visit=visit, text=text, sources=sources, use_model=use_model)
        trace.append(TraceStep(tool="collect_visit_details", label="整理自述并识别待补充信息", status="fallback" if details.mode == "local_fallback" else "done"))
        checklist = await self.execute("build_visit_checklist", visit=visit, risk=risk)
        trace.append(TraceStep(tool="build_visit_checklist", label="建立就诊准备清单", status="done"))
        summary = await self.execute("draft_visit_summary", details=details, risk=risk)
        trace.append(TraceStep(tool="draft_visit_summary", label="依据原始自述草拟就诊摘要", status="done"))
        reply = render_reply(visit, text, risk, sources, candidates.candidates, treatment_request=treatment_request)
        try:
            content = safe_assistant_output(reply.content)
        except ValueError:
            sources = []
            content = "本次资料或模型片段未通过就诊准备输出校验。请向专业医务人员确认。\n\n" + risk.message
            details.mode = "local_fallback"
        return AgentResult(content, risk, sources, trace, reply.suggested_questions, details.mode, int((time.monotonic() - start) * 1000), summary, checklist.items, intake_candidates=candidates.candidates, collection=collection, followup_field=reply.next_field)


def export_text(visit: Visit) -> str:
    lines = [visit.title, "导出时间：" + now(), ""]
    if visit.summary:
        lines.append(visit.summary.text)
    else:
        lines.append("当前填报资料（本人／照护者自述，未经医学核实）")
        for key, label in FIELD_LABELS.items():
            value = getattr(visit.intake, key)
            lines.append(f"{label}：{value if value and value != '未填写' else '未填写'}")
        reports = [message.content for message in visit.messages if message.role == "user"]
        if reports:
            lines.extend(["", "原始对话自述：", *reports])
        if visit.observations:
            lines.extend(["", "本人观察时间线（不作医学解读）："])
            for item in ordered_observations(visit.observations):
                lines.append(f"• {item.recorded_at} {item.kind}：{item.value} {item.unit}；{item.notes}")
    lines.extend(["", "就诊准备清单："])
    lines.extend(("[x] " if item.done else "[ ] ") + item.text for item in visit.checklist)
    if not visit.checklist:
        lines.append("尚未生成")
    if visit.plan:
        lines.extend(["", "就诊沟通与准备计划：", visit.plan.goal])
        lines.extend(f"• {step.title}：{step.detail}" for step in visit.plan.steps)
        lines.extend(["", "准备向医生提问："])
        lines.extend("• " + question for question in visit.plan.questions)
        lines.append(visit.plan.notice)
    lines.extend(["", "紧急提醒规则：" + visit.risk.message, "", "本摘要不能替代专业评估，不提供诊断或处方。"])
    return "\n".join(lines) + "\n"
