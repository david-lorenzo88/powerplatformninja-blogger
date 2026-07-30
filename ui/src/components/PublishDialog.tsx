import { useMutation, useQueryClient } from '@tanstack/react-query'
import { publishDraft } from '../api/client'
import { Modal } from './Modal'

// Publishes a single draft *file* (a version's markdown) to WordPress. Kept as a
// shared component so both the post-detail screen and any future caller push the
// same way, behind the same confirm step.
export function PublishDialog({
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
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['drafts'] })
      qc.invalidateQueries({ queryKey: ['posts'] })
    },
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
            <button
              onClick={onClose}
              className="rounded-lg px-4 py-2 text-sm text-slate-400 hover:text-slate-200"
            >
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
            <button
              onClick={onClose}
              className="rounded-lg px-4 py-2 text-sm text-slate-400 hover:text-slate-200"
            >
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
