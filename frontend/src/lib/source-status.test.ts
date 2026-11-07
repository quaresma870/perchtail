import { describe, expect, it } from 'vitest'
import { sourceStatusView } from './source-status'

const now = new Date('2026-11-06T12:10:00Z')
const remote = (status: 'ok' | 'failing' | 'unknown', message: string | null = null) => ({
  protocol: 'ssh' as const,
  status,
  status_checked_at: '2026-11-06T12:00:00',
  status_message: message,
})

describe('sourceStatusView', () => {
  it('shows a remote source as last known, with how long ago', () => {
    expect(sourceStatusView(remote('ok'), null, true, now)).toEqual({
      state: 'ok',
      tooltip: 'Reachable (10 min ago)',
    })
    expect(sourceStatusView(remote('failing', 'Credentials refused'), null, true, now)).toEqual({
      state: 'failing',
      tooltip: 'Credentials refused (10 min ago)',
    })
    expect(sourceStatusView(remote('unknown'), null, true, now).state).toBe('unknown')
  })

  it('shows an agent source by its connection', () => {
    const agent = { ...remote('failing'), protocol: 'agent' as const }
    expect(sourceStatusView(agent, null, true, now)).toEqual({
      state: 'disconnected',
      tooltip: 'Agent not connected (last seen 10 min ago)',
    })
    expect(sourceStatusView({ ...agent, status: 'ok' }, null, true, now).state).toBe('ok')
  })

  it('prefers a check just made, and says unknown while PerchTail is down', () => {
    const checked = { ok: false, detail: 'Timed out', at: '2026-11-06T12:09:50Z' }
    expect(sourceStatusView(remote('ok'), checked, true, now)).toEqual({
      state: 'failing',
      tooltip: 'Timed out (checked just now)',
    })
    expect(sourceStatusView(remote('ok'), null, false, now).state).toBe('unknown')
  })
})
