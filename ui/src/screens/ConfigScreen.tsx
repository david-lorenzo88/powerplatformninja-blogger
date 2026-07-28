import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  ApiError,
  configHistory,
  configRollback,
  configVersion,
  getConfig,
  listConfig,
  putConfig,
} from '../api/client'
import { CodeEditor } from '../components/CodeEditor'
import { Modal } from '../components/Modal'
import { formatTime } from '../lib/format'

export function ConfigScreen() {
  const [selected, setSelected] = useState<string | null>(null)
  const docs = useQuery({ queryKey: ['config'], queryFn: listConfig })

  // Default to the first document once the list loads.
  useEffect(() => {
    if (!selected && docs.data && docs.data.length > 0) setSelected(docs.data[0].name)
  }, [docs.data, selected])

  return (
    <div className="flex h-full">
      <aside className="w-56 shrink-0 border-r border-slate-800 p-3">
        <h2 className="px-2 pb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">
          Documents
        </h2>
        <ul className="space-y-0.5">
          {docs.data?.map((d) => (
            <li key={d.name}>
              <button
                onClick={() => setSelected(d.name)}
                className={`flex w-full items-center justify-between rounded-md px-2 py-1.5 text-sm ${
                  selected === d.name
                    ? 'bg-accent/15 text-accent'
                    : 'text-slate-300 hover:bg-slate-800/60'
                }`}
              >
                <span className="font-mono text-xs">{d.name}</span>
                <span className="text-[10px] text-slate-500">v{d.version}</span>
              </button>
            </li>
          ))}
        </ul>
      </aside>
      {selected ? (
        <ConfigEditor key={selected} name={selected} />
      ) : (
        <div className="flex flex-1 items-center justify-center text-sm text-slate-500">
          {docs.isLoading ? 'loading…' : 'Select a document.'}
        </div>
      )}
    </div>
  )
}

function ConfigEditor({ name }: { name: string }) {
  const qc = useQueryClient()
  const doc = useQuery({ queryKey: ['config', name], queryFn: () => getConfig(name) })

  const [content, setContent] = useState('')
  const [note, setNote] = useState('')
  const [dirty, setDirty] = useState(false)
  const [saved, setSaved] = useState(false)

  // Load server content into the buffer when it (re)loads.
  useEffect(() => {
    if (doc.data) {
      setContent(doc.data.content)
      setDirty(false)
    }
  }, [doc.data])

  const save = useMutation({
    mutationFn: () => putConfig(name, content, note),
    onSuccess: () => {
      setDirty(false)
      setNote('')
      setSaved(true)
      qc.invalidateQueries({ queryKey: ['config'] })
      qc.invalidateQueries({ queryKey: ['config', name] })
      qc.invalidateQueries({ queryKey: ['config', name, 'history'] })
      setTimeout(() => setSaved(false), 2500)
    },
  })

  // A 422 carries the YAML parser message — surface it inline, never swallow it.
  const parseError =
    save.error instanceof ApiError && save.error.status === 422 ? save.error.message : null
  const otherError =
    save.error && !(save.error instanceof ApiError && save.error.status === 422)
      ? save.error instanceof Error
        ? save.error.message
        : String(save.error)
      : null

  return (
    <div className="flex min-w-0 flex-1 flex-col">
      <div className="flex shrink-0 items-center gap-3 border-b border-slate-800 px-5 py-3">
        <span className="font-mono text-sm text-slate-200">{name}</span>
        {doc.data && (
          <span className="text-xs text-slate-500">
            v{doc.data.version} · {doc.data.format} · {formatTime(doc.data.updated_at)}
          </span>
        )}
        <div className="ml-auto flex items-center gap-2">
          <input
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="change note (optional)"
            className="w-56 rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 text-xs text-slate-200 placeholder:text-slate-600 focus:border-accent focus:outline-none"
          />
          <button
            onClick={() => save.mutate()}
            disabled={!dirty || save.isPending}
            className="rounded-lg bg-accent px-4 py-1.5 text-sm font-semibold text-slate-950 hover:bg-accent-strong disabled:opacity-40"
          >
            {save.isPending ? 'Saving…' : dirty ? 'Save' : saved ? 'Saved ✓' : 'Saved'}
          </button>
        </div>
      </div>

      {parseError && (
        <div className="shrink-0 border-b border-rose-500/30 bg-rose-500/10 px-5 py-2 font-mono text-xs text-rose-300">
          Invalid YAML — not saved: {parseError}
        </div>
      )}
      {otherError && (
        <div className="shrink-0 border-b border-rose-500/30 bg-rose-500/10 px-5 py-2 text-xs text-rose-300">
          {otherError}
        </div>
      )}

      <div className="min-h-0 flex-1 overflow-auto">
        {doc.isLoading ? (
          <p className="p-5 text-sm text-slate-500">loading…</p>
        ) : (
          <CodeEditor
            value={content}
            format={doc.data?.format ?? 'yaml'}
            onChange={(v) => {
              setContent(v)
              setDirty(true)
            }}
          />
        )}
      </div>

      <HistoryBar name={name} />
    </div>
  )
}

