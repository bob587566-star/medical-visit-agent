import { intakeFields, type Intake, type Message, type Risk, type Visit } from './types'

export const EMPTY_REPLY = '本次未收到有效答复。请稍后重试；你已填写的信息仍保留。'
export function requiresHumanSupport(risk?: Risk | null): boolean {
  return risk?.level === 'emergency' || risk?.kind === 'self_harm'
}
export function emptyIntake(): Intake {
  return { chief_complaint: '', onset: '', age_group: '未填写', severity: '未填写', medications: '', allergies: '', medical_history: '', notes: '' }
}
export function copyIntake(intake?: Partial<Intake> | null): Intake {
  const result = emptyIntake()
  for (const key of intakeFields) if (typeof intake?.[key] === 'string') result[key] = intake[key]!
  return result
}
export function replyText(message: Pick<Message, 'content'>): string {
  return typeof message.content === 'string' && message.content.trim() ? message.content : EMPTY_REPLY
}
export function safeSourceUrl(value: unknown): string | null {
  if (typeof value !== 'string' || !value.trim()) return null
  try {
    const url = new URL(value.trim())
    return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password ? url.href : null
  } catch { return null }
}
export function modelChoice(requested: boolean, available: boolean): boolean { return requested === true && available === true }
export function visitHasIntake(visit: Visit): boolean {
  return intakeFields.some(key => !!visit.intake?.[key]?.trim() && visit.intake[key] !== '未填写')
}
export function shortDate(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '刚刚'
  return date.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' })
}
