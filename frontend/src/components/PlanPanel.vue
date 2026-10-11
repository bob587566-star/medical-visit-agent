<script setup lang="ts">
import type { PreparationPlan } from '../types'
defineProps<{ plan: PreparationPlan | null; busy: boolean; available: boolean }>()
defineEmits<{ generate: [] }>()
function categoryLabel(value: string): string { return ({ information: '本人信息', appointment: '行前安排', preparation: '资料准备', communication: '就诊沟通', safety: '就诊提醒', documents: '检查资料', questions: '沟通问题', logistics: '行前准备', observation: '本人记录' } as Record<string, string>)[value] || '准备事项' }
</script>

<template>
  <section class="plan-panel"><div class="plan-heading"><div><span class="step-caption">准备好要问的问题</span><h3>就诊沟通与准备计划</h3><p>依据你的自述，整理与医生沟通的问题及准备事项。</p></div><button class="secondary-button" :disabled="busy || !available" @click="$emit('generate')">{{ busy ? '正在处理…' : plan ? '更新准备计划' : '生成准备计划' }}</button></div><div v-if="plan" class="plan-content"><p class="plan-goal"><span>这次沟通目标</span>{{ plan.goal }}</p><div class="plan-questions"><h4>想向医生确认的问题</h4><ol v-if="plan.questions?.length"><li v-for="question in plan.questions" :key="question">{{ question }}</li></ol><p v-else>当前未生成具体问题，可继续补充就诊信息。</p></div><div class="plan-steps"><h4>就诊前的准备事项</h4><article v-for="(step, index) in plan.steps || []" :key="step.id"><span>{{ String(index + 1).padStart(2, '0') }}</span><div><strong>{{ step.title }}</strong><p>{{ step.detail }}</p><small v-if="step.category">{{ categoryLabel(step.category) }}</small></div></article></div><p class="plan-notice">{{ plan.notice || '计划仅用于就诊沟通与信息准备，不构成诊断或诊疗方案。' }}</p></div><div v-else class="plan-empty"><span aria-hidden="true">✧</span><p>把要问医生的问题和需要携带的资料放在一起。<br />生成后请核对，计划不会提供治疗方案。</p></div></section>
</template>
