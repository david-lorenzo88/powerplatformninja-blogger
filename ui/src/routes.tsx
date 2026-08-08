/* eslint-disable react/only-export-components --
   The rule protects Fast Refresh, which cannot meaningfully apply to a route
   table: these are lazy() handles, not components to hot-swap, and the file's
   only real export is the router. Editing a screen still refreshes normally,
   because the screens themselves live in their own modules. */
import { lazy } from 'react'
import { Navigate, createBrowserRouter } from 'react-router-dom'
import { AppShell } from './components/AppShell'
import { RunsScreen } from './screens/RunsScreen'

// AppShell and RunsScreen stay eager: lazying the shell would blank the tab bar
// on every navigation, and /runs is both the landing route and the manifest's
// start_url, so its chunk would always be fetched a moment later anyway.
//
// Everything else loads on demand. The alternative is what this app shipped
// with — a single 1.4MB bundle in which CodeMirror and React Flow are
// downloaded in order to render a list of runs. Each screen is a named export,
// hence the .then shim.
const RunDetailScreen = lazy(() =>
  import('./screens/RunDetailScreen').then((m) => ({ default: m.RunDetailScreen })),
)
const ConfigScreen = lazy(() =>
  import('./screens/ConfigScreen').then((m) => ({ default: m.ConfigScreen })),
)
const DraftsScreen = lazy(() =>
  import('./screens/DraftsScreen').then((m) => ({ default: m.DraftsScreen })),
)
const PostDetailScreen = lazy(() =>
  import('./screens/PostDetailScreen').then((m) => ({ default: m.PostDetailScreen })),
)
const TopicIdeasScreen = lazy(() =>
  import('./screens/TopicIdeasScreen').then((m) => ({ default: m.TopicIdeasScreen })),
)
const TopicIdeaDetailScreen = lazy(() =>
  import('./screens/TopicIdeaDetailScreen').then((m) => ({ default: m.TopicIdeaDetailScreen })),
)
const SourceReviewsScreen = lazy(() =>
  import('./screens/SourceReviewsScreen').then((m) => ({ default: m.SourceReviewsScreen })),
)
const SourceReviewScreen = lazy(() =>
  import('./screens/SourceReviewScreen').then((m) => ({ default: m.SourceReviewScreen })),
)

export const router = createBrowserRouter([
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
