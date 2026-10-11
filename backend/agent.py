from __future__ import annotations

import time
import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict

from .knowledge import Knowledge
from .model import ModelService, minimal_symptom_payload
from .safety import check_urgent, safe_assistant_output
from .schemas import ChecklistItem, ExtractedDetails, Intake, Risk, Source, Summary, TraceStep, Visit
from .storage import now


FIELD_LABELS = {
    "chief_complaint": "主要不适／就诊原因",
    "onset": "开始时间与变化",
    "age_group": "年龄阶段",
    "severity": "本人填报的程度",
    "medications": "目前使用的药物（本人自述）",
    "allergies": "过敏情况（本人自述）",
    "medical_history": "既往情况（本人自述）",
    "notes": "其他补充（本人自述）",
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
CHECKLIST = [
    ("details", "记录主要不适、开始时间、变化和想问医生的问题"),
    ("medications", "整理目前使用的药物或补充剂资料，供医生核对"),
    ("allergies", "整理已知过敏情况；不清楚的部分明确告诉医生"),
    ("reports", "携带已有检查报告和相关就诊记录（如有）"),
    ("appointment", "通过医院官方渠道核实预约、地点、所需证件及费用"),
]


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


class ChecklistOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[ChecklistItem]


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


class Executor:
    """A fixed, bounded tool plan. Model output cannot choose or invoke tools."""
    ALLOWED = {
        "check_urgent_signs": Risk,
        "search_health_knowledge": SearchOutput,
        "collect_visit_details": DetailsOutput,
        "build_visit_checklist": ChecklistOutput,
        "draft_visit_summary": Summary,
    }

    def __init__(self, knowledge: Knowledge, model: ModelService):
        self.knowledge = knowledge
        self.model = model

    async def execute(self, tool: str, **kwargs):
        if tool not in self.ALLOWED:
            raise ValueError("tool is not in the bounded allowlist")
        handler = getattr(self, "tool_" + tool)
        raw = await handler(**kwargs)
        # Every tool output is schema-validated before the next step sees it.
        return self.ALLOWED[tool].model_validate(raw.model_dump() if isinstance(raw, BaseModel) else raw)

    async def tool_check_urgent_signs(self, text: str, intake: Intake):
        return check_urgent(text, intake)

    async def tool_search_health_knowledge(self, query: str):
        return SearchOutput(sources=self.knowledge.search(query))

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
        return DetailsOutput(intake=visit.intake, user_reports=reports, missing=missing, extracted=extracted, mode=mode)

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
        lines.extend(["", "就诊时待核对：缺失信息、已有检查资料，以及本人希望咨询的问题。"])
        if risk.level == "emergency":
            lines.extend(["", "当前规则提示：" + risk.message])
        return Summary(text="\n".join(lines), generated_at=now())

    async def run(self, visit: Visit, text: str = "", use_model: bool = False, purpose: str = "message") -> AgentResult:
        start = time.monotonic()
        trace: list[TraceStep] = []
        risk = await self.execute("check_urgent_signs", text=text, intake=visit.intake)
        if purpose == "summary" and visit.risk.level == "emergency":
            risk = visit.risk
        trace.append(TraceStep(tool="check_urgent_signs", label="检查当前警示征象", status="done"))
        if risk.level == "emergency" and purpose == "message":
            # This branch must finish before any external request or model wait.
            return AgentResult(risk.message, risk, self.knowledge.by_ids(risk.source_ids), trace, [], "local_emergency", int((time.monotonic() - start) * 1000), None, visit.checklist)

        if purpose == "message" and re.fullmatch(r"(?:你好|您好|嗨|hello|hi|在吗|谢谢|感谢)[\s！!。,.，？?]*", text.strip(), re.I):
            content = "你好，我是知禾。可以帮你整理就诊前想说的症状、查阅有来源的健康资料，并做一份给医生看的自述摘要。你可以先说哪里不舒服、从什么时候开始。\n\n我不提供诊断或用药方案；如有明显紧急情况，请立即联系当地急救。"
            return AgentResult(content, risk, [], trace, ["就诊前要准备什么？", "帮我整理给医生看的摘要"], "local", int((time.monotonic() - start) * 1000), None, visit.checklist)

        treatment_request = purpose == "message" and bool(re.search(
            r"(?:什么药|哪种药|药.*(?:剂量|多少|怎么吃)|开(?:个|点|一张)?(?:药|处方)|诊断|确诊|是什么病|得了什么|能不能停药)", text))
        if treatment_request:
            sources = []
            use_model = False
            trace.append(TraceStep(tool="search_health_knowledge", label="诊断与用药请求不进入自动回答", status="skipped"))

        elif risk.level == "emergency":
            sources = self.knowledge.by_ids(risk.source_ids)
            use_model = False
            trace.append(TraceStep(tool="search_health_knowledge", label="紧急情况下跳过普通知识查询", status="skipped"))
        else:
            found = await self.execute("search_health_knowledge", query=text or visit.intake.chief_complaint or "就诊准备")
            sources = found.sources
            trace.append(TraceStep(tool="search_health_knowledge", label="按人工主题标签检索权威资料", status="done"))
        details = await self.execute("collect_visit_details", visit=visit, text=text, sources=sources, use_model=use_model)
        trace.append(TraceStep(tool="collect_visit_details", label="整理自述并识别待补充信息", status="fallback" if details.mode == "local_fallback" else "done"))
        checklist = await self.execute("build_visit_checklist", visit=visit, risk=risk)
        trace.append(TraceStep(tool="build_visit_checklist", label="建立就诊准备清单", status="done"))
        summary = await self.execute("draft_visit_summary", details=details, risk=risk)
        trace.append(TraceStep(tool="draft_visit_summary", label="依据原始自述草拟就诊摘要", status="done"))
        questions = [FOLLOWUP[key] for key in details.missing if key in FOLLOWUP][:3]
        if treatment_request:
            paragraphs = ["我不能替你诊断、开药、决定剂量或停药。请由医生或药师结合本人情况核对；我可以帮你整理症状、已有用药和需要问的问题，方便就诊时说明。"]
        elif sources:
            paragraphs = ["以下内容仅用于健康教育与就诊准备："]
            paragraphs.extend(source.content for source in sources)
            paragraphs.append("资料：" + "；".join(f"{source.title}（{source.publisher}）" for source in sources))
        else:
            paragraphs = ["当前知识库没有与你的问题对应的已核对资料，无法据此回答这一健康问题。可以继续整理不适、发生时间和想问医生的问题，并向专业医务人员确认。"]
        paragraphs.append(risk.message)
        if questions:
            paragraphs.append("就诊前还可以补充：" + "；".join(questions))
        if details.mode == "model_assisted":
            # Validated fragments affect only a request to confirm, never a claim.
            pieces = details.extracted.symptoms + ([details.extracted.onset] if details.extracted.onset else [])
            if pieces:
                paragraphs.append("可向医生确认的输入片段：" + "、".join(dict.fromkeys(pieces)))
        if details.mode == "local_fallback":
            paragraphs.append("已使用本机整理结果；外部模型不可用、无适用证据或返回内容未通过校验。")
        try:
            content = safe_assistant_output("\n\n".join(paragraphs))
        except ValueError:
            sources = []
            content = "本次资料或模型片段未通过就诊准备输出校验。请向专业医务人员确认。\n\n" + risk.message
            details.mode = "local_fallback"
        return AgentResult(content, risk, sources, trace, questions, details.mode, int((time.monotonic() - start) * 1000), summary, checklist.items)


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
    lines.extend(["", "就诊准备清单："])
    lines.extend(("[x] " if item.done else "[ ] ") + item.text for item in visit.checklist)
    if not visit.checklist:
        lines.append("尚未生成")
    lines.extend(["", "紧急提醒规则：" + visit.risk.message, "", "本摘要不能替代专业评估，不提供诊断或处方。"])
    return "\n".join(lines) + "\n"
