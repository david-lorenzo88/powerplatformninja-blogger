import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { decideFeedReview, getFeedReview, listFeedGroups } from '../api/client'
import type { FeedCandidate } from '../api/types'
import { StatusChip } from '../components/Pills'
import { useOnline } from '../hooks/useOnline'
import { card, primaryBtn, quietBtn } from '../lib/ui'

interface Choice {
  approved: boolean
  realtime: boolean
  groupIds: number[]
}

export function FeedReviewScreen() {
  const { id } = useParams()
  const reviewId = Number(id)
  const navigate = useNavigate()
  const online = useOnline()
  const qc = useQueryClient()

  const review = useQuery({
    queryKey: ['feed-review', reviewId],
    queryFn: () => getFeedReview(reviewId),
    enabled: Number.isFinite(reviewId),
  })
  const groups = useQuery({ queryKey: ['feed-groups'], queryFn: listFeedGroups })

  // Seeded from the server once, then owned locally until decided — the same
  // shape the source-review screen uses.
  const [choices, setChoices] = useState<Record<string, Choice>>({})
  useEffect(() => {
    if (!review.data) return
    setChoices((current) =>
      Object.keys(current).length
        ? current
        : Object.fromEntries(
            (review.data.candidates ?? []).map((c) => [
              c.url,
              { approved: false, realtime: false, groupIds: [] as number[] },
            ]),
          ),
    )
  }, [review.data])

  const decide = useMutation({
    mutationFn: () =>
      decideFeedReview(reviewId, {
        decisions: (review.data?.candidates ?? []).map((c) => ({
          url: c.url,
          approved: choices[c.url]?.approved ?? false,
          name: c.name,
          topics: c.topics,
          group_ids: choices[c.url]?.groupIds ?? [],
          realtime: choices[c.url]?.realtime ?? false,
        })),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['feed-reviews'] })
      qc.invalidateQueries({ queryKey: ['feeds'] })
      qc.invalidateQueries({ queryKey: ['pending'] })
      navigate('/feeds')
    },
  })

  if (review.isLoading) return <p className="p-6 text-sm text-slate-500">loading…</p>
  if (!review.data) return <p className="p-6 text-sm text-slate-500">No such review.</p>
  const r = review.data
  const settled = r.status !== 'pending'
  const approved = Object.values(choices).filter((c) => c.approved).length

  return (
    <div className="flex h-full flex-col">
      <div className="shrink-0 border-b border-slate-800 px-4 py-4 lg:px-6">
        <Link to="/feeds" className="text-xs text-slate-500 hover:text-slate-300">
          ← Feeds
        </Link>
        <div className="mt-2 flex items-baseline gap-3">
          <h1 className="text-lg font-semibold text-slate-100">Feeds found</h1>
          <StatusChip status={r.status} />
          <span className="text-sm text-slate-500">
            {r.candidate_count} candidate{r.candidate_count === 1 ? '' : 's'}
          </span>
        </div>
        {/* What was asked for. A verdict only means something against the
            question — "is this a good feed?" is unanswerable, "is this what I
            asked for?" is not. */}
        {r.instruction.trim() && (
          <p className="mt-2 max-w-3xl border-l-2 border-slate-700 pl-3 text-sm text-slate-300">
            <span className="text-slate-500">You asked for: </span>
            {r.instruction}
          </p>
        )}
        <p className="mt-2 max-w-3xl text-xs text-slate-500">
          Every one of these was fetched and parsed before it got here — a suggestion that did
          not resolve to a real feed with entries was discarded, so nothing below is a guess.
          Nothing is polled until you approve it.
        </p>
      </div>

      <div className="min-h-0 flex-1 overflow-auto px-4 py-4 lg:px-6">
        {settled ? (
          <p className="text-sm text-slate-500">
            Decided — {r.approved_count} feed{r.approved_count === 1 ? '' : 's'} added.
          </p>
        ) : r.candidate_count === 0 ? (
          <p className="text-sm text-slate-500">
            The sweep found nothing new. Everything it suggested was either already followed,
            already declined, or did not resolve to a real feed.
          </p>
        ) : (
          <ul className="flex flex-col gap-2">
            {(r.candidates ?? []).map((c) => (
              <CandidateRow
                key={c.url}
                candidate={c}
                choice={choices[c.url] ?? { approved: false, realtime: false, groupIds: [] }}
                groups={groups.data ?? []}
                onChange={(next) => setChoices((all) => ({ ...all, [c.url]: next }))}
              />
            ))}
          </ul>
        )}
      </div>

      {!settled && r.candidate_count > 0 && (
        <div className="shrink-0 border-t border-slate-800 px-4 py-3 pb-safe lg:px-6">
          {decide.error && (
            <p className="mb-2 text-xs text-rose-400">{String(decide.error)}</p>
          )}
          <div className="flex flex-col gap-2 lg:flex-row lg:items-center">
            <button
              className={primaryBtn}
              disabled={!online || decide.isPending}
              onClick={() => decide.mutate()}
            >
              {decide.isPending
                ? 'Saving…'
                : approved === 0
                  ? 'Decline all'
                  : `Follow ${approved} feed${approved === 1 ? '' : 's'}`}
            </button>
            <p className="text-xs text-slate-600">
              Anything you leave unchecked is declined, and will not be offered again.
            </p>
          </div>
        </div>
      )}
    </div>
  )
}

