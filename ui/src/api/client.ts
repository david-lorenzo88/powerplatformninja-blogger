// Typed wrappers over the FastAPI contract. Every request goes through `request`
// so error handling is uniform: a non-2xx becomes an `ApiError` carrying the
// server's message. FastAPI puts validation/parse errors (e.g. the YAML 422)
// in `detail`, which is exactly what the Config editor surfaces inline.

import type {
  ConfigContent,
  ConfigHistoryItem,
  ConfigListItem,
  ConfigVersion,
  Draft,
  DraftListItem,
  Health,
  Run,
  RunDetail,
  RunEvent,
  RunStatus,
  SuggestRequest,
  WorkflowGraph,
  WriteRequest,
} from './types'

export const API_BASE = '/api'

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: init?.body ? { 'Content-Type': 'application/json' } : undefined,
    ...init,
  })
  if (!res.ok) {
    throw new ApiError(res.status, await errorMessage(res))
  }
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

async function errorMessage(res: Response): Promise<string> {
  try {
    const body = await res.json()
    const detail = (body as { detail?: unknown }).detail
    if (typeof detail === 'string') return detail
    if (detail != null) return JSON.stringify(detail)
    return `${res.status} ${res.statusText}`
  } catch {
    return `${res.status} ${res.statusText}`
  }
}

// -- Health -----------------------------------------------------------------

export const getHealth = () => request<Health>('/health')

// -- Runs -------------------------------------------------------------------

export const listRuns = (params?: { status?: RunStatus; limit?: number }) => {
  const q = new URLSearchParams()
  if (params?.status) q.set('status', params.status)
  if (params?.limit) q.set('limit', String(params.limit))
  const qs = q.toString()
  return request<Run[]>(`/runs${qs ? `?${qs}` : ''}`)
}

export const getRun = (id: string) => request<RunDetail>(`/runs/${id}`)

export const startSuggest = (body: SuggestRequest) =>
  request<{ id: string }>('/runs/suggest', { method: 'POST', body: JSON.stringify(body) })

export const startWrite = (body: WriteRequest) =>
  request<{ id: string }>('/runs/write', { method: 'POST', body: JSON.stringify(body) })

export const cancelRun = (id: string) =>
  request<{ cancelled: boolean }>(`/runs/${id}/cancel`, { method: 'POST' })

// SSE URL for EventSource. Replays from `after`, then follows live.
export const runEventsUrl = (id: string, after = 0) =>
  `${API_BASE}/runs/${id}/events?after=${after}`

// -- Workflows (the canvas topology) ---------------------------------------

export const getWorkflows = () => request<WorkflowGraph[]>('/workflows')

// -- Config -----------------------------------------------------------------

export const listConfig = () => request<ConfigListItem[]>('/config')

export const getConfig = (name: string) => request<ConfigContent>(`/config/${name}`)

export const putConfig = (name: string, content: string, note = '') =>
  request<{ name: string; version: number; updated_at: string }>(`/config/${name}`, {
    method: 'PUT',
    body: JSON.stringify({ content, note }),
  })

export const configHistory = (name: string) =>
  request<ConfigHistoryItem[]>(`/config/${name}/history`)

export const configVersion = (name: string, version: number) =>
  request<ConfigVersion>(`/config/${name}/versions/${version}`)

export const configRollback = (name: string, version: number) =>
  request<{ name: string; version: number }>(`/config/${name}/rollback/${version}`, {
    method: 'POST',
  })

// -- Drafts -----------------------------------------------------------------

export const listDrafts = () => request<DraftListItem[]>('/drafts')

export const getDraft = (name: string) => request<Draft>(`/drafts/${name}`)

export const putDraft = (name: string, markdown: string) =>
  request<{ saved: boolean }>(`/drafts/${name}`, {
    method: 'PUT',
    body: JSON.stringify({ markdown }),
  })

export const draftCoverUrl = (name: string) => `${API_BASE}/drafts/${name}/cover`

export const publishDraft = (name: string, status = 'draft') =>
  request<Record<string, unknown>>(`/drafts/${name}/publish?status=${status}`, {
    method: 'POST',
  })

export type { RunEvent }
