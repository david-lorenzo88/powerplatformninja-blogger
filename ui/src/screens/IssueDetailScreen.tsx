import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useParams } from 'react-router-dom'
import {
  getDeliveries,
  getIssue,
  issueHtmlUrl,
  listChannels,
  listRecipients,
  retryIssue,
  sendIssue,
  updateIssue,
} from '../api/client'
import { Modal } from '../components/Modal'
import { useOnline } from '../hooks/useOnline'
import { formatTime } from '../lib/format'
import { card, field, ghostBtn, label, primaryBtn, quietBtn } from '../lib/ui'
import { IssueStatusChip } from './NewslettersScreen'

type Tab = 'preview' | 'edit' | 'items' | 'delivery'

export function IssueDetailScreen() {
  const { id } = useParams()
  const issueId = Number(id)
  const online = useOnline()
  const qc = useQueryClient()
  const [tab, setTab] = useState<Tab>('preview')
  const [confirming, setConfirming] = useState(false)

  const issue = useQuery({
    queryKey: ['issue', issueId],
    queryFn: () => getIssue(issueId),
    enabled: Number.isFinite(issueId),
  })

  const [subject, setSubject] = useState('')
  const [intro, setIntro] = useState('')
  useEffect(() => {
    if (issue.data) {
      setSubject(issue.data.subject)
      setIntro(issue.data.intro)
    }
  }, [issue.data])

  const save = useMutation({
    mutationFn: () => updateIssue(issueId, { subject, intro }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['issue', issueId] }),
  })

  // Polled while anything is still in flight, then left alone — a delivery
  // that has settled does not change again.
  const deliveries = useQuery({
    queryKey: ['deliveries', issueId],
    queryFn: () => getDeliveries(issueId),
    enabled: Number.isFinite(issueId),
    refetchInterval: (q) => (q.state.data && q.state.data.pending > 0 ? 3000 : false),
  })

  const afterSend = () => {
    qc.invalidateQueries({ queryKey: ['issue', issueId] })
    qc.invalidateQueries({ queryKey: ['deliveries', issueId] })
    qc.invalidateQueries({ queryKey: ['runs'] })
  }
  const send = useMutation({ mutationFn: () => sendIssue(issueId), onSuccess: afterSend })
  const retry = useMutation({ mutationFn: () => retryIssue(issueId), onSuccess: afterSend })

  if (issue.isLoading) return <p className="p-6 text-sm text-slate-500">loading…</p>
  if (!issue.data) return <p className="p-6 text-sm text-slate-500">No such issue.</p>
  const i = issue.data
  const settled = i.status === 'sending' || i.status === 'sent'
  const failed = deliveries.data?.failed ?? 0

  return (
    <div className="flex h-full flex-col">
      <div className="shrink-0 border-b border-slate-800 px-4 py-4 lg:px-6">
        <Link
          to={`/newsletters/${i.newsletter_id}`}
          className="text-xs text-slate-500 hover:text-slate-300"
        >
          ← {i.newsletter_name}
        </Link>
        <div className="mt-2 flex items-start gap-3">
          <h1 className="flex-1 text-lg font-semibold text-slate-100">
            {i.subject || `Issue #${i.number}`}
          </h1>
          <IssueStatusChip status={i.status} />
        </div>
        <p className="mt-1 text-xs text-slate-500">
          #{i.number} · {i.item_count} item{i.item_count === 1 ? '' : 's'} ·{' '}
          {i.created_at ? formatTime(i.created_at) : ''}
          {i.window_from ? ` · covering ${i.window_from.slice(0, 10)} to ${(i.window_to ?? '').slice(0, 10)}` : ''}
        </p>

        {i.status === 'skipped' && (
          <p className="mt-3 rounded-lg border border-slate-700 bg-slate-900/60 px-3 py-2 text-xs text-slate-400">
            No issue was written: {i.error || 'not enough new material'}. Nothing went wrong —
            the run decided there was too little to say and stopped before calling a model.
          </p>
        )}

        {i.status !== 'skipped' && (
          <nav className="mt-3 flex gap-1">
            {(['preview', 'edit', 'items', 'delivery'] as Tab[]).map((t) => (
              <button
                key={t}
                onClick={() => setTab(t)}
                className={`min-h-11 rounded-lg px-3 text-sm capitalize transition-colors lg:min-h-0 lg:py-1.5 ${
                  tab === t
                    ? 'bg-accent/15 text-accent'
                    : 'text-slate-400 hover:bg-slate-800/60 hover:text-slate-200'
                }`}
              >
                {t}
              </button>
            ))}
          </nav>
        )}
      </div>

      <div className="min-h-0 flex-1 overflow-auto">
        {i.status === 'skipped' ? null : tab === 'preview' ? (
          // A sandboxed iframe, not dangerouslySetInnerHTML: the email carries
          // its own inlined styles and a table layout, and dropping that into
          // the document would leak straight into the app shell.
          <iframe
            title="Issue preview"
            src={issueHtmlUrl(i.id)}
            sandbox=""
            className="h-full min-h-[60vh] w-full border-0 bg-white"
          />
        ) : tab === 'edit' ? (
          <div className="mx-auto max-w-3xl p-4 lg:p-6">
            {settled && (
              <p className="mb-3 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-300">
                This issue has already been {i.status}. It can no longer be edited.
              </p>
            )}
            <div className="flex flex-col gap-3">
              <div>
                <span className={label}>Subject</span>
                <input
                  className={field}
                  value={subject}
                  disabled={settled || !online}
                  onChange={(e) => setSubject(e.target.value)}
                />
              </div>
              <div>
                <span className={label}>Intro</span>
                <textarea
                  className={`${field} min-h-24`}
                  value={intro}
                  disabled={settled || !online}
                  onChange={(e) => setIntro(e.target.value)}
                />
              </div>
              {save.error && <p className="text-xs text-rose-400">{String(save.error)}</p>}
              <div>
                <button
                  className={primaryBtn}
                  disabled={settled || !online || save.isPending}
                  onClick={() => save.mutate()}
                >
                  {save.isPending ? 'Saving…' : 'Save'}
                </button>
              </div>
              <details className={`${card} p-3`}>
                <summary className="cursor-pointer text-xs text-slate-500">
                  Markdown source
                </summary>
                <pre className="mt-2 overflow-x-auto whitespace-pre-wrap text-xs text-slate-400">
                  {i.markdown}
                </pre>
              </details>
            </div>
          </div>
        ) : tab === 'delivery' ? (
          <div className="mx-auto max-w-3xl p-4 lg:p-6">
            {(deliveries.data?.deliveries ?? []).length === 0 ? (
              <p className="text-sm text-slate-500">Not sent yet.</p>
            ) : (
              <ul className="flex flex-col gap-2">
                {(deliveries.data?.deliveries ?? []).map((d) => (
                  <li key={d.id} className={`${card} p-3`}>
                    <div className="flex items-baseline gap-2">
                      <span className="text-sm text-slate-200">{d.recipient}</span>
                      <span className="text-xs text-slate-500">{d.channel}</span>
                      <span
                        className={`ml-auto text-xs ${
                          d.status === 'sent'
                            ? 'text-emerald-300'
                            : d.status === 'failed'
                              ? 'text-rose-300'
                              : d.status === 'skipped'
                                ? 'text-slate-500'
                                : 'text-amber-300'
                        }`}
                      >
                        {d.status}
                      </span>
                    </div>
                    {d.error && <p className="mt-1 text-xs text-rose-400">{d.error}</p>}
                    {d.next_retry_at && (
                      <p className="mt-1 text-xs text-slate-600">
                        retrying {formatTime(d.next_retry_at)}
                      </p>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </div>
        ) : (
          <div className="mx-auto max-w-3xl p-4 lg:p-6">
            <p className="mb-3 text-xs text-slate-500">
              Every link here came from a harvested article, not from the model — anything
              the editor named that was not in its candidate list was dropped before this
              issue was written.
            </p>
            <ul className="flex flex-col gap-2">
              {i.items.map((item) => (
                <li key={`${item.article_id}-${item.position}`} className={`${card} p-3`}>
                  <a
                    href={item.url}
                    target="_blank"
                    rel="noreferrer noopener"
                    className="text-sm font-medium text-slate-200 hover:text-accent"
                  >
                    {item.headline}
                  </a>
                  {item.blurb && <p className="mt-1 text-sm text-slate-400">{item.blurb}</p>}
                  <p className="mt-1 text-xs text-slate-600">{item.section}</p>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>

      {i.status !== 'skipped' && (
        <div className="shrink-0 border-t border-slate-800 px-4 py-3 pb-safe lg:px-6">
          <div className="flex flex-col gap-2 lg:flex-row lg:items-center">
            <button
              className={primaryBtn}
              disabled={!online || settled || send.isPending}
              onClick={() => setConfirming(true)}
            >
              {settled ? `Already ${i.status}` : send.isPending ? 'Sending…' : 'Send…'}
            </button>
            {failed > 0 && (
              <button
                className={ghostBtn}
                disabled={!online || retry.isPending}
                onClick={() => retry.mutate()}
              >
                {retry.isPending ? 'Retrying…' : `Retry ${failed} failure${failed === 1 ? '' : 's'}`}
              </button>
            )}
            {deliveries.data && deliveries.data.total > 0 && (
              <p className="text-xs text-slate-500 lg:ml-auto">
                {deliveries.data.sent} sent
                {deliveries.data.failed ? ` · ${deliveries.data.failed} failed` : ''}
                {deliveries.data.pending ? ` · ${deliveries.data.pending} pending` : ''}
                {deliveries.data.skipped ? ` · ${deliveries.data.skipped} skipped` : ''}
              </p>
            )}
          </div>
        </div>
      )}

      {confirming && (
        <SendDialog onClose={() => setConfirming(false)} onSend={() => send.mutate()} />
      )}
    </div>
  )
}

// Sending is the one irreversible thing this app does — an email cannot be
// un-sent — so it goes behind a confirmation that says exactly who will receive
// it, on which channel, rather than a count.
function SendDialog({ onClose, onSend }: { onClose: () => void; onSend: () => void }) {
  const channels = useQuery({ queryKey: ['channels'], queryFn: listChannels })
  const recipients = useQuery({ queryKey: ['recipients'], queryFn: listRecipients })

  const active = (recipients.data ?? []).filter((r) => r.enabled && !r.failed_at)
  const configured = new Map((channels.data ?? []).map((c) => [c.id, c]))
  const willSend = active.filter((r) => configured.get(r.channel)?.configured)
  const willSkip = active.filter((r) => !configured.get(r.channel)?.configured)

  return (
    <Modal title="Send this issue" onClose={onClose}>
      <div className="flex flex-col gap-3">
        {willSend.length === 0 ? (
          <p className="text-sm text-amber-400">
            Nobody is set up to receive this. Add a recipient on a configured channel — or
            use <span className="text-slate-300">Copy out by hand</span>, which needs no setup.
          </p>
        ) : (
          <>
            <p className="text-sm text-slate-300">
              This will go to {willSend.length} recipient{willSend.length === 1 ? '' : 's'}:
            </p>
            <ul className={`${card} max-h-56 overflow-auto p-3`}>
              {willSend.map((r) => (
                <li key={r.id} className="py-0.5 text-xs text-slate-400">
                  <span className="text-slate-300">{r.name}</span>
                  <span className="text-slate-600"> · {r.channel}</span>
                  {r.address && <span className="text-slate-600"> · {r.address}</span>}
                </li>
              ))}
            </ul>
          </>
        )}

        {willSkip.length > 0 && (
          <p className="rounded-lg border border-slate-700 bg-slate-900/60 px-3 py-2 text-xs text-slate-400">
            {willSkip.length} recipient{willSkip.length === 1 ? '' : 's'} will be skipped — their
            channel is not configured. Skipped is not failed; nothing is lost and they can be
            sent to later.
          </p>
        )}

        <p className="text-xs text-slate-600">This cannot be undone.</p>

        <div className="flex flex-col gap-2 lg:flex-row lg:justify-end">
          <button className={quietBtn} onClick={onClose}>
            Cancel
          </button>
          <button
            className={primaryBtn}
            disabled={willSend.length === 0}
            onClick={() => {
              onSend()
              onClose()
            }}
          >
            Send now
          </button>
        </div>
      </div>
    </Modal>
  )
}
