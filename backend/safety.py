from __future__ import annotations

import re

from .schemas import Intake, Risk


LIMITATION = "这些提示不能覆盖所有紧急情况；未命中规则不代表安全，也不能排除疾病。"
UNKNOWN_MESSAGE = "目前信息不足以判断紧急程度。" + LIMITATION
EMERGENCY_MESSAGE = "你描述的情况命中了需要立即处理的警示征象。请立即联系当地急救；中国大陆可拨打 120，并请身边的人协助。不要等待本助手或模型的回复。" + LIMITATION
# Bounded warnings only. 无力/无法呼吸/没有反应 are positive warning signs.
DENIAL = re.compile(
    r"(?:没有|没(?:有)?|并无|未出现|未发生|不伴|不曾|否认|已无|不再|并非|不是|"
    r"无(?=(?:明显|任何)?(?:胸|冷汗|大汗|呼吸困难|气短|意识异常|头痛|头疼)))"
    r"[^，。；！？\n]{0,32}"
)
POSITIVE_INABILITY = re.compile(r"没有(?:任何|明显|正常)?反应|无法呼吸|不能呼吸|无力")
HISTORY = re.compile(r"(?:去年|前年|(?:\d+|[一二三四五六七八九十百]+)年前|小时候|幼时|曾经|以前|过去|上次|历史|既往|当时|上个月|上周)")
OLD_HISTORY = re.compile(r"(?:去年|前年|(?:\d+|[一二三四五六七八九十百]+)年前|小时候|幼时|上个月|上周)")
CURRENT = re.compile(r"(?:现在|目前|此刻|今天|今日|今早|今晨|刚刚|刚才|正在|这次|昨晚|昨天)")
RECENT = re.compile(r"(?:今天|今日|今早|今晨|刚刚|刚才|昨晚|昨天|(?:过去|最近)?\s*(?:[1-9]|1\d|2[0-4])\s*小时(?:前|内))")
HARD_EDUCATION = re.compile(r"(?:科普|举例|故事|小说|角色|假设|如果有人|请解释|资料中的|知识库中的)")
EDUCATION = re.compile(r"(?:是什么|什么意思|有哪些|如何识别|怎样识别|介绍一下|一般|为什么|危险吗)")
PERSON = r"(?:我|本人|孩子|宝宝|父亲|母亲|爸爸|妈妈|丈夫|妻子|家人|患者|病人|朋友|同事|老人)"
SELF_REPORT = re.compile(PERSON + r"(?:的)?(?:现在|目前|此刻|今天|刚刚|刚才|正在|突然|昨晚|昨天)?(?:觉得|感觉|出现|有|发生|呼吸|胸|头|左|右|喉|舌|意识|喘|说|昏迷|失去|严重|不省|叫不醒|没有反应)")
EXPLICIT_CURRENT_REPORT = re.compile(PERSON + r"(?:的)?(?:现在|目前|此刻|正在|今天|刚刚|刚才).{0,20}(?:疼|痛|无力|说话|呼吸|冷汗|叫不醒|没有反应|肿)")


def _affirmed(clause: str, pattern: str) -> bool:
    """Check symptom spans against nearby negation instead of deleting 无力."""
    scopes: list[tuple[int, int]] = []
    for denial in DENIAL.finditer(clause):
        if POSITIVE_INABILITY.match(clause, denial.start()):
            continue
        start, end = denial.span()
        contrast = re.search(r"(?:但是|但|可是|不过|而且|却|现在|目前)", denial.group(0)[1:])
        if contrast:
            end = start + 1 + contrast.start()
        scopes.append((start, end))
    return any(
        not any(start < symptom.end() and end > symptom.start() for start, end in scopes)
        for symptom in re.finditer(pattern, clause)
    )


def current_clauses(text: str) -> list[str]:
    """Keep present/recent reports; suppress explicit education and old events.

    Help questions such as 怎么办 are not education markers. History carries
    across comma fragments until a current time; 突然 does not reset old history.
    """
    result: list[str] = []
    for sentence in re.split(r"[。；！？\n]", text):
        if not sentence.strip():
            continue
        current_reports = list(EXPLICIT_CURRENT_REPORT.finditer(sentence))
        affirmed_current_report = any(
            _affirmed(report.group(0), r"疼|痛|无力|说话|呼吸|冷汗|叫不醒|没有反应|肿")
            for report in current_reports
        )
        if HARD_EDUCATION.search(sentence) and not affirmed_current_report:
            continue
        if EDUCATION.search(sentence) and not (SELF_REPORT.search(sentence) or CURRENT.search(sentence)):
            continue
        historical = False
        time_hint = ""
        current_parts: list[str] = []
        # 不过 in 喘不过气 is part of a symptom, not a contrast marker.
        parts = re.split(r"[，,]|(?:但是|但|可是|(?<!喘)不过)|(?=(?:现在|目前|此刻|这次|今天|今日|今早|今晨|刚刚|刚才))", sentence)
        for part in parts:
            part = part.strip()
            if not part:
                continue
            recent = RECENT.search(part)
            if OLD_HISTORY.search(part):
                historical = True
                time_hint = ""
            elif CURRENT.search(part) or recent:
                historical = False
                time_hint = recent.group(0) if recent else CURRENT.search(part).group(0)
            elif HISTORY.search(part) and not recent:
                historical = True
                time_hint = ""
            if historical:
                continue
            current_parts.append((time_hint + " " + part).strip() if time_hint and not CURRENT.search(part) else part)
        if current_parts:
            # Keep nearby comma fragments together, with commas delimiting
            # negation scopes, for '刚刚头痛，痛得受不了' and associated signs.
            result.append("，".join(current_parts))
    return result


