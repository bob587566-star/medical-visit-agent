import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App.vue'
import CandidateCard from './components/CandidateCard.vue'
import LibraryPanel from './components/LibraryPanel.vue'
import TimelinePanel from './components/TimelinePanel.vue'
import { emptyIntake } from './helpers'
import type { IntakeCandidate, LibraryResult, Source, Visit, Workspace } from './types'

const candidate: IntakeCandidate = { id: 'candidate-onset', field: 'onset', value: '昨天', evidence: '昨天', created_at: '2026-10-12T08:00:00Z' }
function record(): Visit { return { id: 'v2-visit', title: '虚构就诊准备', created_at: '', updated_at: '', intake: emptyIntake(), messages: [], summary: null, checklist: [], risk: null, intake_candidates: [], observations: [], plan: null } }
function response(payload: unknown): Response { return { ok: true, status: 200, json: async () => payload, text: async () => JSON.stringify(payload) } as Response }
function workspace(visit: Visit): Workspace { return { visits_count: 1, messages_count: 1, observations_count: 0, ready_count: 0, knowledge_count: 37, bookmarks_count: 0, recent_visits: [visit], retrieval: { mode: 'lexical_fallback', embedding_available: false, model: '', document_count: 37 }, privacy: { retention_days: 30, local_default: true } } }
const source = (id: string, title: string): Source => ({ id, title, publisher: '测试教育资料', category: '就诊准备', reviewed_at: '2026-10-12', url: 'https://www.who.int/', content: '仅用于测试的资料内容。' })
function library(items: Source[]): LibraryResult { return { items, total: items.length, categories: ['就诊准备'], bookmark_ids: [], retrieval: { mode: 'lexical_fallback', embedding_available: false, model: '', document_count: 37 } } }
let wrapper: VueWrapper | undefined
beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn())
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', '') }
  HTMLDialogElement.prototype.close = function () { this.removeAttribute('open') }
  HTMLElement.prototype.scrollTo = vi.fn()
})
afterEach(() => { wrapper?.unmount(); wrapper = undefined; vi.restoreAllMocks(); vi.unstubAllGlobals() })

describe('V2 本人确认与草稿', () => {
  it('候选待确认标记清楚，历史已处理候选不能重复确认，恶意文本只按文字显示', async () => {
    wrapper = mount(CandidateCard, { props: { candidate: { ...candidate, value: '<img src=x onerror=alert(1)>' }, pending: true } })
    expect(wrapper.text()).toContain('待本人确认')
    expect(wrapper.find('img').exists()).toBe(false)
    await wrapper.get('button').trigger('click')
    expect(wrapper.emitted('confirm')).toHaveLength(1)
    await wrapper.setProps({ pending: false })
    expect(wrapper.get('button').attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('已处理或已失效')
  })
  it('只有明确确认后写入一项，保留其它未保存草稿', async () => {
    const visit = record()
    visit.intake_candidates = [candidate]
    visit.messages = [{ id: 'm', role: 'assistant', content: '请核对这一项。', created_at: '', sources: [], trace: [], suggested_questions: [], intake_candidates: [candidate] }]
    const fetchMock = vi.mocked(fetch).mockImplementation(async path => {
      if (path === '/api/bootstrap') return response({ csrf_token: 'csrf-v2', model_available: false, visits: [visit], current_visit: visit, sources: [] })
      if (path === '/api/workspace') return response(workspace(visit))
      if (String(path).endsWith('/confirm')) return response({ ...visit, intake: { ...emptyIntake(), onset: '昨天' }, intake_candidates: [] })
      return response(visit)
    })
    wrapper = mount(App); await flushPromises()
    await wrapper.get('[data-tab="prepare"]').trigger('click')
    const fields = wrapper.findAll('.intake-form textarea')
    await fields.at(-1)!.setValue('应该保留的未保存备注')
    await wrapper.get('[data-tab="chat"]').trigger('click')
    await wrapper.get('.candidate-confirm').trigger('click')
    expect(fetchMock.mock.calls.filter(([path]) => String(path).endsWith('/confirm'))).toHaveLength(0)
    expect(wrapper.get('.modal-content').text()).toContain('其余草稿保留')
    await wrapper.get('.modal-actions .primary-button').trigger('click'); await flushPromises()
    await wrapper.get('[data-tab="prepare"]').trigger('click')
    expect((wrapper.findAll('.intake-form textarea').at(-1)!.element as HTMLTextAreaElement).value).toBe('应该保留的未保存备注')
    expect((wrapper.findAll('.intake-form input')[0].element as HTMLInputElement).value).toBe('昨天')
    expect(wrapper.text()).toContain('有未保存的修改')
    expect(fetchMock.mock.calls.filter(([path]) => String(path).endsWith('/confirm'))).toHaveLength(1)
  })
})

describe('V2 真实状态与查询', () => {
  it('总览显示接口实际数量与词法回退，不编造患者或性能数据', async () => {
    const visit = record()
    vi.mocked(fetch).mockImplementation(async path => response(path === '/api/bootstrap' ? { csrf_token: 'csrf', model_available: false, visits: [visit], current_visit: visit, sources: [] } : workspace(visit)))
    wrapper = mount(App); await flushPromises()
    expect(wrapper.findAll('.workspace-stats strong').map(item => item.text())).toEqual(['1', '1', '0', '0', '0', '37'])
    expect(wrapper.get('.workspace-status').text()).toContain('主题 / 词法检索')
    expect(wrapper.get('.continue-progress').text()).toContain('/ 7 项核心信息')
  })
  it('较慢的旧资料查询不能覆盖较新的检索结果', async () => {
    let resolveOld!: (response: Response) => void
    const old = new Promise<Response>(resolve => { resolveOld = resolve })
    vi.mocked(fetch).mockImplementation(async path => {
      if (String(path).includes('q=first')) return old
      if (String(path).includes('q=second')) return response(library([source('new', '最新查询结果')]))
      return response(library([]))
    })
    wrapper = mount(LibraryPanel, { props: { available: true } }); await flushPromises()
    await wrapper.get('input').setValue('first'); await wrapper.get('form').trigger('submit')
    await wrapper.get('input').setValue('second'); await wrapper.get('form').trigger('submit'); await flushPromises()
    resolveOld(response(library([source('old', '旧查询结果')]))); await flushPromises()
    expect(wrapper.text()).toContain('最新查询结果')
    expect(wrapper.text()).not.toContain('旧查询结果')
    expect(wrapper.text()).toContain('本机语义服务不可用')
  })
  it('测量时间线提交原始数值和时间，不作疾病解释', async () => {
    wrapper = mount(TimelinePanel, { props: { observations: [], busy: false, available: true } })
    await wrapper.get('select').setValue('temperature')
    await wrapper.findAll('input')[1].setValue('37.2')
    await wrapper.get('form').trigger('submit')
    const value = wrapper.emitted('create')?.[0][0] as Record<string, string>
    expect(value.kind).toBe('temperature'); expect(value.value).toBe('37.2'); expect(value.unit).toBe('℃')
    expect(Number.isNaN(new Date(value.recorded_at).getTime())).toBe(false)
    expect(wrapper.text()).toContain('不解释测量结果')
  })
})
