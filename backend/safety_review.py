"""Optional local semantic escalation. Never lowers a deterministic warning."""
from __future__ import annotations

import json
import re
import time
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from .model import redact_identity
from .safety import EMERGENCY_MESSAGE, SELF_HARM_MESSAGE, _affirmed, current_clauses
from .schemas import Risk


class SafetyClassification(BaseModel):
    model_config = ConfigDict(extra='forbid')
    category: Literal['none', 'self_harm', 'poisoning', 'consciousness', 'seizure', 'severe_bleeding', 'pregnancy_bleeding', 'infant_warning', 'head_injury', 'breathing', 'stroke']
    urgency: Literal['none', 'uncertain', 'emergency']
    context: Literal['current', 'historical', 'hypothetical', 'negated', 'uncertain']
    evidence: str = Field(default='', max_length=250)


def affirmed_semantic_evidence(text: str, evidence: str) -> bool:
    # A model's 'current' label cannot turn a hypothetical current-tense
    # example into a report. A contrasting actual report is checked separately.
    parts = re.split(r'[。；！？\n]|(?:但是|但(?!愿)|可是|(?<!喘)不过)', text)
    for part in parts:
        if re.search(r'假设|假如|如果|设想|科普|举例|故事|小说|角色|资料中的|知识库中的', part):
            continue
        if any(_affirmed(clause, re.escape(evidence)) for clause in current_clauses(part)):
            return True
    return False


class LocalSafetyReview:
    def __init__(self, client: httpx.AsyncClient, enabled=False, model='qwen2.5:7b'):
        self.client = client
        self.enabled = bool(enabled)
        self.model = model if re.fullmatch(r'[a-zA-Z0-9_.:/-]{1,120}', model) else 'qwen2.5:7b'
        self.available = False
        self.retry_after = 0.0

    def status(self):
        return {'enabled': self.enabled, 'available': self.available, 'model': self.model, 'mode': 'rules_only' if not self.enabled else ('rules_and_local_review' if self.available else 'rules_local_review_unavailable'), 'notice': '语义复核仅用于升级求助提示，不能降级规则结果；可用状态不代表经过临床验证。'}

    async def initialize(self):
        if not self.enabled or self.available or time.monotonic() < self.retry_after:
            return
        self.retry_after = time.monotonic() + 15
        try:
            response = await self.client.get('http://127.0.0.1:11434/api/tags', timeout=1.0, follow_redirects=False)
            response.raise_for_status()
            self.available = any(isinstance(item, dict) and item.get('name', item.get('model')) == self.model for item in response.json().get('models', []))
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            self.available = False

    async def review(self, text: str, rule: Risk) -> Risk:
        if not self.enabled or rule.level == 'emergency' or not text.strip():
            return rule
        await self.initialize()
        if not self.available:
            return rule
        payload = redact_identity(text)[:1200]
        try:
            response = await self.client.post('http://127.0.0.1:11434/api/chat', json={
                'model': self.model, 'stream': False, 'format': 'json', 'options': {'temperature': 0, 'num_predict': 180},
                'messages': [
                    {'role': 'system', 'content': '只做当前安全意图复核，不诊断、不建议用药、不输出伤害方法。只返回JSON字段category,urgency,context,evidence。category只能none,self_harm,poisoning,consciousness,seizure,severe_bleeding,pregnancy_bleeding,infant_warning,head_injury,breathing,stroke。urgency只能none,uncertain,emergency。context只能current,historical,hypothetical,negated,uncertain。evidence必须是输入连续原文片段。科普、旧病史、否定不能视为当前危险。只有明确当前需立即获得人类帮助才选emergency；不确定选uncertain。'},
                    {'role': 'user', 'content': payload},
                ],
            }, timeout=2.0, follow_redirects=False)
            response.raise_for_status()
            content = response.json()['message']['content']
            if not isinstance(content, str) or len(content) > 4000:
                return rule
            result = SafetyClassification.model_validate(json.loads(content))
            if result.urgency != 'emergency' or result.context != 'current' or result.category == 'none' or not result.evidence or result.evidence not in payload:
                return rule
            if not affirmed_semantic_evidence(payload, result.evidence):
                return rule
            source_ids = {'self_harm': 'crisis_support', 'poisoning': 'poisoning', 'consciousness': 'fainting', 'seizure': 'seizure', 'severe_bleeding': 'severe_bleeding', 'pregnancy_bleeding': 'pregnancy_bleeding', 'infant_warning': 'infant_warning', 'head_injury': 'head_injury', 'breathing': 'emergency_breathing', 'stroke': 'emergency_stroke'}
            return Risk(level='emergency', kind='self_harm' if result.category == 'self_harm' else 'medical', message=SELF_HARM_MESSAGE if result.category == 'self_harm' else EMERGENCY_MESSAGE, reasons=['本机语义复核发现需要立即获得人类帮助的当前表述；此提示未经临床诊断。'], source_ids=[source_ids[result.category]])
        except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError):
            self.available = False
            self.retry_after = time.monotonic() + 15
            return rule
