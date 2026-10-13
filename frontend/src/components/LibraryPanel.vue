<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { ApiError, request } from '../api'
import { safeSourceUrl } from '../helpers'
import type { EvidenceResult, LibraryResult, Retrieval, Source } from '../types'
const props = withDefaults(defineProps<{ initialCategory?: string; available: boolean; allowSaving?: boolean }>(), { initialCategory: '', allowSaving: true })
const emit = defineEmits<{ changed: [] }>()
const query = ref('')
const category = ref(props.initialCategory)
const onlySaved = ref(false)
const items = ref<Source[]>([])
const categories = ref<string[]>([])
const bookmarkIds = ref<string[]>([])
const total = ref(0)
const retrieval = ref<Retrieval | null>(null)
const loading = ref(false)
const error = ref('')
const savingId = ref('')
const sourceDialog = ref<HTMLDialogElement | null>(null)
const sourceDetail = ref<Source | null>(null)
const detailLoading = ref(false)
const detailError = ref('')
const evidence = ref<EvidenceResult | null>(null)
const evidenceLoading = ref(false)
const evidenceError = ref('')
let querySequence = 0
let detailSequence = 0
const detailHref = computed(() => safeSourceUrl(sourceDetail.value?.url))
const retrievalLabel = computed(() => retrieval.value?.embedding_available ? '本机语义辅助检索' : '本机主题 / 词法检索')
async function load(): Promise<void> {
  if (!props.available) return
  const sequence = ++querySequence
  loading.value = true; error.value = ''; evidence.value = null; evidenceError.value = ''
  try {
    const params = new URLSearchParams({ q: query.value.trim(), category: category.value, bookmarked: String(onlySaved.value) })
    const data = await request<LibraryResult>(`/api/library?${params}`)
    if (sequence !== querySequence) return
    if (!Array.isArray(data.items)) throw new ApiError('资料库返回的内容不完整，请重新加载。')
    items.value = data.items; categories.value = data.categories || []; bookmarkIds.value = data.bookmark_ids || []; total.value = data.total; retrieval.value = data.retrieval || null
  } catch (caught) { if (sequence === querySequence) { items.value = []; error.value = caught instanceof Error ? caught.message : '资料没有加载完成，请重试。' } }
  finally { if (sequence === querySequence) loading.value = false }
}
async function bookmark(source: Source): Promise<void> {
  if (savingId.value || !props.allowSaving) return
  savingId.value = source.id; error.value = ''; detailError.value = ''
  try {
    const result = await request<{ bookmark_ids: string[] }>(`/api/library/bookmarks/${encodeURIComponent(source.id)}`, 'PUT', { saved: !bookmarkIds.value.includes(source.id) })
    if (!Array.isArray(result.bookmark_ids)) throw new ApiError('收藏结果不完整，请稍后重试。')
    bookmarkIds.value = result.bookmark_ids
    emit('changed')
    if (onlySaved.value) await load()
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : '收藏没有更新，请重试。'
    if (sourceDialog.value?.open) detailError.value = error.value
  }
  finally { savingId.value = '' }
}
async function openDetail(source: Source): Promise<void> {
  const sequence = ++detailSequence
  sourceDetail.value = null; detailError.value = ''; detailLoading.value = true
  await nextTick(); if (!sourceDialog.value?.open) sourceDialog.value?.showModal()
  try {
    const result = await request<Source>(`/api/library/${encodeURIComponent(source.id)}`)
    if (sequence !== detailSequence) return
    if (!result.id || typeof result.content !== 'string') throw new ApiError('资料详情不完整，请稍后重试。')
    sourceDetail.value = result
  } catch (caught) { if (sequence === detailSequence) detailError.value = caught instanceof Error ? caught.message : '资料详情没有加载完成。' }
  finally { if (sequence === detailSequence) detailLoading.value = false }
}
function closeDetail(): void { detailSequence++; detailLoading.value = false; sourceDialog.value?.close() }
async function explainRetrieval(): Promise<void> {
  if (!query.value.trim() || evidenceLoading.value) return
  evidenceLoading.value = true; evidenceError.value = ''
  try { evidence.value = await request<EvidenceResult>(`/api/library/evidence?${new URLSearchParams({ q: query.value.trim() })}`) }
  catch (caught) { evidenceError.value = caught instanceof Error ? caught.message : '无法读取检索依据。' }
  finally { evidenceLoading.value = false }
}
watch(() => props.initialCategory, value => { category.value = value; void load() })
watch(() => props.available, value => { if (value) void load() })
onMounted(load)
</script>

