import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App.vue'
import MessageBubble from './components/MessageBubble.vue'
import { request, setCsrfToken } from './api'
import { emptyIntake, EMPTY_REPLY, safeSourceUrl } from './helpers'
import type { Message, Visit } from './types'

function visit(): Visit {
  return { id: 'visit-one', title: '我的就诊准备', created_at: '2026-10-12T09:00:00Z', updated_at: '2026-10-12T09:00:00Z', intake: emptyIntake(), messages: [], summary: null, checklist: [], risk: null }
}
function json(payload: unknown, status = 200): Response {
  return { ok: status >= 200 && status < 300, status, json: async () => payload, text: async () => JSON.stringify(payload) } as Response
}
function message(content: string): Message {
  return { id: 'assistant-one', role: 'assistant', content, created_at: '', sources: [], trace: [], suggested_questions: [] }
}
let wrapper: VueWrapper | undefined
beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn())
  HTMLElement.prototype.scrollTo = vi.fn()
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', '') }
  HTMLDialogElement.prototype.close = function () { this.removeAttribute('open') }
})
afterEach(() => { wrapper?.unmount(); wrapper = undefined; vi.restoreAllMocks(); vi.unstubAllGlobals() })

describe('安全与非空输出', () => {
  it('对话和资料中的 HTML 作为文字显示，危险来源 URL 不变成链接', () => {
    const unsafe = '<img src=x onerror="alert(1)">'
    wrapper = mount(MessageBubble, { props: { message: { ...message(unsafe), sources: [{ id: 's', title: unsafe, publisher: '测试资料', url: 'javascript:alert(1)', reviewed_at: '', content: '<script>alert(1)</script>' }] } } })
    expect(wrapper.text()).toContain(unsafe)
    expect(wrapper.find('img').exists()).toBe(false)
    expect(wrapper.find('script').exists()).toBe(false)
    expect(wrapper.find('a').exists()).toBe(false)
    expect(safeSourceUrl('https://www.who.int/health-topics')).toBe('https://www.who.int/health-topics')
    expect(safeSourceUrl('data:text/html,<script>')).toBe(null)
    expect(safeSourceUrl('https://user:secret@example.com')).toBe(null)
  })
  it('空答复明确显示失败提示，保留界面结构', () => {
    wrapper = mount(MessageBubble, { props: { message: message(' \n ') } })
    expect(wrapper.find('.message-content').text()).toBe(EMPTY_REPLY)
    expect(wrapper.text()).toContain('本机资料整理')
  })
})

