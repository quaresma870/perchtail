import { afterEach, describe, expect, it, vi } from 'vitest'
import { get } from 'svelte/store'

vi.mock('./api', async () => {
  const actual = await vi.importActual<typeof import('./api')>('./api')
  return { ...actual, api: { get: vi.fn() } }
})

import { api } from './api'
import { refreshSystemSettingsIfStale, systemSettings } from './settings'

describe('refreshSystemSettingsIfStale (#196)', () => {
  afterEach(() => vi.useRealTimers())

  it('re-reads settings at most once per interval', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-11-04T12:00:00Z'))
    const tree = { ...get(systemSettings), connections_view_default: 'tree' as const }
    vi.mocked(api.get).mockResolvedValue(tree)

    await refreshSystemSettingsIfStale()
    expect(get(systemSettings).connections_view_default).toBe('tree')
    expect(refreshSystemSettingsIfStale()).toBeUndefined()

    vi.setSystemTime(new Date('2026-11-04T12:00:31Z'))
    await refreshSystemSettingsIfStale()
    expect(api.get).toHaveBeenCalledTimes(2)
  })
})
