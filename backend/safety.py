from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from .schemas import Intake, Risk, Visit


LIMITATION = "这些提示不能覆盖所有紧急情况；未命中规则不代表安全，也不能排除疾病。"
UNKNOWN_MESSAGE = "目前信息不足以判断紧急程度。" + LIMITATION
EMERGENCY_MESSAGE = "你描述的情况命中了需要立即处理的警示征象。请立即联系当地急救；中国大陆可拨打 120，并请身边的人协助。不要等待本助手或模型的回复。" + LIMITATION
SELF_HARM_MESSAGE = (
    "听起来你正承受很大的痛苦，谢谢你把这些告诉我。现在先和一位你信任的人联系，请对方陪着你，尽量不要独处；"
    "在安全的前提下，与可能伤害自己的物品拉开距离，请身边的人帮助保管。"
    "如果你已经伤害了自己、服用了过量药物，或担心马上会行动、无法保证自己的安全，请立即联系当地急救或请人陪你去急诊；"
    "中国大陆可拨打 120。请不要等待本助手回复。你现在能联系到谁来陪你？" + LIMITATION
)
# Bounded warnings only. 无力/无法呼吸/没有反应 are positive warning signs.
DENIAL = re.compile(
    r"(?:没有|没(?:有)?|并无|未出现|未发生|不伴|不曾|否认|已无|不再|并非|不是|"
    r"不(?:想|打算|准备|计划|会)(?=(?:再)?(?:死|自杀|自伤|自傷|轻生|寻短见|伤害自己|结束(?:自己)?(?:的)?生命))|"
    r"未(?=(?:抽搐|晕倒|昏倒|出血|呕吐|服药过量))|"
    r"无(?=(?:明显|任何)?(?:胸|冷汗|大汗|呼吸困难|气短|意识异常|意识模糊|头痛|头疼|抽搐|出血|咯血|咳血|自伤|自杀|晕倒|昏倒|昏睡|呕吐|服药过量)))"
    r"[^，。；！？\n]{0,32}"
)
POSITIVE_INABILITY = re.compile(r"没(?:有)?(?:任何|明显|正常)?反应|没有(?:醒过来|恢复清醒)|无法呼吸|不能呼吸|无力")
HISTORY = re.compile(r"(?:去年|前年|(?:\d+|[一二三四五六七八九十百]+)年前|小时候|幼时|曾经|以前|(?<!晕)(?<!昏)过去|上次|历史|既往|当时|上个月|上周)")
OLD_HISTORY = re.compile(r"(?:去年|前年|(?:\d+|[一二三四五六七八九十百]+)年前|小时候|幼时|上个月|上周)")
CURRENT = re.compile(r"(?:现在|目前|此刻|今天|今日|今早|今晨|刚刚|刚才|正在|这次|昨晚|昨天)")
RECENT = re.compile(r"(?:今天|今日|今早|今晨|刚刚|刚才|昨晚|昨天|(?:过去|最近)?\s*(?:[1-9]|1\d|2[0-4])\s*小时(?:前|内))")
HARD_EDUCATION = re.compile(r"(?:科普|举例|故事|小说|角色|假设|假如|如果(?=(?:我|他|她|有人|宝宝|孩子|患者))|请解释|资料中的|知识库中的)")
EDUCATION = re.compile(r"(?:是什么|什么意思|有哪些|如何识别|怎样识别|介绍一下|一般|为什么|危险吗)")
PERSON = r"(?:我|本人|孩子|宝宝|父亲|母亲|爸爸|妈妈|丈夫|妻子|家人|患者|病人|朋友|同事|老人)"
REPORT_PREDICATES = r"疼|痛|无力|说话|呼吸|喘|冷汗|叫不醒|没有反应|肿|不想活|想死|自杀|自伤|轻生|吃|吞|晕|昏|抽搐|抽蓄|出血|咳.*血|发烧|发热|吐|拒奶"
SELF_REPORT = re.compile(PERSON + r"(?:的)?(?:现在|目前|此刻|今天|刚刚|刚才|正在|突然|昨晚|昨天)?(?:觉得|感觉|出现|有|发生|呼吸|胸|头|左|右|喉|舌|意识|喘|说|昏迷|失去|严重|不省|叫不醒|没有反应|不想活|想死|晕|昏|全身抽|发烧|发热|吐|吞|误服|一次(?:吃|吞)|一口气(?:吃|吞)|(?:想(?!知道|了解|询问|咨询|学习|研究)|准备|打算|决定).{0,6}(?:自杀|自伤|轻生|结束(?:自己)?的生命|伤害自己))")
EXPLICIT_CURRENT_REPORT = re.compile(PERSON + r"(?:的)?(?:现在|目前|此刻|正在|今天|刚刚|刚才).{0,20}(?:" + REPORT_PREDICATES + r")")


