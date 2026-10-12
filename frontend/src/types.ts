export const intakeFields = ['chief_complaint', 'onset', 'age_group', 'severity', 'medications', 'allergies', 'medical_history', 'notes'] as const
export type Intake = Record<typeof intakeFields[number], string>
export const intakeLabels: Record<keyof Intake, string> = { chief_complaint: '主要不适 / 就诊原因', onset: '开始时间', age_group: '年龄组', severity: '本人感受到的严重程度', medications: '正在使用的药物', allergies: '过敏情况', medical_history: '既往病史', notes: '补充备注' }
export interface IntakeCandidate { id: string; field: keyof Intake; value: string; evidence: string; created_at: string }
export interface Collection { stage: 'start' | 'collecting' | 'ready' | 'urgent'; completion: number; missing_fields: string[]; next_question: string; captured_fields: string[] }
export type ObservationKind = 'symptom' | 'temperature' | 'blood_pressure' | 'blood_glucose' | 'note'
export interface Observation { id: string; kind: ObservationKind; value: string; unit: string; recorded_at: string; notes: string; created_at: string }
export interface ObservationInput { kind: ObservationKind; value: string; unit: string; recorded_at: string; notes: string }
export interface PreparationPlan { generated_at: string; source: 'self_report'; goal: string; questions: string[]; steps: { id: string; title: string; detail: string; category: string }[]; notice: string }
export interface Retrieval { mode: string; embedding_available: boolean; model: string; document_count: number }
export interface Workspace { visits_count: number; messages_count: number; observations_count: number; ready_count: number; knowledge_count: number; bookmarks_count: number; recent_visits: Visit[]; retrieval: Retrieval; privacy: { retention_days: number; local_default: boolean } }
export interface LibraryResult { items: Source[]; categories: string[]; bookmark_ids: string[]; total: number; retrieval: Retrieval }
export interface EvidenceResult { items: Source[]; retrieval: Retrieval }
export interface Source {
  id: string; title: string; publisher: string; url: string; reviewed_at: string;
  content: string; category?: string; keywords?: string[]; topics?: string[]
}
export interface Risk { level: 'emergency' | 'prompt' | 'unknown'; kind?: 'medical' | 'self_harm' | 'unknown'; message: string; reasons: string[]; source_ids: string[] }
export interface Trace { tool: string; label: string; status: string }
export interface Message {
  id: string; role: 'user' | 'assistant'; content: string; created_at: string;
  sources: Source[]; trace: Trace[]; risk?: Risk; suggested_questions: string[]; mode?: string; duration_ms?: number; intake_candidates?: IntakeCandidate[]; followup_field?: keyof Intake | null
}
export interface Summary { text: string; generated_at: string; source: 'self_report' }
export interface ChecklistItem { id: string; text: string; done: boolean }
export interface Visit {
  id: string; title: string; created_at: string; updated_at: string; intake: Intake;
  messages: Message[]; summary: Summary | null; checklist: ChecklistItem[]; risk: Risk | null;
  collection?: Collection; intake_candidates?: IntakeCandidate[]; observations?: Observation[]; plan?: PreparationPlan | null
}
export interface Bootstrap {
  csrf_token: string; model_available: boolean; privacy: unknown; sources: Source[];
  visits: Visit[]; current_visit: Visit | null
}
export interface MessageResult { user_message: Message; assistant_message: Message; visit: Visit }
export interface SummaryResult { summary: Summary; checklist: ChecklistItem[]; visit: Visit }
export type TaskGoal = 'prepare_visit' | 'understand_symptom' | 'organize_timeline'
export type TaskTool = 'check_urgent_signs' | 'search_health_knowledge' | 'collect_visit_details' | 'build_visit_checklist' | 'draft_visit_summary' | 'suggest_intake_updates' | 'build_preparation_plan'
export interface PlannedStep { id: string; tool: TaskTool; depends_on: string[]; label: string }
export interface TaskPlan { objective: string; planner_mode: 'local' | 'model' | 'fallback'; steps: PlannedStep[] }
export interface TaskStep extends PlannedStep { status: 'pending' | 'running' | 'done' | 'failed' | 'skipped'; executed_by: 'executor' | 'local_safety'; output: Record<string, unknown>; source_ids: string[]; error: string | null; started_at: string | null; finished_at: string | null; duration_ms: number }
export interface EvidenceClause { clause: string; kind: 'source' | 'self_report' | 'safety_rule'; source_id: string | null; reference: string; exact_span: string; start: number; end: number; verified: boolean; notice: string }
export interface TaskRun { id: string; goal: TaskGoal; status: 'completed' | 'needs_input' | 'emergency' | 'failed'; created_at: string; duration_ms: number; plan: TaskPlan; steps: TaskStep[]; content: string; risk: Risk; sources: Source[]; evidence_ledger: EvidenceClause[]; missing_fields: string[]; next_question: string | null; summary: Summary | null; checklist: ChecklistItem[]; preparation_plan: PreparationPlan | null; planner_notice: string; planner_error: 'unavailable' | 'timeout' | 'network_error' | 'invalid_plan' | null }
export type TaskStatus = 'queued' | 'running' | 'completed' | 'needs_input' | 'emergency' | 'failed' | 'interrupted' | 'superseded'
export interface TaskJob { id: string; visit_id: string; goal: TaskGoal; status: TaskStatus; created_at: string; updated_at: string; result: TaskRun | null; error: string | null; applied_at: string | null; source_revision: string; current_risk?: Risk | null; invalidated_reason?: string | null; can_apply?: boolean }
