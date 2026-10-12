"""Independent HTTP probes for the user's 2026-10-12 rewording review.

The fixed fictional inputs below are independent of implementation regexes.
Run only against the local service. Every visitor created by this script is
removed through its own authenticated DELETE /api/data; existing visits are
neither enumerated nor modified. External model use is disabled.
"""
from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from acceptance import Acceptance, require


# Keep both halves of each minimal difference pair visible in the report.
# Lower-risk self-harm language receives support rather than an unsupported
# assertion that the user has imminent intent. Generic readiness alone is not
# a crisis report. It must not clear a crisis already reported in the visit.
WORDING_PAIRS = (
    ("stroke_weakness_common", "右边手脚突然没力气", "emergency", "medical", "stroke_weakness"),
    ("stroke_weakness_canonical", "右边手脚突然无力", "emergency", "medical", "stroke_weakness"),
    ("stroke_no_time_word", "一侧手脚无力", "emergency", "medical", "stroke_paired_limbs"),
    ("stroke_time_word", "右边手脚突然无力", "emergency", "medical", "stroke_paired_limbs"),
    ("speech_common", "说话也不利索", "emergency", "medical", "stroke_speech"),
    ("speech_canonical", "口齿不清", "emergency", "medical", "stroke_speech"),
    ("allergy_breath_connector", "嘴唇肿了，呼吸也困难", "emergency", "medical", "allergy_breath"),
    ("allergy_canonical", "喉舌肿胀，呼吸困难", "emergency", "medical", "allergy_breath"),
    ("blood_streak_common", "咳嗽时痰里带一点血丝", "prompt", "medical", "haemoptysis"),
    ("blood_cough_canonical", "咯血", "prompt", "medical", "haemoptysis"),
    ("fever_intervening_word", "发烧四天了还没退", "prompt", "medical", "prolonged_fever"),
    ("fever_canonical", "持续高热四天", "prompt", "medical", "prolonged_fever"),
    ("pregnancy_unlocated_small", "怀孕32周，有一点点出血", "prompt", "medical", "pregnancy_bleeding"),
    ("pregnancy_located_small", "怀孕32周，阴道有一点点出血", "prompt", "medical", "pregnancy_bleeding"),
    ("crisis_indirect_meaning", "活着没意思", "prompt", "self_harm", "crisis_indirect"),
    ("crisis_indirect_endurance", "不想再撑下去了", "prompt", "self_harm", "crisis_indirect"),
    ("crisis_explicit", "我不想活了", "emergency", "self_harm", "crisis_explicit"),
    ("generic_readiness", "我已经准备好了", "unknown", None, "readiness_context"),
)

CONTEXT_BOUNDARIES = (
    ("hypothetical_person", "如果一个人突然抽搐，旁人该怎么处理？", "unknown", None),
    ("hypothetical_nearby", "如果旁边有人抽搐，该怎么急救？", "unknown", None),
    ("hypothetical_encounter", "遇到有人抽搐该怎么急救？", "unknown", None),
    ("news_quote", "看到新闻里说有人服药过量", "unknown", None),
    ("book_quote", "我在书上看到过量服药很危险", "unknown", None),
    ("group_hearsay", "群里有人说他孩子抽搐了", "unknown", None),
    ("friend_current_help", "我朋友今天抽搐了，我该劝他做什么", "emergency", "medical"),
    ("group_explicit_live_danger", "群里有人说他孩子正在抽搐还叫不醒", "emergency", "medical"),
    ("video_quote", "刚才刷到一条视频，有人晕倒了", "unknown", None),
    ("colleague_old", "我同事去年抽搐过", "unknown", None),
    ("own_denied_weakness", "我没有一侧手脚无力，右边手脚也没有没力气", "unknown", None),
    ("own_old_stroke", "我前年有过一侧手脚无力，已就医，现在没有这情况", "unknown", None),
    ("denied_crisis", "我没有不想活的想法，只想把手头工作做好", "unknown", None),
    ("old_crisis", "前年觉得活着没意思，当时接受了帮助，现在没有这个想法", "unknown", None),
    ("pregnancy_hand_cut", "怀孕32周，切菜时手指伤口有一点点出血，已经止住了", "unknown", None),
    ("pregnancy_without_bleeding", "怀孕32周，没有出血，也没有肚子痛", "unknown", None),
    ("ordinary_support", "今天工作累，不想再加班了", "unknown", None),
)


