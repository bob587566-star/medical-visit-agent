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
    await wrapper.get('#question').setValue('就诊前准备什么？')
    await wrapper.get('.composer').trigger('submit')
    await flushPromises()
    const first = fetchMock.mock.calls.find(([path]) => String(path).endsWith('/messages'))!
    expect(JSON.parse(String(first[1]?.body)).use_model).toBe(false)
    await wrapper.get('[role="switch"]').trigger('click')
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
    await wrapper.get('.desktop-tabs button').trigger('click')
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
    await wrapper.get('#question').setValue('应该记录哪些信息？')
    await wrapper.get('.composer').trigger('submit')
    await wrapper.get('.desktop-tabs button').trigger('click')
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
    expect(wrapper.get('[role="alert"]').text()).toContain('清单暂未更新，请重试。')
  })
})
