import { get } from 'svelte/store'
import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  findProblemLines,
  nextProblemLine,
  PATTERN_BUDGET_MS,
  previousProblemLine,
  SeverityMatcher,
  slowSeverityPatterns,
  WATCHDOG_GRACE_MS,
  type SeverityWorkerLike,
  type SeverityWorkerReply,
  type SeverityWorkerRequest,
} from './severity-highlighting'
import { matchLines } from './severity-matching'
import type { SeverityPattern } from './types'

function pattern(overrides: Partial<SeverityPattern> = {}): SeverityPattern {
  return {
    id: 1,
    source_id: null,
    level: 'error',
    pattern: 'error',
    pattern_kind: 'glob',
    enabled: true,
    highlight_line: false,
    include_in_navigation: true,
    ...overrides,
  }
}

/** A worker that answers like severity-worker.ts, a moment later, except
 * that it never comes back from a pattern whose id is in `hangOn`, as when
 * one call to the browser's regex engine doesn't return. */
class FakeWorker implements SeverityWorkerLike {
  onmessage: ((event: MessageEvent<SeverityWorkerReply>) => void) | null = null
  terminated = false
  readonly requests: SeverityWorkerRequest[] = []
  private readonly hangOn: Set<number>

  constructor(hangOn: Set<number>) {
    this.hangOn = hangOn
  }

  postMessage(request: SeverityWorkerRequest): void {
    this.requests.push(request)
    setTimeout(() => this.run(request), 0)
  }

  terminate(): void {
    this.terminated = true
  }

  private reply(reply: SeverityWorkerReply): void {
    if (!this.terminated) this.onmessage?.({ data: reply } as MessageEvent<SeverityWorkerReply>)
  }

  private run({ id, lines, patterns }: SeverityWorkerRequest): void {
    const matches = lines.map(() => [] as ReturnType<typeof matchLines>['matches'][number])
    const slow: number[] = []
    for (const p of patterns) {
      this.reply({ id, type: 'start', patternId: p.id })
      if (this.hangOn.has(p.id)) return
      const result = matchLines(lines, [p])
      result.matches.forEach((m, i) => matches[i].push(...m))
      slow.push(...result.slow)
    }
    this.reply({ id, type: 'done', matches, slow })
  }
}

function fakeMatcher(hangOn: number[] = []) {
  const workers: FakeWorker[] = []
  const matcher = new SeverityMatcher(() => {
    const worker = new FakeWorker(new Set(hangOn))
    workers.push(worker)
    return worker
  })
  return { matcher, workers }
}

