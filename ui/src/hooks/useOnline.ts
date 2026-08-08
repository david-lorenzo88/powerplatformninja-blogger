import { useSyncExternalStore } from 'react'

// navigator.onLine is a blunt signal — it means "has a network interface", not
// "can reach the server" — but it is the only one available before a request
// fails, and it is exactly right for the case that matters here: a phone that
// has gone into a tunnel. A server that is up but unreachable still surfaces
// through the health query's own error state.
function subscribe(onChange: () => void): () => void {
  window.addEventListener('online', onChange)
  window.addEventListener('offline', onChange)
  return () => {
    window.removeEventListener('online', onChange)
    window.removeEventListener('offline', onChange)
  }
}

export function useOnline(): boolean {
  return useSyncExternalStore(
    subscribe,
    () => navigator.onLine,
    () => true,
  )
}
