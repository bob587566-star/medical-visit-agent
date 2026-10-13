import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App.vue'
import PrivacyAccountPanel from './components/PrivacyAccountPanel.vue'
import { request, setCsrfToken } from './api'
import { emptyIntake } from './helpers'
import type { Account, PrivacyNotice, Visit } from './types'

const account: Account = { id: 'account-one', username: 'green_leaf', role: 'user' }
const validRecovery = Array(8).fill('deadbeef').join('-')
const privacy: PrivacyNotice = { consent_required: true, accepted: false, notice_version: '2026-10-13.v1', scopes: { local_storage: false, external_ai: false }, retention_days: 7, notice_text: '本机保存健康自述，仅用于虚构资料演示；可以撤回。', model_notice: '另行选择后才允许外部模型处理有限片段。' }
function response(payload: unknown, status = 200): Response { return { ok: status >= 200 && status < 300, status, json: async () => payload, text: async () => JSON.stringify(payload) } as Response }
function record(id = 'visit-one'): Visit { return { id, title: '虚构记录', created_at: '', updated_at: '', intake: emptyIntake(), messages: [], summary: null, checklist: [], risk: null } }
const props = { account: null, privacy, required: true, busy: false }
let wrapper: VueWrapper | undefined
beforeEach(() => { vi.stubGlobal('fetch', vi.fn()); setCsrfToken('initial-csrf'); HTMLElement.prototype.scrollTo = vi.fn(); HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', '') }; HTMLDialogElement.prototype.close = function () { this.removeAttribute('open') } })
afterEach(() => { wrapper?.unmount(); wrapper = undefined; vi.restoreAllMocks(); vi.unstubAllGlobals() })
async function credentials(): Promise<void> { await wrapper!.get('input[name="username"]').setValue('green_leaf'); await wrapper!.get('input[name="password"]').setValue('independent-secret-123') }

describe('隐私选择与身份边界', () => {
  it.each([
    { storage_mode: 'cloud' as const, storage_label: '云端服务', detail: '云端数据库' },
    { storage_mode: 'local' as const, storage_label: '当前本机服务', detail: '当前服务的数据存储' },
  ])('保存同意说明实际位置 $storage_label，仍只在本人选择后提交', async ({ storage_mode, storage_label, detail }) => {
    vi.mocked(fetch).mockResolvedValue(response({}))
    wrapper = mount(PrivacyAccountPanel, { props: { ...props, privacy: { ...privacy, notice_version: '2026-10-14.v2', storage_mode, storage_label, notice_text: '健康自述保存位置将在同意项中显示。' } } })
    const consent = wrapper.findAll('.privacy-consent-form .privacy-check')[0]!
    expect(consent.text()).toContain('保存在' + storage_label)
    expect(consent.text()).toContain(detail)
    if (storage_mode === 'cloud') expect(consent.text()).not.toContain('本机')
    expect(fetch).not.toHaveBeenCalled()
    await consent.get('input').setValue(true)
    await wrapper.get('.privacy-consent-form').trigger('submit'); await flushPromises()
    expect(JSON.parse(String(vi.mocked(fetch).mock.calls[0]![1]!.body))).toEqual({ notice_version: '2026-10-14.v2', local_storage: true, external_ai: false, retention_days: 7 })
  })
  it('两个同意默认均未勾选，保留期限明确，仅明确确认后发送同意', async () => {
    vi.mocked(fetch).mockResolvedValue(response({}))
    wrapper = mount(PrivacyAccountPanel, { props })
    const checks = wrapper.findAll('.privacy-consent-form input[type="checkbox"]')
    expect(checks.map(item => (item.element as HTMLInputElement).checked)).toEqual([false, false])
    expect(wrapper.get('.privacy-consent-submit').attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('2026-10-13.v1')
    await wrapper.get('.privacy-consent-form').trigger('submit'); expect(fetch).not.toHaveBeenCalled()
    await checks[0]!.setValue(true); await wrapper.get('.privacy-retention select').setValue('1')
    await wrapper.get('.privacy-consent-form').trigger('submit'); await flushPromises()
    expect(vi.mocked(fetch).mock.calls[0]![0]).toBe('/api/privacy/consent')
    const options = vi.mocked(fetch).mock.calls[0]![1]!
    expect(JSON.parse(String(options.body))).toEqual({ notice_version: '2026-10-13.v1', local_storage: true, external_ai: false, retention_days: 1 })
    expect(options.headers).toMatchObject({ 'X-CSRF-Token': 'initial-csrf' })
    expect(wrapper.emitted('changed')).toEqual([[]])
  })
  it('外部同意独立选择，接口失败不会被页面当作已同意', async () => {
    vi.mocked(fetch).mockResolvedValue(response({ detail: '告知版本已变化，请重新加载。' }, 409))
    wrapper = mount(PrivacyAccountPanel, { props })
    for (const check of wrapper.findAll('.privacy-consent-form input[type="checkbox"]')) await check.setValue(true)
    await wrapper.get('.privacy-consent-form').trigger('submit'); await flushPromises()
    expect(JSON.parse(String(vi.mocked(fetch).mock.calls[0]![1]!.body)).external_ai).toBe(true)
    expect(wrapper.get('[role="alert"]').text()).toContain('版本已变化')
    expect(wrapper.emitted('changed')).toBeUndefined()
  })
  it('没有告知版本时不能写入同意，昵称限制避免把手机号直接当账号', async () => {
    wrapper = mount(PrivacyAccountPanel, { props: { ...props, privacy: { ...privacy, notice_version: '' } } })
    await wrapper.findAll('.privacy-consent-form input[type="checkbox"]')[0]!.setValue(true)
    await wrapper.get('.privacy-consent-form').trigger('submit'); expect(fetch).not.toHaveBeenCalled()
    await credentials(); await wrapper.get('input[name="username"]').setValue('13800138000')
    expect(wrapper.get('.account-submit').attributes('disabled')).toBeDefined()
    await wrapper.get('.account-form').trigger('submit'); expect(fetch).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('手机号与微信登录尚未接入')
    expect(wrapper.find('select[name="role"]').exists()).toBe(false)
  })
  it('注册先替换CSRF，恢复码只显示一次且没有写浏览器存储', async () => {
    const storage = vi.spyOn(Storage.prototype, 'setItem')
    vi.mocked(fetch).mockImplementation(async path => response(String(path).endsWith('/register') ? { account, csrf_token: 'new-session-csrf', recovery_code: 'ONE-TIME-RECOVERY-CODE' } : {}))
    wrapper = mount(PrivacyAccountPanel, { props })
    await wrapper.findAll('.account-mode-nav button')[1]!.trigger('click'); await credentials()
    await wrapper.get('.account-form').trigger('submit'); await flushPromises()
    expect(wrapper.get('.recovery-code').text()).toBe('ONE-TIME-RECOVERY-CODE')
    expect(wrapper.emitted('changed')).toBeUndefined(); expect(storage).not.toHaveBeenCalled()
    expect(wrapper.find('input[name="password"]').exists()).toBe(false)
    await request('/csrf-probe', 'POST', {})
    expect(vi.mocked(fetch).mock.calls.at(-1)![1]!.headers).toMatchObject({ 'X-CSRF-Token': 'new-session-csrf' })
    await wrapper.get('.recovery-code-box .primary-button').trigger('click')
    expect(wrapper.emitted('changed')).toEqual([[true]])
    expect(wrapper.find('.recovery-code').exists()).toBe(false)
  })
  it('恢复用新密码与恢复码，保留密码的首尾空格，不传用户选择的角色', async () => {
    vi.mocked(fetch).mockResolvedValue(response({ account, csrf_token: 'recovered-csrf' }))
    wrapper = mount(PrivacyAccountPanel, { props })
    await wrapper.findAll('.account-mode-nav button')[2]!.trigger('click'); await credentials()
    await wrapper.get('input[name="password"]').setValue('  independent-secret  ')
    await wrapper.get('input[name="recovery_code"]').setValue(validRecovery)
    await wrapper.get('.account-form').trigger('submit'); await flushPromises()
    expect(vi.mocked(fetch).mock.calls[0]![0]).toBe('/api/account/recover')
    expect(JSON.parse(String(vi.mocked(fetch).mock.calls[0]![1]!.body))).toEqual({ username: 'green_leaf', recovery_code: validRecovery, new_password: '  independent-secret  ' })
    expect(wrapper.emitted('changed')).toEqual([[true]])
  })
  it('无效恢复码始终禁用提交，完整十六进制码可以保留分组和首尾空格', async () => {
    wrapper = mount(PrivacyAccountPanel, { props })
    await wrapper.findAll('.account-mode-nav button')[2]!.trigger('click'); await credentials()
    const input = wrapper.get('input[name="recovery_code"]')
    expect(input.attributes('minlength')).toBe('32'); expect(input.attributes('maxlength')).toBe('100')
    for (const invalid of ['', 'short-private-code', 'a'.repeat(63), 'g'.repeat(64), 'a'.repeat(65), 'a'.repeat(64) + '---'.repeat(13), 'a'.repeat(32) + '中文' + 'a'.repeat(32), 'a'.repeat(32) + ' ' + 'a'.repeat(32)]) {
      await input.setValue(invalid)
      expect(wrapper.get('.account-submit').attributes('disabled')).toBeDefined()
      await wrapper.get('.account-form').trigger('submit')
      expect(fetch).not.toHaveBeenCalled()
    }
    await input.setValue('  ' + validRecovery.toUpperCase() + '  ')
    expect(wrapper.get('.account-submit').attributes('disabled')).toBeUndefined()
  })
  it('恢复成功明确旧码失效，只显示新的码且不声称建立了新账号', async () => {
    const freshCode = Array(8).fill('feed1234').join('-')
    const storage = vi.spyOn(Storage.prototype, 'setItem')
    vi.mocked(fetch).mockResolvedValue(response({ account, csrf_token: 'recovered-new-csrf', recovery_code: freshCode }))
    wrapper = mount(PrivacyAccountPanel, { props })
    await wrapper.findAll('.account-mode-nav button')[2]!.trigger('click'); await credentials()
    await wrapper.get('input[name="recovery_code"]').setValue(validRecovery)
    await wrapper.get('.account-form').trigger('submit'); await flushPromises()
    expect(wrapper.get('[role="status"]').text()).toContain('密码已重设')
    expect(wrapper.get('[role="status"]').text()).toContain('旧恢复码和旧登录会话已失效')
    expect(wrapper.text()).not.toContain('账号已建立')
    expect(wrapper.get('.recovery-code').text()).toBe(freshCode)
    expect(wrapper.get('#recovery-title').text()).toContain('新的恢复码，只显示这一次')
    expect(wrapper.text()).not.toContain(validRecovery)
    expect(storage).not.toHaveBeenCalled(); expect(wrapper.emitted('changed')).toBeUndefined()
    await wrapper.get('.recovery-code-box .primary-button').trigger('click')
    expect(wrapper.find('.recovery-code').exists()).toBe(false)
    expect(wrapper.emitted('changed')).toEqual([[true]])
  })
  it('撤回需要二次确认，取消不发删除；注销还需当前密码', async () => {
    vi.mocked(fetch).mockResolvedValue(response(undefined, 204))
    wrapper = mount(PrivacyAccountPanel, { props: { ...props, account, required: false, privacy: { ...privacy, accepted: true, scopes: { local_storage: true, external_ai: false } } } })
    await wrapper.get('.privacy-withdraw').trigger('click')
    expect(fetch).not.toHaveBeenCalled(); expect(wrapper.get('.privacy-confirmation').text()).toContain('不能从此页面恢复')
    await wrapper.get('.privacy-confirmation .secondary-button').trigger('click'); expect(fetch).not.toHaveBeenCalled()
    await wrapper.get('.account-delete').trigger('click')
    expect(wrapper.get('.privacy-confirm-submit').attributes('disabled')).toBeDefined()
    await wrapper.get('.delete-password input').setValue('current-private-password')
    await wrapper.get('.privacy-confirm-submit').trigger('click'); await flushPromises()
    const [path, options] = vi.mocked(fetch).mock.calls[0]!
    expect(path).toBe('/api/account'); expect(options!.method).toBe('DELETE')
    expect(JSON.parse(String(options!.body))).toEqual({ password: 'current-private-password' })
    expect(wrapper.emitted('changed')).toEqual([[true]])
  })
  it('匿名撤回同样可用；删除失败不会清空页面或报告成功', async () => {
    vi.mocked(fetch).mockResolvedValue(response({ detail: '删除未完成，请重试。' }, 503))
    wrapper = mount(PrivacyAccountPanel, { props: { ...props, required: false, privacy: { ...privacy, accepted: true, scopes: { local_storage: true, external_ai: false } } } })
    await wrapper.get('.privacy-withdraw').trigger('click'); await wrapper.get('.privacy-confirm-submit').trigger('click'); await flushPromises()
    expect(vi.mocked(fetch).mock.calls[0]![0]).toBe('/api/privacy/consent')
    expect(vi.mocked(fetch).mock.calls[0]![1]!.method).toBe('DELETE')
    expect(wrapper.emitted('changed')).toBeUndefined(); expect(wrapper.get('[role="alert"]').text()).toContain('删除未完成')
  })
  it('有草稿时先确认切换身份，退出不会偷偷删除账号资料', async () => {
    vi.mocked(fetch).mockResolvedValue(response({ csrf_token: 'guest-new-csrf' }))
    wrapper = mount(PrivacyAccountPanel, { props: { ...props, account, dirty: true, required: false } })
    await wrapper.get('.account-logout').trigger('click')
    expect(wrapper.get('.privacy-confirmation').text()).toContain('不自动把账号资料复制给访客')
    await wrapper.get('.privacy-confirm-submit').trigger('click'); expect(fetch).not.toHaveBeenCalled()
    await wrapper.get('.privacy-confirmation input[type="checkbox"]').setValue(true)
    await wrapper.get('.privacy-confirm-submit').trigger('click'); await flushPromises()
    expect(vi.mocked(fetch).mock.calls[0]![0]).toBe('/api/account/logout')
    expect(vi.mocked(fetch).mock.calls[0]![1]!.method).toBe('POST')
    expect(wrapper.emitted('changed')).toEqual([[true]])
  })
})

describe('工作台隐私入口与未授权状态', () => {
  it('未同意bootstrap绝不自动建记录，公开资料可以只读浏览', async () => {
    vi.mocked(fetch).mockImplementation(async path => path === '/api/bootstrap' ? response({ csrf_token: 'csrf', model_available: true, privacy, visits: [], current_visit: null, sources: [] }) : response({ items: [], categories: [], bookmark_ids: [], total: 0 }))
    wrapper = mount(App); await flushPromises()
    expect(wrapper.find('.privacy-account-panel').exists()).toBe(true)
    expect(wrapper.find('.work-grid').exists()).toBe(false)
    expect(wrapper.get('.new-visit').attributes('disabled')).toBeDefined()
    expect(vi.mocked(fetch).mock.calls.some(([, options]) => options?.method === 'POST')).toBe(false)
    await wrapper.findAll('.sidebar-main-nav button').at(4)!.trigger('click'); await flushPromises()
    expect(wrapper.find('.library-panel').exists()).toBe(true)
    expect(wrapper.get('.saved-filter').attributes('disabled')).toBeDefined()
    expect(vi.mocked(fetch).mock.calls.some(([path]) => path === '/api/workspace')).toBe(false)
  })
  it('确认同意后重载，再创建记录；未选择外部模型时不启用开关', async () => {
    let accepted = false
    vi.mocked(fetch).mockImplementation(async (path, options) => {
      if (path === '/api/bootstrap') return response({ csrf_token: 'csrf', model_available: true, privacy: { ...privacy, accepted, scopes: { local_storage: accepted, external_ai: false } }, visits: [], current_visit: null, sources: [] })
      if (path === '/api/privacy/consent' && options?.method === 'POST') { accepted = true; return response({}) }
      return response(record())
    })
    wrapper = mount(App); await flushPromises()
    await wrapper.findAll('.privacy-consent-form input[type="checkbox"]')[0]!.setValue(true)
    await wrapper.get('.privacy-consent-form').trigger('submit'); await flushPromises()
    expect(wrapper.find('.privacy-account-panel').exists()).toBe(false)
    const posts = vi.mocked(fetch).mock.calls.filter(([, options]) => options?.method === 'POST')
    expect(posts.map(([path]) => path)).toEqual(['/api/privacy/consent', '/api/visits'])
    expect(wrapper.get('.toggle-switch').attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('尚未单独同意外部模型处理')
  })
  it('登录切换先替换CSRF并清掉旧页面草稿，不把访客原内容合并到其他账号', async () => {
    let loggedIn = false
    vi.mocked(fetch).mockImplementation(async (path, options) => {
      if (path === '/api/bootstrap') return response({ csrf_token: loggedIn ? 'new-session' : 'visitor-session', model_available: false, privacy: { ...privacy, accepted: true, scopes: { local_storage: true, external_ai: false } }, account: loggedIn ? account : null, visits: loggedIn ? [] : [record('guest-visit')], current_visit: loggedIn ? null : record('guest-visit'), sources: [] })
      if (path === '/api/account/login') { loggedIn = true; return response({ account, csrf_token: 'new-session' }) }
      if (path === '/api/visits' && options?.method === 'POST') return response(record('account-visit'))
      return response(record())
    })
    wrapper = mount(App); await flushPromises()
    await wrapper.get('[data-tab="prepare"]').trigger('click')
    await wrapper.get('.intake-form textarea').setValue('仅应留在原访客草稿')
    await wrapper.get('.privacy-settings-link').trigger('click'); await credentials()
    expect(wrapper.get('.account-submit').attributes('disabled')).toBeDefined()
    await wrapper.get('.account-form .privacy-check input').setValue(true)
    await wrapper.get('.account-form').trigger('submit'); await flushPromises()
    expect(wrapper.text()).not.toContain('仅应留在原访客草稿')
    const create = vi.mocked(fetch).mock.calls.find(([path, options]) => path === '/api/visits' && options?.method === 'POST')!
    expect(create[1]!.headers).toMatchObject({ 'X-CSRF-Token': 'new-session' })
    expect(wrapper.findAll('.visit-select').map(item => item.attributes('aria-current'))).toEqual(['page'])
    expect(vi.mocked(fetch).mock.calls.some(([path]) => String(path).includes('guest-visit/intake'))).toBe(false)
  })
  it('公示为空显示未配置，危险链接与HTML不执行，不冒充完成备案', async () => {
    vi.mocked(fetch).mockResolvedValue(response({ csrf_token: 'csrf', model_available: false, privacy, visits: [], current_visit: null, sources: [], compliance: { operator: { name: null, contact_url: null, privacy_url: 'javascript:alert(1)' }, public_service_status: 'unconfigured', records: [{ type: 'algorithm', label: '<img src=x onerror=alert(1)>', number: null, url: 'javascript:alert(1)', status: 'unconfigured' }] } }))
    wrapper = mount(App); await flushPromises()
    expect(wrapper.get('.compliance-footer').text()).toContain('未配置')
    expect(wrapper.get('.compliance-footer').text()).toContain('不代表本项目已取得医疗资格')
    expect(wrapper.find('.compliance-footer a').exists()).toBe(false); expect(wrapper.find('img').exists()).toBe(false)
  })
})
