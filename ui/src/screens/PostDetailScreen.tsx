import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate, useParams } from 'react-router-dom'
import {
  draftCoverUrl,
  getDraft,
  getPost,
  putDraft,
  regenerateCover,
  regeneratePost,
} from '../api/client'
import type { DraftVersion, Post } from '../api/types'
import { CodeEditor } from '../components/CodeEditor'
import { Markdown } from '../components/Markdown'
import { Modal } from '../components/Modal'
import { PublishDialog } from '../components/PublishDialog'
import { formatTime } from '../lib/format'
import { ghostBtn, primaryBtn } from '../lib/ui'
import { ScorePill } from './TopicIdeasScreen'

type Tab = 'read' | 'edit' | 'review' | 'cover'

export function PostDetailScreen() {
  const { id } = useParams<{ id: string }>()
  const postId = Number(id)
  const post = useQuery({
    queryKey: ['post', postId],
    queryFn: () => getPost(postId),
    enabled: Number.isFinite(postId),
  })

  if (post.isLoading) return <p className="p-6 text-sm text-slate-500">loading…</p>
  if (post.isError || !post.data)
    return (
      <div className="p-6 text-sm text-slate-500">
        <Link to="/drafts" className="text-accent hover:underline">
          ← Drafts
        </Link>
        <p className="mt-4">This post could not be found.</p>
      </div>
    )

  return <PostDetail post={post.data} />
}

function PostDetail({ post }: { post: Post }) {
  const versions = post.versions
  const [selectedId, setSelectedId] = useState<number>(
    post.current_version?.id ?? versions[0]?.id ?? 0,
  )
  const [publishing, setPublishing] = useState(false)
  const [regenerating, setRegenerating] = useState(false)

  const selected = versions.find((v) => v.id === selectedId) ?? versions[0]

  return (
    <div className="flex h-full min-w-0 flex-col">
      {/* Header */}
      <div className="shrink-0 border-b border-slate-800 px-4 py-3 lg:px-6">
        <Link to="/drafts" className="text-xs text-accent hover:underline">
          ← Drafts
        </Link>
        <div className="mt-1 flex flex-col items-stretch gap-2 sm:flex-row sm:items-center sm:gap-3">
          <h1 className="truncate text-lg font-semibold text-slate-100">{post.title || post.slug}</h1>
          <div className="flex items-center gap-2 sm:ml-auto">
            <button onClick={() => setRegenerating(true)} className={`${ghostBtn} flex-1 sm:flex-none`}>
              Regenerate…
            </button>
            {selected && (
              <button onClick={() => setPublishing(true)} className={`${primaryBtn} flex-1 sm:flex-none`}>
                Publish…
              </button>
            )}
          </div>
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-slate-500">
          <span>{post.slug}</span>
          {post.topic_idea_id != null && (
            <Link to={`/topic-ideas/${post.topic_idea_id}`} className="text-accent hover:underline">
              ← from topic idea
            </Link>
          )}
          {post.wordpress_post_id != null && (
            <span className="rounded bg-sky-500/15 px-1.5 py-0.5 text-[11px] text-sky-300">
              {post.status === 'published' ? 'published' : 'draft'} · {post.wordpress_post_id}
            </span>
          )}
          {post.edit_link ? (
            <a
              href={post.edit_link}
              target="_blank"
              rel="noreferrer"
              className="text-accent hover:underline"
            >
              Open in WordPress ↗
            </a>
          ) : (
            <span className="text-slate-600">not on WordPress</span>
          )}
          <span>
            {post.version_count} version{post.version_count === 1 ? '' : 's'}
          </span>
        </div>
      </div>

      {/* Body: version list + selected version content. The 256px rail becomes a
          scrolling chip strip below lg — a version picker is a selector, and a
          selector costs a row on a phone, not a column. */}
      <div className="flex min-h-0 flex-1 flex-col lg:flex-row">
        <aside className="shrink-0 border-b border-slate-800 p-2 lg:w-64 lg:overflow-auto lg:border-b-0 lg:border-r lg:p-3">
          <h2 className="hidden px-2 pb-2 text-xs font-semibold uppercase tracking-wide text-slate-500 lg:block">
            Versions
          </h2>
          <ul className="flex gap-2 overflow-x-auto lg:flex-col lg:gap-0 lg:space-y-1 lg:overflow-visible">
            {versions.map((v) => (
              <li key={v.id} className="shrink-0 lg:shrink">
                <button
                  onClick={() => setSelectedId(v.id)}
                  className={`block min-h-11 w-full whitespace-nowrap rounded-lg border px-3 py-2 text-left lg:min-h-0 lg:whitespace-normal ${
                    selected?.id === v.id
                      ? 'border-accent bg-accent/10'
                      : 'border-slate-800 active:border-accent/60 lg:hover:border-slate-600'
                  }`}
                >
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-medium text-slate-200">v{v.version}</span>
                    {v.id === post.current_version?.id && (
                      <span className="rounded bg-slate-700/60 px-1 text-[10px] text-slate-300">
                        current
                      </span>
                    )}
                    <span className="lg:ml-auto">
                      <ScorePill score={v.score} />
                    </span>
                  </div>
                  <div className="mt-1 flex flex-wrap items-center gap-1.5 text-[11px] text-slate-500">
                    <span
                      className={
                        v.approved ? 'text-emerald-300' : 'text-amber-300'
                      }
                    >
                      {v.approved ? 'approved' : 'not approved'}
                    </span>
                    {v.reused_research && <span>· reused research</span>}
                    {v.instructions && <span>· guided</span>}
                  </div>
                  <div className="mt-0.5 hidden text-[11px] text-slate-600 lg:block">
                    {formatTime(v.created_at)}
                  </div>
                </button>
              </li>
            ))}
          </ul>
        </aside>

        {selected ? (
          <VersionView key={selected.id} version={selected} versions={versions} postId={post.id} />
        ) : (
          <div className="flex flex-1 items-center justify-center text-sm text-slate-500">
            This post has no versions yet.
          </div>
        )}
      </div>

      {publishing && selected && (
        <PublishDialog
          name={selected.markdown_file}
          title={selected.title || post.title}
          onClose={() => setPublishing(false)}
        />
      )}
      {regenerating && (
        <RegenerateDialog
          postId={post.id}
          reuseAvailable={versions.some((v) => v.dossier_path)}
          onClose={() => setRegenerating(false)}
        />
      )}
    </div>
  )
}

