import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { listTopicIdeas } from '../api/client'
import type { TopicIdeaSummary } from '../api/types'

const field =
  'rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 text-sm text-slate-200 placeholder:text-slate-600 focus:border-accent focus:outline-none'

type Drafted = 'all' | 'drafted' | 'undrafted'

export function TopicIdeasScreen() {
  const navigate = useNavigate()
  const ideas = useQuery({ queryKey: ['topic-ideas'], queryFn: () => listTopicIdeas() })

  const [q, setQ] = useState('')
  const [watchArea, setWatchArea] = useState('')
  const [postFormat, setPostFormat] = useState('')
  const [drafted, setDrafted] = useState<Drafted>('all')
  const [minScore, setMinScore] = useState(0)

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
      <div className="shrink-0 border-b border-slate-800 px-6 py-4">
        <div className="flex items-baseline gap-3">
          <h1 className="text-lg font-semibold text-slate-100">Topic Ideas</h1>
          <span className="text-sm text-slate-500">
            {filtered.length}
            {ideas.data && filtered.length !== ideas.data.length ? ` of ${ideas.data.length}` : ''}
          </span>
        </div>
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <input
            className={`${field} w-56`}
            placeholder="Search title, slug, keyword…"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
          <select className={field} value={watchArea} onChange={(e) => setWatchArea(e.target.value)}>
            <option value="">All areas</option>
            {watchAreas.map((a) => (
              <option key={a} value={a}>
                {a}
              </option>
            ))}
          </select>
          <select className={field} value={postFormat} onChange={(e) => setPostFormat(e.target.value)}>
            <option value="">All formats</option>
            {postFormats.map((f) => (
              <option key={f} value={f}>
                {f}
              </option>
            ))}
          </select>
          <select
            className={field}
            value={drafted}
            onChange={(e) => setDrafted(e.target.value as Drafted)}
          >
            <option value="all">Drafted & not</option>
            <option value="drafted">Drafted</option>
            <option value="undrafted">Not drafted</option>
          </select>
          <label className="flex items-center gap-2 text-xs text-slate-400">
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
        ) : (
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
        )}
      </div>
    </div>
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

export function ScorePill({ score }: { score: number }) {
  const tone =
    score >= 80
      ? 'bg-emerald-500/15 text-emerald-300'
      : score >= 60
        ? 'bg-amber-500/15 text-amber-300'
        : 'bg-slate-700/40 text-slate-300'
  return (
    <span className={`rounded px-1.5 py-0.5 text-xs font-medium ${tone}`}>{Math.round(score)}</span>
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