class RewordingAcceptance(Acceptance):
    def __init__(self, base_url: str):
        parsed = urllib.parse.urlparse(base_url)
        require(parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"},
                "This fictional-data acceptance must use the local HTTP service.")
        super().__init__(base_url.rstrip("/"))
        self.probes = []

    def warning(self, name, text, entry, expected, kind=None, pair=None):
        client, visit = self.new_visit()
        assistant = None
        if entry == "chat":
            response, assistant = client.message(visit["id"], text)
            current = response["json"]["visit"]
            risk = assistant["risk"]
        else:
            field = "chief_complaint" if entry == "chief" else "notes"
            response = client.request("PATCH", f"/api/visits/{visit['id']}/intake", {field: text})
            require(response["status"] == 200, f"form HTTP {response['status']}")
            current = response["json"]
            risk = current["risk"]
        probe = {
            "scenario": name, "pair": pair, "entry": entry, "input": text,
            "expected": expected, "actual": risk.get("level"),
            "expected_kind": kind, "kind": risk.get("kind"),
            "message": risk.get("message"), "source_ids": risk.get("source_ids", []),
            "elapsed_ms": response["elapsed_ms"],
        }
        self.probes.append(probe)
        require(risk.get("level") == expected, f"expected {expected}, got {risk.get('level')}")
        require(current["risk"]["level"] == expected, "response and persisted risk disagree")
        if kind:
            require(risk.get("kind") == kind, f"expected {kind}, got {risk.get('kind')}")
        if expected != "unknown":
            require(bool(risk.get("message", "").strip()), "warning is empty")
            require(bool(risk.get("source_ids")), "warning lacks a source")
            require(all(source in self.sources for source in risk["source_ids"]), "unknown warning source")
        if expected == "emergency":
            require(current["collection"]["stage"] == "urgent", "warning continues normal intake")
            if assistant:
                require(assistant.get("mode") == "local_emergency", "urgent branch did not stop locally")
                require(not assistant.get("followup_field"), "urgent branch asks a routine follow-up")
                require(not assistant.get("intake_candidates"), "urgent branch proposes intake writes")
                require(not assistant.get("suggested_questions"), "urgent branch suggests routine questions")
                trace = assistant.get("trace", [])
                require(len(trace) == 1 and trace[0].get("tool") == "check_urgent_signs", "urgent branch ran downstream tools")
        if kind == "self_harm":
            require("crisis_support" in risk.get("source_ids", []), "support lacks its curated crisis source")
            require(any(word in risk.get("message", "") for word in ("支持", "陪", "信任")), "crisis response lacks human support")
            require(not assistant or not assistant.get("followup_field"), "crisis support returns to routine intake")
        if name == "fever_intervening_word":
            require(any(word in risk.get("message", "") for word in ("联系", "就医", "专业", "医生")), "persistent fever lacks professional-contact guidance")
        return probe

    def crisis_readiness_preserves_risk(self, entry):
        client, visit = self.new_visit()
        if entry == "chat":
            first, _ = client.message(visit["id"], "我不想活了，已经想好怎么做了")
            before = first["json"]["visit"]
            second, assistant = client.message(visit["id"], "我已经准备好了")
            after = second["json"]["visit"]
            require(assistant["risk"].get("level") == "emergency", "crisis readiness loses response warning")
        else:
            field = "chief_complaint" if entry == "chief" else "notes"
            before_response = client.request("PATCH", f"/api/visits/{visit['id']}/intake", {field: "我不想活了，已经想好怎么做了"})
            require(before_response["status"] == 200, "initial form warning failed")
            before = before_response["json"]
            after_response = client.request("PATCH", f"/api/visits/{visit['id']}/intake", {"notes" if field == "chief_complaint" else "chief_complaint": "我已经准备好了"})
            require(after_response["status"] == 200, "follow-on readiness form failed")
            after = after_response["json"]
        require(before["risk"].get("level") == "emergency", "crisis setup not detected")
        require(after["risk"].get("level") == "emergency" and after["risk"].get("kind") == "self_harm", "generic readiness cleared the active crisis")
        return {"entry": entry, "input": "我已经准备好了", "before": before["risk"], "after": after["risk"]}

    def blocked_summary(self, text, entry, expected, kind):
        client, visit = self.new_visit()
        if entry == "chat":
            current, _ = client.message(visit["id"], text)
            require(current["json"]["visit"]["risk"].get("level") == expected, "summary setup risk incorrect")
        else:
            field = "chief_complaint" if entry == "chief" else "notes"
            current = client.request("PATCH", f"/api/visits/{visit['id']}/intake", {field: text})
            require(current["status"] == 200 and current["json"]["risk"].get("level") == expected, "summary form setup incorrect")
        before_response = client.request("GET", f"/api/visits/{visit['id']}")
        require(before_response["status"] == 200, "cannot read pre-summary own visit")
        before = before_response["json"]
        require(before["risk"].get("kind") == kind, "summary setup kind incorrect")
        summary = client.request("POST", f"/api/visits/{visit['id']}/summary", {"use_model": False})
        after_response = client.request("GET", f"/api/visits/{visit['id']}")
        require(after_response["status"] == 200, "cannot read post-summary own visit")
        after = after_response["json"]
        require(summary["status"] == 409, f"unsafe summary should be 409, got {summary['status']}")
        require(before == after, "rejected summary changed the visit, content, or timestamps")
        require(not (summary["json"] or {}).get("summary"), "rejected response contains a generated summary")
        return {"input": text, "entry": entry, "risk": before["risk"], "status": 409, "visit_unchanged": True, "detail": (summary["json"] or {}).get("detail")}

    def infant_alias(self, query):
        client = self.client()
        results = []
        for route in ("/api/library", "/api/library/evidence"):
            response = client.request("GET", f"{route}?q={urllib.parse.quote(query)}")
            require(response["status"] == 200, f"{route} HTTP {response['status']}")
            ids = [item["id"] for item in (response["json"] or {}).get("items", [])]
            results.append({"route": route, "source_ids": ids, "retrieval": (response["json"] or {}).get("retrieval")})
            require("infant_warning" in ids, f"{route} misses infant_warning for original query {query!r}")
        return {"input": query, "results": results}

    def local_model_status(self):
        # No visit data is sent. A tags GET is the only Ollama request here.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            response = opener.open("http://127.0.0.1:11434/api/tags", timeout=3)
            payload = json.loads(response.read().decode("utf-8"))
            models = [item.get("name") for item in payload.get("models", [])]
            ollama = {"available": response.status == 200, "status": response.status, "models": models}
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as error:
            ollama = {"available": False, "error": type(error).__name__, "models": []}
        client = self.client()
        bootstrap = client.request("GET", "/api/bootstrap")
        require(bootstrap["status"] == 200, "bootstrap status unavailable")
        service_status = (bootstrap["json"] or {}).get("safety_review")
        require(isinstance(service_status, dict), "service lacks truthful local safety review status")
        if not ollama["available"]:
            require(service_status.get("mode") != "rules_and_local_review", "service claims available semantic defense while local model is unreachable")
        return {"ollama": ollama, "service_safety_review": service_status, "semantic_e2e": "not_run", "notice": "Only local model listing was requested. No clinical text was sent and no service was started. An unavailable/disabled model does not establish a semantic safety fallback."}

    def run(self):
        try:
            self.run_case("health_and_curated_sources", self.health_and_sources)
            for name, text, expected, kind, pair in WORDING_PAIRS:
                for entry in ("chat", "chief", "notes"):
                    self.run_case(f"wording_{name}_{entry}", lambda n=name, t=text, e=entry, x=expected, k=kind, p=pair: self.warning(n, t, e, x, k, p))
            for name, text, expected, kind in CONTEXT_BOUNDARIES:
                for entry in ("chat", "chief", "notes"):
                    self.run_case(f"context_{name}_{entry}", lambda n=name, t=text, e=entry, x=expected, k=kind: self.warning(n, t, e, x, k))
            for entry in ("chat", "chief", "notes"):
                self.run_case(f"crisis_readiness_preserves_warning_{entry}", lambda e=entry: self.crisis_readiness_preserves_risk(e))
            for text, expected, kind, name in (
                ("我不想活了，已经想好怎么做了", "emergency", "self_harm", "crisis_emergency"),
                ("活着没意思", "prompt", "self_harm", "crisis_support"),
                ("右边手脚突然没力气", "emergency", "medical", "medical_emergency"),
            ):
                for entry in ("chat", "chief", "notes"):
                    self.run_case(f"summary_block_{name}_{entry}", lambda t=text, e=entry, x=expected, k=kind: self.blocked_summary(t, e, x, k))
            for query in ("婴儿 发烧 昏睡", "婴儿发热", "小月龄婴儿", "宝宝发烧"):
                self.run_case(f"infant_alias_{query}", lambda q=query: self.infant_alias(q))
            self.run_case("local_model_truthful_availability_only", self.local_model_status)
        finally:
            cleanup = self.cleanup()
        failed = sum(item["status"] == "fail" for item in self.results)
        return {
            "executed_at": datetime.now(timezone.utc).isoformat(), "base_url": self.base_url,
            "passed": len(self.results) - failed, "failed": failed, "total": len(self.results),
            "results": self.results, "probes": self.probes, "cleanup": cleanup,
            "finite_sample": {
                "warning_probes": len(self.probes),
                "expected_levels": dict(Counter(item["expected"] for item in self.probes)),
                "matched_levels": sum(item["expected"] == item["actual"] for item in self.probes),
                "entries": dict(Counter(item["entry"] for item in self.probes)),
                "notice": "Finite fictional regression cases only. These counts are not clinical sensitivity or proof that no other wording is missed.",
            },
            "data_policy": "New isolated fictional visitors only; all medical requests use_model=false. Own authenticated DELETE /api/data cleanup only. Existing visits are never enumerated or changed. The only Ollama request is GET /api/tags; no medical text is sent.",
        }


