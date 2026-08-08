import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { cancelRun, getHealth, listRuns } from '../api/client'
import type { Run } from '../api/types'
import { isTerminal } from '../api/types'
import { StatusBadge } from '../components/StatusBadge'
import { StartRunDialog } from '../components/StartRunDialog'
import { useIsDesktop } from '../hooks/useMediaQuery'
import { duration, relativeTime } from '../lib/format'
import { ghostBtn, primaryBtn, rowCard } from '../lib/ui'

export function RunsScreen() {
  const [showStart, setShowStart] = useState(false)
  const navigate = useNavigate()
  const qc = useQueryClient()
  const isDesktop = useIsDesktop()

  const { data: runs, isLoading } = useQuery({
    queryKey: ['runs'],
    queryFn: () => listRuns({ limit: 100 }),
    refetchInterval: 2000,
  })
  const { data: health } = useQuery({ queryKey: ['health'], queryFn: getHealth })

  const cancel = useMutation({
    mutationFn: (id: string) => cancelRun(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['runs'] }),
  })

  // Queue position: order the queued runs oldest-first; index+1 is the place in
  // line. This is what makes runs 3 and 4 under a cap of 2 read as "in queue"
  // rather than looking stuck.
  const queuePosition = useMemo(() => {
    const queued = (runs ?? [])
      .filter((r) => r.status === 'queued')
      .sort((a, b) => (a.queued_at ?? '').localeCompare(b.queued_at ?? ''))
    return new Map(queued.map((r, i) => [r.id, i + 1]))
  }, [runs])

  const runningCount = (runs ?? []).filter((r) => r.status === 'running').length
  const queuedCount = (runs ?? []).filter((r) => r.status === 'queued').length

  return (
    <div className="p-4 lg:p-8">
      <div className="mb-6 flex flex-col items-stretch gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-lg font-semibold text-slate-100">Runs</h1>
          <div className="mt-1 flex items-center gap-3 text-xs text-slate-400">
            <span>
              <span className="text-accent">{runningCount}</span> running
              {health && <span className="text-slate-600"> / {health.concurrency} workers</span>}
            </span>
            {queuedCount > 0 && (
              <span className="rounded-full bg-slate-800 px-2 py-0.5 text-slate-300">
                {queuedCount} queued
              </span>
            )}
          </div>
        </div>
        <button onClick={() => setShowStart(true)} className={primaryBtn}>
          + Start run
        </button>
      </div>

      {isLoading ? (
        <p className="text-sm text-slate-500">loading runs…</p>
      ) : runs && runs.length > 0 ? (
        isDesktop ? (
          <div className="overflow-hidden rounded-xl border border-slate-800">
            <table className="w-full text-sm">
              <thead className="bg-slate-900/60 text-left text-xs uppercase tracking-wide text-slate-500">
                <tr>
                  <th className="px-4 py-2.5 font-medium">Status</th>
                  <th className="px-4 py-2.5 font-medium">Kind</th>
                  <th className="px-4 py-2.5 font-medium">Label</th>
                  <th className="px-4 py-2.5 font-medium">When</th>
                  <th className="px-4 py-2.5 font-medium"></th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800">
                {runs.map((run) => (
                  <RunRow
                    key={run.id}
                    run={run}
                    position={queuePosition.get(run.id)}
                    onOpen={() => navigate(`/runs/${run.id}`)}
                    onCancel={() => cancel.mutate(run.id)}
                    cancelling={cancel.isPending && cancel.variables === run.id}
                  />
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <ul className="space-y-2">
            {runs.map((run) => (
              <RunCard
                key={run.id}
                run={run}
                position={queuePosition.get(run.id)}
                onOpen={() => navigate(`/runs/${run.id}`)}
                onCancel={() => cancel.mutate(run.id)}
                cancelling={cancel.isPending && cancel.variables === run.id}
              />
            ))}
          </ul>
        )
      ) : (
        <div className="rounded-xl border border-dashed border-slate-800 p-10 text-center">
          <p className="text-sm text-slate-400">No runs yet.</p>
          <button onClick={() => setShowStart(true)} className="mt-3 text-sm text-accent hover:underline">
            Start the first one →
          </button>
        </div>
      )}

      {showStart && <StartRunDialog onClose={() => setShowStart(false)} />}
    </div>
  )
}

// The same five columns stacked, with Cancel as a sibling of the card rather
// than a child of it: a button nested inside a button is invalid markup, and the
// stopPropagation the table row relies on is unreliable under touch.
function RunCard({
  run,
  position,
  onOpen,
  onCancel,
  cancelling,
}: {
  run: Run
  position?: number
  onOpen: () => void
  onCancel: () => void
  cancelling: boolean
}) {
  const cancellable = run.status === 'queued' || run.status === 'running'
  return (
    <li>
      <button onClick={onOpen} className={rowCard}>
        <div className="flex items-center gap-2">
          <StatusBadge status={run.status} />
          <span className="ml-auto font-mono text-[11px] text-slate-500">{run.kind}</span>
        </div>
        <div className="mt-2 truncate font-medium text-slate-200">{run.label || '—'}</div>
        <div className="mt-1 text-xs text-slate-400">
          <WhenCell run={run} position={position} />
        </div>
      </button>
      {cancellable && (
        <button
          onClick={onCancel}
          disabled={cancelling}
          className={`${ghostBtn} mt-2 w-full hover:border-rose-500/60 hover:text-rose-300`}
        >
          {cancelling ? 'Cancelling…' : 'Cancel'}
        </button>
      )}
    </li>
  )
}

function RunRow({
  run,
  position,
  onOpen,
  onCancel,
  cancelling,
}: {
  run: Run
  position?: number
  onOpen: () => void
  onCancel: () => void
  cancelling: boolean
}) {
  const cancellable = run.status === 'queued' || run.status === 'running'
  return (
    <tr className="cursor-pointer hover:bg-slate-900/40" onClick={onOpen}>
      <td className="px-4 py-3">
        <StatusBadge status={run.status} />
      </td>
      <td className="px-4 py-3 font-mono text-xs text-slate-400">{run.kind}</td>
      <td className="max-w-xs truncate px-4 py-3 text-slate-200">{run.label || '—'}</td>
      <td className="px-4 py-3 text-xs text-slate-400">
        <WhenCell run={run} position={position} />
      </td>
      <td className="px-4 py-3 text-right">
        {cancellable && (
          <button
            onClick={(e) => {
              e.stopPropagation()
              onCancel()
            }}
            disabled={cancelling}
            className="rounded-md border border-slate-700 px-2.5 py-1 text-xs text-slate-300 hover:border-rose-500/60 hover:text-rose-300 disabled:opacity-50"
          >
            {cancelling ? '…' : 'Cancel'}
          </button>
        )}
      </td>
    </tr>
  )
}

function WhenCell({ run, position }: { run: Run; position?: number }) {
  if (run.status === 'queued') {
    return (
      <span>
        queued {relativeTime(run.queued_at)}
        {position != null && <span className="ml-1 text-slate-500">· position {position}</span>}
      </span>
    )
  }
  if (run.status === 'running') {
    return <span className="text-accent">running · {duration(run.started_at, null)}</span>
  }
  if (isTerminal(run.status)) {
    return (
      <span>
        {relativeTime(run.finished_at)}
        {run.started_at && (
          <span className="ml-1 text-slate-500">· took {duration(run.started_at, run.finished_at)}</span>
        )}
      </span>
    )
  }
  return <span>—</span>
}
