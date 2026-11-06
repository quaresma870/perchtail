// Auto-refresh of the folders open in the explorer (#272): only their
// listings, never file content. Every expanded source or folder registers
// how to re-list itself; one timer re-lists them all, one folder at a time
// per source (so a refresh never opens several connections to the same
// server at once), skipping a source that just failed for a while, and
// doing nothing while the browser tab is hidden.

export interface RefreshTarget {
  sourceId: number
  path: string
  /** Re-lists the folder; false when the source didn't answer. */
  refresh: () => Promise<boolean>
}

const targets = new Map<string, RefreshTarget>()

export const targetKey = (sourceId: number, path: string) => `${sourceId}:${path}`

export function registerFolder(target: RefreshTarget): () => void {
  const key = targetKey(target.sourceId, target.path)
  targets.set(key, target)
  return () => {
    if (targets.get(key) === target) targets.delete(key)
  }
}

/** Re-lists one open folder now, if it's open (e.g. after a file in it
 * turned out to be gone). */
export async function refreshFolder(sourceId: number, path: string): Promise<void> {
  await targets.get(targetKey(sourceId, path))?.refresh()
}

/** The parent folder of a file path ("" for the source's root). */
export const parentPath = (path: string) => (path.includes('/') ? path.slice(0, path.lastIndexOf('/')) : '')

// Per source: consecutive failures, and until when it's left alone.
const backoff = new Map<number, { failures: number; until: number }>()
const MAX_BACKOFF_STEPS = 4

/** One round: every registered folder, sources in parallel, each source's
 * folders one after another. `now` and `intervalMs` drive the backoff. */
export async function refreshAll(intervalMs: number, now = Date.now()): Promise<void> {
  const bySource = new Map<number, RefreshTarget[]>()
  for (const target of targets.values()) {
    const list = bySource.get(target.sourceId) ?? []
    list.push(target)
    bySource.set(target.sourceId, list)
  }
  await Promise.all(
    [...bySource.entries()].map(async ([sourceId, list]) => {
      const state = backoff.get(sourceId)
      if (state && state.until > now) return
      for (const target of list) {
        const ok = await target.refresh().catch(() => false)
        if (!ok) {
          const failures = (state?.failures ?? 0) + 1
          const wait = intervalMs * 2 ** Math.min(failures, MAX_BACKOFF_STEPS)
          backoff.set(sourceId, { failures, until: now + wait })
          return
        }
      }
      backoff.delete(sourceId)
    }),
  )
}

let timer: ReturnType<typeof setInterval> | null = null
let timerSeconds = 0
let running = false

/** Starts (or restarts, with a new interval) the auto-refresh; 0 stops it.
 * The same interval again leaves a running timer alone, so re-reading the
 * System settings doesn't keep pushing the next round back. */
export function startAutoRefresh(seconds: number): void {
  if (timer && seconds === timerSeconds) return
  stopAutoRefresh()
  timerSeconds = seconds
  if (!seconds || seconds <= 0) return
  const intervalMs = seconds * 1000
  timer = setInterval(async () => {
    if (running || document.hidden) return
    running = true
    try {
      await refreshAll(intervalMs)
    } finally {
      running = false
    }
  }, intervalMs)
}

export function stopAutoRefresh(): void {
  if (timer) clearInterval(timer)
  timer = null
  timerSeconds = 0
}

/** For tests. */
export function resetRefreshState(): void {
  targets.clear()
  backoff.clear()
  stopAutoRefresh()
}
