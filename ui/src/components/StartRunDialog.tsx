import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError, getRun, listRuns, startSuggest, startWrite } from '../api/client'
import type { Topic } from '../api/types'
import { field, label, primaryBtn } from '../lib/ui'
import { Modal } from './Modal'

type Mode = 'suggest' | 'write' | 'sources'

export function StartRunDialog({ onClose }: { onClose: () => void }) {
  const [mode, setMode] = useState<Mode>('suggest')
  return (
    <Modal title="Start a run" onClose={onClose} width="max-w-xl">
      <div className="mb-4 flex gap-1 rounded-lg bg-slate-950 p-1">
        {(['suggest', 'write', 'sources'] as Mode[]).map((m) => (
          <button
            key={m}
            onClick={() => setMode(m)}
            className={`flex-1 rounded-md px-3 py-1.5 text-sm font-medium capitalize ${
              mode === m ? 'bg-accent/15 text-accent' : 'text-slate-400 hover:text-slate-200'
            }`}
          >
            {m}
          </button>
        ))}
      </div>
      {mode === 'suggest' && <SuggestForm onClose={onClose} />}
      {mode === 'write' && <WriteForm onClose={onClose} />}
      {mode === 'sources' && <SourcesForm onClose={onClose} />}
    </Modal>
  )
}

function SuggestForm({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient()
  const [instruction, setInstruction] = useState('')
  const [labelText, setLabelText] = useState('')
  const mut = useMutation({
    mutationFn: () => startSuggest({ instruction, label: labelText }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['runs'] })
      onClose()
    },
  })
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault()
        mut.mutate()
      }}
      className="space-y-4"
    >
      <div>
        <label className={label}>Instruction (optional)</label>
        <textarea
          className={`${field} h-24 resize-none`}
          placeholder="Find what is worth writing about on the blog right now…"
          value={instruction}
          onChange={(e) => setInstruction(e.target.value)}
        />
      </div>
      <div>
        <label className={label}>Label (optional)</label>
        <input
          className={field}
          placeholder="Topic discovery"
          value={labelText}
          onChange={(e) => setLabelText(e.target.value)}
        />
      </div>
      <FormFooter
        onClose={onClose}
        pending={mut.isPending}
        error={mut.error}
        label="Start discovery"
      />
    </form>
  )
}

// The same endpoint as a discovery run, with `explore` set: the server turns
// that into its own run kind and its own graph. A sweep always ranges beyond the
// curated feeds — that is what makes it a sweep — so the tab carries no choice
// about it, only the brief.
function SourcesForm({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient()
  const [instruction, setInstruction] = useState('')
  const [labelText, setLabelText] = useState('')
  const mut = useMutation({
    mutationFn: () => startSuggest({ instruction, label: labelText, explore: true }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['runs'] })
      onClose()
    },
  })
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault()
        mut.mutate()
      }}
      className="space-y-4"
    >
      <p className="rounded-lg border border-slate-800 bg-slate-950/40 p-3 text-xs text-slate-500">
        The scouts range beyond the curated feeds and stop with a list of every site they read.
        You approve the sites before any topic is proposed, and the ones you approve are added to
        the blog&rsquo;s trusted sources for future runs.
      </p>
      <div>
        <label className={label}>Instruction (optional)</label>
        <textarea
          className={`${field} h-24 resize-none`}
          placeholder="Find what is worth writing about right now, wherever it lives…"
          value={instruction}
          onChange={(e) => setInstruction(e.target.value)}
        />
      </div>
      <div>
        <label className={label}>Label (optional)</label>
        <input
          className={field}
          placeholder="Source exploration"
          value={labelText}
          onChange={(e) => setLabelText(e.target.value)}
        />
      </div>
      <FormFooter
        onClose={onClose}
        pending={mut.isPending}
        error={mut.error}
        label="Start sweep"
      />
    </form>
  )
}

