<script setup lang="ts">
import { computed } from 'vue'
import type { IntakeCandidate, Message } from '../types'
import { replyText } from '../helpers'
import LeafMark from './LeafMark.vue'
import SourceCard from './SourceCard.vue'
import CandidateCard from './CandidateCard.vue'
const props = defineProps<{ message: Message; disabled?: boolean; pendingCandidateIds?: string[] }>()
defineEmits<{ ask: [question: string]; confirmCandidate: [candidate: IntakeCandidate] }>()
const pendingCount = computed(() => (props.message.intake_candidates || []).filter(candidate => props.pendingCandidateIds === undefined || props.pendingCandidateIds.includes(candidate.id)).length)
const legacyCollectionQuestions = new Set([
  '最想向医生说明的不适是什么？', '不适从什么时候开始，有什么变化？', '就诊者属于哪个年龄阶段？',
  '你自己感觉不适程度是轻、中还是重？', '目前使用的药物或补充剂是否需要向医生核对？',
  '有没有需要告诉医生的过敏情况？不知道也可以写不知道。', '有没有与本次就诊相关的既往情况？',
])
const collecting = computed(() => !!props.message.followup_field || (props.message.suggested_questions || []).some(question => legacyCollectionQuestions.has(question.trim())))
const followupPrompts = computed(() => {
  const content = typeof props.message.content === 'string' ? props.message.content : ''
  return (props.message.suggested_questions || []).filter(question => !content.includes(question))
})
const modeLabel = computed(() => {
  const mode = props.message.mode
  if (['model', 'deepseek', 'model_assisted'].includes(mode || '')) return 'DeepSeek 辅助'
  if (mode === 'local_fallback') return '已回退本机整理'
  if (mode === 'local_emergency') return '紧急提醒优先'
  return '本机资料整理'
})
function statusLabel(status: string): string {
  if (['completed', 'success', 'done'].includes(status)) return '完成'
  if (status === 'skipped') return '跳过'
  if (status === 'fallback') return '本机回退'
  if (['failed', 'error'].includes(status)) return '未完成'
  return status || '状态待确认'
}
</script>

<template>
  <article :class="['message', message.role === 'user' ? 'message-user' : 'message-assistant']" :data-message-id="message.id">
    <div v-if="message.role === 'assistant'" class="assistant-avatar"><LeafMark /></div>
    <div class="message-body">
      <div class="message-label"><span>{{ message.role === 'user' ? '你的自述' : '知禾' }}</span><span v-if="message.role === 'assistant'" class="message-mode">{{ modeLabel }}</span></div>
      <div class="message-content">{{ message.role === 'assistant' ? replyText(message) : message.content }}</div>
      <div v-if="message.role === 'assistant' && collecting" class="followup-prompt" :data-followup-field="message.followup_field || undefined"><span>可以直接回复</span><p v-for="question in followupPrompts" :key="question">{{ question }}</p><small v-if="!followupPrompts.length">说说你知道的情况；不确定也可以告诉我。</small></div>
      <div v-else-if="message.role === 'assistant' && message.suggested_questions?.length" class="followups"><button v-for="question in message.suggested_questions" :key="question" class="followup-chip" :disabled="disabled" @click="$emit('ask', question)">{{ question }} <span aria-hidden="true">↗</span></button></div>
      <div v-if="message.role === 'assistant'" class="message-support">
      <details v-if="message.intake_candidates?.length" class="message-auxiliary candidate-details"><summary><span>已整理 {{ message.intake_candidates.length }} 项</span><small>{{ pendingCount ? '查看 / 存入' : '查看记录' }}</small></summary><div class="message-candidates"><p class="auxiliary-note">{{ pendingCount ? '待本人确认，展开后可逐项核对；不会自动存入表单。' : '这里保留当时整理的内容；已处理或失效的项目不能重复存入。' }}</p><CandidateCard v-for="candidate in message.intake_candidates" :key="candidate.id" :candidate="candidate" :disabled="disabled" :pending="pendingCandidateIds === undefined || pendingCandidateIds.includes(candidate.id)" @confirm="$emit('confirmCandidate', $event)" /></div></details>
      <details v-if="message.sources?.length" class="message-auxiliary source-details"><summary><span>参考 {{ message.sources.length }} 条资料</span><small>查看来源</small></summary><div class="message-sources"><SourceCard v-for="(source, index) in message.sources" :key="source.id || index" :source="source" :index="index" /></div></details>
      <details v-if="message.role === 'assistant' && message.trace?.length" class="execution-details">
        <summary>处理记录 <span>{{ message.trace.length }} 步</span></summary>
        <p>这里展示实际工具及状态，不展示模型隐式思维。</p>
        <ol><li v-for="(step, index) in message.trace" :key="index"><span class="trace-dot" aria-hidden="true"></span><span>{{ step.label || step.tool }}</span><span class="trace-status">{{ statusLabel(step.status) }}</span></li></ol>
      </details>
      </div>
    </div>
  </article>
</template>
