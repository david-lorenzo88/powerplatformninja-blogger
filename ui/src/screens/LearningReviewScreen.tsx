import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import {
  cancelLearningReview,
  decideLearningReview,
  getLearningReview,
  listLearningReviews,
} from '../api/client'
import type { GateReport, LearningProposalView } from '../api/types'
import { StatusChip } from '../components/Pills'
import { BlogSubNav } from '../components/SubNav'
import { CodeEditor } from '../components/CodeEditor'
import { card, ghostBtn, primaryBtn, rowCard } from '../lib/ui'
import { useOnline } from '../hooks/useOnline'

// Approving what the crew should learn.
//
// The order on this screen is deliberate: what the change is, then what the test
// measured, then the examples, and only then the diff. The gate's numbers are the
// thing worth reading — "fires on four of your drafts and none of the twelve
// posts you published" is the evidence. The diff is only how it is spelled, and
// putting it first invites approving on the strength of it looking reasonable.

function Gate({ gate }: { gate: GateReport }) {
  const tone =
    gate.status === 'passed'
      ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-200'
      : gate.status === 'skipped'
        ? 'border-slate-700 bg-slate-800/40 text-slate-300'
        : 'border-rose-500/40 bg-rose-500/10 text-rose-200'
  return (
    <div className={`rounded-lg border p-3 text-sm ${tone}`}>
      <div className="font-medium">
        {gate.status === 'passed'
          ? 'Tested against everything you have published'
          : gate.status === 'skipped'
            ? 'Not mechanically testable'
            : 'Failed the test'}
      </div>
      <p className="mt-1">{gate.reason}</p>
      {gate.status !== 'skipped' && (
        <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 text-xs sm:grid-cols-4">
          <div>
            <dt className="text-slate-400">fires on drafts</dt>
            <dd>{gate.draft_hits}</dd>
          </div>
          <div>
            <dt className="text-slate-400">fires on published</dt>
            <dd>{gate.final_hits}</dd>
          </div>
          <div>
            <dt className="text-slate-400">posts tested</dt>
            <dd>{gate.finals}</dd>
          </div>
          <div>
            <dt className="text-slate-400">other rules changed</dt>
            <dd>{gate.regressions}</dd>
          </div>
        </dl>
      )}
    </div>
  )
}

function Proposal({
  proposal,
  approved,
  onToggle,
  settled,
}: {
  proposal: LearningProposalView
  approved: boolean
  onToggle: () => void
  settled: boolean
}) {
  const [showDiff, setShowDiff] = useState(false)
  const detail = proposal.proposal as Record<string, string>

  return (
    <div className={`${card} p-4`}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="font-medium text-slate-100">{proposal.summary}</h3>
          <p className="mt-1 text-xs text-slate-500">
            {proposal.document}
            {proposal.rule_id ? ` · ${proposal.rule_id}` : ''} · seen in{' '}
            {proposal.distinct_posts} separate posts
          </p>
        </div>
        {!settled && (
          <label className="flex shrink-0 items-center gap-2 text-sm text-slate-300">
            <input
              type="checkbox"
              checked={approved}
              onChange={onToggle}
              className="h-6 w-6 accent-emerald-500 lg:h-4 lg:w-4"
            />
            apply
          </label>
        )}
      </div>

      <div className="mt-3">
        <Gate gate={proposal.gate} />
      </div>

      {proposal.evidence_note && (
        <p className="mt-3 text-sm text-slate-400">{proposal.evidence_note}</p>
      )}

      {proposal.examples.length > 0 && (
        <div className="mt-3 space-y-2">
          <h4 className="text-xs font-medium uppercase tracking-wide text-slate-500">
            Edits you actually made
          </h4>
          {proposal.examples.map((example, i) => (
            <div key={i} className="rounded-lg border border-slate-800 p-2 text-sm">
              <div className="text-rose-200/80 line-through">{example.before}</div>
              <div className="text-emerald-200/80">{example.after || <em>(deleted)</em>}</div>
            </div>
          ))}
        </div>
      )}

      <div className="mt-3 rounded-lg border border-slate-800 bg-slate-950/40 p-3 text-sm">
        {proposal.kind === 'rule' && (
          <>
            <div className="text-slate-200">{detail.rule_text}</div>
            <div className="mt-1 text-xs text-slate-500">
              severity {detail.severity}
              {detail.detector ? ` · detector ${detail.detector}` : ' · judged by a model'}
            </div>
          </>
        )}
        {proposal.kind === 'style_note' && (
          <>
            <div className="text-xs text-slate-500">under {detail.anchor}</div>
            <pre className="mt-1 whitespace-pre-wrap font-sans text-slate-200">
              {detail.note_markdown}
            </pre>
          </>
        )}
        {proposal.kind === 'profile_scalar' && (
          <div className="text-slate-200">
            {detail.profile_key} → <strong>{detail.profile_value}</strong>
          </div>
        )}
        {proposal.kind === 'guidance' && (
          <div className="text-slate-200">
            <span className="text-xs text-slate-500">to the {detail.guidance_agent}: </span>
            {detail.guidance_text}
          </div>
        )}
      </div>

      <button
        type="button"
        onClick={() => setShowDiff((v) => !v)}
        className="mt-2 min-h-11 text-sm text-slate-400 hover:text-slate-200 lg:min-h-0"
      >
        {showDiff ? 'Hide' : 'Show'} the document as it would be saved
      </button>
      {showDiff && (
        <div className="mt-2">
          <CodeEditor
            value={proposal.content}
            format={proposal.document === 'style_guide' ? 'markdown' : 'yaml'}
            readOnly
            height="320px"
          />
        </div>
      )}
    </div>
  )
}