// A convenience preview of what the server will read out of the brief, so the
// operator can see his corpus before he spends a run on it. The server extracts
// the links again on arrival and that list is the authoritative one — keep this
// in step with `extract_urls` in util.py.
const URL_RE = /https?:\/\/[^\s<>"'`\])]+/gi

function extractUrls(text: string): string[] {
  const found: string[] = []
  for (const match of text.matchAll(URL_RE)) {
    const url = match[0].replace(/[.,;:!?'"]+$/, '')
    if (!found.includes(url)) found.push(url)
  }
  return found
}

function WriteForm({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient()
  // Two ways to start a post. `topic` researches outward from an idea the crew
  // proposed; `custom` is the operator's own brief, and the links in it are the
  // entire corpus — the crew reads those pages and nothing else.
  const [source, setSource] = useState<'topic' | 'custom'>('topic')
  const [runId, setRunId] = useState('')
  const [picked, setPicked] = useState<number | null>(null)
  const [rawJson, setRawJson] = useState('')
  const [useRaw, setUseRaw] = useState(false)
  const [brief, setBrief] = useState('')
  const [allowUnreachable, setAllowUnreachable] = useState(false)
  const [push, setPush] = useState(true)
  const [cover, setCover] = useState(true)
  const [translate, setTranslate] = useState(false)
  const [instructions, setInstructions] = useState('')
  const [labelText, setLabelText] = useState('')

  const links = useMemo(() => extractUrls(brief), [brief])

  // Completed suggest runs carry a suggestions[] we can launch a write from.
  const suggestRuns = useQuery({
    queryKey: ['runs', 'succeeded'],
    queryFn: () => listRuns({ status: 'succeeded', limit: 50 }),
    select: (runs) => runs.filter((r) => r.kind === 'suggest'),
    enabled: source === 'topic',
  })
  const runDetail = useQuery({
    queryKey: ['run', runId],
    queryFn: () => getRun(runId),
    enabled: !!runId && !useRaw && source === 'topic',
  })
  const suggestions = (runDetail.data?.result?.suggestions as Topic[] | undefined) ?? []

  const topic: Topic | null = useRaw
    ? parseTopic(rawJson)
    : picked != null
      ? suggestions[picked] ?? null
      : null

  const mut = useMutation({
    mutationFn: () => {
      const common = { push, cover, translate, label: labelText }
      if (source === 'custom') {
        return startWrite({ brief, allow_unreachable: allowUnreachable, ...common })
      }
      if (!topic) throw new Error('Choose a topic first.')
      return startWrite({ topic, instructions, ...common })
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['runs'] })
      onClose()
    },
  })

  // The server proves every link before it queues the run. Offering the override
  // only once that has actually happened keeps it from reading as an ordinary
  // option: it is a decision to run on less than was asked for.
  const unreachable =
    mut.error instanceof ApiError &&
    mut.error.status === 422 &&
    mut.error.message.includes('did not resolve')

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault()
        mut.mutate()
      }}
      className="space-y-4"
    >
      <div className="flex flex-wrap gap-4 rounded-lg border border-slate-800 bg-slate-950/40 p-3">
        <SourceRadio
          checked={source === 'topic'}
          onSelect={() => setSource('topic')}
          title="Topic"
          hint="One the crew proposed. It researches outward from there."
        />
        <SourceRadio
          checked={source === 'custom'}
          onSelect={() => setSource('custom')}
          title="Custom"
          hint="Your brief. The draft is built only from the links in it."
        />
      </div>

      {source === 'custom' ? (
        <>
          <div>
            <label className={label}>Brief</label>
            <textarea
              className={`${field} h-40 resize-none`}
              placeholder={
                'What the post should argue, and the pages it must be built from:\n\n' +
                'https://learn.microsoft.com/…\nhttps://…'
              }
              value={brief}
              onChange={(e) => setBrief(e.target.value)}
            />
            <p className="mt-1 text-xs text-slate-500">
              No web search: the crew reads these pages and nothing else, so the post can only
              say what they say. Anything they leave open comes back as an open question.
            </p>
          </div>
          <div>
            <label className={label}>Sources found ({links.length})</label>
            {links.length === 0 ? (
              <p className="text-xs text-slate-500">
                Paste at least one link — it is the only thing the post can be built from.
              </p>
            ) : (
              <ul className="space-y-1">
                {links.map((url) => (
                  <li
                    key={url}
                    className="truncate rounded-md border border-slate-800 bg-slate-950 px-2 py-1 font-mono text-xs text-slate-400"
                  >
                    {url}
                  </li>
                ))}
              </ul>
            )}
          </div>
          {unreachable && (
            <label className="flex cursor-pointer gap-3 rounded-lg border border-slate-800 bg-slate-950/40 p-3">
              <input
                type="checkbox"
                className="mt-0.5 h-4 w-4 accent-cyan-400"
                checked={allowUnreachable}
                onChange={(e) => setAllowUnreachable(e.target.checked)}
              />
              <span className="text-sm">
                <span className="font-medium text-slate-200">Start anyway</span>
                <span className="mt-0.5 block text-xs text-slate-500">
                  The crew will report what it could not read instead of researching around it.
                </span>
              </span>
            </label>
          )}
        </>
      ) : (
        <>
          <div className="flex items-center justify-between">
            <label className={label}>Topic</label>
            <button
              type="button"
              onClick={() => setUseRaw((v) => !v)}
              className="text-xs text-slate-500 hover:text-accent"
            >
              {useRaw ? 'pick from a suggestion' : 'paste topic JSON'}
            </button>
          </div>

          {useRaw ? (
            <textarea
              className={`${field} h-40 resize-none font-mono text-xs`}
              placeholder='{"title": "...", "slug": "...", "watch_area": "...", ...}'
              value={rawJson}
              onChange={(e) => setRawJson(e.target.value)}
            />
          ) : (
            <div className="space-y-3">
              <select className={field} value={runId} onChange={(e) => { setRunId(e.target.value); setPicked(null) }}>
                <option value="">Select a completed discovery run…</option>
                {suggestRuns.data?.map((r) => (
                  <option key={r.id} value={r.id}>
                    {r.label || 'Topic discovery'} — {r.finished_at?.slice(0, 16).replace('T', ' ')}
                  </option>
                ))}
              </select>
              {suggestRuns.data && suggestRuns.data.length === 0 && (
                <p className="text-xs text-slate-500">
                  No completed discovery runs yet. Run a <span className="text-accent">suggest</span> first,
                  or paste a topic JSON.
                </p>
              )}
              {runDetail.isLoading && <p className="text-xs text-slate-500">loading suggestions…</p>}
              <div className="max-h-52 space-y-1.5 overflow-auto">
                {suggestions.map((s, i) => (
                  <button
                    type="button"
                    key={i}
                    onClick={() => setPicked(i)}
                    className={`block w-full rounded-lg border px-3 py-2 text-left text-sm ${
                      picked === i
                        ? 'border-accent bg-accent/10 text-slate-100'
                        : 'border-slate-800 bg-slate-950 text-slate-300 hover:border-slate-600'
                    }`}
                  >
                    <div className="font-medium">{String(s.title)}</div>
                    <div className="mt-0.5 text-xs text-slate-500">
                      {'score' in s ? `score ${String(s.score)} · ` : ''}
                      {'watch_area' in s ? String(s.watch_area) : ''}
                    </div>
                  </button>
                ))}
              </div>
            </div>
          )}

          <div>
            <label className={label}>Instructions (optional)</label>
            <textarea
              className={`${field} h-20 resize-none`}
              placeholder="Focus on the licensing impact, skip the setup walkthrough"
              value={instructions}
              onChange={(e) => setInstructions(e.target.value)}
            />
            <p className="mt-1 text-xs text-slate-500">
              Steers what the post argues, not just how it reads.
            </p>
          </div>
        </>
      )}

      <div className="flex flex-wrap gap-4 border-t border-slate-800 pt-3">
        <Check label="Push to WordPress" checked={push} onChange={setPush} />
        <Check label="Generate cover" checked={cover} onChange={setCover} />
        <Check label="Translate" checked={translate} onChange={setTranslate} />
      </div>
      <div>
        <label className={label}>Label (optional)</label>
        <input
          className={field}
          placeholder={source === 'custom' ? 'defaults to the first line of the brief' : 'defaults to the topic title'}
          value={labelText}
          onChange={(e) => setLabelText(e.target.value)}
        />
      </div>
      <FormFooter
        onClose={onClose}
        pending={mut.isPending}
        error={mut.error}
        label="Start write"
        disabled={source === 'custom' ? links.length === 0 : !topic}
      />
    </form>
  )
}

