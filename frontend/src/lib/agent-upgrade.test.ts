import { describe, expect, it } from 'vitest'
import {
  anyUpgrading,
  bulkSummary,
  bulkTargets,
  effectivePolicy,
  formatDays,
  normalizeStart,
  runActive,
  runDuration,
  runRequester,
  runStatusText,
  sourcePolicyLabel,
  statusTone,
  upgradeResultText,
  windowText,
} from './agent-upgrade'
import type { AgentRow, AgentUpgradeRun } from './types'

const run = (fields: Partial<AgentUpgradeRun> = {}): AgentUpgradeRun => ({
  id: 1,
  status: 'downloading',
  message: null,
  from_version: '0.6.0',
  to_version: '0.7.0',
  trigger: 'manual',
  requested_by: 'admin',
  started_at: '2026-11-09T10:00:00',
  finished_at: null,
  suspended_source: false,
  ...fields,
})

const row = (fields: Partial<AgentRow> = {}): AgentRow => ({
  source_id: 1,
  name: 'agent01',
  customer: null,
  enabled: true,
  connected: true,
  agent_version: '0.6.0',
  outdated: true,
  policy: 'inherit',
  effective_policy: 'manual',
  failures: 0,
  suspended: false,
  blocker: null,
  last_run: null,
  upgrading: false,
  ...fields,
})

describe('policies', () => {
  it('a source follows the system unless it has its own policy', () => {
    expect(effectivePolicy('inherit', 'automatic')).toBe('automatic')
    expect(effectivePolicy('off', 'automatic')).toBe('off')
    expect(effectivePolicy('automatic', 'off')).toBe('automatic')
  })

  it('names what "inherit" follows', () => {
    expect(sourcePolicyLabel('inherit', 'off')).toBe('Follow the system (Off)')
    expect(sourcePolicyLabel('manual', 'off')).toBe('Manual')
  })
})

describe('upgrade all outdated agents', () => {
  it('asks the agents that could upgrade now, but not suspended ones', () => {
    const rows = [
      row({ source_id: 1 }),
      row({ source_id: 2, blocker: "The agent isn't connected" }),
      row({ source_id: 3, suspended: true, failures: 3 }),
      row({ source_id: 4, blocker: 'Remote upgrades are off for this agent' }),
      row({ source_id: 5, effective_policy: 'automatic' }),
    ]
    expect(bulkTargets(rows).map((r) => r.source_id)).toEqual([1, 5])
  })

  it('summarises the results', () => {
    expect(bulkSummary([]).text).toBe('No outdated agent to upgrade.')
    const result = bulkSummary([
      { source_id: 1, name: 'a', result: 'accepted', detail: null },
      { source_id: 2, name: 'b', result: 'skipped', detail: "The agent isn't connected" },
    ])
    expect(result.text).toBe('1 agent upgrading, 1 not.')
    expect(result.problems.map((p) => p.source_id)).toEqual([2])
  })
})

describe('attempts', () => {
  it('knows which are under way', () => {
    expect(runActive(run({ status: 'requested' }))).toBe(true)
    expect(runActive(run({ status: 'restarting' }))).toBe(true)
    expect(runActive(run({ status: 'timeout' }))).toBe(false)
    expect(runActive(null)).toBe(false)
    expect(anyUpgrading([row(), row({ last_run: run({ status: 'installing' }) })])).toBe(true)
    expect(anyUpgrading([row({ upgrading: true })])).toBe(true)
    expect(anyUpgrading([row({ last_run: run({ status: 'succeeded' }) })])).toBe(false)
  })

  it('tones', () => {
    expect(statusTone('succeeded')).toBe('ok')
    expect(statusTone('accepted')).toBe('pending')
    expect(statusTone('too_old')).toBe('fail')
    expect(statusTone('timeout')).toBe('fail')
  })

  it('describes each state', () => {
    expect(runStatusText(run({ status: 'requested' }))).toBe('Asking the agent to upgrade to 0.7.0…')
    expect(runStatusText(run({ message: 'from GitHub' }))).toBe('Upgrading to 0.7.0: Downloading from GitHub…')
    expect(runStatusText(run({ status: 'succeeded' }))).toBe('Upgraded from 0.6.0 to 0.7.0.')
    expect(runStatusText(run({ status: 'timeout', message: "The agent didn't come back" }))).toBe(
      "The upgrade to 0.7.0 didn't work (didn't come back): The agent didn't come back.",
    )
    expect(runStatusText(run({ status: 'failed' }))).toContain('no reason given')
  })

  it('measures how long it took', () => {
    const now = new Date('2026-11-09T10:10:00Z')
    expect(runDuration(run({ finished_at: '2026-11-09T10:00:45' }), now)).toBe('45 s')
    expect(runDuration(run({ finished_at: '2026-11-09T10:03:05' }), now)).toBe('3 min 5 s')
    expect(runDuration(run({ finished_at: '2026-11-09T10:02:00' }), now)).toBe('2 min')
    expect(runDuration(run(), now)).toBe('10 min')
    expect(runDuration(run({ finished_at: '2026-11-09T11:30:00' }), now)).toBe('1 h 30 min')
  })

  it('says who asked', () => {
    expect(runRequester(run())).toBe('admin')
    expect(runRequester(run({ trigger: 'automatic', requested_by: null }))).toBe('Schedule')
    expect(runRequester(run({ requested_by: null }))).toBe('A deleted user')
  })

  it('words the answer to a request', () => {
    expect(upgradeResultText('accepted', null)).toBe('The agent accepted the upgrade.')
    expect(upgradeResultText('refused', 'The agent refused: no')).toBe('The agent refused: no')
    expect(upgradeResultText('busy', null)).toBe('An upgrade is already in progress.')
  })
})

describe('the window', () => {
  it('names the days', () => {
    expect(formatDays([0, 1, 2, 3, 4, 5, 6])).toBe('Every day')
    expect(formatDays([4, 0, 1, 2, 3])).toBe('Mon–Fri')
    expect(formatDays([5, 6])).toBe('Sat, Sun')
    expect(formatDays([0, 2, 5])).toBe('Mon, Wed, Sat')
    expect(formatDays([])).toBe('No day')
  })

  it('describes the window in the server time zone', () => {
    expect(
      windowText({ window_days: [4], window_start: '22:00', window_hours: 6, timezone: 'Europe/Lisbon' }),
    ).toBe('Fri, from 22:00 for 6 hours (Europe/Lisbon)')
    expect(windowText({ window_days: [0, 1, 2, 3, 4, 5, 6], window_start: '02:00', window_hours: 1, timezone: 'UTC' })).toBe(
      'Every day, from 02:00 for 1 hour (UTC)',
    )
  })

  it('takes start times as HH:MM', () => {
    expect(normalizeStart('2:00')).toBe('02:00')
    expect(normalizeStart(' 22:30 ')).toBe('22:30')
    expect(normalizeStart('24:00')).toBeNull()
    expect(normalizeStart('12:60')).toBeNull()
    expect(normalizeStart('1200')).toBeNull()
  })
})
