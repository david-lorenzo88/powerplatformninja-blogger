// Register the service worker and report when a new build is waiting.
//
// Deliberately hand-rolled rather than virtual:pwa-register, which would pull in
// workbox-window for about thirty lines of work — and this way the update policy
// is visible: we never skipWaiting on our own initiative. Swapping the shell out
// from under a running write would lose the live event log, which is the one
// thing this console exists to show. The operator decides.

let waiting: ServiceWorker | null = null

export function registerSW(onUpdate: () => void): void {
  // Only in production: a worker in front of the Vite dev server caches the very
  // modules HMR is trying to replace.
  if (!import.meta.env.PROD || !('serviceWorker' in navigator)) return

  const register = () => {
    void navigator.serviceWorker
      .register('/sw.js', { scope: '/' })
      .then((registration) => {
        // An installed app has no address bar and no reload button, so if it
        // never asks whether there is a newer build, there is no way for the
        // operator to get one — they are simply stuck on whatever shipped the
        // day they installed it. The browser only checks sw.js on a navigation
        // and roughly once a day, and a PWA resumed from the background does
        // neither. So ask explicitly: now, and every time the app comes back to
        // the foreground. Cheap — sw.js is served no-cache and is ~4KB.
        void registration.update()
        document.addEventListener('visibilitychange', () => {
          if (document.visibilityState === 'visible') void registration.update()
        })
        // Already superseded by the time we arrived.
        if (registration.waiting && navigator.serviceWorker.controller) {
          waiting = registration.waiting
          onUpdate()
          return
        }
        registration.addEventListener('updatefound', () => {
          const installing = registration.installing
          if (!installing) return
          installing.addEventListener('statechange', () => {
            // A controller means this is a replacement rather than a first
            // install, and there is nothing to tell anyone about a first install.
            if (installing.state === 'installed' && navigator.serviceWorker.controller) {
              waiting = installing
              onUpdate()
            }
          })
        })
      })
      .catch(() => {
        // Not worth interrupting anyone over: the app works perfectly well
        // without a worker, just without offline support. It is also what
        // happens when a background update check runs against an expired session
        // and gets Easy Auth's login page instead of a script — in which case
        // the already-registered worker stays put, which is what we want.
      })
  }

  // Deferred to `load` so registration never competes with the first paint. The
  // readyState check is not belt-and-braces: this runs from a React effect, and
  // on a warm cache the app mounts *after* load has already fired — where
  // adding a listener registers nothing at all and the app silently never
  // installs. That is exactly what happened the first time.
  if (document.readyState === 'complete') register()
  else window.addEventListener('load', register, { once: true })
}

export function applyUpdate(): void {
  if (!waiting) {
    location.reload()
    return
  }
  // Reload once the replacement has actually taken over, not before.
  navigator.serviceWorker.addEventListener('controllerchange', () => location.reload(), {
    once: true,
  })
  waiting.postMessage('SKIP_WAITING')
}
