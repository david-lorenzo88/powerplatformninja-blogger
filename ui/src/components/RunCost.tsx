import { useQuery } from '@tanstack/react-query'
import { getRunUsage } from '../api/client'
import type { AgentUsage } from '../api/types'
import { isTerminal, type Run } from '../api/types'
import { formatCost, formatTokens } from '../lib/format'

// What the run cost, and which agent spent it.
//
// The breakdown is the point. A run total tells you a post was expensive; the
// per-agent rows tell you the researcher was two thirds of it, which is the only
// version of the number you can act on.
//
// Every figure here is an estimate at list price. The footer says so on screen
// rather than only in a tooltip, because a number in a table reads as a fact.
export function RunCost({ run }: { run: Run }) {
  const finished = isTerminal(run.status)
  const usage = useQuery({
    queryKey: ['run-usage', run.id],
    queryFn: () => getRunUsage(run.id),
    // While a run is live the rows are still landing; once it is terminal the
    // answer is final and there is nothing to poll for.
    refetchInterval: finished ? false : 5000,
  })

  const total = usage.data?.total
  const agents = usage.data?.agents ?? []
  if (!total) {
    return finished ? (
      <p className="text-xs text-slate-500">This run called no model, so it cost nothing.</p>
    ) : (
      <p className="text-xs text-slate-500">Counting…</p>
    )
  }

  const currency = total.currency
  const peak = Math.max(...agents.map((a) => a.cost_micros), 1)

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
        <span className="text-2xl font-semibold text-slate-100">
          {total.priced ? formatCost(total.cost_micros, currency) : '—'}
        </span>
        <span className="text-xs text-slate-400">
          {formatTokens(total.total_tokens)} tokens · {total.records} call
          {total.records === 1 ? '' : 's'}
          {total.searches > 0 && ` · ${total.searches} searches`}
          {total.images > 0 && ` · ${total.images} image${total.images === 1 ? '' : 's'}`}
        </span>
      </div>

      {!total.priced && (
        <p className="rounded-lg bg-amber-500/10 px-3 py-2 text-xs text-amber-200">
          No price configured for {total.unpriced_models?.join(', ') || 'one of these models'}. The
          token counts are exact; the cost is unknown until a rate is set in{' '}
          <span className="font-mono">model_prices</span>.
        </p>
      )}

      <ul className="space-y-1.5">
        {agents.map((agent) => (
          <AgentRow key={`${agent.agent_id}-${agent.model}-${agent.kind}`} agent={agent} peak={peak} currency={currency} />
        ))}
      </ul>

      <p className="text-[11px] text-slate-500">
        An estimate at list price — no reservation or discount applied. Hosted web searches are
        counted exactly but priced from a hand-set rate.
      </p>
    </div>
  )
}

function AgentRow({
  agent,
  peak,
  currency,
}: {
  agent: AgentUsage
  peak: number
  currency: string
}) {
  // A bar rather than a chart: it answers "which one is big" at a glance, and
  // needs no library to do it.
  const width = Math.max(2, Math.round((agent.cost_micros / peak) * 100))
  return (
    <li>
      <div className="flex items-baseline justify-between gap-2 text-xs">
        <span className="truncate font-mono text-slate-300">
          {agent.agent_id}
          {agent.kind === 'image' && <span className="text-slate-500"> · image</span>}
        </span>
        <span className="shrink-0 tabular-nums text-slate-400">
          {agent.priced ? formatCost(agent.cost_micros, currency) : '—'}
          <span className="ml-2 text-slate-600">{formatTokens(agent.total_tokens)}</span>
        </span>
      </div>
      <div className="mt-1 h-1 overflow-hidden rounded-full bg-slate-800">
        <div className="h-full rounded-full bg-accent/60" style={{ width: `${width}%` }} />
      </div>
    </li>
  )
}
