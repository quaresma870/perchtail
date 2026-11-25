import { describe, expect, it, vi } from 'vitest'
import {
  fetchProgress,
  FINISHING_AFTER_MS,
  MAX_RUNNING_OPENS,
  newRequestId,
  nextStatus,
  OpenQueue,
  progressUrl,
  progressValues,
  PROGRESS_DELAY_MS,
  queuedStatus,
  showsProgress,
  openInTurn,
  readWhole,
  receivingStatus,
  startedStatus,
  statusLabel,
  withRequestId,
  type OpenProgressInfo,
} from './open-progress'

const MB = 1024 * 1024
const copying = (written: number, expected: number | null = 300 * MB): OpenProgressInfo => ({
  expected,
  written,
  phase: 'copying',
})

describe('what the bar of an open says', () => {
  it('says Queued while it waits in the viewer', () => {
    expect(statusLabel(queuedStatus(), 0)).toBe('Queued')
  })

  it('says Waiting for the source… before the server knows it or before its first byte', () => {
    expect(statusLabel(startedStatus(), 0)).toBe('Waiting for the source…')
    expect(statusLabel(nextStatus(startedStatus(), null, 0), 0)).toBe('Waiting for the source…')
    expect(statusLabel(nextStatus(startedStatus(), copying(0), 0), 0)).toBe('Waiting for the source…')
    // A push agent doesn't say the size: still not "0 B".
    expect(statusLabel(nextStatus(startedStatus(), copying(0, null), 0), 0)).toBe('Waiting for the source…')
  })

  it('says how much of how much was copied', () => {
    expect(statusLabel(nextStatus(startedStatus(), copying(128 * MB), 0), 0)).toBe('128 MB of 300 MB')
  })

  it('says only how much when the size is unknown', () => {
    expect(statusLabel(nextStatus(startedStatus(), copying(128 * MB, null), 0), 0)).toBe('128 MB')
  })

  it('says Redacting… once redaction runs', () => {
    const redacting: OpenProgressInfo = { expected: 300 * MB, written: 300 * MB, phase: 'redacting' }
    expect(statusLabel(nextStatus(startedStatus(), redacting, 0), 0)).toBe('Redacting…')
  })

  it('keeps the last progress while the server no longer knows the open, then says Finishing…', () => {
    let status = nextStatus(startedStatus(), copying(128 * MB), 0)
    status = nextStatus(status, null, 1_000)
    expect(status.missingSince).toBe(1_000)
    expect(statusLabel(status, 1_000)).toBe('128 MB of 300 MB')
    status = nextStatus(status, null, 5_000)
    expect(status.missingSince).toBe(1_000)
    expect(statusLabel(status, 1_000 + FINISHING_AFTER_MS)).toBe('128 MB of 300 MB')
    expect(statusLabel(status, 1_001 + FINISHING_AFTER_MS)).toBe('Finishing…')
    // Known again: back to its progress.
    status = nextStatus(status, copying(200 * MB), 20_000)
    expect(status.missingSince).toBeNull()
    expect(statusLabel(status, 20_000)).toBe('200 MB of 300 MB')
  })

  it('says Receiving… while the browser reads the answer, whatever the server says', () => {
    let status = receivingStatus(nextStatus(startedStatus(), copying(300 * MB), 0))
    expect(statusLabel(status, 0)).toBe('Receiving…')
    status = nextStatus(status, null, 1_000)
    expect(statusLabel(status, 1_000 + 2 * FINISHING_AFTER_MS)).toBe('Receiving…')
  })

  it('never says Finishing… for an open the server never knew', () => {
    const status = nextStatus(startedStatus(), null, 0)
    expect(statusLabel(status, 60_000)).toBe('Waiting for the source…')
  })

  it('gives the bar a value only while copying a file of known size', () => {
    expect(progressValues({ expected: 300, written: 128, phase: 'copying' })).toEqual({ now: 128, max: 300 })
    expect(progressValues({ expected: 300, written: 400, phase: 'copying' })).toEqual({ now: 300, max: 300 })
    expect(progressValues({ expected: 0, written: 0, phase: 'copying' })).toEqual({ now: 0, max: 1 })
    expect(progressValues({ expected: null, written: 128, phase: 'copying' })).toBeNull()
    expect(progressValues({ expected: 300, written: 300, phase: 'redacting' })).toBeNull()
    expect(progressValues(null)).toBeNull()
  })
})

describe('asking for the progress', () => {
  const a = 'a'.repeat(32)
  const b = 'b'.repeat(32)

  it('asks about every open in one request', async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({ [a]: copying(1), [b]: null })))
    expect(await fetchProgress([a, b], fetcher)).toEqual({ [a]: copying(1), [b]: null })
    expect(fetcher).toHaveBeenCalledTimes(1)
    expect(fetcher).toHaveBeenCalledWith(`/open-progress?request_id=${a}&request_id=${b}`, { credentials: 'include' })
  })

  it('asks nothing without opens, and gives null when the request fails', async () => {
    const fetcher = vi.fn(async () => new Response('', { status: 500 }))
    expect(await fetchProgress([], fetcher)).toEqual({})
    expect(fetcher).not.toHaveBeenCalled()
    expect(await fetchProgress([a], fetcher)).toBeNull()
    expect(await fetchProgress([a], async () => Promise.reject(new Error('offline')))).toBeNull()
  })

  it('never sends more than eight ids', () => {
    const ids = Array.from({ length: 10 }, (_, n) => n.toString(16).padStart(32, '0'))
    expect(new URLSearchParams(progressUrl(ids).split('?')[1]).getAll('request_id')).toEqual(ids.slice(0, 8))
  })
})

