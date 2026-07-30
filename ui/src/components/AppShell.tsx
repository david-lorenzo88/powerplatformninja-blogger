import { useQuery } from '@tanstack/react-query'
import { NavLink, Outlet } from 'react-router-dom'
import { getHealth } from '../api/client'
import type { Health } from '../api/types'

const NAV = [
  { to: '/runs', label: 'Runs', icon: '▷' },
  { to: '/topic-ideas', label: 'Topic Ideas', icon: '◆' },
  { to: '/drafts', label: 'Drafts', icon: '✎' },
  { to: '/config', label: 'Config', icon: '⚙' },
]

function navClass({ isActive }: { isActive: boolean }): string {
  const base =
    'flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors'
  return isActive
    ? `${base} bg-accent/15 text-accent`
    : `${base} text-slate-400 hover:bg-slate-800/60 hover:text-slate-200`
}

function ConfigDot({ ok, label }: { ok: boolean; label: string }) {
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-slate-400" title={label}>
      <span className={`h-2 w-2 rounded-full ${ok ? 'bg-emerald-400' : 'bg-slate-600'}`} />
      {label}
    </span>
  )
}

function HealthBar({ health }: { health?: Health }) {
  if (!health) {
    return <span className="text-xs text-slate-500">connecting to server…</span>
  }
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
      <span className="text-xs text-slate-500">
        concurrency <span className="font-mono text-slate-300">{health.concurrency}</span>
      </span>
      <ConfigDot ok={health.wordpress.configured} label="WordPress" />
      <ConfigDot ok={health.cover.configured} label="Cover" />
      <ConfigDot ok={health.search.configured} label={`Search (${health.search.provider})`} />
      <ConfigDot ok={health.translation.enabled} label="Translation" />
    </div>
  )
}

export function AppShell() {
  const { data: health, isError } = useQuery({
    queryKey: ['health'],
    queryFn: getHealth,
    refetchInterval: 30_000,
  })

  return (
    <div className="flex h-full">
      <aside className="flex w-56 shrink-0 flex-col border-r border-slate-800 bg-slate-950/60 p-4">
        <div className="mb-6 px-2">
          <div className="text-sm font-semibold tracking-tight text-slate-100">PPN Blogger</div>
          <div className="text-xs text-slate-500">crew console</div>
        </div>
        <nav className="flex flex-col gap-1">
          {NAV.map((item) => (
            <NavLink key={item.to} to={item.to} className={navClass}>
              <span className="text-accent/70">{item.icon}</span>
              {item.label}
            </NavLink>
          ))}
        </nav>
        <div className="mt-auto px-2 text-xs text-slate-600">
          {isError ? (
            <span className="text-rose-400">server unreachable</span>
          ) : (
            <span>{health?.language ? `lang: ${health.language}` : ''}</span>
          )}
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-14 shrink-0 items-center border-b border-slate-800 bg-slate-950/40 px-6">
          <HealthBar health={health} />
        </header>
        <main className="min-h-0 flex-1 overflow-auto">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
