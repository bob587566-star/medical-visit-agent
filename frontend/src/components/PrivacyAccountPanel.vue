<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ApiError, request, setCsrfToken } from '../api'
import type { Account, PrivacyNotice } from '../types'

const props = defineProps<{ account: Account | null; privacy: Partial<PrivacyNotice> | null; required: boolean; busy: boolean; dirty?: boolean }>()
const emit = defineEmits<{ changed: [identityChanged?: boolean]; close: [] }>()
const action = ref('')
const error = ref('')
const notice = ref('')
const mode = ref<'register' | 'login' | 'recover'>('login')
const username = ref('')
const password = ref('')
const recoveryInput = ref('')
const recoveryCode = ref('')
const localStorageConsent = ref(false)
const externalAiConsent = ref(false)
const retention = ref(7)
const confirmAction = ref<'withdraw' | 'delete' | 'logout' | null>(null)
const deletePassword = ref('')
const discardDraft = ref(false)
const locked = computed(() => props.busy || !!action.value || !!recoveryCode.value)
const accountActionAllowed = computed(() => !props.dirty || discardDraft.value)
const validRecoveryCode = computed(() => recoveryInput.value.length >= 32 && recoveryInput.value.length <= 100 && /^[a-f0-9]{64}$/i.test(recoveryInput.value.trim().replaceAll('-', '')))
const validCredentials = computed(() => /^[A-Za-z][A-Za-z0-9._-]{2,39}$/.test(username.value.trim()) && password.value.length >= 15 && password.value.length <= 128 && (mode.value !== 'recover' || validRecoveryCode.value))
const noticeVersion = computed(() => props.privacy?.notice_version || '')
const roleLabel = computed(() => ({ user: '个人账号', reviewer: '内容审核账号', admin: '管理账号' }[props.account?.role || 'user']))
watch(() => props.privacy, privacy => {
  localStorageConsent.value = privacy?.accepted === true && privacy?.scopes?.local_storage === true
  externalAiConsent.value = privacy?.accepted === true && privacy?.scopes?.external_ai === true
  retention.value = [1, 7, 30].includes(privacy?.retention_days || 0) ? privacy!.retention_days! : 7
}, { immediate: true })
watch(mode, () => { password.value = ''; recoveryInput.value = ''; error.value = ''; notice.value = '' })
async function run(label: string, work: () => Promise<void>): Promise<void> {
  if (locked.value) return
  action.value = label; error.value = ''; notice.value = ''
  try { await work() }
  catch (caught) { error.value = caught instanceof Error ? caught.message : '操作没有完成，请重试。' }
  finally { action.value = '' }
}
async function accountRequest(): Promise<void> {
  if (!validCredentials.value || !accountActionAllowed.value) return
  await run('account', async () => {
    const body = mode.value === 'recover'
      ? { username: username.value.trim(), recovery_code: recoveryInput.value.trim(), new_password: password.value }
      : { username: username.value.trim(), password: password.value }
    const result = await request<{ account: Account; csrf_token: string; recovery_code?: string }>(`/api/account/${mode.value}`, 'POST', body)
    password.value = ''; recoveryInput.value = ''
    if (!result.csrf_token || !result.account?.id) throw new ApiError('账号会话未完整返回，请重新连接后核对账号状态。')
    setCsrfToken(result.csrf_token)
    // The one-time recovery code stays in component memory until acknowledged.
    if (result.recovery_code) {
      recoveryCode.value = result.recovery_code
      notice.value = mode.value === 'recover' ? '密码已重设，旧恢复码和旧登录会话已失效。请私密保存新的恢复码，再返回工作台。' : '账号已建立。先私密保存恢复码，再返回工作台。'
    }
    else emit('changed', true)
  })
}
function acknowledgeRecovery(): void { recoveryCode.value = ''; emit('changed', true) }
async function copyRecovery(): Promise<void> {
  try {
    if (!navigator.clipboard?.writeText) throw new Error('浏览器未允许复制，请手动选中恢复码保存。')
    await navigator.clipboard.writeText(recoveryCode.value); notice.value = '恢复码已复制，请放入你的密码管理器，不要发送给别人。'
  } catch (caught) { error.value = caught instanceof Error ? caught.message : '请手动保存恢复码。' }
}
async function consent(): Promise<void> {
  if (!localStorageConsent.value || !noticeVersion.value) return
  await run('consent', async () => {
    await request('/api/privacy/consent', 'POST', { notice_version: noticeVersion.value, local_storage: true, external_ai: externalAiConsent.value, retention_days: Number(retention.value) })
    emit('changed')
  })
}
async function exportData(): Promise<void> {
  await run('export', async () => {
    const data = await request<unknown>('/api/privacy/export')
    const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: 'application/json;charset=utf-8' }))
    const link = document.createElement('a'); link.href = url; link.download = '知禾-本人资料.json'; document.body.append(link); link.click(); link.remove()
    window.setTimeout(() => URL.revokeObjectURL(url), 1000); notice.value = '已导出本人资料。下载文件含自述，请妥善保管。'
  })
}
async function confirmDestructive(): Promise<void> {
  const selected = confirmAction.value
  if (!selected || (selected === 'delete' && !deletePassword.value) || (selected === 'logout' && !accountActionAllowed.value)) return
  await run(selected, async () => {
    if (selected === 'withdraw') await request('/api/privacy/consent', 'DELETE')
    else {
      const result = await request<{ csrf_token?: string } | undefined>(selected === 'delete' ? '/api/account' : '/api/account/logout', selected === 'delete' ? 'DELETE' : 'POST', selected === 'delete' ? { password: deletePassword.value } : {})
      if (result?.csrf_token) setCsrfToken(result.csrf_token)
    }
    deletePassword.value = ''; confirmAction.value = null; emit('changed', true)
  })
}
</script>