function VersionView({
  version,
  versions,
  postId,
}: {
  version: DraftVersion
  versions: DraftVersion[]
  postId: number
}) {
  const qc = useQueryClient()
  const [tab, setTab] = useState<Tab>('read')
  const [guidanceOpen, setGuidanceOpen] = useState(false)
  const name = version.markdown_file

  // Every guidance note that shaped this version — its own plus the earlier
  // ones it still inherits (the writer accumulates them server-side). Oldest
  // first, so reading top-to-bottom follows how the intent built up.
  const guidanceHistory = versions
    .filter((v) => v.version <= version.version && v.instructions.trim())
    .sort((a, b) => a.version - b.version)
  const draft = useQuery({
    queryKey: ['draft', name],
    queryFn: () => getDraft(name),
    enabled: !!name,
  })

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
      qc.invalidateQueries({ queryKey: ['post', postId] })
    },
  })

  return (
    <div className="flex min-w-0 flex-1 flex-col">
      <div className="flex shrink-0 items-center gap-1 overflow-x-auto border-b border-slate-800 px-4 py-2 lg:px-6">
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
        {tab === 'edit' && (
          <button
            onClick={() => save.mutate()}
            disabled={!dirty || save.isPending}
            className="ml-auto rounded-lg border border-slate-700 px-3 py-1 text-sm text-slate-200 hover:border-accent disabled:opacity-40"
          >
            {save.isPending ? 'Saving…' : dirty ? 'Save edits' : 'Saved'}
          </button>
        )}
      </div>

      {guidanceHistory.length > 0 && (
        <div className="shrink-0 border-b border-slate-800 bg-slate-900/40">
          <button
            onClick={() => setGuidanceOpen((o) => !o)}
            className="flex min-h-11 w-full items-center gap-2 px-4 py-2 text-left text-xs text-slate-400 hover:text-slate-200 lg:min-h-0 lg:px-6"
            aria-expanded={guidanceOpen}
          >
            <svg
              className={`h-3 w-3 shrink-0 transition-transform ${guidanceOpen ? 'rotate-90' : ''}`}
              viewBox="0 0 12 12"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.5"
            >
              <path d="M4 2.5 8 6l-4 3.5" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
            <span className="font-semibold uppercase tracking-wide">Guidance</span>
            <span className="rounded bg-slate-700/60 px-1.5 text-[10px] text-slate-300">
              {guidanceHistory.length}
            </span>
            {!guidanceOpen && (
              <span className="min-w-0 flex-1 truncate text-slate-500">
                {guidanceHistory[guidanceHistory.length - 1].instructions}
              </span>
            )}
          </button>
          {guidanceOpen && (
            <div className="max-h-56 space-y-3 overflow-auto px-4 pb-3 lg:px-6">
              {guidanceHistory.map((v) => (
                <div key={v.id} className="flex gap-3">
                  <span
                    className={`mt-0.5 shrink-0 rounded px-1.5 text-[11px] ${
                      v.id === version.id
                        ? 'bg-accent/15 text-accent'
                        : 'bg-slate-800 text-slate-400'
                    }`}
                  >
                    v{v.version}
                  </span>
                  <p className="whitespace-pre-wrap text-sm leading-relaxed text-slate-300">
                    {v.instructions}
                  </p>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

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
              <p className="text-sm text-slate-500">No review report for this version.</p>
            )}
          </div>
        ) : (
          <CoverPanel postId={postId} name={name} hasCover={version.has_cover} />
        )}
      </div>
    </div>
  )
}

function CoverPanel({
  postId,
  name,
  hasCover,
}: {
  postId: number
  name: string
  hasCover: boolean
}) {
  const qc = useQueryClient()
  const navigate = useNavigate()
  const [instructions, setInstructions] = useState('')
  const [broken, setBroken] = useState(false)

  const gen = useMutation({
    mutationFn: () => regenerateCover(postId, instructions),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ['runs'] })
      qc.invalidateQueries({ queryKey: ['post', postId] })
      navigate(`/runs/${res.id}`)
    },
  })

  const showImage = hasCover && !broken

  return (
    <div className="mx-auto max-w-2xl p-6">
      {showImage ? (
        <img
          src={draftCoverUrl(name)}
          alt="cover"
          onError={() => setBroken(true)}
          className="mb-6 max-h-[60dvh] rounded-lg border border-slate-800"
        />
      ) : (
        <p className="mb-6 rounded-lg border border-dashed border-slate-800 px-4 py-8 text-center text-sm text-slate-500">
          No cover yet. Generate one below.
        </p>
      )}

      <div className="rounded-xl border border-slate-800 bg-slate-900/40 p-4">
        <h3 className="text-sm font-semibold text-slate-200">
          {showImage ? 'Regenerate the cover' : 'Generate a cover'}
        </h3>
        <p className="mt-1 text-xs text-slate-500">
          There is one cover per post — this replaces it. Describe the art you want; leave blank to
          use the draft's own concept. You will land on the run to watch it.
        </p>
        <textarea
          className="mt-3 h-24 w-full resize-none rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-200 placeholder:text-slate-600 focus:border-accent focus:outline-none"
          placeholder="e.g. glowing wireframe data tables splitting into light shards, deep violet and cyan…"
          value={instructions}
          onChange={(e) => setInstructions(e.target.value)}
        />
        {gen.error != null && (
          <p className="mt-2 rounded-lg bg-rose-500/10 px-3 py-2 text-xs text-rose-300">
            {gen.error instanceof Error ? gen.error.message : String(gen.error)}
          </p>
        )}
        <div className="mt-3 flex justify-end">
          <button
            onClick={() => gen.mutate()}
            disabled={gen.isPending}
            className="rounded-lg bg-accent px-4 py-2 text-sm font-semibold text-slate-950 hover:bg-accent-strong disabled:opacity-50"
          >
            {gen.isPending ? 'Starting…' : showImage ? 'Regenerate cover' : 'Generate cover'}
          </button>
        </div>
      </div>
    </div>
  )
}

