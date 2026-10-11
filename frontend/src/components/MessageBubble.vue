<script setup lang="ts">
import { computed } from 'vue'
import type { IntakeCandidate, Message } from '../types'
import { replyText } from '../helpers'
import LeafMark from './LeafMark.vue'
import SourceCard from './SourceCard.vue'
import CandidateCard from './CandidateCard.vue'
const props = defineProps<{ message: Message; disabled?: boolean; pendingCandidateIds?: string[] }>()
defineEmits<{ ask: [question: string]; confirmCandidate: [candidate: IntakeCandidate] }>()
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
  <article :class="['message', message.role === 'user' ? 'message-user' : 'message-assistant']">
    <div v-if="message.role === 'assistant'" class="assistant-avatar"><LeafMark /></div>
    <div class="message-body">
      <div class="message-label"><span>{{ message.role === 'user' ? '你的自述' : '知禾' }}</span><span v-if="message.role === 'assistant'" class="message-mode">{{ modeLabel }}</span></div>
      <div class="message-content">{{ message.role === 'assistant' ? replyText(message) : message.content }}</div>
      <div v-if="message.role === 'assistant' && message.intake_candidates?.length" class="message-candidates"><CandidateCard v-for="candidate in message.intake_candidates" :key="candidate.id" :candidate="candidate" :disabled="disabled" :pending="pendingCandidateIds === undefined || pendingCandidateIds.includes(candidate.id)" @confirm="$emit('confirmCandidate', $event)" /></div>
      <div v-if="message.role === 'assistant' && message.sources?.length" class="message-sources"><p class="small-heading">本次参考资料</p><SourceCard v-for="(source, index) in message.sources" :key="source.id || index" :source="source" :index="index" /></div>
      <details v-if="message.role === 'assistant' && message.trace?.length" class="execution-details">
        <summary>查看工具执行过程 <span>{{ message.trace.length }} 项</span></summary>
        <p>这里展示实际工具及状态，不展示模型隐式思维。</p>
        <ol><li v-for="(step, index) in message.trace" :key="index"><span class="trace-dot" aria-hidden="true"></span><span>{{ step.label || step.tool }}</span><span class="trace-status">{{ statusLabel(step.status) }}</span></li></ol>
      </details>
      <div v-if="message.role === 'assistant' && message.suggested_questions?.length" class="followups"><button v-for="question in message.suggested_questions" :key="question" class="followup-chip" :disabled="disabled" @click="$emit('ask', question)">{{ question }} <span aria-hidden="true">↗</span></button></div>
    </div>
  </article>
</template>
