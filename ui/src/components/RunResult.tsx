import type { Run } from '../api/types'

// Compact summary of a finished run's result, shaped per kind. The result dicts
// come from RunManager._dispatch in server/runs.py.
export function RunResult({ run }: { run: Run }) {
  if (run.error) {
    return (
      <p className="mt-2 rounded-lg bg-rose-500/10 px-3 py-2 text-xs text-rose-300">{run.error}</p>
    )
  }
  const r = run.result
  if (!r) return null

  if (run.kind === 'write') {
    const approved = r.approved as boolean | undefined
    const blockers = (r.blockers as string[] | undefined) ?? []
    return (
      <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
        {'title' in r && <span className="text-slate-300">{String(r.title)}</span>}
        {approved != null && (
          <Chip tone={approved ? 'good' : 'warn'}>{approved ? 'approved' : 'not approved'}</Chip>
        )}
        {'score' in r && r.score != null && <Chip tone="neutral">score {String(r.score)}</Chip>}
        {blockers.length > 0 && <Chip tone="warn">{blockers.length} blocker(s)</Chip>}
        {r.post_id != null && <Chip tone="good">post {String(r.post_id)}</Chip>}
        {typeof r.edit_link === 'string' && r.edit_link && (
          <a
            href={r.edit_link}
            target="_blank"
            rel="noreferrer"
            className="text-accent hover:underline"
          >
            open in WordPress ↗
          </a>
        )}
        {typeof r.cover_error === 'string' && r.cover_error && (
          <Chip tone="warn">cover: {r.cover_error}</Chip>
        )}
      </div>
    )
  }

  if (run.kind === 'suggest') {
    const suggestions = (r.suggestions as Array<Record<string, unknown>> | undefined) ?? []
    return (
      <div className="mt-2 text-xs text-slate-400">
        {suggestions.length} suggestion{suggestions.length === 1 ? '' : 's'}
        {suggestions.length > 0 && (
          <span className="text-slate-500">
            {' '}
            · {suggestions.slice(0, 3).map((s) => String(s.title)).join(' · ')}
            {suggestions.length > 3 && ' …'}
          </span>
        )}
      </div>
    )
  }

  return null
}

function Chip({ children, tone }: { children: React.ReactNode; tone: 'good' | 'warn' | 'neutral' }) {
  const cls =
    tone === 'good'
      ? 'bg-emerald-500/15 text-emerald-300'
      : tone === 'warn'
        ? 'bg-amber-500/15 text-amber-300'
        : 'bg-slate-700/50 text-slate-300'
  return <span className={`rounded-full px-2 py-0.5 ${cls}`}>{children}</span>
}
