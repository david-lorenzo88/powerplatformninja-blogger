import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { getUsageRollup } from '../api/client'
import type { UsageBucket } from '../api/types'
import { formatCost, formatTokens, relativeTime } from '../lib/format'
import { card, field } from '../lib/ui'

// What the crew has cost over time.
//
// Only runs driven through the server are here: a CLI run prints its own figure
// and stores nothing, so this deliberately says so rather than letting an empty
// week read as a quiet one.

const WINDOWS = [
  { label: 'Last 7 days', hours: 24 * 7 },
  { label: 'Last 30 days', hours: 24 * 30 },
  { label: 'Last 90 days', hours: 24 * 90 },
  { label: 'All time', hours: 0 },
]

export function SpendScreen() {
  const [hours, setHours] = useState(24 * 30)
  const [groupBy, setGroupBy] = useState<'day' | 'kind'>('day')

  const rollup = useQuery({
    queryKey: ['usage-rollup', hours, groupBy],
    // The server takes a bare number of hours as well as an ISO timestamp,
    // which sidesteps encoding a `+00:00` offset into a query string.
    queryFn: () => getUsageRollup({ since: hours ? String(hours) : undefined, group_by: groupBy }),
  })

  const buckets = rollup.data?.buckets ?? []
  const top = rollup.data?.top_runs ?? []
  const total = buckets.reduce((sum, b) => sum + b.cost_micros, 0)
  const tokens = buckets.reduce((sum, b) => sum + b.total_tokens, 0)
  const currency = rollup.data?.currency ?? ''

  return (
    <div className="space-y-4 p-4 lg:p-6">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="text-lg font-semibold text-slate-100">Spend</h1>
        <div className="ml-auto flex flex-wrap gap-2">
          <select
            value={hours}
            onChange={(e) => setHours(Number(e.target.value))}
            className={`${field} lg:w-40`}
          >
            {WINDOWS.map((w) => (
              <option key={w.label} value={w.hours}>
                {w.label}
              </option>
            ))}
          </select>
          <select
            value={groupBy}
            onChange={(e) => setGroupBy(e.target.value as 'day' | 'kind')}
            className={`${field} lg:w-36`}
          >
            <option value="day">By day</option>
            <option value="kind">By run kind</option>
          </select>
        </div>
      </div>

      {rollup.isLoading ? (
        <p className="text-sm text-slate-500">loading…</p>
      ) : buckets.length === 0 ? (
        <div className={`${card} p-6 text-sm text-slate-400`}>
          <p className="font-medium text-slate-300">Nothing recorded in this window.</p>
          <p className="mt-1 text-xs">
            Only runs started from here are stored. A run launched with the{' '}
            <span className="font-mono">ppn</span> CLI prints its own cost and keeps no row.
          </p>
        </div>
      ) : (
        <>
          <div className={`${card} p-4`}>
            <div className="flex flex-wrap items-baseline gap-x-6 gap-y-1">
              <div>
                <div className="text-2xl font-semibold text-slate-100">
                  {formatCost(total, currency)}
                </div>
                <div className="text-xs text-slate-500">estimated, at list price</div>
              </div>
              <div className="text-sm text-slate-400">
                {formatTokens(tokens)} tokens over{' '}
                {buckets.reduce((sum, b) => sum + b.records, 0)} model calls
              </div>
            </div>
          </div>

          <BucketChart buckets={buckets} currency={currency} />

          {top.length > 0 && (
            <div className={card}>
              <h2 className="border-b border-slate-800 px-4 py-2.5 text-xs font-semibold uppercase tracking-wide text-slate-500">
                Priciest runs
              </h2>
              <ul className="divide-y divide-slate-800">
                {top.map((run) => (
                  <li key={run.run_id}>
                    <Link
                      to={`/runs/${run.run_id}`}
                      className="flex items-baseline gap-3 px-4 py-2.5 text-sm hover:bg-slate-900/40"
                    >
                      <span className="w-20 shrink-0 tabular-nums text-slate-200">
                        {formatCost(run.cost_micros, currency)}
                      </span>
                      <span className="w-16 shrink-0 font-mono text-xs text-slate-500">
                        {run.kind}
                      </span>
                      <span className="truncate text-slate-300">{run.label || '—'}</span>
                      <span className="ml-auto shrink-0 text-xs text-slate-500">
                        {relativeTime(run.finished_at)}
                      </span>
                    </Link>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </>
      )}

      <p className="text-[11px] text-slate-500">
        Counts are exact; the money is those counts times the rates in the{' '}
        <Link to="/config" className="text-accent hover:underline">
          model_prices
        </Link>{' '}
        document. No reservation or discount is applied, so this is a list-price estimate — Azure
        Cost Management remains the bill.
      </p>
    </div>
  )
}

// Horizontal bars built from divs. A chart library would be ~40KB added to a
// bundle that already lazy-loads React Flow and CodeMirror, to draw ten bars.
function BucketChart({ buckets, currency }: { buckets: UsageBucket[]; currency: string }) {
  const peak = Math.max(...buckets.map((b) => b.cost_micros), 1)
  return (
    <div className={`${card} divide-y divide-slate-800`}>
      {buckets.map((bucket) => (
        <div key={bucket.key} className="flex items-center gap-3 px-4 py-2">
          <span className="w-24 shrink-0 font-mono text-xs text-slate-400">{bucket.key}</span>
          <div className="h-4 flex-1 overflow-hidden rounded bg-slate-800/60">
            <div
              className="h-full rounded bg-accent/60"
              style={{ width: `${Math.max(1, Math.round((bucket.cost_micros / peak) * 100))}%` }}
            />
          </div>
          <span className="w-20 shrink-0 text-right text-xs tabular-nums text-slate-300">
            {formatCost(bucket.cost_micros, currency)}
          </span>
          <span className="hidden w-16 shrink-0 text-right text-xs text-slate-600 lg:inline">
            {formatTokens(bucket.total_tokens)}
          </span>
        </div>
      ))}
    </div>
  )
}
