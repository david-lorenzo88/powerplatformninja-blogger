// Mirrors the server contract in docs/ARCHITECTURE.md and src/ppn_blogger/server.
// Kept in one place so every screen shares one source of truth for shapes.

// `explore` sweeps the open web and stops at a source review; `shortlist` is the
// second half, run once the operator has approved sources. They are separate
// kinds rather than a flag because each is a different workflow graph, and the
// canvas is keyed by kind.
// `ingest` is the news fetch — no models, but it runs through the same queue so
// a scheduled fetch is visible in the same list as everything else.
export type RunKind =
  | 'suggest'
  | 'explore'
  | 'shortlist'
  | 'write'
  | 'cover'
  | 'ingest'
  | 'newsletter'

export type RunStatus =
  | 'queued'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'cancelled'
  | 'interrupted'

export const TERMINAL: RunStatus[] = ['succeeded', 'failed', 'cancelled', 'interrupted']

export function isTerminal(status: RunStatus): boolean {
  return TERMINAL.includes(status)
}

export interface Health {
  ok: boolean
  language: string
  concurrency: number
  cover: { enabled: boolean; model: string; route: string; configured: boolean }
  wordpress: { configured: boolean; url: string }
  search: { provider: string; configured: boolean }
  translation: { enabled: boolean }
  // public_key is empty when push is unconfigured; the private key never
  // leaves the server.
  push: { configured: boolean; public_key: string }
}

export interface Run {
  id: string
  kind: RunKind
  status: RunStatus
  label: string
  params: Record<string, unknown>
  result: Record<string, unknown> | null
  error: string | null
  config_version: string | null
  queued_at: string | null
  started_at: string | null
  finished_at: string | null
}

// Per-executor status the server derives from the event log (derive_nodes).
// The client re-derives the same shape so canvas and log can never disagree.
export interface NodeState {
  id: string
  events: number
  logs: number
  first_seen: string
  last_seen: string
  status: 'pending' | 'active'
}

export interface RunDetail extends Run {
  nodes: Record<string, NodeState>
  event_count: number
}

export type EventKind = 'status' | 'node' | 'log' | 'eof'

// One row of the run event log (LiveEvent.to_dict), or the synthetic eof frame.
export interface RunEvent {
  seq: number
  kind: EventKind
  executor_id: string
  level: string
  message: string
  data: Record<string, unknown> | null
  ts: string
}

export interface WorkflowGraph {
  kind: RunKind
  title: string
  mermaid: string
  nodes: string[]
}

export interface ConfigListItem {
  name: string
  format: 'yaml' | 'markdown' | string
  version: number
  note: string
  updated_at: string
}

export interface ConfigContent {
  name: string
  format: string
  version: number
  content: string
  updated_at: string
}

export interface ConfigHistoryItem {
  version: number
  note: string
  created_at: string
  size: number
}

export interface ConfigVersion {
  name: string
  version: number
  content: string
  note: string
}

export interface DraftListItem {
  file: string
  title?: string
  slug?: string
  language?: string
  translation_of?: string
  category?: string
  tags?: string[]
  word_count?: number
  read_minutes?: number
  generated?: string
  review?: { approved: boolean; score: number; blockers: number }
  has_review?: boolean
  has_cover?: boolean
  [key: string]: unknown
}

export interface Draft {
  file: string
  front_matter: Record<string, unknown>
  markdown: string
  review: string
}

// Catalog: topic ideas, posts and draft versions (server/catalog.py).

export interface TopicIdeaSummary {
  id: number
  slug: string
  title: string
  watch_area: string
  post_format: string
  primary_keyword: string
  score: number
  audience_fit: number
  timeliness: number
  effort: number
  duplicate_risk: string
  generated_on: string
  created_at: string | null
  has_draft: boolean
  post_id: number | null
  posts: { id: number; slug: string; status: string }[]
}