describe('SeverityMatcher', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  it('matches lines in the worker', async () => {
    const { matcher, workers } = fakeMatcher()
    const matches = await matcher.match(['ok', 'an error'], [pattern({ id: 101 })])
    expect(matches.map((m) => m.length)).toEqual([0, 1])
    expect(workers).toHaveLength(1)
  })

  it('stops a worker stuck on a pattern, turns the pattern off, and keeps the others', async () => {
    vi.useFakeTimers()
    const { matcher, workers } = fakeMatcher([201])
    const stuck = pattern({ id: 201, pattern: '(a+)+$', pattern_kind: 'regex' })
    const fine = pattern({ id: 202, pattern: 'error', level: 'warning' })

    const result = matcher.match(['aaaa error'], [stuck, fine])
    // Within its budget and the grace, the worker is left alone.
    await vi.advanceTimersByTimeAsync(PATTERN_BUDGET_MS + WATCHDOG_GRACE_MS - 1)
    expect(workers[0].terminated).toBe(false)
    await vi.advanceTimersByTimeAsync(2)
    expect(workers[0].terminated).toBe(true)

    const matches = await result
    expect(matches[0]).toEqual([expect.objectContaining({ level: 'warning', matchStart: 5 })])
    expect(get(slowSeverityPatterns).filter((p) => p.id === 201)).toHaveLength(1)
    // A new worker took the request without the pattern, and never sees it again.
    expect(workers).toHaveLength(2)
    expect(workers[1].requests[0].patterns.map((p) => p.id)).toEqual([202])
    vi.useRealTimers()
    await matcher.match(['aaaa'], [stuck, fine])
    expect(workers[1].requests[1].patterns.map((p) => p.id)).toEqual([202])
  })

  it('turns off a pattern the worker found over its budget', async () => {
    const { matcher } = fakeMatcher()
    let now = 0
    const spy = vi
      .spyOn(performance, 'now')
      .mockImplementation(() => (now += PATTERN_BUDGET_MS + 1))
    const slow = pattern({ id: 301 })
    const matches = await matcher.match(['error'], [slow])
    spy.mockRestore()

    expect(matches).toEqual([[]])
    expect(get(slowSeverityPatterns).filter((p) => p.id === 301)).toHaveLength(1)
    // Turned off: it matches nothing any more, and is listed once.
    expect(await matcher.match(['error'], [slow])).toEqual([[]])
    expect(get(slowSeverityPatterns).filter((p) => p.id === 301)).toHaveLength(1)
  })

  it('answers requests one at a time, in order', async () => {
    const { matcher } = fakeMatcher()
    const p = pattern({ id: 401 })
    const [first, second] = await Promise.all([
      matcher.match(['error'], [p]),
      matcher.match(['ok', 'error error'], [p]),
    ])
    expect(first.map((m) => m.length)).toEqual([1])
    expect(second.map((m) => m.length)).toEqual([0, 2])
  })

  it('needs no worker when no pattern applies', async () => {
    const { matcher, workers } = fakeMatcher()
    expect(await matcher.match(['error'], [pattern({ enabled: false })])).toEqual([[]])
    expect(workers).toHaveLength(0)
  })
})

describe('findProblemLines', () => {
  const content = 'line one\nERROR here\nline three\nwarn: retry\nline five'

  it('returns 1-indexed lines with a navigation-eligible match', async () => {
    const { matcher } = fakeMatcher()
    expect(await findProblemLines(content, [pattern({ pattern: 'error' })], matcher)).toEqual([2])
  })

  it('excludes patterns with include_in_navigation false', async () => {
    const { matcher, workers } = fakeMatcher()
    expect(
      await findProblemLines(
        content,
        [pattern({ pattern: 'error', include_in_navigation: false })],
        matcher,
      ),
    ).toEqual([])
    expect(workers).toHaveLength(0)
  })

  it('collects matches across multiple patterns, sorted ascending', async () => {
    const { matcher } = fakeMatcher()
    expect(
      await findProblemLines(
        content,
        [
          pattern({ id: 501, pattern: 'warn', level: 'warning' }),
          pattern({ id: 502, pattern: 'error', level: 'error' }),
        ],
        matcher,
      ),
    ).toEqual([2, 4])
  })

  it('matches only the first 2000 characters of each line', async () => {
    const { matcher } = fakeMatcher()
    const p = pattern({ id: 503 })
    expect(await findProblemLines('x'.repeat(2500) + 'error\nerror', [p], matcher)).toEqual([2])
  })

  it('returns empty for no patterns', async () => {
    const { matcher } = fakeMatcher()
    expect(await findProblemLines(content, [], matcher)).toEqual([])
  })
})

describe('nextProblemLine', () => {
  it('steps to the next line after the current position', () => {
    expect(nextProblemLine([2, 4, 9], 2)).toBe(4)
  })

  it('wraps around to the first problem line past the end', () => {
    expect(nextProblemLine([2, 4, 9], 9)).toBe(2)
    expect(nextProblemLine([2, 4, 9], 100)).toBe(2)
  })

  it('returns null when there are no problem lines', () => {
    expect(nextProblemLine([], 5)).toBeNull()
  })
})

describe('previousProblemLine', () => {
  it('steps to the previous line before the current position', () => {
    expect(previousProblemLine([2, 4, 9], 9)).toBe(4)
  })

  it('wraps around to the last problem line before the start', () => {
    expect(previousProblemLine([2, 4, 9], 2)).toBe(9)
    expect(previousProblemLine([2, 4, 9], 1)).toBe(9)
  })

  it('returns null when there are no problem lines', () => {
    expect(previousProblemLine([], 5)).toBeNull()
  })
})
