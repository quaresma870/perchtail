// Progress and Cancel for a file opened whole: the viewer names each /open
// with a random request id, asks GET /open-progress about it once a second
// after the first second, and Cancel asks POST /open-cancel to stop it.
import { formatSize } from './file-details'

/** An open that answers sooner shows nothing. */
export const PROGRESS_DELAY_MS = 1000
/** How often the progress is asked for after that. */
export const PROGRESS_POLL_MS = 1000

/** What GET /open-progress answers. */
export interface OpenProgressInfo {
  expected: number | null
  written: number
  phase: 'copying' | 'redacting'
}

/** 32 random hex characters, as /open takes in `request_id`. */
export function newRequestId(
  random: (bytes: Uint8Array<ArrayBuffer>) => Uint8Array = (b) => crypto.getRandomValues(b),
): string {
  const bytes = random(new Uint8Array(new ArrayBuffer(16)))
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('')
}

/** "128 MB of 300 MB"; "128 MB" when the size isn't known; "Redacting…"
 * once the copy is done and redaction runs; "Starting…" before the first
 * answer. */
export function progressLabel(progress: OpenProgressInfo | null): string {
  if (progress === null) return 'Starting…'
  if (progress.phase === 'redacting') return 'Redacting…'
  if (progress.expected === null) return formatSize(progress.written)
  return `${formatSize(progress.written)} of ${formatSize(progress.expected)}`
}

/** The bar's value and maximum (for aria-valuenow / aria-valuemax), when
 * the size is known and the copy is still running. */
export function progressValues(progress: OpenProgressInfo | null): { now: number; max: number } | null {
  if (progress === null || progress.expected === null || progress.phase !== 'copying') return null
  const max = Math.max(progress.expected, 1)
  return { now: Math.min(progress.written, max), max }
}

/** Whether an open that started `elapsedMs` ago shows its progress. */
export function showsProgress(elapsedMs: number, delay = PROGRESS_DELAY_MS): boolean {
  return elapsedMs >= delay
}

/** The /open URL with the request id. */
export function withRequestId(url: string, requestId: string): string {
  return `${url}${url.includes('?') ? '&' : '?'}request_id=${requestId}`
}
