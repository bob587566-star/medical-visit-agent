<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import type { Observation, ObservationInput, ObservationKind } from '../types'
const props = defineProps<{ observations: Observation[]; busy: boolean; available: boolean }>()
const emit = defineEmits<{ create: [input: ObservationInput]; delete: [id: string] }>()
const labels: Record<ObservationKind, string> = { symptom: '症状变化', temperature: '体温自述', blood_pressure: '血压自述', blood_glucose: '血糖自述', note: '一般备注' }
const kindUnits: Record<ObservationKind, string> = { symptom: '', temperature: '℃', blood_pressure: 'mmHg', blood_glucose: 'mmol/L', note: '' }
function localNow(): string { return new Date(Date.now() - new Date().getTimezoneOffset() * 60_000).toISOString().slice(0, 16) }
const form = reactive({ kind: 'symptom' as ObservationKind, value: '', unit: '', recorded_at: localNow(), notes: '' })
const confirmDelete = ref('')
const validation = ref('')
const groups = computed(() => {
  const records = [...props.observations].sort((a, b) => new Date(b.recorded_at).getTime() - new Date(a.recorded_at).getTime())
  const grouped: { day: string; items: Observation[] }[] = []
  for (const item of records) {
    const date = new Date(item.recorded_at)
    const day = Number.isNaN(date.getTime()) ? '日期待核对' : date.toLocaleDateString('zh-CN', { year: 'numeric', month: 'long', day: 'numeric' })
    const last = grouped.at(-1)
    if (last?.day === day) last.items.push(item)
    else grouped.push({ day, items: [item] })
  }
  return grouped
})
function time(value: string): string { const date = new Date(value); return Number.isNaN(date.getTime()) ? value : date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false }) }
function submit(): void {
  validation.value = ''
  if (!form.value.trim()) { validation.value = '先填写你记录的内容或实际测量值。'; return }
  const date = new Date(form.recorded_at)
  if (Number.isNaN(date.getTime())) { validation.value = '请填写有效的记录时间。'; return }
  emit('create', { kind: form.kind, value: form.value.trim(), unit: form.unit.trim(), recorded_at: date.toISOString(), notes: form.notes.trim() })
}
watch(() => form.kind, kind => { form.unit = kindUnits[kind] })
watch(() => props.observations.length, (length, old) => { if (length > old) { form.value = ''; form.notes = ''; form.recorded_at = localNow() } confirmDelete.value = '' })
</script>

<template>
  <div class="timeline-panel v2-panel"><div class="section-heading"><div><span class="step-caption">把变化记下来 · 本人记录</span><h2>症状与测量时间线</h2><p>记录发生时间与原始数值，方便就诊时说明。这里不解释测量结果，也不判断疾病风险。</p></div><span class="self-report-tag">{{ observations.length }} 条记录</span></div>
    <form class="observation-form" @submit.prevent="submit"><div class="observation-form-row"><label class="field"><span id="observation-kind-label">记录类型</span><select v-model="form.kind" aria-labelledby="observation-kind-label" :disabled="busy || !available"><option v-for="(label, kind) in labels" :key="kind" :value="kind">{{ label }}</option></select></label><label class="field"><span>发生 / 测量时间</span><input v-model="form.recorded_at" type="datetime-local" :disabled="busy || !available" required /></label></div><div class="observation-value-row"><label class="field"><span>{{ form.kind === 'symptom' || form.kind === 'note' ? '本人记录的内容' : '本人测得的原始数值' }}</span><input v-model="form.value" maxlength="300" :placeholder="form.kind === 'symptom' ? '例如：咳嗽出现的时间、次数或变化（请用虚构演示）' : form.kind === 'blood_pressure' ? '例如：记录设备显示的完整数值' : '请填写你的原始记录，不作解释'" :disabled="busy || !available" required /></label><label class="field observation-unit"><span>单位</span><input v-model="form.unit" maxlength="30" placeholder="可留空" :disabled="busy || !available" /></label></div><label class="field"><span>记录备注 <small>可选</small></span><textarea v-model="form.notes" maxlength="1000" rows="2" placeholder="例如：测量设备、测量时间条件。不要填写身份信息。" :disabled="busy || !available"></textarea></label><p v-if="validation" class="inline-validation" role="alert">{{ validation }}</p><div class="observation-footer"><p><span aria-hidden="true">◇</span> 这些内容是本人自述，未经医学核实。</p><button class="primary-button" :disabled="busy || !available || !form.value.trim()" type="submit">添加到时间线 <span aria-hidden="true">＋</span></button></div></form>
    <div v-if="!groups.length" class="timeline-empty"><span aria-hidden="true">◷</span><h3>还没有记录变化</h3><p>从一条带时间的自述开始。记录只用于整理信息，不能据此推断疾病。</p></div>
    <section v-for="group in groups" :key="group.day" class="timeline-day"><h3>{{ group.day }}</h3><ol><li v-for="item in group.items" :key="item.id"><div class="timeline-time"><span aria-hidden="true"></span>{{ time(item.recorded_at) }}</div><article><div class="timeline-item-heading"><span>{{ labels[item.kind] || '本人记录' }}</span><button :disabled="busy" :aria-label="`删除${labels[item.kind] || '记录'}：${item.value}`" @click="confirmDelete = item.id">删除</button></div><p class="observation-value">{{ item.value }}<span v-if="item.unit"> {{ item.unit }}</span></p><p v-if="item.notes" class="observation-notes">{{ item.notes }}</p><small>来源：本人记录 · 未作医学解释</small><div v-if="confirmDelete === item.id" class="inline-delete-confirm"><span>删除这条记录？</span><button :disabled="busy" @click="confirmDelete = ''">保留</button><button :disabled="busy" @click="$emit('delete', item.id)">确认删除</button></div></article></li></ol></section>
  </div>
</template>
