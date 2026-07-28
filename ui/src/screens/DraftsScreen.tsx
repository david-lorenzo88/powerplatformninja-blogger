import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { draftCoverUrl, getDraft, listDrafts, publishDraft, putDraft } from '../api/client'
import type { DraftListItem } from '../api/types'
import { CodeEditor } from '../components/CodeEditor'
import { Markdown } from '../components/Markdown'
import { Modal } from '../components/Modal'

type Tab = 'read' | 'edit' | 'review' | 'cover'

export function DraftsScreen() {
  const [selected, setSelected] = useState<string | null>(null)
  const drafts = useQuery({ queryKey: ['drafts'], queryFn: listDrafts })

  useEffect(() => {
    if (!selected && drafts.data && drafts.data.length > 0) setSelected(drafts.data[0].file)
  }, [drafts.data, selected])

  return (
    <div className="flex h-full">
      <aside className="w-72 shrink-0 overflow-auto border-r border-slate-800 p-3">
        <h2 className="px-2 pb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">
          Drafts
        </h2>
        {drafts.data?.length === 0 && (
          <p className="px-2 text-sm text-slate-500">No drafts yet. Run a write.</p>
        )}
        <ul className="space-y-1">
          {drafts.data?.map((d) => (
            <li key={d.file}>
              <button
                onClick={() => setSelected(d.file)}
                className={`block w-full rounded-lg border px-3 py-2 text-left ${
                  selected === d.file
                    ? 'border-accent bg-accent/10'
                    : 'border-slate-800 hover:border-slate-600'
                }`}
              >
                <div className="text-sm font-medium text-slate-200">{d.title ?? d.file}</div>
                <div className="mt-1 flex flex-wrap items-center gap-2 text-[11px] text-slate-500">
                  {d.review && (
                    <span
                      className={`rounded px-1.5 ${
                        d.review.approved
                          ? 'bg-emerald-500/15 text-emerald-300'
                          : 'bg-amber-500/15 text-amber-300'
                      }`}
                    >
                      {d.review.approved ? 'approved' : 'not approved'} · {d.review.score}
                    </span>
                  )}
                  {d.word_count != null && <span>{d.word_count} words</span>}
                  {d.read_minutes != null && <span>· {d.read_minutes} min</span>}
                </div>
              </button>
            </li>
          ))}
        </ul>
      </aside>

      {selected ? (
        <DraftDetail key={selected} name={selected} meta={drafts.data?.find((d) => d.file === selected)} />
      ) : (
        <div className="flex flex-1 items-center justify-center text-sm text-slate-500">
          {drafts.isLoading ? 'loading…' : 'Select a draft.'}
        </div>
      )}
    </div>
  )
}

