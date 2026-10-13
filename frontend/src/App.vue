<script setup lang="ts">
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue'
import { ApiError, request, setCsrfToken } from './api'
import { copyIntake, emptyIntake, modelChoice, replyText, requiresHumanSupport, safeSourceUrl, shortDate, visitHasIntake } from './helpers'
import { intakeFields, intakeLabels, type Account, type Bootstrap, type ChecklistItem, type ComplianceInfo, type Intake, type IntakeCandidate, type MessageResult, type ObservationInput, type PreparationPlan, type PrivacyNotice, type Source, type SummaryResult, type Visit, type Workspace } from './types'
import LeafMark from './components/LeafMark.vue'
import MessageBubble from './components/MessageBubble.vue'
import OverviewPanel from './components/OverviewPanel.vue'
import TimelinePanel from './components/TimelinePanel.vue'
import LibraryPanel from './components/LibraryPanel.vue'
import PlanPanel from './components/PlanPanel.vue'
import TaskAgentPanel from './components/TaskAgentPanel.vue'
import SafetyAlert from './components/SafetyAlert.vue'
import PrivacyAccountPanel from './components/PrivacyAccountPanel.vue'
import './chat.css'

type Tab = 'overview' | 'prepare' | 'chat' | 'timeline' | 'library' | 'summary'
const tab = ref<Tab>('overview')
const taskPanelOpen = ref(false)
const navigation: { id: Tab; label: string; short: string; icon: string }[] = [
  { id: 'overview', label: '工作台总览', short: '总览', icon: '⊞' }, { id: 'prepare', label: '信息准备', short: '准备', icon: '▤' },
  { id: 'chat', label: '健康问答', short: '问答', icon: '◌' }, { id: 'timeline', label: '变化时间线', short: '记录', icon: '◷' },
  { id: 'library', label: '健康资料库', short: '资料', icon: '▧' }, { id: 'summary', label: '就诊摘要', short: '摘要', icon: '✧' },
]
const visits = ref<Visit[]>([])
const currentVisit = ref<Visit | null>(null)
const draft = reactive<Intake>(emptyIntake())
const dirty = ref(false)
const revision = ref(0)
const busy = ref('bootstrap')
const initialized = ref(false)
const account = ref<Account | null>(null)
const privacy = ref<Partial<PrivacyNotice> | null>(null)
const compliance = ref<ComplianceInfo | null>(null)
const privacyPanelOpen = ref(false)
const consentGate = computed(() => privacy.value?.consent_required === true && (privacy.value.accepted !== true || privacy.value.scopes?.local_storage !== true))
const externalAiAllowed = computed(() => !privacy.value?.scopes || privacy.value.scopes.external_ai === true)
const permittedModelAvailable = computed(() => modelAvailable.value && externalAiAllowed.value)
const error = ref('')
const notice = ref('')
const messageInput = ref('')
const pendingMessage = ref('')
const modelAvailable = ref(false)
const useModel = ref(false)
const modelNotice = ref('默认关闭。开启后，脱敏后的有限症状词、时间和程度片段会发送给 DeepSeek；不发送身份、表单、访客 Cookie 或完整对话。')
const draftLocked = computed(() => ['bootstrap', 'create', 'select', 'delete', 'clear', 'candidate'].includes(busy.value))
const workspaceData = ref<Workspace | null>(null)
const workspaceError = ref('')
const workspaceLoading = ref(false)
const knowledgeSources = ref<Source[]>([])
const libraryCategory = ref('')
const recordTitleDraft = ref('')
const pendingCandidateIds = computed(() => (currentVisit.value?.intake_candidates || []).map(candidate => candidate.id))
let workspaceSequence = 0
const conversation = ref<HTMLElement | null>(null)
const composer = ref<HTMLTextAreaElement | null>(null)
const dialog = ref<HTMLDialogElement | null>(null)
const modal = ref<{ kind: 'records' | 'delete' | 'clear' | 'rename' | 'candidate'; visitId?: string; candidate?: IntakeCandidate } | null>(null)
const completion = computed(() => intakeFields.filter(key => key !== 'notes' && draft[key].trim() && draft[key] !== '未填写').length)
const messages = computed(() => currentVisit.value?.messages || [])
const checklist = computed(() => currentVisit.value?.checklist || [])
const checklistDone = computed(() => checklist.value.filter(item => item.done).length)
const summary = computed(() => currentVisit.value?.summary?.text || '')
const risk = computed(() => currentVisit.value?.risk)
const humanSupport = computed(() => requiresHumanSupport(risk.value))
const supportNotice = computed(() => risk.value?.kind === 'self_harm' ? '先获得陪伴和支持，暂不生成摘要或准备计划。你的原始记录仍保留，可以下载，也可以继续聊。' : '当前需要立即关注安全，暂不生成摘要或准备计划。你的原始记录仍保留，可以下载。')
const currentTitle = computed(() => currentVisit.value?.title || '新的就诊准备')
const busyLabel = computed(() => ({ bootstrap: '正在连接本机服务…', create: '正在新建记录…', select: '正在打开记录…', save: '正在保存信息…', send: '正在核查提醒与查询资料…', summary: '正在整理自述摘要…', checklist: '正在更新清单…', delete: '正在删除记录…', clear: '正在清空本人的数据…', export: '正在准备下载…' }[busy.value] || '正在处理…'))
const printIntake = computed(() => [
  ['主诉', draft.chief_complaint], ['开始时间', draft.onset], ['年龄组', draft.age_group],
  ['自述严重程度', draft.severity], ['正在使用的药物', draft.medications], ['过敏情况', draft.allergies],
  ['既往病史', draft.medical_history], ['补充备注', draft.notes],
])
const starters = [
  { icon: '↗', title: '就诊前带些什么？', text: '就诊前需要准备哪些资料？', subtitle: '让第一次沟通更从容' },
  { icon: '◷', title: '怎么描述我的不适？', text: '我应该怎样向医生描述症状？', subtitle: '从开始时间到变化过程' },
  { icon: '✧', title: '了解健康资料', text: '发热时应记录哪些信息？', subtitle: '查看有来源的健康信息' },
]

