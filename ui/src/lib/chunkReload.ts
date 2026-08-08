// A one-shot latch for the reload that recovers from a chunk-load failure.
//
// Route chunks carry a content hash and the server keeps no old build, so a tab
// held open across a deploy asks for asset names that no longer exist. The only
// recovery is a reload — and reloading unconditionally would loop, so the first
// one sets this flag and a successful mount clears it.
const KEY = 'ppn:chunk-reload'

export function reloadOnce(): void {
  if (sessionStorage.getItem(KEY)) return
  sessionStorage.setItem(KEY, '1')
  location.reload()
}

export function clearChunkReloadFlag(): void {
  sessionStorage.removeItem(KEY)
}

export function forceReload(): void {
  sessionStorage.removeItem(KEY)
  location.reload()
}
