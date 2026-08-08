import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient } from '@tanstack/react-query'
import { PersistQueryClientProvider } from '@tanstack/react-query-persist-client'
import { RouterProvider } from 'react-router-dom'
import './index.css'
import { CACHE_BUSTER, persister, shouldPersist } from './lib/persist'
import { router } from './routes'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false,
      // Cached entries have to outlive the tab for persistence to mean
      // anything: gcTime is the ceiling on how long a query may sit in the
      // cache unused, and the default five minutes would evict everything
      // long before the next time the app is opened.
      gcTime: 7 * 24 * 60 * 60 * 1000,
    },
  },
})

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <PersistQueryClientProvider
      client={queryClient}
      persistOptions={{
        persister,
        maxAge: 7 * 24 * 60 * 60 * 1000,
        buster: CACHE_BUSTER,
        dehydrateOptions: { shouldDehydrateQuery: shouldPersist },
      }}
    >
      <RouterProvider router={router} />
    </PersistQueryClientProvider>
  </StrictMode>,
)
