import { Suspense, useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { NavLink, Outlet } from 'react-router-dom'
import { getHealth, listSourceReviews } from '../api/client'
import type { Health } from '../api/types'
import { useOnline } from '../hooks/useOnline'
import { applyUpdate, registerSW } from '../lib/registerSW'
import { ChunkErrorBoundary } from './ChunkErrorBoundary'

const NAV = [
  { to: '/runs', label: 'Runs', short: 'Runs', icon: '▷' },
  { to: '/topic-ideas', label: 'Topic Ideas', short: 'Ideas', icon: '◆' },
  { to: '/source-reviews', label: 'Sources', short: 'Sources', icon: '⌖' },
  { to: '/drafts', label: 'Drafts', short: 'Drafts', icon: '✎' },
  { to: '/config', label: 'Config', short: 'Config', icon: '⚙' },
]

function navClass({ isActive }: { isActive: boolean }): string {
  const base =
    'flex min-h-11 items-center gap-3 rounded-lg px-3 text-sm font-medium transition-colors'
  return isActive
    ? `${base} bg-accent/15 text-accent`
    : `${base} text-slate-400 hover:bg-slate-800/60 hover:text-slate-200`
}

// The tap target is the whole 56px column, not the glyph — a thumb aims at a
// region, and a fifth of the screen width is the region it gets.
function tabClass({ isActive }: { isActive: boolean }): string {
  const base = 'flex min-h-14 flex-col items-center justify-center gap-0.5 px-1 transition-colors'
  return isActive ? `${base} text-accent` : `${base} text-slate-500 active:text-slate-300`
}

function ConfigDot({ ok, label }: { ok: boolean; label: string }) {
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-slate-400" title={label}>
      <span className={`h-2 w-2 rounded-full ${ok ? 'bg-emerald-400' : 'bg-slate-600'}`} />
      {label}
    </span>
  )
}

