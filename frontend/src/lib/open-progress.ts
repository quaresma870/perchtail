// Progress and Cancel for a file opened whole: the viewer names each /open
// with a random request id, asks GET /open-progress about all the opens in
// progress in one request once a second, after each one's first second,
// and Cancel asks POST /open-cancel to stop one. At most
// MAX_RUNNING_OPENS run at once; the others wait in the viewer (OpenQueue),
// so the browser keeps connections free for the progress and the rest of
// the page.
import { formatSize } from './file-details'

/** An open that answers sooner shows nothing. */
export const PROGRESS_DELAY_MS = 1000
/** How often the progress is asked for after that. */
export const PROGRESS_POLL_MS = 1000
/** How many files are opened whole at once; the others are queued. */
export const MAX_RUNNING_OPENS = 3
/** How many ids GET /open-progress takes in one request. */
export const MAX_PROGRESS_IDS = 8
/** How long the server may not know an open it knew before the bar says
 * "Finishing…": the copy is over there, and the answer is on its way. */
export const FINISHING_AFTER_MS = 10_000

/** What GET /open-progress answers for one open. */
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

/** The /open URL with the request id. */
export function withRequestId(url: string, requestId: string): string {
  return `${url}${url.includes('?') ? '&' : '?'}request_id=${requestId}`
}

/** Whether an open that started `elapsedMs` ago shows its progress. */
export function showsProgress(elapsedMs: number, delay = PROGRESS_DELAY_MS): boolean {
  return elapsedMs >= delay
}

// -- what the bar says --------------------------------------------------------

/** What the viewer knows of one open. */
export interface OpenStatus {
  /** Waiting in the viewer's queue; not sent yet. */
  queued: boolean
  /** The last progress the server gave, kept while it doesn't know the id. */
  progress: OpenProgressInfo | null
  /** Whether the server ever answered with its progress. */
  seen: boolean
  /** When the server stopped knowing an open it knew (ms), or null. */
  missingSince: number | null
  /** The answer's headers are in and the browser is reading its body. */
  receiving: boolean
}

export function queuedStatus(): OpenStatus {
  return { queued: true, progress: null, seen: false, missingSince: null, receiving: false }
}

export function startedStatus(): OpenStatus {
  return { queued: false, progress: null, seen: false, missingSince: null, receiving: false }
}

export function receivingStatus(status: OpenStatus): OpenStatus {
  return { ...status, queued: false, receiving: true }
}

/** The status after the server's answer for this open: its progress, or
 * null when it doesn't know the id (not there yet, or finished). */
export function nextStatus(status: OpenStatus, answer: OpenProgressInfo | null, now: number): OpenStatus {
  if (status.receiving) return status
  if (answer !== null) return { queued: false, progress: answer, seen: true, missingSince: null, receiving: false }
  if (!status.seen) return status
  return { ...status, missingSince: status.missingSince ?? now }
}

/** "Queued"; "Receiving…" while the browser reads the answer;
 * "Finishing…" once the server hasn't known an open it knew for
 * FINISHING_AFTER_MS; "Redacting…"; "Waiting for the source…" before the
 * first byte; "128 MB of 300 MB"; "128 MB" when the size isn't known.
 * Never "0 B" alone. */
export function statusLabel(status: OpenStatus, now: number): string {
  if (status.queued) return 'Queued'
  if (status.receiving) return 'Receiving…'
  if (status.missingSince !== null && now - status.missingSince > FINISHING_AFTER_MS) return 'Finishing…'
  const progress = status.progress
  if (progress?.phase === 'redacting') return 'Redacting…'
  if (progress === null || progress.written <= 0) return 'Waiting for the source…'
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

// -- one request for every open -------------------------------------------------

/** GET /open-progress for these ids (at most MAX_PROGRESS_IDS). */
export function progressUrl(requestIds: string[]): string {
  const params = new URLSearchParams()
  for (const id of requestIds.slice(0, MAX_PROGRESS_IDS)) params.append('request_id', id)
  return `/open-progress?${params}`
}

/** The progress of every id in one request: each id's progress, or null
 * when the server doesn't know it. Null when the request failed. */
export async function fetchProgress(
  requestIds: string[],
  fetcher: (url: string, init: RequestInit) => Promise<Response> = fetch,
): Promise<Record<string, OpenProgressInfo | null> | null> {
  if (requestIds.length === 0) return {}
  try {
    const answer = await fetcher(progressUrl(requestIds), { credentials: 'include' })
    if (!answer.ok) return null
    return (await answer.json()) as Record<string, OpenProgressInfo | null>
  } catch {
    return null
  }
}

// -- the queue ------------------------------------------------------------------

/** At most `limit` opens at once, the others started in the order they
 * came (FIFO). `enter` resolves true when the open may start (it must then
 * call `leave` once it is over), or false when its signal aborted while
 * it waited: it never started, and holds nothing. */
export class OpenQueue {
  private running = 0
  private waiting: Array<() => void> = []

  constructor(readonly limit = MAX_RUNNING_OPENS) {}

  get active(): number {
    return this.running
  }

  get queued(): number {
    return this.waiting.length
  }

  enter(signal?: AbortSignal): Promise<boolean> {
    if (signal?.aborted) return Promise.resolve(false)
    if (this.running < this.limit) {
      this.running++
      return Promise.resolve(true)
    }
    return new Promise((resolve) => {
      const start = () => {
        signal?.removeEventListener('abort', abort)
        this.running++
        resolve(true)
      }
      const abort = () => {
        this.waiting = this.waiting.filter((waiter) => waiter !== start)
        resolve(false)
      }
      this.waiting.push(start)
      signal?.addEventListener('abort', abort, { once: true })
    })
  }

  leave(): void {
    this.running = Math.max(this.running - 1, 0)
    if (this.running < this.limit) this.waiting.shift()?.()
  }
}

/** The opens of every viewer in this page: a viewer left and come back to
 * still shares the browser's connections with the opens it started. */
export const openQueue = new OpenQueue()

/** Statuses whose Response can't carry a body. */
const NULL_BODY_STATUSES = new Set([204, 205, 304])

/** The response with its whole body read: the connection it came on is
 * free once this resolves. Rejects, as reading the body does, when the
 * request's signal aborts. */
export async function readWhole(response: Response): Promise<Response> {
  const body = await response.arrayBuffer()
  return new Response(NULL_BODY_STATUSES.has(response.status) ? null : body, {
    status: response.status,
    statusText: response.statusText,
    headers: response.headers,
  })
}

/** One open in its turn of `queue`: waits for the turn (null when `signal`
 * aborts first: nothing was sent), calls `started`, sends `request`, calls
 * `receiving` once the headers are in, and gives the turn back only once
 * the whole body was read, so a turn holds the browser connection for as
 * long as the open uses it. */
export async function openInTurn(
  queue: OpenQueue,
  signal: AbortSignal,
  request: () => Promise<Response>,
  hooks: { started?: () => void; receiving?: () => void } = {},
): Promise<Response | null> {
  if (!(await queue.enter(signal))) return null
  try {
    hooks.started?.()
    const response = await request()
    hooks.receiving?.()
    return await readWhole(response)
  } finally {
    queue.leave()
  }
}
