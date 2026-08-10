import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  createRecipient,
  deleteRecipient,
  listChannels,
  listNewsletters,
  listRecipients,
  updateRecipient,
} from '../api/client'
import type { ChannelId, ChannelInfo } from '../api/types'
import { Modal } from '../components/Modal'
import { LettersSubNav } from '../components/SubNav'
import { useOnline } from '../hooks/useOnline'
import { card, field, label, primaryBtn, quietBtn } from '../lib/ui'

// What each channel can and cannot do. These are platform facts, not settings,
// and the place to say them is where someone is about to choose one — not in a
// README they will read after it has failed.
const CAVEATS: Record<ChannelId, string> = {
  webpush:
    'Goes to every browser that granted notifications. Carries a link, not the issue.',
  manual: 'Marks the issue handled so you can copy the markdown out yourself.',
  email: 'The only channel that carries the whole issue.',
  telegram:
    'The only channel that can post to a group. Use the chat id (negative for groups), and add the bot to the group first.',
  whatsapp:
    'Individual numbers only — Meta has no group API. Sends a pre-approved template with a link, billed per conversation.',
}

const PLACEHOLDER: Record<ChannelId, string> = {
  webpush: '',
  manual: '',
  email: 'someone@example.com',
  telegram: '-1001234567890',
  whatsapp: '+34600111222',
}