function DraftDetail({ name, meta }: { name: string; meta?: DraftListItem }) {
  const qc = useQueryClient()
  const [tab, setTab] = useState<Tab>('read')
  const [publishing, setPublishing] = useState(false)

  const draft = useQuery({ queryKey: ['draft', name], queryFn: () => getDraft(name) })

  const [markdown, setMarkdown] = useState('')
  const [dirty, setDirty] = useState(false)
  useEffect(() => {
    if (draft.data) {
      setMarkdown(draft.data.markdown)
      setDirty(false)
    }
  }, [draft.data])

  const save = useMutation({
    mutationFn: () => putDraft(name, markdown),
    onSuccess: () => {
      setDirty(false)
      qc.invalidateQueries({ queryKey: ['draft', name] })
      qc.invalidateQueries({ queryKey: ['drafts'] })
    },
  })

  return (
    <div className="flex min-w-0 flex-1 flex-col">
      <div className="shrink-0 border-b border-slate-800 px-6 py-3">
        <div className="flex items-center gap-3">
          <h1 className="truncate text-lg font-semibold text-slate-100">
            {meta?.title ?? name}
          </h1>
          <div className="ml-auto flex items-center gap-2">
            {tab === 'edit' && (
              <button
                onClick={() => save.mutate()}
                disabled={!dirty || save.isPending}
                className="rounded-lg border border-slate-700 px-3 py-1.5 text-sm text-slate-200 hover:border-accent disabled:opacity-40"
              >
                {save.isPending ? 'Saving…' : dirty ? 'Save edits' : 'Saved'}
              </button>
            )}
            <button
              onClick={() => setPublishing(true)}
              className="rounded-lg bg-accent px-4 py-1.5 text-sm font-semibold text-slate-950 hover:bg-accent-strong"
            >
              Publish…
            </button>
          </div>
        </div>
        <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-slate-500">
          {meta?.category && <span>{meta.category}</span>}
          {meta?.word_count != null && <span>· {meta.word_count} words</span>}
          {meta?.read_minutes != null && <span>· {meta.read_minutes} min</span>}
          {meta?.tags?.slice(0, 4).map((t) => (
            <span key={t} className="rounded bg-slate-800 px-1.5 py-0.5 text-slate-400">
              {t}
            </span>
          ))}
        </div>
        <div className="mt-3 flex gap-1">
          {(['read', 'edit', 'review', 'cover'] as Tab[]).map((t) => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={`rounded-md px-3 py-1 text-sm font-medium capitalize ${
                tab === t ? 'bg-accent/15 text-accent' : 'text-slate-400 hover:text-slate-200'
              }`}
            >
              {t}
            </button>
          ))}
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-auto">
        {draft.isLoading ? (
          <p className="p-6 text-sm text-slate-500">loading…</p>
        ) : tab === 'read' ? (
          <div className="mx-auto max-w-3xl p-6">
            <Markdown>{draft.data?.markdown ?? ''}</Markdown>
          </div>
        ) : tab === 'edit' ? (
          <CodeEditor
            value={markdown}
            format="markdown"
            onChange={(v) => {
              setMarkdown(v)
              setDirty(true)
            }}
          />
        ) : tab === 'review' ? (
          <div className="mx-auto max-w-3xl p-6">
            {draft.data?.review ? (
              <Markdown>{draft.data.review}</Markdown>
            ) : (
              <p className="text-sm text-slate-500">No review report for this draft.</p>
            )}
          </div>
        ) : (
          <div className="p-6">
            {meta?.has_cover ? (
              <img
                src={draftCoverUrl(name)}
                alt="cover"
                className="max-h-[70vh] rounded-lg border border-slate-800"
              />
            ) : (
              <p className="text-sm text-slate-500">No cover for this draft.</p>
            )}
          </div>
        )}
      </div>

      {publishing && (
        <PublishDialog name={name} title={meta?.title ?? name} onClose={() => setPublishing(false)} />
      )}
    </div>
  )
}

function PublishDialog({
  name,
  title,
  onClose,
}: {
  name: string
  title: string
  onClose: () => void
}) {
  const qc = useQueryClient()
  const pub = useMutation({
    mutationFn: (status: string) => publishDraft(name, status),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['drafts'] }),
  })

  const result = pub.data as { post_id?: number | string; edit_link?: string } | undefined

  return (
    <Modal title="Publish to WordPress" onClose={onClose}>
      {result ? (
        <div className="space-y-3 text-sm text-slate-300">
          <p className="text-emerald-300">Pushed to WordPress.</p>
          {result.post_id != null && <p>Post {String(result.post_id)}</p>}
          {result.edit_link && (
            <a
              href={result.edit_link}
              target="_blank"
              rel="noreferrer"
              className="text-accent hover:underline"
            >
              Open in WordPress ↗
            </a>
          )}
          <div className="flex justify-end">
            <button onClick={onClose} className="rounded-lg px-4 py-2 text-sm text-slate-400 hover:text-slate-200">
              Close
            </button>
          </div>
        </div>
      ) : (
        <div className="space-y-4">
          <p className="text-sm text-slate-300">
            This writes to the live blog. <span className="text-slate-100">{title}</span>
          </p>
          <p className="text-xs text-slate-500">
            “WordPress draft” updates the post in place and leaves it unpublished for review.
            “Publish live” makes it public immediately.
          </p>
          {pub.error != null && (
            <p className="rounded-lg bg-rose-500/10 px-3 py-2 text-xs text-rose-300">
              {pub.error instanceof Error ? pub.error.message : String(pub.error)}
            </p>
          )}
          <div className="flex justify-end gap-2">
            <button onClick={onClose} className="rounded-lg px-4 py-2 text-sm text-slate-400 hover:text-slate-200">
              Cancel
            </button>
            <button
              onClick={() => pub.mutate('draft')}
              disabled={pub.isPending}
              className="rounded-lg border border-slate-600 px-4 py-2 text-sm text-slate-200 hover:border-accent disabled:opacity-50"
            >
              {pub.isPending ? 'Working…' : 'Save as WordPress draft'}
            </button>
            <button
              onClick={() => pub.mutate('publish')}
              disabled={pub.isPending}
              className="rounded-lg bg-rose-500/90 px-4 py-2 text-sm font-semibold text-white hover:bg-rose-500 disabled:opacity-50"
            >
              Publish live
            </button>
          </div>
        </div>
      )}
    </Modal>
  )
}
