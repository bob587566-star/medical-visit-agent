import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App.vue'
import { emptyIntake } from './helpers'
import { request, setCsrfToken } from './api'
import type { PrivacyNotice, Visit, Workspace } from './types'

const privacy: PrivacyNotice = { consent_required: true, accepted: true, notice_version: '2026-10-14.v2', storage_mode: 'cloud', storage_label: '云端服务', scopes: { local_storage: true, external_ai: false }, retention_days: 7, notice_text: '资料加密保存于当前服务；保存前需本人同意。', model_notice: '外部模型默认关闭。' }
const visit: Visit = { id: 'cloud-fictional-visit', title: '虚构就诊准备', created_at: '', updated_at: '', intake: emptyIntake(), messages: [{ id: 'reply', role: 'assistant', mode: 'local_fallback', content: '虚构资料整理结果。', created_at: '', sources: [], trace: [{ tool: 'search', label: '查询公开资料', status: 'fallback' }], suggested_questions: [] }], summary: null, checklist: [], risk: null }
const retrieval = { mode: 'lexical_fallback', embedding_available: false, model: '', document_count: 46 }
const workspace: Workspace = { visits_count: 1, messages_count: 1, observations_count: 0, ready_count: 0, knowledge_count: 46, bookmarks_count: 0, recent_visits: [visit], retrieval, privacy: { retention_days: 7, local_default: false } }
function response(payload: unknown): Response { return { ok: true, status: 200, json: async () => payload, text: async () => JSON.stringify(payload) } as Response }
let wrapper: VueWrapper | undefined
beforeEach(() => { vi.stubGlobal('fetch', vi.fn()); setCsrfToken(''); HTMLElement.prototype.scrollTo = vi.fn() })
afterEach(() => { wrapper?.unmount(); wrapper = undefined; vi.restoreAllMocks(); vi.unstubAllGlobals() })
function mockBootstrap(notice = privacy): void {
  vi.mocked(fetch).mockImplementation(async path => {
    if (path === '/api/bootstrap') return response({ csrf_token: 'cloud-test-csrf', model_available: false, privacy: notice, visits: notice.accepted ? [visit] : [], current_visit: notice.accepted ? visit : null, sources: [] })
    if (path === '/api/workspace') return response(workspace)
    if (String(path).startsWith('/api/library')) return response({ items: [], categories: [], bookmark_ids: [], total: 0, retrieval })
    throw new Error('Unexpected test request: ' + path)
  })
}

describe('云端部署的实际处理位置', () => {
  it('未授权的云端页面显示实际保存位置，仍不自动创建资料', async () => {
    mockBootstrap({ ...privacy, accepted: false, scopes: { local_storage: false, external_ai: false } })
    wrapper = mount(App); await flushPromises()
    expect(wrapper.get('.local-status').text()).toBe('云端服务已连接')
    expect(wrapper.get('.privacy-consent-form').text()).toContain('保存在云端服务')
    expect(wrapper.get('.privacy-consent-form').text()).toContain('云端数据库')
    expect(vi.mocked(fetch).mock.calls.some(([, options]) => options?.method === 'POST')).toBe(false)
  })
  it('总览、聊天及资料检索采用云端文案，规则回退不会声称在访问者电脑处理', async () => {
    mockBootstrap(); wrapper = mount(App); await flushPromises()
    expect(wrapper.get('.workspace-status').text()).toContain('默认由云端服务处理')
    expect(wrapper.get('.model-card').text()).toContain('当前：云端服务处理')
    expect(wrapper.findComponent({ name: 'OverviewPanel' }).props('cloud')).toBe(true)
    await wrapper.get('[data-tab="chat"]').trigger('click'); await flushPromises()
    expect(wrapper.get('.message-mode').text()).toBe('已回退服务内规则整理')
    expect(wrapper.get('.trace-status').text()).toBe('服务内回退')
    expect(wrapper.get('.composer-bottom').text()).toContain('默认由云端服务处理')
    await wrapper.get('[data-tab="library"]').trigger('click'); await flushPromises()
    expect(wrapper.get('.library-panel').text()).toContain('云端主题 / 词法检索')
    expect(wrapper.get('.library-search label').text()).toBe('搜索云端健康资料')
    expect(wrapper.get('.library-panel').text()).not.toContain('本机语义服务不可用')
  })
  it('连接失败提示适用于云端访问，不指示用户启动本机后端', async () => {
    vi.mocked(fetch).mockRejectedValue(new TypeError('test network failure'))
    await expect(request('/api/bootstrap')).rejects.toThrow('暂时无法连接服务。请检查网络连接，然后重试。')
  })
})