export interface TopicIdea extends TopicIdeaSummary {
  angle: string
  problem_statement: string
  why_now: string
  novelty: string
  key_questions: string[]
  seed_sources: string[]
  data: Topic
  suggest_run_id: string | null
}

export interface DraftVersion {
  id: number
  post_id: number
  version: number
  write_run_id: string | null
  instructions: string
  reused_research: boolean
  title: string
  slug: string
  approved: boolean
  score: number
  blockers: number
  markdown_path: string
  markdown_file: string
  report_path: string
  cover_path: string
  has_cover: boolean
  dossier_path: string
  wordpress_post_id: number | null
  edit_link: string
  created_at: string | null
}

export interface PostSummary {
  id: number
  slug: string
  title: string
  status: string
  topic_idea_id: number | null
  wordpress_post_id: number | null
  edit_link: string
  link: string
  version_count: number
  current_version: DraftVersion | null
  created_at: string | null
  updated_at: string | null
}

export interface Post extends PostSummary {
  topic_idea: TopicIdeaSummary | null
  versions: DraftVersion[]
}

export interface RegenerateRequest {
  instructions?: string
  reuse_research?: boolean
  push?: boolean | null
  cover?: boolean | null
}

// Source reviews: the approval step in the middle of an exploration run
// (server/reviews.py).

export type SourceReviewStatus = 'pending' | 'approved' | 'cancelled'

export interface SignalItem {
  title: string
  url: string
  published: string | null
  source_name: string | null
  why_it_matters: string
  watch_area: string
}

export interface SourceCandidate {
  domain: string
  name: string
  known: boolean
  current_tier: string
  suggested_tier: string
  item_count: number
  scouts: string[]
  items: SignalItem[]
}

export interface SourceReviewSummary {
  id: number
  run_id: string | null
  status: SourceReviewStatus
  instruction: string
  generated_on: string
  candidate_count: number
  new_count: number
  signal_count: number
  config_version: number | null
  shortlist_run_id: string | null
  created_at: string | null
  decided_at: string | null
}

export interface SourceDecision {
  domain: string
  approved: boolean
  tier?: string
}

export interface TrustTier {
  id: string
  label: string
  score: number
}

export interface SourceReview extends SourceReviewSummary {
  candidates: SourceCandidate[]
  decisions: SourceDecision[]
  // The tier menu comes from the server so the UI never hard-codes a list that
  // lives in sources.yaml.
  tiers: TrustTier[]
}

export interface DecideResult {
  review_id: number
  approved: string[]
  declined: string[]
  config_version: number | null
  run_id: string
}

// Request bodies.
export interface SuggestRequest {
  instruction?: string
  label?: string
  explore?: boolean
}

export interface DecideRequest {
  decisions: SourceDecision[]
  start_shortlist?: boolean
  instruction?: string
  label?: string
}

export interface Topic {
  title: string
  [key: string]: unknown
}

export interface WriteRequest {
  topic: Topic
  push?: boolean | null
  cover?: boolean | null
  translate?: boolean | null
  label?: string
}

// -- News: feeds, groups, articles -------------------------------------------

// ok is the ordinary state. 'failing' means the last fetch errored; 'stale'
// means it fetches perfectly but has published nothing in months — a different
// problem, and only the second is a judgement call for the operator.
export type FeedHealth = 'ok' | 'stale' | 'failing' | 'disabled'

export interface Feed {
  id: number
  url: string
  name: string
  title: string
  site_url: string
  domain: string
  tier: string
  topics: string[]
  enabled: boolean
  realtime: boolean
  origin: string
  group_ids: number[]
  entry_count: number
  poll_interval_minutes: number
  next_poll_at: string | null
  last_checked_at: string | null
  last_success_at: string | null
  last_entry_at: string | null
  last_status: number
  last_error: string
  consecutive_failures: number
  notes: string
  health: FeedHealth
  created_at: string | null
}

export interface FeedGroup {
  id: number
  slug: string
  name: string
  description: string
  feed_count: number
  created_at: string | null
  feed_ids?: number[]
}

