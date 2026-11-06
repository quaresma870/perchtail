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
