import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Navigate, RouterProvider, createBrowserRouter } from 'react-router-dom'
import './index.css'
import { AppShell } from './components/AppShell'
import { RunsScreen } from './screens/RunsScreen'
import { RunDetailScreen } from './screens/RunDetailScreen'
import { ConfigScreen } from './screens/ConfigScreen'
import { DraftsScreen } from './screens/DraftsScreen'
import { PostDetailScreen } from './screens/PostDetailScreen'
import { TopicIdeasScreen } from './screens/TopicIdeasScreen'
import { TopicIdeaDetailScreen } from './screens/TopicIdeaDetailScreen'
import { SourceReviewsScreen } from './screens/SourceReviewsScreen'
import { SourceReviewScreen } from './screens/SourceReviewScreen'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: 1, refetchOnWindowFocus: false },
  },
})

const router = createBrowserRouter([
  {
    path: '/',
    element: <AppShell />,
    children: [
      { index: true, element: <Navigate to="/runs" replace /> },
      { path: 'runs', element: <RunsScreen /> },
      { path: 'runs/:id', element: <RunDetailScreen /> },
      { path: 'topic-ideas', element: <TopicIdeasScreen /> },
      { path: 'topic-ideas/:id', element: <TopicIdeaDetailScreen /> },
      { path: 'source-reviews', element: <SourceReviewsScreen /> },
      { path: 'source-reviews/:id', element: <SourceReviewScreen /> },
      { path: 'config', element: <ConfigScreen /> },
      { path: 'drafts', element: <DraftsScreen /> },
      { path: 'drafts/:id', element: <PostDetailScreen /> },
    ],
  },
])

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </StrictMode>,
)
