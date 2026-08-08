// The approval step in the middle of an exploration run.
//
// A wide-web sweep stops here: the scouts have read the open web, and nothing
// they found reaches the topic editor until these boxes are ticked. Approving a
// site also files it into sources.yaml, so the decision applies to every future
// topic run and to the Researcher and Source Checker on every future draft —
// which is why the tier dropdown matters and is spelled out on screen.

import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate, useParams } from 'react-router-dom'
import { cancelSourceReview, decideSourceReview, getSourceReview } from '../api/client'
import type { SourceCandidate, SourceReview, TrustTier } from '../api/types'
import { fieldSm as field, ghostBtn, primaryBtn } from '../lib/ui'

interface Choice {
  approved: boolean
  tier: string
}

export function SourceReviewScreen() {
  const { id } = useParams<{ id: string }>()
  const reviewId = Number(id)
  const navigate = useNavigate()
  const qc = useQueryClient()

  const review = useQuery({
    queryKey: ['source-review', reviewId],
    queryFn: () => getSourceReview(reviewId),
    enabled: Number.isFinite(reviewId),
  })

  const [choices, setChoices] = useState<Record<string, Choice>>({})
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})

  // Seed from the server's suggestion: sites the config already trusts start
  // ticked, brand new ones start unticked. Nothing is approved by default that
  // was not already trusted — the point of the screen is the explicit yes.
  useEffect(() => {
    if (!review.data) return
    setChoices((current) =>
      Object.keys(current).length
        ? current
        : Object.fromEntries(
            review.data.candidates.map((c) => [
              c.domain,
              { approved: c.known, tier: c.suggested_tier },
            ]),
          ),
    )
  }, [review.data])

  const decide = useMutation({
    mutationFn: (startShortlist: boolean) =>
      decideSourceReview(reviewId, {
        decisions: (review.data?.candidates ?? []).map((c) => ({
          domain: c.domain,
          approved: choices[c.domain]?.approved ?? false,
          tier: choices[c.domain]?.tier ?? c.suggested_tier,
        })),
        start_shortlist: startShortlist,
      }),
    onSuccess: (result) => {
      qc.invalidateQueries({ queryKey: ['source-reviews'] })
      qc.invalidateQueries({ queryKey: ['source-review', reviewId] })
      qc.invalidateQueries({ queryKey: ['runs'] })
      qc.invalidateQueries({ queryKey: ['config'] })
      navigate(result.run_id ? `/runs/${result.run_id}` : '/source-reviews')
    },
  })

  const drop = useMutation({
    mutationFn: () => cancelSourceReview(reviewId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['source-reviews'] })
      navigate('/source-reviews')
    },
  })

  const data = review.data
  const approvedCount = useMemo(
    () => Object.values(choices).filter((c) => c.approved).length,
    [choices],
  )
  const newSites = (data?.candidates ?? []).filter((c) => !c.known)
  const knownSites = (data?.candidates ?? []).filter((c) => c.known)
  const settled = data != null && data.status !== 'pending'

  if (review.isLoading) {
    return <p className="p-6 text-sm text-slate-500">Loading…</p>
  }
  if (review.isError || !data) {
    return <p className="p-6 text-sm text-rose-400">{String(review.error ?? 'Not found')}</p>
  }

  const setAll = (domains: string[], approved: boolean) =>
    setChoices((current) => {
      const next = { ...current }
      for (const domain of domains) {
        next[domain] = { ...next[domain], approved }
      }
      return next
    })

  return (
    <div className="flex h-full flex-col">
      <div className="shrink-0 border-b border-slate-800 px-4 py-4 lg:px-6">
        <div className="flex flex-wrap items-baseline gap-3">
          <h1 className="text-lg font-semibold text-slate-100">Source review #{data.id}</h1>
          <StatusChip status={data.status} />
          <span className="text-sm text-slate-500">
            {data.candidate_count} site{data.candidate_count === 1 ? '' : 's'} · {data.new_count} new
            · {data.signal_count} signals
          </span>
        </div>
        {data.instruction && (
          <p className="mt-1 text-sm text-slate-400">“{data.instruction}”</p>
        )}
        <p className="mt-2 max-w-3xl text-xs text-slate-500">
          Only approved sites reach the topic editor. Approving one also adds it to{' '}
          <span className="font-mono text-slate-400">sources.yaml</span> at the tier you choose, so
          it is trusted by future topic runs and by the Source Checker on every future draft.
          Turning down a site the config has never seen means it is never proposed again.
        </p>
      </div>

      <div className="min-h-0 flex-1 overflow-auto px-4 py-4 lg:px-6">
        {settled ? (
          <DecidedSummary review={data} />
        ) : (
          <>
            <Group
              title="New sites"
              subtitle="Never used before. Tick to trust; leave unticked to never be asked again."
              candidates={newSites}
              choices={choices}
              tiers={data.tiers}
              expanded={expanded}
              onToggleExpand={(d) => setExpanded((e) => ({ ...e, [d]: !e[d] }))}
              onChange={(domain, choice) =>
                setChoices((current) => ({ ...current, [domain]: choice }))
              }
              onAll={(approved) => setAll(newSites.map((c) => c.domain), approved)}
            />
            <Group
              title="Already trusted"
              subtitle="Already in sources.yaml. Unticking one skips it for this shortlist only."
              candidates={knownSites}
              choices={choices}
              tiers={data.tiers}
              expanded={expanded}
              onToggleExpand={(d) => setExpanded((e) => ({ ...e, [d]: !e[d] }))}
              onChange={(domain, choice) =>
                setChoices((current) => ({ ...current, [domain]: choice }))
              }
              onAll={(approved) => setAll(knownSites.map((c) => c.domain), approved)}
            />
          </>
        )}
      </div>

      {!settled && (
        <div className="shrink-0 border-t border-slate-800 px-4 py-3 pb-safe lg:px-6">
          {decide.error && (
            <p className="mb-2 text-xs text-rose-400">{String(decide.error)}</p>
          )}
          <div className="flex flex-col gap-2 lg:flex-row lg:flex-wrap lg:items-center lg:gap-3">
            <button
              className={primaryBtn}
              disabled={approvedCount === 0 || decide.isPending}
              onClick={() => decide.mutate(true)}
            >
              {decide.isPending
                ? 'Applying…'
                : `Approve ${approvedCount} site${approvedCount === 1 ? '' : 's'} & build shortlist`}
            </button>
            <button
              className={ghostBtn}
              disabled={approvedCount === 0 || decide.isPending}
              onClick={() => decide.mutate(false)}
              title="File the decisions into sources.yaml without generating topics now"
            >
              Save sources only
            </button>
            <button
              className={`${ghostBtn} lg:ml-auto`}
              disabled={drop.isPending}
              onClick={() => drop.mutate()}
            >
              Discard review
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

function Group({
  title,
  subtitle,
  candidates,
  choices,
  tiers,
  expanded,
  onToggleExpand,
  onChange,
  onAll,
}: {
  title: string
  subtitle: string
  candidates: SourceCandidate[]
  choices: Record<string, Choice>
  tiers: TrustTier[]
  expanded: Record<string, boolean>
  onToggleExpand: (domain: string) => void
  onChange: (domain: string, choice: Choice) => void
  onAll: (approved: boolean) => void
}) {
  if (candidates.length === 0) return null
  return (
    <section className="mb-6">
      <div className="mb-2 flex flex-wrap items-baseline gap-3">
        <h2 className="text-sm font-semibold text-slate-200">
          {title} <span className="text-slate-500">({candidates.length})</span>
        </h2>
        <p className="text-xs text-slate-500">{subtitle}</p>
        <div className="ml-auto flex gap-2 text-xs">
          <button className="text-accent hover:underline" onClick={() => onAll(true)}>
            all
          </button>
          <span className="text-slate-700">|</span>
          <button className="text-slate-400 hover:underline" onClick={() => onAll(false)}>
            none
          </button>
        </div>
      </div>
      <ul className="space-y-2">
        {candidates.map((candidate) => (
          <CandidateRow
            key={candidate.domain}
            candidate={candidate}
            choice={
              choices[candidate.domain] ?? {
                approved: candidate.known,
                tier: candidate.suggested_tier,
              }
            }
            tiers={tiers}
            expanded={!!expanded[candidate.domain]}
            onToggleExpand={() => onToggleExpand(candidate.domain)}
            onChange={(choice) => onChange(candidate.domain, choice)}
          />
        ))}
      </ul>
    </section>
  )
}

function CandidateRow({
  candidate,
  choice,
  tiers,
  expanded,
  onToggleExpand,
  onChange,
}: {
  candidate: SourceCandidate
  choice: Choice
  tiers: TrustTier[]
  expanded: boolean
  onToggleExpand: () => void
  onChange: (choice: Choice) => void
}) {
  return (
    <li
      className={`rounded-lg border px-3 py-2 transition-colors ${
        choice.approved ? 'border-accent/40 bg-accent/5' : 'border-slate-800 bg-slate-950/40'
      }`}
    >
      <div className="flex flex-wrap items-center gap-3">
        <input
          type="checkbox"
          className="h-6 w-6 shrink-0 accent-cyan-400 lg:h-4 lg:w-4"
          checked={choice.approved}
          onChange={(e) => onChange({ ...choice, approved: e.target.checked })}
          aria-label={`Approve ${candidate.domain}`}
        />
        <div className="min-w-0">
          <div className="truncate font-mono text-sm text-slate-200">{candidate.domain}</div>
          {candidate.name && candidate.name !== candidate.domain && (
            <div className="truncate text-xs text-slate-500">{candidate.name}</div>
          )}
        </div>
        {!candidate.known && (
          <span className="rounded bg-amber-500/15 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-amber-300">
            new
          </span>
        )}
        <button
          className="min-h-11 text-xs text-slate-400 hover:text-accent active:text-accent lg:min-h-0"
          onClick={onToggleExpand}
          aria-expanded={expanded}
        >
          {candidate.item_count} item{candidate.item_count === 1 ? '' : 's'} {expanded ? '▴' : '▾'}
        </button>
        <span className="text-[11px] text-slate-600">{candidate.scouts.join(', ')}</span>
        <label className="flex w-full items-center gap-2 text-xs text-slate-500 lg:ml-auto lg:w-auto">
          trust
          <select
            className={field}
            value={choice.tier}
            disabled={!choice.approved}
            onChange={(e) => onChange({ ...choice, tier: e.target.value })}
          >
            {tiers.map((tier) => (
              <option key={tier.id} value={tier.id}>
                {tier.id} ({tier.score})
              </option>
            ))}
          </select>
        </label>
      </div>

      {expanded && (
        <ul className="mt-2 space-y-2 border-t border-slate-800 pt-2">
          {candidate.items.map((item) => (
            <li key={item.url} className="text-xs">
              <a
                href={item.url}
                target="_blank"
                rel="noreferrer"
                className="text-accent hover:underline"
              >
                {item.title || item.url} ↗
              </a>
              <p className="text-slate-500">{item.why_it_matters}</p>
            </li>
          ))}
        </ul>
      )}
    </li>
  )
}

function DecidedSummary({ review }: { review: SourceReview }) {
  const approved = review.decisions.filter((d) => d.approved)
  const declined = review.decisions.filter((d) => !d.approved)
  return (
    <div className="space-y-4 text-sm">
      <div>
        <h2 className="mb-1 text-sm font-semibold text-slate-200">Approved ({approved.length})</h2>
        <ul className="space-y-1">
          {approved.map((d) => (
            <li key={d.domain} className="font-mono text-xs text-slate-300">
              {d.domain} <span className="text-slate-500">· {d.tier}</span>
            </li>
          ))}
        </ul>
      </div>
      <div>
        <h2 className="mb-1 text-sm font-semibold text-slate-200">Turned down ({declined.length})</h2>
        <ul className="space-y-1">
          {declined.map((d) => (
            <li key={d.domain} className="font-mono text-xs text-slate-500">
              {d.domain}
            </li>
          ))}
        </ul>
      </div>
      {review.config_version != null && (
        <p className="text-xs text-slate-500">
          Filed into <span className="font-mono">sources.yaml</span> as version{' '}
          {review.config_version}.
        </p>
      )}
    </div>
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
