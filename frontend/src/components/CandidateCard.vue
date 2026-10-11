<script setup lang="ts">
import { intakeLabels, type IntakeCandidate } from '../types'
defineProps<{ candidate: IntakeCandidate; disabled?: boolean; pending?: boolean; currentValue?: string }>()
defineEmits<{ confirm: [candidate: IntakeCandidate] }>()
</script>

<template>
  <article class="candidate-card">
    <div class="candidate-top"><span aria-hidden="true">✧</span><strong>{{ intakeLabels[candidate.field] || '就诊信息' }}</strong><span :class="['candidate-state', { handled: pending === false }]">{{ pending === false ? '已处理或已失效' : '待本人确认' }}</span></div>
    <p class="candidate-value">{{ candidate.value }}</p>
    <blockquote><span>依据你的自述</span>{{ candidate.evidence }}</blockquote>
    <p class="candidate-notice">候选仅供你核对。未经确认，不会写入就诊表单。</p>
    <button class="candidate-confirm" :disabled="disabled || pending === false" @click="$emit('confirm', candidate)">检查并确认这一项 <span aria-hidden="true">→</span></button>
  </article>
</template>