export interface Article {
  id: number
  feed_id: number
  feed_name: string
  url: string
  title: string
  author: string
  summary: string
  domain: string
  tier: string
  tags: string[]
  language: string
  published_at: string | null
  fetched_at: string | null
}

export interface FeedProbeEntry {
  title: string
  url: string
  published: string | null
  summary: string
}

export interface FeedProbe {
  ok: boolean
  url: string
  discovered_from?: string
  title?: string
  site_url?: string
  language?: string
  entry_count?: number
  newest?: string | null
  entries: FeedProbeEntry[]
  error: string
}

export interface NewsSummary {
  feeds: number
  feeds_enabled: number
  feeds_failing: number
  feeds_realtime: number
  groups: number
  articles: number
  articles_last_24h: number
  ingest_interval_minutes: number
  realtime_interval_minutes: number
  effective_min_cadence_minutes: number
  // False means the polling cadence is holding Azure SQL awake around the clock.
  db_can_autopause: boolean
}

export interface ScheduleJob {
  key: string
  label: string
  enabled: boolean
  interval_minutes: number
  next_due_at: string | null
  last_finished_at: string | null
  last_status: string
  last_detail: string
  last_error: string
  runs: number
}

export interface Schedule {
  enabled: boolean
  jobs: ScheduleJob[]
  watched_feeds: number
  scheduled_newsletters: number
  effective_min_cadence_minutes: number
  // False means the polling cadence is holding Azure SQL awake around the clock.
  db_can_autopause: boolean
}

export interface FeedCreateRequest {
  url: string
  name?: string
  tier?: string
  topics?: string[]
  realtime?: boolean
  group_ids?: number[]
  notes?: string
}

export interface FeedPatchRequest {
  name?: string
  tier?: string
  topics?: string[]
  enabled?: boolean
  realtime?: boolean
  notes?: string
  poll_interval_minutes?: number
  group_ids?: number[]
}

export interface ArticleFilters {
  group_id?: number
  feed_id?: number
  since?: string
  q?: string
  limit?: number
}


// -- Newsletters --------------------------------------------------------------

export type ScheduleKind = 'manual' | 'interval' | 'weekly' | 'monthly'
export type IssueStatus = 'draft' | 'ready' | 'sending' | 'sent' | 'failed' | 'skipped'

export interface NewsletterSummary {
  id: number
  slug: string
  name: string
  description: string
  enabled: boolean
  schedule_kind: ScheduleKind
  interval_minutes: number
  weekday: number
  day_of_month: number
  hour_local: number
  minute_local: number
  timezone: string
  lookback_hours: number
  max_items: number
  min_items: number
  max_per_feed: number
  audience: string
  tone: string
  auto_send: boolean
  group_ids: number[]
  issue_count: number
  next_due_at: string | null
  last_run_at: string | null
  last_issue_id: number | null
  created_at: string | null
  // The next few fire times, computed server-side. A schedule nobody can
  // preview is a schedule nobody trusts.
  upcoming: string[]
}

export interface NewsletterIssueSummary {
  id: number
  newsletter_id: number
  newsletter_name: string
  run_id: string | null
  number: number
  status: IssueStatus
  subject: string
  preheader: string
  item_count: number
  generated_on: string
  error: string
  window_from: string | null
  window_to: string | null
  created_at: string | null
  sent_at: string | null
}

export interface IssueItem {
  article_id: number
  section: string
  position: number
  headline: string
  blurb: string
  url: string
}

export interface NewsletterIssue extends NewsletterIssueSummary {
  intro: string
  markdown: string
  text_body: string
  items: IssueItem[]
}

export interface NewsletterPreview {
  newsletter_id: number
  window_from: string
  window_to: string
  candidates: { id: number; title: string; url: string; source: string; published: string }[]
  already_used?: number
  min_items?: number
  max_items?: number
  enough?: boolean
  reason?: string
}
