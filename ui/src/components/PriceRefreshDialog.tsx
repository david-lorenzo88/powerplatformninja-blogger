import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { getPriceCandidates, refreshPrices } from '../api/client'
import type { PriceChange } from '../api/types'
import { Modal } from './Modal'
import { field, ghostBtn, primaryBtn } from '../lib/ui'

// Keeping the price table in step with Azure, in the two steps that job takes.
//
// **Refresh** re-reads meters already bound to a model and shows what moved. It
// is safe to apply unattended because it can only ever change a number — the
// meter names are fixed — and because a run's cost is stored when the run
// happens, so a new price never rewrites history.
//
// **Bind** is the manual half, and it stays manual on purpose. Azure's meter
// names cannot be matched to a deployment without guessing (gpt-5 is "5 pp",
// gpt-5.4 is "5.4", and one region carries 400+ GPT rows), and a wrong guess
// prices you against the wrong meter — which reads exactly like a correct
// answer. So this offers the shortlist and a person picks.
export function PriceRefreshDialog({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient()
  const [mode, setMode] = useState<'refresh' | 'bind'>('refresh')

  return (
    <Modal title="Prices from Azure" onClose={onClose}>
      <div className="mb-4 flex gap-1 border-b border-slate-800">
        <Tab active={mode === 'refresh'} onClick={() => setMode('refresh')}>
          Refresh bound prices
        </Tab>
        <Tab active={mode === 'bind'} onClick={() => setMode('bind')}>
          Bind a model
        </Tab>
      </div>
      {mode === 'refresh' ? <RefreshPanel qc={qc} onClose={onClose} /> : <BindPanel />}
    </Modal>
  )
}

function RefreshPanel({
  qc,
  onClose,
}: {
  qc: ReturnType<typeof useQueryClient>
  onClose: () => void
}) {
  const check = useQuery({
    queryKey: ['price-refresh'],
    queryFn: () => refreshPrices(false),
    // A live call to Microsoft. Run it when the dialog opens and not again
    // until asked — this is not a screen to poll.
    staleTime: Infinity,
    gcTime: 0,
  })

  const apply = useMutation({
    mutationFn: () => refreshPrices(true),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['config'] })
      qc.invalidateQueries({ queryKey: ['config', 'model_prices'] })
      onClose()
    },
  })

  if (check.isLoading) return <p className="text-sm text-slate-400">Asking Azure…</p>
  if (check.error) {
    return (
      <p className="text-sm text-rose-300">
        Could not reach the price feed. Your current prices are untouched.
      </p>
    )
  }

  const changes = check.data?.changes ?? []
  const moved = changes.filter((c) => c.changed)
  const missing = changes.filter((c) => !c.found)

  if (changes.length === 0) {
    return (
      <p className="text-sm text-slate-400">
        No meters are bound yet. Use <span className="font-semibold">Bind a model</span> first.
      </p>
    )
  }

  return (
    <div className="space-y-3">
      <p className="text-sm text-slate-400">
        {moved.length === 0
          ? `Checked ${changes.length} meters — everything is current.`
          : `${moved.length} of ${changes.length} prices have moved.`}
      </p>

      <ul className="divide-y divide-slate-800 rounded-lg border border-slate-800">
        {changes.map((c) => (
          <ChangeRow key={`${c.model}-${c.direction}`} change={c} />
        ))}
      </ul>

      {missing.length > 0 && (
        <p className="rounded-lg bg-amber-500/10 px-3 py-2 text-xs text-amber-200">
          {missing.length} bound meter(s) no longer exist in the feed. Their last known prices are
          kept — a stale estimate beats none — but the binding wants re-checking.
        </p>
      )}

      <div className="flex justify-end gap-2 pt-1">
        <button onClick={onClose} className={ghostBtn}>
          Close
        </button>
        <button
          onClick={() => apply.mutate()}
          disabled={moved.length === 0 || apply.isPending}
          className={primaryBtn}
        >
          {apply.isPending ? 'Saving…' : `Apply ${moved.length} change(s)`}
        </button>
      </div>
      {apply.error && (
        <p className="text-xs text-rose-300">{(apply.error as Error).message}</p>
      )}
    </div>
  )
}

