import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { getTopicIdea, startWrite } from '../api/client'
import type { TopicIdea } from '../api/types'
import { Modal } from '../components/Modal'
import { ScorePill } from '../components/Pills'

export function TopicIdeaDetailScreen() {
  const { id } = useParams<{ id: string }>()
  const ideaId = Number(id)
  const idea = useQuery({
    queryKey: ['topic-idea', ideaId],
    queryFn: () => getTopicIdea(ideaId),
    enabled: Number.isFinite(ideaId),
  })

  if (idea.isLoading) return <p className="p-6 text-sm text-slate-500">loading…</p>
  if (idea.isError || !idea.data)
    return (
      <div className="p-6 text-sm text-slate-500">
        <Link to="/topic-ideas" className="text-accent hover:underline">
          ← Topic Ideas
        </Link>
        <p className="mt-4">This topic idea could not be found.</p>
      </div>
    )

  return <IdeaDetail idea={idea.data} />
}

function IdeaDetail({ idea }: { idea: TopicIdea }) {
  const [writing, setWriting] = useState(false)

  return (
    <div className="mx-auto max-w-3xl p-4 lg:p-6">
      <Link to="/topic-ideas" className="text-sm text-accent hover:underline">
        ← Topic Ideas
      </Link>

      <div className="mt-4 flex items-start gap-3">
        <h1 className="flex-1 text-2xl font-semibold text-slate-100">{idea.title || idea.slug}</h1>
        <ScorePill score={idea.score} />
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-slate-500">
        <Chip>{idea.watch_area || 'no area'}</Chip>
        <Chip>{idea.post_format || 'no format'}</Chip>
        {idea.primary_keyword && <Chip>kw: {idea.primary_keyword}</Chip>}
        <span>
          fit {idea.audience_fit}/5 · timeliness {idea.timeliness}/5 · effort {idea.effort}/5
        </span>
      </div>

      {/* Drafted state — the link between an idea and its post. */}
      <div className="mt-5 rounded-xl border border-slate-800 bg-slate-900/40 p-4">
        {idea.has_draft && idea.post_id != null ? (
          <div className="flex flex-col items-stretch gap-3 sm:flex-row sm:items-center sm:justify-between gap-3">
            <p className="text-sm text-slate-300">A draft has been created from this idea.</p>
            <Link
              to={`/drafts/${idea.post_id}`}
              className="rounded-lg border border-slate-700 px-3 py-1.5 text-sm text-slate-200 hover:border-accent"
            >
              View post & versions →
            </Link>
          </div>
        ) : (
          <div className="flex flex-col items-stretch gap-3 sm:flex-row sm:items-center sm:justify-between gap-3">
            <p className="text-sm text-slate-400">No draft yet.</p>
            <button
              onClick={() => setWriting(true)}
              className="rounded-lg bg-accent px-4 py-1.5 text-sm font-semibold text-slate-950 hover:bg-accent-strong"
            >
              Write a draft from this idea
            </button>
          </div>
        )}
      </div>

      <Section title="Angle">{idea.angle}</Section>
      <Section title="Problem">{idea.problem_statement}</Section>
      <Section title="Why now">{idea.why_now}</Section>
      <Section title="Novelty">{idea.novelty}</Section>
      {idea.duplicate_risk && <Section title="Overlap risk">{idea.duplicate_risk}</Section>}

      {idea.key_questions.length > 0 && (
        <div className="mt-5">
          <h2 className="text-xs font-semibold uppercase tracking-wide text-slate-500">
            Research must answer
          </h2>
          <ul className="mt-2 list-disc space-y-1 pl-5 text-sm text-slate-300">
            {idea.key_questions.map((qn, i) => (
              <li key={i}>{qn}</li>
            ))}
          </ul>
        </div>
      )}

      {idea.seed_sources.length > 0 && (
        <div className="mt-5">
          <h2 className="text-xs font-semibold uppercase tracking-wide text-slate-500">
            Seed sources
          </h2>
          <ul className="mt-2 space-y-1 text-sm">
            {idea.seed_sources.map((u, i) => (
              <li key={i}>
                <a
                  href={u}
                  target="_blank"
                  rel="noreferrer"
                  className="break-all text-accent hover:underline"
                >
                  {u}
                </a>
              </li>
            ))}
          </ul>
        </div>
      )}

      {writing && <WriteFromIdeaDialog idea={idea} onClose={() => setWriting(false)} />}
    </div>
  )
}

