import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
export default defineConfig({
  plugins: [react()],
  server: { proxy: { '/gateway': { target: 'http://127.0.0.1:4188', changeOrigin: true, rewrite: path => path.replace(/^\/gateway/, '') } } },
  test: { environment: 'jsdom', setupFiles: ['./tests/setup.ts'], include: ['tests/**/*.test.{ts,tsx}'] },
})
