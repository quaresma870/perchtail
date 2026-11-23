import { afterEach, describe, expect, it, vi } from 'vitest'
import { get } from 'svelte/store'
import {
  noteAnswered,
  noteUnanswered,
  resetServerStatus,
  serverReachable,
  trackedFetch,
} from './server-status'

afterEach(() => {
  resetServerStatus()
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe('serverReachable', () => {
  it('needs two failures in a row, and one answer to recover', () => {
    vi.useFakeTimers()
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('offline')))
    noteUnanswered()
    expect(get(serverReachable)).toBe(true)
    noteUnanswered()
    expect(get(serverReachable)).toBe(false)
    noteAnswered()
    expect(get(serverReachable)).toBe(true)
  })

  it('pings /healthz until the server answers', async () => {
    vi.useFakeTimers()
    const fetchMock = vi
      .fn()
      .mockRejectedValueOnce(new TypeError('offline'))
      .mockResolvedValue(new Response('ok', { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    noteUnanswered()
    noteUnanswered()
    await vi.advanceTimersByTimeAsync(2000) // first ping fails
    expect(get(serverReachable)).toBe(false)
    await vi.advanceTimersByTimeAsync(4000) // second answers
    expect(get(serverReachable)).toBe(true)
    expect(fetchMock).toHaveBeenCalledWith('/healthz', { cache: 'no-store' })
  })

  it('counts the probe refusing a remote browser as PerchTail being back (#366)', async () => {
    vi.useFakeTimers()
    const refused = () => new Response('{"detail":"Unauthorized"}', { status: 401, headers: { 'X-PerchTail': '1' } })
    const fromProxy = () => new Response('', { status: 401 })
    const fetchMock = vi.fn().mockResolvedValueOnce(fromProxy()).mockImplementation(async () => refused())
    vi.stubGlobal('fetch', fetchMock)
    noteUnanswered()
    noteUnanswered()
    await vi.advanceTimersByTimeAsync(2000) // a proxy's answer isn't PerchTail
    expect(get(serverReachable)).toBe(false)
    await vi.advanceTimersByTimeAsync(4000) // PerchTail's own 401 is
    expect(get(serverReachable)).toBe(true)
  })

  it('counts a gateway error as no answer, any other status as an answer', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('', { status: 502 })))
    await trackedFetch('/x')
    await trackedFetch('/x')
    expect(get(serverReachable)).toBe(false)
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('', { status: 404 })))
    await trackedFetch('/x')
    expect(get(serverReachable)).toBe(true)
  })
})

describe('a 502 from PerchTail itself', () => {
  it('is a failing source, not PerchTail being down', async () => {
    const fromPerchTail = () =>
      new Response('{"detail":"The source isn\'t reachable"}', {
        status: 502,
        headers: { 'X-PerchTail': '1' },
      })
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async () => fromPerchTail()))
    await trackedFetch('/sources/1/browse')
    await trackedFetch('/sources/1/browse')
    expect(get(serverReachable)).toBe(true)
  })
})
