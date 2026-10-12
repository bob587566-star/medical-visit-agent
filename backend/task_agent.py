"""Bounded goal planning, real tool execution, and exact-source evidence.

The external planner chooses a dependency graph, never text, tool arguments,
clinical conclusions, or database writes. Every run uses a private snapshot.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from .planning import CONVERSATION_ORDER, FIELD_LABELS, REQUIRED_FIELDS, conversation_context
from .conversation import QUESTIONS
from .safety import check_conversation_urgent, safe_assistant_output
from .schemas import ChecklistItem, PreparationPlan, Risk, Source, Summary, Visit
from .storage import now
from .knowledge_versions import knowledge_version


TaskGoal = Literal["prepare_visit", "understand_symptom", "organize_timeline"]
TaskTool = Literal["check_urgent_signs", "search_health_knowledge", "collect_visit_details", "build_visit_checklist", "draft_visit_summary", "suggest_intake_updates", "build_preparation_plan"]
MAX_TOOL_CALLS = 8
PLANNER_ENDPOINT = "https://api.deepseek.com/chat/completions"
OBJECTIVES = {
    "prepare_visit": "依据已保存自述和观察，整理就诊摘要、待补充信息与准备清单。",
    "understand_symptom": "查找与本人已描述的不适对应的已核对健康资料，并展示可追溯摘录。",
    "organize_timeline": "按实际记录时间整理本人观察，标出缺少的记录，不解释测量数值。",
}
LABELS = {
    "check_urgent_signs": "先检查紧急警示",
    "search_health_knowledge": "查询对应的已核对资料",
    "collect_visit_details": "读取自述与待补充项目",
    "build_visit_checklist": "草拟就诊准备清单",
    "draft_visit_summary": "草拟自述与观察摘要",
    "suggest_intake_updates": "提取需本人核对的原话片段",
    "build_preparation_plan": "草拟准备步骤与医生提问清单",
}
REQUIRED_TOOLS = {
    "prepare_visit": {"check_urgent_signs", "collect_visit_details", "build_visit_checklist", "draft_visit_summary", "build_preparation_plan"},
    "understand_symptom": {"check_urgent_signs", "collect_visit_details", "search_health_knowledge"},
    "organize_timeline": {"check_urgent_signs", "collect_visit_details", "draft_visit_summary"},
}


class TaskModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class PlannedStep(TaskModel):
    id: str = Field(min_length=1, max_length=40, pattern=r"^[a-z][a-z0-9_-]*$")
    tool: TaskTool
    depends_on: list[str] = Field(default_factory=list, max_length=7)
    label: str = Field(default="", max_length=100)


class TaskPlan(TaskModel):
    objective: str = Field(max_length=300)
    planner_mode: Literal["local", "model", "fallback"] = "local"
    steps: list[PlannedStep] = Field(min_length=1, max_length=MAX_TOOL_CALLS)


class TaskStep(TaskModel):
    id: str
    tool: TaskTool
    label: str
    depends_on: list[str] = Field(default_factory=list)
    status: Literal["pending", "running", "done", "failed", "skipped"] = "pending"
    executed_by: Literal["executor", "local_safety"] = "executor"
    output: dict[str, Any] = Field(default_factory=dict)
    source_ids: list[str] = Field(default_factory=list)
    error: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    duration_ms: int = 0


class EvidenceClause(TaskModel):
    clause: str
    kind: Literal["source", "self_report", "safety_rule"]
    source_id: str | None = None
    reference: str
    exact_span: str
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    verified: bool = True
    notice: str = ""


class TaskRun(TaskModel):
    id: str
    goal: TaskGoal
    status: Literal["completed", "needs_input", "emergency", "failed"]
    created_at: str
    duration_ms: int = 0
    plan: TaskPlan
    steps: list[TaskStep] = Field(default_factory=list, max_length=MAX_TOOL_CALLS)
    content: str
    risk: Risk
    sources: list[Source] = Field(default_factory=list, max_length=8)
    evidence_ledger: list[EvidenceClause] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    next_question: str | None = None
    summary: Summary | None = None
    checklist: list[ChecklistItem] = Field(default_factory=list)
    preparation_plan: PreparationPlan | None = None
    planner_notice: str = ""
    planner_error: Literal["unavailable", "timeout", "network_error", "invalid_plan"] | None = None
    knowledge_version: str = ""


class _ModelStep(TaskModel):
    id: str = Field(min_length=1, max_length=40, pattern=r"^[a-z][a-z0-9_-]*$")
    tool: TaskTool
    depends_on: list[str] = Field(default_factory=list, max_length=7)


class _ModelPlan(TaskModel):
    steps: list[_ModelStep] = Field(min_length=1, max_length=MAX_TOOL_CALLS)


def _ordered_plan(plan: TaskPlan, goal: TaskGoal) -> TaskPlan:
    """Validate both a DAG and the semantic dependencies needed by tools."""
    index = {step.id: step for step in plan.steps}
    tools = {step.tool: step for step in plan.steps}
    if len(index) != len(plan.steps) or len(tools) != len(plan.steps):
        raise ValueError("duplicate_steps")
    if not REQUIRED_TOOLS[goal].issubset(tools):
        raise ValueError("missing_goal_tools")
    safety = tools["check_urgent_signs"]
    if safety.depends_on:
        raise ValueError("safety_cannot_wait")
    for step in plan.steps:
        if len(set(step.depends_on)) != len(step.depends_on) or any(dependency not in index or dependency == step.id for dependency in step.depends_on):
            raise ValueError("invalid_dependency")
    ordered: list[PlannedStep] = []
    remaining = list(plan.steps)
    done: set[str] = set()
    while remaining:
        ready = [step for step in remaining if set(step.depends_on).issubset(done)]
        if not ready:
            raise ValueError("cyclic_dependency")
        # Safety always executes first even if an untrusted model lists it last.
        step = safety if safety in ready and safety.id not in done else ready[0]
        ordered.append(step)
        remaining.remove(step)
        done.add(step.id)

    def ancestors(step: PlannedStep) -> set[str]:
        found = set(step.depends_on)
        for dependency in step.depends_on:
            found.update(ancestors(index[dependency]))
        return found

    for step in ordered:
        if step.tool != "check_urgent_signs" and safety.id not in ancestors(step):
            raise ValueError("step_does_not_depend_on_safety")
        if step.tool == "draft_visit_summary" and tools["collect_visit_details"].id not in ancestors(step):
            raise ValueError("summary_needs_details")
        if step.tool == "build_preparation_plan" and "collect_visit_details" in tools and tools["collect_visit_details"].id not in ancestors(step):
            raise ValueError("preparation_needs_details")
    return plan.model_copy(update={"steps": ordered})


def _local_plan(goal: TaskGoal, mode: Literal["local", "fallback"] = "local") -> TaskPlan:
    specifications: list[tuple[str, TaskTool, list[str]]] = [("safety", "check_urgent_signs", [])]
    if goal == "prepare_visit":
        specifications.extend([
            ("sources", "search_health_knowledge", ["safety"]),
            ("details", "collect_visit_details", ["safety", "sources"]),
            ("checklist", "build_visit_checklist", ["details"]),
            ("summary", "draft_visit_summary", ["details"]),
            ("preparation", "build_preparation_plan", ["details", "checklist"]),
        ])
    elif goal == "understand_symptom":
        specifications.extend([
            ("details", "collect_visit_details", ["safety"]),
            ("sources", "search_health_knowledge", ["details"]),
        ])
    else:
        specifications.extend([
            ("details", "collect_visit_details", ["safety"]),
            ("summary", "draft_visit_summary", ["details"]),
        ])
    return TaskPlan(objective=OBJECTIVES[goal], planner_mode=mode, steps=[PlannedStep(id=identifier, tool=tool, depends_on=dependencies, label=LABELS[tool]) for identifier, tool, dependencies in specifications])


class TaskEngine:
    def __init__(self, executor, model_service, *, planner_timeout: float = 8.0, tool_timeout: float = 10.0):
        self.executor = executor
        self.model_service = model_service
        self.planner_timeout = planner_timeout
        self.tool_timeout = tool_timeout

    async def _plan(self, visit: Visit, goal: TaskGoal, use_model: bool) -> tuple[TaskPlan, str | None]:
        if not use_model:
            return _local_plan(goal), None
        model = self.model_service
        if not getattr(model, "available", False):
            return _local_plan(goal, "fallback"), "unavailable"
        # Only metadata with bounded server-owned keys crosses this boundary.
        # The full library IDs are public and do not reveal symptom vocabulary.
        metadata = {
            "goal": goal,
            "filled_fields": [key for key in REQUIRED_FIELDS if getattr(visit.intake, key) not in {"", "未填写"}],
            "observation_count": len(visit.observations),
            "public_source_ids": [source.id for source in self.executor.knowledge.all()],
        }
        prompt = (
            "为一个只读就诊准备任务选择真实工具和依赖。只输出JSON对象steps，最多8项；"
            "每项只有id、tool、depends_on。id唯一且是小写英文标识，tool不得重复。"
            "工具只能从以下清单选择：" + ",".join(LABELS) + "。"
            "必须含check_urgent_signs且它不能有依赖，所有其他项直接或间接依赖它。"
            "draft_visit_summary必须依赖collect_visit_details，build_preparation_plan也必须依赖collect_visit_details。"
            "当前目标必需工具：" + ",".join(sorted(REQUIRED_TOOLS[goal])) + "。"
            "不输出参数、医学文案、诊断、处方或写入操作。只基于提供的目标及填写状态规划。"
            "json格式示例：" + json.dumps({'steps': [step.model_dump(include={'id', 'tool', 'depends_on'}) for step in _local_plan(goal).steps]}, ensure_ascii=False)
        )
        try:
            response = await asyncio.wait_for(model.client.post(
                PLANNER_ENDPOINT,
                headers={"Authorization": "Bearer " + model.api_key, "Content-Type": "application/json"},
                json={"model": model.model, "thinking": {"type": "disabled"}, "temperature": 0, "max_tokens": 800, "response_format": {"type": "json_object"}, "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": json.dumps(metadata, ensure_ascii=False)}]},
                timeout=self.planner_timeout,
                follow_redirects=False,
            ), timeout=self.planner_timeout)
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            if not isinstance(content, str) or len(content) > 20000:
                raise ValueError("invalid_planner_response")
            proposed = _ModelPlan.model_validate(json.loads(content))
            plan = TaskPlan(objective=OBJECTIVES[goal], planner_mode="model", steps=[PlannedStep(**step.model_dump(), label=LABELS[step.tool]) for step in proposed.steps])
            return _ordered_plan(plan, goal), None
        except (asyncio.TimeoutError, httpx.TimeoutException):
            return _local_plan(goal, "fallback"), "timeout"
        except httpx.HTTPError:
            return _local_plan(goal, "fallback"), "network_error"
        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
            return _local_plan(goal, "fallback"), "invalid_plan"

    def _query(self, visit: Visit, goal: TaskGoal) -> str:
        if goal == "prepare_visit":
            return "就诊准备"
        if goal == "organize_timeline":
            return "记录症状"
        # This goal has no free-form query argument. Unsupported institutional
        # or prescribing text remains unsupported by the knowledge gate.
        return conversation_context(visit).get("chief_complaint", "")

    def _verified_sources(self, sources: list[Source]) -> list[Source]:
        bounded = sources[:3]
        official = {source.id: source for source in self.executor.knowledge.by_ids([source.id for source in bounded])}
        for source in bounded:
            if source.id not in official or source.model_dump() != official[source.id].model_dump():
                raise ValueError("evidence_not_in_library")
            safe_assistant_output(source.content)
        return bounded

    async def _execute(self, step: PlannedStep, visit: Visit, goal: TaskGoal, risk: Risk, outputs: dict[str, Any]):
        # A fresh deep copy per call makes even an unexpected mutating tool
        # unable to change saved intake or influence a later step's snapshot.
        snapshot = visit.model_copy(deep=True)
        snapshot.risk = risk.model_copy(deep=True)
        sources = next((output.sources for output in outputs.values() if hasattr(output, "sources")), [])
        if step.tool == "check_urgent_signs":
            return await self.executor.execute(step.tool, text="", intake=snapshot.intake, visit=snapshot)
        if step.tool == "search_health_knowledge":
            output = await self.executor.execute(step.tool, query=self._query(snapshot, goal))
            return output.model_copy(update={"sources": self._verified_sources(output.sources)})
        if step.tool == "collect_visit_details":
            return await self.executor.execute(step.tool, visit=snapshot, text="", sources=sources, use_model=False)
        if step.tool == "build_visit_checklist":
            return await self.executor.execute(step.tool, visit=snapshot, risk=risk)
        if step.tool == "draft_visit_summary":
            details = next((output for output in outputs.values() if hasattr(output, "user_reports") and hasattr(output, "intake")), None)
            if details is None:
                raise ValueError("missing_details_output")
            return await self.executor.execute(step.tool, details=details.model_copy(deep=True), risk=risk)
        if step.tool == "suggest_intake_updates":
            text = next((message.content for message in reversed(snapshot.messages) if message.role == "user"), "")
            return await self.executor.execute(step.tool, visit=snapshot, text=text)
        if step.tool == "build_preparation_plan":
            return await self.executor.execute(step.tool, visit=snapshot)
        raise ValueError("tool_not_allowed")

    @staticmethod
    def _source_evidence(sources: list[Source]) -> list[EvidenceClause]:
        evidence: list[EvidenceClause] = []
        for source in sources:
            for match in list(re.finditer(r"[^。！？\n]+[。！？]?", source.content))[:2]:
                span = match.group()
                evidence.append(EvidenceClause(clause=span, kind="source", source_id=source.id, reference=source.url, exact_span=span, start=match.start(), end=match.end(), notice="公共资料原文摘录；仅核对出处，不代表个人诊断。"))
        return evidence

    @staticmethod
    def _self_evidence(visit: Visit) -> list[EvidenceClause]:
        evidence: list[EvidenceClause] = []
        for key, value in visit.intake.model_dump().items():
            if value and value != "未填写":
                evidence.append(EvidenceClause(clause=value, kind="self_report", reference="intake." + key, exact_span=value, start=0, end=len(value), notice="本人已填写的自述，未经医学核实。"))
        for message in visit.messages:
            if message.role == "user":
                evidence.append(EvidenceClause(clause=message.content, kind="self_report", reference="message." + message.id, exact_span=message.content, start=0, end=len(message.content), notice="对话原文，未自动写入本人表单。"))
        for observation in visit.observations:
            evidence.append(EvidenceClause(clause=observation.value, kind="self_report", reference="observation." + observation.id, exact_span=observation.value, start=0, end=len(observation.value), notice="本人观察原始记录；不解释数值。"))
        return evidence

    async def run(self, visit: Visit, goal: str, use_model: bool = False) -> TaskRun:
        if goal not in OBJECTIVES:
            raise ValueError("unknown_task_goal")
        started = time.monotonic()
        snapshot = visit.model_copy(deep=True)
        stamp = now()
        source_version = knowledge_version(self.executor.knowledge)
        identifier = uuid.uuid4().hex
        # This synchronous local gate precedes planning, retrieval and all I/O.
        risk = check_conversation_urgent("", snapshot)
        if snapshot.risk.level == "emergency":
            risk = snapshot.risk
        if risk.level == "emergency":
            safety = PlannedStep(id="safety", tool="check_urgent_signs", label=LABELS["check_urgent_signs"])
            sources = self._verified_sources(self.executor.knowledge.by_ids(risk.source_ids))
            return TaskRun(id=identifier, goal=goal, status="emergency", created_at=stamp, duration_ms=int((time.monotonic() - started) * 1000), plan=TaskPlan(objective="优先处理当前紧急警示，暂停其他任务。", steps=[safety]), steps=[TaskStep(**safety.model_dump(), status="done", executed_by="local_safety", output=risk.model_dump(), source_ids=risk.source_ids, started_at=stamp, finished_at=now())], content=risk.message, risk=risk, sources=sources, evidence_ledger=[EvidenceClause(clause=risk.message, kind="safety_rule", reference="local.emergency_gate", exact_span=risk.message, start=0, end=len(risk.message), notice="本机有限紧急规则提示；不能覆盖全部紧急情况。")])

        plan, planner_error = await self._plan(snapshot, goal, use_model)
        plan = _ordered_plan(plan, goal)
        results: list[TaskStep] = []
        outputs: dict[str, Any] = {}
        by_id: dict[str, TaskStep] = {}
        for specification in plan.steps:
            result = TaskStep(**specification.model_dump())
            results.append(result)
            by_id[result.id] = result
            if any(by_id[dependency].status != "done" for dependency in result.depends_on):
                result.status = "skipped"
                result.error = "dependency_not_completed"
                continue
            result.status = "running"
            result.started_at = now()
            step_started = time.monotonic()
            try:
                output = await asyncio.wait_for(self._execute(specification, snapshot, goal, risk, outputs), timeout=self.tool_timeout)
                if specification.tool == "check_urgent_signs":
                    risk = output
                result.output = output.model_dump(mode="json")
                result.source_ids = [source.id for source in output.sources] if hasattr(output, "sources") else []
                outputs[result.id] = output
                result.status = "done"
            except asyncio.TimeoutError:
                result.status, result.error = "failed", "tool_timeout"
            except Exception:
                # Errors carry a safe category, never raw patient inputs,
                # model content, response bodies, credentials, or exception text.
                result.status, result.error = "failed", "tool_output_unavailable"
            result.finished_at = now()
            result.duration_ms = int((time.monotonic() - step_started) * 1000)
            if risk.level == "emergency":
                break

        source_output = next((output for output in outputs.values() if hasattr(output, "sources")), None)
        sources = source_output.sources if source_output else []
        summary = next((output for output in outputs.values() if isinstance(output, Summary)), None)
        checklist_output = next((output for output in outputs.values() if hasattr(output, "items")), None)
        preparation = next((output for output in outputs.values() if isinstance(output, PreparationPlan)), None)
        missing = [key for key in REQUIRED_FIELDS if getattr(snapshot.intake, key) in {"", "未填写"}]
        context = conversation_context(snapshot)
        next_field = next((key for key in CONVERSATION_ORDER if key not in context), None)
        question = QUESTIONS[next_field] if next_field else ("请核对尚未确认的聊天片段，再写入就诊资料。" if missing else None)
        failed = any(step.status in {"failed", "skipped"} for step in results)
        status = "failed" if failed else "completed"
        evidence = self._source_evidence(sources)
        if risk.level == "emergency":
            status, content, question = "emergency", risk.message, None
            evidence.append(EvidenceClause(clause=risk.message, kind="safety_rule", reference="local.emergency_gate", exact_span=risk.message, start=0, end=len(risk.message)))
        elif goal == "understand_symptom":
            if not sources:
                status = "failed" if failed else "needs_input"
                content = "当前没有与所述不适对应的已核对资料，无法据此作医学解释。已保留原始自述，不推测病因或用药。"
                question = "你想了解哪种不适？可以补充具体描述，或将问题留给专业医务人员核对。"
            else:
                content = "以下是对应资料的原文摘录，不能据此确定个人情况：\n\n" + "\n\n".join(f"{''.join(entry.clause for entry in evidence if entry.source_id == source.id)}\n出处：{source.publisher}《{source.title}》" for source in sources)
                if missing and not failed:
                    status = "needs_input"
        elif goal == "organize_timeline":
            evidence.extend(self._self_evidence(snapshot))
            content = summary.text if summary else "本次观察摘要未能完成；没有更改原始记录。"
            question = None
            if not snapshot.observations:
                status = "failed" if failed else "needs_input"
                question = "要记录哪条观察？请补充实际观察时间、内容，以及测量单位（如有）。"
        else:
            evidence.extend(self._self_evidence(snapshot))
            content = "已草拟就诊准备材料；以下本人自述尚需核对。"
            if summary:
                content += "\n\n" + summary.text
            if preparation:
                content += "\n\n准备步骤：\n" + "\n".join(f"• {step.title}：{step.detail}" for step in preparation.steps)
            if missing and not failed:
                status = "needs_input"
                content += "\n\n待补充或核对：" + "、".join(FIELD_LABELS[key] for key in missing) + "。"
        if status == "failed":
            content = "部分任务步骤未能完成，已停止依赖这些结果的步骤。可查看实际执行记录；原始资料没有更改。\n\n" + content
        return TaskRun(id=identifier, goal=goal, status=status, created_at=stamp, duration_ms=int((time.monotonic() - started) * 1000), plan=plan, steps=results, content=content, risk=risk, sources=sources, evidence_ledger=evidence, missing_fields=missing, next_question=question if status == "needs_input" else None, summary=summary, checklist=checklist_output.items if checklist_output else [], preparation_plan=preparation, planner_notice="外部规划不可用或计划未通过校验，已按本机规则执行。" if planner_error else "", planner_error=planner_error, knowledge_version=source_version)