function markDraft(): void { dirty.value = true; revision.value++ }
function applyVisit(visit: Visit, forceDraft = false): void {
  if (!visit || typeof visit.id !== 'string' || !visit.id.trim()) throw new ApiError('服务返回的记录不完整，请重新打开记录或稍后重试。')
  if (currentVisit.value?.id !== visit.id) useModel.value = false
  currentVisit.value = { ...visit, intake: copyIntake(visit.intake), messages: Array.isArray(visit.messages) ? visit.messages : [], checklist: Array.isArray(visit.checklist) ? visit.checklist : [], summary: visit.summary || null, risk: visit.risk || null }
  const index = visits.value.findIndex(item => item.id === visit.id)
  if (index >= 0) visits.value.splice(index, 1, currentVisit.value)
  else visits.value.unshift(currentVisit.value)
  if (forceDraft || !dirty.value) { Object.assign(draft, copyIntake(visit.intake)); dirty.value = false }
}
async function run(action: string, work: () => Promise<void>): Promise<void> {
  if (busy.value) return
  busy.value = action; error.value = ''; notice.value = ''
  try { await work(); if (initialized.value) void loadWorkspace() }
  catch (caught) { error.value = caught instanceof Error ? caught.message : '操作没有完成，请稍后重试。' }
  finally { busy.value = '' }
}
async function persistDraft(): Promise<void> {
  if (!dirty.value || !currentVisit.value) return
  const sentRevision = revision.value
  const visitId = currentVisit.value.id
  const saved = await request<Visit>(`/api/visits/${encodeURIComponent(visitId)}/intake`, 'PATCH', copyIntake(draft))
  if (currentVisit.value?.id !== visitId) return
  applyVisit(saved)
  if (revision.value === sentRevision) { Object.assign(draft, copyIntake(saved.intake)); dirty.value = false }
}
async function bootstrap(): Promise<void> {
  if (busy.value && busy.value !== 'bootstrap') return
  busy.value = 'bootstrap'; error.value = ''; notice.value = ''
  try {
    const data = await request<Bootstrap>('/api/bootstrap')
    if (!data.csrf_token) throw new ApiError('服务未提供会话校验信息，请重新连接。')
    setCsrfToken(data.csrf_token)
    account.value = data.account || null
    privacy.value = data.privacy && typeof data.privacy === 'object' ? data.privacy : null
    compliance.value = data.compliance || null
    modelAvailable.value = data.model_available === true
    if (data.privacy && typeof data.privacy === 'object' && 'model_notice' in data.privacy && typeof data.privacy.model_notice === 'string') modelNotice.value = `默认关闭。${data.privacy.model_notice}`
    useModel.value = false
    visits.value = Array.isArray(data.visits) ? data.visits : []
    knowledgeSources.value = Array.isArray(data.sources) ? data.sources : []
    initialized.value = true
    if (consentGate.value) { resetPersonalView(); return }
    const visit = data.current_visit || await request<Visit>('/api/visits', 'POST', {})
    applyVisit(visit, !dirty.value); void loadWorkspace()
  } catch (caught) { error.value = caught instanceof Error ? caught.message : '连接失败，请重试。' }
  finally { busy.value = '' }
}
function resetPersonalView(): void {
  workspaceSequence++; workspaceData.value = null; workspaceError.value = ''; workspaceLoading.value = false
  visits.value = []; currentVisit.value = null; dirty.value = false; Object.assign(draft, emptyIntake())
  messageInput.value = ''; pendingMessage.value = ''; useModel.value = false; modal.value = null; taskPanelOpen.value = false
}
async function privacyChanged(identityChanged = false): Promise<void> {
  if (identityChanged) resetPersonalView()
  privacyPanelOpen.value = false
  await bootstrap()
}
function complianceHref(value: string | null | undefined): string | null { return value?.startsWith('https://') ? safeSourceUrl(value) : null }
async function createVisit(): Promise<void> {
  if (consentGate.value || privacyPanelOpen.value) return
  await run('create', async () => { await persistDraft(); applyVisit(await request<Visit>('/api/visits', 'POST', {}), true); messageInput.value = ''; tab.value = 'prepare'; modal.value = null; notice.value = '新的就诊记录已创建。' })
}
async function selectVisit(id: string): Promise<void> {
  if (consentGate.value || privacyPanelOpen.value) return
  if (id === currentVisit.value?.id) { modal.value = null; return }
  await run('select', async () => { await persistDraft(); applyVisit(await request<Visit>(`/api/visits/${encodeURIComponent(id)}`), true); messageInput.value = ''; modal.value = null })
}
async function saveIntake(): Promise<void> {
  await run('save', async () => { await persistDraft(); notice.value = dirty.value ? '刚才的信息已保存。你又修改了表单，记得再次保存。' : '就诊信息已保存，可继续问答或生成摘要。' })
}
async function refreshVisit(fallback: Visit): Promise<void> {
  try { applyVisit(await request<Visit>(`/api/visits/${encodeURIComponent(fallback.id)}`)) }
  catch (caught) { applyVisit(fallback); error.value = `结果已生成，但记录刷新没有完成。${caught instanceof Error ? caught.message : '请稍后重试。'}` }
}
async function sendMessage(question?: string): Promise<void> {
  const content = (question ?? messageInput.value).trim()
  if (!content || !currentVisit.value || busy.value) return
  tab.value = 'chat'
  pendingMessage.value = content
  if (messageInput.value.trim() === content) messageInput.value = ''
  await run('send', async () => {
    await scrollConversation('pending')
    await persistDraft()
    const id = currentVisit.value!.id
    const result = await request<MessageResult>(`/api/visits/${encodeURIComponent(id)}/messages`, 'POST', { content, use_model: modelChoice(useModel.value, permittedModelAvailable.value) })
    if (!result.visit) throw new ApiError('服务返回的记录不完整。请重新打开当前记录。')
    await refreshVisit(result.visit)
    pendingMessage.value = ''
    if (!result.assistant_message?.content?.trim()) error.value = replyText({ content: '' })
    await scrollConversation('reply', result.assistant_message?.id)
  })
  if (pendingMessage.value && !messageInput.value.trim()) messageInput.value = content
  pendingMessage.value = ''
}
async function generateSummary(): Promise<void> {
  if (!currentVisit.value) return
  if (humanSupport.value) { notice.value = supportNotice.value; return }
  tab.value = 'summary'
  await run('summary', async () => {
    await persistDraft()
    if (humanSupport.value) throw new ApiError(supportNotice.value)
    const result = await request<SummaryResult>(`/api/visits/${encodeURIComponent(currentVisit.value!.id)}/summary`, 'POST', { use_model: modelChoice(useModel.value, permittedModelAvailable.value) })
    if (!result.visit) throw new ApiError('摘要生成结果不完整，请稍后重试。')
    await refreshVisit(result.visit)
    if (!currentVisit.value?.summary?.text?.trim()) throw new ApiError('本次没有收到有效摘要，请检查填写信息后重试。')
    notice.value = '摘要已生成。请检查自述内容，确认后再给医生查看。'
  })
}
async function toggleChecklist(item: ChecklistItem): Promise<void> {
  if (!currentVisit.value) return
  await run('checklist', async () => { applyVisit(await request<Visit>(`/api/visits/${encodeURIComponent(currentVisit.value!.id)}/checklist`, 'PATCH', { item_id: item.id, done: !item.done })) })
}
function changeChecklist(event: Event, item: ChecklistItem): void {
  // Only the confirmed server value is displayed, including when a request fails.
  ;(event.target as HTMLInputElement).checked = item.done
  void toggleChecklist(item)
}
function fillExample(): void {
  Object.assign(draft, { chief_complaint: '【虚构示例】最近两天轻微咽部不适，想整理就诊信息。', onset: '两天前开始，尚未记录具体时刻', age_group: '成人', severity: '轻', medications: '未使用药物（虚构示例）', allergies: '不清楚，待核实', medical_history: '未填写，待本人补充', notes: '此记录仅用于演示，不代表真实病情。' })
  markDraft(); notice.value = '已填入虚构示例。请勿把演示内容当作本人就诊资料。'
}
async function copySummary(): Promise<void> {
  error.value = ''; notice.value = ''
  try {
    if (!summary.value) throw new Error('请先生成摘要。')
    if (navigator.clipboard?.writeText) await navigator.clipboard.writeText('AI 辅助整理，请核对后使用；不构成诊断或处方。\n\n' + summary.value)
    else {
      const text = document.createElement('textarea'); text.value = 'AI 辅助整理，请核对后使用；不构成诊断或处方。\n\n' + summary.value; text.setAttribute('readonly', ''); text.style.position = 'fixed'; text.style.opacity = '0'; document.body.append(text); text.select()
      const success = document.execCommand('copy'); text.remove(); if (!success) throw new Error('浏览器未允许复制。你可以选中摘要文字后手动复制。')
    }
    notice.value = '摘要已复制。'
  } catch (caught) { error.value = caught instanceof Error ? caught.message : '复制没有完成，请手动选中摘要复制。' }
}
async function downloadText(): Promise<void> {
  if (!currentVisit.value) return
  await run('export', async () => {
    await persistDraft()
    const content = await request<string>(`/api/visits/${encodeURIComponent(currentVisit.value!.id)}/export`, 'GET', undefined, true)
    if (!content.trim()) throw new ApiError('没有收到可下载的内容，请稍后重试。')
    const url = URL.createObjectURL(new Blob(['\uFEFF', content], { type: 'text/plain;charset=utf-8' }))
    const link = document.createElement('a'); link.href = url; link.download = `知禾-就诊准备-${currentVisit.value!.id.slice(0, 8)}.txt`; document.body.append(link); link.click(); link.remove(); window.setTimeout(() => URL.revokeObjectURL(url), 1000)
    notice.value = '已下载当前就诊资料。'
  })
}
async function printSummary(): Promise<void> {
  if (!currentVisit.value || busy.value) return
  await run('save', async () => { await persistDraft(); await nextTick(); window.print() })
}
async function confirmDelete(): Promise<void> {
  const id = modal.value?.visitId
  if (!id) return
  await run('delete', async () => {
    await request<void>(`/api/visits/${encodeURIComponent(id)}`, 'DELETE')
    visits.value = visits.value.filter(visit => visit.id !== id)
    modal.value = null
    if (currentVisit.value?.id === id) {
      currentVisit.value = null; dirty.value = false; Object.assign(draft, emptyIntake()); messageInput.value = ''
      const next = visits.value[0] ? await request<Visit>(`/api/visits/${encodeURIComponent(visits.value[0].id)}`) : await request<Visit>('/api/visits', 'POST', {})
      applyVisit(next, true)
    }
    notice.value = '所选记录已删除。'
  })
}
async function clearData(): Promise<void> {
  if (consentGate.value || privacyPanelOpen.value) return
  await run('clear', async () => {
    await request<void>('/api/data', 'DELETE')
    visits.value = []; currentVisit.value = null; dirty.value = false; Object.assign(draft, emptyIntake()); messageInput.value = ''; useModel.value = false; modal.value = null
    applyVisit(await request<Visit>('/api/visits', 'POST', {}), true); tab.value = 'prepare'; notice.value = '本人的历史资料已清空。已打开一份空白记录。'
  })
}
async function loadWorkspace(): Promise<void> {
  if (!initialized.value || consentGate.value) return
  const sequence = ++workspaceSequence
  workspaceLoading.value = true; workspaceError.value = ''
  try {
    const data = await request<Workspace>('/api/workspace')
    if (sequence !== workspaceSequence) return
    if (!data || typeof data.visits_count !== 'number') throw new ApiError('工作台统计暂未就绪。现有就诊记录仍可正常使用。')
    workspaceData.value = data
  } catch (caught) { if (sequence === workspaceSequence) workspaceError.value = caught instanceof Error ? caught.message : '工作台没有加载完成，请重试。' }
  finally { if (sequence === workspaceSequence) workspaceLoading.value = false }
}
function openCategory(category: string): void { libraryCategory.value = category; tab.value = 'library' }
function appliedTask(visit: Visit): void { if (visit.id === currentVisit.value?.id) { applyVisit(visit); void loadWorkspace() } }
function navigateTask(section: 'prepare' | 'chat' | 'timeline'): void { taskPanelOpen.value = false; tab.value = section }
function openRename(): void { recordTitleDraft.value = currentVisit.value?.title || ''; modal.value = { kind: 'rename' } }
async function renameVisit(): Promise<void> {
  const title = recordTitleDraft.value.trim()
  if (!currentVisit.value || !title) return
  await run('rename', async () => { applyVisit(await request<Visit>(`/api/visits/${encodeURIComponent(currentVisit.value!.id)}/title`, 'PATCH', { title })); modal.value = null; notice.value = '记录标题已更新。' })
}
function reviewCandidate(candidate: IntakeCandidate): void {
  if (!pendingCandidateIds.value.includes(candidate.id)) { error.value = '这项候选已处理或失效，请查看最新就诊信息。'; return }
  modal.value = { kind: 'candidate', candidate }
}
async function confirmCandidate(): Promise<void> {
  const candidate = modal.value?.candidate
  if (!candidate || !currentVisit.value) return
  const hadDraft = dirty.value
  const sentRevision = revision.value
  await run('candidate', async () => {
    const updated = await request<Visit>(`/api/visits/${encodeURIComponent(currentVisit.value!.id)}/candidates/${encodeURIComponent(candidate.id)}/confirm`, 'POST', {})
    applyVisit(updated)
    if (hadDraft && revision.value === sentRevision) { draft[candidate.field] = copyIntake(updated.intake)[candidate.field]; markDraft() }
    modal.value = null
    notice.value = hadDraft ? '这一项已确认。其余未保存草稿仍保留，请核对后保存就诊信息。' : '这一项已由你确认并写入就诊信息。'
  })
}
async function createObservation(input: ObservationInput): Promise<void> {
  if (!currentVisit.value) return
  await run('observation', async () => { applyVisit(await request<Visit>(`/api/visits/${encodeURIComponent(currentVisit.value!.id)}/observations`, 'POST', input)); notice.value = '本人记录已添加到时间线。数值未作医学解释。' })
}
async function deleteObservation(id: string): Promise<void> {
  if (!currentVisit.value) return
  await run('observation', async () => { applyVisit(await request<Visit>(`/api/visits/${encodeURIComponent(currentVisit.value!.id)}/observations/${encodeURIComponent(id)}`, 'DELETE')); notice.value = '这条时间线记录已删除。' })
}
async function generatePlan(): Promise<void> {
  if (!currentVisit.value) return
  if (humanSupport.value) { notice.value = supportNotice.value; return }
  await run('plan', async () => {
    await persistDraft()
    if (humanSupport.value) throw new ApiError(supportNotice.value)
    const result = await request<{ plan: PreparationPlan; visit: Visit }>(`/api/visits/${encodeURIComponent(currentVisit.value!.id)}/plan`, 'POST', {})
    if (!result.plan || !result.visit) throw new ApiError('准备计划没有生成完整，请重试。')
    await refreshVisit(result.visit); notice.value = '就诊沟通与准备计划已更新，请核对后使用。'
  })
}
async function scrollConversation(target: 'pending' | 'reply', messageId?: string): Promise<void> {
  await nextTick()
  const container = conversation.value
  if (!container) return
  const replies = Array.from(container.querySelectorAll<HTMLElement>('.message-assistant'))
  const anchor = target === 'pending' ? container.querySelector<HTMLElement>('.message-pending') : replies.find(reply => reply.dataset.messageId === messageId) || replies.at(-1)
  if (!anchor) return
  const top = anchor.getBoundingClientRect().top - container.getBoundingClientRect().top + container.scrollTop - 18
  container.scrollTo({ top: Math.max(0, top), behavior: 'smooth' })
}
function focusComposer(): void { tab.value = 'chat'; nextTick(() => composer.value?.focus()) }
function composerKey(event: KeyboardEvent): void { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); void sendMessage() } }
function closeModal(): void { if (!busy.value) modal.value = null }
watch(modal, async value => { await nextTick(); if (value && !dialog.value?.open) dialog.value?.showModal(); if (!value && dialog.value?.open) dialog.value?.close() })
watch(tab, () => { taskPanelOpen.value = false })
onMounted(bootstrap)
</script>

