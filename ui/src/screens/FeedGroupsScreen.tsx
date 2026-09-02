import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  createFeedGroup,
  deleteFeedGroup,
  getFeedGroup,
  listFeedGroups,
  listFeeds,
  setFeedGroupFeeds,
  setFeedGroupRealtime,
} from '../api/client'
import type { FeedGroup } from '../api/types'
import { Modal } from '../components/Modal'
import { NewsSubNav } from '../components/SubNav'
import { useOnline } from '../hooks/useOnline'
import { card, field, ghostBtn, label, primaryBtn, quietBtn, rowCard } from '../lib/ui'

// Groups are edited in place rather than on their own route: a group is a name
// and a set of checkboxes, and a detail screen for that is a navigation the
// operator has to undo. The membership editor opens over the list.
export function FeedGroupsScreen() {
  const online = useOnline()
  const qc = useQueryClient()
  const [creating, setCreating] = useState(false)
  const [editing, setEditing] = useState<number | null>(null)

  const groups = useQuery({ queryKey: ['feed-groups'], queryFn: listFeedGroups })

  return (
    <div className="flex h-full flex-col">
      <div className="shrink-0 border-b border-slate-800 px-4 py-4 lg:px-6">
        <div className="flex items-baseline gap-3">
          <h1 className="text-lg font-semibold text-slate-100">Groups</h1>
          <span className="text-sm text-slate-500">{groups.data?.length ?? 0}</span>
          <button
            className={`${primaryBtn} ml-auto`}
            disabled={!online}
            onClick={() => setCreating(true)}
          >
            New group
          </button>
        </div>
        <div className="mt-3">
          <NewsSubNav />
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-auto p-4 lg:px-6">
        {groups.isLoading ? (
          <p className="text-sm text-slate-500">loading…</p>
        ) : (groups.data ?? []).length === 0 ? (
          <div className="rounded-xl border border-dashed border-slate-800 p-6 text-sm text-slate-500">
            <p>No groups yet.</p>
            <p className="mt-2">
              A group bundles feeds that belong together — &ldquo;AI research&rdquo;,
              &ldquo;Microsoft&rdquo; — and is what a newsletter will draw from.
            </p>
          </div>
        ) : (
          <ul className="flex flex-col gap-2">
            {(groups.data ?? []).map((g) => (
              <li key={g.id}>
                <button className={rowCard} onClick={() => setEditing(g.id)}>
                  <div className="flex items-baseline gap-2">
                    <span className="text-sm font-medium text-slate-200">{g.name}</span>
                    <span className="text-xs text-slate-500">
                      {g.feed_count} feed{g.feed_count === 1 ? '' : 's'}
                    </span>
                    {g.feeds_realtime > 0 && (
                      <span className="text-xs text-cyan-300">
                        {g.feeds_realtime === g.feed_count
                          ? 'all watched'
                          : `${g.feeds_realtime} watched`}
                      </span>
                    )}
                  </div>
                  {g.description && (
                    <p className="mt-1 text-xs text-slate-500">{g.description}</p>
                  )}
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>

      {creating && <CreateGroupDialog onClose={() => setCreating(false)} />}
      {editing !== null && (
        <EditMembersDialog
          groupId={editing}
          onClose={() => {
            setEditing(null)
            qc.invalidateQueries({ queryKey: ['feed-groups'] })
          }}
        />
      )}
    </div>
  )
}

function CreateGroupDialog({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient()
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')

  const save = useMutation({
    mutationFn: () => createFeedGroup(name.trim(), description.trim()),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['feed-groups'] })
      onClose()
    },
  })

  return (
    <Modal title="New group" onClose={onClose}>
      <div className="flex flex-col gap-3">
        <div>
          <span className={label}>Name</span>
          <input
            className={field}
            autoFocus
            placeholder="AI research"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </div>
        <div>
          <span className={label}>What belongs here (optional)</span>
          <input
            className={field}
            placeholder="Papers and preprints worth reading"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          />
        </div>
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

function EditMembersDialog({ groupId, onClose }: { groupId: number; onClose: () => void }) {
  const qc = useQueryClient()
  const group = useQuery({ queryKey: ['feed-group', groupId], queryFn: () => getFeedGroup(groupId) })
  const feeds = useQuery({ queryKey: ['feeds'], queryFn: () => listFeeds() })

  // Seeded from the server once, then owned locally until saved — the same shape
  // the source-review screen uses for its per-candidate choices.
  const [selected, setSelected] = useState<number[] | null>(null)
  useEffect(() => {
    if (group.data && selected === null) setSelected(group.data.feed_ids ?? [])
  }, [group.data, selected])

  const save = useMutation({
    mutationFn: () => setFeedGroupFeeds(groupId, selected ?? []),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['feed-groups'] })
      qc.invalidateQueries({ queryKey: ['feeds'] })
      qc.invalidateQueries({ queryKey: ['articles'] })
      onClose()
    },
  })

  const remove = useMutation({
    mutationFn: () => deleteFeedGroup(groupId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['feed-groups'] })
      onClose()
    },
  })

  const chosen = selected ?? []

  return (
    <Modal title={group.data?.name ?? 'Group'} onClose={onClose}>
      <div className="flex flex-col gap-3">
        <WatchGroup groupId={groupId} group={group.data} />

        <div className="flex items-center gap-3 text-xs text-slate-500">
          <span>
            {chosen.length} of {feeds.data?.length ?? 0} selected
          </span>
          <button
            className={`${quietBtn} ml-auto px-2 text-xs`}
            onClick={() => setSelected((feeds.data ?? []).map((f) => f.id))}
          >
            All
          </button>
          <button className={`${quietBtn} px-2 text-xs`} onClick={() => setSelected([])}>
            None
          </button>
        </div>

        <ul className={`${card} max-h-72 overflow-auto p-1`}>
          {(feeds.data ?? []).map((f) => {
            const on = chosen.includes(f.id)
            return (
              <li key={f.id}>
                <label className="flex min-h-11 cursor-pointer items-center gap-3 rounded-lg px-2 text-sm text-slate-300 hover:bg-slate-800/50">
                  <input
                    type="checkbox"
                    className="h-6 w-6 shrink-0 accent-cyan-400 lg:h-4 lg:w-4"
                    checked={on}
                    onChange={() =>
                      setSelected((ids) =>
                        (ids ?? []).includes(f.id)
                          ? (ids ?? []).filter((i) => i !== f.id)
                          : [...(ids ?? []), f.id],
                      )
                    }
                  />
                  <span className="flex-1 truncate">
                    {f.name || f.title || f.url}
                    <span className="block text-xs text-slate-500">{f.domain}</span>
                  </span>
                </label>
              </li>
            )
          })}
        </ul>

        {save.error && <p className="text-xs text-rose-400">{String(save.error)}</p>}

        <div className="flex flex-col gap-2 lg:flex-row lg:items-center">
          <button
            className={ghostBtn}
            disabled={remove.isPending}
            onClick={() => remove.mutate()}
          >
            Delete group
          </button>
          <button className={`${quietBtn} lg:ml-auto`} onClick={onClose}>
            Cancel
          </button>
          <button className={primaryBtn} disabled={save.isPending} onClick={() => save.mutate()}>
            {save.isPending ? 'Saving…' : 'Save'}
          </button>
        </div>
      </div>
    </Modal>
  )
}

