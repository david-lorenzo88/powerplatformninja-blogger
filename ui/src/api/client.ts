// Typed wrappers over the FastAPI contract. Every request goes through `request`
// so error handling is uniform: a non-2xx becomes an `ApiError` carrying the
// server's message. FastAPI puts validation/parse errors (e.g. the YAML 422)
// in `detail`, which is exactly what the Config editor surfaces inline.

import type {
  Article,
  ArticleFilters,
  ConfigContent,
  ConfigHistoryItem,
  ConfigListItem,
  ConfigVersion,
  DecideRequest,
  DecideResult,
  Draft,
  DraftListItem,
  DraftVersion,
  Feed,
  FeedCreateRequest,
  FeedGroup,
  FeedPatchRequest,
  FeedProbe,
  Health,
  NewsSummary,
  Post,
  PostSummary,
  RegenerateRequest,
  Run,
  RunDetail,
  RunEvent,
  RunStatus,
  SourceReview,
  SourceReviewStatus,
  SourceReviewSummary,
  SuggestRequest,
  TopicIdea,
  TopicIdeaSummary,
  WorkflowGraph,
  WriteRequest,
} from './types'

export const API_BASE = '/api'

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

// Entra Easy Auth answers a signed-out request with a 302 to Microsoft, not a
// 401. Under the default redirect mode `fetch` follows that cross-origin, CORS
// blocks the result, and the whole thing arrives as a bare "Failed to fetch" —
// indistinguishable from being offline, which is how a expired session used to
// present: six polling queries all failing for no stated reason.
//
// `redirect: 'manual'` turns it into an opaqueredirect we can recognise. Safe
// here because no endpoint in this file legitimately redirects: every path is a
// concrete FastAPI route declared without a trailing slash, so the framework's
// own redirect_slashes never fires. Re-check that if routes are added.
let redirecting = false

// Bouncing to the login page is the right move once. Doing it on every page load
// is an infinite loop that leaves the operator unable to use the app at all, and
// `redirecting` cannot prevent that on its own because a full navigation resets
// it. So remember across loads: if we redirected moments ago and are being asked
// again, the login is not fixing anything and looping will not help.
const LOGIN_AT = 'ppn:login-redirect-at'
const LOGIN_COOLDOWN_MS = 20_000

function toLogin(): never {
  const previous = Number(sessionStorage.getItem(LOGIN_AT) ?? 0)
  const looping = Number.isFinite(previous) && Date.now() - previous < LOGIN_COOLDOWN_MS

  // Six concurrent polls would otherwise each call location.replace.
  if (!redirecting && !looping) {
    redirecting = true
    sessionStorage.setItem(LOGIN_AT, String(Date.now()))
    const back = `${location.pathname}${location.search}${location.hash}`
    // Must be a relative path: Easy Auth rejects absolute external URLs unless
    // they are listed in allowedExternalRedirectUrls.
    location.replace(`/.auth/login/aad?post_login_redirect_uri=${encodeURIComponent(back)}`)
  }
  throw new ApiError(
    401,
    looping
      ? 'Signed in, but the server still refused the request. Reload to try again.'
      : 'Signed out — redirecting to sign in.',
  )
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: init?.body ? { 'Content-Type': 'application/json' } : undefined,
    credentials: 'same-origin',
    redirect: 'manual',
    ...init,
  })

  if (res.type === 'opaqueredirect') toLogin()
  // Belt and braces: some Easy Auth configurations serve the login page inline
  // at 200 rather than redirecting. HTML from a JSON endpoint means the same.
  if (res.ok && res.status !== 204 && (res.headers.get('content-type') ?? '').includes('text/html')) {
    toLogin()
  }

  if (!res.ok) {
    throw new ApiError(res.status, await errorMessage(res))
  }
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

async function errorMessage(res: Response): Promise<string> {
  try {
    const body = await res.json()
    const detail = (body as { detail?: unknown }).detail
    if (typeof detail === 'string') return detail
    if (detail != null) return JSON.stringify(detail)
    return `${res.status} ${res.statusText}`
  } catch {
    return `${res.status} ${res.statusText}`
  }
}

// -- Health -----------------------------------------------------------------

export const getHealth = () => request<Health>('/health')

// -- Runs -------------------------------------------------------------------

export const listRuns = (params?: { status?: RunStatus; limit?: number }) => {
  const q = new URLSearchParams()
  if (params?.status) q.set('status', params.status)
  if (params?.limit) q.set('limit', String(params.limit))
  const qs = q.toString()
  return request<Run[]>(`/runs${qs ? `?${qs}` : ''}`)
}

export const getRun = (id: string) => request<RunDetail>(`/runs/${id}`)

