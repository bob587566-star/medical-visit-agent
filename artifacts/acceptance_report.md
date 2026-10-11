# 就诊准备 Agent 独立验收

实际执行时间（UTC）：2026-10-11T21:32:43.368567+00:00

目标：http://127.0.0.1:8270

结果：36/36 通过，0 失败。

所有病例均为虚构，临床请求均明确 use_model=false。

| 验收项 | 结果 | 证据/失败原因 |
| --- | --- | --- |
| health_and_authoritative_source_catalog | pass | 实际HTTP响应及内容详见JSON报告 |
| cookie_isolation_and_cross_visitor_access | pass | 实际HTTP响应及内容详见JSON报告 |
| csrf_all_mutating_endpoints | pass | 实际HTTP响应及内容详见JSON报告 |
| create_save_reload_intake | pass | 实际HTTP响应及内容详见JSON报告 |
| greeting_does_not_search_medical_knowledge | pass | 实际HTTP响应及内容详见JSON报告 |
| visit_preparation_has_true_catalog_citations | pass | 实际HTTP响应及内容详见JSON报告 |
| unknown_hospital_repeated_answers_nonempty | pass | 实际HTTP响应及内容详见JSON报告 |
| medication_request_cannot_create_prescription | pass | 实际HTTP响应及内容详见JSON报告 |
| current_chest_pain_cold_sweat_immediate_local_priority | pass | 实际HTTP响应及内容详见JSON报告 |
| negated_emergency_not_red_alert | pass | 实际HTTP响应及内容详见JSON报告 |
| educational_emergency_not_red_alert | pass | 实际HTTP响应及内容详见JSON报告 |
| historical_emergency_not_red_alert | pass | 实际HTTP响应及内容详见JSON报告 |
| old_sudden_emergency_not_red_alert | pass | 实际HTTP响应及内容详见JSON报告 |
| negated_chest_but_current_breathing_emergency | pass | 实际HTTP响应及内容详见JSON报告 |
| recent_stroke_resolved_still_urgent | pass | 实际HTTP响应及内容详见JSON报告 |
| new_current_emergency_after_history | pass | 实际HTTP响应及内容详见JSON报告 |
| unrelated_negation_does_not_suppress_emergency | pass | 实际HTTP响应及内容详见JSON报告 |
| current_emergency_help_question_without_pronoun | pass | 实际HTTP响应及内容详见JSON报告 |
| current_severe_breathing_emergency | pass | 实际HTTP响应及内容详见JSON报告 |
| current_sudden_stroke_warning | pass | 实际HTTP响应及内容详见JSON报告 |
| current_throat_swelling_breathing_emergency | pass | 实际HTTP响应及内容详见JSON报告 |
| current_altered_consciousness_emergency | pass | 实际HTTP响应及内容详见JSON报告 |
| no_response_is_not_symptom_denial | pass | 实际HTTP响应及内容详见JSON报告 |
| unable_to_breathe_is_not_symptom_denial | pass | 实际HTTP响应及内容详见JSON报告 |
| current_sudden_extremely_painful_headache | pass | 实际HTTP响应及内容详见JSON报告 |
| summary_retains_unresolved_current_emergency | pass | 实际HTTP响应及内容详见JSON报告 |
| summary_self_report_checklist_and_utf8_download | pass | 实际HTTP响应及内容详见JSON报告 |
| summary_does_not_promote_injection_to_facts | pass | 实际HTTP响应及内容详见JSON报告 |
| html_payload_stays_api_plain_text | pass | 实际HTTP响应及内容详见JSON报告 |
| special_nonblank_inputs_answer_without_corruption | pass | 实际HTTP响应及内容详见JSON报告 |
| invalid_requests_are_explicit_and_do_not_mutate | pass | 实际HTTP响应及内容详见JSON报告 |
| valid_token_cannot_override_cross_origin_rejection | pass | 实际HTTP响应及内容详见JSON报告 |
| editing_intake_invalidates_stale_summary_and_export | pass | 实际HTTP响应及内容详见JSON报告 |
| five_concurrent_local_requests_stable | pass | 实际HTTP响应及内容详见JSON报告 |
| five_concurrent_same_visit_no_lost_messages | pass | 实际HTTP响应及内容详见JSON报告 |
| delete_and_clear_respect_visitor_boundary | pass | 实际HTTP响应及内容详见JSON报告 |

测试记录清理：{"fictional_test_sessions": 44, "cleanup_failures": []}

验证范围：
- HTTP checks do not establish frontend DOM safety; browser or static review is required.
- Catalog citation integrity is checked against /api/knowledge; source integrity is not clinical review or validation.
- HTTP clinical requests deliberately use local processing. Separately executed mocked-model tests validate minimization and failure handling; they do not measure the real provider or clinical accuracy.
- Five concurrent local requests check response and persistence consistency only; they do not establish production throughput or long-running availability.
