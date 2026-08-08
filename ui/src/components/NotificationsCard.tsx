import { useEffect, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { getHealth, sendTestPush } from '../api/client'
import { disablePush, enablePush, isStandalone, pushState, syncSubscription } from '../lib/push'
import type { PushState } from '../lib/push'
import { card, ghostBtn, primaryBtn } from '../lib/ui'

// Why this is a card on /config rather than a toggle in a menu: turning it on is
// a one-time decision with three separate ways to fail, each needing its own
// explanation. A toggle that silently does nothing on an un-installed iPhone is
// worse than no toggle.
export function NotificationsCard() {
  const health = useQuery({ queryKey: ['health'], queryFn: getHealth })
  const [state, setState] = useState<PushState | null>(null)
  const [error, setError] = useState('')
  const [testResult, setTestResult] = useState('')
  // What the *server* thinks, which is the half that actually decides whether a
  // notification can be delivered. Kept separate from `state`, which is only
  // what this browser believes.
  const [registered, setRegistered] = useState<number | null>(null)

  useEffect(() => {
    void (async () => {
      const s = await pushState()
      setState(s)
      if (s !== 'on') return
      // This browser holds a subscription; make sure the server has it too.
      // Silent on failure: it is a background repair, and the visible
      // consequence (0 devices registered) is already reported below.
      try {
        setRegistered(await syncSubscription())
      } catch {
        setRegistered(0)
      }
    })()
  }, [])

  const toggle = useMutation({
    mutationFn: async () => {
      setError('')
      setTestResult('')
      const key = health.data?.push.public_key ?? ''
      return state === 'on' ? disablePush() : enablePush(key)
    },
    onSuccess: async (next) => {
      setState(next)
      setRegistered(next === 'on' ? await syncSubscription().catch(() => 0) : null)
    },
    onError: (e) => setError(e instanceof Error ? e.message : String(e)),
  })

  const test = useMutation({
    mutationFn: sendTestPush,
    onSuccess: (r) => {
      setRegistered(r.subscriptions)
      setTestResult(
        r.delivered
          ? `Sent to ${r.delivered} device${r.delivered === 1 ? '' : 's'}.`
          : r.subscriptions === 0
            ? 'The server has no devices registered. Turn it off and on again to re-register this one.'
            : `${r.subscriptions} device(s) registered but none accepted the push — they may have expired.`,
      )
    },
    onError: (e) => setTestResult(e instanceof Error ? e.message : String(e)),
  })

  const configured = health.data?.push.configured ?? false

  return (
    <div className={`${card} p-4`}>
      <h2 className="text-sm font-semibold text-slate-100">Notifications</h2>
      <p className="mt-1 text-xs text-slate-400">
        Get told when a run finishes or a sweep needs your verdict, even with the app closed.
      </p>

      {!configured ? (
        <p className="mt-3 text-xs text-amber-300">
          The server has no VAPID keys. Set <span className="font-mono">PPN_VAPID_PUBLIC_KEY</span>,{' '}
          <span className="font-mono">PPN_VAPID_PRIVATE_KEY</span> and{' '}
          <span className="font-mono">PPN_VAPID_SUBJECT</span> — see docs/DEPLOYMENT.md.
        </p>
      ) : state === 'needs-install' ? (
        <p className="mt-3 text-xs text-amber-300">
          On iPhone and iPad, notifications only work once this is added to the Home Screen. Tap
          Share, then <strong>Add to Home Screen</strong>, and open it from there.
        </p>
      ) : state === 'unsupported' ? (
        <p className="mt-3 text-xs text-slate-500">This browser has no Push API.</p>
      ) : state === 'denied' ? (
        <p className="mt-3 text-xs text-rose-300">
          Notifications are blocked for this site. Only your browser or OS settings can undo that —
          the page cannot ask again.
        </p>
      ) : (
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <button
            onClick={() => toggle.mutate()}
            disabled={toggle.isPending || state == null}
            className={state === 'on' ? ghostBtn : primaryBtn}
          >
            {toggle.isPending
              ? 'Working…'
              : state === 'on'
                ? 'Turn off on this device'
                : 'Turn on for this device'}
          </button>
          {state === 'on' && (
            <button onClick={() => test.mutate()} disabled={test.isPending} className={ghostBtn}>
              {test.isPending ? 'Sending…' : 'Send a test'}
            </button>
          )}
        </div>
      )}

      {state === 'on' && (
        <p className="mt-2 text-xs text-slate-500">
          {registered == null
            ? 'Checking with the server…'
            : registered > 0
              ? `Registered on the server · ${registered} device${registered === 1 ? '' : 's'}.`
              : 'This browser is subscribed, but the server has no record of it.'}
        </p>
      )}
      {state === 'on' && !isStandalone() && (
        <p className="mt-2 text-xs text-slate-500">
          Installed to the Home Screen, these arrive like any other app's.
        </p>
      )}
      {error && <p className="mt-2 text-xs text-rose-300">{error}</p>}
      {testResult && <p className="mt-2 text-xs text-slate-400">{testResult}</p>}
    </div>
  )
}
