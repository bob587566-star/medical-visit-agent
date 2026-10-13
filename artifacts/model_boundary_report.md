# 外部模型边界独立验证

实际执行时间（UTC）：2026-10-13T11:09:26.034258+00:00

结果：18/18 通过，0 失败。

仅用虚构数据和MockTransport，未产生外部网络请求。

| 验证项 | 结果 | 说明 |
| --- | --- | --- |
| valid_exact_fragments_only | pass | 证据详见JSON报告 |
| hallucinated_diagnosis_rejected | pass | 证据详见JSON报告 |
| dose_and_medication_rejected | pass | 证据详见JSON报告 |
| extra_tool_or_instruction_field_rejected | pass | 证据详见JSON报告 |
| invalid_json_rejected | pass | 证据详见JSON报告 |
| wrong_field_type_rejected | pass | 证据详见JSON报告 |
| null_model_content_rejected | pass | 证据详见JSON报告 |
| empty_model_object_fails_closed | pass | 证据详见JSON报告 |
| oversized_model_fragment_rejected | pass | 证据详见JSON报告 |
| http_error_falls_back | pass | 证据详见JSON报告 |
| timeout_falls_back | pass | 证据详见JSON报告 |
| identity_history_cookie_and_instructions_minimized | pass | 证据详见JSON报告 |
| fixed_executor_rejects_unknown_tools | pass | 证据详见JSON报告 |
| emergency_bypasses_even_selected_model | pass | 证据详见JSON报告 |
| unknown_query_does_not_reuse_intake_or_call_model | pass | 证据详见JSON报告 |
| executor_timeout_preserves_sourced_local_answer | pass | 证据详见JSON报告 |
| unconfigured_model_never_opens_transport | pass | 证据详见JSON报告 |
| default_local_executor_never_calls_model | pass | 证据详见JSON报告 |
