// Whether PerchTail itself answers (#276). Fed by the requests the app makes
// anyway; when they keep failing, a light /healthz ping with backoff takes
// over until the server answers, and App.svelte shows a banner meanwhile.
import { writable } from 'svelte/store'

export const serverReachable = writable(true)

// Two failures in a row before the banner: one dropped request isn't an
// outage.
const FAILURES_BEFORE_BANNER = 2
const PING_MIN_MS = 2000
const PING_MAX_MS = 30000

let failures = 0
let pingTimer: ReturnType<typeof setTimeout> | null = null
let pingDelay = PING_MIN_MS

/** A response came back from the server (any status but a gateway error). */
export function noteAnswered(): void {
  failures = 0
  pingDelay = PING_MIN_MS
  if (pingTimer) {
    clearTimeout(pingTimer)
    pingTimer = null
  }
  serverReachable.set(true)
}

/** A request got no answer from PerchTail (network error, or a proxy in
 * front of it saying the server is gone). */
export function noteUnanswered(): void {
  failures += 1
  if (failures >= FAILURES_BEFORE_BANNER) {
    serverReachable.set(false)
    schedulePing()
  }
}

/** Whether a response means "PerchTail didn't answer": what a reverse
 * proxy says when the app behind it is down. */
export function isGatewayFailure(status: number): boolean {
  return status === 502 || status === 503 || status === 504
}

function schedulePing(): void {
  if (pingTimer) return
  pingTimer = setTimeout(async () => {
    pingTimer = null
    try {
      const response = await fetch('/healthz', { cache: 'no-store' })
      if (response.ok) {
        noteAnswered()
        return
      }
    } catch {
      // still down
    }
    pingDelay = Math.min(pingDelay * 2, PING_MAX_MS)
    schedulePing()
  }, pingDelay)
}

/** fetch(), reporting to serverReachable. */
export async function trackedFetch(input: string, init?: RequestInit): Promise<Response> {
  try {
    const response = await fetch(input, init)
    if (isGatewayFailure(response.status)) noteUnanswered()
    else noteAnswered()
    return response
  } catch (err) {
    noteUnanswered()
    throw err
  }
}

/** For tests. */
export function resetServerStatus(): void {
  failures = 0
  pingDelay = PING_MIN_MS
  if (pingTimer) clearTimeout(pingTimer)
  pingTimer = null
  serverReachable.set(true)
}
