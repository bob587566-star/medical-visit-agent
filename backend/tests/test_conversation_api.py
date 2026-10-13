"""Regression for the reported dialogue failure; all inputs are fictional."""
from fastapi.testclient import TestClient
import pytest

from backend.tests.demo_factory import create_demo_app as create_app
from backend.model import ModelService


def test_chat_continues_without_requiring_form_confirmation(tmp_path):
    with TestClient(create_app(tmp_path / 'dialogue.sqlite3', ModelService())) as client:
        token = client.get('/api/bootstrap').json()['csrf_token']
        headers = {'X-CSRF-Token': token}
        visit = client.post('/api/visits', json={}, headers=headers).json()
        base = f"/api/visits/{visit['id']}"
        turns = [('头疼想睡觉', 'onset'), ('昨晚开始的', 'severity'), ('轻微', 'age_group'), ('成人', 'medications'), ('没有用药', 'allergies'), ('不知道', 'medical_history'), ('没有既往病史', None)]
        for index, (text, expected_next) in enumerate(turns):
            response = client.post(base + '/messages', json={'content': text, 'use_model': False}, headers=headers)
            assert response.status_code == 200
            data = response.json()
            assistant = data['assistant_message']
            assert assistant['content'].strip()
            assert assistant['followup_field'] == expected_next
            assert '最想向医生说明的不适是什么' not in assistant['content']
            assert '以下内容仅用于健康教育' not in assistant['content']
            if index == 0:
                assert '头疼' in assistant['content']
                assert '想睡觉' in assistant['content']
                assert len(assistant['content']) < 300
            assert data['visit']['intake']['chief_complaint'] == ''
            assert data['visit']['collection']['completion'] == 0
        saved = client.get(base).json()
        assert len(saved['messages']) == 14
        assert {candidate['field'] for candidate in saved['intake_candidates']} == {'chief_complaint', 'onset', 'severity', 'age_group', 'medications', 'allergies', 'medical_history'}
        candidate = next(c for c in saved['intake_candidates'] if c['field'] == 'chief_complaint')
        confirmed = client.post(base + f"/candidates/{candidate['id']}/confirm", json={}, headers=headers).json()
        assert confirmed['intake']['chief_complaint'] == '头疼想睡觉'
        assert confirmed['collection']['completion'] == 14


def test_unknown_question_after_symptoms_does_not_reuse_old_evidence(tmp_path):
    with TestClient(create_app(tmp_path / 'dialogue.sqlite3', ModelService())) as client:
        headers = {'X-CSRF-Token': client.get('/api/bootstrap').json()['csrf_token']}
        record = client.post('/api/visits', json={}, headers=headers).json()
        base = f"/api/visits/{record['id']}"
        client.post(base + '/messages', json={'content': '头疼想睡觉'}, headers=headers)
        response = client.post(base + '/messages', json={'content': '这家医院的地址在哪'}, headers=headers)
        assert response.status_code == 200
        answer = response.json()['assistant_message']
        assert answer['content']
        assert answer['sources'] == []
        assert answer['followup_field'] is None
        assert '没有' in answer['content'] or '不掌握' in answer['content']
        assert not answer['intake_candidates']


def test_contextual_health_followup_can_find_existing_symptom_sources(tmp_path):
    with TestClient(create_app(tmp_path / 'dialogue.sqlite3', ModelService())) as client:
        headers = {'X-CSRF-Token': client.get('/api/bootstrap').json()['csrf_token']}
        record = client.post('/api/visits', json={}, headers=headers).json()
        base = f"/api/visits/{record['id']}"
        client.post(base + '/messages', json={'content': '头疼想睡觉'}, headers=headers)
        response = client.post(base + '/messages', json={'content': '那我该怎么办？'}, headers=headers)
        assert response.status_code == 200
        answer = response.json()['assistant_message']
        assert {source['id'] for source in answer['sources']} == {'headache'}
        assert answer['followup_field'] is None
        assert '没有对应' not in answer['content']
        assert '诊断为' not in answer['content']


def test_user_can_decline_collection_and_continue_normal_chat(tmp_path):
    with TestClient(create_app(tmp_path / 'dialogue.sqlite3', ModelService())) as client:
        headers = {'X-CSRF-Token': client.get('/api/bootstrap').json()['csrf_token']}
        record = client.post('/api/visits', json={}, headers=headers).json()
        base = f"/api/visits/{record['id']}"
        for text in ('头疼想睡觉', '不想填了', '谢谢'):
            response = client.post(base + '/messages', json={'content': text}, headers=headers)
            assert response.status_code == 200
            if text != '头疼想睡觉':
                answer = response.json()['assistant_message']
                assert answer['followup_field'] is None
                assert not answer['intake_candidates']
                assert '没有与你的问题对应' not in answer['content']


def test_named_drug_and_frequency_request_is_not_a_symptom_fact(tmp_path):
    with TestClient(create_app(tmp_path / 'dialogue.sqlite3', ModelService())) as client:
        headers = {'X-CSRF-Token': client.get('/api/bootstrap').json()['csrf_token']}
        record = client.post('/api/visits', json={}, headers=headers).json()
        base = f"/api/visits/{record['id']}"
        response = client.post(base + '/messages', json={'content': '虚构病例：咽痛两天，请直接开阿莫西林或布洛芬，告诉我一次多少毫克、一天几次。'}, headers=headers)
        answer = response.json()['assistant_message']
        assert '不能' in answer['content']
        assert answer['followup_field'] is None
        assert answer['intake_candidates'] == []
        assert response.json()['visit']['intake_candidates'] == []
        assert answer['sources'] == []


@pytest.mark.parametrize('first,second,urgent', [
    ('我现在胸痛', '还冒冷汗', True),
    ('我现在喉咙肿了', '还呼吸困难', True),
    ('刚刚开始头痛', '痛得受不了', True),
    ('我现在没有胸痛', '还冒冷汗', False),
    ('去年曾经胸痛', '还冒冷汗', False),
    ('科普胸痛有哪些原因', '还冒冷汗', False),
    ('我爸爸现在胸痛', '还冒冷汗的是我妈妈', False),
    ('我爸爸现在胸痛', '还有我姐姐冒冷汗', False),
    ('我爸爸现在胸痛', '还有奶奶呼吸困难', False),
    ('我爸爸现在胸痛', '还有张三冒冷汗', False),
    ('我爸爸现在胸痛', '还有我冒冷汗', False),
    ('头疼想睡觉', '昨晚开始的', False),
])
def test_linked_symptom_reports_are_checked_before_model_or_retrieval(tmp_path, first, second, urgent):
    class NoExternalModel:
        available = True

        async def extract(self, payload):
            pytest.fail('test must not call external model')

    with TestClient(create_app(tmp_path / 'dialogue.sqlite3', NoExternalModel())) as client:
        headers = {'X-CSRF-Token': client.get('/api/bootstrap').json()['csrf_token']}
        record = client.post('/api/visits', json={}, headers=headers).json()
        base = f"/api/visits/{record['id']}"
        client.post(base + '/messages', json={'content': first, 'use_model': False}, headers=headers)
        response = client.post(base + '/messages', json={'content': second, 'use_model': urgent}, headers=headers)
        assert response.status_code == 200
        answer = response.json()['assistant_message']
        assert (answer['risk']['level'] == 'emergency') is urgent
        if urgent:
            assert '120' in answer['content']
            assert answer['followup_field'] is None
            assert [step['tool'] for step in answer['trace']] == ['check_urgent_signs']