describe('对话优先与下一轮输入', () => {
  it('正文位于辅助信息之前，候选、来源和工具记录默认收起', () => {
    wrapper = mount(MessageBubble, { props: { message: { ...message('先告诉我这两天有什么变化。'), intake_candidates: [{ id: 'c', field: 'onset', value: '两天', evidence: '两天', created_at: '' }], sources: [{ id: 's', title: '就诊准备资料', publisher: '测试', reviewed_at: '', url: 'https://www.who.int/', content: '测试内容' }], trace: [{ tool: 'check', label: '核查提醒', status: 'done' }] } } })
    const body = wrapper.get('.message-body')
    expect(body.element.children[1].className).toBe('message-content')
    expect(wrapper.get('.candidate-details summary').text()).toContain('已整理 1 项')
    expect(wrapper.get('.source-details summary').text()).toContain('参考 1 条资料')
    for (const details of wrapper.findAll('details')) expect(details.attributes('open')).toBeUndefined()
    expect(wrapper.emitted('confirmCandidate')).toBeUndefined()
  })
  it('采集提示和旧版固定采集问题不能作为用户消息点击发送，真正的用户建议仍可发送', async () => {
    wrapper = mount(MessageBubble, { props: { message: { ...message('请说说具体时间。'), followup_field: 'onset', suggested_questions: ['什么时候开始的？'] } } })
    expect(wrapper.get('.followup-prompt').text()).toContain('什么时候开始的？')
    expect(wrapper.find('.followup-chip').exists()).toBe(false)
    const legacyQuestions = ['最想向医生说明的不适是什么？', '不适从什么时候开始，有什么变化？', '就诊者属于哪个年龄阶段？', '你自己感觉不适程度是轻、中还是重？', '目前使用的药物或补充剂是否需要向医生核对？', '有没有需要告诉医生的过敏情况？不知道也可以写不知道。', '有没有与本次就诊相关的既往情况？']
    for (const question of legacyQuestions) {
      await wrapper.setProps({ message: { ...message(question), suggested_questions: [question] } })
      expect(wrapper.find('.followup-chip').exists()).toBe(false)
      expect(wrapper.find('.followup-prompt').exists()).toBe(true)
    }
    expect(wrapper.emitted('ask')).toBeUndefined()
    await wrapper.setProps({ message: { ...message('可以带上已有资料。'), followup_field: null, suggested_questions: ['已有检查报告应该怎样整理？'] } })
    await wrapper.get('.followup-chip').trigger('click')
    expect(wrapper.emitted('ask')).toEqual([['已有检查报告应该怎样整理？']])
  })
  it('发送立即显示本轮文字与等待状态，答复不会清空下一条正在编辑的文字', async () => {
    const record = visit()
    let resolveMessage!: (response: Response) => void
    const pending = new Promise<Response>(resolve => { resolveMessage = resolve })
    const sent = { ...message('【虚构】我有点咳嗽'), id: 'user-one', role: 'user' as const }
    const reply = { ...message('先聊聊，咳嗽从什么时候开始？'), followup_field: 'onset' as const }
    const updated = { ...record, messages: [sent, reply] }
    let refreshed = false
    vi.mocked(fetch).mockImplementation(async path => {
      if (path === '/api/bootstrap') return json({ csrf_token: 'csrf', model_available: false, visits: [record], current_visit: record, sources: [] })
      if (String(path).endsWith('/messages')) return pending
      return json(refreshed ? updated : record)
    })
    wrapper = mount(App); await flushPromises()
    await wrapper.get('[data-tab="chat"]').trigger('click')
    await wrapper.get('#question').setValue(sent.content)
    await wrapper.get('.composer').trigger('submit')
    expect(wrapper.get('.message-pending').text()).toContain(sent.content)
    expect(wrapper.get('.working-message').text()).toContain('正在回复')
    expect(wrapper.find('.welcome').exists()).toBe(false)
    await wrapper.get('#question').setValue('我还想补充另一件事')
    refreshed = true
    resolveMessage(json({ user_message: sent, assistant_message: reply, visit: updated })); await flushPromises()
    expect(wrapper.find('.message-pending').exists()).toBe(false)
    expect(wrapper.find('.working-message').exists()).toBe(false)
    expect(wrapper.get('.message-assistant .message-content').text()).toBe(reply.content)
    expect((wrapper.get('#question').element as HTMLTextAreaElement).value).toBe('我还想补充另一件事')
  })
  it('发送失败保留原问题，并显示可以再次发送的明确错误', async () => {
    const record = visit()
    vi.mocked(fetch).mockImplementation(async path => path === '/api/bootstrap' ? json({ csrf_token: 'csrf', model_available: false, visits: [record], current_visit: record, sources: [] }) : String(path).endsWith('/messages') ? json({ detail: '本机暂未完成回复，请重试。' }, 503) : json(record))
    wrapper = mount(App); await flushPromises()
    await wrapper.get('[data-tab="chat"]').trigger('click')
    await wrapper.get('#question').setValue('【虚构】需要带什么资料？')
    await wrapper.get('.composer').trigger('submit'); await flushPromises()
    expect((wrapper.get('#question').element as HTMLTextAreaElement).value).toBe('【虚构】需要带什么资料？')
    expect(wrapper.get('.feedback-error').text()).toContain('本机暂未完成回复')
    expect(wrapper.find('.working-message').exists()).toBe(false)
  })
})

describe('请求与隐私', () => {
  it('修改请求携带同源 Cookie 与 CSRF，并展示后端 detail 错误', async () => {
    setCsrfToken('test-csrf')
    const fetchMock = vi.mocked(fetch).mockResolvedValue(json({ detail: '会话校验失败，请重新连接。' }, 403))
    await expect(request('/api/visits', 'POST', { title: '准备' })).rejects.toThrow('会话校验失败，请重新连接。')
    const options = fetchMock.mock.calls[0][1]!
    expect(options.credentials).toBe('same-origin')
    expect(options.headers).toMatchObject({ 'X-CSRF-Token': 'test-csrf' })
  })
  it('外部模型默认关闭，只有用户显式开启后才发送 use_model=true', async () => {
    const record = visit()
    const fetchMock = vi.mocked(fetch).mockImplementation(async (path, options) => {
      if (path === '/api/bootstrap') return json({ csrf_token: 'csrf', model_available: true, visits: [record], current_visit: record, sources: [] })
      if (String(path).endsWith('/messages')) return json({ user_message: { ...message('问题'), role: 'user' }, assistant_message: message('资料答复'), visit: record })
      if (options?.method === 'GET' || !options?.method) return json(record)
      throw new Error(`unexpected ${String(path)}`)
    })
    wrapper = mount(App)
    await flushPromises()
    expect(wrapper.get('[role="switch"]').attributes('aria-checked')).toBe('false')
    await wrapper.get('[data-tab="chat"]').trigger('click')
    await wrapper.get('#question').setValue('就诊前准备什么？')
    await wrapper.get('.composer').trigger('submit')
    await flushPromises()
    const first = fetchMock.mock.calls.find(([path]) => String(path).endsWith('/messages'))!
    expect(JSON.parse(String(first[1]?.body)).use_model).toBe(false)
    await wrapper.get('[role="switch"]').trigger('click')
    await wrapper.get('[data-tab="chat"]').trigger('click')
    await wrapper.get('#question').setValue('怎样整理信息？')
    await wrapper.get('.composer').trigger('submit')
    await flushPromises()
    const calls = fetchMock.mock.calls.filter(([path]) => String(path).endsWith('/messages'))
    expect(JSON.parse(String(calls[1][1]?.body)).use_model).toBe(true)
  })
  it('模型不可用时禁用开关，无记录时自动建立本人的空白记录', async () => {
    const record = visit()
    const fetchMock = vi.mocked(fetch).mockImplementation(async path => path === '/api/bootstrap' ? json({ csrf_token: 'csrf', model_available: false, visits: [], current_visit: null, sources: [] }) : json(record))
    wrapper = mount(App)
    await flushPromises()
    expect(wrapper.get('[role="switch"]').attributes('disabled')).toBeDefined()
    expect(wrapper.get('[role="switch"]').attributes('aria-checked')).toBe('false')
    expect(fetchMock.mock.calls.some(([path, options]) => path === '/api/visits' && options?.method === 'POST')).toBe(true)
  })
})

