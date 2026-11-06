// Incremental follow (#254): the viewer asks only for what was appended
// since the bytes it holds (GET /sources/{id}/follow), and appends it.

export const FOLLOW_INTERVALS_MS = [1000, 2000, 5000, 10000, 30000] as const
export const DEFAULT_FOLLOW_INTERVAL_MS = 2000
const STORAGE_KEY = 'perchtail.followIntervalMs'

/** The interval the viewer last used, if it's one of the offered ones. A
 * per-browser convenience: storage can be missing or blocked. */
export function loadFollowInterval(): number {
  try {
    const stored = Number(localStorage.getItem(STORAGE_KEY))
    if ((FOLLOW_INTERVALS_MS as readonly number[]).includes(stored)) return stored
  } catch {
    // private window or blocked storage: the default is fine
  }
  return DEFAULT_FOLLOW_INTERVAL_MS
}

export function saveFollowInterval(ms: number): void {
  try {
    localStorage.setItem(STORAGE_KEY, String(ms))
  } catch {
    // not remembered, that's all
  }
}

export function followUrl(
  sourceId: number,
  path: string,
  offset: number,
  head: string | null,
  start: boolean,
): string {
  const params = new URLSearchParams({ path, offset: String(offset) })
  if (head) params.set('head', head)
  if (start) params.set('start', 'true')
  return `/sources/${sourceId}/follow?${params}`
}

export interface FollowAnswer {
  // The file shrank or starts differently (rotated or truncated): reload.
  reset: boolean
  offset: number
  head: string | null
  // More is already waiting beyond this answer: ask again right away.
  more: boolean
}

export function readFollowHeaders(headers: Headers, previousOffset: number): FollowAnswer {
  const offset = Number(headers.get('x-follow-offset'))
  return {
    reset: headers.get('x-follow-reset') === '1',
    offset: Number.isFinite(offset) && headers.has('x-follow-offset') ? offset : previousOffset,
    head: headers.get('x-follow-head'),
    more: headers.get('x-follow-more') === '1',
  }
}

/** How many lines `text` adds when appended (a final line without a
 * newline still counts). */
export function countLines(text: string): number {
  if (!text) return 0
  let lines = 0
  for (let i = 0; i < text.length; i++) if (text.charCodeAt(i) === 10) lines++
  return text.endsWith('\n') ? lines : lines + 1
}

export function formatInterval(ms: number): string {
  return `${ms / 1000} s`
}
