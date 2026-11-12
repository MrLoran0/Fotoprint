import { defineConfig, loadEnv } from 'vite'

// /api проксируется на бэкенд: VITE_DEV_API_TARGET из окружения или из .env.local (по умолчанию :8000)
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), 'VITE_')
  const target = process.env.VITE_DEV_API_TARGET || env.VITE_DEV_API_TARGET || 'http://localhost:8000'
  return {
    server: { proxy: { '/api': { target, changeOrigin: true, rewrite: path => path.replace(/^\/api/, '') } } },
  }
})
