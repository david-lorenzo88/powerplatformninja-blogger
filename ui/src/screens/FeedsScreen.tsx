import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import {
  ApiError,
  createFeed,
  getNewsSummary,
  getSchedule,
  listFeedGroups,
  listFeedReviews,
  listFeeds,
  refreshAllFeeds,
  startDiscovery,
  validateFeed,
} from '../api/client'
import type { Feed, FeedHealth, FeedProbe } from '../api/types'
import { Modal } from '../components/Modal'
import { NewsSubNav } from '../components/SubNav'
import { useIsDesktop } from '../hooks/useMediaQuery'
import { useOnline } from '../hooks/useOnline'
import { HEALTH_STYLES, relativeTime, relativeToNow } from '../lib/format'
import { field, ghostBtn, label, primaryBtn, quietBtn, rowCard } from '../lib/ui'

export function FeedsScreen() {
  const navigate = useNavigate()
  const isDesktop = useIsDesktop()
  const online = useOnline()
  const qc = useQueryClient()

  const [q, setQ] = useState('')
  const [health, setHealth] = useState<'all' | FeedHealth>('all')
  const [adding, setAdding] = useState(false)

  const feeds = useQuery({ queryKey: ['feeds'], queryFn: () => listFeeds() })
  const groups = useQuery({ queryKey: ['feed-groups'], queryFn: listFeedGroups })

  // A sweep that has stopped for a verdict is a paid-for run waiting on you, so
  // it gets a banner rather than waiting to be stumbled upon.
  const reviews = useQuery({
    queryKey: ['feed-reviews', 'pending'],
    queryFn: () => listFeedReviews('pending'),
    refetchInterval: 15_000,
  })

  const discover = useMutation({
    mutationFn: () => startDiscovery(),
    onSuccess: (run) => {
      qc.invalidateQueries({ queryKey: ['runs'] })
      navigate(`/runs/${run.id}`)
    },
  })

  const refresh = useMutation({
    mutationFn: refreshAllFeeds,
    onSuccess: (run) => {
      qc.invalidateQueries({ queryKey: ['runs'] })
      navigate(`/runs/${run.id}`)
    },
  })

  const filtered = useMemo(() => {
    const term = q.trim().toLowerCase()
    return (feeds.data ?? []).filter((f) => {
      if (health !== 'all' && f.health !== health) return false
      if (term && !`${f.name} ${f.title} ${f.domain} ${f.url}`.toLowerCase().includes(term)) {
        return false
      }
      return true
    })
  }, [feeds.data, q, health])

  const failing = (feeds.data ?? []).filter((f) => f.health === 'failing').length

  return (
    <div className="flex h-full flex-col">
      <div className="shrink-0 border-b border-slate-800 px-4 py-4 lg:px-6">
        <div className="flex items-baseline gap-3">
          <h1 className="text-lg font-semibold text-slate-100">Feeds</h1>
          <span className="text-sm text-slate-500">
            {filtered.length}
            {feeds.data && filtered.length !== feeds.data.length ? ` of ${feeds.data.length}` : ''}
          </span>
        </div>

        <div className="mt-3">
          <NewsSubNav />
        </div>

        <ScheduleBar />

        {(reviews.data ?? []).length > 0 && (
          <button
            className="mt-3 block w-full rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-left text-xs text-amber-300"
            onClick={() => navigate(`/feed-reviews/${reviews.data![0].id}`)}
          >
            {reviews.data![0].candidate_count} verified feed
            {reviews.data![0].candidate_count === 1 ? '' : 's'} are waiting for your verdict.
          </button>
        )}

        {failing > 0 && (
          // A feed that has quietly started failing is the whole reason this
          // subsystem records HTTP status at all; it should not need looking for.
          <p className="mt-3 rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-xs text-rose-300">
            {failing} feed{failing === 1 ? ' is' : 's are'} failing to fetch.
          </p>
        )}

        <div className="mt-3 flex flex-col gap-2 lg:flex-row lg:flex-wrap lg:items-center">
          <input
            className={`${field} lg:w-56`}
            placeholder="Search feeds…"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
          <select
            className={`${field} lg:w-40`}
            value={health}
            onChange={(e) => setHealth(e.target.value as 'all' | FeedHealth)}
          >
            <option value="all">Any health</option>
            <option value="ok">OK</option>
            <option value="failing">Failing</option>
            <option value="stale">Stale</option>
            <option value="disabled">Disabled</option>
          </select>
          <div className="flex gap-2 lg:ml-auto">
            <button
              className={ghostBtn}
              disabled={!online || refresh.isPending}
              onClick={() => refresh.mutate()}
            >
              {refresh.isPending ? 'Starting…' : 'Fetch now'}
            </button>
            <button
              className={ghostBtn}
              disabled={!online || discover.isPending}
              onClick={() => discover.mutate()}
              title="Sweep for new sources. Every suggestion is fetched and checked before you see it."
            >
              {discover.isPending ? 'Starting…' : 'Find new'}
            </button>
            <button className={primaryBtn} disabled={!online} onClick={() => setAdding(true)}>
              Add feed
            </button>
          </div>
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-auto">
        {feeds.isLoading ? (
          <p className="p-6 text-sm text-slate-500">loading…</p>
        ) : filtered.length === 0 ? (
          <p className="p-6 text-sm text-slate-500">
            {feeds.data?.length ? 'No feeds match these filters.' : 'No feeds yet. Add one.'}
          </p>
        ) : isDesktop ? (
          <table className="w-full text-sm">
            <thead className="sticky top-0 bg-slate-950/80 text-left text-xs uppercase text-slate-500 backdrop-blur">
              <tr>
                <th className="px-6 py-2 font-medium">Feed</th>
                <th className="px-3 py-2 font-medium">Groups</th>
                <th className="px-3 py-2 font-medium">Items</th>
                <th className="px-3 py-2 font-medium">Latest</th>
                <th className="px-3 py-2 font-medium">Health</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((f) => (
                <tr
                  key={f.id}
                  onClick={() => navigate(`/feeds/${f.id}`)}
                  className="cursor-pointer border-t border-slate-800/60 hover:bg-slate-900/50"
                >
                  <td className="px-6 py-2">
                    <div className="font-medium text-slate-200">{f.name || f.title || f.url}</div>
                    <div className="text-xs text-slate-500">{f.domain}</div>
                  </td>
                  <td className="px-3 py-2 text-xs text-slate-400">
                    {groupNames(f, groups.data) || '—'}
                  </td>
                  <td className="px-3 py-2 text-slate-400">{f.entry_count}</td>
                  <td className="px-3 py-2 text-xs text-slate-500">
                    {f.last_entry_at ? relativeTime(f.last_entry_at) : '—'}
                  </td>
                  <td className="px-3 py-2">
                    <HealthChip feed={f} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <ul className="flex flex-col gap-2 p-4">
            {filtered.map((f) => (
              <li key={f.id}>
                <button className={rowCard} onClick={() => navigate(`/feeds/${f.id}`)}>
                  <div className="flex items-start gap-2">
                    <span className="flex-1 text-sm font-medium text-slate-200">
                      {f.name || f.title || f.url}
                    </span>
                    <HealthChip feed={f} />
                  </div>
                  <div className="mt-1 text-xs text-slate-500">
                    {f.domain} · {f.entry_count} item{f.entry_count === 1 ? '' : 's'}
                    {f.last_entry_at ? ` · ${relativeTime(f.last_entry_at)}` : ''}
                  </div>
                  {groupNames(f, groups.data) && (
                    <div className="mt-1 text-xs text-slate-400">{groupNames(f, groups.data)}</div>
                  )}
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>

      {adding && <AddFeedDialog onClose={() => setAdding(false)} />}
    </div>
  )
}

// When the feeds next refresh themselves, and what that cadence costs.
//
// The auto-pause line is not decoration. Azure SQL is serverless with a
// 60-minute idle pause, so watching even one feed closely means it never pauses
// — the difference between near-zero and roughly $150-200/month. That belongs
// on screen next to the setting that causes it, not buried in a config file.
function ScheduleBar() {
  const schedule = useQuery({
    queryKey: ['schedule'],
    queryFn: getSchedule,
    refetchInterval: 60_000,
  })
  if (!schedule.data) return null
  const { enabled, jobs, watched_feeds, scheduled_newsletters, db_can_autopause } = schedule.data
  const fetchJob = jobs.find((j) => j.key === 'fetch')

  return (
    <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-slate-500">
      {enabled ? (
        <span>
          Next fetch{' '}
          <span className="text-slate-300">
            {fetchJob?.next_due_at ? relativeToNow(fetchJob.next_due_at) : 'not scheduled'}
          </span>
        </span>
      ) : (
        <span className="text-slate-600">Automatic fetching is off — use Fetch now.</span>
      )}
      {watched_feeds > 0 && (
        <span>
          <span className="text-slate-300">{watched_feeds}</span> watched closely
        </span>
      )}
      {!db_can_autopause && (
        <span
          className="text-amber-400"
          title={
            watched_feeds > 0
              ? 'A closely-watched feed polls every 15 minutes'
              : 'A scheduled newsletter is checked every 15 minutes'
          }
        >
          database stays awake · ~$150–200/mo
          {watched_feeds === 0 && scheduled_newsletters > 0 ? ' (scheduled newsletter)' : ''}
        </span>
      )}
      {fetchJob?.last_error && <span className="text-rose-400">{fetchJob.last_error}</span>}
    </div>
  )
}


export function HealthChip({ feed }: { feed: Feed }) {
  const text = feed.health === 'failing' && feed.last_status ? `HTTP ${feed.last_status}` : feed.health
  return (
    <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${HEALTH_STYLES[feed.health]}`}>
      {feed.realtime && feed.health === 'ok' ? 'live' : text}
    </span>
  )
}

// The cadence has a bill attached, so the toggle says so rather than leaving it
// in a settings doc: Azure SQL is serverless with a 60-minute auto-pause, and
// anything polling faster than hourly keeps it awake around the clock.
export function RealtimeToggle({
  checked,
  onChange,
}: {
  checked: boolean
  onChange: (value: boolean) => void
}) {
  const summary = useQuery({ queryKey: ['news-summary'], queryFn: getNewsSummary })
  const fast = summary.data?.realtime_interval_minutes ?? 15
  const slow = summary.data?.ingest_interval_minutes ?? 360
  const firstOne = checked && (summary.data?.feeds_realtime ?? 0) === 0

  return (
    <div>
      <label className="flex min-h-11 items-start gap-3 py-1 text-sm text-slate-300">
        <input
          type="checkbox"
          className="mt-0.5 h-6 w-6 shrink-0 accent-cyan-400 lg:h-4 lg:w-4"
          checked={checked}
          onChange={(e) => onChange(e.target.checked)}
        />
        <span>
          Watch this one closely
          <span className="block text-xs text-slate-500">
            Checked every {fast} minutes instead of every {Math.round(slow / 60)} hours, and
            notifies you when it publishes.
          </span>
        </span>
      </label>
      {firstOne && (
        <p className="mt-1 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-300">
          This is the first close-watched feed. Polling more often than hourly stops the database
          idling, which costs roughly $150–200/month more than letting it pause.
        </p>
      )}
    </div>
  )
}

function groupNames(feed: Feed, groups?: { id: number; name: string }[]): string {
  if (!groups) return ''
  return feed.group_ids
    .map((id) => groups.find((g) => g.id === id)?.name)
    .filter(Boolean)
    .join(', ')
}

// Paste, validate, look at what came back, then save. The preview is the point:
// a URL that fetches is not necessarily a feed, and the server refuses to store
// one it could not parse — so showing the operator the entries it found is what
// turns "add feed" from a guess into a confirmation.
function AddFeedDialog({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient()
  const [url, setUrl] = useState('')
  const [name, setName] = useState('')
  const [realtime, setRealtime] = useState(false)
  const [probe, setProbe] = useState<FeedProbe | null>(null)

  const groups = useQuery({ queryKey: ['feed-groups'], queryFn: listFeedGroups })
  const [groupIds, setGroupIds] = useState<number[]>([])

  const check = useMutation({
    mutationFn: () => validateFeed(url.trim()),
    onSuccess: (result) => {
      setProbe(result)
      if (result.ok && !name) setName(result.title ?? '')
    },
  })

  const save = useMutation({
    mutationFn: () =>
      createFeed({
        url: probe?.url ?? url.trim(),
        name: name.trim(),
        realtime,
        group_ids: groupIds,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['feeds'] })
      qc.invalidateQueries({ queryKey: ['feed-groups'] })
      qc.invalidateQueries({ queryKey: ['articles'] })
      onClose()
    },
  })

  const saveError =
    save.error instanceof ApiError ? save.error.message : save.error ? String(save.error) : ''

  return (
    <Modal title="Add a feed" onClose={onClose}>
      <div className="flex flex-col gap-3">
        <div>
          <span className={label}>Feed or site address</span>
          <input
            className={field}
            placeholder="https://simonwillison.net/"
            value={url}
            autoFocus
            onChange={(e) => {
              setUrl(e.target.value)
              setProbe(null)
            }}
          />
          <p className="mt-1 text-xs text-slate-500">
            A site address is fine — we look for its feed.
          </p>
        </div>

        <button
          className={ghostBtn}
          disabled={!url.trim() || check.isPending}
          onClick={() => check.mutate()}
        >
          {check.isPending ? 'Checking…' : 'Check this address'}
        </button>

        {check.error && <p className="text-xs text-rose-400">{String(check.error)}</p>}

        {probe && !probe.ok && <p className="text-xs text-rose-400">{probe.error}</p>}

        {probe?.ok && (
          <div className="rounded-lg border border-emerald-500/30 bg-emerald-500/5 p-3">
            <p className="text-sm font-medium text-slate-200">{probe.title || probe.url}</p>
            <p className="mt-0.5 break-all text-xs text-slate-500">{probe.url}</p>
            <p className="mt-1 text-xs text-emerald-300">
              {probe.entry_count} entries found
              {probe.discovered_from ? ' (discovered from the page you pasted)' : ''}
            </p>
            <ul className="mt-2 flex flex-col gap-1">
              {probe.entries.map((e) => (
                <li key={e.url} className="truncate text-xs text-slate-400">
                  · {e.title || e.url}
                </li>
              ))}
            </ul>
          </div>
        )}

        {probe?.ok && (
          <>
            <div>
              <span className={label}>Name</span>
              <input className={field} value={name} onChange={(e) => setName(e.target.value)} />
            </div>

            {(groups.data ?? []).length > 0 && (
              <div>
                <span className={label}>Groups</span>
                <div className="flex flex-wrap gap-2">
                  {(groups.data ?? []).map((g) => {
                    const on = groupIds.includes(g.id)
                    return (
                      <button
                        key={g.id}
                        onClick={() =>
                          setGroupIds((ids) =>
                            on ? ids.filter((i) => i !== g.id) : [...ids, g.id],
                          )
                        }
                        className={`min-h-11 rounded-lg border px-3 text-sm lg:min-h-0 lg:py-1 ${
                          on
                            ? 'border-accent/40 bg-accent/10 text-accent'
                            : 'border-slate-700 text-slate-400'
                        }`}
                      >
                        {g.name}
                      </button>
                    )
                  })}
                </div>
              </div>
            )}

            <RealtimeToggle checked={realtime} onChange={setRealtime} />
          </>
        )}

        {saveError && <p className="text-xs text-rose-400">{saveError}</p>}

        <div className="mt-1 flex flex-col gap-2 lg:flex-row lg:justify-end">
          <button className={quietBtn} onClick={onClose}>
            Cancel
          </button>
          <button
            className={primaryBtn}
            disabled={!probe?.ok || save.isPending}
            onClick={() => save.mutate()}
          >
            {save.isPending ? 'Adding…' : 'Add feed'}
          </button>
        </div>
      </div>
    </Modal>
  )
}