export function LearningReviewScreen() {
  const { id } = useParams()
  const reviewId = Number(id)
  const navigate = useNavigate()
  const qc = useQueryClient()
  const online = useOnline()
  const [chosen, setChosen] = useState<Record<string, boolean>>({})

  const review = useQuery({
    queryKey: ['learning-review', reviewId],
    queryFn: () => getLearningReview(reviewId),
    enabled: Number.isFinite(reviewId),
  })

  // Seeded once from the server, then owned locally, so a background refetch
  // cannot undo a tick the author has just made.
  useEffect(() => {
    if (!review.data) return
    setChosen((current) =>
      Object.keys(current).length
        ? current
        : Object.fromEntries(review.data.proposals.map((p) => [p.fingerprint, false])),
    )
  }, [review.data])

  const decide = useMutation({
    mutationFn: () =>
      decideLearningReview(reviewId, {
        decisions: Object.entries(chosen).map(([fingerprint, approved]) => ({
          fingerprint,
          approved,
        })),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['learning-reviews'] })
      qc.invalidateQueries({ queryKey: ['learning'] })
      qc.invalidateQueries({ queryKey: ['config'] })
      qc.invalidateQueries({ queryKey: ['pending'] })
      navigate('/learning')
    },
  })

  if (!review.data) {
    return <div className="p-4 text-sm text-slate-400">Loading…</div>
  }
  const r = review.data
  const settled = r.status !== 'pending'
  const approving = Object.values(chosen).filter(Boolean).length

  return (
    <div className="flex h-full flex-col">
      <header className="shrink-0 border-b border-slate-800 p-4">
        <Link to="/learning" className="text-sm text-slate-400 hover:text-slate-200">
          ← Learning
        </Link>
        <div className="mt-2 flex flex-wrap items-center gap-3">
          <h1 className="text-lg font-semibold text-slate-100">
            {r.proposal_count} proposed improvement{r.proposal_count === 1 ? '' : 's'}
          </h1>
          <StatusChip status={r.status} />
        </div>
        <p className="mt-1 max-w-2xl text-sm text-slate-400">
          Each of these comes from a correction you made by hand in at least three separate
          posts, and each was run against every draft the crew has written and every post you
          published. Nothing is applied until you tick it.
        </p>
      </header>

      <div className="min-h-0 flex-1 space-y-4 overflow-auto p-4">
        {settled && r.applied.length > 0 && (
          <div className="rounded-lg border border-emerald-500/40 bg-emerald-500/10 p-3 text-sm text-emerald-200">
            Applied {r.applied.length} change
            {r.applied.length === 1 ? '' : 's'}:{' '}
            {r.applied.map((a) => `${a.document} v${a.version}`).join(', ')}. Roll any of them
            back from the Config screen.
          </div>
        )}
        {r.proposals.map((proposal) => (
          <Proposal
            key={proposal.fingerprint}
            proposal={proposal}
            approved={Boolean(chosen[proposal.fingerprint])}
            settled={settled}
            onToggle={() =>
              setChosen((current) => ({
                ...current,
                [proposal.fingerprint]: !current[proposal.fingerprint],
              }))
            }
          />
        ))}
      </div>

      {!settled && (
        <div className="shrink-0 border-t border-slate-800 p-4 pb-safe">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <p className="text-sm text-slate-400">
              {approving === 0
                ? 'Nothing ticked — everything here will be declined and never offered again.'
                : `Applying ${approving} change${approving === 1 ? '' : 's'}, each as a new config version you can roll back.`}
            </p>
            <div className="flex gap-2">
              <button
                type="button"
                className={ghostBtn}
                onClick={() => {
                  void cancelLearningReview(reviewId).then(() => navigate('/learning'))
                }}
              >
                Decide later
              </button>
              <button
                type="button"
                className={primaryBtn}
                disabled={!online || decide.isPending}
                onClick={() => decide.mutate()}
              >
                {decide.isPending ? 'Applying…' : 'Save decisions'}
              </button>
            </div>
          </div>
          {decide.isError && (
            <p className="mt-2 text-sm text-rose-300">
              {(decide.error as Error).message || 'Could not save that.'}
            </p>
          )}
        </div>
      )}
    </div>
  )
}

export function LearningReviewsScreen() {
  const reviews = useQuery({
    queryKey: ['learning-reviews'],
    queryFn: () => listLearningReviews(),
    refetchInterval: 10_000,
  })

  return (
    <div className="flex h-full flex-col">
      <div className="shrink-0">
        <BlogSubNav />
      </div>
      <div className="min-h-0 flex-1 overflow-auto p-4">
        <h1 className="mb-3 text-xl font-semibold text-slate-100">Proposed improvements</h1>
        {!reviews.data?.length ? (
          <p className="text-sm text-slate-500">
            Nothing proposed yet. A correction has to recur across three separate posts, and
            survive being tested against everything you have published, before it reaches
            this screen.
          </p>
        ) : (
          <div className="space-y-2">
            {reviews.data.map((r) => (
              <Link key={r.id} to={`/learning-reviews/${r.id}`} className={rowCard}>
                <div className="flex flex-wrap items-baseline justify-between gap-2">
                  <span className="font-medium text-slate-100">
                    {r.proposal_count} improvement{r.proposal_count === 1 ? '' : 's'}
                  </span>
                  <StatusChip status={r.status} />
                </div>
                <div className="mt-1 text-xs text-slate-500">
                  {r.generated_on}
                  {r.status === 'approved' ? ` · applied ${r.applied_count}` : ''}
                </div>
              </Link>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
