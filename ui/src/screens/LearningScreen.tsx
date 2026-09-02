import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import {
  getDeltaPair,
  getLearningMetrics,
  listDeltaPairs,
  listLearningCandidates,
  startLearningSweep,
} from '../api/client'
import { DiffView, SectionDiff } from '../components/DiffView'
import { StatusChip } from '../components/Pills'
import { BlogSubNav } from '../components/SubNav'
import { card, ghostBtn, rowCard } from '../lib/ui'

// What the crew wrote, what you published, and what the difference is worth.
//
// Every figure here is arithmetic over stored pairs. No model produces a score,
// deliberately: an LLM quality number would be the thing to optimise, and
// optimising it is how a loop like this learns to game itself. The models on the
// server classify what kind of edit happened; the numbers are counted.

const pct = (value: number) => `${Math.round(value * 100)}%`

function Figure({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className={`${card} p-4`}>
      <div className="text-2xl font-semibold text-slate-100">{value}</div>
      <div className="mt-1 text-sm text-slate-400">{label}</div>
      {hint && <div className="mt-1 text-xs text-slate-500">{hint}</div>}
    </div>
  )
}

export function LearningScreen() {
  const metrics = useQuery({ queryKey: ['learning', 'metrics'], queryFn: getLearningMetrics })
  const pairs = useQuery({ queryKey: ['delta-pairs'], queryFn: () => listDeltaPairs() })
  const candidates = useQuery({
    queryKey: ['learning', 'candidates'],
    queryFn: () => listLearningCandidates(),
  })
  const [sweeping, setSweeping] = useState(false)

  const m = metrics.data
  const captured = (pairs.data ?? []).filter((p) => p.status !== 'awaiting_final')

  return (
    <div className="flex h-full flex-col">
      <div className="shrink-0">
        <BlogSubNav />
      </div>
      <div className="min-h-0 flex-1 space-y-6 overflow-auto p-4">
        <header className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h1 className="text-xl font-semibold text-slate-100">Learning</h1>
            <p className="mt-1 max-w-2xl text-sm text-slate-400">
              Every post you finish is a correction to the crew. This is what those
              corrections add up to — and nothing here changes the configuration until you
              approve it.
            </p>
          </div>
          <button
            type="button"
            className={ghostBtn}
            disabled={sweeping}
            onClick={() => {
              setSweeping(true)
              void startLearningSweep().finally(() => setSweeping(false))
            }}
          >
            {sweeping ? 'Starting…' : 'Look for improvements'}
          </button>
        </header>

        {m && (
          <section className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Figure
              label="Published unchanged"
              value={m.pairs ? pct(m.clean_rate) : '—'}
              hint="The number to watch. It should rise."
            />
            <Figure
              label="Words you change, on average"
              value={m.pairs ? pct(m.mean_edit_rate) : '—'}
              hint="It should fall."
            />
            <Figure label="Pairs captured" value={String(m.pairs)} />
            <Figure
              label="Drafts not yet published"
              value={String(m.awaiting_final)}
              hint="They teach nothing until you publish."
            />
          </section>
        )}

        {m && m.discard_rate > 0.5 && (
          <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-sm text-amber-200">
            {pct(m.discard_rate)} of recent pairs were discarded. Nothing will be proposed
            until that settles — a rule learned from what is left would be learned from noise.
          </div>
        )}

        {m && Object.keys(m.by_section).length > 0 && (
          <section>
            <h2 className="mb-2 text-sm font-semibold text-slate-300">Where your edits land</h2>
            <div className="space-y-1">
              {Object.entries(m.by_section)
                .sort((a, b) => b[1] - a[1])
                .slice(0, 8)
                .map(([name, share]) => (
                  <div key={name} className="flex items-center gap-3 text-sm">
                    <span className="w-48 shrink-0 truncate text-slate-400">{name}</span>
                    <div className="h-2 flex-1 overflow-hidden rounded bg-slate-800">
                      <div
                        className="h-full rounded bg-accent/70"
                        style={{ width: `${Math.round(share * 100)}%` }}
                      />
                    </div>
                    <span className="w-10 shrink-0 text-right text-xs text-slate-500">
                      {pct(share)}
                    </span>
                  </div>
                ))}
            </div>
          </section>
        )}

        <section>
          <h2 className="mb-2 text-sm font-semibold text-slate-300">
            Patterns accruing ({candidates.data?.length ?? 0})
          </h2>
          {!candidates.data?.length ? (
            <p className="text-sm text-slate-500">
              Nothing yet. A correction has to appear in three separate posts before it is
              worth proposing — one post is an opinion, not a habit.
            </p>
          ) : (
            <div className="space-y-2">
              {candidates.data.map((c) => (
                <div key={c.id} className={`${card} p-3`}>
                  <div className="flex flex-wrap items-baseline justify-between gap-2">
                    <span className="text-sm text-slate-200">{c.label}</span>
                    <StatusChip status={c.status} />
                  </div>
                  <div className="mt-1 text-xs text-slate-500">
                    {c.edit_kind} → {c.target} · seen in {c.distinct_posts} post
                    {c.distinct_posts === 1 ? '' : 's'} ({c.occurrences} time
                    {c.occurrences === 1 ? '' : 's'})
                  </div>
                </div>
              ))}
            </div>
          )}
        </section>

        <section>
          <h2 className="mb-2 text-sm font-semibold text-slate-300">
            Posts ({captured.length})
          </h2>
          {!captured.length ? (
            <p className="text-sm text-slate-500">
              A pair is recorded when a write run finishes, and completed when you publish
              the draft.
            </p>
          ) : (
            <div className="space-y-2">
              {[...captured]
                .sort((a, b) => b.edit_rate - a.edit_rate)
                .map((pair) => (
                  <Link key={pair.id} to={`/learning/pairs/${pair.id}`} className={rowCard}>
                    <div className="flex flex-wrap items-baseline justify-between gap-2">
                      <span className="font-medium text-slate-100">
                        {pair.title || pair.slug}
                      </span>
                      <span
                        className={
                          pair.identical
                            ? 'text-sm text-emerald-300'
                            : 'text-sm text-slate-300'
                        }
                      >
                        {pair.identical ? 'published unchanged' : `${pct(pair.edit_rate)} changed`}
                      </span>
                    </div>
                    <div className="mt-1 text-xs text-slate-500">
                      {pair.changed_blocks} of {pair.total_blocks} blocks · {pair.status}
                      {pair.discard_reason ? ` · ${pair.discard_reason}` : ''}
                    </div>
                  </Link>
                ))}
            </div>
          )}
        </section>
      </div>
    </div>
  )
}