CHEST = r"胸痛|胸口痛|胸部疼痛|胸口疼|胸部痛|胸疼|胸口(?:很|非常)?疼"
BREATH = r"呼吸困难|呼吸不畅|气短|喘不过气|无法呼吸|不能呼吸|喘不上气"
SEVERE_BREATH = r"严重呼吸困难|呼吸(?:非常|十分|很|特别)困难|喘不过气|无法呼吸|不能呼吸|喘不上气|呼吸困难(?:很严重|严重|加重)"
SIDE_WEAKNESS = r"(?:单侧|一侧|左侧|右侧|左边|右边|左手|右手|左臂|右臂|左腿|右腿).{0,12}(?:无力|麻木|抬不起来)|(?:嘴角|一侧脸).{0,8}(?:歪|下垂)"
SPEECH = r"说不出话|说话不清|口齿不清|语言异常|言语异常|无法说话|不能说话"
SUDDEN = r"突然|突发|刚刚|刚才"
SWELLING = r"(?:喉|舌|咽|嘴唇).{0,6}(?:肿|肿胀)|(?:肿|肿胀).{0,6}(?:喉|舌)"
HEADACHE = r"头痛|头疼"
EXTREME_PAIN = r"剧烈|极(?:其)?(?:剧烈|痛)|非常(?:痛|疼)|特别(?:痛|疼)|痛得受不了|疼得受不了|从未有过的(?:痛|疼)"


def check_urgent(text: str, intake: Intake | None = None) -> Risk:
    # Medical history, medications, allergies and quoted past events are never
    # treated as current symptoms. Form chief complaint and notes are self report.
    candidates = [text]
    if intake:
        candidates.extend([intake.chief_complaint, intake.notes])
    clauses = [clause for candidate in candidates for clause in current_clauses(candidate)]
    reasons: list[str] = []
    source_ids: list[str] = []
    # A user may put associated symptoms into separate sentences.
    chest_any = any(_affirmed(clause, CHEST) for clause in clauses)
    associated_any = any(_affirmed(clause, BREATH + r"|冷汗|大汗") for clause in clauses)
    if chest_any and associated_any:
        reasons.append("当前描述包含胸痛，并伴呼吸困难或冷汗")
        source_ids.append("emergency_chest")
    swelling_any = any(_affirmed(clause, SWELLING) for clause in clauses)
    if swelling_any and any(_affirmed(clause, BREATH) for clause in clauses):
        reasons.append("当前描述包含喉舌肿胀并伴呼吸困难")
        source_ids.append("emergency_allergy")
    for clause in clauses:
        if _affirmed(clause, SEVERE_BREATH):
            reasons.append("当前描述包含严重呼吸困难")
            source_ids.append("emergency_breathing")
        # A later 'currently no symptoms' cannot erase a separate recent
        # positive stroke-warning report. NHS recommends help even if resolved.
        if (RECENT.search(clause) or CURRENT.search(clause) or re.search(SUDDEN, clause)) and (
            _affirmed(clause, SIDE_WEAKNESS) or _affirmed(clause, SPEECH)
        ):
            reasons.append("当前或近期描述包含单侧无力或语言异常，即使已缓解也应立即求助")
            source_ids.append("emergency_stroke")
        if _affirmed(clause, r"昏迷|意识不清|失去意识|意识异常|叫不醒|没有(?:任何|明显|正常)?反应|不省人事"):
            reasons.append("当前描述包含意识异常")
            source_ids.append("emergency_call")
        if _affirmed(clause, HEADACHE) and _affirmed(clause, SUDDEN) and _affirmed(clause, EXTREME_PAIN):
            reasons.append("当前描述包含突然出现的极剧烈头痛")
            source_ids.append("headache")
        if _affirmed(clause, HEADACHE) and (_affirmed(clause, SIDE_WEAKNESS) or _affirmed(clause, SPEECH)):
            reasons.append("当前描述包含头痛伴单侧无力或语言异常")
            source_ids.append("headache")
    if reasons:
        return Risk(level="emergency", message=EMERGENCY_MESSAGE, reasons=list(dict.fromkeys(reasons)), source_ids=list(dict.fromkeys(source_ids)))
    if intake and (intake.severity == "重" or intake.age_group in {"儿童", "孕产期"}):
        return Risk(level="prompt", message="已填报的年龄阶段或症状程度需要尽快向专业医务人员确认。" + LIMITATION, reasons=["填报为重度症状或需要特别评估的人群"])
    if any(_affirmed(clause, CHEST + r"|呼吸困难|持续高热|咳血|便血|症状加重") for clause in clauses):
        return Risk(level="prompt", message="你描述的部分症状需要及时向专业医务人员咨询或就诊。" + LIMITATION, reasons=["当前描述包含需要专业评估的症状"])
    return Risk(level="unknown", message=UNKNOWN_MESSAGE)


def safe_assistant_output(text: str) -> str:
    """Reject generated clinical instructions, drug doses, and diagnosis claims."""
    prohibited = re.compile(r"(?:你|您)(?:患有|得了|就是|确诊为)|诊断(?:为|是)|(?:服用|口服|注射|用药|吃药).{0,16}(?:\d|毫克|mg|克|片)|\d+(?:\.\d+)?\s*(?:mg|毫克|mcg|微克|毫升|mL)|阿莫西林|布洛芬|对乙酰氨基酚|阿司匹林|青霉素|头孢|抗生素疗程", re.I)
    if prohibited.search(text):
        raise ValueError("输出超出就诊准备范围")
    return text
