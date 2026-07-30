import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { listPosts } from '../api/client'
import type { PostSummary } from '../api/types'
import { formatTime } from '../lib/format'
import { ScorePill } from './TopicIdeasScreen'

const field =
  'rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 text-sm text-slate-200 placeholder:text-slate-600 focus:border-accent focus:outline-none'

type Tri = 'all' | 'yes' | 'no'

export function DraftsScreen() {
  const navigate = useNavigate()
  const posts = useQuery({
    queryKey: ['posts'],
    queryFn: () => listPosts(),
    refetchInterval: 5000,
  })

  const [q, setQ] = useState('')
  const [approved, setApproved] = useState<Tri>('all')
  const [published, setPublished] = useState<Tri>('all')
  const [hasCover, setHasCover] = useState<Tri>('all')

  const filtered = useMemo(() => {
    const term = q.trim().toLowerCase()
    return (posts.data ?? []).filter((p) => {
      const isApproved = !!p.current_version?.approved
      const isPublished = p.wordpress_post_id != null
      const cover = !!p.current_version?.has_cover
      if (approved !== 'all' && (approved === 'yes') !== isApproved) return false
      if (published !== 'all' && (published === 'yes') !== isPublished) return false
      if (hasCover !== 'all' && (hasCover === 'yes') !== cover) return false
      if (term && !`${p.title} ${p.slug}`.toLowerCase().includes(term)) return false
      return true
    })
  }, [posts.data, q, approved, published, hasCover])

  return (
    <div className="flex h-full flex-col">
      <div className="shrink-0 border-b border-slate-800 px-6 py-4">
        <div className="flex items-baseline gap-3">
          <h1 className="text-lg font-semibold text-slate-100">Drafts</h1>
          <span className="text-sm text-slate-500">
            {filtered.length}
            {posts.data && filtered.length !== posts.data.length ? ` of ${posts.data.length}` : ''}
          </span>
        </div>
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <input
            className={`${field} w-56`}
            placeholder="Search title or slug…"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
          <TriSelect label="Approved" value={approved} onChange={setApproved} />
          <TriSelect label="Published" value={published} onChange={setPublished} />
          <TriSelect label="Cover" value={hasCover} onChange={setHasCover} />
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-auto">
        {posts.isLoading ? (
          <p className="p-6 text-sm text-slate-500">loading…</p>
        ) : filtered.length === 0 ? (
          <p className="p-6 text-sm text-slate-500">
            {posts.data?.length ? 'No drafts match these filters.' : 'No drafts yet. Run a write.'}
          </p>
        ) : (
          <table className="w-full border-collapse text-sm">
            <thead className="sticky top-0 bg-slate-950/90 text-left text-xs uppercase tracking-wide text-slate-500 backdrop-blur">
              <tr>
                <th className="px-6 py-2 font-medium">Title</th>
                <th className="px-3 py-2 font-medium">Review</th>
                <th className="px-3 py-2 font-medium">Versions</th>
                <th className="px-3 py-2 font-medium">WordPress</th>
                <th className="px-3 py-2 font-medium">Updated</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((p) => (
                <PostRow key={p.id} post={p} onOpen={() => navigate(`/drafts/${p.id}`)} />
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}

function PostRow({ post, onOpen }: { post: PostSummary; onOpen: () => void }) {
  const cur = post.current_version
  return (
    <tr
      onClick={onOpen}
      className="cursor-pointer border-b border-slate-800/70 hover:bg-slate-800/30"
    >
      <td className="px-6 py-3">
        <div className="font-medium text-slate-200">{post.title || post.slug}</div>
        <div className="mt-0.5 flex items-center gap-2 text-xs text-slate-500">
          {post.slug}
          {cur?.has_cover && <span className="text-slate-600">· cover</span>}
        </div>
      </td>
      <td className="px-3 py-3">
        {cur ? (
          <span className="flex items-center gap-1.5">
            <span
              className={`rounded px-1.5 py-0.5 text-[11px] ${
                cur.approved
                  ? 'bg-emerald-500/15 text-emerald-300'
                  : 'bg-amber-500/15 text-amber-300'
              }`}
            >
              {cur.approved ? 'approved' : 'not approved'}
            </span>
            <ScorePill score={cur.score} />
          </span>
        ) : (
          <span className="text-slate-600">—</span>
        )}
      </td>
      <td className="px-3 py-3 text-slate-400">{post.version_count}</td>
      <td className="px-3 py-3">
        {post.wordpress_post_id != null ? (
          <span className="rounded bg-sky-500/15 px-1.5 py-0.5 text-[11px] text-sky-300">
            {post.status === 'published' ? 'published' : 'draft'} · {post.wordpress_post_id}
          </span>
        ) : (
          <span className="text-[11px] text-slate-600">not pushed</span>
        )}
      </td>
      <td className="px-3 py-3 text-xs text-slate-500">{formatTime(post.updated_at)}</td>
    </tr>
  )
}

function TriSelect({
  label,
  value,
  onChange,
}: {
  label: string
  value: Tri
  onChange: (v: Tri) => void
}) {
  return (
    <select className={field} value={value} onChange={(e) => onChange(e.target.value as Tri)}>
      <option value="all">{label}: any</option>
      <option value="yes">{label}: yes</option>
      <option value="no">{label}: no</option>
    </select>
  )
}