// Buttons rather than a checkbox, deliberately. Watching is a property of a
// *feed* — it is what puts one on the fifteen-minute cadence — so this is a bulk
// write over the group's members applied when pressed, not a setting the group
// remembers. A checkbox would promise that a feed added tomorrow inherits it,
// and it would not: a feed quietly joining the fast cadence is a bill nobody
// chose. The count above says what the group looks like now.
function WatchGroup({ groupId, group }: { groupId: number; group?: FeedGroup }) {
  const qc = useQueryClient()
  const watched = group?.feeds_realtime ?? 0
  const total = group?.feed_count ?? 0

  const apply = useMutation({
    mutationFn: (realtime: boolean) => setFeedGroupRealtime(groupId, realtime),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['feed-group', groupId] })
      qc.invalidateQueries({ queryKey: ['feed-groups'] })
      qc.invalidateQueries({ queryKey: ['feeds'] })
      qc.invalidateQueries({ queryKey: ['news-summary'] })
    },
  })

  if (total === 0) return null

  return (
    <div className={`${card} p-3`}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm text-slate-300">
          Watched closely:{' '}
          <span className={watched > 0 ? 'text-cyan-300' : 'text-slate-500'}>
            {watched} of {total}
          </span>
        </span>
        <div className="ml-auto flex gap-2">
          <button
            className={`${quietBtn} px-2 text-xs`}
            disabled={apply.isPending || watched === total}
            onClick={() => apply.mutate(true)}
          >
            Watch all
          </button>
          <button
            className={`${quietBtn} px-2 text-xs`}
            disabled={apply.isPending || watched === 0}
            onClick={() => apply.mutate(false)}
          >
            Stop watching
          </button>
        </div>
      </div>
      <p className="mt-1 text-xs text-slate-500">
        Applies to the feeds in this group now — checked every 15 minutes instead of every 6
        hours, notifying you and relaying to Telegram when they publish. A feed added later has
        to be included again.
      </p>
      {watched === 0 && (
        <p className="mt-2 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-300">
          Polling more often than hourly stops the database idling, which costs roughly
          $150–200/month more than letting it pause.
        </p>
      )}
      {apply.error && <p className="mt-1 text-xs text-rose-400">{String(apply.error)}</p>}
    </div>
  )
}
