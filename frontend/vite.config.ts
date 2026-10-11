import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: { port: 5174, proxy: { '/api': 'http://127.0.0.1:8270', '/healthz': 'http://127.0.0.1:8270', '/readyz': 'http://127.0.0.1:8270' } },
  test: { environment: 'jsdom', include: ['src/**/*.test.ts'] },
})