function CandidateRow({
  candidate,
  choice,
  groups,
  onChange,
}: {
  candidate: FeedCandidate
  choice: Choice
  groups: { id: number; name: string }[]
  onChange: (next: Choice) => void
}) {
  const [open, setOpen] = useState(false)

  return (
    <li
      className={`${card} p-4 ${
        choice.approved ? 'border-accent/40 bg-accent/5' : 'border-slate-800 bg-slate-950/40'
      }`}
    >
      <label className="flex cursor-pointer items-start gap-3">
        <input
          type="checkbox"
          className="mt-0.5 h-6 w-6 shrink-0 accent-cyan-400 lg:h-4 lg:w-4"
          checked={choice.approved}
          onChange={(e) => onChange({ ...choice, approved: e.target.checked })}
        />
        <span className="min-w-0 flex-1">
          <span className="block text-sm font-medium text-slate-200">{candidate.name}</span>
          <span className="block break-all text-xs text-slate-500">{candidate.url}</span>
          <span className="mt-1 block text-xs text-slate-400">{candidate.reason}</span>
          <span className="mt-1 block text-xs text-slate-600">
            {candidate.entry_count} entries
            {candidate.newest ? ` · newest ${candidate.newest.slice(0, 10)}` : ''}
            {candidate.topics.length ? ` · ${candidate.topics.join(', ')}` : ''}
          </span>
        </span>
      </label>

      {choice.approved && (
        <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-slate-800 pt-3">
          <span className="text-xs text-slate-500">Add to:</span>
          {groups.length === 0 && (
            <span className="text-xs text-slate-600">no groups yet</span>
          )}
          {groups.map((g) => {
            const on = choice.groupIds.includes(g.id)
            return (
              <button
                key={g.id}
                onClick={() =>
                  onChange({
                    ...choice,
                    groupIds: on
                      ? choice.groupIds.filter((i) => i !== g.id)
                      : [...choice.groupIds, g.id],
                  })
                }
                className={`min-h-11 rounded-lg border px-3 text-xs lg:min-h-0 lg:py-1 ${
                  on
                    ? 'border-accent/40 bg-accent/10 text-accent'
                    : 'border-slate-700 text-slate-400'
                }`}
              >
                {g.name}
              </button>
            )
          })}
          <label className="ml-auto flex min-h-11 items-center gap-2 text-xs text-slate-400 lg:min-h-0">
            <input
              type="checkbox"
              className="h-5 w-5 accent-cyan-400 lg:h-4 lg:w-4"
              checked={choice.realtime}
              onChange={(e) => onChange({ ...choice, realtime: e.target.checked })}
            />
            watch closely
          </label>
        </div>
      )}

      {candidate.sample_titles.length > 0 && (
        <>
          <button className={`${quietBtn} mt-1 px-0 text-xs`} onClick={() => setOpen((o) => !o)}>
            {open ? 'Hide' : 'Show'} what it published {open ? '▴' : '▾'}
          </button>
          {open && (
            <ul className="mt-1 flex flex-col gap-0.5">
              {candidate.sample_titles.map((title) => (
                <li key={title} className="truncate text-xs text-slate-500">
                  · {title}
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </li>
  )
}

export function FeedReviewsScreen() {
  const navigate = useNavigate()
  const reviews = useQuery({
    queryKey: ['feed-reviews'],
    queryFn: () => listFeedReviewsAll(),
    refetchInterval: 10_000,
  })

  return (
    <div className="flex h-full flex-col">
      <div className="shrink-0 border-b border-slate-800 px-4 py-4 lg:px-6">
        <h1 className="text-lg font-semibold text-slate-100">Feed reviews</h1>
      </div>
      <div className="min-h-0 flex-1 overflow-auto p-4 lg:px-6">
        {(reviews.data ?? []).length === 0 ? (
          <p className="text-sm text-slate-500">
            No sweeps yet. Start one from the <Link className="text-accent" to="/feeds">Feeds</Link>{' '}
            screen.
          </p>
        ) : (
          <ul className="flex flex-col gap-2">
            {(reviews.data ?? []).map((r) => (
              <li key={r.id}>
                <button
                  className={`${card} block w-full p-4 text-left hover:border-slate-600`}
                  onClick={() => navigate(`/feed-reviews/${r.id}`)}
                >
                  <div className="flex items-baseline gap-2">
                    <span className="text-sm text-slate-200">
                      {r.candidate_count} candidate{r.candidate_count === 1 ? '' : 's'}
                    </span>
                    <StatusChip status={r.status} />
                    <span className="ml-auto text-xs text-slate-500">{r.generated_on}</span>
                  </div>
                  {/* The brief is what distinguishes one sweep from another;
                      without it every row reads "12 candidates". */}
                  <p className="mt-1 line-clamp-2 text-xs text-slate-500">
                    {r.instruction.trim() || 'A general sweep across your usual sections.'}
                  </p>
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  )
}

// Declared here rather than imported to keep the client surface small; the
// listing is only used by this screen.
async function listFeedReviewsAll() {
  const { listFeedReviews } = await import('../api/client')
  return listFeedReviews()
}
