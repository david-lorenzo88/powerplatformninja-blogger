import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { createNewsletter, listIssues, listNewsletters } from '../api/client'
import { Modal } from '../components/Modal'
import { LettersSubNav } from '../components/SubNav'
import { useOnline } from '../hooks/useOnline'
import { describeSchedule, relativeTime, relativeToNow } from '../lib/format'
import { card, field, label, primaryBtn, quietBtn, rowCard } from '../lib/ui'

export function NewslettersScreen() {
  const navigate = useNavigate()
  const online = useOnline()
  const [creating, setCreating] = useState(false)

  const letters = useQuery({ queryKey: ['newsletters'], queryFn: listNewsletters })

  return (
    <div className="flex h-full flex-col">
      <div className="shrink-0 border-b border-slate-800 px-4 py-4 lg:px-6">
        <div className="flex items-baseline gap-3">
          <h1 className="text-lg font-semibold text-slate-100">Newsletters</h1>
          <span className="text-sm text-slate-500">{letters.data?.length ?? 0}</span>
          <button
            className={`${primaryBtn} ml-auto`}
            disabled={!online}
            onClick={() => setCreating(true)}
          >
            New newsletter
          </button>
        </div>
        <div className="mt-3">
          <LettersSubNav />
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-auto p-4 lg:px-6">
        {letters.isLoading ? (
          <p className="text-sm text-slate-500">loading…</p>
        ) : (letters.data ?? []).length === 0 ? (
          <div className="rounded-xl border border-dashed border-slate-800 p-6 text-sm text-slate-500">
            <p>No newsletters yet.</p>
            <p className="mt-2">
              A newsletter draws from one or more feed groups and generates an issue on a
              schedule. Nothing is sent — you read the issue here and decide.
            </p>
          </div>
        ) : (
          <ul className="flex flex-col gap-2">
            {(letters.data ?? []).map((n) => (
              <li key={n.id}>
                <button className={rowCard} onClick={() => navigate(`/newsletters/${n.id}`)}>
                  <div className="flex items-baseline gap-2">
                    <span className="text-sm font-medium text-slate-200">{n.name}</span>
                    {!n.enabled && <span className="text-xs text-slate-600">paused</span>}
                    <span className="ml-auto text-xs text-slate-500">
                      {n.issue_count} issue{n.issue_count === 1 ? '' : 's'}
                    </span>
                  </div>
                  <div className="mt-1 text-xs text-slate-500">
                    {describeSchedule(n)}
                    {n.next_due_at ? ` · next ${relativeToNow(n.next_due_at)}` : ''}
                  </div>
                  {n.description && (
                    <p className="mt-1 text-xs text-slate-500">{n.description}</p>
                  )}
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>

      {creating && <NewNewsletterDialog onClose={() => setCreating(false)} />}
    </div>
  )
}

export function IssuesScreen() {
  const navigate = useNavigate()
  const issues = useQuery({ queryKey: ['issues'], queryFn: () => listIssues() })

  return (
    <div className="flex h-full flex-col">
      <div className="shrink-0 border-b border-slate-800 px-4 py-4 lg:px-6">
        <div className="flex items-baseline gap-3">
          <h1 className="text-lg font-semibold text-slate-100">Issues</h1>
          <span className="text-sm text-slate-500">{issues.data?.length ?? 0}</span>
        </div>
        <div className="mt-3">
          <LettersSubNav />
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-auto p-4 lg:px-6">
        {issues.isLoading ? (
          <p className="text-sm text-slate-500">loading…</p>
        ) : (issues.data ?? []).length === 0 ? (
          <p className="text-sm text-slate-500">Nothing generated yet.</p>
        ) : (
          <ul className="flex flex-col gap-2">
            {(issues.data ?? []).map((i) => (
              <li key={i.id}>
                <button
                  className={rowCard}
                  onClick={() => navigate(`/newsletters/issues/${i.id}`)}
                >
                  <div className="flex items-baseline gap-2">
                    <span className="text-sm font-medium text-slate-200">
                      {i.subject || `Issue #${i.number}`}
                    </span>
                    <IssueStatusChip status={i.status} />
                  </div>
                  <div className="mt-1 text-xs text-slate-500">
                    {i.newsletter_name} · #{i.number} · {i.item_count} item
                    {i.item_count === 1 ? '' : 's'}
                    {i.created_at ? ` · ${relativeTime(i.created_at)}` : ''}
                  </div>
                  {i.status === 'skipped' && i.error && (
                    <p className="mt-1 text-xs text-slate-600">{i.error}</p>
                  )}
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  )
}

const ISSUE_STATUS: Record<string, string> = {
  draft: 'bg-slate-700/40 text-slate-300',
  ready: 'bg-accent/15 text-accent',
  sending: 'bg-amber-500/15 text-amber-300',
  sent: 'bg-emerald-500/15 text-emerald-300',
  failed: 'bg-rose-500/15 text-rose-300',
  // Not a failure: the week was quiet and the run said so rather than padding.
  skipped: 'bg-slate-800 text-slate-500',
}

export function IssueStatusChip({ status }: { status: string }) {
  return (
    <span
      className={`rounded-full px-2 py-0.5 text-xs font-medium ${
        ISSUE_STATUS[status] ?? ISSUE_STATUS.draft
      }`}
    >
      {status}
    </span>
  )
}



function NewNewsletterDialog({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient()
  const navigate = useNavigate()
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')

  const save = useMutation({
    mutationFn: () => createNewsletter({ name: name.trim(), description: description.trim() }),
    onSuccess: (created) => {
      qc.invalidateQueries({ queryKey: ['newsletters'] })
      onClose()
      // Straight to the detail screen: a newsletter with no groups and no
      // schedule cannot do anything yet, and that is the next thing to fix.
      navigate(`/newsletters/${created.id}`)
    },
  })

  return (
    <Modal title="New newsletter" onClose={onClose}>
      <div className="flex flex-col gap-3">
        <div>
          <span className={label}>Name</span>
          <input
            className={field}
            autoFocus
            placeholder="AI & Microsoft weekly"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </div>
        <div>
          <span className={label}>What it is for (optional)</span>
          <input
            className={field}
            placeholder="What shipped this week, for people who build things"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          />
        </div>
        <p className={`${card} p-3 text-xs text-slate-500`}>
          You will pick its feed groups and schedule next. Nothing sends: an issue is
          generated for you to read.
        </p>
        {save.error && <p className="text-xs text-rose-400">{String(save.error)}</p>}
        <div className="flex flex-col gap-2 lg:flex-row lg:justify-end">
          <button className={quietBtn} onClick={onClose}>
            Cancel
          </button>
          <button
            className={primaryBtn}
            disabled={!name.trim() || save.isPending}
            onClick={() => save.mutate()}
          >
            {save.isPending ? 'Creating…' : 'Create'}
          </button>
        </div>
      </div>
    </Modal>
  )
}
