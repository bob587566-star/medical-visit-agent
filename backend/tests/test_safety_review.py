"""Fictional local-review boundary tests, not clinical semantic validation."""
import asyncio
import json

import httpx
import pytest

from backend.safety import EMERGENCY_MESSAGE, SELF_HARM_MESSAGE
from backend.safety_review import LocalSafetyReview
from backend.schemas import Risk


def classification(**changes):
    return {
        'category': 'self_harm', 'urgency': 'emergency',
        'context': 'current', 'evidence': '不想活了', **changes,
    }


def run_review(text='我现在不想活了', reply=None, *, rule=None, enabled=True,
               model='qwen2.5:7b', tags=None, chat_status=200,
               chat_error=None, content=None, response_body=None,
               tags_status=200, tags_error=None):
    requests = []
    if rule is None:
        rule = Risk(level='prompt', kind='medical', message='原有专业求助提示', reasons=['原有理由'], source_ids=['cough'])
    if reply is None:
        reply = classification()

    def handler(request):
        requests.append(request)
        assert request.url.scheme == 'http'
        assert request.url.host == '127.0.0.1'
        assert request.url.port == 11434
        if request.url.path == '/api/tags':
            if tags_error:
                raise tags_error('fictional availability failure', request=request)
            if tags_status == 302:
                return httpx.Response(302, headers={'location': 'https://untrusted.example/test'})
            return httpx.Response(tags_status, json={'models': [{'name': model}]} if tags is None else tags)
        assert request.url.path == '/api/chat' and request.method == 'POST'
        if chat_error:
            raise chat_error('fictional local failure', request=request)
        if chat_status == 302:
            return httpx.Response(302, headers={'location': 'https://untrusted.example/test'})
        return httpx.Response(chat_status, json=response_body if response_body is not None else {
            'message': {'content': json.dumps(reply, ensure_ascii=False) if content is None else content},
        })

    async def scenario():
        # A hostile client default cannot redirect the fixed local endpoint.
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                     base_url='https://untrusted.example', follow_redirects=True,
                                     trust_env=False) as client:
            review = LocalSafetyReview(client, enabled=enabled, model=model)
            before = review.status()
            result = await review.review(text, rule)
            return result, review.status(), before, review.retry_after

    result, status, before, retry_after = asyncio.run(scenario())
    return result, requests, status, before, retry_after, rule


@pytest.mark.parametrize('text', ['我现在不想活了', '', '   '])
def test_disabled_has_no_availability_or_model_io(text):
    result, requests, status, _, _, rule = run_review(text, enabled=False)
    assert result is rule and not requests
    assert status['enabled'] is False and status['available'] is False
    assert status['mode'] == 'rules_only'


def test_rule_emergency_short_circuits_even_if_model_would_downgrade():
    rule = Risk(level='emergency', kind='medical', message=EMERGENCY_MESSAGE,
                reasons=['规则命中'], source_ids=['emergency_chest'])
    result, requests, status, _, _, _ = run_review(reply=classification(urgency='none'), rule=rule)
    assert result is rule and not requests
    assert not status['available']


@pytest.mark.parametrize('text', ['', '  \n  '])
def test_empty_input_does_not_start_local_model(text):
    result, requests, _, _, _, rule = run_review(text)
    assert result is rule and not requests


@pytest.mark.parametrize('category,evidence,source,kind', [
    ('self_harm', '不想活了', 'crisis_support', 'self_harm'),
    ('poisoning', '药吃过量了', 'poisoning', 'medical'),
    ('consciousness', '失去意识', 'fainting', 'medical'),
    ('seizure', '全身抽搐', 'seizure', 'medical'),
    ('severe_bleeding', '血流不止', 'severe_bleeding', 'medical'),
    ('pregnancy_bleeding', '怀孕后阴道出血', 'pregnancy_bleeding', 'medical'),
    ('infant_warning', '宝宝昏睡', 'infant_warning', 'medical'),
    ('head_injury', '撞到头后一直吐', 'head_injury', 'medical'),
    ('breathing', '喘不上气', 'emergency_breathing', 'medical'),
    ('stroke', '左手抬不起来', 'emergency_stroke', 'medical'),
])
def test_current_exact_span_can_only_escalate(category, evidence, source, kind):
    result, requests, status, before, _, _ = run_review(
        '我现在' + evidence, classification(category=category, evidence=evidence))
    assert result.level == 'emergency' and result.kind == kind
    assert result.source_ids == [source]
    assert result.message == (SELF_HARM_MESSAGE if kind == 'self_harm' else EMERGENCY_MESSAGE)
    assert len(requests) == 2 and status['available']
    assert before['mode'] == 'rules_local_review_unavailable'
    assert status['mode'] == 'rules_and_local_review'
    assert '不代表经过临床验证' in status['notice']


