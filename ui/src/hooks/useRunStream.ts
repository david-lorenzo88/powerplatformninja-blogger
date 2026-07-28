import { useEffect, useRef, useState } from 'react'
import { runEventsUrl } from '../api/client'
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

      es.onopen = () => setStreamStatus('open')

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
        // Resume from the last seq we saw after a short backoff.
        setStreamStatus('connecting')
        reconnectTimer = window.setTimeout(connect, 1500)
      }
    }

    connect()

    return () => {
      doneRef.current = true
      es?.close()
      if (reconnectTimer) window.clearTimeout(reconnectTimer)
      if (flushTimer) window.clearTimeout(flushTimer)
    }
  }, [runId])

  return { events, streamStatus, terminalStatus }
}
