import { expect, test, type Page } from '@playwright/test'
import { mkdir, readFile } from 'node:fs/promises'
import { resolve } from 'node:path'

const artifacts = resolve(process.cwd(), '../artifacts')
const fictionalChief = '【虚构病例】轻微咳嗽持续两天，想整理需要向医生说明的信息。'

test.afterEach(async ({ page }) => {
  // This request context belongs only to the isolated test browser, never to a user's browser.
  const bootstrap = await page.request.get('/api/bootstrap')
  if (!bootstrap.ok()) return
  const data = await bootstrap.json()
  if (typeof data.csrf_token === 'string') {
    const deleted = await page.request.delete('/api/data', { headers: { 'X-CSRF-Token': data.csrf_token } })
    expect(deleted.status()).toBe(204)
  }
})

async function openApp(page: Page): Promise<void> {
  await page.goto('/')
  await expect(page.locator('.local-status')).toContainText('本机服务已连接')
  await expect(page.getByRole('switch')).toHaveAttribute('aria-checked', 'false')
}
async function tab(page: Page, name: '信息准备' | '健康问答' | '就诊摘要'): Promise<void> {
  await page.locator('.desktop-tabs').getByRole('button', { name, exact: true }).click()
}
async function fillFictionalIntake(page: Page): Promise<void> {
  await tab(page, '信息准备')
  await page.getByLabel('主要不适 / 就诊原因').fill(fictionalChief)
  await page.getByLabel('什么时候开始的？').fill('【虚构】两天前开始，具体时间待核实')
  await page.getByLabel('年龄组', { exact: true }).selectOption('成人')
  await page.getByLabel('你感受到的严重程度').selectOption('轻')
  await page.getByLabel('正在使用的药物').fill('【虚构】未使用药物')
  await page.getByLabel('过敏情况').fill('【虚构】不清楚，待核实')
  await page.getByLabel('既往病史').fill('【虚构】未填写，待本人补充')
  await page.getByLabel('补充备注').fill('全部内容仅为虚构 E2E 演示，不代表真实病情。')
  await page.getByRole('button', { name: '保存就诊信息', exact: false }).click()
  await expect(page.locator('.feedback-success')).toContainText('就诊信息已保存')
}
async function ask(page: Page, content: string): Promise<void> {
  await tab(page, '健康问答')
  const count = await page.locator('.message-assistant').count()
  await page.getByLabel('描述不适或提出健康问题').fill(content)
  await page.getByRole('button', { name: '发送问题', exact: true }).click()
  await expect(page.locator('.message-assistant')).toHaveCount(count + 1)
  await expect(page.locator('.working-message')).toHaveCount(0)
  await expect(page.locator('.message-assistant').last().locator('.message-content')).not.toBeEmpty()
}
async function assertNoOverflow(page: Page, width: number): Promise<void> {
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width)
  expect(await page.evaluate(() => document.body.scrollWidth)).toBeLessThanOrEqual(width)
}
async function screenshot(page: Page, filename: string): Promise<void> {
  await mkdir(artifacts, { recursive: true })
  await page.evaluate(() => window.scrollTo({ top: 0, behavior: 'instant' }))
  await page.screenshot({ path: resolve(artifacts, filename), fullPage: true })
}

test('真实 UI：填表保存刷新、健康来源/未知题、摘要、清单、下载与打印', async ({ page }) => {
  const externalChoices: boolean[] = []
  const pageErrors: string[] = []
  page.on('pageerror', error => pageErrors.push(error.message))
  page.on('request', request => {
    if (request.method() === 'POST' && /\/(messages|summary)$/.test(request.url())) externalChoices.push(request.postDataJSON().use_model)
  })
  await openApp(page)
  await screenshot(page, 'zhihe-desktop-1440-welcome.png')
  await fillFictionalIntake(page)
  await page.reload()
  await expect(page.locator('.local-status')).toContainText('本机服务已连接')
  await tab(page, '信息准备')
  await expect(page.getByLabel('主要不适 / 就诊原因')).toHaveValue(fictionalChief)
  await expect(page.getByLabel('过敏情况')).toHaveValue('【虚构】不清楚，待核实')

  await ask(page, '【虚构案例】就诊准备需要带什么材料？')
  const sourced = page.locator('.message-assistant').last()
  await expect(sourced.locator('.source-card')).not.toHaveCount(0)
  const urls = await sourced.locator('.source-card a').evaluateAll(links => links.map(link => (link as HTMLAnchorElement).href))
  expect(urls.length).toBeGreaterThan(0)
  expect(urls.every(url => url.startsWith('https://'))).toBe(true)
  await sourced.locator('.execution-details summary').click()
  await expect(sourced.locator('.execution-details ol li')).toHaveCount(5)
  await expect(sourced.locator('.execution-details')).toContainText('不展示模型隐式思维')
  await screenshot(page, 'zhihe-desktop-1440-chat.png')

  await ask(page, '【虚构主题】量子纠缠蓝色羽毛球的医学解释')
  const unsupported = page.locator('.message-assistant').last()
  await expect(unsupported.locator('.message-content')).toContainText('没有与你的问题对应的已核对资料')
  await expect(unsupported.locator('.source-card')).toHaveCount(0)

  await page.locator('.generate-button').click()
  await expect(page.locator('.summary-document')).toBeVisible()
  await expect(page.locator('.summary-text').first()).toContainText(fictionalChief)
  await expect(page.locator('.summary-text').first()).toContainText('未经医学核实')
  await expect(page.locator('.summary-text').first()).toContainText('未填写')

  const item = page.locator('.checklist input').first()
  await item.click()
  await expect(item).toBeChecked()
  await page.reload()
  await expect(page.locator('.local-status')).toContainText('本机服务已连接')
  await expect(page.locator('.checklist input').first()).toBeChecked()
  await tab(page, '就诊摘要')

  const downloadPromise = page.waitForEvent('download')
  await page.getByRole('button', { name: '下载 TXT', exact: true }).click()
  const download = await downloadPromise
  expect(download.suggestedFilename()).toMatch(/知禾-就诊准备-.+\.txt$/)
  const downloadPath = resolve(artifacts, 'browser-export-fictional.txt')
  await download.saveAs(downloadPath)
  const downloaded = await readFile(downloadPath, 'utf8')
  expect(downloaded).toContain(fictionalChief)
  expect(downloaded).toContain('未经医学核实')
  expect(downloaded).toContain('[x]')
  expect(downloaded).toContain('不提供诊断或处方')
  await page.evaluate(() => { window.print = () => { document.documentElement.dataset.printRequested = 'yes' } })
  await page.getByRole('button', { name: '打印', exact: true }).click()
  await expect(page.locator('html')).toHaveAttribute('data-print-requested', 'yes')
  await screenshot(page, 'zhihe-desktop-1440-summary.png')
  await page.emulateMedia({ media: 'print' })
  await expect(page.locator('.print-only')).toBeVisible()
  await expect(page.locator('.workspace')).toBeHidden()
  await expect(page.locator('.print-only')).toContainText(fictionalChief)
  await page.pdf({ path: resolve(artifacts, 'zhihe-print-fictional-summary.pdf'), format: 'A4', printBackground: true })
  expect((await readFile(resolve(artifacts, 'zhihe-print-fictional-summary.pdf'))).subarray(0, 4).toString()).toBe('%PDF')
  expect(externalChoices.length).toBeGreaterThanOrEqual(3)
  expect(externalChoices.every(choice => choice === false)).toBe(true)
  expect(pageErrors).toEqual([])
})

