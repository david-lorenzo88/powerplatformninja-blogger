// The recovery for a chunk-load failure, and the guard that stops it looping.
//
// Route chunks carry a content hash and the single replica keeps no old build,
// so a tab held open across a deploy asks for asset names that no longer exist.
// The only recovery is a reload — and an unguarded one loops forever.
//
// The guard is a timestamp, not a flag. A flag has to be cleared by something,
// and the only thing that can clear it is a successful render — but AppShell
// always mounts before any lazy route resolves, so clearing it there means it is
// already gone by the time the route fails. That is not a hypothetical: it
// produced an actual infinite reload loop against a stopped server.
//
// A timestamp needs no clearing. One reload is attempted, and a second within
// the cooldown is refused, so a persistent failure lands on the error UI instead
// of a loop. Months later, the next deploy's failure is outside the window and
// gets its own single retry.
const KEY = 'ppn:chunk-reload-at'
const COOLDOWN_MS = 30_000

/** Reload unless we already tried within the cooldown. Returns whether it did. */
export function reloadOnce(): boolean {
  const previous = Number(sessionStorage.getItem(KEY) ?? 0)
  if (Number.isFinite(previous) && Date.now() - previous < COOLDOWN_MS) return false
  sessionStorage.setItem(KEY, String(Date.now()))
  location.reload()
  return true
}

/** The manual button: the operator asked, so the cooldown does not apply. */
export function forceReload(): void {
  sessionStorage.removeItem(KEY)
  location.reload()
}
