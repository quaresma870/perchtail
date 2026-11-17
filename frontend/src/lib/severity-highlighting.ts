import { writable } from 'svelte/store'
import { nextLine, previousLine } from './line-cycle'
import {
  HIGHLIGHT_MAX_CHARS,
  PATTERN_BUDGET_MS,
  type LinesMatched,
  type SeverityMatch,
} from './severity-matching'
import type { SeverityPattern } from './types'

export { HIGHLIGHT_MAX_CHARS, PATTERN_BUDGET_MS, type SeverityMatch }

/** How long past its budget a pattern may keep the worker busy before the
 * worker is stopped: the worker checks the budget itself between calls to
 * the regex engine, so this only catches one call that doesn't return. */
export const WATCHDOG_GRACE_MS = 500

/** A pattern over its budget (severity-matching.ts) is turned off until the
 * page is reloaded, and listed in `slowSeverityPatterns` for the viewer to
 * say so. */
const slowPatternIds = new Set<number>()
export const slowSeverityPatterns = writable<SeverityPattern[]>([])

function turnOff(pattern: SeverityPattern): void {
  if (slowPatternIds.has(pattern.id)) return
  slowPatternIds.add(pattern.id)
  slowSeverityPatterns.update((list) => [...list, pattern])
}

export const LEVEL_CLASS: Record<SeverityPattern['level'], string> = {
  error: 'cm-level-error',
  warning: 'cm-level-warn',
  info: 'cm-level-info',
  debug: 'cm-level-debug',
}

/** What the page asks the worker (severity-worker.ts), and what it answers:
 * `start` before each pattern runs, then `done`. */
export interface SeverityWorkerRequest {
  id: number
  lines: string[]
  patterns: SeverityPattern[]
}

export type SeverityWorkerReply =
  | { id: number; type: 'start'; patternId: number }
  | ({ id: number; type: 'done' } & LinesMatched)

/** The part of a Worker the matcher uses. */
export interface SeverityWorkerLike {
  onmessage: ((event: MessageEvent<SeverityWorkerReply>) => void) | null
  postMessage(request: SeverityWorkerRequest): void
  terminate(): void
}

interface Job {
  lines: string[]
  patterns: SeverityPattern[]
  resolve: (matches: SeverityMatch[][]) => void
}

/** Runs severity patterns in a Web Worker, one request at a time. When a
 * pattern keeps the worker busy past PATTERN_BUDGET_MS + WATCHDOG_GRACE_MS
 * (one call to the regex engine that doesn't return), the worker is
 * stopped, the pattern turned off, and the request sent again to a new
 * worker without it, so the other patterns still apply. */
export class SeverityMatcher {
  private worker: SeverityWorkerLike | null = null
  private readonly queue: Job[] = []
  private current: Job | null = null
  private currentId = 0
  private watchdog: ReturnType<typeof setTimeout> | undefined
  private readonly createWorker: () => SeverityWorkerLike

  constructor(createWorker: () => SeverityWorkerLike) {
    this.createWorker = createWorker
  }

  /** The matches of each line, from the enabled patterns not turned off. */
  match(lines: string[], patterns: SeverityPattern[]): Promise<SeverityMatch[][]> {
    return new Promise((resolve) => {
      this.queue.push({ lines, patterns: patterns.filter((p) => p.enabled), resolve })
      this.next()
    })
  }

  private next(): void {
    while (!this.current && this.queue.length > 0) {
      this.current = this.queue.shift() ?? null
      this.send()
    }
  }

  private send(): void {
    const job = this.current
    if (!job) return
    job.patterns = job.patterns.filter((p) => !slowPatternIds.has(p.id))
    if (job.patterns.length === 0 || job.lines.length === 0) {
      this.finish(job.lines.map(() => []))
      return
    }
    if (!this.worker) {
      this.worker = this.createWorker()
      this.worker.onmessage = (event) => this.receive(event.data)
    }
    this.currentId += 1
    this.worker.postMessage({ id: this.currentId, lines: job.lines, patterns: job.patterns })
  }

  private receive(reply: SeverityWorkerReply): void {
    const job = this.current
    if (!job || reply.id !== this.currentId) return
    clearTimeout(this.watchdog)
    if (reply.type === 'start') {
      const pattern = job.patterns.find((p) => p.id === reply.patternId)
      if (pattern) {
        this.watchdog = setTimeout(
          () => this.stuck(pattern),
          PATTERN_BUDGET_MS + WATCHDOG_GRACE_MS,
        )
      }
      return
    }
    for (const id of reply.slow) {
      const pattern = job.patterns.find((p) => p.id === id)
      if (pattern) turnOff(pattern)
    }
    this.finish(reply.matches)
  }

  private stuck(pattern: SeverityPattern): void {
    this.worker?.terminate()
    this.worker = null
    turnOff(pattern)
    this.send()
  }

  private finish(matches: SeverityMatch[][]): void {
    const job = this.current
    this.current = null
    job?.resolve(matches)
    this.next()
  }
}

/** The page's own matcher. The worker is a file of the bundle, served from
 * the page's origin, which the CSP allows: it has no `worker-src` or
 * `child-src`, so workers fall back to `script-src 'self'`
 * (backend/app/main.py). */
export const severityMatcher = new SeverityMatcher(
  () =>
    new Worker(new URL('./severity-worker.ts', import.meta.url), {
      type: 'module',
    }) as unknown as SeverityWorkerLike,
)

/** 1-indexed line numbers containing a match from a navigation-eligible
 * pattern (`include_in_navigation`), for the "next/previous problem" step
 * command -- deliberately a per-pattern flag rather than a fixed
 * warn-or-worse severity floor, so an admin decides what counts as a "step
 * to" problem instead of it being hardcoded. */
export async function findProblemLines(
  content: string,
  patterns: SeverityPattern[],
  matcher: SeverityMatcher = severityMatcher,
): Promise<number[]> {
  const navPatterns = patterns.filter((p) => p.include_in_navigation)
  if (navPatterns.length === 0) return []

  const matches = await matcher.match(content.split('\n'), navPatterns)
  const result: number[] = []
  matches.forEach((lineMatches, index) => {
    if (lineMatches.length > 0) result.push(index + 1)
  })
  return result
}

/** Steps forward from `currentLine` to the next problem line, wrapping
 * around to the first one past the end of the document. `problemLines`
 * must already be sorted ascending (as `findProblemLines` returns it).
 * Thin aliases over the generic `line-cycle.ts` steppers -- also used
 * as-is for bookmark navigation (Viewer.svelte), which needs the exact
 * same wrap-around behavior over its own list of marked lines. */
export const nextProblemLine = nextLine
export const previousProblemLine = previousLine