export function DeltaPairScreen() {
  const { id } = useParams()
  const pairId = Number(id)
  const [highlight, setHighlight] = useState<string | undefined>()
  const pair = useQuery({
    queryKey: ['delta-pair', pairId],
    queryFn: () => getDeltaPair(pairId),
    enabled: Number.isFinite(pairId),
  })

  if (!pair.data) {
    return <div className="p-4 text-sm text-slate-400">Loading…</div>
  }
  const p = pair.data
  const hunks = p.diff?.hunks ?? []
  const sections = p.diff?.sections ?? []

  return (
    <div className="flex h-full flex-col">
      <header className={`shrink-0 border-b border-slate-800 p-4`}>
        <Link to="/learning" className="text-sm text-slate-400 hover:text-slate-200">
          ← Learning
        </Link>
        <h1 className="mt-2 text-lg font-semibold text-slate-100">{p.title || p.slug}</h1>
        <div className="mt-1 flex flex-wrap gap-3 text-xs text-slate-500">
          <span>{p.status}</span>
          <span>captured from the {p.capture_source === 'in_app' ? 'app' : p.capture_source}</span>
          {p.link && (
            <a href={p.link} target="_blank" rel="noreferrer" className="hover:text-slate-300">
              view post
            </a>
          )}
          {p.edit_link && (
            <a href={p.edit_link} target="_blank" rel="noreferrer" className="hover:text-slate-300">
              edit in WordPress
            </a>
          )}
          <span>
            rules v{p.config.validation_rules ?? '—'} · style v{p.config.style_guide ?? '—'} ·
            profile v{p.config.blog_profile ?? '—'}
          </span>
        </div>
      </header>

      <div className="min-h-0 flex-1 space-y-6 overflow-auto p-4">
        <section className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <Figure
            label="Words changed"
            value={p.identical ? 'none' : pct(p.edit_rate)}
            hint={p.identical ? 'published exactly as written' : undefined}
          />
          <Figure
            label="Vocabulary kept"
            value={pct(p.overlap)}
            hint={p.overlap > 0.8 ? 'a rephrasing' : 'the content itself changed'}
          />
          <Figure label="Blocks touched" value={`${p.changed_blocks}/${p.total_blocks}`} />
          <Figure label="Observations" value={String(p.observations.length)} />
        </section>

        <section>
          <h2 className="mb-2 text-sm font-semibold text-slate-300">Structure</h2>
          <SectionDiff sections={sections} />
        </section>

        {p.observations.length > 0 && (
          <section>
            <h2 className="mb-2 text-sm font-semibold text-slate-300">What this taught</h2>
            <div className="space-y-2">
              {p.observations.map((o) => (
                <button
                  key={o.id}
                  type="button"
                  onClick={() => setHighlight(highlight === o.before ? undefined : o.before)}
                  className={`${rowCard} ${highlight === o.before ? 'border-accent/60' : ''}`}
                >
                  <div className="flex flex-wrap items-baseline justify-between gap-2">
                    <span className="text-sm text-slate-200">{o.signature}</span>
                    <span className="text-xs text-slate-500">
                      {o.edit_kind} → {o.target}
                    </span>
                  </div>
                  {o.rationale && (
                    <div className="mt-1 text-xs text-slate-500">{o.rationale}</div>
                  )}
                </button>
              ))}
            </div>
          </section>
        )}

        <section>
          <h2 className="mb-2 text-sm font-semibold text-slate-300">The changes</h2>
          <DiffView hunks={hunks} highlight={highlight} />
        </section>
      </div>
    </div>
  )
}
