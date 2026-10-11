from __future__ import annotations

import json
import hashlib
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
    "sore_throat": ["喉咙痛", "嗓子疼", "咽痛", "吞咽疼", "喉咙疼", "嗓子痛"],
    "dizziness": ["头晕", "眩晕", "天旋地转", "站起来发晕"],
    "fatigue": ["疲劳", "疲倦", "乏力", "总觉得累", "没精神", "容易累"],
    "insomnia": ["失眠", "睡不着", "入睡困难", "半夜醒", "睡眠不好", "早醒"],
    "back_pain": ["腰痛", "腰疼", "背痛", "背疼", "下背痛", "腰背疼"],
    "constipation": ["便秘", "排便困难", "大便干", "大便很硬", "拉不出来"],
    "urinary_symptoms": ["尿痛", "尿频", "尿急", "小便疼", "排尿痛", "尿血", "血尿"],
    "dehydration": ["脱水", "尿量变少", "小便变少", "尿色深", "口干", "很少尿"],
    "palpitations": ["心悸", "心慌", "心跳很快", "心跳乱", "心跳明显", "心跳不规律"],
    "contact_dermatitis": ["接触性皮炎", "皮肤瘙痒", "皮肤痒", "皮肤发痒", "碰了洗涤剂", "接触后起疹"],
    "hay_fever": ["花粉", "花粉过敏", "过敏性鼻炎", "季节性鼻炎", "打喷嚏", "鼻子痒", "流鼻涕"],
    "earache": ["耳痛", "耳朵疼", "耳朵痛", "耳朵流液", "听力变化"],
    "toothache": ["牙痛", "牙疼", "牙龈肿", "牙齿疼", "咬东西疼"],
    "red_eye": ["眼红", "眼睛红", "眼睛发红", "眼睛痛", "眼睛疼", "视力变化"],
    "sprains": ["扭伤", "拉伤", "崴脚", "脚踝扭", "踝关节扭", "运动受伤"],
    "fainting": ["晕倒", "昏厥", "晕厥", "短暂失去意识"],
    "nosebleed": ["鼻出血", "流鼻血", "鼻子出血"],
    "child_fever": ["孩子发烧", "小孩发烧", "宝宝发烧", "儿童发热", "儿童发烧", "婴儿发烧", "孩子发热"],
    "pregnancy_preparation": ["产检", "孕期就诊", "孕期检查", "怀孕看医生", "孕期准备", "怀孕就诊"],
    "blood_test_preparation": ["抽血准备", "验血准备", "抽血前", "空腹抽血", "抽血要空腹", "验血前", "禁食"],
    "lab_results": ["化验结果", "化验单", "检查报告", "参考范围", "检验报告", "化验报告", "化验异常"],
}

# These subjects need institutional, personal, or prescribing data that this
# editorial library does not have. Shared words cannot establish support.
UNSUPPORTED_QUERY = re.compile(
    r"医院.{0,10}(?:地址|电话|收费|多少钱|营业|挂号|预约)|"
    r"(?:门店|门诊|诊所|公司|实体店).{0,8}(?:地址|在哪|在哪里|位置)|"
    r"医保|报销|保险|挂号费|预约号|医生排班|"
    r"(?:吃|服|用|口服|注射).{0,12}(?:多少|几片|几粒|剂量)|"
    r"(?:药物|药品|处方|退烧药|止咳药|降压药).{0,8}(?:推荐|剂量|多少|用量)|"
    r"开(?:药|处方)|(?:mg|毫克|微克)\s*(?:吗|合适|够不够)", re.I
)


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
            raw_bytes = self.path.read_bytes()
            if len(raw_bytes) > 2_000_000:
                raise ValueError("knowledge file too large")
            signature = hashlib.sha256(raw_bytes).hexdigest()
        except (OSError, ValueError):
            self.items = []
            self.signature = None
            self.load_error = "知识库文件未就绪"
            return
        if signature == self.signature:
            return
        try:
            data = json.loads(raw_bytes.decode("utf-8-sig"))
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
        except (ValueError, TypeError, AttributeError, UnicodeError):
            # Invalid editorial data fails closed; never serve stale evidence.
            self.items = []
            self.signature = None
            self.load_error = "知识库格式或内容校验未通过"

    def all(self) -> list[Source]:
        self.refresh()
        return list(self.items)

    def aliases(self, item: Source) -> list[str]:
        aliases = list(item.topics) + list(item.keywords)
        for topic in item.topics:
            aliases.extend(TOPIC_ALIASES.get(topic, []))
        return list(dict.fromkeys(aliases))

    def categories(self) -> list[str]:
        return sorted({item.category for item in self.all()})

    def candidates(self, query: str) -> list[Source]:
        """Human-edited topic support precedes every lexical/vector ranking."""
        items = self.all()
        query = query.strip()
        if not query or len(query) > 8000 or UNSUPPORTED_QUERY.search(query):
            return []
        eligible = [item for item in items if any(matches(query, alias) for alias in self.aliases(item))]
        # A child's fever question is not evidence for applying adult advice.
        if re.search(r"孩子|小孩|儿童|宝宝|婴儿", query) and re.search(r"发烧|发热|体温", query):
            eligible = [item for item in eligible if item.id != "fever"]
        return eligible

    def search(self, query: str, limit: int = 3) -> list[Source]:
        eligible = self.candidates(query)
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
        return [item for _, item in sorted(scores, key=lambda pair: (-pair[0], pair[1].id))[:max(0, limit)]]

    def by_ids(self, ids: list[str]) -> list[Source]:
        wanted = set(ids)
        return [item for item in self.all() if item.id in wanted]