export function RecipientsScreen() {
  const online = useOnline()
  const qc = useQueryClient()
  const [adding, setAdding] = useState(false)

  const channels = useQuery({ queryKey: ['channels'], queryFn: listChannels })
  const recipients = useQuery({ queryKey: ['recipients'], queryFn: listRecipients })
  const letters = useQuery({ queryKey: ['newsletters'], queryFn: listNewsletters })

  const patch = useMutation({
    mutationFn: ({ id, changes }: { id: number; changes: Record<string, unknown> }) =>
      updateRecipient(id, changes),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['recipients'] }),
  })
  const remove = useMutation({
    mutationFn: deleteRecipient,
    onSuccess: () => qc.invalidateQueries({ queryKey: ['recipients'] }),
  })

  const byId = new Map((letters.data ?? []).map((n) => [n.id, n.name]))

  return (
    <div className="flex h-full flex-col">
      <div className="shrink-0 border-b border-slate-800 px-4 py-4 lg:px-6">
        <div className="flex items-baseline gap-3">
          <h1 className="text-lg font-semibold text-slate-100">Recipients</h1>
          <span className="text-sm text-slate-500">{recipients.data?.length ?? 0}</span>
          <button
            className={`${primaryBtn} ml-auto`}
            disabled={!online}
            onClick={() => setAdding(true)}
          >
            Add recipient
          </button>
        </div>
        <div className="mt-3">
          <LettersSubNav />
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-auto p-4 lg:px-6">
        <section className="mb-6">
          <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">
            Channels
          </h2>
          <ul className="flex flex-col gap-2">
            {(channels.data ?? []).map((c) => (
              <li key={c.id} className={`${card} p-3`}>
                <div className="flex items-center gap-2">
                  <span
                    className={`h-2 w-2 rounded-full ${
                      c.configured ? 'bg-emerald-400' : 'bg-slate-600'
                    }`}
                  />
                  <span className="text-sm text-slate-200">{c.label}</span>
                  <span className="ml-auto text-xs text-slate-500">{c.detail}</span>
                </div>
                <p className="mt-1 text-xs text-slate-500">{CAVEATS[c.id]}</p>
              </li>
            ))}
          </ul>
        </section>

        <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">
          Who gets the issues
        </h2>
        {(recipients.data ?? []).length === 0 ? (
          <p className="text-sm text-slate-500">
            Nobody yet. Add one — or use <span className="text-slate-300">Copy out by hand</span>,
            which needs no setup at all.
          </p>
        ) : (
          <ul className="flex flex-col gap-2">
            {(recipients.data ?? []).map((r) => (
              <li key={r.id} className={`${card} p-3`}>
                <div className="flex items-baseline gap-2">
                  <span className="text-sm font-medium text-slate-200">{r.name}</span>
                  <span className="text-xs text-slate-500">{r.channel}</span>
                  {!r.enabled && <span className="text-xs text-slate-600">paused</span>}
                  <label className="ml-auto flex min-h-11 items-center gap-2 text-xs text-slate-500 lg:min-h-0">
                    <input
                      type="checkbox"
                      className="h-5 w-5 accent-cyan-400 lg:h-4 lg:w-4"
                      checked={r.enabled}
                      disabled={!online || patch.isPending}
                      onChange={(e) =>
                        patch.mutate({ id: r.id, changes: { enabled: e.target.checked } })
                      }
                    />
                    on
                  </label>
                </div>
                {r.address && (
                  <p className="mt-0.5 break-all text-xs text-slate-500">{r.address}</p>
                )}
                <p className="mt-1 text-xs text-slate-600">
                  {r.newsletter_ids.length === 0
                    ? 'every newsletter'
                    : r.newsletter_ids.map((i) => byId.get(i) ?? `#${i}`).join(', ')}
                </p>
                {r.failed_at && (
                  // Parked rather than deleted: the channel said this address is
                  // permanently bad, so it is skipped until someone says otherwise.
                  <p className="mt-1 rounded border border-rose-500/30 bg-rose-500/10 px-2 py-1 text-xs text-rose-300">
                    Parked after a permanent failure: {r.last_error}. Toggling it back on
                    clears this.
                  </p>
                )}
                <button
                  className={`${quietBtn} mt-1 px-0 text-xs`}
                  disabled={!online}
                  onClick={() => remove.mutate(r.id)}
                >
                  Remove
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>

      {adding && (
        <AddRecipientDialog channels={channels.data ?? []} onClose={() => setAdding(false)} />
      )}
    </div>
  )
}

function AddRecipientDialog({
  channels,
  onClose,
}: {
  channels: ChannelInfo[]
  onClose: () => void
}) {
  const qc = useQueryClient()
  const [channel, setChannel] = useState<ChannelId>('email')
  const [address, setAddress] = useState('')
  const [name, setName] = useState('')

  const chosen = channels.find((c) => c.id === channel)

  const save = useMutation({
    mutationFn: () => createRecipient({ channel, address: address.trim(), name: name.trim() }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['recipients'] })
      onClose()
    },
  })

  return (
    <Modal title="Add a recipient" onClose={onClose}>
      <div className="flex flex-col gap-3">
        <div>
          <span className={label}>Channel</span>
          <select
            className={field}
            value={channel}
            onChange={(e) => setChannel(e.target.value as ChannelId)}
          >
            {channels.map((c) => (
              <option key={c.id} value={c.id}>
                {c.label}
                {c.configured ? '' : ' — not configured'}
              </option>
            ))}
          </select>
          <p className="mt-1 text-xs text-slate-500">{CAVEATS[channel]}</p>
        </div>

        {chosen && !chosen.broadcast && (
          <div>
            <span className={label}>
              {channel === 'telegram' ? 'Chat id' : channel === 'whatsapp' ? 'Phone number' : 'Address'}
            </span>
            <input
              className={field}
              autoFocus
              placeholder={PLACEHOLDER[channel]}
              value={address}
              onChange={(e) => setAddress(e.target.value)}
            />
          </div>
        )}

        <div>
          <span className={label}>Name (optional)</span>
          <input className={field} value={name} onChange={(e) => setName(e.target.value)} />
        </div>

        {chosen && !chosen.configured && (
          <p className="rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-300">
            {chosen.label} is not configured yet ({chosen.detail}). You can add the recipient now
            — sends will be skipped, not failed, until it is set up.
          </p>
        )}

        {save.error && <p className="text-xs text-rose-400">{String(save.error)}</p>}

        <div className="flex flex-col gap-2 lg:flex-row lg:justify-end">
          <button className={quietBtn} onClick={onClose}>
            Cancel
          </button>
          <button
            className={primaryBtn}
            disabled={save.isPending || (!!chosen && !chosen.broadcast && !address.trim())}
            onClick={() => save.mutate()}
          >
            {save.isPending ? 'Adding…' : 'Add'}
          </button>
        </div>
      </div>
    </Modal>
  )
}
