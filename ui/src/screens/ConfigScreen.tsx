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
import { NotificationsCard } from '../components/NotificationsCard'
import { Modal } from '../components/Modal'
import { PriceRefreshDialog } from '../components/PriceRefreshDialog'
import { formatTime } from '../lib/format'
import { field, ghostBtn, primaryBtn } from '../lib/ui'

// Notifications are a per-device setting rather than a config document, so they
// share this screen but not the document list.
//
// They began as one more chip on the end of that list, which on a phone put them
// 329px past the right edge of a 374px strip: invisible, with nothing to suggest
// the row scrolled at all. Below `lg` they now get their own full-width row
// above the documents. The vertical rail on desktop never had the problem, so
// there they stay at the foot of it.
const NOTIFICATIONS = '__notifications__'

function NotificationsButton({ active, onSelect }: { active: boolean; onSelect: () => void }) {
  return (
    <button
      onClick={onSelect}
      className={`flex min-h-11 w-full items-center gap-2 rounded-md px-3 text-sm lg:min-h-0 lg:px-2 lg:py-1.5 ${
        active
          ? 'bg-accent/15 text-accent'
          : 'text-slate-300 active:bg-slate-800/60 lg:hover:bg-slate-800/60'
      }`}
    >
      <span aria-hidden="true">🔔</span>
      <span className="text-xs">Notifications</span>
    </button>
  )
}

export function ConfigScreen() {
  const [selected, setSelected] = useState<string | null>(null)
  const docs = useQuery({ queryKey: ['config'], queryFn: listConfig })

  // Default to the first document once the list loads.
  useEffect(() => {
    if (!selected && docs.data && docs.data.length > 0) setSelected(docs.data[0].name)
  }, [docs.data, selected])

  return (
    <div className="flex h-full flex-col lg:flex-row">
      {/* A selector rail on desktop; the same five documents as a scrolling chip
          strip on a phone, where 224px of permanent sidebar is not affordable. */}
      <aside className="shrink-0 border-b border-slate-800 p-2 lg:w-56 lg:border-b-0 lg:border-r lg:p-3">
        <div className="mb-2 lg:hidden">
          <NotificationsButton
            active={selected === NOTIFICATIONS}
            onSelect={() => setSelected(NOTIFICATIONS)}
          />
        </div>
        <h2 className="hidden px-2 pb-2 text-xs font-semibold uppercase tracking-wide text-slate-500 lg:block">
          Documents
        </h2>
        <ul className="flex gap-1 overflow-x-auto lg:flex-col lg:space-y-0.5 lg:overflow-visible">
          {docs.data?.map((d) => (
            <li key={d.name} className="shrink-0 lg:shrink">
              <button
                onClick={() => setSelected(d.name)}
                className={`flex min-h-11 w-full items-center gap-2 whitespace-nowrap rounded-md px-3 text-sm lg:min-h-0 lg:justify-between lg:px-2 lg:py-1.5 ${
                  selected === d.name
                    ? 'bg-accent/15 text-accent'
                    : 'text-slate-300 active:bg-slate-800/60 lg:hover:bg-slate-800/60'
                }`}
              >
                <span className="font-mono text-xs">{d.name}</span>
                <span className="text-[10px] text-slate-500">v{d.version}</span>
              </button>
            </li>
          ))}
          <li className="hidden lg:block lg:pt-2">
            <NotificationsButton
              active={selected === NOTIFICATIONS}
              onSelect={() => setSelected(NOTIFICATIONS)}
            />
          </li>
        </ul>
      </aside>
      {selected === NOTIFICATIONS ? (
        <div className="min-h-0 flex-1 overflow-auto p-4 lg:p-6">
          <div className="mx-auto max-w-xl">
            <NotificationsCard />
          </div>
        </div>
      ) : selected ? (
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

  const [pricesOpen, setPricesOpen] = useState(false)
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
    // min-h-0 matters now that the parent stacks vertically below lg: without it
    // the editor takes its content height and pushes the history strip off the
    // bottom of the screen.
    <div className="flex min-h-0 min-w-0 flex-1 flex-col">
      <div className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-2 border-b border-slate-800 px-4 py-3 lg:flex-nowrap lg:px-5">
        <span className="font-mono text-sm text-slate-200">{name}</span>
        {doc.data && (
          <span className="text-xs text-slate-500">
            v{doc.data.version} · {doc.data.format} · {formatTime(doc.data.updated_at)}
          </span>
        )}
        <div className="flex w-full items-center gap-2 lg:ml-auto lg:w-auto">
          {/* Only on the prices document: nothing else has an upstream to
              reconcile against. */}
          {name === 'model_prices' && (
            <button onClick={() => setPricesOpen(true)} className={`${ghostBtn} shrink-0`}>
              Update from Azure
            </button>
          )}
          <input
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="change note (optional)"
            className={`${field} flex-1 lg:w-56 lg:flex-none lg:text-xs`}
          />
          <button onClick={() => save.mutate()} disabled={!dirty || save.isPending} className={primaryBtn}>
            {save.isPending ? 'Saving…' : dirty ? 'Save' : saved ? 'Saved ✓' : 'Saved'}
          </button>
        </div>
      </div>

      {pricesOpen && <PriceRefreshDialog onClose={() => setPricesOpen(false)} />}

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
    <div className="shrink-0 border-t border-slate-800 px-4 py-2 lg:px-5">
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
            <button
              onClick={() => setViewing(h.version)}
              className="min-h-11 px-1 text-accent hover:underline lg:min-h-0"
            >
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