test('真实 DOM：恶意标记只显示为自述文字，不执行脚本', async ({ page }) => {
  const payload = '<img src=x onerror="window.__zhiheXss=true">【虚构】'
  const note = '<script>window.__zhiheXss=true</script>仅虚构安全测试'
  await openApp(page)
  await tab(page, '信息准备')
  await page.getByLabel('主要不适 / 就诊原因').fill(payload)
  await page.getByLabel('补充备注').fill(note)
  await page.getByRole('button', { name: '保存就诊信息', exact: false }).click()
  await expect(page.locator('.feedback-success')).toContainText('已保存')
  await ask(page, payload)
  await expect(page.locator('.message-user').last()).toContainText(payload)
  await expect(page.locator('.message-user img')).toHaveCount(0)
  await page.locator('.generate-button').click()
  await expect(page.locator('.summary-document')).toBeVisible()
  await expect(page.locator('.summary-text').first()).toContainText(payload)
  await expect(page.locator('.summary-text').first()).toContainText(note)
  await expect(page.locator('.summary-document img')).toHaveCount(0)
  await expect(page.locator('.summary-document script')).toHaveCount(0)
  expect(await page.evaluate(() => '__zhiheXss' in window)).toBe(false)
})

test('虚构当前急症：紧急卡优先显示，并保留明确工具状态', async ({ page }) => {
  await openApp(page)
  await ask(page, '【虚构演示】我现在严重呼吸困难，几乎喘不过气。')
  await expect(page.locator('.risk-emergency')).toBeVisible()
  await expect(page.locator('.risk-emergency')).toContainText('立即联系当地急救')
  await expect(page.locator('.message-assistant').last().locator('.message-mode')).toHaveText('紧急提醒优先')
  await expect(page.locator('.message-assistant').last().locator('.execution-details ol li')).toHaveCount(1)
  await screenshot(page, 'zhihe-desktop-1440-emergency-fictional.png')
})

for (const width of [390, 320]) {
  test(`${width}px 手机：三页签与长内容无横向溢出`, async ({ page }) => {
    await page.setViewportSize({ width, height: 844 })
    await openApp(page)
    await expect(page.locator('.mobile-tabs')).toBeVisible()
    await assertNoOverflow(page, width)
    await screenshot(page, `zhihe-mobile-${width}-welcome.png`)
    await fillFictionalIntake(page)
    await assertNoOverflow(page, width)
    await screenshot(page, `zhihe-mobile-${width}-prepare.png`)
    await ask(page, '【虚构案例】就诊准备需要带什么材料？')
    await expect(page.locator('.source-card')).not.toHaveCount(0)
    await assertNoOverflow(page, width)
    await screenshot(page, `zhihe-mobile-${width}-chat.png`)
    await page.locator('.generate-button').click()
    await expect(page.locator('.summary-document')).toBeVisible()
    await assertNoOverflow(page, width)
    await screenshot(page, `zhihe-mobile-${width}-summary.png`)
    await page.locator('.mobile-tabs').getByRole('button', { name: '准备', exact: true }).click()
    await expect(page.locator('.intake-form')).toBeVisible()
    await page.locator('.mobile-tabs').getByRole('button', { name: '问答', exact: true }).click()
    await expect(page.locator('.composer')).toBeVisible()
    await page.locator('.mobile-tabs').getByRole('button', { name: '摘要', exact: true }).click()
    await expect(page.locator('.summary-document')).toBeVisible()
    await expect(page.getByRole('switch')).toHaveAttribute('aria-checked', 'false')
  })
}