def _affirmed(clause: str, pattern: str) -> bool:
    """Check symptom spans against nearby negation instead of deleting 无力."""
    scopes: list[tuple[int, int]] = []
    for denial in DENIAL.finditer(clause):
        if POSITIVE_INABILITY.match(clause, denial.start()):
            continue
        # Denying a small quantity does not deny the bleeding itself.
        if re.match(r'不是(?:一点|少量|几滴|血丝)', denial.group(0)):
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
        if HARD_EDUCATION.search(sentence):
            # Current-tense examples remain examples. Only a separate contrast
            # that explicitly reports a present event can reset that context.
            parts = re.split(r'(?:但是|但(?!愿)|可是|(?<!喘)不过)', sentence)
            reports = []
            for part in parts:
                marker = HARD_EDUCATION.search(part)
                actual = part[:marker.start()] if marker else part
                if EXPLICIT_CURRENT_REPORT.search(actual) and _affirmed(actual, REPORT_PREDICATES):
                    reports.append(actual)
            if not reports:
                continue
            sentence = '，'.join(reports)
        current_reports = [report for report in EXPLICIT_CURRENT_REPORT.finditer(sentence)
                           if not re.search(r"看(?:过|了)?|读|研究|了解|讨论|听说|故事|小说|角色|资料", report.group(0))]
        affirmed_current_report = any(
            _affirmed(report.group(0), REPORT_PREDICATES)
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

# These patterns inspect current self reports, not medication doses or diagnoses.
SELF_HARM = (
    r"不想(?:再)?活(?:下去|着)?(?:了)?(?=[，,。；！？\s]|$)|不想(?:再)?活下去了|"
    r"活不下去(?:了)?|不如死了算了|(?:我|自己).{0,8}(?:想死|想要死)|"
    r"(?:想(?!知道|了解|询问|咨询|学习|研究)|打算|准备|计划|决定|要).{0,6}(?:自杀|自傷|自伤|轻生|寻短见|伤害自己|结束(?:自己)?(?:的)?生命|了结自己)|"
    r"(?:自杀|自傷|自伤|轻生)(?:念头|想法|意念|计划)|(?:想|打算|准备).{0,4}一了百了"
)
CONSCIOUSNESS = r"昏迷|意识不清|意识(?:有点|有些|越来越)?模糊|神[志智]不清|失去意识|意识异常|叫不醒|喊不醒|(?:怎么|怎样)(?:叫|喊).{0,6}(?:不醒|没(?:有)?反应)|呼之不应|没(?:有)?(?:任何|明显|正常)?反应|不省人事|没有醒过来|没有恢复清醒"
FAINT = r"晕倒|晕到(?:了|地上)|昏倒|昏到(?:了|地上)|晕过去|昏过去|晕厥|昏厥"
RECOVERED = r"(?:完全|彻底)(?:恢复|清醒|缓过来)|(?:已经|现在|目前)(?:恢复正常|完全好了)|(?:已经|现在|目前)(?:都)?(?:清醒了|缓过来了|醒过来了)"
SEIZURE = r"(?:全身|四肢|手脚|身体)(?:都|突然|一直)?(?:抽搐|抽蓄|抽风)|(?<!眼皮)(?<!眼睑)(?<!嘴角)(?<!眼角)(?:抽搐|抽蓄)|癫痫发作|癫癎发作"
BLEEDING = r"血流不止|血止不住|止不住(?:的)?血|血流个不停|血一直(?:流|往外冒)|伤口.{0,8}(?:流血不停|血流不停|止不住)|(?:按住|压住).{0,12}(?:还在|还是|仍然|仍在).{0,4}(?:流血|出血)"
HAEMOPTYSIS = r"咯血|咳血|咳出(?:来)?[^，。；！？\n]{0,18}血|咳嗽.{0,5}(?:带血|有血)"
LARGE_BLOOD = r"全是血|都是血|满口血|一(?:大)?口一(?:大)?口|大量|很多血|血.{0,4}(?:不停|不止)|不(?:只|止)是(?:一点|少量|血丝)"
PREGNANCY = r"怀孕|孕妇|孕期|孕晚期|妊娠|孕\s*\d+\s*周|孕\s*[一二三四五六七八九十]+\s*周"
VAGINAL_BLEED = r"阴道(?:在|有|少量|大量|一点|点滴){0,2}(?:出血|流血)|下面(?:在|有|少量|大量){0,2}(?:出血|流血)|见红"
ABDOMINAL_PAIN = r"腹痛|(?:肚子|小腹|下腹)(?:还|也|一直|又|很|有点|有一点点)?(?:疼|痛)|腹部(?:疼|痛)"
HEAD_INJURY = r"摔(?:到|了|伤)?(?:头|脑袋)|撞(?:到|了|伤)?(?:头|脑袋)|磕(?:到|了|伤)?(?:头|脑袋)|头部(?:外伤|受伤)|头被撞|脑袋.{0,3}(?:磕|撞)"
WORSENING_DROWSINESS = r"越来越(?:困|嗜睡|难叫醒)|无法保持清醒|昏睡|困得(?:睁不开眼|叫不醒)|睡着(?:后)?叫不醒"
VOMITING = r"呕吐|吐了|吐过|一直吐|反复吐|接连吐"


def _reported_overdose(clause: str) -> bool:
    if _affirmed(clause, r'(?:把|将)(?:一(?:整)?(?:瓶|盒|板)|一把).{0,8}(?:药|药片).{0,5}(?:都|全)(?:吃|吞|服)(?:下|了)?'):
        return True
    if _affirmed(clause, r"(?:服药|药物|吃药|用药|吞药)过量|过量(?:服药|吃药|服用|用药)|(?:药|药片).{0,6}(?:吃多了|吃过量|吃超量)|误服(?:了)?(?:药|安眠药)|吞(?:下|了)?.{0,5}(?:一(?:整)?(?:瓶|盒|板)|一把).{0,8}(?:药|药片)"):
        return True
    # A reported acute handful/large count is a warning, not an inferred safe
    # dosage threshold. Regular prescribed schedules do not use this branch.
    large_count = r"(?:1\d|[2-9]\d{1,2}|[1-9]\d{3,}|十几|十|[二两三四五六七八九]十|一百|几十)\s*(?:片|粒|颗)"
    acute = r"(?:一次|一口气|一下子|刚才|刚刚).{0,10}(?:吃|吞|服).{0,6}" + large_count + r".{0,8}(?:药|药片)"
    return _affirmed(clause, acute)


def _infant_age_months(text: str) -> float | None:
    if re.search(r"新生儿|刚满月|刚出生|出生不到(?:三|3)个月", text):
        return 1
    match = re.search(r"(?:出生)?\s*(\d{1,2}|一|二|两|三|四|五|六)\s*(?:个(?:多)?)?(?:月龄|个月|月)(?:大|的|龄|宝宝|婴儿|孩子|，|,|\s|$)", text)
    if match:
        number = match.group(1)
        return float(number) if number.isdigit() else {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6}[number]
    match = re.search(r"(?:出生|宝宝|婴儿).{0,5}(\d{1,3})\s*天", text)
    return int(match.group(1)) / 30 if match else None


def _reported_temperature(text: str) -> float | None:
    match = re.search(r"(3[5-9]|4[0-2])(?:\.(\d+)\s*(?:℃|度|摄氏度)|度([零一二三四五六七八九\d])|\s*(?:℃|度|摄氏度))", text)
    if not match:
        match = re.search(r"(?:体温|烧到|发烧到)\s*(3[5-9]|4[0-2])(?:\.(\d+)|度([零一二三四五六七八九\d]))?", text)
    if match:
        decimal = match.group(2) or match.group(3) or "0"
        if not decimal.isdigit():
            decimal = str("零一二三四五六七八九".index(decimal))
        return float(match.group(1) + "." + decimal)
    return None


def check_conversation_urgent(text: str, visit: Visit) -> Risk:
    """Check an explicitly linked recent continuation before any model I/O.

    Only the immediately preceding raw user report can supply context. We do
    not turn pending form proposals, arbitrary history, educational text or a
    different person's report into current clinical facts.
    """
    direct = check_urgent(text, visit.intake)
    if visit.risk.level == "emergency":
        if direct.kind == "self_harm":
            return direct
        return visit.risk
    if direct.level == "emergency" or not re.match(r"^(?:还|也|并且|而且|同时|伴(?:有|着)?|痛得|疼得|更|越来越)", text.strip()):
        return direct
    if HARD_EDUCATION.search(text) or EDUCATION.search(text):
        return direct
    # A newly named person is a new report, not an implicit continuation.
    if re.search(r"爸爸|父亲|妈妈|母亲|爷爷|奶奶|外公|外婆|姥姥|姥爷|哥哥|姐姐|弟弟|妹妹|叔叔|阿姨|伯伯|姑姑|舅舅|家人|孩子|宝宝|老人|朋友|同事|丈夫|妻子|患者|病人|另一个人|其他人|别人", text):
        return direct
    # Require a symptom predicate after the connector. '还有张三…' and
    # '还有我…' introduce a subject, rather than extend the prior report.
    continuation = re.sub(r"^(?:还|也|并且|而且|同时|伴(?:有|着)?|更|越来越)[，,\s]*", "", text.strip(), count=1)
    if not re.match(r"^(?:冒(?:冷|大)汗|(?:有|出现|感觉|觉得)(?:些|一些|点|有点|比较|更|很|特别|非常|严重)?(?:冷汗|大汗|胸|呼吸|气短|喉|舌|肿|头|痛|疼)|胸|呼吸|气短|喘|冷汗|大汗|喉|舌|肿|说话|痛|疼|无法呼吸|不能呼吸|严重呼吸困难|肚子|小腹|下腹|腹痛|出血|流血|阴道|吐|呕吐|困|昏睡|不吃奶|抽搐|意识|没有|不伴|并无|没|不)", continuation):
        return direct
    previous = next((message for message in reversed(visit.messages) if message.role == "user"), None)
    if previous is None:
        return direct
    try:
        stamp = datetime.fromisoformat(previous.created_at.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            return direct
        elapsed = datetime.now(timezone.utc) - stamp
        if elapsed < timedelta(0) or elapsed > timedelta(minutes=30):
            return direct
    except (ValueError, TypeError):
        return direct
    previous_clauses = current_clauses(previous.content)
    symptoms = "|".join((CHEST, BREATH, SWELLING, HEADACHE, SIDE_WEAKNESS, SPEECH, PREGNANCY, VAGINAL_BLEED, HEAD_INJURY, FAINT, SEIZURE, HAEMOPTYSIS, r"宝宝.{0,10}(?:发热|发烧)|低月龄"))
    if not any(_affirmed(clause, symptoms) for clause in previous_clauses):
        return direct
    if (EDUCATION.search(previous.content) or HARD_EDUCATION.search(previous.content)) and not EXPLICIT_CURRENT_REPORT.search(previous.content):
        return direct
    combined = check_urgent("，".join([*previous_clauses, text]), visit.intake)
    return max((direct, combined), key=lambda risk: {"unknown": 0, "prompt": 1, "emergency": 2}[risk.level])


def check_urgent(text: str, intake: Intake | None = None) -> Risk:
    # Medical history, medications, allergies and quoted past events are never
    # treated as current symptoms. Form chief complaint and notes are self report.
    candidates = [text]
    if intake:
        candidates.extend([intake.chief_complaint, intake.notes])
    clauses = [clause for candidate in candidates for clause in current_clauses(candidate)]
    current_text = "，".join(clauses)
    reasons: list[str] = []
    source_ids: list[str] = []
    prompt_reasons: list[str] = []
    prompt_sources: list[str] = []
    if any(_affirmed(clause, SELF_HARM) for clause in clauses):
        return Risk(level="emergency", kind="self_harm", message=SELF_HARM_MESSAGE,
                    reasons=["当前自述包含自伤或自杀意念，需要立即获得真人陪伴与危机支持"], source_ids=["crisis_support"])
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
        if _affirmed(clause, CONSCIOUSNESS):
            reasons.append("当前描述包含意识异常")
            source_ids.append("emergency_call")
        if _affirmed(clause, HEADACHE) and _affirmed(clause, SUDDEN) and _affirmed(clause, EXTREME_PAIN):
            reasons.append("当前描述包含突然出现的极剧烈头痛")
            source_ids.append("headache")
        if _affirmed(clause, HEADACHE) and (_affirmed(clause, SIDE_WEAKNESS) or _affirmed(clause, SPEECH)):
            reasons.append("当前描述包含头痛伴单侧无力或语言异常")
            source_ids.append("headache")
        if _reported_overdose(clause):
            reasons.append("当前描述包含可能服药过量或一次性大量服药，需要立即求助")
            source_ids.append("poisoning")
        if _affirmed(clause, BLEEDING):
            reasons.append("当前描述包含无法止住的出血")
            source_ids.append("severe_bleeding")
        if _affirmed(clause, HAEMOPTYSIS):
            if _affirmed(clause, LARGE_BLOOD) or _affirmed(clause, BREATH + "|" + CHEST):
                reasons.append("当前描述包含大量咯血或咯血伴其他危险征象")
                source_ids.append("severe_haemoptysis")
            else:
                prompt_reasons.append("咯血或痰中带血需要及时专业评估")
                prompt_sources.append("severe_haemoptysis")
        if _affirmed(clause, FAINT):
            # A fully recovered faint without other warning signs warrants
            # assessment. Unknown recovery is conservatively escalated.
            high_risk_faint = _affirmed(clause, r"(?:跑步|运动|锻炼|躺着|平躺).{0,10}(?:晕倒|昏倒|晕厥|昏厥)|(?:晕倒|昏倒).{0,10}(?:跑步|运动|锻炼|躺着|平躺)|心悸|心跳不规律")
            if _affirmed(current_text, RECOVERED) and not high_risk_faint and not any(_affirmed(current_text, sign) for sign in (CONSCIOUSNESS, CHEST, BREATH, SIDE_WEAKNESS, SPEECH)):
                prompt_reasons.append("近期晕倒后虽已完全恢复，仍需要专业评估")
                prompt_sources.append("emergency_call")
            else:
                reasons.append("近期晕倒且未确认已完全恢复，需要立即有人协助评估")
                source_ids.append("emergency_call")
        if _affirmed(clause, SEIZURE):
            familiar_episode = re.search(r"(?:有|患有|诊断为|确诊为)癫痫", "，".join(candidates))
            usual = re.search(r"和平(?:时|常)一样|像平(?:时|常)一样|与平(?:时|常)一样|通常的发作", current_text)
            plan = re.search(r"(?:医生|医务人员).{0,10}(?:方案|计划)|急救计划", current_text)
            exceptional = _affirmed(current_text, r"第一次|首次|持续(?:了)?(?:[5-9]|\d{2,}|五|六|七|八|九|十)\s*分钟|反复抽搐|连续抽搐|一阵接一阵|没有恢复清醒|没有醒过来|呼吸困难|受伤")
            if familiar_episode and usual and plan and _affirmed(current_text, RECOVERED) and not exceptional:
                prompt_reasons.append("已知癫痫的常规短暂发作并完全恢复，应遵循已有医务人员计划并及时联系专业人员")
                prompt_sources.append("seizure")
            else:
                reasons.append("近期描述包含抽搐，首次或恢复情况未确认，需要立即真人协助与专业评估")
                source_ids.append("seizure")

    pregnant = (intake is not None and intake.age_group == "孕产期") or _affirmed(current_text, PREGNANCY)
    if pregnant and _affirmed(current_text, VAGINAL_BLEED):
        small_mild = _affirmed(current_text, r"少量|一点(?:点)?|点滴|一丝|血丝") and _affirmed(current_text, r"轻微|隐隐|有一点点疼|有一点点痛")
        danger = _affirmed(current_text, LARGE_BLOOD + "|" + FAINT + "|" + CONSCIOUSNESS + "|" + EXTREME_PAIN)
        if danger or (_affirmed(current_text, ABDOMINAL_PAIN) and not small_mild):
            reasons.append("当前孕期出血伴腹痛或其他危险征象，需要立即获得产科或急救评估")
            source_ids.append("pregnancy_bleeding")
        else:
            prompt_reasons.append("孕期出血需要尽快联系产科专业人员，少量出血也需要评估")
            prompt_sources.append("pregnancy_bleeding")

    infant_age = _infant_age_months(current_text)
    if infant_age is not None and infant_age <= 6:
        temperature = _reported_temperature(current_text)
        reported_fever = _affirmed(current_text, r"发烧|发热|高热|高烧") or (temperature is not None and temperature >= 38)
        infant_danger = _affirmed(current_text, WORSENING_DROWSINESS + "|" + CONSCIOUSNESS + "|" + SEVERE_BREATH)
        poor_feed = _affirmed(current_text, r"不吃奶|拒奶|拒绝吃奶|喂不进去|不肯吃奶|不吃不喝")
        if infant_danger and (reported_fever or poor_feed):
            reasons.append("低月龄婴儿发热或拒奶，并有昏睡、难唤醒等危险表现")
            source_ids.append("infant_warning")
        elif (infant_age < 3 and reported_fever) or (3 <= infant_age <= 6 and temperature is not None and temperature >= 39) or poor_feed:
            prompt_reasons.append("低月龄婴儿发热或进食异常，需要立即联系专业医务人员")
            prompt_sources.append("infant_warning")

    if _affirmed(current_text, HEAD_INJURY):
        if _affirmed(current_text, WORSENING_DROWSINESS + "|" + CONSCIOUSNESS + "|" + SEIZURE + "|" + SIDE_WEAKNESS + "|" + SPEECH):
            reasons.append("头部受伤后出现越来越困、难唤醒或神经异常，需要立即求助")
            source_ids.append("head_injury")
        elif _affirmed(current_text, VOMITING):
            # CDC treats repeated vomiting after head trauma as a danger sign.
            if _affirmed(current_text, r"吐了(?:[2-9]|\d{2,}|两|二|三|四|五|六|七|八|九|十)次|反复(?:呕吐|吐)|接连吐|一直吐"):
                reasons.append("头部受伤后反复呕吐，需要立即专业评估")
                source_ids.append("head_injury")
            else:
                prompt_reasons.append("头部受伤后呕吐，需要及时专业评估")
                prompt_sources.append("head_injury")
    if reasons:
        return Risk(level="emergency", kind="medical", message=EMERGENCY_MESSAGE, reasons=list(dict.fromkeys(reasons)), source_ids=list(dict.fromkeys(source_ids)))
    if prompt_reasons:
        return Risk(level="prompt", kind="medical", message="请及时联系专业医务人员或就诊，说明这些实际发生的情况；症状加重或出现难唤醒、呼吸困难等表现时立即联系当地急救。" + LIMITATION,
                    reasons=list(dict.fromkeys(prompt_reasons)), source_ids=list(dict.fromkeys(prompt_sources)))
    persistent_fever = r"(?:高烧|高热|发烧|发热).{0,10}(?:三天|3天|[4-9]天|\d{2,}天).{0,6}(?:不退|没退|未退|持续)|(?:持续|连续).{0,4}(?:高热|高烧).{0,6}(?:三天|3天|[4-9]天|\d{2,}天)"
    if any(_affirmed(clause, persistent_fever) for clause in clauses):
        return Risk(level="prompt", kind="medical", message="持续发热需要及时向专业医务人员咨询或就诊；出现意识异常、呼吸困难等危险表现时立即联系当地急救。" + LIMITATION,
                    reasons=["当前描述包含多日持续高热或发热不退"], source_ids=["fever"])
    if intake and (intake.severity == "重" or intake.age_group in {"儿童", "孕产期"}):
        return Risk(level="prompt", kind="medical", message="已填报的年龄阶段或症状程度需要尽快向专业医务人员确认。" + LIMITATION, reasons=["填报为重度症状或需要特别评估的人群"])
    if any(_affirmed(clause, CHEST + r"|呼吸困难|持续高热|咳血|便血|症状加重") for clause in clauses):
        return Risk(level="prompt", kind="medical", message="你描述的部分症状需要及时向专业医务人员咨询或就诊。" + LIMITATION, reasons=["当前描述包含需要专业评估的症状"])
    return Risk(level="unknown", message=UNKNOWN_MESSAGE)


def safe_assistant_output(text: str) -> str:
    """Reject generated clinical instructions, drug doses, and diagnosis claims."""
    prohibited = re.compile(r"(?:你|您)(?:患有|得了|就是|确诊为)|诊断(?:为|是)|(?:服用|口服|注射|用药|吃药).{0,16}(?:\d|毫克|mg|克|片)|\d+(?:\.\d+)?\s*(?:mg|毫克|mcg|微克|毫升|mL)|阿莫西林|布洛芬|对乙酰氨基酚|阿司匹林|青霉素|头孢|抗生素疗程", re.I)
    if prohibited.search(text):
        raise ValueError("输出超出就诊准备范围")
    return text
