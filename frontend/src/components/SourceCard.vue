<script setup lang="ts">
import { computed } from 'vue'
import type { Source } from '../types'
import { safeSourceUrl } from '../helpers'
const props = defineProps<{ source: Source; index?: number }>()
const href = computed(() => safeSourceUrl(props.source.url))
</script>

<template>
  <article class="source-card">
    <span class="source-number">{{ index === undefined ? '↗' : String(index + 1).padStart(2, '0') }}</span>
    <div class="source-detail">
      <a v-if="href" :href="href" target="_blank" rel="noopener noreferrer" class="source-title">{{ source.title }} <span aria-hidden="true">↗</span><span class="sr-only">（在新标签页打开）</span></a>
      <span v-else class="source-title">{{ source.title || '资料来源' }}</span>
      <p>{{ source.publisher || '资料库' }}<template v-if="source.reviewed_at"> · 校核 {{ source.reviewed_at }}</template></p>
      <details v-if="source.content" class="source-excerpt"><summary>查看资料摘录</summary><p>{{ source.content }}</p></details>
    </div>
  </article>
</template>
