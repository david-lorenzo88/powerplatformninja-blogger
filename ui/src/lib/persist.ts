import { createAsyncStoragePersister } from '@tanstack/query-async-storage-persister'
import type { Query } from '@tanstack/react-query'
import { clear, del, get, set } from 'idb-keyval'

// Keep drafts readable on a train.
//
// IndexedDB rather than localStorage: a post's markdown plus its review report
// runs to tens of kilobytes, several posts blow past localStorage's ~5MB and it
// throws rather than evicting.
//
// The allow-list is the important part, and it is an allow-list on purpose. What
// is worth reading offline is content — drafts, reports, the topic backlog — all
// of which is either finished or changes slowly. Run state is the opposite: a
// cached `running` shown an hour later is not stale data, it is a lie about
// what the crew is doing right now, and worse than showing nothing. Health is
// the same. So neither is persisted, and both simply come back empty offline.
const PERSISTED = new Set(['posts', 'post', 'drafts', 'draft', 'draft-versions', 'topic-ideas', 'topic-idea'])

export function shouldPersist(query: Query): boolean {
  const root = query.queryKey[0]
  return typeof root === 'string' && PERSISTED.has(root)
}

export const persister = createAsyncStoragePersister({
  storage: {
    getItem: async (key) => (await get<string>(key)) ?? null,
    setItem: (key, value) => set(key, value),
    removeItem: (key) => del(key),
  },
  key: 'ppn-query-cache',
  throttleTime: 2000,
})

// A build's assets are content-hashed, but the cache this writes is not — so a
// deploy that changes a response shape would otherwise rehydrate yesterday's
// objects into today's components. Busting on the build id makes the cache
// belong to exactly one build.
export const CACHE_BUSTER = __BUILD_ID__

export async function clearPersistedCache(): Promise<void> {
  await clear()
}