<template>
  <section class="privacy-account-panel" aria-labelledby="privacy-account-title" :aria-busy="!!action">
    <div class="privacy-account-heading"><div><span class="step-caption">你的选择 · 可以随时修改</span><h2 id="privacy-account-title">隐私与账号</h2></div><button v-if="!required && !recoveryCode" type="button" class="secondary-button" :disabled="locked" @click="emit('close')">返回工作台</button></div>
    <p v-if="required" class="privacy-gate-note">开始记录前，请先阅读并选择是否保存。未同意时不会建立就诊记录，仍可查看公开健康资料。</p>
    <p class="account-fictional-note">当前只用于虚构资料演示，不提供医师接诊。请不要填写真实患者信息。</p>
    <div v-if="error" class="privacy-feedback privacy-error" role="alert">{{ error }}</div><div v-if="notice" class="privacy-feedback" role="status">{{ notice }}</div>
    <section v-if="recoveryCode" class="recovery-code-box" aria-labelledby="recovery-title">
      <h3 id="recovery-title">{{ mode === 'recover' ? '新的恢复码，只显示这一次' : '只显示这一次的恢复码' }}</h3><p>恢复码可以重设密码。请私密保存，不要发给客服、放在截图中或公开分享。页面不会把它写入浏览器长期存储。</p>
      <code class="recovery-code">{{ recoveryCode }}</code><div class="privacy-button-row"><button type="button" class="secondary-button" @click="copyRecovery">复制恢复码</button><button type="button" class="primary-button" @click="acknowledgeRecovery">已私密保存，返回工作台</button></div>
    </section>
    <template v-else>
      <div class="privacy-account-grid">
        <form class="privacy-consent-form" @submit.prevent="consent">
          <h3>保存与外部辅助，分别选择</h3><p class="privacy-notice-text">{{ privacy?.notice_text || '隐私说明尚未读取，请重新连接后再选择保存。' }}</p><p class="privacy-version">告知版本：{{ noticeVersion || '尚未读取' }}</p>
          <label class="privacy-check"><input v-model="localStorageConsent" type="checkbox" :disabled="locked" /><span>同意将本人的健康自述保存在本机<small>填写、对话和任务结果可能包含敏感健康信息。</small></span></label>
          <label class="privacy-retention">保存期限<select v-model="retention" :disabled="locked"><option :value="1">1 天</option><option :value="7">7 天</option><option :value="30">30 天</option></select></label>
          <label class="privacy-check"><input v-model="externalAiConsent" type="checkbox" :disabled="locked" /><span>另行同意外部模型辅助<small>{{ privacy?.model_notice || '默认不发送。外部模型的数据用途说明尚未读取。' }}</small></span></label>
          <p class="privacy-small">未选择外部辅助仍可使用本机整理。该选择只是授权范围，问答和目标任务还需分别主动开启外部模型。</p>
          <button type="submit" class="primary-button privacy-consent-submit" :disabled="locked || !localStorageConsent || !noticeVersion">{{ action === 'consent' ? '正在保存选择…' : privacy?.accepted ? '更新隐私选择' : '确认选择，开始准备' }}</button>
        </form>
        <section class="privacy-account-card">
          <template v-if="account"><h3>当前账号</h3><p class="account-name">{{ account.username }}</p><span class="account-role">{{ roleLabel }}</span><p>账号用于找回本人的准备资料，不是手机号实名认证或医师执业身份。</p><button class="secondary-button account-logout" :disabled="locked" @click="confirmAction = 'logout'">退出登录</button></template>
          <template v-else><h3>换浏览器后也能找回</h3><p>使用昵称与密码建立账号。当前浏览器已有的本人记录会绑定到注册账号；登录其他账号不会自动合并访客资料。</p><nav class="account-mode-nav" aria-label="账号操作"><button v-for="item in [{ id: 'login', label: '登录' }, { id: 'register', label: '注册' }, { id: 'recover', label: '恢复账号' }]" :key="item.id" :class="{ selected: mode === item.id }" :disabled="locked" @click="mode = item.id as typeof mode">{{ item.label }}</button></nav>
            <form class="account-form" @submit.prevent="accountRequest"><label>昵称账号<input v-model="username" name="username" minlength="3" maxlength="40" pattern="[A-Za-z][A-Za-z0-9._-]{2,39}" autocomplete="username" required :disabled="locked" /><small>3–40 位，英文字母开头，仅字母、数字、点、下划线或连字符。不要用真名或手机号。</small></label><label v-if="mode === 'recover'">私密恢复码<input v-model="recoveryInput" name="recovery_code" minlength="32" maxlength="100" autocomplete="off" required :disabled="locked" /><small>填写完整恢复码。忽略首尾空格和分组连字符后，应为 64 位十六进制字符。</small></label><label>{{ mode === 'recover' ? '新密码' : '密码' }}<input v-model="password" type="password" name="password" minlength="15" maxlength="128" :autocomplete="mode === 'login' ? 'current-password' : 'new-password'" required :disabled="locked" /><small>至少 15 个字符。请使用独立密码，不要填健康信息。</small></label>
              <label v-if="dirty" class="privacy-check"><input v-model="discardDraft" type="checkbox" :disabled="locked" /><span>我已保存需要的内容，允许切换身份后清除当前未保存草稿</span></label>
              <button type="submit" class="primary-button account-submit" :disabled="locked || !validCredentials || !accountActionAllowed">{{ action === 'account' ? '正在处理…' : mode === 'register' ? '建立账号' : mode === 'recover' ? '重设密码并登录' : '登录账号' }}</button>
            </form>
          </template>
          <p class="privacy-small">手机号与微信登录尚未接入；角色不可自行选择。恢复码丢失且忘记密码时，无法证明账号归属。</p>
        </section>
      </div>
      <section class="privacy-rights"><div><h3>查看、撤回与删除</h3><p>导出文件含本人自述；请妥善保存。撤回保存同意会删除当前身份的已存资料，不会替你删除本地下载文件或其他副本。</p></div><div class="privacy-button-row"><button class="secondary-button privacy-export" :disabled="locked || !privacy?.accepted" @click="exportData">导出本人资料</button><button class="privacy-withdraw" :disabled="locked || !privacy?.accepted" @click="confirmAction = 'withdraw'">撤回保存同意</button><button v-if="account" class="account-delete" :disabled="locked" @click="confirmAction = 'delete'">注销账号</button></div></section>
      <section v-if="confirmAction" class="privacy-confirmation" role="group" aria-labelledby="privacy-confirm-title"><h3 id="privacy-confirm-title">{{ confirmAction === 'withdraw' ? '撤回并删除已存资料？' : confirmAction === 'delete' ? '永久注销这个账号？' : '退出当前账号？' }}</h3><p>{{ confirmAction === 'withdraw' ? '就诊记录、聊天、任务和收藏等当前身份资料将被删除，不能从此页面恢复。撤回后停止保存；已有下载文件需你自行处理。' : confirmAction === 'delete' ? '账号及关联就诊记录、聊天、任务、同意记录和收藏将被删除。恢复码也不能恢复注销后的账号。' : '登录资料保留在账号下。退出后切换为新的访客会话，不自动把账号资料复制给访客。' }}</p><label v-if="confirmAction === 'delete'" class="delete-password">输入当前密码确认<input v-model="deletePassword" type="password" autocomplete="current-password" :disabled="!!action" /></label><label v-if="confirmAction === 'logout' && dirty" class="privacy-check"><input v-model="discardDraft" type="checkbox" :disabled="!!action" /><span>允许退出后清除当前未保存草稿</span></label><div class="privacy-button-row"><button type="button" class="secondary-button" :disabled="!!action" @click="confirmAction = null; deletePassword = ''">取消，保留资料</button><button type="button" class="danger-button privacy-confirm-submit" :disabled="locked || (confirmAction === 'delete' && !deletePassword) || (confirmAction === 'logout' && !accountActionAllowed)" @click="confirmDestructive">{{ action ? '正在处理…' : confirmAction === 'withdraw' ? '确认撤回并删除' : confirmAction === 'delete' ? '确认注销并删除' : '确认退出' }}</button></div></section>
    </template>
  </section>
</template>
