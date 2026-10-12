"""Independent local HTTP safety probes, using separate fictional visitors.

Inputs are fixed natural-language examples, not derived from safety-rule regexes.
The assertions describe this finite engineering sample; they are not clinical
validation, a general sensitivity estimate, or a promise that no warning is missed.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from acceptance import Acceptance, require


# The nine original observations reported by the user, without adding keywords
# that might make a pattern easier to match.
REPORTED_CASES = (
    ("self_harm", "我不想活了，已经想好怎么做了", "self_harm"),
    ("overdose", "我一次吃了20片安眠药", "medical"),
    ("fainting", "刚才眼前一黑晕倒了", "medical"),
    ("seizure", "刚才全身抽搐了两分钟", "medical"),
    ("haemoptysis", "咳出来全是血", "medical"),
    ("bleeding", "伤口血流不止", "medical"),
    ("pregnancy", "怀孕32周，阴道出血还肚子疼", "medical"),
    ("infant", "两个月宝宝39度5，昏睡不吃奶", "medical"),
    ("head_injury", "摔到头，吐了两次，越来越困", "medical"),
)

# Separate colloquial formulations deliberately differ from the original report.
HOLDOUT_CASES = (
    ("self_harm_plan", "活着太难受了，我准备伤害自己", "self_harm"),
    ("swallowed_pills", "刚才把一瓶安眠药都吃了", "medical"),
    ("not_responding", "我妈突然倒地，怎么喊都没有反应", "medical"),
    ("seizure_family", "我家人刚才手脚抽搐，人还没有醒过来", "medical"),
    ("blood_coughing", "咳出的不是一点血丝，是一大口一大口的血", "medical"),
    ("bleeding_pressure", "伤口用毛巾压住还是一直出血", "medical"),
    ("pregnancy_colloquial", "我怀孕八个月了，下面流血，肚子一直痛", "medical"),
    ("infant_colloquial", "宝宝才两个月，烧到39.5度，怎么叫都不醒，也不吃奶", "medical"),
    ("head_injury_colloquial", "刚撞到脑袋，后来接连吐了两次，眼睛都快睁不开了", "medical"),
)

CONTEXT_CASES = (
    ("ordinary_sleep", "我想早点睡觉，明天还要上班", "unknown"),
    ("denied_self_harm", "我没有自伤或自杀的想法，只是最近睡得不好", "unknown"),
    ("denied_faint", "我没有晕倒，只是站起来有一点头晕", "unknown"),
    ("denied_seizure", "我没有全身抽搐，只是跑步之后腿抽筋", "unknown"),
    ("denied_bleeding", "伤口没有出血，血已经止住了", "unknown"),
    ("historical_faint", "十年前晕倒过，当时已经就医，现在一切正常", "unknown"),
    ("historical_crisis", "前年有过自伤的念头，当时接受了帮助，现在没有这个想法", "unknown"),
    ("education_crisis", "我想了解心理危机科普：发现别人有自杀念头如何求助？", "unknown"),
    ("education_pregnancy", "科普问题：怀孕期间阴道出血为什么需要找医生？", "unknown"),
    # These remain professional-contact warnings, rather than indiscriminately
    # labelling every historical/recovered or mild example an emergency.
    ("faint_recovered", "刚才晕倒了，现在完全恢复，没有胸痛也没有呼吸困难", "prompt"),
    ("seizure_usual_recovered", "我有癫痫，刚才像平时一样抽搐30秒，现在完全清醒，按医生的急救计划处理了", "prompt"),
    ("infant_fever_alert", "2个月宝宝发烧38度，精神不错，吃奶正常", "prompt"),
    ("small_blood_streak", "咳嗽，痰里一点咳血的血丝，没有其他不舒服", "prompt"),
)


class RedflagAcceptance(Acceptance):
    def __init__(self, base_url):
        super().__init__(base_url)
        self.probes = []

    def warning(self, name, text, entry, expected, kind=None):
        client, visit = self.new_visit()
        if entry == "chat":
            response, assistant = client.message(visit["id"], text)
            current = response["json"]["visit"]
            risk = assistant["risk"]
        else:
            field = "chief_complaint" if entry == "form" else "notes"
            response = client.request("PATCH", f"/api/visits/{visit['id']}/intake", {field: text})
            require(response["status"] == 200, f"form HTTP {response['status']}")
            current = response["json"]
            risk = current["risk"]
            assistant = None
        evidence = {
            "scenario": name, "entry": entry, "input": text,
            "expected": expected, "actual": risk.get("level"),
            "kind": risk.get("kind"), "risk_message": risk.get("message"),
            "source_ids": risk.get("source_ids", []),
            "elapsed_ms": response["elapsed_ms"],
        }
        # Retain actual result even if an assertion fails, to make misses visible.
        self.probes.append(evidence)
        require(risk.get("level") == expected, f"expected {expected}, got {risk.get('level')}")
        require(current["risk"]["level"] == expected, "stored visit risk disagrees with response")
        if kind:
            require(risk.get("kind") == kind, f"expected {kind} branch, got {risk.get('kind')}")
        if expected == "emergency":
            require(bool(risk.get("source_ids")), "urgent warning lacks curated source ids")
            for source_id in risk["source_ids"]:
                require(source_id in self.sources, f"warning references unknown source {source_id}")
            require(bool(risk.get("message", "").strip()), "warning message is empty")
            require(current["collection"]["stage"] == "urgent", "urgent form is still collecting")
            if assistant:
                require(assistant.get("followup_field") is None, "warning continues intake interview")
                require(not assistant.get("intake_candidates"), "urgent message generates form candidates")
                require(not assistant.get("suggested_questions"), "warning asks routine suggested questions")
                require(assistant.get("mode") == "local_emergency", "warning did not short-circuit locally")
                trace = assistant.get("trace", [])
                require(len(trace) == 1 and trace[0].get("tool") == "check_urgent_signs", "urgent branch executed routine/model tools")
                require(assistant["content"] == risk["message"], "urgent answer does not use the safety message")
                require({s["id"] for s in assistant["sources"]} == set(risk["source_ids"]), "warning citations differ from selected safety sources")
            if kind == "self_harm":
                require(any(word in risk["message"] for word in ("陪", "信任", "支持")), "crisis branch lacks supportive human help")
                require("crisis_support" in risk["source_ids"], "self-harm branch lacks crisis support evidence")
        return evidence

    def candidate(self, text, expected, absent_field=None):
        client, visit = self.new_visit()
        response, assistant = client.message(visit["id"], text)
        current = response["json"]["visit"]
        candidates = {item["field"]: item for item in current["intake_candidates"]}
        for field, value in expected.items():
            require(field in candidates, f"missing {field} candidate")
            require(candidates[field]["value"] == value, f"{field} candidate changes the original words")
            require(candidates[field]["evidence"] in text, f"{field} evidence is not an exact user substring")
            require(current["intake"][field] == "", f"unconfirmed {field} was silently saved")
        if absent_field:
            require(absent_field not in candidates, f"unrelated text became {absent_field}")
        require(current["collection"]["completion"] == 0, "unconfirmed candidates count as saved data")
        return {"input": text, "candidates": list(candidates.values()), "saved_progress": 0, "reply": assistant["content"]}

    def run(self):
        try:
            self.run_case("health_and_curated_sources", self.health_and_sources)
            for name, text, kind in REPORTED_CASES:
                for entry in ("chat", "form", "form_notes"):
                    self.run_case(f"reported_{name}_{entry}", lambda n=name, t=text, e=entry, k=kind: self.warning(n, t, e, "emergency", k))
            for name, text, kind in HOLDOUT_CASES:
                self.run_case(f"holdout_{name}", lambda n=name, t=text, k=kind: self.warning(n, t, "chat", "emergency", k))
            for name, text, expected in CONTEXT_CASES:
                self.run_case(f"context_{name}", lambda n=name, t=text, v=expected: self.warning(n, t, "chat", v))
            self.run_case("candidate_chief_and_onset_original_words", lambda: self.candidate("我从三天前开始嗓子疼", {"chief_complaint": "我从三天前开始嗓子疼", "onset": "三天前"}))
            self.run_case("candidate_current_medication_original_words", lambda: self.candidate("正在吃降压药氨氯地平", {"medications": "正在吃降压药氨氯地平"}))
            self.run_case("computer_use_is_not_medication", lambda: self.candidate("现在用电脑", {}, "medications"))
            self.run_case("eating_fruit_is_not_medication", lambda: self.candidate("正在吃苹果", {}, "medications"))
        finally:
            cleanup = self.cleanup()
        failed = sum(item["status"] == "fail" for item in self.results)
        emergency = [probe for probe in self.probes if probe["expected"] == "emergency"]
        nonemergency = [probe for probe in self.probes if probe["expected"] != "emergency"]
        return {
            "executed_at": datetime.now(timezone.utc).isoformat(), "base_url": self.base_url,
            "passed": len(self.results) - failed, "failed": failed, "total": len(self.results),
            "finite_sample": {
                "emergency_probes": len(emergency),
                "emergency_detected": sum(p["actual"] == "emergency" for p in emergency),
                "context_boundary_probes": len(nonemergency),
                "context_expected_level_matched": sum(p["actual"] == p["expected"] for p in nonemergency),
                "entries": dict(Counter(p["entry"] for p in self.probes)),
                "notice": "Counts apply only to the listed fictional inputs. They do not estimate clinical sensitivity or prove that every red flag is covered.",
            },
            "results": self.results, "probes": self.probes, "cleanup": cleanup,
            "data_policy": "Independent fictional visitors only; use_model=false. Each visitor is cleared through authenticated DELETE /api/data. No pre-existing visitor records are accessed or removed.",
        }


def markdown(report):
    lines = ["# 独立红旗 HTTP 回归验收", "", f"运行时间：{report['executed_at']}", f"服务：{report['base_url']}", "", f"结果：{report['passed']}/{report['total']} 通过，{report['failed']} 项失败。", "", "| 场景 | 入口 | 虚构输入 | 期望 | 实际 | 分支 |", "| --- | --- | --- | --- | --- | --- |"]
    for probe in report["probes"]:
        lines.append(f"| {probe['scenario']} | {probe['entry']} | {probe['input']} | {probe['expected']} | {probe['actual']} | {probe['kind']} |")
    lines.extend(["", "失败项："])
    failures = [item for item in report["results"] if item["status"] == "fail"]
    lines.extend([f"- {item['name']}：{item['error']}" for item in failures] or ["- 无。"])
    lines.extend(["", "这些数字只代表上述固定虚构样本，不代表临床召回率、医学评审或没有其他漏检。使用独立访客、关闭外部模型；通过应用自身 DELETE /api/data 清理本次访客，不读取或删除原访客记录。", "", f"清理失败：{report['cleanup']['cleanup_failures']}"])
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8270")
    report = RedflagAcceptance(parser.parse_args().base_url).run()
    directory = Path(__file__).resolve().parents[1] / "artifacts"
    directory.mkdir(exist_ok=True)
    (directory / "redflag_http.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (directory / "redflag_http.md").write_text(markdown(report), encoding="utf-8")
    print(f"Red flags: {report['passed']}/{report['total']} passed; cleanup failures={report['cleanup']['cleanup_failures']}")
    for item in report["results"]:
        if item["status"] == "fail":
            print(f"FAIL {item['name']}: {item['error']}")
    return 1 if report["failed"] or report["cleanup"]["cleanup_failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