function ConfigDots({ health }: { health: Health }) {
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

// Four labelled dots plus a counter do not fit beside a title at 390px, so on a
// phone the header carries one summary dot that expands the full row beneath it.
// Nothing is hidden, it just costs a tap.
function HealthBar({
  health,
  error,
  open,
  onToggle,
}: {
  health?: Health
  error: boolean
  open: boolean
  onToggle: () => void
}) {
  if (!health) {
    return <span className="text-xs text-slate-500">connecting to server…</span>
  }
  const allOk =
    health.wordpress.configured &&
    health.cover.configured &&
    health.search.configured &&
    health.translation.enabled
  return (
    <>
      <div className="hidden lg:block">
        <ConfigDots health={health} />
      </div>
      <button
        onClick={onToggle}
        aria-expanded={open}
        aria-label="Service status"
        className="flex min-h-11 items-center gap-2 px-2 text-xs text-slate-400 lg:hidden"
      >
        <span
          className={`h-2 w-2 rounded-full ${
            error ? 'bg-rose-400' : allOk ? 'bg-emerald-400' : 'bg-amber-400'
          }`}
        />
        <span className="font-mono text-slate-300">{health.concurrency}</span>
        <span className="text-slate-600">{open ? '▴' : '▾'}</span>
      </button>
    </>
  )
}

export function AppShell() {
  const { data: health, isError } = useQuery({
    queryKey: ['health'],
    queryFn: getHealth,
    refetchInterval: 30_000,
  })
  // A pending review is a paid-for run that has stopped until it is answered,
  // so it gets a count in the nav rather than waiting to be stumbled upon.
  const { data: pending } = useQuery({
    queryKey: ['source-reviews', 'pending'],
    queryFn: () => listSourceReviews('pending'),
    refetchInterval: 15_000,
  })
  const pendingCount = pending?.length ?? 0
  const [healthOpen, setHealthOpen] = useState(false)
  const [updateReady, setUpdateReady] = useState(false)
  const online = useOnline()

  useEffect(() => registerSW(() => setUpdateReady(true)), [])

  // TanStack Query already suspends refetchInterval while the document is
  // hidden, which is what we want on a phone. The cost is on the way back: the
  // badge and the health dots would then be up to 15 and 30 seconds stale, and
  // a pending source review sitting unnoticed is the one thing this nav exists
  // to prevent. Refresh exactly those two on return rather than turning
  // refetchOnWindowFocus back on globally.
  const qc = useQueryClient()
  useEffect(() => {
    const onVisible = () => {
      if (document.visibilityState !== 'visible') return
      void qc.invalidateQueries({ queryKey: ['health'] })
      void qc.invalidateQueries({ queryKey: ['source-reviews', 'pending'] })
    }
    document.addEventListener('visibilitychange', onVisible)
    return () => document.removeEventListener('visibilitychange', onVisible)
  }, [qc])

  return (
    <div className="flex h-full flex-col lg:flex-row">
      {/* The desktop sidebar. Below lg it is not rendered at all rather than
          collapsed — the bottom tab bar carries the same five destinations. */}
      <aside className="hidden w-56 shrink-0 flex-col border-r border-slate-800 bg-slate-950/60 p-4 lg:flex">
        <div className="mb-6 px-2">
          <div className="text-sm font-semibold tracking-tight text-slate-100">PPN Blogger</div>
          <div className="text-xs text-slate-500">crew console</div>
        </div>
        <nav className="flex flex-col gap-1">
          {NAV.map((item) => (
            <NavLink key={item.to} to={item.to} className={navClass}>
              <span className="text-accent/70">{item.icon}</span>
              {item.label}
              {item.to === '/source-reviews' && pendingCount > 0 && (
                <span className="ml-auto rounded-full bg-amber-500/20 px-2 text-xs font-semibold text-amber-300">
                  {pendingCount}
                </span>
              )}
            </NavLink>
          ))}
        </nav>
        <div className="mt-auto px-2 text-xs text-slate-600">
          {isError && online ? (
            <span className="text-rose-400">server unreachable</span>
          ) : (
            <span>{health?.language ? `lang: ${health.language}` : ''}</span>
          )}
        </div>
      </aside>

      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
        {/* pt-safe on the header, not the row inside it, so the bar's own
            background extends up behind the status bar rather than leaving a
            bare strip above it.
            This is not optional decoration: `apple-mobile-web-app-status-bar-style`
            is black-translucent, which is what gives an installed app the full
            screen — and it means iOS paints the page *underneath* the clock and
            the battery. Without the inset the title sits directly behind them. */}
        <header className="shrink-0 border-b border-slate-800 bg-slate-950/40 pt-safe">
          <div className="flex h-14 items-center gap-3 px-4 lg:px-6">
            <span className="text-sm font-semibold tracking-tight text-slate-100 lg:hidden">
              PPN Blogger
            </span>
            <div className="ml-auto lg:ml-0">
              <HealthBar
                health={health}
                error={isError}
                open={healthOpen}
                onToggle={() => setHealthOpen((o) => !o)}
              />
            </div>
          </div>
        </header>

        {!online && (
          <div className="shrink-0 border-b border-amber-500/30 bg-amber-500/10 px-4 py-2 text-xs text-amber-300">
            You're offline — showing what was last loaded. Runs resume when you reconnect.
          </div>
        )}

        {updateReady && (
          <div className="flex shrink-0 items-center gap-3 border-b border-accent/30 bg-accent/10 px-4 py-2 text-xs text-accent">
            <span>A new version is ready.</span>
            <button onClick={applyUpdate} className="ml-auto min-h-11 font-semibold underline lg:min-h-0">
              Reload
            </button>
          </div>
        )}

        {healthOpen && health && (
          <div className="shrink-0 border-b border-slate-800 bg-slate-950/60 px-4 py-2 lg:hidden">
            <ConfigDots health={health} />
            {isError && online && (
              <span className="text-xs text-rose-400">server unreachable</span>
            )}
          </div>
        )}

        {/* One Suspense for every route, placed here so the header and tab bar
            stay painted across a chunk load — on a phone a boundary inside the
            route would flash the whole shell away. */}
        <main className="min-h-0 flex-1 overflow-auto overscroll-contain">
          <ChunkErrorBoundary>
            <Suspense fallback={<p className="p-6 text-sm text-slate-500">loading…</p>}>
              <Outlet />
            </Suspense>
          </ChunkErrorBoundary>
        </main>

        {/* An ordinary flex child rather than `position: fixed` — so <main> can
            never be obscured by it and no padding has to track this bar's
            height. pb-safe clears the home indicator. */}
        <nav className="shrink-0 border-t border-slate-800 bg-slate-950/95 pb-safe backdrop-blur lg:hidden">
          <ul className="flex">
            {NAV.map((item) => (
              <li key={item.to} className="flex-1">
                <NavLink to={item.to} className={tabClass} aria-label={item.label}>
                  <span className="relative text-lg leading-none" aria-hidden="true">
                    {item.icon}
                    {item.to === '/source-reviews' && pendingCount > 0 && (
                      <span className="absolute -right-2.5 -top-1 min-w-4 rounded-full bg-amber-500 px-1 text-center text-[10px] font-bold leading-4 text-slate-950">
                        {pendingCount}
                      </span>
                    )}
                  </span>
                  <span className="text-[10px] font-medium">{item.short}</span>
                </NavLink>
              </li>
            ))}
          </ul>
        </nav>
      </div>
    </div>
  )
}
