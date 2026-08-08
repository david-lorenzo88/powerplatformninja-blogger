import { useCallback, useSyncExternalStore } from 'react'

// CSS handles nearly every breakpoint in this app. This hook exists only for the
// two places where rendering the wrong branch is expensive rather than merely
// invisible: mounting the React Flow canvas on a phone downloads a chunk nobody
// asked for, and building a hundred table rows CSS would immediately hide costs
// real time on the weakest device that will ever open this. Everywhere else a
// `lg:` prefix is correct — it cannot flash and it costs nothing.
//
// useSyncExternalStore rather than useState + useEffect: the latter renders the
// desktop branch once before the effect corrects it, which on the run screen
// means React Flow loads on a phone anyway — the exact thing being avoided.
export function useMediaQuery(query: string): boolean {
  const subscribe = useCallback(
    (onChange: () => void) => {
      const mql = window.matchMedia(query)
      mql.addEventListener('change', onChange)
      return () => mql.removeEventListener('change', onChange)
    },
    [query],
  )
  return useSyncExternalStore(
    subscribe,
    () => window.matchMedia(query).matches,
    () => false,
  )
}

// The one breakpoint the app branches on in JavaScript, kept beside the hook so
// it can never drift from the `lg:` prefix Tailwind generates from its own
// --breakpoint-lg: 64rem.
export const useIsDesktop = () => useMediaQuery('(min-width: 64rem)')
