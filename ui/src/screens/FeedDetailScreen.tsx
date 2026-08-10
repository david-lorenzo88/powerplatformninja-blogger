import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate, useParams } from 'react-router-dom'
import {
  deleteFeed,
  getFeed,
  listArticles,
  listFeedGroups,
  refreshFeed,
  updateFeed,
} from '../api/client'
import { useOnline } from '../hooks/useOnline'
import { formatTime, relativeTime } from '../lib/format'
import { card, ghostBtn, primaryBtn } from '../lib/ui'
import { HealthChip, RealtimeToggle } from './FeedsScreen'

export function FeedDetailScreen() {
  const { id } = useParams()
  const feedId = Number(id)
  const navigate = useNavigate()
  const online = useOnline()
  const qc = useQueryClient()

  const feed = useQuery({
    queryKey: ['feed', feedId],
    queryFn: () => getFeed(feedId),
    enabled: Number.isFinite(feedId),
  })
  const groups = useQuery({ queryKey: ['feed-groups'], queryFn: listFeedGroups })
  const articles = useQuery({
    queryKey: ['articles', { feed_id: feedId }],
    queryFn: () => listArticles({ feed_id: feedId, limit: 25 }),
    enabled: Number.isFinite(feedId),
  })

  const patch = useMutation({
    mutationFn: (changes: Parameters<typeof updateFeed>[1]) => updateFeed(feedId, changes),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['feed', feedId] })
      qc.invalidateQueries({ queryKey: ['feeds'] })
      qc.invalidateQueries({ queryKey: ['news-summary'] })
    },
  })

  const refresh = useMutation({
    mutationFn: () => refreshFeed(feedId),
    onSuccess: (run) => {
      qc.invalidateQueries({ queryKey: ['runs'] })
      navigate(`/runs/${run.id}`)
    },
  })

  const remove = useMutation({
    mutationFn: () => deleteFeed(feedId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['feeds'] })
      navigate('/feeds')
    },
  })

  if (feed.isLoading) return <p className="p-6 text-sm text-slate-500">loading…</p>
  if (!feed.data) return <p className="p-6 text-sm text-slate-500">No such feed.</p>
  const f = feed.data

  return (
    <div className="mx-auto max-w-3xl p-4 lg:p-6">
      <Link to="/feeds" className="text-xs text-slate-500 hover:text-slate-300">
        ← Feeds
      </Link>

      <div className="mt-2 flex items-start gap-3">
        <h1 className="flex-1 text-lg font-semibold text-slate-100">
          {f.name || f.title || f.url}
        </h1>
        <HealthChip feed={f} />
      </div>
      <a
        href={f.site_url || f.url}
        target="_blank"
        rel="noreferrer noopener"
        className="mt-1 block break-all text-xs text-slate-500 hover:text-accent"
      >
        {f.url}
      </a>

      {f.last_error && (
        <p className="mt-3 rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-xs text-rose-300">
          Last fetch failed: {f.last_error}
          {f.consecutive_failures > 1 && ` (${f.consecutive_failures} times running)`}
        </p>
      )}

      {!f.enabled && (
        <div className="mt-3 rounded-lg border border-slate-700 bg-slate-900/60 px-3 py-2 text-xs text-slate-400">
          This feed is not being polled.{' '}
          <button
            className="font-semibold text-accent hover:underline"
            disabled={!online || patch.isPending}
            onClick={() => patch.mutate({ enabled: true })}
          >
            Turn it back on
          </button>
        </div>
      )}

      <dl className={`${card} mt-4 grid grid-cols-2 gap-x-4 gap-y-3 p-4 text-sm lg:grid-cols-3`}>
        <Fact term="Items held" value={String(f.entry_count)} />
        <Fact term="Latest entry" value={f.last_entry_at ? relativeTime(f.last_entry_at) : '—'} />
        <Fact term="Last checked" value={f.last_checked_at ? relativeTime(f.last_checked_at) : '—'} />
        <Fact term="Next check" value={f.next_poll_at ? formatTime(f.next_poll_at) : 'not scheduled'} />
        <Fact term="Trust tier" value={f.tier} />
        <Fact term="Added" value={f.origin === 'seed' ? 'from sources.yaml' : f.origin} />
      </dl>

      <div className={`${card} mt-4 p-4`}>
        <RealtimeToggle
          checked={f.realtime}
          onChange={(value) => patch.mutate({ realtime: value })}
        />
      </div>

      <section className="mt-6">
        <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">
          Groups
        </h2>
        <div className="flex flex-wrap gap-2">
          {(groups.data ?? []).length === 0 && (
            <p className="text-sm text-slate-500">
              No groups yet — <Link className="text-accent hover:underline" to="/feed-groups">make one</Link>.
            </p>
          )}
          {(groups.data ?? []).map((g) => {
            const on = f.group_ids.includes(g.id)
            return (
              <button
                key={g.id}
                disabled={!online || patch.isPending}
                onClick={() =>
                  patch.mutate({
                    group_ids: on ? f.group_ids.filter((i) => i !== g.id) : [...f.group_ids, g.id],
                  })
                }
                className={`min-h-11 rounded-lg border px-3 text-sm lg:min-h-0 lg:py-1 ${
                  on
                    ? 'border-accent/40 bg-accent/10 text-accent'
                    : 'border-slate-700 text-slate-400 hover:border-slate-500'
                }`}
              >
                {g.name}
              </button>
            )
          })}
        </div>
      </section>

      <section className="mt-6">
        <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">
          Recent items
        </h2>
        {(articles.data ?? []).length === 0 ? (
          <p className="text-sm text-slate-500">Nothing harvested from this feed yet.</p>
        ) : (
          <ul className="flex flex-col gap-2">
            {(articles.data ?? []).map((a) => (
              <li key={a.id} className={`${card} p-3`}>
                <a
                  href={a.url}
                  target="_blank"
                  rel="noreferrer noopener"
                  className="text-sm text-slate-200 hover:text-accent"
                >
                  {a.title || a.url}
                </a>
                <div className="mt-0.5 text-xs text-slate-500">
                  {relativeTime(a.published_at ?? a.fetched_at)}
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>

      <div className="mt-6 flex flex-col gap-2 pb-safe lg:flex-row lg:items-center">
        <button
          className={primaryBtn}
          disabled={!online || refresh.isPending}
          onClick={() => refresh.mutate()}
        >
          {refresh.isPending ? 'Starting…' : 'Fetch this feed now'}
        </button>
        <button
          className={`${ghostBtn} lg:ml-auto`}
          disabled={!online || remove.isPending}
          onClick={() => remove.mutate()}
        >
          Remove feed
        </button>
      </div>
      {/* Removing keeps the articles: a digest that already cited one must go on
          resolving, so dropping a noisy source does not rewrite history. */}
      <p className="mt-2 text-xs text-slate-600">
        Removing stops the polling and keeps everything already harvested.
      </p>
    </div>
  )
}

function Fact({ term, value }: { term: string; value: string }) {
  return (
    <div>
      <dt className="text-xs text-slate-500">{term}</dt>
      <dd className="text-slate-200">{value}</dd>
    </div>
  )
}
