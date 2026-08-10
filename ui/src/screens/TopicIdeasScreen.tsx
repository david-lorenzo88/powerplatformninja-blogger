import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { listTopicIdeas } from '../api/client'
import type { TopicIdeaSummary } from '../api/types'
import { ScorePill } from '../components/Pills'
import { BlogSubNav } from '../components/SubNav'
import { useIsDesktop } from '../hooks/useMediaQuery'
import { field, quietBtn, rowCard } from '../lib/ui'

type Drafted = 'all' | 'drafted' | 'undrafted'

export function TopicIdeasScreen() {
  const navigate = useNavigate()
  const isDesktop = useIsDesktop()
  const ideas = useQuery({ queryKey: ['topic-ideas'], queryFn: () => listTopicIdeas() })

  const [q, setQ] = useState('')
  const [watchArea, setWatchArea] = useState('')
  const [postFormat, setPostFormat] = useState('')
  const [drafted, setDrafted] = useState<Drafted>('all')
  const [minScore, setMinScore] = useState(0)
  // Five filter controls is most of a phone screen before a single idea shows,
  // so on mobile they collapse behind a count of how many are actually set.
  const [filtersOpen, setFiltersOpen] = useState(false)
  const activeFilters =
    (q ? 1 : 0) +
    (watchArea ? 1 : 0) +
    (postFormat ? 1 : 0) +
    (drafted !== 'all' ? 1 : 0) +
    (minScore > 0 ? 1 : 0)

  const watchAreas = useMemo(() => distinct(ideas.data, (i) => i.watch_area), [ideas.data])
  const postFormats = useMemo(() => distinct(ideas.data, (i) => i.post_format), [ideas.data])

  const filtered = useMemo(() => {
    const term = q.trim().toLowerCase()
    return (ideas.data ?? []).filter((i) => {
      if (watchArea && i.watch_area !== watchArea) return false
      if (postFormat && i.post_format !== postFormat) return false
      if (drafted === 'drafted' && !i.has_draft) return false
      if (drafted === 'undrafted' && i.has_draft) return false
      if (i.score < minScore) return false
      if (term) {
        const hay = `${i.title} ${i.slug} ${i.primary_keyword}`.toLowerCase()
        if (!hay.includes(term)) return false
      }
      return true
    })
  }, [ideas.data, q, watchArea, postFormat, drafted, minScore])

  return (
    <div className="flex h-full flex-col">
      <div className="shrink-0 border-b border-slate-800 px-4 py-4 lg:px-6">
        <div className="flex items-baseline gap-3">
          <h1 className="text-lg font-semibold text-slate-100">Topic Ideas</h1>
          <span className="text-sm text-slate-500">
            {filtered.length}
            {ideas.data && filtered.length !== ideas.data.length ? ` of ${ideas.data.length}` : ''}
          </span>
          {!isDesktop && (
            <button
              onClick={() => setFiltersOpen((o) => !o)}
              aria-expanded={filtersOpen}
              className={`${quietBtn} ml-auto px-2 text-xs`}
            >
              Filters{activeFilters > 0 && ` · ${activeFilters}`} {filtersOpen ? '▴' : '▾'}
            </button>
          )}
        </div>
        <div className="mt-3">
          <BlogSubNav />
        </div>
        <div
          className={`mt-3 grid-cols-2 gap-2 lg:flex lg:flex-wrap lg:items-center ${
            isDesktop || filtersOpen ? 'grid' : 'hidden'
          }`}
        >
          <input
            className={`${field} col-span-2 lg:col-span-1 lg:w-56`}
            placeholder="Search title, slug, keyword…"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
          <select
            className={`${field} lg:w-auto`}
            value={watchArea}
            onChange={(e) => setWatchArea(e.target.value)}
          >
            <option value="">All areas</option>
            {watchAreas.map((a) => (
              <option key={a} value={a}>
                {a}
              </option>
            ))}
          </select>
          <select
            className={`${field} lg:w-auto`}
            value={postFormat}
            onChange={(e) => setPostFormat(e.target.value)}
          >
            <option value="">All formats</option>
            {postFormats.map((f) => (
              <option key={f} value={f}>
                {f}
              </option>
            ))}
          </select>
          <select
            className={`${field} lg:w-auto`}
            value={drafted}
            onChange={(e) => setDrafted(e.target.value as Drafted)}
          >
            <option value="all">Drafted & not</option>
            <option value="drafted">Drafted</option>
            <option value="undrafted">Not drafted</option>
          </select>
          <label className="col-span-2 flex min-h-11 items-center gap-2 text-xs text-slate-400 lg:col-span-1 lg:min-h-0">
            min score
            <input
              type="range"
              min={0}
              max={100}
              value={minScore}
              onChange={(e) => setMinScore(Number(e.target.value))}
              className="accent-[#c084fc]"
            />
            <span className="w-6 font-mono text-slate-300">{minScore}</span>
          </label>
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-auto">
        {ideas.isLoading ? (
          <p className="p-6 text-sm text-slate-500">loading…</p>
        ) : filtered.length === 0 ? (
          <p className="p-6 text-sm text-slate-500">
            {ideas.data?.length ? 'No ideas match these filters.' : 'No topic ideas yet. Run a suggest.'}
          </p>
        ) : isDesktop ? (
          <table className="w-full border-collapse text-sm">
            <thead className="sticky top-0 bg-slate-950/90 text-left text-xs uppercase tracking-wide text-slate-500 backdrop-blur">
              <tr>
                <th className="px-6 py-2 font-medium">Title</th>
                <th className="px-3 py-2 font-medium">Score</th>
                <th className="px-3 py-2 font-medium">Area</th>
                <th className="px-3 py-2 font-medium">Format</th>
                <th className="px-3 py-2 font-medium">Draft</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((i) => (
                <IdeaRow key={i.id} idea={i} onOpen={() => navigate(`/topic-ideas/${i.id}`)} />
              ))}
            </tbody>
          </table>
        ) : (
          <ul className="space-y-2 p-4">
            {filtered.map((i) => (
              <li key={i.id}>
                <IdeaCard idea={i} onOpen={() => navigate(`/topic-ideas/${i.id}`)} />
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  )
}

function IdeaCard({ idea, onOpen }: { idea: TopicIdeaSummary; onOpen: () => void }) {
  return (
    <button onClick={onOpen} className={rowCard}>
      <div className="flex items-start gap-2">
        <span className="min-w-0 flex-1 font-medium text-slate-200">{idea.title || idea.slug}</span>
        <ScorePill score={idea.score} />
      </div>
      {idea.primary_keyword && (
        <div className="mt-0.5 truncate text-xs text-slate-500">{idea.primary_keyword}</div>
      )}
      <div className="mt-2 flex flex-wrap items-center gap-1.5 text-[11px] text-slate-400">
        {idea.watch_area && <span className="rounded bg-slate-800 px-1.5 py-0.5">{idea.watch_area}</span>}
        {idea.post_format && <span className="rounded bg-slate-800 px-1.5 py-0.5">{idea.post_format}</span>}
        {idea.has_draft && (
          <span className="rounded bg-emerald-500/15 px-1.5 py-0.5 text-emerald-300">drafted</span>
        )}
      </div>
    </button>
  )
}

function IdeaRow({ idea, onOpen }: { idea: TopicIdeaSummary; onOpen: () => void }) {
  return (
    <tr
      onClick={onOpen}
      className="cursor-pointer border-b border-slate-800/70 hover:bg-slate-800/30"
    >
      <td className="px-6 py-3">
        <div className="font-medium text-slate-200">{idea.title || idea.slug}</div>
        {idea.primary_keyword && (
          <div className="mt-0.5 text-xs text-slate-500">{idea.primary_keyword}</div>
        )}
      </td>
      <td className="px-3 py-3">
        <ScorePill score={idea.score} />
      </td>
      <td className="px-3 py-3 text-slate-400">{idea.watch_area || '—'}</td>
      <td className="px-3 py-3 text-slate-400">{idea.post_format || '—'}</td>
      <td className="px-3 py-3">
        {idea.has_draft ? (
          <span className="rounded bg-emerald-500/15 px-1.5 py-0.5 text-[11px] text-emerald-300">
            drafted
          </span>
        ) : (
          <span className="text-[11px] text-slate-600">—</span>
        )}
      </td>
    </tr>
  )
}


function distinct(
  items: TopicIdeaSummary[] | undefined,
  pick: (i: TopicIdeaSummary) => string,
): string[] {
  const seen = new Set<string>()
  for (const i of items ?? []) {
    const v = pick(i)
    if (v) seen.add(v)
  }
  return [...seen].sort()
}
