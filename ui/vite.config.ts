import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// The SPA is served same-origin from `ui/dist` in production and from the Vite
// dev server in development. Proxying `/api` to the running `ppn serve` keeps
// both `fetch` and `EventSource` (SSE) same-origin in dev too, so the stream
// never has to negotiate CORS. Point the proxy elsewhere with PPN_API_TARGET.
const apiTarget = process.env.PPN_API_TARGET ?? 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      '/api': {
        target: apiTarget,
        changeOrigin: true,
      },
    },
  },
})
