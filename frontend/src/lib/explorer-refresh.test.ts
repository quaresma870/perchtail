import { afterEach, describe, expect, it } from 'vitest'
import {
  parentPath,
  refreshAll,
  refreshFolder,
  registerFolder,
  resetRefreshState,
} from './explorer-refresh'

afterEach(() => resetRefreshState())

function recorder(log: string[], name: string, ok = true, delayMs = 0) {
  return async () => {
    log.push(`start ${name}`)
    await new Promise((r) => setTimeout(r, delayMs))
    log.push(`end ${name}`)
    return ok
  }
}

describe('refreshAll', () => {
  it('re-lists one folder at a time per source', async () => {
    const log: string[] = []
    registerFolder({ sourceId: 1, path: '', refresh: recorder(log, 'a', true, 5) })
    registerFolder({ sourceId: 1, path: 'x', refresh: recorder(log, 'b') })
    await refreshAll(1000, 0)
    expect(log).toEqual(['start a', 'end a', 'start b', 'end b'])
  })

  it('leaves a failing source alone for a while, then tries again', async () => {
    const log: string[] = []
    registerFolder({ sourceId: 2, path: '', refresh: recorder(log, 'fail', false) })
    await refreshAll(1000, 0)
    await refreshAll(1000, 1000) // still backing off (2 s after one failure)
    expect(log.filter((l) => l.startsWith('start'))).toHaveLength(1)
    await refreshAll(1000, 2500)
    expect(log.filter((l) => l.startsWith('start'))).toHaveLength(2)
  })

  it('stops refreshing a folder once it unregisters', async () => {
    const log: string[] = []
    const unregister = registerFolder({ sourceId: 3, path: '', refresh: recorder(log, 'a') })
    unregister()
    await refreshAll(1000, 0)
    expect(log).toEqual([])
  })
})

describe('refreshFolder and parentPath', () => {
  it('re-lists the open parent of a file', async () => {
    const log: string[] = []
    registerFolder({ sourceId: 4, path: 'logs', refresh: recorder(log, 'logs') })
    await refreshFolder(4, parentPath('logs/app.log'))
    await refreshFolder(4, parentPath('other/app.log')) // not open: nothing
    expect(log).toEqual(['start logs', 'end logs'])
    expect(parentPath('app.log')).toBe('')
  })
})

describe('startAutoRefresh', () => {
  it('keeps a running timer when the interval is unchanged', async () => {
    const { startAutoRefresh } = await import('./explorer-refresh')
    const { vi } = await import('vitest')
    vi.useFakeTimers()
    try {
      const log: string[] = []
      registerFolder({ sourceId: 9, path: '', refresh: recorder(log, 'x') })
      startAutoRefresh(10)
      await vi.advanceTimersByTimeAsync(6000)
      startAutoRefresh(10) // e.g. the System settings were read again
      await vi.advanceTimersByTimeAsync(5000)
      expect(log).toContain('start x')
    } finally {
      vi.useRealTimers()
    }
  })
})
