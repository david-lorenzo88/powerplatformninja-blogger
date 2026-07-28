import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { getRun, listRuns, startSuggest, startWrite } from '../api/client'
import type { Topic } from '../api/types'
import { Modal } from './Modal'

type Mode = 'suggest' | 'write'

const field = 'w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-200 placeholder:text-slate-600 focus:border-accent focus:outline-none'
const label = 'mb-1 block text-xs font-medium text-slate-400'
const primaryBtn = 'rounded-lg bg-accent px-4 py-2 text-sm font-semibold text-slate-950 hover:bg-accent-strong disabled:opacity-50'

export function StartRunDialog({ onClose }: { onClose: () => void }) {
  const [mode, setMode] = useState<Mode>('suggest')
  return (
    <Modal title="Start a run" onClose={onClose} width="max-w-xl">
      <div className="mb-4 flex gap-1 rounded-lg bg-slate-950 p-1">
        {(['suggest', 'write'] as Mode[]).map((m) => (
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
      {mode === 'suggest' ? (
        <SuggestForm onClose={onClose} />
      ) : (
        <WriteForm onClose={onClose} />
      )}
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
      <FormFooter onClose={onClose} pending={mut.isPending} error={mut.error} label="Start discovery" />
    </form>
  )
}

function WriteForm({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient()
  const [runId, setRunId] = useState('')
  const [picked, setPicked] = useState<number | null>(null)
  const [rawJson, setRawJson] = useState('')
  const [useRaw, setUseRaw] = useState(false)
  const [push, setPush] = useState(true)
  const [cover, setCover] = useState(true)
  const [translate, setTranslate] = useState(false)
  const [labelText, setLabelText] = useState('')

  // Completed suggest runs carry a suggestions[] we can launch a write from.
  const suggestRuns = useQuery({
    queryKey: ['runs', 'succeeded'],
    queryFn: () => listRuns({ status: 'succeeded', limit: 50 }),
    select: (runs) => runs.filter((r) => r.kind === 'suggest'),
  })
  const runDetail = useQuery({
    queryKey: ['run', runId],
    queryFn: () => getRun(runId),
    enabled: !!runId && !useRaw,
  })
  const suggestions = (runDetail.data?.result?.suggestions as Topic[] | undefined) ?? []

  const topic: Topic | null = useRaw
    ? parseTopic(rawJson)
    : picked != null
      ? suggestions[picked] ?? null
      : null

  const mut = useMutation({
    mutationFn: () => {
      if (!topic) throw new Error('Choose a topic first.')
      return startWrite({ topic, push, cover, translate, label: labelText })
    },
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

      <div className="flex flex-wrap gap-4 border-t border-slate-800 pt-3">
        <Check label="Push to WordPress" checked={push} onChange={setPush} />
        <Check label="Generate cover" checked={cover} onChange={setCover} />
        <Check label="Translate" checked={translate} onChange={setTranslate} />
      </div>
      <div>
        <label className={label}>Label (optional)</label>
        <input
          className={field}
          placeholder="defaults to the topic title"
          value={labelText}
          onChange={(e) => setLabelText(e.target.value)}
        />
      </div>
      <FormFooter
        onClose={onClose}
        pending={mut.isPending}
        error={mut.error}
        label="Start write"
        disabled={!topic}
      />
    </form>
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
