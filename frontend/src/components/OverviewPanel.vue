<script setup lang="ts">
import { computed } from 'vue'
import { shortDate } from '../helpers'
import type { Source, Visit, Workspace } from '../types'
import LeafMark from './LeafMark.vue'
const props = defineProps<{ visit: Visit | null; workspace: Workspace | null; error: string; loading: boolean; sources: Source[]; completion: number; busy: boolean }>()
defineEmits<{ navigate: [tab: 'prepare' | 'chat' | 'timeline' | 'library' | 'summary']; select: [id: string]; category: [category: string]; refresh: []; rename: [] }>()
const categories = computed(() => [...new Set(props.sources.map(source => source.category).filter((value): value is string => !!value))].slice(0, 6))
const stats = computed(() => props.workspace ? [
  { value: props.workspace.visits_count, title: '本人的就诊记录', icon: '▤' },
  { value: props.workspace.messages_count, title: '本人的对话消息', icon: '◌' },
  { value: props.workspace.ready_count, title: '核心信息已整理记录', icon: '☑' },
  { value: props.workspace.observations_count, title: '本人时间线记录', icon: '◷' },
  { value: props.workspace.bookmarks_count, title: '已收藏的资料', icon: '⌑' },
  { value: props.workspace.knowledge_count, title: '本机教育资料条目', icon: '▧' },
] : [])
const stage = computed(() => ({ start: '从已知信息开始', collecting: '正在补充信息', ready: '基础信息已整理', urgent: '紧急提醒优先', support: '先获得陪伴支持' }[props.visit?.collection?.stage || 'start']))
</script>

<template>
  <div class="overview-panel v2-panel">
    <div class="overview-heading"><div><span class="step-caption">你的就诊准备工作台</span><h2>一份记录，连接准备的每一步。</h2><p>从整理自述，到记录变化、查阅资料，再带着清晰的问题与医生沟通。</p></div><div class="overview-leaf" aria-hidden="true"><LeafMark /></div></div>
    <div v-if="error" class="inline-error" role="alert"><p>{{ error }}</p><button :disabled="loading" @click="$emit('refresh')">重新加载总览</button></div>
    <div v-if="loading && !workspace" class="v2-loading" role="status"><span class="spinner" aria-hidden="true"></span>正在读取本人的工作台…</div>
    <div v-else-if="workspace" class="workspace-stats"><article v-for="stat in stats" :key="stat.title"><span aria-hidden="true">{{ stat.icon }}</span><strong>{{ stat.value }}</strong><p>{{ stat.title }}</p></article></div>
    <section class="continue-card"><div class="continue-header"><div><span class="small-heading">当前就诊记录</span><h3>{{ visit?.title || '等待打开记录' }}</h3></div><button :disabled="busy || !visit" @click="$emit('rename')">修改标题</button></div><div class="continue-progress"><span>{{ stage }}</span><strong>{{ completion }} / 7 项核心信息</strong></div><div class="completion-track"><span :style="{ width: `${completion / 7 * 100}%` }"></span></div><p class="next-question">{{ visit?.collection?.next_question || '先记下主要不适和开始时间，不确定的信息可以留空。' }}</p><div class="continue-actions"><button class="primary-button" :disabled="busy || !visit" @click="$emit('navigate', 'prepare')">继续整理信息 <span aria-hidden="true">→</span></button><button class="secondary-button" :disabled="busy || !visit" @click="$emit('navigate', 'timeline')">记录今天的变化</button><button class="secondary-button" :disabled="busy || !visit" @click="$emit('navigate', 'summary')">查看摘要与计划</button></div></section>
    <div class="overview-two-column"><section class="overview-recent"><div class="v2-section-title"><h3>最近的就诊准备</h3><span>{{ workspace?.recent_visits?.length || 0 }} 条</span></div><div v-if="workspace?.recent_visits?.length" class="recent-records"><button v-for="record in workspace.recent_visits.slice(0, 5)" :key="record.id" :disabled="busy" @click="$emit('select', record.id)"><span class="recent-record-icon" aria-hidden="true">▤</span><span><strong>{{ record.title }}</strong><small>{{ shortDate(record.updated_at) }} · {{ record.summary ? '已有自述摘要' : '待继续准备' }}</small></span><span aria-hidden="true">→</span></button></div><p v-else class="v2-empty-inline">暂无最近记录。新建或保存记录后，会显示在这里。</p></section><section class="overview-topics"><div class="v2-section-title"><h3>按主题查看健康资料</h3><button :disabled="busy" @click="$emit('navigate', 'library')">查看全部 →</button></div><div class="topic-quick-links"><button v-for="category in categories" :key="category" :disabled="busy" @click="$emit('category', category)">{{ category }} <span aria-hidden="true">↗</span></button></div><p v-if="!categories.length" class="v2-empty-inline">主题资料会在本机资料库加载后显示。</p><p class="topic-note">资料仅用于健康教育与就诊沟通，不能替代专业评估。医疗专业审核尚未完成。</p></section></div>
    <section class="workspace-status" v-if="workspace"><div><span aria-hidden="true">◇</span><p><strong>默认在本机处理</strong><small>当前访客资料保留 {{ workspace.privacy?.retention_days || '待确认' }} 天；可以主动清空。</small></p></div><div><span aria-hidden="true">⌕</span><p><strong>{{ workspace.retrieval?.embedding_available ? '本机语义服务可用' : '主题 / 词法检索' }}</strong><small>{{ workspace.retrieval?.embedding_available ? '语义辅助仅使用本机服务，临床正文仍来自教育资料。' : '语义服务不可用时，使用本机主题与词法检索。' }}</small></p></div></section>
  </div>
</template>
