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
const LearningScreen = lazy(() =>
  import('./screens/LearningScreen').then((m) => ({ default: m.LearningScreen })),
)
const DeltaPairScreen = lazy(() =>
  import('./screens/LearningScreen').then((m) => ({ default: m.DeltaPairScreen })),
)
const LearningReviewsScreen = lazy(() =>
  import('./screens/LearningReviewScreen').then((m) => ({ default: m.LearningReviewsScreen })),
)
const LearningReviewScreen = lazy(() =>
  import('./screens/LearningReviewScreen').then((m) => ({ default: m.LearningReviewScreen })),
)
const SourceReviewsScreen = lazy(() =>
  import('./screens/SourceReviewsScreen').then((m) => ({ default: m.SourceReviewsScreen })),
)
const SourceReviewScreen = lazy(() =>
  import('./screens/SourceReviewScreen').then((m) => ({ default: m.SourceReviewScreen })),
)
const ArticlesScreen = lazy(() =>
  import('./screens/ArticlesScreen').then((m) => ({ default: m.ArticlesScreen })),
)
const FeedsScreen = lazy(() =>
  import('./screens/FeedsScreen').then((m) => ({ default: m.FeedsScreen })),
)
const FeedDetailScreen = lazy(() =>
  import('./screens/FeedDetailScreen').then((m) => ({ default: m.FeedDetailScreen })),
)
const FeedGroupsScreen = lazy(() =>
  import('./screens/FeedGroupsScreen').then((m) => ({ default: m.FeedGroupsScreen })),
)
const NewslettersScreen = lazy(() =>
  import('./screens/NewslettersScreen').then((m) => ({ default: m.NewslettersScreen })),
)
const IssuesScreen = lazy(() =>
  import('./screens/NewslettersScreen').then((m) => ({ default: m.IssuesScreen })),
)
const NewsletterDetailScreen = lazy(() =>
  import('./screens/NewsletterDetailScreen').then((m) => ({ default: m.NewsletterDetailScreen })),
)
const IssueDetailScreen = lazy(() =>
  import('./screens/IssueDetailScreen').then((m) => ({ default: m.IssueDetailScreen })),
)
const FeedReviewScreen = lazy(() =>
  import('./screens/FeedReviewScreen').then((m) => ({ default: m.FeedReviewScreen })),
)
const FeedReviewsScreen = lazy(() =>
  import('./screens/FeedReviewScreen').then((m) => ({ default: m.FeedReviewsScreen })),
)
const RecipientsScreen = lazy(() =>
  import('./screens/RecipientsScreen').then((m) => ({ default: m.RecipientsScreen })),
)
const SpendScreen = lazy(() =>
  import('./screens/SpendScreen').then((m) => ({ default: m.SpendScreen })),
)

export const router = createBrowserRouter([
  {
    path: '/',
    element: <AppShell />,
    children: [
      { index: true, element: <Navigate to="/runs" replace /> },
      { path: 'runs', element: <RunsScreen /> },
      { path: 'runs/:id', element: <RunDetailScreen /> },
      { path: 'spend', element: <SpendScreen /> },
      { path: 'topic-ideas', element: <TopicIdeasScreen /> },
      { path: 'topic-ideas/:id', element: <TopicIdeaDetailScreen /> },
      { path: 'learning', element: <LearningScreen /> },
      { path: 'learning/pairs/:id', element: <DeltaPairScreen /> },
      { path: 'learning-reviews', element: <LearningReviewsScreen /> },
      { path: 'learning-reviews/:id', element: <LearningReviewScreen /> },
      { path: 'source-reviews', element: <SourceReviewsScreen /> },
      { path: 'source-reviews/:id', element: <SourceReviewScreen /> },
      { path: 'config', element: <ConfigScreen /> },
      { path: 'drafts', element: <DraftsScreen /> },
      { path: 'drafts/:id', element: <PostDetailScreen /> },
      { path: 'articles', element: <ArticlesScreen /> },
      { path: 'feeds', element: <FeedsScreen /> },
      { path: 'feeds/:id', element: <FeedDetailScreen /> },
      { path: 'feed-groups', element: <FeedGroupsScreen /> },
      { path: 'feed-reviews', element: <FeedReviewsScreen /> },
      { path: 'feed-reviews/:id', element: <FeedReviewScreen /> },
      { path: 'newsletters', element: <NewslettersScreen /> },
      // Before ':id', or "issues" is parsed as a newsletter id.
      { path: 'newsletters/issues', element: <IssuesScreen /> },
      { path: 'newsletters/recipients', element: <RecipientsScreen /> },
      { path: 'newsletters/issues/:id', element: <IssueDetailScreen /> },
      { path: 'newsletters/:id', element: <NewsletterDetailScreen /> },
    ],
  },
])