export const startSuggest = (body: SuggestRequest) =>
  request<{ id: string }>('/runs/suggest', { method: 'POST', body: JSON.stringify(body) })

export const startWrite = (body: WriteRequest) =>
  request<{ id: string }>('/runs/write', { method: 'POST', body: JSON.stringify(body) })

export const cancelRun = (id: string) =>
  request<{ cancelled: boolean }>(`/runs/${id}/cancel`, { method: 'POST' })

// SSE URL for EventSource. Replays from `after`, then follows live.
export const runEventsUrl = (id: string, after = 0) =>
  `${API_BASE}/runs/${id}/events?after=${after}`

// -- Source reviews ---------------------------------------------------------

export const listSourceReviews = (status?: SourceReviewStatus) =>
  request<SourceReviewSummary[]>(`/source-reviews${status ? `?status=${status}` : ''}`)

export const getSourceReview = (id: number) => request<SourceReview>(`/source-reviews/${id}`)

export const decideSourceReview = (id: number, body: DecideRequest) =>
  request<DecideResult>(`/source-reviews/${id}/decide`, {
    method: 'POST',
    body: JSON.stringify(body),
  })

export const cancelSourceReview = (id: number) =>
  request<{ cancelled: boolean }>(`/source-reviews/${id}/cancel`, { method: 'POST' })

// -- Workflows (the canvas topology) ---------------------------------------

export const getWorkflows = () => request<WorkflowGraph[]>('/workflows')

// -- Config -----------------------------------------------------------------

export const listConfig = () => request<ConfigListItem[]>('/config')

export const getConfig = (name: string) => request<ConfigContent>(`/config/${name}`)

export const putConfig = (name: string, content: string, note = '') =>
  request<{ name: string; version: number; updated_at: string }>(`/config/${name}`, {
    method: 'PUT',
    body: JSON.stringify({ content, note }),
  })

export const configHistory = (name: string) =>
  request<ConfigHistoryItem[]>(`/config/${name}/history`)

export const configVersion = (name: string, version: number) =>
  request<ConfigVersion>(`/config/${name}/versions/${version}`)

export const configRollback = (name: string, version: number) =>
  request<{ name: string; version: number }>(`/config/${name}/rollback/${version}`, {
    method: 'POST',
  })

// -- Drafts -----------------------------------------------------------------

export const listDrafts = () => request<DraftListItem[]>('/drafts')

export const getDraft = (name: string) => request<Draft>(`/drafts/${name}`)

export const putDraft = (name: string, markdown: string) =>
  request<{ saved: boolean }>(`/drafts/${name}`, {
    method: 'PUT',
    body: JSON.stringify({ markdown }),
  })

export const draftCoverUrl = (name: string) => `${API_BASE}/drafts/${name}/cover`

export const publishDraft = (name: string, status = 'draft') =>
  request<Record<string, unknown>>(`/drafts/${name}/publish?status=${status}`, {
    method: 'POST',
  })

// -- Topic ideas ------------------------------------------------------------

export interface TopicIdeaFilters {
  watch_area?: string
  post_format?: string
  has_draft?: boolean
  min_score?: number
  q?: string
}

export const listTopicIdeas = (filters: TopicIdeaFilters = {}) => {
  const query = new URLSearchParams()
  if (filters.watch_area) query.set('watch_area', filters.watch_area)
  if (filters.post_format) query.set('post_format', filters.post_format)
  if (filters.has_draft != null) query.set('has_draft', String(filters.has_draft))
  if (filters.min_score != null) query.set('min_score', String(filters.min_score))
  if (filters.q) query.set('q', filters.q)
  const qs = query.toString()
  return request<TopicIdeaSummary[]>(`/topic-ideas${qs ? `?${qs}` : ''}`)
}

export const getTopicIdea = (id: number) => request<TopicIdea>(`/topic-ideas/${id}`)

// -- Posts & draft versions -------------------------------------------------

export interface PostFilters {
  status?: string
  approved?: boolean
  has_cover?: boolean
  published?: boolean
  q?: string
}

export const listPosts = (filters: PostFilters = {}) => {
  const query = new URLSearchParams()
  if (filters.status) query.set('status', filters.status)
  if (filters.approved != null) query.set('approved', String(filters.approved))
  if (filters.has_cover != null) query.set('has_cover', String(filters.has_cover))
  if (filters.published != null) query.set('published', String(filters.published))
  if (filters.q) query.set('q', filters.q)
  const qs = query.toString()
  return request<PostSummary[]>(`/posts${qs ? `?${qs}` : ''}`)
}

export const getPost = (id: number) => request<Post>(`/posts/${id}`)

