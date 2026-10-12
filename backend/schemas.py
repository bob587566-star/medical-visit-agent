from __future__ import annotations

from typing import Literal
from datetime import datetime, timedelta, timezone
from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Intake(StrictModel):
    chief_complaint: str = Field(default="", max_length=2000)
    onset: str = Field(default="", max_length=500)
    age_group: Literal["未填写", "成人", "儿童", "老年人", "孕产期"] = "未填写"
    severity: Literal["未填写", "轻", "中", "重"] = "未填写"
    medications: str = Field(default="", max_length=2000)
    allergies: str = Field(default="", max_length=2000)
    medical_history: str = Field(default="", max_length=2000)
    notes: str = Field(default="", max_length=3000)


class CreateVisit(StrictModel):
    title: str = Field(default="新的就诊准备", min_length=1, max_length=100)


class MessageRequest(StrictModel):
    content: str = Field(min_length=1, max_length=4000)
    use_model: bool = False

    @field_validator("content")
    @classmethod
    def non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("消息不能为空")
        return value


class SummaryRequest(StrictModel):
    use_model: bool = False


class ChecklistUpdate(StrictModel):
    item_id: str = Field(min_length=1, max_length=100)
    done: bool


class RenameVisit(StrictModel):
    title: str = Field(min_length=1, max_length=100)


class BookmarkUpdate(StrictModel):
    saved: bool


IntakeField = Literal["chief_complaint", "onset", "age_group", "severity", "medications", "allergies", "medical_history", "notes"]


class CollectionState(StrictModel):
    stage: Literal["start", "collecting", "ready", "urgent"] = "start"
    completion: int = Field(default=0, ge=0, le=100)
    missing_fields: list[IntakeField] = Field(default_factory=list, max_length=8)
    next_question: str = Field(default="", max_length=1000)
    captured_fields: list[IntakeField] = Field(default_factory=list, max_length=8)


class IntakeCandidate(StrictModel):
    id: str = Field(min_length=1, max_length=100)
    field: IntakeField
    value: str = Field(min_length=1, max_length=2000)
    evidence: str = Field(min_length=1, max_length=4000)
    created_at: str


class ObservationRequest(StrictModel):
    kind: Literal["symptom", "temperature", "blood_pressure", "blood_glucose", "note"]
    value: str = Field(min_length=1, max_length=300)
    unit: str = Field(default="", max_length=30)
    recorded_at: str = Field(min_length=1, max_length=100)
    notes: str = Field(default="", max_length=1000)

    @field_validator("recorded_at")
    @classmethod
    def valid_time(cls, value):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if not 1900 <= parsed.year <= 2100:
            raise ValueError("记录日期不在支持范围")
        if not parsed.tzinfo:
            parsed = parsed.replace(tzinfo=timezone(timedelta(hours=8)))
        return parsed.isoformat(timespec="seconds")


class Observation(ObservationRequest):
    id: str
    created_at: str


class PlanStep(StrictModel):
    id: str
    title: str = Field(max_length=200)
    detail: str = Field(max_length=3000)
    category: Literal["information", "documents", "appointment", "questions"]


class PreparationPlan(StrictModel):
    generated_at: str
    source: Literal["self_report"] = "self_report"
    goal: str = Field(max_length=3000)
    questions: list[str] = Field(default_factory=list, max_length=20)
    steps: list[PlanStep] = Field(default_factory=list, max_length=20)
    notice: str = Field(max_length=3000)


class Source(StrictModel):
    id: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=300)
    publisher: str = Field(min_length=1, max_length=300)
    url: str = Field(min_length=1, max_length=2000)
    reviewed_at: str = Field(min_length=1, max_length=100)
    source_reviewed_at: str | None = Field(default=None, max_length=100)
    content: str = Field(min_length=1, max_length=8000)
    category: str = Field(default="就诊准备", max_length=100)
    keywords: list[str] = Field(default_factory=list, max_length=100)
    topics: list[str] = Field(default_factory=list, max_length=100)


class Risk(StrictModel):
    level: Literal["emergency", "prompt", "unknown"] = "unknown"
    kind: Literal["medical", "self_harm", "unknown"] = "unknown"
    message: str
    reasons: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)


class TraceStep(StrictModel):
    tool: Literal["check_urgent_signs", "search_health_knowledge", "collect_visit_details", "build_visit_checklist", "draft_visit_summary", "suggest_intake_updates", "build_preparation_plan"]
    label: str
    status: Literal["done", "fallback", "skipped"]


class ChecklistItem(StrictModel):
    id: str
    text: str
    done: bool = False


class Summary(StrictModel):
    text: str
    generated_at: str
    source: Literal["self_report"] = "self_report"


class Message(StrictModel):
    id: str
    role: Literal["user", "assistant"]
    content: str
    created_at: str
    sources: list[Source] = Field(default_factory=list)
    trace: list[TraceStep] = Field(default_factory=list)
    risk: Risk
    suggested_questions: list[str] = Field(default_factory=list)
    mode: str = "local"
    duration_ms: int = 0
    intake_candidates: list[IntakeCandidate] = Field(default_factory=list)
    followup_field: IntakeField | None = None


class Visit(StrictModel):
    id: str
    title: str
    created_at: str
    updated_at: str
    intake: Intake = Field(default_factory=Intake)
    messages: list[Message] = Field(default_factory=list)
    summary: Summary | None = None
    checklist: list[ChecklistItem] = Field(default_factory=list)
    risk: Risk
    collection: CollectionState = Field(default_factory=CollectionState)
    intake_candidates: list[IntakeCandidate] = Field(default_factory=list)
    observations: list[Observation] = Field(default_factory=list)
    plan: PreparationPlan | None = None


class ExtractedDetails(StrictModel):
    """Only bounded exact excerpts may cross the model boundary."""
    symptoms: list[str] = Field(default_factory=list, max_length=6)
    onset: str = Field(default="", max_length=200)
    severity: str = Field(default="", max_length=100)

    @field_validator("symptoms")
    @classmethod
    def bounded_symptoms(cls, values: list[str]) -> list[str]:
        if any(len(value) > 200 for value in values):
            raise ValueError("症状片段过长")
        return values
