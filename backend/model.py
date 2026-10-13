from __future__ import annotations

import json
import re
from pathlib import Path

import httpx

from .knowledge import Knowledge, matches
from .schemas import ExtractedDetails, Source
from .safety import _affirmed, current_clauses
from .egress import check_egress


def read_project_env(path: Path) -> dict[str, str]:
    """Read only this project's configuration; never log its values."""
    if not path.exists():
        return {}
    values = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


def redact_identity(text: str) -> str:
    text = re.sub(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", "[已遮盖邮箱]", text, flags=re.I)
    text = re.sub(r"(?<!\d)\d{17}[0-9Xx](?!\d)|(?<!\d)\d{15}(?!\d)", "[已遮盖证件号]", text)
    text = re.sub(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)|(?<!\d)\d{3,4}[- ]\d{7,8}(?!\d)", "[已遮盖电话]", text)
    text = re.sub(r"(?:我叫|姓名[：:]?|名字是|住在|住址[：:]?|地址[：:]?|联系人[：:]?)[^，。；\n]{1,60}", "[已遮盖身份信息]", text)
    return text


def minimal_symptom_payload(text: str, sources: list[Source], knowledge: Knowledge) -> str:
    """Allowlist symptom aliases and timing, never transmit the original text.

    This also drops incidental names, addresses, history, identifiers and all
    prompt instructions rather than relying on imperfect regex redaction alone.
    """
    redacted = redact_identity(text)
    clauses = current_clauses(redacted)
    aliases = list(dict.fromkeys(alias for source in sources for alias in knowledge.aliases(source)
                                if matches(redacted, alias)
                                and any(_affirmed(clause, re.escape(alias)) for clause in clauses)))
    symptom_aliases = [alias for alias in aliases if re.search(r"[\u4e00-\u9fff]", alias)][:8]
    if not symptom_aliases:
        return ""
    timing = re.findall(r"(?:今天|昨天|昨晚|今早|刚刚|刚才|目前|现在|突然|突发|\d{1,3}\s*(?:分钟|小时|天|周|个月))", redacted)[:4]
    severity = re.findall(r"(?:轻微|严重|很痛|加重|缓解|持续|间歇)", redacted)[:4]
    # The payload contains only bounded clinical vocabulary, not identity/history.
    return "；".join(dict.fromkeys(symptom_aliases + timing + severity))[:600]


class ModelService:
    def __init__(self, api_key: str = "", model: str = "deepseek-chat", client: httpx.AsyncClient | None = None):
        self.api_key = api_key
        self.model = model if re.fullmatch(r"[a-zA-Z0-9_.-]{1,80}", model) else "deepseek-chat"
        self.client = client

    @property
    def available(self) -> bool:
        return bool(self.api_key and self.client)

    async def extract(self, payload: str) -> ExtractedDetails | None:
        if not self.available or not payload:
            return None
        try:
            check_egress()
            response = await self.client.post(
                "https://api.deepseek.com/chat/completions",
                headers={"Authorization": "Bearer " + self.api_key, "Content-Type": "application/json"},
                json={
                    "model": self.model,
                    "temperature": 0,
                    "thinking": {"type": "disabled"},
                    "max_tokens": 250,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {"role": "system", "content": "只提取输入中已有的短片段。输出JSON且只有symptoms(字符串数组)、onset(字符串)、severity(字符串)。每个非空值必须为原文连续片段，不推测、不诊断、不提供药物或治疗、不添加字段；缺失用空值。"},
                        {"role": "user", "content": payload},
                    ],
                },
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            extracted = ExtractedDetails.model_validate(json.loads(content))
            fragments = extracted.symptoms + [extracted.onset, extracted.severity]
            if not any(fragments):
                return None
            if any(value and value not in payload for value in fragments):
                return None
            return extracted
        except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError):
            return None
