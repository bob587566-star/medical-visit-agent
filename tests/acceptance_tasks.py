"""Real local asynchronous task acceptance with independent fictional visitors.

External planning is opt-in. That case verifies actual planner_mode and reports
fallback honestly; it does not send real health records or expose credentials.
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from acceptance import Acceptance, require


FULL_INTAKE = {
    "chief_complaint": "虚构验收：轻微嗓子疼",
    "onset": "三天前",
    "age_group": "成人",
    "severity": "轻",
    "medications": "未用药",
    "allergies": "无已知过敏",
    "medical_history": "无既往病史",
    "notes": "全部内容为虚构工程验收资料",
}


class TaskAcceptance(Acceptance):
    def __init__(self, base_url, external_planner=False):
        super().__init__(base_url)
        self.external_planner = external_planner
        self.external_result = {"requested": external_planner, "attempted": False}

    def prepare(self, full=True):
        client, visit = self.new_visit()
        intake = FULL_INTAKE.copy() if full else {"chief_complaint": "虚构验收：轻微嗓子疼"}
        response = client.request("PATCH", f"/api/visits/{visit['id']}/intake", intake)
        require(response["status"] == 200, "fictional intake save failed")
        return client, response["json"], f"/api/visits/{visit['id']}"

    def execute(self, client, base, goal="prepare_visit", use_model=False):
        response = client.request("POST", base + "/tasks", {"goal": goal, "use_model": use_model})
        require(response["status"] == 202, f"task start HTTP {response['status']}")
        queued = response["json"]["task"]
        require(queued["status"] == "queued" and queued["result"] is None, "task does not expose an actual queued draft state")
        statuses = [queued["status"]]
        started = time.monotonic()
        while time.monotonic() - started < 90:
            read = client.request("GET", base + "/tasks/" + queued["id"])
            require(read["status"] == 200, f"task poll HTTP {read['status']}")
            task = read["json"]
            if task["status"] != statuses[-1]:
                statuses.append(task["status"])
            if task["status"] not in {"queued", "running"}:
                require(isinstance(task.get("result"), dict), "terminal task has no structured execution result")
                require(task["result"].get("id") == task["id"], "job and execution result ids disagree")
                return task, {"observed_states": statuses, "elapsed_ms": round((time.monotonic() - started) * 1000, 2), "initial_http_status": 202}
            time.sleep(.2)
        raise AssertionError("task did not reach a terminal state within 90 seconds")

    @staticmethod
    def details(task, polling):
        result = task["result"]
        return {
            **polling, "status": task["status"], "result_status": result["status"],
            "goal": result["goal"], "planner_mode": result["plan"]["planner_mode"],
            "planner_error": result.get("planner_error"), "can_apply": task["can_apply"],
            "tools": [{"tool": step["tool"], "status": step["status"], "depends_on": step["depends_on"], "executed_by": step["executed_by"]} for step in result["steps"]],
            "evidence_count": len(result["evidence_ledger"]), "knowledge_version": result["knowledge_version"],
        }

    def local_goal(self, goal, full=True):
        client, before, base = self.prepare(full)
        if goal == "organize_timeline":
            observation = client.request("POST", base + "/observations", {"kind": "symptom", "value": "虚构：晚间轻微咽部不适", "unit": "", "recorded_at": "2026-10-12T19:00:00+08:00", "notes": "原始观察，不解释"})
            require(observation["status"] in {200, 201}, "fictional observation creation failed")
            before = client.request("GET", base)["json"]
        task, polling = self.execute(client, base, goal)
        require(task["status"] == ("completed" if full else "needs_input"), "wrong local task terminal status")
        require(task["result"]["plan"]["planner_mode"] == "local", "local task falsely reports model planning")
        require(all(step["status"] == "done" for step in task["result"]["steps"]), "local tool failed or fabricated completion")
        after = client.request("GET", base)["json"]
        require(after == before, "task automatically changed stored visit")
        if goal == "understand_symptom":
            require(bool(task["result"]["sources"]), "known symptom task lacks knowledge evidence")
            sources = {source["id"]: source for source in task["result"]["sources"]}
            evidence = [entry for entry in task["result"]["evidence_ledger"] if entry["kind"] == "source"]
            require(bool(evidence), "knowledge task lacks exact source clauses")
            for entry in evidence:
                source = sources[entry["source_id"]]
                require(source["content"][entry["start"]:entry["end"]] == entry["exact_span"] == entry["clause"], "source evidence is not an exact source span")
        return {**self.details(task, polling), "visit_unchanged_until_apply": True}

    def explicit_apply(self):
        client, before, base = self.prepare()
        task, polling = self.execute(client, base)
        require(task["can_apply"] and before["summary"] is None, "prepared draft is not explicitly eligible")
        applied = client.request("POST", base + f"/tasks/{task['id']}/apply", {})
        require(applied["status"] == 200, f"explicit apply HTTP {applied['status']}")
        require(applied["json"]["task"]["applied_at"] and applied["json"]["visit"]["summary"], "explicit apply did not persist summary and marker")
        require(applied["json"]["visit"]["intake"] == before["intake"], "applying a draft changed confirmed health facts")
        stored = client.request("GET", base)["json"]
        require(stored["summary"] == task["result"]["summary"], "stored summary differs from confirmed draft")
        retry = client.request("POST", base + f"/tasks/{task['id']}/apply", {})
        require(retry["status"] == 409, "duplicate apply should be explicitly rejected")
        require(client.request("GET", base)["json"] == stored, "duplicate apply mutated saved data")
        reread = client.request("GET", base + f"/tasks/{task['id']}")["json"]
        require(reread["applied_at"] and not reread["can_apply"], "applied result remains eligible on fresh read")
        return {**self.details(task, polling), "apply_http": 200, "duplicate_apply_http": 409, "facts_unchanged": True}

    def owner_and_csrf(self):
        client, before, base = self.prepare()
        rejected = client.request("POST", base + "/tasks", {"goal": "prepare_visit", "use_model": False}, csrf=False)
        require(rejected["status"] == 403, "task creation accepts missing CSRF")
        task, polling = self.execute(client, base)
        stranger = self.client()
        attempts = [
            ("GET", base + "/tasks", None),
            ("GET", base + f"/tasks/{task['id']}", None),
            ("POST", base + "/tasks", {"goal": "prepare_visit", "use_model": False}),
            ("POST", base + f"/tasks/{task['id']}/apply", {}),
        ]
        statuses = []
        for method, path, body in attempts:
            status = stranger.request(method, path, body)["status"]
            require(status == 404, f"cross-visitor {method} task request got {status}")
            statuses.append({"method": method, "endpoint": path.rsplit("/", 1)[-1] if method == "POST" else "task_read", "status": status})
        require(client.request("GET", base)["json"] == before, "cross-visitor probe mutated owner record")
        return {"csrf_rejection_http": 403, "cross_visitor_attempts": statuses, "owner_data_unchanged": True}

    def stale_result(self):
        client, before, base = self.prepare()
        task, polling = self.execute(client, base)
        newer = FULL_INTAKE.copy()
        newer["notes"] = "虚构：任务完成后补充新资料"
        saved = client.request("PATCH", base + "/intake", newer)
        require(saved["status"] == 200, "updating fictional intake failed")
        current = client.request("GET", base + f"/tasks/{task['id']}")["json"]
        require(current["status"] == "superseded" and not current["can_apply"] and current["invalidated_reason"], "stale task display is not superseded")
        require(current["result"] == task["result"], "stale display rewrote historical execution")
        blocked = client.request("POST", base + f"/tasks/{task['id']}/apply", {})
        require(blocked["status"] == 409, "stale draft can overwrite newer data")
        require(client.request("GET", base)["json"] == saved["json"], "stale apply mutated current record")
        return {"status": "superseded", "can_apply": False, "apply_http": 409, "historical_result_unchanged": True}

    def new_red_alert(self):
        client, before, base = self.prepare()
        task, polling = self.execute(client, base)
        _, assistant = client.message(before["id"], "现在胸口疼，冒冷汗")
        require(assistant["risk"]["level"] == "emergency", "fictional new warning not detected")
        current = client.request("GET", base + f"/tasks/{task['id']}")["json"]
        listed = next(item for item in client.request("GET", base + "/tasks")["json"]["items"] if item["id"] == task["id"])
        for display in (current, listed):
            require(display["status"] == "emergency" and display["current_risk"]["level"] == "emergency", "task display ignores current red alert")
            require(not display["can_apply"] and display["invalidated_reason"], "red-alert task remains applicable")
            require(display["result"] == task["result"], "red-alert status rewrites historical execution")
        blocked = client.request("POST", base + f"/tasks/{task['id']}/apply", {})
        require(blocked["status"] == 409, "red-alert record accepts task draft")
        require(client.request("GET", base)["json"]["summary"] is None, "blocked safety apply stored a summary")
        return {"detail_and_list_status": "emergency", "can_apply": False, "apply_http": 409, "historical_result_status": task["result"]["status"], "historical_result_unchanged": True}

    def urgent_task_short_circuit(self):
        client, visit = self.new_visit()
        saved = client.request("PATCH", f"/api/visits/{visit['id']}/intake", {"chief_complaint": "虚构验收：伤口血流不止"})
        require(saved["status"] == 200 and saved["json"]["risk"]["level"] == "emergency", "urgent form not detected")
        task, polling = self.execute(client, f"/api/visits/{visit['id']}")
        require(task["status"] == "emergency" and not task["can_apply"], "urgent task does not short-circuit")
        require(task["result"]["plan"]["planner_mode"] == "local", "urgent task uses external planner")
        require(len(task["result"]["steps"]) == 1 and task["result"]["steps"][0]["executed_by"] == "local_safety", "urgent task executes routine tools")
        return self.details(task, polling)

    def audit_metadata(self):
        client, visit, base = self.prepare()
        self.execute(client, base)
        response = client.request("GET", "/api/tasks/audit")
        require(response["status"] == 200, "audit read failed")
        audit = response["json"]
        require(audit["verified"] and audit["event_count"] >= 3, "task audit integrity/events missing")
        require("嗓子疼" not in json.dumps(audit, ensure_ascii=False), "audit exposes raw clinical input")
        return {"verified": True, "event_count": audit["event_count"], "raw_clinical_input_in_events": False, "notice": "Local HMAC integrity check, not external immutable auditing."}

    def external(self):
        client, before, base = self.prepare()
        boot = client.request("GET", "/api/bootstrap")["json"]
        self.external_result["model_available"] = bool(boot.get("model_available"))
        if not boot.get("model_available"):
            self.external_result.update({"attempted": False, "reason": "model_unavailable"})
            return self.external_result
        self.external_result["attempted"] = True
        task, polling = self.execute(client, base, use_model=True)
        mode = task["result"]["plan"]["planner_mode"]
        self.external_result.update({"planner_mode": mode, "planner_error": task["result"].get("planner_error"), "status": task["status"], "model_planner_succeeded": mode == "model", "notice": "Only goal enum, filled field names, observation count and public source IDs are supplied to the external planner; clinical field values, chat history and identity are not included."})
        require(mode in {"model", "fallback"}, "explicit planner attempt has an invalid mode")
        require(task["status"] == "completed", "external/fallback task did not complete actual local tools")
        require(client.request("GET", base)["json"] == before, "external planning changed original record")
        return {**self.details(task, polling), **self.external_result}

    def run(self):
        cases = [
            ("local_prepare_needs_input_readonly", lambda: self.local_goal("prepare_visit", False)),
            ("local_prepare_completed_readonly", lambda: self.local_goal("prepare_visit")),
            ("local_understand_with_exact_source_evidence", lambda: self.local_goal("understand_symptom")),
            ("local_timeline_uses_original_observation", lambda: self.local_goal("organize_timeline")),
            ("explicit_apply_and_duplicate_rejection", self.explicit_apply),
            ("csrf_and_cross_visitor_isolation", self.owner_and_csrf),
            ("updated_record_supersedes_historical_task", self.stale_result),
            ("new_red_alert_invalidates_card_and_apply", self.new_red_alert),
            ("urgent_task_local_safety_short_circuit", self.urgent_task_short_circuit),
            ("audit_is_integrity_checked_metadata", self.audit_metadata),
        ]
        if self.external_planner:
            cases.append(("explicit_external_planner_actual_mode", self.external))
        try:
            for name, operation in cases:
                self.run_case(name, operation)
                print(f"{self.results[-1]['status'].upper():4} {name}", flush=True)
        finally:
            cleanup = self.cleanup()
        failed = sum(item["status"] == "fail" for item in self.results)
        return {"executed_at": datetime.now(timezone.utc).isoformat(), "base_url": self.base_url, "passed": len(self.results) - failed, "failed": failed, "total": len(self.results), "results": self.results, "external_planner": self.external_result, "cleanup": cleanup, "data_policy": "Independent fictional visitors only; own sessions cleared through authenticated DELETE /api/data. Only --external-planner enables one metadata-only planner attempt. No credentials, cookies or CSRF values are written to reports."}


def markdown(report):
    lines = ["# 异步任务真实 HTTP 验收", "", f"时间：{report['executed_at']}", f"服务：{report['base_url']}", "", f"结果：{report['passed']}/{report['total']} 通过，{report['failed']} 项失败。", "", "| 场景 | 结果 |", "| --- | --- |"]
    lines.extend(f"| {item['name']} | {item['status']} |" for item in report["results"])
    lines.extend(["", "外部规划实际结果：", "", "```json", json.dumps(report["external_planner"], ensure_ascii=False, indent=2), "```", "", "失败项："])
    failures = [item for item in report["results"] if item["status"] == "fail"]
    lines.extend([f"- {item['name']}：{item['error']}" for item in failures] or ["- 无。"])
    lines.extend(["", f"独立访客清理失败：{report['cleanup']['cleanup_failures']}", "", "仅为有限虚构工程样本；不代表临床验收或真实患者数据部署。外部规划的 fallback 会明确标记，不计作模型规划成功。"])
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8270")
    parser.add_argument("--external-planner", action="store_true", help="Explicitly allow one metadata-only external planning attempt with a fictional visit.")
    args = parser.parse_args()
    report = TaskAcceptance(args.base_url, args.external_planner).run()
    directory = Path(__file__).resolve().parents[1] / "artifacts"
    directory.mkdir(exist_ok=True)
    (directory / "task_http.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (directory / "task_http.md").write_text(markdown(report), encoding="utf-8")
    print(f"Tasks: {report['passed']}/{report['total']} passed; planner={report['external_planner']}; cleanup failures={report['cleanup']['cleanup_failures']}")
    for item in report["results"]:
        if item["status"] == "fail":
            print(f"FAIL {item['name']}: {item['error']}")
    return 1 if report["failed"] or report["cleanup"]["cleanup_failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
