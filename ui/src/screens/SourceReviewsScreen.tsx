// Every source review, pending first. A pending one is a run waiting on you:
// the sweep has already been paid for and stops until it is answered.

import { useQuery } from '@tanstack/react-query'
import { Link, useNavigate } from 'react-router-dom'
import { listSourceReviews } from '../api/client'
import { StatusChip } from './SourceReviewScreen'

export function SourceReviewsScreen() {
  const navigate = useNavigate()
  const reviews = useQuery({
    queryKey: ['source-reviews'],
    queryFn: () => listSourceReviews(),
    refetchInterval: 10_000,
  })

  const rows = reviews.data ?? []
  const pending = rows.filter((r) => r.status === 'pending')

  return (
    <div className="flex h-full flex-col">
      <div className="shrink-0 border-b border-slate-800 px-6 py-4">
        <div className="flex items-baseline gap-3">
          <h1 className="text-lg font-semibold text-slate-100">Source Reviews</h1>
          <span className="text-sm text-slate-500">
            {pending.length} waiting on you · {rows.length} total
          </span>
        </div>
        <p className="mt-1 max-w-3xl text-xs text-slate-500">
          Start a run with <span className="text-slate-300">Search the whole web</span> and the
          scouts range beyond the curated feeds. They stop here, with every site they read, so you
          can decide which ones the blog is allowed to use.
        </p>
      </div>

      <div className="min-h-0 flex-1 overflow-auto px-6 py-4">
        {reviews.isLoading && <p className="text-sm text-slate-500">Loading…</p>}
        {!reviews.isLoading && rows.length === 0 && (
          <p className="text-sm text-slate-500">
            No source reviews yet. Start one from{' '}
            <Link className="text-accent hover:underline" to="/runs">
              Runs
            </Link>
            .
          </p>
        )}
        <ul className="space-y-2">
          {rows.map((review) => (
            <li key={review.id}>
              <button
                onClick={() => navigate(`/source-reviews/${review.id}`)}
                className="w-full rounded-lg border border-slate-800 bg-slate-950/40 px-4 py-3 text-left hover:border-slate-600"
              >
                <div className="flex flex-wrap items-center gap-3">
                  <span className="text-sm font-semibold text-slate-200">#{review.id}</span>
                  <StatusChip status={review.status} />
                  <span className="text-xs text-slate-400">
                    {review.candidate_count} sites · {review.new_count} new ·{' '}
                    {review.signal_count} signals
                  </span>
                  <span className="ml-auto text-xs text-slate-600">
                    {review.created_at?.slice(0, 16).replace('T', ' ')}
                  </span>
                </div>
                {review.instruction && (
                  <p className="mt-1 truncate text-xs text-slate-500">“{review.instruction}”</p>
                )}
              </button>
            </li>
          ))}
        </ul>
      </div>
    </div>
  )
}