<template>
  <div class="library-panel v2-panel"><div class="section-heading"><div><span class="step-caption">有来源的教育资料 · 本机搜索</span><h2>查阅资料，准备更好的问题</h2><p>查看整理内容和权威来源，收藏与本次就诊有关的资料。医疗专业审核尚未完成。</p></div><span class="self-report-tag">{{ retrievalLabel }}</span></div>
    <form class="library-search" @submit.prevent="load"><label class="sr-only" for="library-query">搜索本机健康资料</label><span aria-hidden="true">⌕</span><input id="library-query" v-model="query" maxlength="500" placeholder="搜索主题、症状词或就诊准备问题…" :disabled="!available" /><button class="primary-button" :disabled="loading || !available" type="submit">{{ loading ? '搜索中…' : '搜索资料' }}</button></form>
    <div class="library-filters"><label><span>主题分类</span><select v-model="category" aria-label="资料主题分类" :disabled="loading || !available" @change="load"><option value="">全部分类</option><option v-for="item in categories" :key="item" :value="item">{{ item }}</option></select></label><button :class="['saved-filter', { active: onlySaved }]" :aria-pressed="onlySaved" :disabled="loading || !available || !allowSaving" @click="onlySaved = !onlySaved; load()">{{ onlySaved ? '★' : '☆' }} 只看收藏</button><span class="library-count">{{ error ? '加载未完成' : `${total} 条资料` }}</span></div>
    <div v-if="error" class="inline-error" role="alert"><p>{{ error }}</p><button :disabled="loading" @click="load">重试</button></div><p v-if="loading" class="v2-loading" role="status"><span class="spinner" aria-hidden="true"></span>正在查询本机资料…</p>
    <div v-else-if="!items.length && !error" class="library-empty"><span aria-hidden="true">⌕</span><h3>{{ onlySaved ? '当前还没有匹配的收藏' : '没有找到匹配资料' }}</h3><p>可以调整主题词或分类。资料未覆盖的问题，不会用无关内容拼凑答案。</p></div>
    <div v-else class="library-grid"><article v-for="source in items" :key="source.id" class="library-item"><div class="library-item-top"><span>{{ source.category || '健康教育' }}</span><button :disabled="!!savingId || !allowSaving" :aria-label="`${bookmarkIds.includes(source.id) ? '取消收藏' : '收藏'}：${source.title}`" :aria-pressed="bookmarkIds.includes(source.id)" @click="bookmark(source)">{{ bookmarkIds.includes(source.id) ? '★' : '☆' }}</button></div><h3>{{ source.title }}</h3><p class="library-item-publisher">{{ source.publisher }} · 校核 {{ source.reviewed_at }}</p><p class="library-item-excerpt">{{ source.content }}</p><button class="library-read" :disabled="detailLoading" @click="openDetail(source)">阅读条目与来源 <span aria-hidden="true">↗</span></button></article></div>
    <section v-if="retrieval" class="library-retrieval"><div><span aria-hidden="true">◇</span><p><strong>{{ retrievalLabel }}</strong><small>{{ retrieval.embedding_available ? '语义辅助只使用本机服务，不向第三方嵌入服务发送查询。' : '本机语义服务不可用，当前使用主题 / 词法检索。' }} 临床正文始终来自已有教育资料。</small></p></div><button v-if="query.trim()" class="secondary-button" :disabled="evidenceLoading" @click="explainRetrieval">{{ evidenceLoading ? '正在读取…' : '查看检索依据' }}</button><p v-if="evidenceError" class="inline-validation" role="alert">{{ evidenceError }}</p><div v-if="evidence" class="evidence-results"><strong>本次定位到 {{ evidence.items?.length || 0 }} 条资料</strong><ul><li v-for="item in evidence.items || []" :key="item.id">{{ item.title }}</li></ul><p>检索状态：{{ evidence.retrieval?.embedding_available ? '本机语义辅助可用' : '主题 / 词法回退' }}。这些结果仅用于资料定位，不代表疾病概率或医疗置信度。</p></div></section>
    <dialog ref="sourceDialog" class="source-dialog" aria-labelledby="source-detail-title" @cancel.prevent="closeDetail" @click="($event.target === sourceDialog) && closeDetail()"><div class="source-dialog-content"><div class="source-dialog-top"><span class="small-heading">健康教育资料详情</span><button aria-label="关闭资料详情" class="close-modal" @click="closeDetail">×</button></div><p v-if="detailLoading" class="v2-loading" role="status">正在读取资料详情…</p><div v-if="detailError" class="inline-error" role="alert"><p>{{ detailError }}</p></div><template v-if="sourceDetail"><h2 id="source-detail-title">{{ sourceDetail.title }}</h2><div class="source-detail-meta"><span>{{ sourceDetail.publisher }}</span><span>校核日期 {{ sourceDetail.reviewed_at }}</span><span>{{ sourceDetail.category }}</span></div><div class="source-full-content">{{ sourceDetail.content }}</div><p class="source-detail-boundary">这里展示资料库中的整理内容。完整原文请打开下方来源链接；资料用于健康教育与就诊准备，医疗专业审核尚未完成。</p><div class="source-detail-actions"><a v-if="detailHref" :href="detailHref" target="_blank" rel="noopener noreferrer" class="primary-button">查看来源原文 ↗<span class="sr-only">（在新标签页打开）</span></a><span v-else>来源链接不可用</span><button class="secondary-button" :disabled="!!savingId || !allowSaving" @click="bookmark(sourceDetail)">{{ bookmarkIds.includes(sourceDetail.id) ? '取消收藏' : '收藏这份资料' }}</button></div></template></div></dialog>
  </div>
</template>