describe('就诊草稿保留', () => {
  it('保存过程中新输入的内容不被较早的服务器回包覆盖', async () => {
    const record = visit()
    let resolveSave!: (response: Response) => void
    const pending = new Promise<Response>(resolve => { resolveSave = resolve })
    vi.mocked(fetch).mockImplementation(async path => path === '/api/bootstrap' ? json({ csrf_token: 'csrf', model_available: false, visits: [record], current_visit: record, sources: [] }) : pending)
    wrapper = mount(App)
    await flushPromises()
    await wrapper.get('[data-tab="prepare"]').trigger('click')
    const field = wrapper.get('.intake-form textarea')
    await field.setValue('第一次填写的自述')
    await wrapper.get('.intake-form').trigger('submit')
    await field.setValue('保存途中新增的自述')
    resolveSave(json({ ...record, intake: { ...emptyIntake(), chief_complaint: '第一次填写的自述' } }))
    await flushPromises()
    expect((field.element as HTMLTextAreaElement).value).toBe('保存途中新增的自述')
    expect(wrapper.text()).toContain('有未保存的修改')
  })
  it('AI 完成后的记录刷新保留等待期间新编辑的草稿', async () => {
    const record = visit()
    let resolveMessage!: (response: Response) => void
    const pending = new Promise<Response>(resolve => { resolveMessage = resolve })
    vi.mocked(fetch).mockImplementation(async path => {
      if (path === '/api/bootstrap') return json({ csrf_token: 'csrf', model_available: false, visits: [record], current_visit: record, sources: [] })
      if (String(path).endsWith('/messages')) return pending
      return json(record)
    })
    wrapper = mount(App)
    await flushPromises()
    await wrapper.get('[data-tab="chat"]').trigger('click')
    await wrapper.get('#question').setValue('应该记录哪些信息？')
    await wrapper.get('.composer').trigger('submit')
    await wrapper.get('[data-tab="prepare"]').trigger('click')
    const field = wrapper.get('.intake-form textarea')
    await field.setValue('等待答复时新补充的自述')
    resolveMessage(json({ user_message: { ...message('应该记录哪些信息？'), role: 'user' }, assistant_message: message('资料答复'), visit: record }))
    await flushPromises()
    expect((field.element as HTMLTextAreaElement).value).toBe('等待答复时新补充的自述')
    expect(wrapper.text()).toContain('有未保存的修改')
  })
})

describe('清单失败状态', () => {
  it('清单更新失败时保留上次确认的勾选状态并展示错误', async () => {
    const record = { ...visit(), checklist: [{ id: 'item-one', text: '携带既往检查资料', done: false }] }
    vi.mocked(fetch).mockImplementation(async path => path === '/api/bootstrap' ? json({ csrf_token: 'csrf', model_available: false, visits: [record], current_visit: record, sources: [] }) : json({ message: '清单暂未更新，请重试。' }, 503))
    wrapper = mount(App)
    await flushPromises()
    await wrapper.get('.checklist input').setValue(true)
    await flushPromises()
    expect((wrapper.get('.checklist input').element as HTMLInputElement).checked).toBe(false)
    expect(wrapper.get('.feedback-error[role="alert"]').text()).toContain('清单暂未更新，请重试。')
  })
})
