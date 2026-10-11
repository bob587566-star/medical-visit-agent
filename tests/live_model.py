"""Explicit, opt-in smoke test with a fixed fictional input. Never reads records."""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from acceptance import Client


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--external", action="store_true", help="send limited fictional symptom fragments to the configured DeepSeek")
    parser.add_argument("--base-url", default="http://127.0.0.1:8270")
    args = parser.parse_args()
    if not args.external:
        print("未调用外部服务。仅 --external 显式选择后测试固定虚构资料。")
        return 0
    client = Client(args.base_url)
    bootstrap = client.bootstrap()["json"]
    if not bootstrap["model_available"]:
        print("模型未配置，本机模式仍可使用。")
        return 1
    record = client.create("固定虚构模型连通验收")
    started = time.monotonic()
    try:
        result = client.request("POST", f"/api/visits/{record['id']}/messages", {
            "content": "虚构演示：咳嗽两天，目前轻微。", "use_model": True,
        })
        assistant = (result["json"] or {}).get("assistant_message") or {}
        report = {
            "status": result["status"], "mode": assistant.get("mode"),
            "duration_ms": round((time.monotonic() - started) * 1000),
            "nonempty_answer": bool(assistant.get("content", "").strip()),
            "has_sources": bool(assistant.get("sources")),
            "input": "固定虚构咳嗽症状，发送有限别名与时间／程度片段，无真实患者资料",
        }
        (Path(__file__).resolve().parents[1] / "artifacts" / "live_model_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False))
        return 0 if report["status"] == 200 and report["nonempty_answer"] else 1
    finally:
        client.request("DELETE", "/api/data")


if __name__ == "__main__":
    raise SystemExit(main())