function HistoryBar({ name }: { name: string }) {
  const qc = useQueryClient()
  const [viewing, setViewing] = useState<number | null>(null)
  const history = useQuery({
    queryKey: ['config', name, 'history'],
    queryFn: () => configHistory(name),
  })

  const rollback = useMutation({
    mutationFn: (version: number) => configRollback(name, version),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['config'] })
      qc.invalidateQueries({ queryKey: ['config', name] })
      qc.invalidateQueries({ queryKey: ['config', name, 'history'] })
      setViewing(null)
    },
  })

  return (
    <div className="shrink-0 border-t border-slate-800 px-5 py-2">
      <div className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-slate-500">
        History
      </div>
      <div className="flex gap-2 overflow-x-auto pb-1">
        {history.data?.map((h) => (
          <div
            key={h.version}
            className="flex shrink-0 items-center gap-2 rounded-lg border border-slate-800 bg-slate-900/50 px-2.5 py-1 text-xs"
          >
            <span className="font-mono text-slate-300">v{h.version}</span>
            <span className="max-w-[10rem] truncate text-slate-500" title={h.note}>
              {h.note || '—'}
            </span>
            <button onClick={() => setViewing(h.version)} className="text-accent hover:underline">
              view
            </button>
          </div>
        ))}
        {history.data && history.data.length === 0 && (
          <span className="text-xs text-slate-500">no history yet</span>
        )}
      </div>

      {viewing != null && (
        <VersionModal
          name={name}
          version={viewing}
          onClose={() => setViewing(null)}
          onRollback={() => rollback.mutate(viewing)}
          rollingBack={rollback.isPending}
        />
      )}
    </div>
  )
}

function VersionModal({
  name,
  version,
  onClose,
  onRollback,
  rollingBack,
}: {
  name: string
  version: number
  onClose: () => void
  onRollback: () => void
  rollingBack: boolean
}) {
  const v = useQuery({
    queryKey: ['config', name, 'versions', version],
    queryFn: () => configVersion(name, version),
  })
  return (
    <Modal title={`${name} — v${version}`} onClose={onClose} width="max-w-3xl">
      <div className="mb-3 max-h-[50vh] overflow-auto rounded-lg border border-slate-800">
        <CodeEditor value={v.data?.content ?? ''} format="yaml" readOnly height="auto" />
      </div>
      <div className="flex items-center justify-between">
        <span className="text-xs text-slate-500">{v.data?.note}</span>
        <button
          onClick={onRollback}
          disabled={rollingBack}
          className="rounded-lg border border-accent/50 px-4 py-2 text-sm font-medium text-accent hover:bg-accent/10 disabled:opacity-50"
        >
          {rollingBack ? 'Restoring…' : `Restore v${version} as newest`}
        </button>
      </div>
    </Modal>
  )
}