@pytest.mark.parametrize('changes', [
    {'urgency': 'none'}, {'urgency': 'uncertain'}, {'category': 'none'},
    {'context': 'negated'}, {'context': 'historical'},
    {'context': 'hypothetical'}, {'context': 'uncertain'},
    {'evidence': ''}, {'evidence': '原文不存在的危险表述'},
    {'evidence': '不想活了。并且胸痛'},
    {'evidence': 'x' * 251}, {'category': 'prescribe'},
    {'urgency': 'diagnosis'}, {'context': 'confirmed_diagnosis'},
    {'write_prescription': True}, {'tool': 'run_shell'},
])
def test_unapproved_or_noncurrent_classification_keeps_exact_original_rule(changes):
    result, _, _, _, _, rule = run_review(reply=classification(**changes))
    assert result is rule


@pytest.mark.parametrize('text,evidence', [
    ('我没有不想活了，最近睡得一般', '不想活了'),
    ('我不打算自伤，最近睡得一般', '自伤'),
    ('去年我不想活了，当时已就医，现在只是整理历史', '不想活了'),
    ('上个月我晕倒了，现在只是整理旧病史', '晕倒了'),
    ('假设有人不想活了，一般怎么支持他？', '不想活了'),
    ('故事中的人物不想活了，解释一下这个情节', '不想活了'),
    ('科普：不想活了是什么感觉？我本人没有这些症状', '不想活了'),
    ('假设我现在不想活了，应该怎么应对？', '不想活了'),
])
def test_local_model_cannot_override_denial_history_or_hypothetical_text(text, evidence):
    # Deliberately adversarial model output: it incorrectly labels the input
    # current/emergency. The deterministic context check must still reject it.
    result, _, _, _, _, rule = run_review(text, classification(evidence=evidence))
    assert result is rule


@pytest.mark.parametrize('content,response_body', [
    ('not-json', None), ('[]', None), ('null', None),
    ('x' * 4001, None), (None, {'message': {'content': {'category': 'self_harm'}}}),
    (None, {'message': {}}), (None, {'choices': []}),
])
def test_malformed_local_response_never_replaces_existing_rule(content, response_body):
    result, _, _, _, _, rule = run_review(content=content, response_body=response_body)
    assert result is rule


@pytest.mark.parametrize('error', [httpx.ReadTimeout, httpx.ConnectError])
def test_local_timeout_or_network_error_preserves_prompt_and_marks_unavailable(error):
    result, requests, status, _, retry_after, rule = run_review(chat_error=error)
    assert result is rule and len(requests) == 2
    assert status['mode'] == 'rules_local_review_unavailable' and not status['available']
    assert retry_after > 0


@pytest.mark.parametrize('status', [302, 500])
def test_chat_redirect_or_server_error_is_not_followed_or_used(status):
    result, requests, state, _, _, rule = run_review(chat_status=status)
    assert result is rule and len(requests) == 2 and not state['available']
    assert all(request.url.host == '127.0.0.1' for request in requests)


@pytest.mark.parametrize('options', [
    {'tags': {'models': []}}, {'tags': {'models': [{'name': 'other-model'}]}},
    {'tags': {'models': None}}, {'tags': []}, {'tags_status': 302},
    {'tags_status': 500}, {'tags_error': httpx.ReadTimeout},
])
def test_unavailable_local_model_never_calls_chat(options):
    result, requests, status, _, _, rule = run_review(**options)
    assert result is rule and len(requests) == 1
    assert requests[0].url.path == '/api/tags' and not status['available']


def test_local_payload_is_bounded_and_identity_masked_without_external_hops():
    text = '我现在不想活了。姓名：虚构甲；电话13900001234；地址不存在路9号；邮箱fictional@example.test；证件号110101199001010011。' + '咨询资料' * 600
    result, requests, _, _, _, _ = run_review(text)
    assert result.level == 'emergency'
    payload = json.loads(requests[-1].content)
    message = payload['messages'][1]['content']
    assert len(message) <= 1200
    assert not any(value in message for value in ['虚构甲', '13900001234', '不存在路9号', 'fictional@example.test', '110101199001010011'])
    assert payload['stream'] is False and payload['format'] == 'json'
    assert payload['options']['temperature'] == 0
    assert {str(request.url) for request in requests} == {
        'http://127.0.0.1:11434/api/tags', 'http://127.0.0.1:11434/api/chat',
    }


def test_failed_local_availability_is_not_retried_immediately():
    requests = []
    def handler(request):
        requests.append(request)
        raise httpx.ConnectError('fictional unavailable', request=request)
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False) as client:
            review = LocalSafetyReview(client, enabled=True)
            rule = Risk(level='prompt', message='原有提示')
            assert await review.review('我现在不想活了', rule) is rule
            assert await review.review('我现在不想活了', rule) is rule
            assert not review.status()['available']
    asyncio.run(scenario())
    assert len(requests) == 1