function RegenerateDialog({
  postId,
  reuseAvailable,
  onClose,
}: {
  postId: number
  reuseAvailable: boolean
  onClose: () => void
}) {
  const qc = useQueryClient()
  const navigate = useNavigate()
  const [instructions, setInstructions] = useState('')
  const [reuseResearch, setReuseResearch] = useState(reuseAvailable)
  const [push, setPush] = useState(false)
  const [cover, setCover] = useState(true)

  const mut = useMutation({
    mutationFn: () =>
      regeneratePost(postId, {
        instructions,
        reuse_research: reuseResearch,
        push,
        cover,
      }),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ['runs'] })
      qc.invalidateQueries({ queryKey: ['post', postId] })
      onClose()
      navigate(`/runs/${res.id}`)
    },
  })

  return (
    <Modal title="Regenerate this draft" onClose={onClose} width="max-w-xl">
      <div className="space-y-4">
        <p className="text-sm text-slate-300">
          Runs the write pipeline again to produce a new version. Add instructions the writer
          should honour — e.g. “lead with the migration steps”, “make it shorter”, “drop the FAQ”.
        </p>
        <div>
          <label className="mb-1 block text-xs font-medium text-slate-400">Instructions</label>
          <textarea
            className="h-28 w-full resize-none rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-200 placeholder:text-slate-600 focus:border-accent focus:outline-none"
            placeholder="What should change in this version?"
            value={instructions}
            onChange={(e) => setInstructions(e.target.value)}
          />
        </div>

        <label className="flex cursor-pointer items-start gap-2 text-sm text-slate-300">
          <input
            type="checkbox"
            checked={reuseResearch}
            disabled={!reuseAvailable}
            onChange={(e) => setReuseResearch(e.target.checked)}
            className="mt-0.5 h-4 w-4 accent-[#c084fc]"
          />
          <span>
            Reuse existing research
            <span className="block text-xs text-slate-500">
              {reuseAvailable
                ? 'Fast — skips the research stage and only rewrites from the saved dossier.'
                : 'No saved research found for this post; a fresh run is required.'}
            </span>
          </span>
        </label>

        <div className="flex flex-wrap gap-4 border-t border-slate-800 pt-3">
          <Check label="Push to WordPress" checked={push} onChange={setPush} />
          <Check label="Generate cover" checked={cover} onChange={setCover} />
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
            {mut.isPending ? 'Starting…' : 'Regenerate'}
          </button>
        </div>
      </div>
    </Modal>
  )
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
