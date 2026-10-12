import { mount, flushPromises, type VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App.vue'
import TaskAgentPanel from './components/TaskAgentPanel.vue'
import MessageBubble from './components/MessageBubble.vue'
import { emptyIntake, requiresHumanSupport } from './helpers'
import type { Visit, Risk } from './types'

const support: Risk = { level: 'prompt', kind: 'self_harm', message: '请先联系信任的人陪着你。你现在是否安全？', reasons: [], source_ids: ['crisis_support'] }
const record: Visit = { id: 'fictional-support', title: '虚构支持验收', created_at: '', updated_at: '', intake: { ...emptyIntake(), chief_complaint: '【虚构】应当保留的自述' }, messages: [], risk: support, summary: null, checklist: [], collection: { stage: 'support', completion: 0, next_question: '', missing_fields: [], captured_fields: [] } }
function response(payload: unknown): Response { return { ok: true, status: 200, json: async () => payload, text: async () => JSON.stringify(payload) } as Response }
let wrapper: VueWrapper | undefined
beforeEach(() => { vi.stubGlobal('fetch', vi.fn()); HTMLElement.prototype.scrollTo = vi.fn() })
afterEach(() => { wrapper?.unmount(); wrapper = undefined; vi.unstubAllGlobals(); vi.restoreAllMocks() })

describe('独立陪伴支持流程', () => {
  it('陪伴支持与普通医疗prompt分开判断', () => {
    expect(requiresHumanSupport(support)).toBe(true)
    expect(requiresHumanSupport({ ...support, kind: 'medical' })).toBe(false)
    expect(requiresHumanSupport({ ...support, kind: 'medical', level: 'emergency' })).toBe(true)
    expect(requiresHumanSupport(null)).toBe(false)
  })
  it('摘要和计划暂停，原自述、聊天和下载保留，不发生成请求', async () => {
    const mocked = vi.mocked(fetch).mockImplementation(async path => response(path === '/api/bootstrap' ? { csrf_token: 'fictional', model_available: false, sources: [], visits: [record], current_visit: record } : record))
    wrapper = mount(App); await flushPromises()
    expect(wrapper.get('.generate-button').attributes('disabled')).toBeDefined()
    await wrapper.get('[data-tab="summary"]').trigger('click')
    expect(wrapper.get('.summary-support-note').text()).toContain('支持')
    expect(wrapper.get('.summary-empty button').attributes('disabled')).toBeDefined()
    expect(wrapper.get('.plan-heading button').attributes('disabled')).toBeDefined()
    expect(wrapper.findAll('.summary-actions button').find(button => button.text() === '下载 TXT')!.attributes('disabled')).toBeUndefined()
    await wrapper.get('[data-tab="prepare"]').trigger('click')
    expect((wrapper.get('.intake-form textarea').element as HTMLTextAreaElement).value).toContain('应当保留')
    await wrapper.get('[data-tab="chat"]').trigger('click')
    expect(wrapper.get('.send-button').attributes('disabled')).toBeDefined() // Empty composer only.
    await wrapper.get('#question').setValue('【虚构】朋友会过来陪我')
    expect(wrapper.get('.send-button').attributes('disabled')).toBeUndefined()
    expect(mocked.mock.calls.some(([, options]) => options?.method === 'POST')).toBe(false)
  })
  it('等待支持的任务正常显示状态，不能保存也不假称急诊', async () => {
    vi.mocked(fetch).mockResolvedValue(response({ items: [{ id: 'fictional-task', visit_id: record.id, goal: 'prepare_visit', status: 'support_required', created_at: '', updated_at: '', result: null, current_risk: support, invalidated_reason: '先关注安全与支持', can_apply: false, applied_at: null }] }))
    wrapper = mount(TaskAgentPanel, { props: { visitId: record.id, dirty: false, busy: false, modelAvailable: false, currentRisk: support } }); await flushPromises()
    expect(wrapper.get('.task-state').text()).toBe('先获得陪伴支持')
    expect(wrapper.find('.task-save>.primary-button').exists()).toBe(false)
    expect(wrapper.get('.task-risk').text()).toContain('先获得陪伴支持')
    expect(wrapper.text()).not.toContain('当前安全提醒优先')
  })
  it('聊天标签使用陪伴支持，不混成表单整理', () => {
    wrapper = mount(MessageBubble, { props: { message: { id: 'fictional-reply', role: 'assistant', created_at: '', content: support.message, mode: 'local_support', risk: support, sources: [], trace: [], suggested_questions: [] } } })
    expect(wrapper.get('.message-mode').text()).toBe('陪伴支持优先')
    expect(wrapper.find('.followup-prompt').exists()).toBe(false)
  })
})