function WriteFromIdeaDialog({ idea, onClose }: { idea: TopicIdea; onClose: () => void }) {
  const qc = useQueryClient()
  const navigate = useNavigate()
  const [instructions, setInstructions] = useState('')
  const [push, setPush] = useState(false)
  const [cover, setCover] = useState(true)
  const [translate, setTranslate] = useState(false)

  const mut = useMutation({
    mutationFn: () =>
      startWrite({
        topic: idea.data,
        instructions,
        push,
        cover,
        translate,
        label: idea.title || idea.slug,
      }),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ['runs'] })
      onClose()
      navigate(`/runs/${res.id}`)
    },
  })

  return (
    <Modal title="Write a draft from this idea" onClose={onClose} width="max-w-xl">
      <div className="space-y-4">
        <p className="text-sm text-slate-300">
          Launches the full write pipeline for{' '}
          <span className="text-slate-100">{idea.title || idea.slug}</span>. You will land on the run
          so you can watch it, and the draft appears under Drafts when it finishes.
        </p>
        <div>
          <label className="mb-1 block text-xs font-medium text-slate-400">
            Instructions <span className="text-slate-600">(optional)</span>
          </label>
          <textarea
            className="h-24 w-full resize-none rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-200 placeholder:text-slate-600 focus:border-accent focus:outline-none"
            placeholder="Focus on the licensing impact, skip the setup walkthrough"
            value={instructions}
            onChange={(e) => setInstructions(e.target.value)}
          />
          <p className="mt-1 text-xs text-slate-500">
            Steers what the post argues, not just how it reads. This outranks the idea's own angle
            where the two disagree.
          </p>
        </div>
        <div className="flex flex-wrap gap-4">
          <Check label="Push to WordPress" checked={push} onChange={setPush} />
          <Check label="Generate cover" checked={cover} onChange={setCover} />
          <Check label="Translate" checked={translate} onChange={setTranslate} />
        </div>
        {mut.error != null && (
          <p className="rounded-lg bg-rose-500/10 px-3 py-2 text-xs text-rose-300">
            {mut.error instanceof Error ? mut.error.message : String(mut.error)}
          </p>
        )}
        <div className="flex justify-end gap-2">
          <button
            onClick={onClose}
            className="rounded-lg px-4 py-2 text-sm text-slate-400 hover:text-slate-200"
          >
            Cancel
          </button>
          <button
            onClick={() => mut.mutate()}
            disabled={mut.isPending}
            className="rounded-lg bg-accent px-4 py-2 text-sm font-semibold text-slate-950 hover:bg-accent-strong disabled:opacity-50"
          >
            {mut.isPending ? 'Starting…' : 'Start write'}
          </button>
        </div>
      </div>
    </Modal>
  )
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  if (!children) return null
  return (
    <div className="mt-5">
      <h2 className="text-xs font-semibold uppercase tracking-wide text-slate-500">{title}</h2>
      <p className="mt-1 text-sm leading-relaxed text-slate-300">{children}</p>
    </div>
  )
}

function Chip({ children }: { children: React.ReactNode }) {
  return <span className="rounded bg-slate-800 px-1.5 py-0.5 text-slate-400">{children}</span>
}

function Check({
  label,
  checked,
  onChange,
}: {
  label: string
  checked: boolean
  onChange: (v: boolean) => void
}) {
  return (
    <label className="flex cursor-pointer items-center gap-2 text-sm text-slate-300">
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        className="h-4 w-4 accent-[#c084fc]"
      />
      {label}
    </label>
  )
}
