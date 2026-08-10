import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { listArticles, listFeedGroups, refreshAllFeeds } from '../api/client'
import type { Article } from '../api/types'
import { NewsSubNav } from '../components/SubNav'
import { useOnline } from '../hooks/useOnline'
import { relativeTime, toDate } from '../lib/format'
import { card, field, ghostBtn, quietBtn } from '../lib/ui'

const WINDOWS = [
  { value: '', label: 'All time' },
  { value: '24', label: 'Last 24h' },
  { value: '72', label: 'Last 3 days' },
  { value: '168', label: 'Last week' },
]

// Unlike the topic and draft lists, this one filters on the server. Those hold a
// backlog of tens of items and fetch it whole; this one grows without limit, and
// pulling every article ever harvested in order to filter five of them in the
// browser is the thing that would make the screen unusable by month three.
export function ArticlesScreen() {
  const navigate = useNavigate()
  const online = useOnline()
  const qc = useQueryClient()

  const [q, setQ] = useState('')
  const [groupId, setGroupId] = useState<number | ''>('')
  const [since, setSince] = useState('')
  const [filtersOpen, setFiltersOpen] = useState(false)
  const activeFilters = (q ? 1 : 0) + (groupId !== '' ? 1 : 0) + (since ? 1 : 0)

  const groups = useQuery({ queryKey: ['feed-groups'], queryFn: listFeedGroups })
  const articles = useQuery({
    queryKey: ['articles', { q, groupId, since }],
    queryFn: () =>
      listArticles({
        q: q.trim() || undefined,
        group_id: groupId === '' ? undefined : groupId,
        since: since || undefined,
        limit: 200,
      }),
  })

  const refresh = useMutation({
    mutationFn: refreshAllFeeds,
    onSuccess: (run) => {
      qc.invalidateQueries({ queryKey: ['runs'] })
      navigate(`/runs/${run.id}`)
    },
  })

  const grouped = useMemo(() => groupByDay(articles.data ?? []), [articles.data])

  return (
    <div className="flex h-full flex-col">
      <div className="shrink-0 border-b border-slate-800 px-4 py-4 lg:px-6">
        <div className="flex items-baseline gap-3">
          <h1 className="text-lg font-semibold text-slate-100">News</h1>
          <span className="text-sm text-slate-500">
            {articles.data ? `${articles.data.length} article${articles.data.length === 1 ? '' : 's'}` : ''}
          </span>
          <button
            onClick={() => setFiltersOpen((o) => !o)}
            aria-expanded={filtersOpen}
            className={`${quietBtn} ml-auto px-2 text-xs lg:hidden`}
          >
            Filters{activeFilters > 0 && ` · ${activeFilters}`} {filtersOpen ? '▴' : '▾'}
          </button>
        </div>

        <div className="mt-3">
          <NewsSubNav />
        </div>

        <div
          className={`mt-3 grid-cols-2 gap-2 lg:flex lg:flex-wrap lg:items-center ${
            filtersOpen ? 'grid' : 'hidden lg:flex'
          }`}
        >
          <input
            className={`${field} col-span-2 lg:w-64`}
            placeholder="Search headlines and summaries…"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
          <select
            className={`${field} lg:w-44`}
            value={groupId}
            onChange={(e) => setGroupId(e.target.value === '' ? '' : Number(e.target.value))}
          >
            <option value="">All groups</option>
            {(groups.data ?? []).map((g) => (
              <option key={g.id} value={g.id}>
                {g.name}
              </option>
            ))}
          </select>
          <select
            className={`${field} lg:w-36`}
            value={since}
            onChange={(e) => setSince(e.target.value)}
          >
            {WINDOWS.map((w) => (
              <option key={w.value} value={w.value}>
                {w.label}
              </option>
            ))}
          </select>
          <button
            className={`${ghostBtn} col-span-2 lg:ml-auto`}
            disabled={!online || refresh.isPending}
            onClick={() => refresh.mutate()}
          >
            {refresh.isPending ? 'Starting…' : 'Fetch now'}
          </button>
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-auto px-4 py-4 lg:px-6">
        {articles.isLoading ? (
          <p className="text-sm text-slate-500">loading…</p>
        ) : (articles.data ?? []).length === 0 ? (
          <EmptyState filtered={activeFilters > 0} />
        ) : (
          grouped.map(([day, items]) => (
            <section key={day} className="mb-6">
              <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">
                {day}
              </h2>
              <ul className="flex flex-col gap-2">
                {items.map((a) => (
                  <ArticleCard key={a.id} article={a} />
                ))}
              </ul>
            </section>
          ))
        )}
      </div>
    </div>
  )
}

function ArticleCard({ article }: { article: Article }) {
  return (
    <li className={`${card} p-4`}>
      {/* The card is not itself a link: the headline goes to the original and
          the source name filters this list, and a link inside a link is invalid
          markup — the same rule rowCard's comment states. */}
      <a
        href={article.url}
        target="_blank"
        rel="noreferrer noopener"
        className="block text-sm font-semibold text-slate-100 hover:text-accent active:text-accent"
      >
        {article.title || article.url}
      </a>
      {article.summary && (
        <p className="mt-1.5 line-clamp-3 text-sm text-slate-400">{article.summary}</p>
      )}
      <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-500">
        <span className="text-slate-400">{article.feed_name}</span>
        <span>{relativeTime(article.published_at ?? article.fetched_at)}</span>
        {article.author && <span>{article.author}</span>}
        {article.tags.slice(0, 3).map((tag) => (
          <span key={tag} className="rounded bg-slate-800/60 px-1.5 py-0.5 text-slate-400">
            {tag}
          </span>
        ))}
      </div>
    </li>
  )
}

function EmptyState({ filtered }: { filtered: boolean }) {
  if (filtered) {
    return <p className="text-sm text-slate-500">No articles match these filters.</p>
  }
  return (
    <div className="rounded-xl border border-dashed border-slate-800 p-6 text-sm text-slate-500">
      <p>Nothing harvested yet.</p>
      <p className="mt-2">
        Add a source on the{' '}
        <a className="text-accent hover:underline" href="/feeds">
          Feeds
        </a>{' '}
        tab, then press <span className="text-slate-300">Fetch now</span>.
      </p>
    </div>
  )
}

function groupByDay(articles: Article[]): [string, Article[]][] {
  const buckets = new Map<string, Article[]>()
  for (const article of articles) {
    const key = dayLabel(article.published_at ?? article.fetched_at)
    const bucket = buckets.get(key)
    if (bucket) bucket.push(article)
    else buckets.set(key, [article])
  }
  return [...buckets.entries()]
}

function dayLabel(value: string | null): string {
  if (!value) return 'Undated'
  // toDate, not new Date: the server emits naive UTC, which the platform parser
  // would read as local time and shift every heading by the offset.
  const date = toDate(value)
  if (Number.isNaN(date.getTime())) return 'Undated'

  const today = new Date()
  const isSameDay = (a: Date, b: Date) => a.toDateString() === b.toDateString()
  if (isSameDay(date, today)) return 'Today'
  const yesterday = new Date(today)
  yesterday.setDate(today.getDate() - 1)
  if (isSameDay(date, yesterday)) return 'Yesterday'

  return date.toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short' })
}