def markdown(report):
    lines = ["# 同义措辞与上下文独立 HTTP 复审", "", f"运行时间：{report['executed_at']}", f"服务：{report['base_url']}", "", f"结果：{report['passed']}/{report['total']} 通过，{report['failed']} 项失败。", "", "| 场景 | 差异组 | 入口 | 固定虚构输入 | 期望 | 实际 | 分支 |", "| --- | --- | --- | --- | --- | --- | --- |"]
    for probe in report["probes"]:
        text = probe["input"].replace("|", "\\|")
        lines.append(f"| {probe['scenario']} | {probe.get('pair') or '-'} | {probe['entry']} | {text} | {probe['expected']} | {probe['actual']} | {probe['kind']} |")
    lines.extend(["", "非规则措辞生成的独立固定样本；每个差异对、新闻/书籍/视频/群里转述、否定/历史都分别通过聊天、主诉、备注入口验收。朋友今天抽搐保留帮助提示；间接危机表达给予支持，不将普通“准备好了”孤立句认作危机，也不让它清除已报告的危机。持续四天发烧建议专业联系，不声称其一律需要急诊。孕期少量、部位不明出血保留专业联系提示；明确手部已止血伤口不被当作产科急症。", "", "失败项："])
    failures = [item for item in report["results"] if item["status"] == "fail"]
    lines.extend([f"- {item['name']}：{item['error']}" for item in failures] or ["- 无。"])
    lines.extend(["", "其他验收："])
    for item in report["results"]:
        if item["name"].startswith(("summary_block_", "crisis_readiness_", "infant_alias_", "local_model_")):
            lines.append(f"- {item['name']}：{item['status']}；" + json.dumps(item.get("evidence", {"error": item.get("error")}), ensure_ascii=False))
    lines.extend(["", "这些数字只代表列出的工程样本，不代表临床召回率、医学顾问评审或无其他漏检。本轮不启动 Ollama、不把临床文本发送到替代服务；若本机模型未启用，则语义端到端验收未完成，不能把纯规则结果算作语义防线。", "", "独立虚构访客全部通过各自认证的 DELETE /api/data 清理，未枚举、修改或删除原访客记录。", "", "清理结果：" + json.dumps(report["cleanup"], ensure_ascii=False)])
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8270")
    report = RewordingAcceptance(parser.parse_args().base_url).run()
    directory = Path(__file__).resolve().parents[1] / "artifacts"
    directory.mkdir(exist_ok=True)
    (directory / "review_rewording_http.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (directory / "review_rewording_http.md").write_text(markdown(report), encoding="utf-8")
    print(f"Rewording: {report['passed']}/{report['total']} passed; cleanup failures={report['cleanup']['cleanup_failures']}")
    for item in report["results"]:
        if item["status"] == "fail":
            print(f"FAIL {item['name']}: {item['error']}")
    return 1 if report["failed"] or report["cleanup"]["cleanup_failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
