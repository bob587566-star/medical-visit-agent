from __future__ import annotations

import json
import math
import re
from collections import Counter
from pathlib import Path

from .schemas import Source
from .safety import safe_assistant_output


# Topic gating comes from editorial tags and these explicit symptom aliases.
# BM25 ranks only the gated set; it cannot introduce an unrelated topic.
TOPIC_ALIASES = {
    "headache": ["头痛", "头疼", "偏头痛"],
    "fever": ["发热", "发烧", "高热", "体温"],
    "cough": ["咳嗽", "咳痰"],
    "respiratory": ["咳嗽", "咳痰", "呼吸困难", "气短", "喘不过气"],
    "breathing": ["呼吸困难", "气短", "喘不过气", "严重呼吸困难"],
    "chest_pain": ["胸痛", "胸口痛", "胸口疼", "胸部疼痛"],
    "stroke": ["单侧无力", "一侧无力", "左侧无力", "右侧无力", "口齿不清", "说话不清", "说不出话", "语言异常"],
    "allergy": ["过敏", "喉咙肿", "舌头肿", "喉舌肿胀"],
    "abdominal_pain": ["腹痛", "肚子痛", "腹部疼痛", "肚子疼"],
    "diarrhea": ["腹泻", "拉肚子"],
    "hypertension": ["高血压", "血压"],
    "diabetes": ["糖尿病", "血糖"],
    "visit_preparation": ["就诊准备", "看病准备", "就医准备", "就诊清单", "带什么", "看医生", "准备材料", "就诊摘要"],
    "emergency_call": ["急救电话", "急救号码", "120", "急救"],
}


def tokens(text: str) -> list[str]:
    text = text.lower()
    words = re.findall(r"[a-z0-9_]+", text)
    for part in re.findall(r"[\u4e00-\u9fff]+", text):
        words.extend(part[i:i + 2] for i in range(max(1, len(part) - 1)))
    return words


def matches(text: str, alias: str) -> bool:
    if not alias or len(alias.strip()) < 2:
        return False
    if re.fullmatch(r"[a-zA-Z0-9_ -]+", alias):
        return bool(re.search(r"(?<![a-z0-9_])" + re.escape(alias.lower()) + r"(?![a-z0-9_])", text.lower()))
    return alias.lower() in text.lower()


class Knowledge:
    def __init__(self, path: Path):
        self.path = path
        self.signature = None
        self.items: list[Source] = []
        self.load_error = ""

    def refresh(self) -> None:
        try:
            stat = self.path.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
        except FileNotFoundError:
            self.items = []
            self.load_error = "知识库文件未就绪"
            return
        if signature == self.signature:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
            raw = data if isinstance(data, list) else data.get("items", data.get("articles", []))
            items = [Source.model_validate(item) for item in raw]
            if len({item.id for item in items}) != len(items):
                raise ValueError("duplicate source id")
            for item in items:
                if not item.url.startswith("https://"):
                    raise ValueError("knowledge source must use HTTPS")
                safe_assistant_output(item.content)
            self.items = items
            self.signature = signature
            self.load_error = ""
        except (ValueError, TypeError, AttributeError):
            # Invalid editorial data fails closed; never serve stale evidence.
            self.items = []
            self.load_error = "知识库格式或内容校验未通过"

    def all(self) -> list[Source]:
        self.refresh()
        return list(self.items)

    def aliases(self, item: Source) -> list[str]:
        aliases = list(item.topics) + list(item.keywords)
        for topic in item.topics:
            aliases.extend(TOPIC_ALIASES.get(topic, []))
        return list(dict.fromkeys(aliases))

    def search(self, query: str, limit: int = 3) -> list[Source]:
        items = self.all()
        eligible = [item for item in items if any(matches(query, alias) for alias in self.aliases(item))]
        if not eligible:
            return []
        corpus = [tokens(item.title + " " + item.content + " " + " ".join(item.keywords)) for item in eligible]
        frequencies = [Counter(document) for document in corpus]
        query_tokens = set(tokens(query))
        average = sum(map(len, corpus)) / max(1, len(corpus))
        scores = []
        for item, document, counts in zip(eligible, corpus, frequencies):
            score = 0.0
            for token in query_tokens:
                tf = counts[token]
                if not tf:
                    continue
                df = sum(token in other for other in frequencies)
                idf = math.log(1 + (len(corpus) - df + 0.5) / (df + 0.5))
                score += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * len(document) / max(1, average)))
            scores.append((score, item))
        return [item for _, item in sorted(scores, key=lambda pair: (-pair[0], pair[1].id))[:limit]]

    def by_ids(self, ids: list[str]) -> list[Source]:
        wanted = set(ids)
        return [item for item in self.all() if item.id in wanted]
