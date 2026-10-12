"""Real local HTTP dialogue checks. All visits and clinical inputs are fictional."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from acceptance import Acceptance, require


class DialogueAcceptance(Acceptance):
    def connected_turns(self):
        client, visit = self.new_visit()
        transcript = []
        for text, field in [('头疼想睡觉', 'onset'), ('昨晚开始的', 'severity'), ('轻微', 'age_group'), ('成人', 'medications'), ('没有用药', 'allergies'), ('不知道', 'medical_history'), ('没有既往病史', None)]:
            response, assistant = client.message(visit['id'], text)
            require(assistant.get('followup_field') == field, 'dialogue is stuck on an already answered field')
            require(response['json']['visit']['intake']['chief_complaint'] == '', 'spoken information silently saved')
            require(response['json']['visit']['collection']['completion'] == 0, 'unconfirmed information falsely increases saved progress')
            transcript.append({'user': text, 'assistant': assistant['content'], 'next_field': field, 'elapsed_ms': response['elapsed_ms']})
        require(len(client.request('GET', f"/api/visits/{visit['id']}")['json']['intake_candidates']) == 7, 'spoken fields were lost between turns')
        return {'transcript': transcript, 'saved_form_progress': 0, 'confirmation_required_only_for_saving': True}

    def contextual_vs_unknown(self):
        client, visit = self.new_visit()
        client.message(visit['id'], '头疼想睡觉')
        _, contextual = client.message(visit['id'], '那我该怎么办？')
        require({s['id'] for s in contextual['sources']} == {'headache'}, 'explicit continuation cannot retrieve the reported symptom')
        require(contextual.get('followup_field') is None, 'knowledge answer forces a form question')
        _, unknown = client.message(visit['id'], '这家医院的地址在哪')
        require(not unknown['sources'] and not unknown['intake_candidates'] and unknown.get('followup_field') is None, 'unknown institution reuses a prior health answer')
        return {'contextual_answer': contextual['content'], 'unknown_answer': unknown['content']}

    def pause_and_social(self):
        client, visit = self.new_visit()
        client.message(visit['id'], '头疼想睡觉')
        answers = []
        for text in ('不想填了', '我这两天继续咳嗽', '谢谢', '你能做什么'):
            _, assistant = client.message(visit['id'], text)
            require(assistant.get('followup_field') is None, 'declined collection was resumed without permission')
            answers.append({'user': text, 'assistant': assistant['content']})
        return {'answers': answers}

    def linked_warnings(self):
        evidence = []
        for first, second, expected in [('我现在胸痛', '还冒冷汗', 'emergency'), ('刚刚开始头痛', '痛得受不了', 'emergency'), ('我现在没有胸痛', '还冒冷汗', 'unknown'), ('去年曾胸痛', '还冒冷汗', 'unknown'), ('我爸爸现在胸痛', '还有我姐姐冒冷汗', 'unknown')]:
            client, visit = self.new_visit()
            client.message(visit['id'], first)
            _, assistant = client.message(visit['id'], second)
            require(assistant['risk']['level'] == expected, 'two-turn warning or context exclusion failed')
            if expected == 'emergency':
                require('120' in assistant['content'] and assistant.get('followup_field') is None, 'urgent branch continues collection')
            evidence.append({'first': first, 'second': second, 'level': assistant['risk']['level']})
        return evidence

    def run(self):
        for name, function in [('seven_connected_turns_without_form_clicks', self.connected_turns), ('contextual_question_and_unknown_institution', self.contextual_vs_unknown), ('pause_and_social_replies', self.pause_and_social), ('linked_warning_and_denial_history_person_exclusions', self.linked_warnings)]:
            self.run_case(name, function)
            print(f"{self.results[-1]['status'].upper():4} {name}", flush=True)
        cleanup = self.cleanup()
        failed = sum(item['status'] == 'fail' for item in self.results)
        return {'executed_at': datetime.now(timezone.utc).isoformat(), 'base_url': self.base_url, 'passed': len(self.results) - failed, 'failed': failed, 'total': len(self.results), 'results': self.results, 'cleanup': cleanup, 'data_policy': 'Only independent fictional visitors; use_model=false; no external clinical requests. This is dialogue and engineering validation, not clinical validation.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:8270')
    report = DialogueAcceptance(parser.parse_args().base_url).run()
    path = Path(__file__).resolve().parents[1] / 'artifacts' / 'conversation_http.json'
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return 1 if report['failed'] or report['cleanup']['cleanup_failures'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
