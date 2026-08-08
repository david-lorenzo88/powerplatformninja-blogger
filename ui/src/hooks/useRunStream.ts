import { useEffect, useRef, useState } from 'react'
import { getHealth, runEventsUrl } from '../api/client'
import type { RunEvent, RunStatus } from '../api/types'

export interface RunStream {
  events: RunEvent[]
  streamStatus: 'connecting' | 'open' | 'closed'
  // Set from the synthetic `eof` frame that ends the stream.
  terminalStatus: RunStatus | null
}

// Subscribe to GET /api/runs/{id}/events. The server replays from `?after=<seq>`
// then follows live, so a browser arriving at a finished run animates exactly
// like one that watched it happen. Sequence numbers are strictly increasing per
// run: we guard on `seq` (never dedupe by content) and reconnect with the last
// seq we saw, so a dropped connection resumes with only what we missed. The
// `: keep-alive` comments the server sends every 15s are swallowed by
// EventSource and need no handling here.
export function useRunStream(runId: string | undefined): RunStream {
  const [events, setEvents] = useState<RunEvent[]>([])
  const [streamStatus, setStreamStatus] = useState<'connecting' | 'open' | 'closed'>('connecting')
  const [terminalStatus, setTerminalStatus] = useState<RunStatus | null>(null)

  const lastSeqRef = useRef(0)
  const doneRef = useRef(false)

  useEffect(() => {
    if (!runId) return

    setEvents([])
    setTerminalStatus(null)
    setStreamStatus('connecting')
    lastSeqRef.current = 0
    doneRef.current = false

    let es: EventSource | null = null
    let reconnectTimer: number | undefined
    // Consecutive failures with no intervening onopen. Used both to back off
    // and to decide when a "server is down" is more likely a "session expired".
    let failures = 0

    // A finished run replays thousands of events in a burst, and a live write
    // run can be chatty. Buffer incoming events and flush on a timer so we
    // re-render a handful of times a second, not once per event.
    let buffer: RunEvent[] = []
    let flushTimer: number | undefined
    const flush = () => {
      flushTimer = undefined
      if (buffer.length === 0) return
      const batch = buffer
      buffer = []
      setEvents((prev) => prev.concat(batch))
    }
    const scheduleFlush = () => {
      if (flushTimer === undefined) flushTimer = window.setTimeout(flush, 120)
    }

    const connect = () => {
      if (doneRef.current) return
      es = new EventSource(runEventsUrl(runId, lastSeqRef.current))

      es.onopen = () => {
        failures = 0
        setStreamStatus('open')
      }

      es.onmessage = (e) => {
        let payload: RunEvent & { status?: RunStatus }
        try {
          payload = JSON.parse(e.data)
        } catch {
          return
        }
        if (payload.kind === 'eof') {
          doneRef.current = true
          flush()
          setTerminalStatus(payload.status ?? null)
          setStreamStatus('closed')
          es?.close()
          return
        }
        if (typeof payload.seq !== 'number' || payload.seq <= lastSeqRef.current) return
        lastSeqRef.current = payload.seq
        buffer.push(payload)
        scheduleFlush()
      }

      es.onerror = () => {
        es?.close()
        if (doneRef.current) {
          setStreamStatus('closed')
          return
        }
        failures += 1
        // EventSource cannot be given a redirect mode, so against Easy Auth an
        // expired session looks exactly like a dead server — and the old fixed
        // 1.5s retry then hammered the login redirect forever, silently. After a
        // few failures ask /api/health, which goes through request() and will
        // navigate to the login page if that is what is actually wrong.
        if (failures === 3) void getHealth().catch(() => {})
        // Resume from the last seq we saw, backing off to 30s.
        setStreamStatus('connecting')
        reconnectTimer = window.setTimeout(connect, Math.min(1500 * 2 ** (failures - 1), 30_000))
      }
    }

    // Mobile browsers tear down an EventSource when the tab or installed app
    // goes to the background, and do not reliably fire onerror when they do — so
    // coming back could sit in `connecting` for a whole backoff, or forever.
    // Retry at once on becoming visible instead; lastSeqRef makes it lossless.
    const onVisible = () => {
      if (document.visibilityState !== 'visible' || doneRef.current) return
      if (es && es.readyState === EventSource.OPEN) return
      if (reconnectTimer) window.clearTimeout(reconnectTimer)
      es?.close()
      failures = 0
      setStreamStatus('connecting')
      connect()
    }
    document.addEventListener('visibilitychange', onVisible)

    connect()

    return () => {
      doneRef.current = true
      document.removeEventListener('visibilitychange', onVisible)
      es?.close()
      if (reconnectTimer) window.clearTimeout(reconnectTimer)
      if (flushTimer) window.clearTimeout(flushTimer)
    }
  }, [runId])

  return { events, streamStatus, terminalStatus }
}
