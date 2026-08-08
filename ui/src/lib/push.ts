// Turning notifications on for this browser.
//
// Three things have to line up before a notification can arrive: the server has
// VAPID keys, this browser has been granted permission, and a service worker is
// registered to receive the message. Each fails differently and each needs
// saying differently, which is why this returns a reason rather than a boolean.

import { subscribePush, unsubscribePush } from '../api/client'

export type PushState =
  | 'unsupported' // no Push API at all — desktop Safari, or an old browser
  | 'needs-install' // iOS: Web Push only works once added to the Home Screen
  | 'denied' // permission refused; only the OS settings can undo this
  | 'off'
  | 'on'

// iOS gained Web Push in 16.4, but only for an installed PWA — in Safari itself
// the API is absent, so `unsupported` there is the honest answer rather than a
// button that silently does nothing.
export function isStandalone(): boolean {
  return (
    window.matchMedia('(display-mode: standalone)').matches ||
    // Safari's own, non-standard flag.
    (navigator as unknown as { standalone?: boolean }).standalone === true
  )
}

function isIOS(): boolean {
  return (
    /iPad|iPhone|iPod/.test(navigator.userAgent) ||
    // iPadOS reports as a Mac; the touch points give it away.
    (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1)
  )
}

export async function pushState(): Promise<PushState> {
  if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
    return isIOS() && !isStandalone() ? 'needs-install' : 'unsupported'
  }
  if (Notification.permission === 'denied') return 'denied'
  const registration = await navigator.serviceWorker.getRegistration()
  const existing = await registration?.pushManager.getSubscription()
  return existing ? 'on' : 'off'
}

/**
 * Re-register the browser's existing subscription with the server.
 *
 * The browser and the server hold this state separately and they can diverge:
 * `pushManager.subscribe()` succeeds locally whether or not the POST that
 * follows it does, so a failed or interrupted registration leaves the browser
 * convinced it is subscribed while the server has no row — and nothing in the
 * old flow could ever recover from that. The symptom is a card reading "on"
 * next to a test that reports no devices registered, forever.
 *
 * Safe to call on every mount: the endpoint upserts on the endpoint's unique
 * index, so this is idempotent. Returns how many devices the server has after
 * reconciling, or null when there is nothing local to sync.
 */
export async function syncSubscription(): Promise<number | null> {
  if (!('serviceWorker' in navigator) || !('PushManager' in window)) return null
  const registration = await navigator.serviceWorker.getRegistration()
  const existing = await registration?.pushManager.getSubscription()
  if (!existing) return null
  const json = existing.toJSON() as { endpoint?: string; keys?: Record<string, string> }
  if (!json.endpoint || !json.keys?.p256dh || !json.keys?.auth) return null
  const result = await subscribePush({
    endpoint: json.endpoint,
    keys: { p256dh: json.keys.p256dh, auth: json.keys.auth },
  })
  return result.subscriptions
}

// VAPID keys travel as base64url; PushManager wants raw bytes. Backed by an
// explicit ArrayBuffer because Uint8Array<ArrayBufferLike> — which could be a
// SharedArrayBuffer — is not assignable to BufferSource.
function decodeKey(base64url: string): ArrayBuffer {
  const padded = (base64url + '='.repeat((4 - (base64url.length % 4)) % 4))
    .replace(/-/g, '+')
    .replace(/_/g, '/')
  const raw = atob(padded)
  const buffer = new ArrayBuffer(raw.length)
  const bytes = new Uint8Array(buffer)
  for (let i = 0; i < raw.length; i += 1) bytes[i] = raw.charCodeAt(i)
  return buffer
}

/**
 * Ask for permission and register with the server.
 * Must be called from a user gesture — Safari refuses the prompt otherwise.
 */
export async function enablePush(publicKey: string): Promise<PushState> {
  const state = await pushState()
  if (state === 'unsupported' || state === 'needs-install' || state === 'denied') return state

  const permission = await Notification.requestPermission()
  if (permission !== 'granted') return permission === 'denied' ? 'denied' : 'off'

  const registration = await navigator.serviceWorker.ready
  const subscription =
    (await registration.pushManager.getSubscription()) ??
    (await registration.pushManager.subscribe({
      // Required by Chrome, and honest: every push this app sends shows a
      // notification. A silent push would be refused.
      userVisibleOnly: true,
      applicationServerKey: decodeKey(publicKey),
    }))

  const json = subscription.toJSON() as { endpoint?: string; keys?: Record<string, string> }
  if (!json.endpoint || !json.keys?.p256dh || !json.keys?.auth) return 'off'
  await subscribePush({
    endpoint: json.endpoint,
    keys: { p256dh: json.keys.p256dh, auth: json.keys.auth },
  })
  return 'on'
}

export async function disablePush(): Promise<PushState> {
  const registration = await navigator.serviceWorker.getRegistration()
  const subscription = await registration?.pushManager.getSubscription()
  if (!subscription) return 'off'
  // Tell the server first: if the local unsubscribe succeeded and this did not,
  // the row would linger and be retried on every run with no way to remove it.
  await unsubscribePush({ endpoint: subscription.endpoint })
  await subscription.unsubscribe()
  return 'off'
}