function SourceRadio({
  checked,
  onSelect,
  title,
  hint,
}: {
  checked: boolean
  onSelect: () => void
  title: string
  hint: string
}) {
  return (
    <label className="flex flex-1 cursor-pointer gap-3">
      <input
        type="radio"
        name="write-source"
        className="mt-0.5 h-4 w-4 accent-[#c084fc]"
        checked={checked}
        onChange={onSelect}
      />
      <span className="text-sm">
        <span className="font-medium text-slate-200">{title}</span>
        <span className="mt-0.5 block text-xs text-slate-500">{hint}</span>
      </span>
    </label>
  )
}

function Check({ label: text, checked, onChange }: { label: string; checked: boolean; onChange: (v: boolean) => void }) {
  return (
    <label className="flex cursor-pointer items-center gap-2 text-sm text-slate-300">
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        className="h-4 w-4 accent-[#c084fc]"
      />
      {text}
    </label>
  )
}

function FormFooter({
  onClose,
  pending,
  error,
  label,
  disabled,
}: {
  onClose: () => void
  pending: boolean
  error: unknown
  label: string
  disabled?: boolean
}) {
  return (
    <div className="space-y-2">
      {error != null && (
        <p className="rounded-lg bg-rose-500/10 px-3 py-2 text-xs text-rose-300">
          {error instanceof Error ? error.message : String(error)}
        </p>
      )}
      <div className="flex justify-end gap-2">
        <button
          type="button"
          onClick={onClose}
          className="rounded-lg px-4 py-2 text-sm text-slate-400 hover:text-slate-200"
        >
          Cancel
        </button>
        <button type="submit" disabled={pending || disabled} className={primaryBtn}>
          {pending ? 'Starting…' : label}
        </button>
      </div>
    </div>
  )
}

function parseTopic(raw: string): Topic | null {
  try {
    const obj = JSON.parse(raw)
    if (obj && typeof obj === 'object' && typeof obj.title === 'string') return obj as Topic
    return null
  } catch {
    return null
  }
}
