import type { FeedHealth, RunStatus } from '../api/types'

// Tailwind classes per run status. One place so badges, rows and the run-detail
// header all colour a status identically.
export const STATUS_STYLES: Record<RunStatus, string> = {
  queued: 'bg-slate-700/40 text-slate-300 ring-slate-500/40',
  running: 'bg-accent/15 text-accent ring-accent/50',
  succeeded: 'bg-emerald-500/15 text-emerald-300 ring-emerald-500/40',
  failed: 'bg-rose-500/15 text-rose-300 ring-rose-500/40',
  cancelled: 'bg-amber-500/15 text-amber-300 ring-amber-500/40',
  interrupted: 'bg-orange-500/15 text-orange-300 ring-orange-500/40',
}

// Same idea for feed health. 'failing' and 'stale' are deliberately different
// colours as well as different words: one is broken, the other is merely quiet,
// and only the second is a judgement call for the operator.
export const HEALTH_STYLES: Record<FeedHealth, string> = {
  ok: 'bg-emerald-500/15 text-emerald-300',
  stale: 'bg-slate-700/40 text-slate-300',
  failing: 'bg-rose-500/15 text-rose-300',
  disabled: 'bg-slate-800 text-slate-500',
}

// The server emits naive UTC timestamps (e.g. "2026-07-28T11:19:34.208040") with
// no timezone designator. `new Date()` would read those as *local* time, throwing
// every duration off by the UTC offset — so append 'Z' when none is present.
export function toDate(iso: string): Date {
  const hasTz = /[zZ]$|[+-]\d{2}:?\d{2}$/.test(iso)
  return new Date(hasTz ? iso : `${iso}Z`)
}

export function formatTime(iso: string | null): string {
  if (!iso) return '—'
  const d = toDate(iso)
  return d.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}

export function relativeTime(iso: string | null, now = Date.now()): string {
  if (!iso) return '—'
  const secs = Math.round((now - toDate(iso).getTime()) / 1000)
  if (secs < 0) return 'just now'
  if (secs < 60) return `${secs}s ago`
  const mins = Math.floor(secs / 60)
  if (mins < 60) return `${mins}m ago`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours}h ago`
  return `${Math.floor(hours / 24)}d ago`
}

// Human duration between two ISO timestamps (or to now if the second is null).
export function duration(startIso: string | null, endIso: string | null, now = Date.now()): string {
  if (!startIso) return '—'
  const end = endIso ? toDate(endIso).getTime() : now
  const secs = Math.max(0, Math.round((end - toDate(startIso).getTime()) / 1000))
  if (secs < 60) return `${secs}s`
  const mins = Math.floor(secs / 60)
  const rem = secs % 60
  if (mins < 60) return `${mins}m ${rem}s`
  const hours = Math.floor(mins / 60)
  return `${hours}h ${mins % 60}m`
}


// relativeTime only looks backwards. A schedule is about the future, so the
// feeds and newsletters screens both need this — hence here rather than in one
// of them.
export function relativeToNow(iso: string, now = Date.now()): string {
  const seconds = Math.round((toDate(iso).getTime() - now) / 1000)
  if (seconds <= 0) return 'due now'
  if (seconds < 90) return 'in under a minute'
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `in ${minutes}m`
  const hours = Math.round(minutes / 60)
  return hours < 48 ? `in ${hours}h` : `in ${Math.round(hours / 24)}d`
}

const DAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']

// One sentence for a newsletter's schedule, matching the four kinds the server
// supports (server/newsletters.py SCHEDULE_KINDS).
export function describeSchedule(n: {
  schedule_kind: string
  interval_minutes: number
  weekday: number
  day_of_month: number
  hour_local: number
  minute_local: number
}): string {
  const at = `${String(n.hour_local).padStart(2, '0')}:${String(n.minute_local).padStart(2, '0')}`
  switch (n.schedule_kind) {
    case 'interval':
      return n.interval_minutes % 60 === 0
        ? `every ${n.interval_minutes / 60}h`
        : `every ${n.interval_minutes}m`
    case 'weekly':
      return `${DAYS[n.weekday] ?? 'Monday'}s at ${at}`
    case 'monthly':
      return `day ${n.day_of_month} at ${at}`
    default:
      return 'manual only'
  }
}
