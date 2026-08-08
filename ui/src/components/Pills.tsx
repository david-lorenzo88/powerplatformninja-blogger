// Two small badges that four screens share.
//
// They used to live inside TopicIdeasScreen and SourceReviewScreen, which was
// fine while everything shipped in one bundle. Now that routes are split, an
// import of ScorePill from /drafts would drag the whole topic-ideas screen into
// the drafts chunk — so they live here, where nothing else comes with them.

export function ScorePill({ score }: { score: number }) {
  const tone =
    score >= 80
      ? 'bg-emerald-500/15 text-emerald-300'
      : score >= 60
        ? 'bg-amber-500/15 text-amber-300'
        : 'bg-slate-700/40 text-slate-300'
  return (
    <span className={`rounded px-1.5 py-0.5 text-xs font-medium ${tone}`}>{Math.round(score)}</span>
  )
}

export function StatusChip({ status }: { status: string }) {
  const tone =
    status === 'pending'
      ? 'bg-amber-500/15 text-amber-300'
      : status === 'approved'
        ? 'bg-emerald-500/15 text-emerald-300'
        : 'bg-slate-700/40 text-slate-400'
  return <span className={`rounded px-2 py-0.5 text-xs font-medium ${tone}`}>{status}</span>
}
