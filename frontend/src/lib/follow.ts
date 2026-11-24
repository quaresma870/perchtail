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
  end?: number,
): string {
  const params = new URLSearchParams({ path, offset: String(offset) })
  if (head) params.set('head', head)
  if (start) params.set('start', 'true')
  // Only what comes before `end` ("Load earlier").
  if (end !== undefined) params.set('end', String(end))
  return `/sources/${sourceId}/follow?${params}`
}

/** Where a redacted Follow answer's first line starts in the original
 * (X-Follow-Start), or null when the server didn't say. */
export function readFollowStart(headers: Headers): number | null {
  if (!headers.has('x-follow-start')) return null
  const value = Number(headers.get('x-follow-start'))
  return Number.isFinite(value) && value >= 0 ? value : null
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

/** Redaction (#382, #249): what the server says about content it redacted
 * (/open and /follow). Offsets stay in bytes of the original file, so the
 * Tail goes on from `sourceOffset`, not from the length received; the last
 * `partialBytes` of the body stand for an unfinished last line, which the
 * next Follow answer sends again; `aligned` says a Follow answer starts at
 * the next whole line rather than at the offset asked. */
export interface RedactionInfo {
  redacted: boolean
  count: number
  sourceOffset: number | null
  partialBytes: number
  aligned: boolean
}

function headerNumber(headers: Headers, name: string): number | null {
  if (!headers.has(name)) return null
  const value = Number(headers.get(name))
  return Number.isFinite(value) && value >= 0 ? value : null
}

export function readRedactionHeaders(headers: Headers): RedactionInfo {
  const redacted = headers.get('x-redacted') === '1'
  return {
    redacted,
    count: redacted ? (headerNumber(headers, 'x-redaction-count') ?? 0) : 0,
    sourceOffset: redacted ? headerNumber(headers, 'x-source-offset') : null,
    partialBytes: redacted ? (headerNumber(headers, 'x-follow-partial') ?? 0) : 0,
    aligned: headers.get('x-follow-aligned') === '1',
  }
}

/** Decodes a body whose last `partialBytes` stand for an unfinished line:
 * the text, and how many characters at its end the next Follow answer
 * replaces. */
export function decodeWithPartial(
  decoder: TextDecoder,
  bytes: Uint8Array,
  partialBytes: number,
): { text: string; partialChars: number } {
  const cut = Math.max(0, bytes.byteLength - Math.min(partialBytes, bytes.byteLength))
  const text = decoder.decode(bytes.subarray(0, cut), { stream: true })
  const partial = cut < bytes.byteLength ? decoder.decode(bytes.subarray(cut), { stream: true }) : ''
  return { text: text + partial, partialChars: partial.length }
}

/** Appends a Follow answer to `appended`, first dropping the `partialChars`
 * at the end of what the viewer holds (the unfinished line the answer sends
 * again). What can't be dropped from `appended` is dropped from the tab's
 * content: returned as `dropFromContent`. */
export function appendReplacingPartial(
  appended: string,
  dropFromContent: number,
  partialChars: number,
  text: string,
): { appended: string; dropFromContent: number } {
  if (appended.length >= partialChars) {
    return { appended: appended.slice(0, appended.length - partialChars) + text, dropFromContent }
  }
  return { appended: text, dropFromContent: dropFromContent + partialChars - appended.length }
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
