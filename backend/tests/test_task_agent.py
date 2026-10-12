"""Goal planner, actual executor and evidence checks using fictional data."""
import asyncio
import json
from pathlib import Path

import httpx
import pytest

from backend.agent import Executor
from backend.knowledge import Knowledge
from backend.model import ModelService
from backend.safety import check_urgent
from backend.schemas import Intake, Visit
from backend.storage import now
from backend.task_agent import TaskEngine


def record(complaint='虚构：咳嗽两天'):
    return Visit(id='fictional', title='虚构任务', created_at=now(), updated_at=now(), intake=Intake(chief_complaint=complaint), risk=check_urgent(''))


def knowledge():
    return Knowledge(Path(__file__).resolve().parents[2] / 'knowledge' / 'articles.json')


def test_local_goals_execute_different_real_tools_and_preserve_original():
    async def scenario():
        visit = record()
        original = visit.model_dump_json()
        engine = TaskEngine(Executor(knowledge(), ModelService()), ModelService())
        prepare = await engine.run(visit, 'prepare_visit')
        assert prepare.status == 'needs_input'
        assert prepare.summary and prepare.preparation_plan and prepare.checklist
        assert all(step.status == 'done' and step.started_at for step in prepare.steps)
        understand = await engine.run(visit, 'understand_symptom')
        assert {source.id for source in understand.sources} == {'cough'}
        assert {step.tool for step in prepare.steps} != {step.tool for step in understand.steps}
        for evidence in understand.evidence_ledger:
            source = next(source for source in understand.sources if source.id == evidence.source_id)
            assert source.content[evidence.start:evidence.end] == evidence.exact_span == evidence.clause
        assert visit.model_dump_json() == original
    asyncio.run(scenario())


def test_no_evidence_cannot_produce_medical_explanation():
    async def scenario():
        engine = TaskEngine(Executor(knowledge(), ModelService()), ModelService())
        result = await engine.run(record('虚构医院的地址和挂号费用'), 'understand_symptom')
        assert result.status == 'needs_input'
        assert result.sources == [] and result.evidence_ledger == []
        assert '无法' in result.content
    asyncio.run(scenario())


def test_emergency_precedes_even_selected_external_planner():
    async def scenario():
        class NoExternal:
            available = True
        visit = record('我现在胸痛冒冷汗')
        result = await TaskEngine(Executor(knowledge(), NoExternal()), NoExternal()).run(visit, 'prepare_visit', use_model=True)
        assert result.status == 'emergency'
        assert len(result.steps) == 1 and result.steps[0].executed_by == 'local_safety'
        assert result.summary is None
        assert '120' in result.content
    asyncio.run(scenario())


VALID_PLAN = {'steps': [{'id': 'safety', 'tool': 'check_urgent_signs', 'depends_on': []}, {'id': 'details', 'tool': 'collect_visit_details', 'depends_on': ['safety']}, {'id': 'sources', 'tool': 'search_health_knowledge', 'depends_on': ['details']}]}


def planned(reply, *, status=200, timeout=False):
    requests = []
    def handler(request):
        requests.append(request)
        if timeout:
            raise httpx.ReadTimeout('fictional', request=request)
        return httpx.Response(status, json={'choices': [{'message': {'content': json.dumps(reply)}}]})
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            model = ModelService('fictional-key', client=client)
            visit = record('虚构：咳嗽。姓名：任务假人；电话13900001234；地址不存在路9号')
            result = await TaskEngine(Executor(knowledge(), model), model).run(visit, 'understand_symptom', use_model=True)
            return result
    return asyncio.run(scenario()), requests


def test_model_plan_metadata_contains_no_report_identity_or_values():
    result, requests = planned(VALID_PLAN)
    assert result.plan.planner_mode == 'model'
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == 'https://api.deepseek.com/chat/completions'
    assert json.loads(request.content)['thinking'] == {'type': 'disabled'}
    metadata = json.loads(json.loads(request.content)['messages'][1]['content'])
    assert set(metadata) == {'goal', 'filled_fields', 'observation_count', 'public_source_ids'}
    assert not any(term in request.content.decode() for term in ('13900001234', '任务假人', '不存在路', '咳嗽。姓名'))
    assert [step.tool for step in result.steps] == ['check_urgent_signs', 'collect_visit_details', 'search_health_knowledge']


@pytest.mark.parametrize('reply', [
    {'steps': [{'id': 'a', 'tool': 'execute_python', 'depends_on': []}]},
    {'steps': [{'id': 'safety', 'tool': 'check_urgent_signs', 'depends_on': ['details']}, {'id': 'details', 'tool': 'collect_visit_details', 'depends_on': ['safety']}, {'id': 'sources', 'tool': 'search_health_knowledge', 'depends_on': ['details']}]},
    {'steps': [{'id': 'safety', 'tool': 'check_urgent_signs', 'depends_on': []}, {'id': 'sources', 'tool': 'search_health_knowledge', 'depends_on': []}]},
    {**VALID_PLAN, 'prescription': 'fictional instruction'},
])
def test_invalid_model_graph_falls_back_without_running_unapproved_steps(reply):
    result, _ = planned(reply)
    assert result.plan.planner_mode == 'fallback'
    assert result.planner_error == 'invalid_plan'
    assert all(step.tool in Executor.ALLOWED for step in result.steps)


@pytest.mark.parametrize('status,timeout,expected', [(500, False, 'network_error'), (200, True, 'timeout')])
def test_planner_outage_keeps_a_working_local_result(status, timeout, expected):
    result, _ = planned(VALID_PLAN, status=status, timeout=timeout)
    assert result.plan.planner_mode == 'fallback' and result.planner_error == expected
    assert result.sources


def test_tool_failure_stops_dependent_steps_and_preserves_saved_report():
    async def scenario():
        class FailingExecutor(Executor):
            async def tool_collect_visit_details(self, **kwargs):
                raise RuntimeError('fictional private exception must not escape')
        visit = record()
        result = await TaskEngine(FailingExecutor(knowledge(), ModelService()), ModelService()).run(visit, 'prepare_visit')
        assert result.status == 'failed'
        assert any(step.status == 'failed' for step in result.steps)
        assert any(step.status == 'skipped' for step in result.steps)
        assert result.summary is None
        assert 'fictional private exception' not in result.model_dump_json()
        assert visit.intake.chief_complaint == '虚构：咳嗽两天'
    asyncio.run(scenario())