<template>
  <div class="app-shell">
    <aside class="sidebar" aria-label="就诊记录与设置">
      <a class="brand" href="#main" aria-label="知禾，就诊准备"><span class="brand-symbol"><LeafMark /></span><span><strong>知禾</strong><small>就诊准备</small></span></a>
      <button class="new-visit" :disabled="!!busy || !initialized || consentGate || privacyPanelOpen" @click="createVisit"><span aria-hidden="true">＋</span> 新的就诊准备</button>
      <nav class="sidebar-main-nav" aria-label="工作台主导航"><button v-for="item in navigation" :key="item.id" :class="{ selected: tab === item.id }" :aria-current="tab === item.id ? 'page' : undefined" @click="tab = item.id"><span aria-hidden="true">{{ item.icon }}</span>{{ item.label }}<i v-if="tab === item.id" aria-hidden="true"></i></button></nav><div class="sidebar-section-title"><span>我的就诊记录</span><span>{{ visits.length }}</span></div>
      <nav class="visit-list" aria-label="本人就诊记录">
        <div v-for="visit in visits" :key="visit.id" :class="['visit-item', { active: visit.id === currentVisit?.id }]">
          <button class="visit-select" :disabled="!!busy" :aria-current="visit.id === currentVisit?.id ? 'page' : undefined" @click="selectVisit(visit.id)"><span class="record-icon" aria-hidden="true">▤</span><span><strong>{{ visit.title || '新的就诊准备' }}</strong><small>{{ shortDate(visit.updated_at || visit.created_at) }} · {{ visit.summary ? '已有摘要' : visitHasIntake(visit) ? '信息已填写' : '待整理' }}</small></span></button>
          <button class="delete-record" :disabled="!!busy" :aria-label="`删除记录：${visit.title || '新的就诊准备'}`" @click="modal = { kind: 'delete', visitId: visit.id }">×</button>
        </div>
        <p v-if="!visits.length" class="sidebar-empty">{{ consentGate ? '选择保存后，可建立本人记录。' : '尚无本人记录。' }}</p>
      </nav>
      <div class="sidebar-bottom">
        <div class="privacy-mini"><span class="shield-symbol" aria-hidden="true">◇</span><div><strong>属于你的私密准备</strong><p>{{ account ? '已绑定昵称账号，可使用密码找回。' : '当前为访客，可在设置中建立账号。' }}<br />请只用虚构资料演示。</p></div></div>
        <button class="clear-data" :disabled="!!busy || !initialized || consentGate || privacyPanelOpen" @click="modal = { kind: 'clear' }"><span aria-hidden="true">⌫</span> 清空本人全部资料</button>
        <button class="privacy-settings-link" :disabled="!!busy" @click="privacyPanelOpen = true">隐私与账号设置</button><span class="sidebar-note">每次就诊，都多一分从容。</span>
      </div>
    </aside>

    <div class="workspace">
      <header class="topbar"><span class="topbar-path">我的健康空间 <span aria-hidden="true">/</span> <strong>就诊准备</strong></span><a class="mobile-brand" href="#main"><LeafMark /><strong>知禾</strong></a><div class="topbar-actions"><span class="local-status"><span aria-hidden="true"></span>{{ initialized ? '本机服务已连接' : '等待连接' }}</span><button class="mobile-records" :disabled="!!busy || !initialized" @click="modal = { kind: 'records' }">我的记录</button><button class="visitor-badge privacy-open-button" :aria-label="account ? '打开隐私与账号：' + account.username : '打开隐私与账号：匿名访客'" :disabled="!!busy" @click="privacyPanelOpen = true">{{ account ? account.username.slice(0, 1).toUpperCase() : '访' }}</button></div></header>

      <main id="main" :class="['main-content', { 'chat-active': tab === 'chat' }]">
        <section class="page-heading"><div><div class="eyebrow"><span aria-hidden="true"></span> 把健康沟通，准备得更好</div><h1>{{ tab === 'chat' ? '先聊聊，你想了解什么？' : '就诊之前，先把思绪理清。' }}</h1><p>整理信息，帮助与医生沟通；不提供诊断或用药建议。</p></div><div class="heading-art" aria-hidden="true"><div class="art-orbit"></div><LeafMark /><span class="art-spark">✦</span><span class="art-dot"></span></div></section>

        <div v-if="error" class="feedback feedback-error" role="alert"><span aria-hidden="true">!</span><p>{{ error }}</p><button v-if="!initialized" :disabled="!!busy" @click="bootstrap">重新连接</button><button v-else aria-label="关闭错误提示" @click="error = ''">×</button></div>
        <div v-if="notice" class="feedback feedback-success" role="status"><span aria-hidden="true">✓</span><p>{{ notice }}</p><button aria-label="关闭提示" @click="notice = ''">×</button></div>
        <PrivacyAccountPanel v-if="initialized && (privacyPanelOpen || consentGate)" :account="account" :privacy="privacy" :required="consentGate" :busy="!!busy" :dirty="dirty" @changed="privacyChanged" @close="privacyPanelOpen = false" /><SafetyAlert v-if="risk && risk.level !== 'unknown' && risk.message" :risk="risk" />

        <div v-if="(!privacyPanelOpen && !consentGate) || tab === 'library'" class="work-grid">
          <section class="main-panel" :aria-busy="!!busy">
            <div class="panel-top"><nav class="desktop-tabs" aria-label="工作区"><button v-for="item in navigation" :key="item.id" :data-tab="item.id" :class="{ selected: tab === item.id }" :aria-current="tab === item.id ? 'page' : undefined" @click="tab = item.id"><span aria-hidden="true">{{ item.icon }}</span>{{ item.label }}</button></nav></div>

            <div v-if="!taskPanelOpen && (tab === 'overview' || tab === 'chat')" class="task-agent-entry"><span>选定目标，查看计划与实际工具执行</span><button :disabled="!!busy || !currentVisit" @click="taskPanelOpen = true"><span aria-hidden="true">◇</span> 运行准备任务</button></div>
            <TaskAgentPanel v-if="taskPanelOpen && currentVisit" :visit-id="currentVisit.id" :model-available="permittedModelAvailable" :dirty="dirty" :busy="!!busy" :current-risk="risk" @close="taskPanelOpen = false" @save-draft="saveIntake" @applied="appliedTask" @navigate="navigateTask" /><OverviewPanel v-else-if="tab === 'overview'" :visit="currentVisit" :workspace="workspaceData" :error="workspaceError" :loading="workspaceLoading" :sources="knowledgeSources" :completion="completion" :busy="!!busy" @navigate="tab = $event" @select="selectVisit" @category="openCategory" @refresh="loadWorkspace" @rename="openRename" /><TimelinePanel v-else-if="tab === 'timeline'" :observations="currentVisit?.observations || []" :busy="!!busy" :available="!!currentVisit" @create="createObservation" @delete="deleteObservation" /><LibraryPanel v-else-if="tab === 'library'" :initial-category="libraryCategory" :available="initialized" :allow-saving="!consentGate && (privacy?.accepted !== false)" @changed="loadWorkspace" /><div v-else-if="tab === 'prepare'" class="prepare-panel">
              <div class="section-heading"><div><span class="step-caption">STEP 01 · 把感受写下来</span><h2>先从你知道的信息开始</h2><p>不确定的地方可以留空，医生会帮助你进一步确认。</p></div><button class="example-button" :disabled="!!busy || !currentVisit" @click="fillExample">填入虚构示例</button></div>
              <form class="intake-form" @submit.prevent="saveIntake">
                <label class="field field-full"><span>主要不适 / 就诊原因</span><textarea v-model="draft.chief_complaint" rows="3" maxlength="2000" placeholder="例如：哪里不舒服？最希望医生帮你解决什么？" :disabled="!currentVisit || draftLocked" @input="markDraft"></textarea><small>用自己的话描述即可，不必先判断是什么疾病。</small></label>
                <label class="field field-full"><span>什么时候开始的？</span><input v-model="draft.onset" maxlength="500" placeholder="例如：两天前开始；间断出现；是否有变化" :disabled="!currentVisit || draftLocked" @input="markDraft" /></label>
                <label class="field"><span id="age-group-label">年龄组</span><select aria-labelledby="age-group-label" v-model="draft.age_group" :disabled="!currentVisit || draftLocked" @change="markDraft"><option>未填写</option><option>成人</option><option>儿童</option><option>老年人</option><option>孕产期</option></select></label>
                <label class="field"><span id="severity-label">你感受到的严重程度</span><select aria-labelledby="severity-label" v-model="draft.severity" :disabled="!currentVisit || draftLocked" @change="markDraft"><option>未填写</option><option>轻</option><option>中</option><option>重</option></select></label>
                <label class="field field-full"><span>正在使用的药物</span><input v-model="draft.medications" maxlength="2000" placeholder="可记录已有药物名称；没有或不清楚也可以说明" :disabled="!currentVisit || draftLocked" @input="markDraft" /></label>
                <label class="field field-full"><span>过敏情况</span><input v-model="draft.allergies" maxlength="2000" placeholder="已知过敏情况；不清楚请写“不清楚”" :disabled="!currentVisit || draftLocked" @input="markDraft" /></label>
                <label class="field field-full"><span>既往病史</span><textarea v-model="draft.medical_history" rows="2" maxlength="2000" placeholder="已由医生确认的病史、手术史等" :disabled="!currentVisit || draftLocked" @input="markDraft"></textarea></label>
                <label class="field field-full"><span>补充备注 <small>可选</small></span><textarea v-model="draft.notes" rows="2" maxlength="3000" placeholder="已有检查、生活变化，或想向医生提出的问题" :disabled="!currentVisit || draftLocked" @input="markDraft"></textarea></label>
                <div class="form-footer"><p><span aria-hidden="true">◇</span> {{ dirty ? '有未保存的修改' : '只填写与你这次就诊有关的信息' }}</p><button class="primary-button" :disabled="!!busy || !currentVisit || !dirty" type="submit">{{ busy === 'save' ? '保存中…' : '保存就诊信息' }} <span aria-hidden="true">↗</span></button></div>
              </form>
            </div>

            <div v-else-if="tab === 'chat'" class="chat-panel">
              <div class="chat-context"><span>{{ currentTitle }}</span><button :disabled="!!busy || !currentVisit" @click="tab = 'prepare'">就诊信息 <span v-if="dirty">· 有草稿</span><span aria-hidden="true"> ↗</span></button></div>
              <div ref="conversation" class="conversation" aria-label="健康问答记录" tabindex="0">
                <div v-if="!messages.length && !pendingMessage" class="welcome"><div class="welcome-mark"><LeafMark /></div><h2>你好，我是知禾。</h2><p>说说你现在最想问的事，我们从这里聊起。</p><div class="starter-grid"><button v-for="starter in starters" :key="starter.title" :disabled="!!busy || !currentVisit" @click="sendMessage(starter.text)"><span class="starter-icon" aria-hidden="true">{{ starter.icon }}</span><strong>{{ starter.title }}</strong><span class="starter-arrow" aria-hidden="true">↗</span></button></div></div>
                <MessageBubble v-for="message in messages" :key="message.id" :message="message" :disabled="!!busy" :pending-candidate-ids="pendingCandidateIds" @ask="sendMessage" @confirm-candidate="reviewCandidate" />
                <article v-if="pendingMessage && busy === 'send'" class="message message-user message-pending"><div class="message-body"><div class="message-label">你</div><div class="message-content">{{ pendingMessage }}</div></div></article>
                <div v-if="busy === 'send'" class="working-message" role="status"><LeafMark /><span class="working-dots" aria-hidden="true"><i></i><i></i><i></i></span><p>正在回复…<small>核查安全提醒和相关资料</small></p></div>
              </div>
              <form class="composer" @submit.prevent="sendMessage()"><label for="question" class="sr-only">描述不适或提出健康问题</label><textarea id="question" ref="composer" v-model="messageInput" rows="2" maxlength="4000" :disabled="!currentVisit" placeholder="描述你的不适，或问问就诊前需要准备什么…" @keydown="composerKey"></textarea><div class="composer-bottom"><span><span class="composer-lock" aria-hidden="true">◇</span> {{ useModel && modelAvailable ? '已选择 DeepSeek 辅助' : '默认在本机处理' }}<span class="enter-tip"> · Enter 发送，Shift + Enter 换行</span></span><button class="send-button" type="submit" :disabled="!!busy || !currentVisit || !messageInput.trim()" aria-label="发送问题"><span aria-hidden="true">↑</span></button></div></form>
              <p class="composer-note">避免填写姓名、电话、证件号或地址。回答仅用于健康教育与就诊准备。</p>
            </div>

            <div v-else class="summary-panel"><p v-if="humanSupport" class="summary-support-note" role="status">{{ supportNotice }}</p><div class="section-heading"><div><span class="step-caption">STEP 03 · 带着清楚的信息出发</span><h2>给医生看的自述摘要</h2><p>忠实整理你的填写和自述，请核对后使用。</p></div><span class="self-report-tag">本人自述</span></div><div v-if="summary" class="summary-document"><div class="document-heading"><LeafMark /><span>知禾 · 就诊准备记录</span><small v-if="currentVisit?.summary?.generated_at">{{ shortDate(currentVisit.summary.generated_at) }}</small></div><div class="summary-text">{{ summary }}</div><div class="document-footer">AI 辅助整理 · 来源：本人填写与自述 · 未填写信息不作推测<br />本摘要不构成诊断、处方或医疗建议。</div></div><div v-else class="summary-empty"><div class="empty-document" aria-hidden="true"><span></span><span></span><span></span><i>✦</i></div><h3>让医生更快了解你的情况</h3><p>把已填写的信息和对话中的自述整理成一页摘要。<br />不确定或未填写的内容会明确标记。</p><button class="primary-button" :disabled="!!busy || !currentVisit || humanSupport" @click="generateSummary">{{ busy === 'summary' ? '正在生成…' : '生成就诊摘要' }} <span aria-hidden="true">✧</span></button></div><div class="summary-actions"><button v-if="summary" class="primary-button" :disabled="!!busy || !currentVisit || humanSupport" @click="generateSummary">{{ busy === 'summary' ? '正在整理…' : '重新生成摘要' }}</button><button class="secondary-button" :disabled="!!busy || !summary" @click="copySummary">复制摘要</button><button class="secondary-button" :disabled="!!busy || !currentVisit" @click="downloadText">下载 TXT</button><button class="secondary-button" :disabled="!!busy || !currentVisit" @click="printSummary">打印</button></div><p class="summary-help">修改就诊信息后，可重新生成摘要。没有摘要时，下载或打印会包含当前已填写资料。</p><PlanPanel :plan="currentVisit?.plan || null" :busy="!!busy" :available="!!currentVisit && !humanSupport" @generate="generatePlan" /></div>
            <div v-if="busy && busy !== 'send'" class="operation-status" role="status"><span class="spinner" aria-hidden="true"></span>{{ busyLabel }}</div>
          </section>

          <aside class="preparation-column" aria-label="就诊准备概览">
            <section class="preparation-card"><div class="card-heading"><span class="card-title-icon" aria-hidden="true">▤</span><h2>这次就诊的准备</h2><span class="card-live-dot" aria-hidden="true"></span></div><p class="current-record-title">{{ currentTitle }}</p><div class="completion-row"><span>核心信息填写进度</span><strong>{{ completion }}<small> / 7</small></strong></div><div class="completion-track" role="progressbar" :aria-valuenow="completion" :aria-valuemin="0" :aria-valuemax="7" aria-label="就诊信息填写进度"><span :style="{ width: `${completion / 7 * 100}%` }"></span></div><div class="information-preview"><div><span>主要不适</span><p>{{ draft.chief_complaint || '等待你的描述' }}</p></div><div><span>开始时间</span><p>{{ draft.onset || '尚未填写' }}</p></div></div><button class="card-text-button" :disabled="!!busy || !currentVisit" @click="tab = 'prepare'">{{ dirty ? '继续编辑 · 有未保存修改' : '补充就诊信息' }} <span aria-hidden="true">→</span></button><div class="card-divider"></div><div class="checklist-heading"><h3>随身准备清单</h3><span>{{ checklist.length ? `${checklistDone}/${checklist.length}` : '待生成' }}</span></div><ul v-if="checklist.length" class="checklist"><li v-for="item in checklist" :key="item.id"><label :class="{ checked: item.done }"><input type="checkbox" :checked="item.done" :disabled="!!busy" @change="changeChecklist($event, item)" /><span>{{ item.text }}</span></label></li></ul><div v-else class="checklist-empty"><span aria-hidden="true">☑</span><p>生成摘要后，专属准备清单<br />会出现在这里。</p></div><button class="generate-button" :disabled="!!busy || !currentVisit || humanSupport" @click="generateSummary"><span aria-hidden="true">✧</span> {{ busy === 'summary' ? '正在整理摘要…' : summary ? '更新就诊摘要' : '一键生成就诊摘要' }} <span aria-hidden="true">→</span></button><small class="card-caption">{{ humanSupport ? supportNotice : '整理你的自述，保留未知与待确认的信息。' }}</small></section>

            <section class="model-card"><div class="model-heading"><div><span aria-hidden="true">◇</span><strong>外部模型辅助</strong></div><button class="toggle-switch" type="button" role="switch" :aria-checked="useModel" aria-label="允许将脱敏的有限症状片段发送给 DeepSeek" :disabled="!permittedModelAvailable || !!busy || consentGate" @click="useModel = !useModel"><span></span></button></div><p v-if="modelAvailable && !externalAiAllowed">你尚未单独同意外部模型处理。可在隐私与账号设置中阅读说明并选择；未同意仍可使用本机整理。</p><p v-else-if="modelAvailable">{{ modelNotice }}</p><p v-else>本机资料整理可直接使用。DeepSeek 尚未配置，外部模型开关已禁用。</p><span class="privacy-state">{{ useModel && modelAvailable ? '已允许本次记录的外部辅助' : '当前：本机处理' }}</span></section>

            <section class="gentle-reminder"><span class="reminder-icon" aria-hidden="true">✳</span><div><h3>把安全放在前面</h3><p>如有严重呼吸困难、胸痛伴冷汗、突发单侧无力或意识异常，请立即联系当地急救，别等待在线答复。</p><p>未触发提醒也不代表安全。本工具不能排除紧急情况或疾病。</p><button :disabled="!!busy || !currentVisit" @click="focusComposer">了解就诊准备 <span aria-hidden="true">↗</span></button></div></section>
            <p class="right-footnote">信息有序，心里有底。<span aria-hidden="true">✦</span></p>
          </aside>
        </div>
        <footer class="main-footer"><span>知禾 · 让每一次健康沟通更清楚</span><span>健康教育与就诊准备工具</span></footer><section v-if="compliance" class="compliance-footer" aria-label="公开服务信息"><p>运营主体：{{ compliance.operator?.name || '未配置' }} · 公开服务状态：{{ compliance.public_service_status === 'unconfigured' ? '待配置与审核' : '以运营主体公布的信息为准' }}</p><p v-for="record in compliance.records || []" :key="record.type"><span>{{ record.label }}：{{ record.number || '未配置' }}<template v-if="record.model_name"> · 模型名称：{{ record.model_name }}（按配置）</template></span><a v-if="record.number && complianceHref(record.url)" :href="complianceHref(record.url)!" target="_blank" rel="noopener noreferrer">查看公示</a></p><p>配置编号不代表本项目已取得医疗资格或完成合规审验。</p><a v-if="complianceHref(compliance.operator?.privacy_url)" :href="complianceHref(compliance.operator?.privacy_url)!" target="_blank" rel="noopener noreferrer">隐私说明</a><button :disabled="!!busy" @click="privacyPanelOpen = true">隐私与账号设置</button></section>
      </main>
    </div>

    <nav class="mobile-tabs" aria-label="移动端工作区"><button v-for="item in navigation" :key="item.id" :class="{ selected: tab === item.id }" :aria-current="tab === item.id ? 'page' : undefined" @click="tab = item.id"><span aria-hidden="true">{{ item.icon }}</span>{{ item.short }}</button></nav>

    <dialog ref="dialog" class="modal-dialog" :aria-labelledby="modal?.kind === 'records' ? 'records-title' : 'confirm-title'" @cancel.prevent="closeModal" @click="($event.target === dialog) && closeModal()">
      <div v-if="modal?.kind === 'records'" class="modal-content"><div class="modal-heading"><h2 id="records-title">我的就诊记录</h2><button class="close-modal" :disabled="!!busy" aria-label="关闭记录列表" @click="closeModal">×</button></div><button class="primary-button modal-new" :disabled="!!busy" @click="createVisit">＋ 新的就诊准备</button><div class="modal-record-list"><div v-for="visit in visits" :key="visit.id" :class="['visit-item', { active: visit.id === currentVisit?.id }]"><button class="visit-select" :disabled="!!busy" @click="selectVisit(visit.id)"><span class="record-icon" aria-hidden="true">▤</span><span><strong>{{ visit.title || '新的就诊准备' }}</strong><small>{{ shortDate(visit.updated_at || visit.created_at) }}</small></span></button><button class="delete-record" :disabled="!!busy" :aria-label="`删除记录：${visit.title}`" @click="modal = { kind: 'delete', visitId: visit.id }">×</button></div></div><button class="clear-data" :disabled="!!busy" @click="modal = { kind: 'clear' }">清空本人全部资料</button></div>
      <form v-else-if="modal?.kind === 'rename'" class="modal-content" @submit.prevent="renameVisit"><div class="modal-heading"><h2 id="confirm-title">给这次准备取个标题</h2><button type="button" class="close-modal" :disabled="!!busy" aria-label="关闭标题修改" @click="closeModal">×</button></div><label class="field"><span>记录标题</span><input v-model="recordTitleDraft" maxlength="100" required :disabled="!!busy" placeholder="例如：虚构示例 · 咳嗽就诊准备" /></label><p>使用能帮助你区分记录的标题，请勿填写姓名等身份信息。</p><div v-if="error" class="modal-error" role="alert">{{ error }}</div><div class="modal-actions"><button type="button" class="secondary-button" :disabled="!!busy" @click="closeModal">取消</button><button type="submit" class="primary-button" :disabled="!!busy || !recordTitleDraft.trim()">保存标题</button></div></form>
      <div v-else-if="modal?.kind === 'candidate' && modal.candidate" class="modal-content"><div class="modal-heading"><h2 id="confirm-title">确认这项本人自述</h2><button class="close-modal" :disabled="!!busy" aria-label="关闭候选核对" @click="closeModal">×</button></div><span class="candidate-state">待本人确认</span><h3 class="candidate-review-label">{{ intakeLabels[modal.candidate.field] }}</h3><p class="candidate-review-value">{{ modal.candidate.value }}</p><blockquote class="candidate-evidence">{{ modal.candidate.evidence }}</blockquote><p>以上为依据自述提出的候选，不是已核实的医学结论。</p><div v-if="draft[modal.candidate.field] && draft[modal.candidate.field] !== '未填写'" class="candidate-existing"><strong>当前表单这一项</strong><p>{{ draft[modal.candidate.field] }}</p></div><p v-if="dirty" class="candidate-draft-warning">你有未保存的草稿。确认后仅替换上面这一项，其余草稿保留；请核对后再保存。</p><p v-else>确认后会把这一项写入就诊信息。请先检查是否准确。</p><div v-if="error" class="modal-error" role="alert">{{ error }}</div><div class="modal-actions"><button class="secondary-button" :disabled="!!busy" @click="closeModal">暂不确认</button><button class="primary-button" :disabled="!!busy" @click="confirmCandidate">确认写入这一项</button></div></div>
      <div v-else-if="modal" class="modal-content"><div class="confirmation-symbol" aria-hidden="true">⌫</div><h2 id="confirm-title">{{ modal.kind === 'clear' ? '清空本人的全部资料？' : '删除这份就诊记录？' }}</h2><p>{{ modal.kind === 'clear' ? '当前访客的全部就诊记录、对话、摘要与清单都会被删除。删除后无法恢复。' : '这份记录中的填写信息、对话、摘要与清单都会被删除。删除后无法恢复。' }}</p><div v-if="error" class="modal-error" role="alert">{{ error }}</div><div class="modal-actions"><button class="secondary-button" :disabled="!!busy" autofocus @click="closeModal">保留资料</button><button class="danger-button" :disabled="!!busy" @click="modal.kind === 'clear' ? clearData() : confirmDelete()">{{ busy ? '正在删除…' : modal.kind === 'clear' ? '确认清空' : '确认删除' }}</button></div></div>
    </dialog>

    <section class="print-only"><h1>知禾 · 就诊准备记录</h1><p>以下为本人填写与自述，不构成诊断、处方或医疗建议。</p><div v-if="summary" class="summary-text">{{ summary }}</div><dl v-else><template v-for="[label, value] in printIntake" :key="label"><dt>{{ label }}</dt><dd>{{ value?.trim() || '未填写' }}</dd></template></dl><div v-if="checklist.length"><h2>就诊准备清单</h2><p v-for="item in checklist" :key="item.id">{{ item.done ? '☑' : '☐' }} {{ item.text }}</p></div><div v-if="currentVisit?.plan"><h2>就诊沟通与准备计划</h2><p>{{ currentVisit.plan.goal }}</p><p v-for="question in currentVisit.plan.questions" :key="question">· {{ question }}</p><p v-for="step in currentVisit.plan.steps" :key="step.id">{{ step.title }}：{{ step.detail }}</p><p>{{ currentVisit.plan.notice }}</p></div><p v-if="risk?.message">就诊提醒：{{ risk.message }}</p></section>
  </div>
</template>

<style scoped>
.task-agent-entry{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:10px 25px;border-bottom:1px solid #edf0e6;background:#fcfdf8}
.task-agent-entry>span{font-size:11px;color:#98a98a;line-height:1.7;min-width:0}
.task-agent-entry>button{background:#f0f5e7;color:#718e5e;border:1px solid #e0e9d3;border-radius:7px;padding:6px 10px;min-height:32px;font-size:11px;white-space:nowrap;flex-shrink:0}
.task-agent-entry>button>span{font-size:14px;margin-right:4px}
@media(max-width:760px){.task-agent-entry{padding:9px 15px;gap:10px}.task-agent-entry>span{font-size:10px;max-width:160px}.task-agent-entry>button{font-size:11px;min-height:35px;padding:6px 8px}}
</style>
