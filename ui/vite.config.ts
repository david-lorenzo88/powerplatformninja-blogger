import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { VitePWA } from 'vite-plugin-pwa'

// The SPA is served same-origin from `ui/dist` in production and from the Vite
// dev server in development. Proxying `/api` to the running `ppn serve` keeps
// both `fetch` and `EventSource` (SSE) same-origin in dev too, so the stream
// never has to negotiate CORS. Point the proxy elsewhere with PPN_API_TARGET.
const apiTarget = process.env.PPN_API_TARGET ?? 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [
    react(),
    tailwindcss(),
    VitePWA({
      // injectManifest, not generateSW: generateSW serialises its config into a
      // workbox-build call, so it cannot express a function — and the guard that
      // stops an Easy Auth login redirect being cached *is* a function. The push
      // handler that lands next is the same story.
      strategies: 'injectManifest',
      srcDir: 'src',
      filename: 'sw.ts',
      // Registered by hand in lib/registerSW.ts so the update prompt is ours.
      injectRegister: false,
      // public/manifest.webmanifest is authored, not generated: its path has to
      // be stable and unhashed because Easy Auth excludes it by exact path.
      manifest: false,
      injectManifest: {
        globDirectory: 'dist',
        // The shell only. Precaching every route chunk would pull CodeMirror and
        // React Flow down on install over cellular, undoing the code splitting;
        // route chunks are cached at runtime on first use instead, which is
        // sound because their names carry a content hash.
        globPatterns: [
          'index.html',
          'assets/app-*.js',
          'assets/*.css',
          'favicon.svg',
          'icons/*.png',
          'apple-touch-icon.png',
          'manifest.webmanifest',
        ],
      },
      // Exercise the worker against the production build served by `ppn serve`,
      // which is the only path that also runs the real cache headers and the
      // manifest's media type. A worker in front of the dev server would just
      // cache modules HMR is trying to replace.
      devOptions: { enabled: false },
    }),
  ],
  build: {
    rollupOptions: {
      // A stable entry name so the precache glob above can match it. Vite
      // otherwise names it after the entry *module* (index.html), which is both
      // unobvious and free to change.
      output: { entryFileNames: 'assets/app-[hash].js' },
    },
  },
  server: {
    proxy: {
      '/api': {
        target: apiTarget,
        changeOrigin: true,
      },
    },
  },
})
