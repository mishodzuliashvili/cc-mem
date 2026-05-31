import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Dev server proxies /api to the Python backend on :8765, so the frontend and
// backend feel like one origin (no CORS dance). `base: './'` makes the built
// assets load correctly when the Python server serves ui/dist at /.
export default defineConfig({
  base: './',
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://localhost:8765',
    },
  },
})
