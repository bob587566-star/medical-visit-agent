import { expect, test, type Page } from '@playwright/test'

const credentials = new Map<Page, { username: string; password: string }>()

test.afterEach(async ({ page }) => {
  const data = await (await page.request.get('/api/bootstrap')).json()
  const identity = credentials.get(page)
  let csrf = data.csrf_token
  if (identity) {
    if (!data.account) {
      const login = await page.request.post('/api/account/login', { headers: { 'X-CSRF-Token': csrf }, data: identity })
      if (login.ok()) csrf = (await login.json()).csrf_token
    }
    await page.request.delete('/api/account', { headers: { 'X-CSRF-Token': csrf }, data: { password: identity.password } })
    credentials.delete(page)
  } else {
    await page.request.delete('/api/privacy/consent', { headers: { 'X-CSRF-Token': csrf } })
  }
})

async function agree(page: Page) {
  await page.getByRole('checkbox', { name: '同意将本人的健康自述保存在当前本机服务', exact: false }).check()
  await page.getByRole('button', { name: '确认选择，开始准备' }).click()
  await expect(page.locator('.privacy-gate-note')).toHaveCount(0)
  await expect(page.locator('.new-visit')).toBeEnabled()
}

test('同意前不建记录；公开资料可读；手机隐私页完整且外部同意不预选', async ({ page }) => {
  await page.goto('/')
  await expect(page.locator('.privacy-gate-note')).toBeVisible()
  let bootstrap = await (await page.request.get('/api/bootstrap')).json()
  expect(bootstrap.visits).toEqual([])
  expect(bootstrap.privacy.accepted).toBe(false)
  await expect(page.getByRole('checkbox', { name: '另行同意外部模型辅助', exact: false })).not.toBeChecked()
  for (const width of [390, 320]) {
    await page.setViewportSize({ width, height: 844 })
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width)
    await expect(page.locator('.privacy-consent-submit')).toBeDisabled()
  }
  await page.setViewportSize({ width: 1440, height: 1080 })
  await page.locator('.sidebar-main-nav').getByRole('button', { name: '健康资料库', exact: false }).click()
  await expect(page.locator('.library-panel')).toBeVisible()
  bootstrap = await (await page.request.get('/api/bootstrap')).json()
  expect(bootstrap.visits).toEqual([])
  await agree(page)
  bootstrap = await (await page.request.get('/api/bootstrap')).json()
  expect(bootstrap.privacy.scopes).toEqual({ local_storage: true, external_ai: false })
  expect(bootstrap.visits).toHaveLength(1)
  await expect(page.getByRole('switch')).toBeDisabled()
})

test('真实账号闭环：绑定访客、清Cookie登录找回、一次性恢复、导出、撤回与注销', async ({ page }) => {
  await page.goto('/')
  await agree(page)
  const before = await (await page.request.get('/api/bootstrap')).json()
  const visitId = before.current_visit.id
  await page.locator('.privacy-settings-link').click()
  await page.locator('.account-mode-nav').getByRole('button', { name: '注册', exact: true }).click()
  const identity = { username: 'fictional_' + Date.now().toString(36), password: 'Only-fictional-password-2026!' }
  await page.getByLabel('昵称账号', { exact: false }).fill(identity.username)
  await page.locator('.account-form input[name=password]').fill(identity.password)
  await page.getByRole('button', { name: '建立账号', exact: true }).click()
  await expect(page.locator('.recovery-code')).toBeVisible()
  const recovery = (await page.locator('.recovery-code').textContent())!.trim()
  credentials.set(page, identity)
  await page.getByRole('button', { name: '已私密保存，返回工作台' }).click()
  await expect(page.locator('.privacy-gate-note')).toHaveCount(0)
  expect((await (await page.request.get('/api/bootstrap')).json()).current_visit.id).toBe(visitId)
  await page.context().clearCookies()
  await page.reload()
  await expect(page.locator('.privacy-gate-note')).toBeVisible()
  await page.getByLabel('昵称账号', { exact: false }).fill(identity.username)
  await page.locator('.account-form input[name=password]').fill(identity.password)
  await page.getByRole('button', { name: '登录账号', exact: true }).click()
  await expect(page.locator('.privacy-gate-note')).toHaveCount(0)
  expect((await (await page.request.get('/api/bootstrap')).json()).current_visit.id).toBe(visitId)
  await page.context().clearCookies()
  await page.reload()
  await page.locator('.account-mode-nav').getByRole('button', { name: '恢复账号', exact: true }).click()
  await page.getByLabel('昵称账号', { exact: false }).fill(identity.username)
  await page.getByLabel('私密恢复码').fill(recovery)
  const newPassword = 'Changed-fictional-password-2026!'
  await page.locator('.account-form input[name=password]').fill(newPassword)
  await page.getByRole('button', { name: '重设密码并登录' }).click()
  await expect(page.locator('.recovery-code')).toBeVisible()
  identity.password = newPassword
  await page.getByRole('button', { name: '已私密保存，返回工作台' }).click()
  await page.locator('.privacy-settings-link').click()
  const downloadEvent = page.waitForEvent('download')
  await page.getByRole('button', { name: '导出本人资料' }).click()
  expect((await downloadEvent).suggestedFilename()).toContain('本人资料')
  await page.getByRole('button', { name: '撤回保存同意', exact: true }).click()
  await page.getByRole('button', { name: '确认撤回并删除' }).click()
  await expect(page.locator('.privacy-gate-note')).toBeVisible()
  const after = await (await page.request.get('/api/bootstrap')).json()
  expect(after.account.username).toBe(identity.username)
  expect(after.visits).toEqual([])
  expect(after.privacy.accepted).toBe(false)
  await page.getByRole('button', { name: '注销账号', exact: true }).click()
  await page.getByLabel('输入当前密码确认').fill(newPassword)
  await page.getByRole('button', { name: '确认注销并删除' }).click()
  await expect(page.locator('.account-form')).toBeVisible()
  expect((await (await page.request.get('/api/bootstrap')).json()).account).toBeNull()
  credentials.delete(page)
})
