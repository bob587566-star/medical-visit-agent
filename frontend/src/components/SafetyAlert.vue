<script setup lang="ts">
import { ref } from 'vue'
import type { Risk } from '../types'
defineProps<{ risk: Risk }>()
const country = ref('unknown')
// Exact contacts returned by the helpline lookup tool. Never infer another
// region's number from a country name or a web article.
const singapore = { name: 'Samaritans of Singapore', phone: '1767', phoneUrl: 'tel:1767?oai_link_source=model_response_hotline', website: 'https://www.sos.org.sg/our-services/?oai_link_source=model_response_hotline' }
</script>

<template>
  <div :class="['risk-banner', risk.level === 'emergency' ? 'risk-emergency' : 'risk-prompt', { 'crisis-support': risk.kind === 'self_harm' }]" role="alert">
    <span class="risk-icon" aria-hidden="true">{{ risk.kind === 'self_harm' ? '♡' : '!' }}</span>
    <div>
      <strong>{{ risk.kind === 'self_harm' ? '先让你得到陪伴和支持' : risk.level === 'emergency' ? '先关注紧急情况' : '需要留意的就诊提醒' }}</strong>
      <p>{{ risk.message }}</p>
      <template v-if="risk.kind === 'self_harm'">
        <div class="crisis-contacts"><label>你当前所在的国家或地区<select v-model="country"><option value="unknown">先不选择</option><option value="CN">中国大陆</option><option value="SG">新加坡</option><option value="other">其他地区</option></select></label>
          <p v-if="country === 'SG'">{{ singapore.name }} · 全天热线：<a :href="singapore.phoneUrl">{{ singapore.phone }}</a>。<a :href="singapore.website" target="_blank" rel="noopener noreferrer">查看当地支持服务</a></p>
          <p v-else-if="country !== 'unknown'">当前没有可提供的已核实本地区心理支持热线。请让信任的人陪着你，协助联系当地专业支持；有即时危险时联系当地急救。</p>
          <small>仅按你选择的地区展示已核实资源，不会自动拨打或发送消息。</small>
        </div>
      </template>
      <ul v-else-if="risk.reasons?.length"><li v-for="reason in risk.reasons" :key="reason">{{ reason }}</li></ul>
      <small>{{ risk.kind === 'self_harm' ? '这里会暂停自动资料整理，先关注你的安全和支持。' : '提醒不能覆盖所有紧急情况，也不能排除疾病。' }}</small>
    </div>
  </div>
</template>

<style scoped>
.crisis-support{border-color:#dfc9b9;background:#fff8f2;color:#805749}.crisis-contacts{margin-top:15px;padding:12px 0;border-top:1px solid #e9d9cb}.crisis-contacts label{display:flex;flex-wrap:wrap;align-items:center;gap:12px;font-size:13px}.crisis-contacts select{padding:8px 10px;border:1px solid #dfcdbb;border-radius:7px;background:white;color:#805749;font:inherit}.crisis-contacts a{color:#745045;text-decoration:underline}.crisis-contacts small{display:block;margin-top:9px;line-height:1.8}
</style>