describe('the queue of opens', () => {
  const flush = () => new Promise((resolve) => setTimeout(resolve, 0))

  it('runs at most three at once, the others in the order they came', async () => {
    expect(MAX_RUNNING_OPENS).toBe(3)
    const queue = new OpenQueue()
    const started: number[] = []
    let most = 0
    const opens = Array.from({ length: 6 }, (_, n) =>
      queue.enter().then((go) => {
        expect(go).toBe(true)
        started.push(n)
        most = Math.max(most, queue.active)
      }),
    )
    await flush()
    expect(started).toEqual([0, 1, 2])
    expect(queue.queued).toBe(3)
    queue.leave()
    await flush()
    expect(started).toEqual([0, 1, 2, 3])
    queue.leave()
    queue.leave()
    await flush()
    expect(started).toEqual([0, 1, 2, 3, 4, 5])
    await Promise.all(opens)
    expect(most).toBe(3)
    expect(queue.active).toBe(3)
    queue.leave()
    queue.leave()
    queue.leave()
    expect(queue.active).toBe(0)
  })

  it('lets a queued open be cancelled without it ever starting', async () => {
    const queue = new OpenQueue(1)
    expect(await queue.enter()).toBe(true)
    const first = new AbortController()
    const second = new AbortController()
    const firstTurn = queue.enter(first.signal)
    const secondTurn = queue.enter(second.signal)
    first.abort()
    expect(await firstTurn).toBe(false)
    expect(queue.queued).toBe(1)
    queue.leave()
    expect(await secondTurn).toBe(true)
    expect(queue.active).toBe(1)
    // Already cancelled: never queued.
    const gone = new AbortController()
    gone.abort()
    expect(await queue.enter(gone.signal)).toBe(false)
    expect(queue.queued).toBe(0)
  })

  it('keeps an open’s turn until its whole body was read', async () => {
    const queue = new OpenQueue()
    // Each answer's headers are in at once, its body's end only when the
    // test says so.
    const finishBody: Array<() => void> = []
    const slowBody = (n: number) =>
      new ReadableStream<Uint8Array>({
        start(controller) {
          controller.enqueue(new TextEncoder().encode(`file ${n}, `))
          finishBody[n] = () => {
            controller.enqueue(new TextEncoder().encode('last part'))
            controller.close()
          }
        },
      })
    const sent: number[] = []
    const receiving: number[] = []
    const answers = Array.from({ length: 4 }, (_, n) =>
      openInTurn(
        queue,
        new AbortController().signal,
        async () => {
          sent.push(n)
          return new Response(slowBody(n), { headers: { 'x-scratch-key': `k${n}` } })
        },
        { receiving: () => receiving.push(n) },
      ),
    )
    await flush()
    // The headers of the first three are in, their bodies aren't: the
    // fourth waits.
    expect(receiving).toEqual([0, 1, 2])
    expect(sent).toEqual([0, 1, 2])
    expect(queue.active).toBe(3)
    expect(queue.queued).toBe(1)
    finishBody[0]()
    const first = await answers[0]
    await flush()
    expect(sent).toEqual([0, 1, 2, 3])
    expect(await first!.text()).toBe('file 0, last part')
    expect(first!.headers.get('x-scratch-key')).toBe('k0')
    expect(first!.status).toBe(200)
    for (const n of [1, 2, 3]) finishBody[n]()
    expect(await (await answers[3])!.text()).toBe('file 3, last part')
    await Promise.all(answers)
    expect(queue.active).toBe(0)
  })

  it('gives the turn back when the body can’t be read, and null when cancelled while queued', async () => {
    const queue = new OpenQueue(1)
    const broken = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.error(new Error('connection lost'))
      },
    })
    await expect(openInTurn(queue, new AbortController().signal, async () => new Response(broken))).rejects.toThrow()
    expect(queue.active).toBe(0)
    const cancelled = new AbortController()
    cancelled.abort()
    const request = vi.fn(async () => new Response('x'))
    expect(await openInTurn(queue, cancelled.signal, request)).toBeNull()
    expect(request).not.toHaveBeenCalled()
  })

  it('keeps an error answer’s status and body', async () => {
    const answer = await readWhole(new Response('{"detail":"gone"}', { status: 404, statusText: 'Not Found' }))
    expect(answer.ok).toBe(false)
    expect(answer.status).toBe(404)
    expect(answer.statusText).toBe('Not Found')
    expect(await answer.json()).toEqual({ detail: 'gone' })
    expect((await readWhole(new Response(null, { status: 204 }))).status).toBe(204)
  })
})

describe('showing the bar', () => {
  it('shows nothing for an open that answers within the first second', () => {
    expect(PROGRESS_DELAY_MS).toBe(1000)
    expect(showsProgress(0)).toBe(false)
    expect(showsProgress(999)).toBe(false)
    expect(showsProgress(1000)).toBe(true)
  })
})

describe('the request id', () => {
  it('is 32 lowercase hex characters from 16 random bytes', () => {
    const id = newRequestId((bytes) => bytes.map((_, i) => (i * 17) % 256))
    expect(id).toBe('00112233445566778899aabbccddeeff')
    expect(newRequestId()).toMatch(/^[0-9a-f]{32}$/)
    expect(newRequestId()).not.toBe(newRequestId())
  })

  it('is added to the /open URL', () => {
    expect(withRequestId('/sources/1/open?path=a.log', 'ab'.repeat(16))).toBe(
      `/sources/1/open?path=a.log&request_id=${'ab'.repeat(16)}`,
    )
  })
})
