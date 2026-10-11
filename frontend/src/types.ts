export const intakeFields = ['chief_complaint', 'onset', 'age_group', 'severity', 'medications', 'allergies', 'medical_history', 'notes'] as const
export type Intake = Record<typeof intakeFields[number], string>
export interface Source {
  id: string; title: string; publisher: string; url: string; reviewed_at: string;
  content: string; category?: string; keywords?: string[]; topics?: string[]
}
export interface Risk { level: 'emergency' | 'prompt' | 'unknown'; message: string; reasons: string[]; source_ids: string[] }
export interface Trace { tool: string; label: string; status: string }
export interface Message {
  id: string; role: 'user' | 'assistant'; content: string; created_at: string;
  sources: Source[]; trace: Trace[]; risk?: Risk; suggested_questions: string[]; mode?: string; duration_ms?: number
}
export interface Summary { text: string; generated_at: string; source: 'self_report' }
export interface ChecklistItem { id: string; text: string; done: boolean }
export interface Visit {
  id: string; title: string; created_at: string; updated_at: string; intake: Intake;
  messages: Message[]; summary: Summary | null; checklist: ChecklistItem[]; risk: Risk | null
}
export interface Bootstrap {
  csrf_token: string; model_available: boolean; privacy: unknown; sources: Source[];
  visits: Visit[]; current_visit: Visit | null
}
export interface MessageResult { user_message: Message; assistant_message: Message; visit: Visit }
export interface SummaryResult { summary: Summary; checklist: ChecklistItem[]; visit: Visit }
