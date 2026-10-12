# 异步任务真实 HTTP 验收

时间：2026-10-12T11:17:11.499553+00:00
服务：http://127.0.0.1:8270

结果：11/11 通过，0 项失败。

| 场景 | 结果 |
| --- | --- |
| local_prepare_needs_input_readonly | pass |
| local_prepare_completed_readonly | pass |
| local_understand_with_exact_source_evidence | pass |
| local_timeline_uses_original_observation | pass |
| explicit_apply_and_duplicate_rejection | pass |
| csrf_and_cross_visitor_isolation | pass |
| updated_record_supersedes_historical_task | pass |
| new_red_alert_invalidates_card_and_apply | pass |
| urgent_task_local_safety_short_circuit | pass |
| audit_is_integrity_checked_metadata | pass |
| explicit_external_planner_actual_mode | pass |

外部规划实际结果：

```json
{
  "requested": true,
  "attempted": true,
  "model_available": true,
  "planner_mode": "model",
  "planner_error": null,
  "status": "completed",
  "model_planner_succeeded": true,
  "notice": "Only goal enum, filled field names, observation count and public source IDs are supplied to the external planner; clinical field values, chat history and identity are not included."
}
```

失败项：
- 无。

独立访客清理失败：[]

仅为有限虚构工程样本；不代表临床验收或真实患者数据部署。外部规划的 fallback 会明确标记，不计作模型规划成功。