export const getPostVersions = (id: number) => request<DraftVersion[]>(`/posts/${id}/versions`)

export const getDraftVersion = (id: number) => request<DraftVersion>(`/draft-versions/${id}`)

export const regeneratePost = (id: number, body: RegenerateRequest) =>
  request<{ id: string; run_id: string }>(`/posts/${id}/regenerate`, {
    method: 'POST',
    body: JSON.stringify(body),
  })

export const regenerateCover = (id: number, instructions: string) =>
  request<{ id: string; run_id: string }>(`/posts/${id}/cover`, {
    method: 'POST',
    body: JSON.stringify({ instructions }),
  })

// -- Web Push ---------------------------------------------------------------

export interface PushSubscriptionBody {
  endpoint: string
  keys: { p256dh: string; auth: string }
}

export const subscribePush = (body: PushSubscriptionBody) =>
  request<{ id: number; subscriptions: number }>('/push/subscribe', {
    method: 'POST',
    body: JSON.stringify(body),
  })

export const unsubscribePush = (body: { endpoint: string }) =>
  request<{ removed: boolean; subscriptions: number }>('/push/unsubscribe', {
    method: 'POST',
    body: JSON.stringify(body),
  })

export const sendTestPush = () =>
  request<{ delivered: number; subscriptions: number }>('/push/test', { method: 'POST' })

// -- News: feeds, groups, articles -------------------------------------------
//
// These sit under /api/news on a second FastAPI router. Same rule as everything
// above: no path may carry a trailing slash, or `redirect: 'manual'` reads the
// resulting 307 as an expired Easy Auth session and bounces to the login page.

export interface FeedFilters {
  enabled?: boolean
  realtime?: boolean
  group_id?: number
  q?: string
}

export const listFeeds = (filters: FeedFilters = {}) => {
  const query = new URLSearchParams()
  if (filters.enabled !== undefined) query.set('enabled', String(filters.enabled))
  if (filters.realtime !== undefined) query.set('realtime', String(filters.realtime))
  if (filters.group_id !== undefined) query.set('group_id', String(filters.group_id))
  if (filters.q) query.set('q', filters.q)
  const qs = query.toString()
  return request<Feed[]>(`/news/feeds${qs ? `?${qs}` : ''}`)
}

export const getFeed = (id: number) => request<Feed>(`/news/feeds/${id}`)

export const createFeed = (body: FeedCreateRequest) =>
  request<Feed>('/news/feeds', { method: 'POST', body: JSON.stringify(body) })

export const updateFeed = (id: number, body: FeedPatchRequest) =>
  request<Feed>(`/news/feeds/${id}`, { method: 'PATCH', body: JSON.stringify(body) })

export const deleteFeed = (id: number, purge = false) =>
  request<{ deleted: boolean }>(`/news/feeds/${id}?purge=${purge}`, { method: 'DELETE' })

export const validateFeed = (url: string) =>
  request<FeedProbe>('/news/feeds/validate', { method: 'POST', body: JSON.stringify({ url }) })

export const refreshFeed = (id: number) =>
  request<{ id: string; run_id: string }>(`/news/feeds/${id}/refresh`, { method: 'POST' })

export const refreshAllFeeds = () =>
  request<{ id: string; run_id: string }>('/news/refresh', { method: 'POST' })

export const listFeedGroups = () => request<FeedGroup[]>('/news/feed-groups')

export const getFeedGroup = (id: number) => request<FeedGroup>(`/news/feed-groups/${id}`)

export const createFeedGroup = (name: string, description = '') =>
  request<FeedGroup>('/news/feed-groups', {
    method: 'POST',
    body: JSON.stringify({ name, description }),
  })

export const deleteFeedGroup = (id: number) =>
  request<{ deleted: boolean }>(`/news/feed-groups/${id}`, { method: 'DELETE' })

export const setFeedGroupFeeds = (id: number, feed_ids: number[]) =>
  request<FeedGroup>(`/news/feed-groups/${id}/feeds`, {
    method: 'PUT',
    body: JSON.stringify({ feed_ids }),
  })

export const listArticles = (filters: ArticleFilters = {}) => {
  const query = new URLSearchParams()
  if (filters.group_id !== undefined) query.set('group_id', String(filters.group_id))
  if (filters.feed_id !== undefined) query.set('feed_id', String(filters.feed_id))
  if (filters.since) query.set('since', filters.since)
  if (filters.q) query.set('q', filters.q)
  if (filters.limit) query.set('limit', String(filters.limit))
  const qs = query.toString()
  return request<Article[]>(`/news/articles${qs ? `?${qs}` : ''}`)
}

export const getNewsSummary = () => request<NewsSummary>('/news/summary')

export type { RunEvent }