function ChangeRow({ change }: { change: PriceChange }) {
  return (
    <li className="flex items-baseline gap-2 px-3 py-2 text-xs">
      <span className="w-24 shrink-0 truncate font-mono text-slate-300">{change.model}</span>
      <span className="w-24 shrink-0 text-slate-500">{change.direction}</span>
      <span className="tabular-nums text-slate-400">{change.old ?? '—'}</span>
      {change.changed ? (
        <>
          <span className="text-slate-600">→</span>
          <span className="tabular-nums font-semibold text-accent">{change.new}</span>
        </>
      ) : (
        <span className={change.found ? 'text-emerald-400/70' : 'text-amber-300/80'}>
          {change.found ? 'unchanged' : 'meter not found'}
        </span>
      )}
      {/* The meter name is the whole basis of the figure — show it, don't hide
          it behind a tooltip. */}
      <span className="ml-auto hidden truncate font-mono text-[10px] text-slate-600 lg:inline">
        {change.meter}
      </span>
    </li>
  )
}

function BindPanel() {
  const [model, setModel] = useState('')
  const [query, setQuery] = useState('')

  const candidates = useQuery({
    queryKey: ['price-candidates', query],
    queryFn: () => getPriceCandidates(query),
    enabled: query.length > 0,
    staleTime: Infinity,
    gcTime: 0,
  })

  const suggested = candidates.data?.suggested ?? {}

  return (
    <div className="space-y-3">
      <form
        onSubmit={(e) => {
          e.preventDefault()
          setQuery(model.trim())
        }}
        className="flex gap-2"
      >
        <input
          value={model}
          onChange={(e) => setModel(e.target.value)}
          placeholder="model, e.g. gpt-5"
          className={`${field} flex-1`}
        />
        <button type="submit" className={primaryBtn} disabled={!model.trim()}>
          Find meters
        </button>
      </form>

      {candidates.isLoading && <p className="text-sm text-slate-400">Asking Azure…</p>}
      {candidates.data && (
        <>
          <p className="text-xs text-slate-500">
            {candidates.data.region} · tier {candidates.data.tier} · {candidates.data.currency}
          </p>
          {candidates.data.candidates.length === 0 ? (
            <p className="text-sm text-slate-400">
              No meters matched. Check the model name — the feed does not use deployment names.
            </p>
          ) : (
            <ul className="max-h-64 divide-y divide-slate-800 overflow-auto rounded-lg border border-slate-800">
              {candidates.data.candidates.map((c) => (
                <li key={c.meter} className="flex items-baseline gap-2 px-3 py-2 text-xs">
                  <span
                    className={`w-24 shrink-0 ${c.direction ? 'text-accent' : 'text-slate-600'}`}
                  >
                    {c.direction ?? 'other tier'}
                  </span>
                  <span className="w-20 shrink-0 text-right tabular-nums text-slate-300">
                    {c.price_per_million.toFixed(4)}
                  </span>
                  <span className="truncate font-mono text-slate-400">{c.meter}</span>
                </li>
              ))}
            </ul>
          )}

          {Object.keys(suggested).length > 0 && (
            <div className="rounded-lg border border-slate-800 bg-slate-950/60 p-3">
              <p className="mb-2 text-xs text-slate-400">
                Unambiguous for this tier. Paste under{' '}
                <span className="font-mono text-slate-300">{candidates.data.model}</span> in the
                document:
              </p>
              <pre className="overflow-x-auto text-[11px] leading-relaxed text-slate-300">
                {`    meters:\n${Object.entries(suggested)
                  .map(([direction, meter]) => `      ${direction}: "${meter}"`)
                  .join('\n')}`}
              </pre>
            </div>
          )}
        </>
      )}
    </div>
  )
}

function Tab({
  active,
  onClick,
  children,
}: {
  active: boolean
  onClick: () => void
  children: React.ReactNode
}) {
  return (
    <button
      onClick={onClick}
      className={`min-h-11 px-3 text-sm lg:min-h-0 lg:py-2 ${
        active
          ? 'border-b-2 border-accent font-medium text-accent'
          : 'text-slate-400 hover:text-slate-200'
      }`}
    >
      {children}
    </button>
  )
}
