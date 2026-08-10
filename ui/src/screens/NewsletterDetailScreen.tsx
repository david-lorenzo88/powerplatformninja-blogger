import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate, useParams } from 'react-router-dom'
import {
  deleteNewsletter,
  generateIssue,
  getNewsletter,
  listFeedGroups,
  listIssues,
  previewNewsletter,
  updateNewsletter,
} from '../api/client'
import type { NewsletterSummary, ScheduleKind } from '../api/types'
import { useOnline } from '../hooks/useOnline'
import { formatTime, relativeTime, relativeToNow } from '../lib/format'
import { card, field, ghostBtn, label, primaryBtn } from '../lib/ui'
import { IssueStatusChip } from './NewslettersScreen'

const DAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']

export function NewsletterDetailScreen() {
  const { id } = useParams()
  const letterId = Number(id)
  const navigate = useNavigate()
  const online = useOnline()
  const qc = useQueryClient()

  const letter = useQuery({
    queryKey: ['newsletter', letterId],
    queryFn: () => getNewsletter(letterId),
    enabled: Number.isFinite(letterId),
  })
  const groups = useQuery({ queryKey: ['feed-groups'], queryFn: listFeedGroups })
  const issues = useQuery({
    queryKey: ['issues', letterId],
    queryFn: () => listIssues(letterId),
    enabled: Number.isFinite(letterId),
  })
  // Free: no model is called, so it can refresh as often as the page likes.
  const preview = useQuery({
    queryKey: ['newsletter-preview', letterId],
    queryFn: () => previewNewsletter(letterId),
    enabled: Number.isFinite(letterId),
  })

  const patch = useMutation({
    mutationFn: (changes: Partial<NewsletterSummary>) => updateNewsletter(letterId, changes),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['newsletter', letterId] })
      qc.invalidateQueries({ queryKey: ['newsletters'] })
      qc.invalidateQueries({ queryKey: ['newsletter-preview', letterId] })
    },
  })

  const generate = useMutation({
    mutationFn: () => generateIssue(letterId),
    onSuccess: (run) => {
      qc.invalidateQueries({ queryKey: ['runs'] })
      navigate(`/runs/${run.id}`)
    },
  })

  const remove = useMutation({
    mutationFn: () => deleteNewsletter(letterId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['newsletters'] })
      navigate('/newsletters')
    },
  })

  if (letter.isLoading) return <p className="p-6 text-sm text-slate-500">loading…</p>
  if (!letter.data) return <p className="p-6 text-sm text-slate-500">No such newsletter.</p>
  const n = letter.data

  return (
    <div className="mx-auto max-w-3xl p-4 lg:p-6">
      <Link to="/newsletters" className="text-xs text-slate-500 hover:text-slate-300">
        ← Newsletters
      </Link>

      <div className="mt-2 flex items-start gap-3">
        <h1 className="flex-1 text-lg font-semibold text-slate-100">{n.name}</h1>
        <label className="flex min-h-11 items-center gap-2 text-xs text-slate-400">
          <input
            type="checkbox"
            className="h-5 w-5 accent-cyan-400 lg:h-4 lg:w-4"
            checked={n.enabled}
            disabled={!online || patch.isPending}
            onChange={(e) => patch.mutate({ enabled: e.target.checked })}
          />
          enabled
        </label>
      </div>
      {n.description && <p className="mt-1 text-sm text-slate-500">{n.description}</p>}

      {/* What the next issue would contain, computed without a model. The
          cheapest possible feedback loop for tuning groups and the window. */}
      <div className={`${card} mt-4 p-4`}>
        <div className="flex items-baseline gap-2">
          <h2 className="text-xs font-semibold uppercase tracking-wide text-slate-500">
            Next issue would use
          </h2>
          <span className="ml-auto text-xs text-slate-600">no model is called</span>
        </div>
        {preview.isLoading ? (
          <p className="mt-2 text-sm text-slate-500">checking…</p>
        ) : preview.data?.reason ? (
          <p className="mt-2 text-sm text-amber-400">{preview.data.reason}</p>
        ) : (
          <>
            <p className="mt-2 text-sm text-slate-300">
              <span className={preview.data?.enough ? 'text-emerald-300' : 'text-amber-400'}>
                {preview.data?.candidates.length ?? 0} article
                {preview.data?.candidates.length === 1 ? '' : 's'}
              </span>{' '}
              in the last {n.lookback_hours}h
              {preview.data?.enough === false && ` — below the minimum of ${n.min_items}, so a run would skip`}
            </p>
            <ul className="mt-2 flex flex-col gap-1">
              {(preview.data?.candidates ?? []).slice(0, 5).map((c) => (
                <li key={c.id} className="truncate text-xs text-slate-500">
                  · {c.title} <span className="text-slate-600">— {c.source}</span>
                </li>
              ))}
            </ul>
          </>
        )}
      </div>

      <ScheduleEditor newsletter={n} onChange={(c) => patch.mutate(c)} busy={!online || patch.isPending} />

      <section className="mt-6">
        <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">
          Feed groups
        </h2>
        <div className="flex flex-wrap gap-2">
          {(groups.data ?? []).length === 0 && (
            <p className="text-sm text-slate-500">
              No groups yet —{' '}
              <Link className="text-accent hover:underline" to="/feed-groups">
                make one
              </Link>
              .
            </p>
          )}
          {(groups.data ?? []).map((g) => {
            const on = n.group_ids.includes(g.id)
            return (
              <button
                key={g.id}
                disabled={!online || patch.isPending}
                onClick={() =>
                  patch.mutate({
                    group_ids: on ? n.group_ids.filter((i) => i !== g.id) : [...n.group_ids, g.id],
                  })
                }
                className={`min-h-11 rounded-lg border px-3 text-sm lg:min-h-0 lg:py-1 ${
                  on
                    ? 'border-accent/40 bg-accent/10 text-accent'
                    : 'border-slate-700 text-slate-400 hover:border-slate-500'
                }`}
              >
                {g.name}
                <span className="ml-1 text-xs text-slate-500">{g.feed_count}</span>
              </button>
            )
          })}
        </div>
      </section>

      <section className="mt-6">
        <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">
          Issues
        </h2>
        {(issues.data ?? []).length === 0 ? (
          <p className="text-sm text-slate-500">None yet.</p>
        ) : (
          <ul className="flex flex-col gap-2">
            {(issues.data ?? []).map((i) => (
              <li key={i.id}>
                <Link to={`/newsletters/issues/${i.id}`} className={`${card} block p-3`}>
                  <div className="flex items-baseline gap-2">
                    <span className="text-sm text-slate-200">
                      #{i.number} {i.subject || '(no subject)'}
                    </span>
                    <IssueStatusChip status={i.status} />
                  </div>
                  <div className="mt-0.5 text-xs text-slate-500">
                    {i.item_count} item{i.item_count === 1 ? '' : 's'}
                    {i.created_at ? ` · ${relativeTime(i.created_at)}` : ''}
                    {i.error ? ` · ${i.error}` : ''}
                  </div>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </section>

      <div className="mt-6 flex flex-col gap-2 pb-safe lg:flex-row lg:items-center">
        <button
          className={primaryBtn}
          disabled={!online || generate.isPending}
          onClick={() => generate.mutate()}
        >
          {generate.isPending ? 'Starting…' : 'Generate an issue now'}
        </button>
        <button
          className={`${ghostBtn} lg:ml-auto`}
          disabled={!online || remove.isPending}
          onClick={() => remove.mutate()}
        >
          Delete newsletter
        </button>
      </div>
      <p className="mt-2 text-xs text-slate-600">
        Generating writes a draft issue for you to read. Nothing is sent to anyone.
      </p>
    </div>
  )
}

function ScheduleEditor({
  newsletter,
  onChange,
  busy,
}: {
  newsletter: NewsletterSummary
  onChange: (changes: Partial<NewsletterSummary>) => void
  busy: boolean
}) {
  const [draft, setDraft] = useState(newsletter)
  useEffect(() => setDraft(newsletter), [newsletter])

  const set = (changes: Partial<NewsletterSummary>) => {
    setDraft({ ...draft, ...changes })
    onChange(changes)
  }

  return (
    <section className={`${card} mt-4 p-4`}>
      <h2 className="mb-3 text-xs font-semibold uppercase tracking-wide text-slate-500">
        Schedule
      </h2>
      <div className="flex flex-col gap-3 lg:flex-row lg:flex-wrap lg:items-end">
        <div>
          <span className={label}>How often</span>
          <select
            className={`${field} lg:w-40`}
            value={draft.schedule_kind}
            disabled={busy}
            onChange={(e) => set({ schedule_kind: e.target.value as ScheduleKind })}
          >
            <option value="manual">Only when I ask</option>
            <option value="interval">Every N hours</option>
            <option value="weekly">Weekly</option>
            <option value="monthly">Monthly</option>
          </select>
        </div>

        {draft.schedule_kind === 'interval' && (
          <div>
            <span className={label}>Hours</span>
            <input
              type="number"
              min={1}
              className={`${field} lg:w-24`}
              value={Math.max(1, Math.round(draft.interval_minutes / 60)) || 24}
              disabled={busy}
              onChange={(e) => set({ interval_minutes: Number(e.target.value) * 60 })}
            />
          </div>
        )}

        {draft.schedule_kind === 'weekly' && (
          <div>
            <span className={label}>Day</span>
            <select
              className={`${field} lg:w-36`}
              value={draft.weekday}
              disabled={busy}
              onChange={(e) => set({ weekday: Number(e.target.value) })}
            >
              {DAYS.map((d, i) => (
                <option key={d} value={i}>
                  {d}
                </option>
              ))}
            </select>
          </div>
        )}

        {draft.schedule_kind === 'monthly' && (
          <div>
            <span className={label}>Day of month</span>
            <input
              type="number"
              min={1}
              max={28}
              className={`${field} lg:w-24`}
              value={draft.day_of_month}
              disabled={busy}
              onChange={(e) => set({ day_of_month: Number(e.target.value) })}
            />
            {/* Capped at 28 on the server too: "the 31st" quietly meaning "the
                28th" in February is a schedule that lies about itself. */}
            <p className="mt-1 text-xs text-slate-600">1–28</p>
          </div>
        )}

        {draft.schedule_kind !== 'manual' && draft.schedule_kind !== 'interval' && (
          <div>
            <span className={label}>At</span>
            <input
              type="time"
              className={`${field} lg:w-32`}
              value={`${String(draft.hour_local).padStart(2, '0')}:${String(draft.minute_local).padStart(2, '0')}`}
              disabled={busy}
              onChange={(e) => {
                const [h, m] = e.target.value.split(':').map(Number)
                set({ hour_local: h || 0, minute_local: m || 0 })
              }}
            />
            <p className="mt-1 text-xs text-slate-600">{draft.timezone}</p>
          </div>
        )}
      </div>

      {/* A schedule nobody can preview is a schedule nobody trusts. */}
      {newsletter.upcoming.length > 0 ? (
        <p className="mt-3 text-xs text-slate-500">
          Next:{' '}
          {newsletter.upcoming.map((t, i) => (
            <span key={t}>
              {i > 0 && ', '}
              <span className="text-slate-300" title={formatTime(t)}>
                {relativeToNow(t)}
              </span>
            </span>
          ))}
        </p>
      ) : (
        <p className="mt-3 text-xs text-slate-600">
          Runs only when you press Generate.
        </p>
      )}
    </section>
  )
}
