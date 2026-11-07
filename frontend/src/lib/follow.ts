// Incremental follow (#254): the viewer asks only for what was appended
// since the bytes it holds (GET /sources/{id}/follow), and appends it.

// How often Tail polls is a System setting (#283), one of these.
export const TAIL_INTERVALS_SECONDS = [1, 2, 5, 10] as const
export const DEFAULT_TAIL_INTERVAL_SECONDS = 2

/** The poll interval in ms for a System setting value, falling back to the
 * default for anything that isn't offered. */
export function tailIntervalMs(seconds: number | undefined): number {
  const valid = (TAIL_INTERVALS_SECONDS as readonly number[]).includes(seconds ?? NaN)
  return (valid ? seconds! : DEFAULT_TAIL_INTERVAL_SECONDS) * 1000
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
