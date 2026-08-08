/// <reference lib="webworker" />

// __WB_MANIFEST is a build-time placeholder: vite-plugin-pwa replaces it with
// the precache list once the hashed filenames are known. Declared here rather
// than pulled from workbox types, since no workbox runtime is installed.
declare const self: ServiceWorkerGlobalScope & {
  __WB_MANIFEST: Array<{ url: string; revision: string | null }>
}

// Precache the shell, serve navigations network-first, and — the rule that
// actually matters here — never write an Easy Auth login redirect into a cache.
//
// The app sits behind Container Apps Easy Auth with RedirectToLoginPage, so
// every request from an expired session is answered with a 302 to
// login.microsoftonline.com. Caching one of those as index.html would pin the
// operator to a Microsoft login page rendered *as the app*, in a standalone
// window with no address bar, on every launch, until they cleared site data.
// Two guards prevent it and neither is optional: `response.redirected`, and
// `response.type !== 'basic'`.
//
// Responses are always passed back untouched. A navigation whose fetch was
// redirected comes back as an opaqueredirect, and handing that to respondWith is
// precisely what makes the browser follow it to the login page. The guards
// govern what we *store*, never what we return.
//
// No workbox runtime. injectManifest only requires that self.__WB_MANIFEST be
// referenced; the rest is the plain Cache API. That keeps this file ~100 legible
// lines instead of an opaque bundle, which matters a great deal when its whole
// job is a security-adjacent guard someone has to be able to audit.

const VERSION = 'v1'
const SHELL_CACHE = `ppn-shell-${VERSION}`
const ASSET_CACHE = `ppn-assets-${VERSION}`
const SHELL = '/index.html'

const MANIFEST = self.__WB_MANIFEST

function safeToCache(response: Response): boolean {
  return response.ok && response.type === 'basic' && !response.redirected
}

self.addEventListener('install', (event) => {
  event.waitUntil(
    (async () => {
      const cache = await caches.open(SHELL_CACHE)
      // Fetched and checked one at a time rather than cache.addAll: an install
      // that happens to run with a stale session must fail loudly and leave the
      // previous worker in place, not activate with a cache full of login pages.
      await Promise.all(
        MANIFEST.map(async ({ url }) => {
          const response = await fetch(url, { credentials: 'same-origin', cache: 'reload' })
          if (!safeToCache(response)) throw new Error(`refusing to precache ${url}`)
          await cache.put(url, response)
        }),
      )
    })(),
  )
})

self.addEventListener('activate', (event) => {
  event.waitUntil(
    (async () => {
      const keep = new Set<string>([SHELL_CACHE, ASSET_CACHE])
      const keys = await caches.keys()
      await Promise.all(keys.filter((k) => !keep.has(k)).map((k) => caches.delete(k)))
      await self.clients.claim()
    })(),
  )
})

// Only ever at the page's request, once the operator has accepted the update
// prompt. Never on our own initiative: swapping the shell out from under a
// running write would lose the event log they are watching, which is the one
// thing this app exists to show.
self.addEventListener('message', (event) => {
  if (event.data === 'SKIP_WAITING') void self.skipWaiting()
})

self.addEventListener('fetch', (event) => {
  const { request } = event
  const url = new URL(request.url)

  // Everything below is deliberately NOT handled. No respondWith means the
  // browser's own networking, untouched:
  //   - the API, including the SSE stream at /api/runs/{id}/events, which a
  //     service worker would buffer into uselessness
  //   - /.auth/*, the Easy Auth endpoints themselves
  //   - anything cross-origin, and anything that is not a GET
  if (request.method !== 'GET') return
  if (url.origin !== self.location.origin) return
  if (url.pathname.startsWith('/api/')) return
  if (url.pathname.startsWith('/.auth/')) return
  if (request.headers.get('accept')?.includes('text/event-stream')) return

  if (request.mode === 'navigate') {
    event.respondWith(networkFirstShell(request))
    return
  }
  if (url.pathname.startsWith('/assets/')) {
    event.respondWith(cacheFirstImmutable(request))
  }
})

async function networkFirstShell(request: Request): Promise<Response> {
  try {
    const response = await fetch(request)
    if (safeToCache(response)) {
      const cache = await caches.open(SHELL_CACHE)
      void cache.put(SHELL, response.clone())
    }
    // Redirected, opaque, or an error status: hand it straight back. The browser
    // follows it to the login page, which is exactly right. Note the fallback
    // below triggers on a *network* failure only, never on an HTTP status — a
    // reachable server always wins.
    return response
  } catch {
    const cached = await caches.match(SHELL, { cacheName: SHELL_CACHE })
    return cached ?? Response.error()
  }
}

// Names under /assets/ carry a content hash, so a hit can never be stale and a
// miss is always a genuinely new file.
async function cacheFirstImmutable(request: Request): Promise<Response> {
  const cache = await caches.open(ASSET_CACHE)
  const cached = await cache.match(request)
  if (cached) return cached
  const response = await fetch(request)
  if (safeToCache(response)) void cache.put(request, response.clone())
  return response
}
