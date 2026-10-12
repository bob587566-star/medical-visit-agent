"""Exercise V2 workflows through the running local HTTP service with fictional data."""
from __future__ import annotations

import argparse
import concurrent.futures
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from acceptance import Acceptance, Client, INTAKE, require


class WorkspaceAcceptance(Acceptance):
    def collection_confirmation(self):
        client, visit = self.new_visit()
        base = f"/api/visits/{visit['id']}"
        response, _ = client.message(visit['id'], '我咳嗽三天了')
        current = response['json']['visit']
        require(current['intake']['chief_complaint'] == '', 'chat silently wrote an intake fact')
        candidates = list(current['intake_candidates'])
        require({'chief_complaint', 'onset'} <= {c['field'] for c in candidates}, 'missing explicit self-report candidates')
        for candidate in candidates:
            saved = client.request('POST', base + f"/candidates/{candidate['id']}/confirm", {})
            require(saved['status'] == 200, 'candidate confirmation failed')
            require(saved['json']['intake'][candidate['field']] == candidate['value'], 'confirmation altered candidate')
            require(client.request('POST', base + f"/candidates/{candidate['id']}/confirm", {})['status'] == 404, 'consumed candidate can be reused')
        for field, answer in [('age_group', '我是成年人'), ('severity', '我觉得程度是轻'), ('medications', '我没有用药'), ('allergies', '我没有过敏'), ('medical_history', '我没有既往病史')]:
            before = client.request('GET', base)['json']['intake'][field]
            response, _ = client.message(visit['id'], answer)
            current = response['json']['visit']
            require(current['intake'][field] == before, f'{field} silently saved')
            candidate = next((c for c in current['intake_candidates'] if c['field'] == field), None)
            require(candidate is not None, f'{field} missing candidate')
            require(client.request('POST', base + f"/candidates/{candidate['id']}/confirm", {})['status'] == 200, f'{field} cannot be confirmed')
        current = client.request('GET', base)['json']
        require(current['collection']['stage'] == 'ready' and current['collection']['completion'] == 100, 'seven-field collection did not finish')
        workspace = client.request('GET', '/api/workspace')['json']
        require(workspace['ready_count'] == 1 and workspace['visits_count'] == 1, 'workspace counts are not actual saved values')
        return {'confirmed_fields': current['collection']['captured_fields'], 'completion': 100, 'ready_count': 1}

    def focused_short_answer(self):
        client, visit = self.new_visit()
        base = f"/api/visits/{visit['id']}"
        saved = client.request('PATCH', base + '/intake', {**INTAKE, 'age_group': '未填写', 'medications': '', 'allergies': '', 'medical_history': ''})
        require(saved['status'] == 200, 'could not prepare partially filled intake')
        _, assistant = client.message(visit['id'], '你好')
        require(assistant.get('followup_field') == 'age_group', 'focus question does not match the missing age field')
        response, assistant = client.message(visit['id'], '成人')
        require('知识库没有' not in assistant['content'], 'collection short answer is wrongly treated as unknown medical question')
        candidate = next((c for c in response['json']['visit']['intake_candidates'] if c['field'] == 'age_group'), None)
        require(candidate and candidate['value'] == '成人', 'focused age answer was not understood')
        require(response['json']['visit']['intake']['age_group'] == '未填写', 'short answer was saved without confirmation')
        return {'answer': assistant['content'], 'candidate_field': 'age_group', 'stored_only_after_confirmation': True}

    def observation_summary_plan(self):
        client, visit = self.new_visit()
        base = f"/api/visits/{visit['id']}"
        client.request('PATCH', base + '/intake', INTAKE)
        entries = [('temperature', '37.4', '°C', '2026-10-12T09:00:00+08:00'), ('note', '虚构观察：醒后休息', '', '2026-10-12T00:30:00+00:00')]
        current = None
        for kind, value, unit, stamp in entries:
            response = client.request('POST', base + '/observations', {'kind': kind, 'value': value, 'unit': unit, 'recorded_at': stamp, 'notes': '虚构验收记录'})
            require(response['status'] == 200, 'cannot save observation')
            current = response['json']
        require(current['observations'][0]['kind'] == 'note', 'timeline sorts strings instead of chronological times')
        plan = client.request('POST', base + '/plan', {})
        require(plan['status'] == 200 and plan['json']['plan']['source'] == 'self_report', 'plan does not retain self-report boundary')
        require(INTAKE['chief_complaint'] in plan['json']['plan']['goal'], 'plan ignores actual chief complaint')
        summary = client.request('POST', base + '/summary', {'use_model': False})
        require(summary['status'] == 200 and '37.4' in summary['json']['summary']['text'], 'summary lost timeline')
        require('37.4' in client.request('GET', base + '/export')['text'], 'download lost timeline')
        deleted = client.request('DELETE', base + f"/observations/{current['observations'][0]['id']}")
        require(deleted['status'] == 200 and deleted['json']['summary'] is None and deleted['json']['plan'] is None, 'editing timeline retains stale generated material')
        require(client.request('GET', '/api/workspace')['json']['observations_count'] == 1, 'workspace observation count stale')
        return {'ordered_by_instant': True, 'timeline_in_summary_and_export': True, 'stale_plan_and_summary_cleared': True}

    def library_bookmarks(self):
        owner = self.client()
        stranger = self.client()
        library = owner.request('GET', '/api/library')['json']
        require(library['total'] >= 37 and len(library['categories']) >= 6, 'expanded catalog unavailable')
        source = library['items'][0]
        detail = owner.request('GET', '/api/library/' + source['id'])
        require(detail['status'] == 200 and detail['json']['content'] == source['content'], 'detail differs from curated source')
        by_category = owner.request('GET', '/api/library?category=' + quote(source['category']))['json']
        require(by_category['items'] and all(s['category'] == source['category'] for s in by_category['items']), 'category filter ignored')
        saved = owner.request('PUT', '/api/library/bookmarks/' + source['id'], {'saved': True})
        require(saved['status'] == 200, 'cannot bookmark source')
        require(owner.request('GET', '/api/library?bookmarked=true')['json']['total'] == 1, 'saved-only filter ignored')
        require(stranger.request('GET', '/api/library?bookmarked=true')['json']['total'] == 0, 'bookmarks leaked to another visitor')
        require(owner.request('GET', '/api/library/no-such-article')['status'] == 404, 'missing source silently falls back')
        require(owner.request('PUT', '/api/library/bookmarks/no-such-article', {'saved': True})['status'] == 404, 'invalid source bookmark accepted')
        return {'catalog_total': library['total'], 'categories': library['categories'], 'retrieval': library['retrieval'], 'bookmark_isolation': True}

    def unknown_and_supported_evidence(self):
        client, visit = self.new_visit()
        supported = client.request('GET', '/api/library/evidence?q=' + quote('咳嗽该怎么记录'))['json']
        require(supported['items'] and all(s['id'] == 'cough' for s in supported['items']), 'supported topic lost or mixed with unrelated data')
        unknown = client.request('GET', '/api/library/evidence?q=' + quote('不存在的医院挂号费和地址'))['json']
        require(unknown['items'] == [], 'unverified hospital data given sources')
        for _ in range(3):
            _, assistant = client.message(visit['id'], '这家医院在哪？')
            require(assistant['content'].strip() and not assistant['sources'], 'repeated unknown question gives empty/fabricated answer')
        return {'supported_source': 'cough', 'unknown_sources': 0, 'repeated_nonempty_answers': 3}

    def new_routes_authorization(self):
        owner, visit = self.new_visit()
        stranger = self.client()
        base = f"/api/visits/{visit['id']}"
        routes = [('PATCH', base + '/title', {'title': '虚构改名'}), ('POST', base + '/plan', {}), ('POST', base + '/candidates/missing/confirm', {}), ('POST', base + '/observations', {'kind': 'note', 'value': '虚构', 'recorded_at': '2026-10-12T08:00:00+08:00'}), ('DELETE', base + '/observations/missing', None)]
        for method, path, data in routes:
            require(owner.request(method, path, data, csrf=False)['status'] == 403, 'new mutation accepts missing CSRF')
            require(stranger.request(method, path, data)['status'] == 404, 'new route permits cross-visitor access')
        result = owner.request('PATCH', base + '/title', {'title': '虚构V2验收标题'})
        require(result['status'] == 200 and owner.request('GET', base)['json']['title'] == '虚构V2验收标题', 'title change not persisted')
        return {'new_record_routes': len(routes), 'csrf_denied': 403, 'cross_visitor_denied': 404, 'rename_persisted': True}

    def validation_does_not_mutate(self):
        client, visit = self.new_visit()
        base = f"/api/visits/{visit['id']}"
        invalid = [{'kind': 'diagnosis', 'value': '虚构', 'recorded_at': '2026-10-12T08:00:00+08:00'}, {'kind': 'note', 'value': '', 'recorded_at': '2026-10-12T08:00:00+08:00'}, {'kind': 'note', 'value': '虚构', 'recorded_at': 'not-a-time'}]
        for body in invalid:
            require(client.request('POST', base + '/observations', body)['status'] == 422, 'invalid timeline silently accepted')
        require(client.request('GET', base)['json']['observations'] == [], 'rejected timeline mutated record')
        return {'invalid_requests': len(invalid), 'status': 422, 'observations': 0}

    def concurrent_timeline(self):
        owner, visit = self.new_visit()
        base = f"/api/visits/{visit['id']}"
        clients = []
        for _ in range(5):
            client = Client(self.base_url)
            for cookie in owner.jar:
                client.jar.set_cookie(cookie)
            client.csrf = owner.csrf
            clients.append(client)
        def add(index):
            response = clients[index].request('POST', base + '/observations', {'kind': 'note', 'value': f'虚构观察-{index}', 'recorded_at': f'2026-10-12T08:0{index}:00+08:00'})
            require(response['status'] == 200, 'parallel timeline request failed')
            return response['elapsed_ms']
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            times = list(pool.map(add, range(5)))
        observations = owner.request('GET', base)['json']['observations']
        require(len(observations) == 5 and len({o['id'] for o in observations}) == 5, 'concurrent timeline loses or duplicates records')
        require({o['value'] for o in observations} == {f'虚构观察-{i}' for i in range(5)}, 'parallel raw observations changed')
        return {'stored_observations': 5, 'elapsed_ms': times}

    def run(self):
        cases = [('confirmed_collection_reaches_ready_without_silent_facts', self.collection_confirmation), ('focused_short_answer_uses_collection_context', self.focused_short_answer), ('timeline_plan_summary_export_and_invalidation', self.observation_summary_plan), ('source_details_categories_and_private_bookmarks', self.library_bookmarks), ('supported_vs_unknown_evidence_and_repeated_answers', self.unknown_and_supported_evidence), ('new_mutations_csrf_and_cross_visitor_authorization', self.new_routes_authorization), ('invalid_observations_do_not_mutate_record', self.validation_does_not_mutate), ('five_parallel_timeline_writes_no_lost_updates', self.concurrent_timeline)]
        for name, function in cases:
            self.run_case(name, function)
            print(f"{self.results[-1]['status'].upper():4} {name}", flush=True)
        cleanup = self.cleanup()
        failed = sum(r['status'] == 'fail' for r in self.results)
        return {'executed_at': datetime.now(timezone.utc).isoformat(), 'base_url': self.base_url, 'passed': len(self.results) - failed, 'failed': failed, 'total': len(self.results), 'results': self.results, 'cleanup': cleanup, 'data_policy': 'Only independent fictional visitors. use_model=false. No external model requests. Actual local embedding availability is reported, not assumed.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:8270')
    args = parser.parse_args()
    report = WorkspaceAcceptance(args.base_url).run()
    directory = Path(__file__).resolve().parents[1] / 'artifacts'
    (directory / 'acceptance_v2.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    lines = ['# 健康沟通工作台 V2 · 实际HTTP验收', '', f"执行时间（UTC）：{report['executed_at']}", f"目标：{report['base_url']}", f"结果：{report['passed']}/{report['total']} 通过", '', '| 流程 | 结果 |', '| --- | --- |']
    lines.extend(f"| {r['name']} | {r['status']} {r.get('error', '')} |" for r in report['results'])
    lines.extend(['', '独立的虚构访客数据已清理：' + json.dumps(report['cleanup'], ensure_ascii=False), '', '本次不验证临床准确性、真实模型效果或生产负载。'])
    (directory / 'acceptance_v2.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return 1 if report['failed'] or report['cleanup']['cleanup_failures'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
