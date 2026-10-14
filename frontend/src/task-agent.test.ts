import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import TaskAgentPanel from './components/TaskAgentPanel.vue'
import App from './App.vue'
import { setCsrfToken } from './api'
import { emptyIntake } from './helpers'
import type { TaskJob, TaskRun, Visit } from './types'

function response(payload: unknown, status = 200): Response { return { ok: status >= 200 && status < 300, status, json: async () => payload, text: async () => JSON.stringify(payload) } as Response }
function run(): TaskRun {
  const first = { id: 'safety', tool: 'check_urgent_signs' as const, label: '先检查紧急警示', depends_on: [] }
  const second = { id: 'details', tool: 'collect_visit_details' as const, label: '读取自述与待补充项目', depends_on: ['safety'] }
  const output = { recorded: '<img src=x onerror=alert(1)>【虚构自述】' }
  return { id: 'run-one', goal: 'prepare_visit', status: 'needs_input', created_at: '2026-10-12T08:00:00Z', duration_ms: 1, plan: { objective: '依据已保存资料准备一次虚构就诊', planner_mode: 'local', steps: [first, second] }, steps: [first, second].map(step => ({ ...step, status: 'done', executed_by: 'executor', output, source_ids: [], error: null, started_at: null, finished_at: null, duration_ms: 1 })), content: '【虚构】已有材料整理成待核对草稿。', risk: { level: 'unknown', message: '', reasons: [], source_ids: [] }, sources: [{ id: 'unsafe-source', title: '<script>原文仅按文字显示</script>', publisher: '测试资料', url: 'javascript:alert(1)', reviewed_at: '2026-10-12', content: '<img src=x onerror=alert(1)>' }], evidence_ledger: [{ clause: output.recorded, kind: 'self_report', source_id: null, reference: '本人原话', exact_span: output.recorded, start: 0, end: 20, verified: true, notice: '本人自述未经医学核实' }], missing_fields: ['onset'], next_question: '这种不舒服什么时候开始？', summary: { text: '【虚构】待核对摘要', generated_at: '', source: 'self_report' }, checklist: [{ id: 'one', text: '整理已有资料', done: false }], preparation_plan: null, planner_notice: '', planner_error: null }
}
function job(status: TaskJob['status'] = 'needs_input', visitId = 'visit-one'): TaskJob { return { id: 'job-one', visit_id: visitId, goal: 'prepare_visit', status, created_at: '2026-10-12T08:00:00Z', updated_at: '2026-10-12T08:00:00Z', result: ['queued', 'running', 'interrupted'].includes(status) ? null : run(), error: null, applied_at: null, source_revision: 'internal-revision-never-display' } }
function visit(): Visit { return { id: 'visit-one', title: '虚构就诊准备', created_at: '', updated_at: '', intake: emptyIntake(), messages: [], summary: null, checklist: [], risk: null } }
const props = { visitId: 'visit-one', modelAvailable: true, dirty: false, busy: false }
let wrapper: VueWrapper | undefined
beforeEach(() => { setCsrfToken('csrf-task'); vi.stubGlobal('fetch', vi.fn()); HTMLElement.prototype.scrollTo = vi.fn() })
afterEach(() => { wrapper?.unmount(); wrapper = undefined; vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

describe('实际任务与外部规划边界', () => {
  it('云端任务显示服务内规则规划，外部规划尚未启用时不误报未配置', async () => {
    vi.mocked(fetch).mockResolvedValue(response({ items: [job()] }))
    wrapper = mount(TaskAgentPanel, { props: { ...props, cloud: true, modelAvailable: false } }); await flushPromises()
    expect(wrapper.get('.task-plan').text()).toContain('服务内规则规划')
    expect(wrapper.get('.task-model-choice').text()).toContain('外部规划当前未启用，使用服务内规则')
    expect(wrapper.get('.task-model-choice').text()).not.toContain('未配置')
    expect(wrapper.text()).not.toContain('本机规则规划')
    expect(wrapper.get('.task-model-choice input').attributes('disabled')).toBeDefined()
  })
  it('实际 DeepSeek 计划成功与额度限制回退分别显示，不把回退标成模型成功', async () => {
    const task = job()
    task.result!.plan.planner_mode = 'model'
    vi.mocked(fetch).mockResolvedValue(response({ items: [task] }))
    wrapper = mount(TaskAgentPanel, { props: { ...props, cloud: true } }); await flushPromises()
    expect(wrapper.get('.task-planner').text()).toBe('DeepSeek 规划')
    expect(wrapper.find('.task-fallback').exists()).toBe(false)
    wrapper.unmount()
    task.result!.plan.planner_mode = 'fallback'
    task.result!.planner_error = 'rate_limited'
    task.result!.planner_notice = '已暂停外部规划，使用内置规则执行。'
    wrapper = mount(TaskAgentPanel, { props: { ...props, cloud: true } }); await flushPromises()
    expect(wrapper.get('.task-planner').text()).toBe('服务内回退规划')
    expect(wrapper.get('.task-fallback').text()).toContain('体验调用额度已用完或请求过于频繁')
    expect(wrapper.get('.task-fallback').text()).toContain('已使用服务内规则')
    expect((wrapper.get('.task-model-choice input').element as HTMLInputElement).checked).toBe(false)
    expect(vi.mocked(fetch).mock.calls.every(([, options]) => options?.method === 'GET')).toBe(true)
  })
  it('当前红警立即覆盖旧任务的可保存展示，即使旧结果还未重新读取', async () => {
    vi.mocked(fetch).mockResolvedValue(response({ items: [job()] }))
    wrapper = mount(TaskAgentPanel, { props }); await flushPromises()
    expect(wrapper.find('.task-save>.primary-button').exists()).toBe(true)
    await wrapper.setProps({ currentRisk: { level: 'emergency', kind: 'medical', message: '请立即获得帮助', reasons: [], source_ids: [] } })
    expect(wrapper.get('.task-state').text()).toBe('当前安全提醒优先')
    expect(wrapper.get('.task-risk').text()).toContain('暂停使用旧任务结果')
    expect(wrapper.find('.task-save>.primary-button').exists()).toBe(false)
  })
  it('过期资料明确标为过期，尚无结果的当前红警也能正常显示', async () => {
    const task = { ...job('superseded'), can_apply: false, invalidated_reason: '资料已更新，请重新运行' }
    vi.mocked(fetch).mockResolvedValue(response({ items: [task] }))
    wrapper = mount(TaskAgentPanel, { props }); await flushPromises()
    expect(wrapper.get('.task-state').text()).toBe('结果已过期')
    expect(wrapper.find('.task-save>.primary-button').exists()).toBe(false)
    wrapper.unmount()
    vi.mocked(fetch).mockResolvedValue(response({ items: [{ ...job('emergency'), result: null, can_apply: false, invalidated_reason: '当前安全优先' }] }))
    wrapper = mount(TaskAgentPanel, { props }); await flushPromises()
    expect(wrapper.get('.task-state').text()).toBe('当前安全提醒优先')
    expect(wrapper.text()).not.toContain('完整结果没有读到')
  })
  it('启动默认不使用外部模型，轮询真实状态并在终态停止，没有伪百分比', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    let reads = 0
    const mocked = vi.mocked(fetch).mockImplementation(async (path, options) => {
      if (options?.method === 'POST') return response({ task: job('queued') }, 202)
      if (String(path).endsWith('/tasks')) return response({ items: [] })
      reads++; return response(job(reads === 1 ? 'running' : 'needs_input'))
    })
    wrapper = mount(TaskAgentPanel, { props }); await flushPromises()
    await wrapper.get('.task-start').trigger('click'); await flushPromises()
    const posted = mocked.mock.calls.find(([, options]) => options?.method === 'POST')!
    expect(JSON.parse(String(posted[1]?.body))).toEqual({ goal: 'prepare_visit', use_model: false })
    expect(posted[1]?.credentials).toBe('same-origin')
    expect(posted[1]?.headers).toMatchObject({ 'X-CSRF-Token': 'csrf-task' })
    expect(wrapper.get('.task-state').text()).toBe('已排队')
    expect(wrapper.find('[role="progressbar"]').exists()).toBe(false)
    await vi.advanceTimersByTimeAsync(800); await flushPromises()
    expect(wrapper.get('.task-state').text()).toBe('正在执行')
    await vi.advanceTimersByTimeAsync(800); await flushPromises()
    expect(wrapper.get('.task-state').text()).toBe('需要补充信息')
    expect(wrapper.get('.task-plan').text()).toContain('本机规则规划')
    expect(wrapper.get('.task-steps').text()).toContain('工具执行器')
    await vi.advanceTimersByTimeAsync(8000)
    expect(reads).toBe(2)
  })
  it('只有独立选中规划开关才发送 true，回退原因实际显示，危险输出仍是文字', async () => {
    const task = job(); task.result!.plan.planner_mode = 'fallback'; task.result!.planner_notice = '计划未通过校验，已按内置规则执行。'; task.result!.planner_error = 'invalid_plan'
    const mocked = vi.mocked(fetch).mockImplementation(async (_path, options) => response(options?.method === 'POST' ? { task } : { items: [] }))
    wrapper = mount(TaskAgentPanel, { props }); await flushPromises()
    expect((wrapper.get('.task-model-choice input').element as HTMLInputElement).checked).toBe(false)
    await wrapper.get('.task-model-choice input').setValue(true)
    await wrapper.get('.task-start').trigger('click'); await flushPromises()
    const posted = mocked.mock.calls.find(([, options]) => options?.method === 'POST')!
    expect(JSON.parse(String(posted[1]?.body)).use_model).toBe(true)
    expect(wrapper.get('.task-plan').text()).toContain('本机回退规划')
    expect(wrapper.get('.task-fallback').text()).toContain('计划未通过')
    expect(wrapper.find('img').exists()).toBe(false); expect(wrapper.find('script').exists()).toBe(false); expect(wrapper.find('a').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('internal-revision-never-display')
    await wrapper.setProps({ modelAvailable: false })
    expect(wrapper.get('.task-model-choice input').attributes('disabled')).toBeDefined()
  })
  it('缺信息的准备结果必须另行确认保存，确认前不调用 apply，成功后不重复保存', async () => {
    const task = job()
    const mocked = vi.mocked(fetch).mockImplementation(async (_path, options) => options?.method === 'POST' ? response({ task: { ...task, applied_at: '2026-10-12T08:01:00Z' }, visit: visit() }) : response({ items: [task] }))
    wrapper = mount(TaskAgentPanel, { props: { ...props, dirty: true } }); await flushPromises()
    expect(wrapper.get('.task-draft-note').text()).toContain('草稿尚未加入')
    await wrapper.get('.task-save>.primary-button').trigger('click')
    expect(mocked.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(0)
    expect(wrapper.get('.task-apply-confirmation').text()).toContain('未保存草稿会保留')
    expect(wrapper.get('.task-incomplete').text()).toContain('尚未完整')
    await wrapper.get('.task-apply-confirmation .primary-button').trigger('click'); await flushPromises()
    expect(mocked.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(1)
    expect(wrapper.emitted('applied')).toEqual([[visit()]])
    expect(wrapper.find('.task-save>.primary-button').exists()).toBe(false)
    expect(wrapper.get('.task-save').text()).toContain('本人已确认保存')
  })
  it('过期任务被409拒绝时保留结果并显示原因，不向父页面报告保存成功', async () => {
    vi.mocked(fetch).mockImplementation(async (_path, options) => options?.method === 'POST' ? response({ detail: '本人资料已变化，请重新运行任务。' }, 409) : response({ items: [job()] }))
    wrapper = mount(TaskAgentPanel, { props }); await flushPromises()
    await wrapper.get('.task-save>.primary-button').trigger('click')
    await wrapper.get('.task-apply-confirmation .primary-button').trigger('click'); await flushPromises()
    expect(wrapper.get('.task-apply-confirmation').text()).toContain('资料已变化')
    expect(wrapper.get('.task-result-content').text()).toContain('待核对草稿')
    expect(wrapper.emitted('applied')).toBeUndefined()
  })
  it('切换记录后忽略旧记录慢回包，并在关闭面板时停止轮询', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    let resolveOld!: (value: Response) => void
    const old = new Promise<Response>(resolve => { resolveOld = resolve })
    const mocked = vi.mocked(fetch).mockImplementation(async path => String(path).includes('visit-one') ? old : response({ items: [job('running', 'visit-two')] }))
    wrapper = mount(TaskAgentPanel, { props }); await flushPromises()
    await wrapper.setProps({ visitId: 'visit-two' }); await flushPromises()
    resolveOld(response({ items: [job()] })); await flushPromises()
    expect(wrapper.find('.task-result-content').exists()).toBe(false)
    expect(wrapper.get('.task-state').text()).toBe('正在执行')
    wrapper.unmount(); wrapper = undefined
    await vi.advanceTimersByTimeAsync(8000)
    expect(mocked.mock.calls).toHaveLength(2)
  })
  it('从App保存任务只更新准备材料，保留未保存本人信息', async () => {
    const record = visit(); const task = job()
    vi.mocked(fetch).mockImplementation(async (path, options) => {
      if (path === '/api/bootstrap') return response({ csrf_token: 'csrf', model_available: true, visits: [record], current_visit: record, sources: [] })
      if (String(path).endsWith('/tasks')) return response({ items: [task] })
      if (String(path).endsWith('/apply') && options?.method === 'POST') return response({ task: { ...task, applied_at: '2026-10-12T08:01:00Z' }, visit: { ...record, summary: task.result!.summary } })
      return response(record)
    })
    wrapper = mount(App); await flushPromises()
    await wrapper.get('[data-tab="prepare"]').trigger('click')
    await wrapper.get('.intake-form textarea').setValue('【虚构】不能被任务结果覆盖的新草稿')
    await wrapper.get('[data-tab="overview"]').trigger('click')
    await wrapper.get('.task-agent-entry button').trigger('click'); await flushPromises()
    expect((wrapper.get('.task-model-choice input').element as HTMLInputElement).checked).toBe(false)
    await wrapper.get('.task-save>.primary-button').trigger('click')
    await wrapper.get('.task-apply-confirmation .primary-button').trigger('click'); await flushPromises()
    await wrapper.get('[data-tab="prepare"]').trigger('click')
    expect((wrapper.get('.intake-form textarea').element as HTMLTextAreaElement).value).toContain('不能被任务结果覆盖')
    expect(wrapper.get('.form-footer').text()).toContain('有未保存的修改')
  })
})
