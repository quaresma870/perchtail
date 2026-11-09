import { describe, expect, it } from 'vitest'
import {
  upgradableAgents,
  upgradeBlocker,
  upgradeInProgress,
  upgradeResultText,
  upgradeStatusText,
} from './agent-upgrade'
import type { AgentUpgrade } from './types'

const agent = {
  protocol: 'agent' as const,
  enabled: true,
  is_system: false,
  agent_connected: true,
  agent_outdated: true,
  agent_upgrade: null as AgentUpgrade | null,
}

const upgrade = (fields: Partial<AgentUpgrade>): AgentUpgrade => ({
  status: 'running',
  stage: null,
  detail: null,
  from_version: '0.6.0',
  to_version: '0.7.0',
  started_at: '2026-11-09T10:00:00',
  finished_at: null,
  ...fields,
})

describe('upgradeBlocker', () => {
  it('offers the upgrade for a connected, outdated agent when the setting is on', () => {
    expect(upgradeBlocker(agent, true)).toBeNull()
  })

  it('says why it is not offered', () => {
    expect(upgradeBlocker(agent, false)).toContain('Settings → System')
    expect(upgradeBlocker({ ...agent, protocol: 'ssh' }, true)).toBe('Not a push-agent source')
    expect(upgradeBlocker({ ...agent, enabled: false }, true)).toBe('The source is disabled')
    expect(upgradeBlocker({ ...agent, agent_connected: false }, true)).toBe("The agent isn't connected")
    expect(upgradeBlocker({ ...agent, agent_outdated: false }, true)).toBe('The agent is up to date')
    expect(upgradeBlocker({ ...agent, agent_upgrade: upgrade({ status: 'restarting' }) }, true)).toBe(
      'An upgrade is in progress',
    )
  })

  it('offers it again after an upgrade ended', () => {
    expect(upgradeBlocker({ ...agent, agent_upgrade: upgrade({ status: 'failed' }) }, true)).toBeNull()
  })
})

describe('upgradableAgents', () => {
  it('keeps only the agents the bulk action would upgrade', () => {
    const sources = [
      { ...agent, id: 1 },
      { ...agent, id: 2, agent_outdated: false },
      { ...agent, id: 3, agent_connected: false },
      { ...agent, id: 4, protocol: 'smb' as const },
    ]
    expect(upgradableAgents(sources, true).map((s) => s.id)).toEqual([1])
    expect(upgradableAgents(sources, false)).toEqual([])
  })
})

describe('upgradeStatusText', () => {
  it('describes each state', () => {
    expect(upgradeInProgress(upgrade({ status: 'requested' }))).toBe(true)
    expect(upgradeInProgress(upgrade({ status: 'succeeded' }))).toBe(false)
    expect(upgradeInProgress(null)).toBe(false)
    expect(upgradeStatusText(upgrade({ status: 'requested' }))).toBe('Asking the agent to upgrade to 0.7.0…')
    expect(upgradeStatusText(upgrade({ stage: 'downloading', detail: 'from GitHub' }))).toBe(
      'Upgrading to 0.7.0: Downloading from GitHub…',
    )
    expect(upgradeStatusText(upgrade({ status: 'restarting', stage: 'restarting' }))).toBe(
      'Upgrading to 0.7.0: Restarting with the new version…',
    )
    expect(upgradeStatusText(upgrade({ status: 'succeeded' }))).toBe('Upgraded from 0.6.0 to 0.7.0.')
    expect(upgradeStatusText(upgrade({ status: 'failed', detail: 'bad signature' }))).toBe(
      'The upgrade to 0.7.0 failed: bad signature. The agent keeps running its current version.',
    )
  })

  it('words the answer to a request', () => {
    expect(upgradeResultText('accepted', null)).toBe('The agent accepted the upgrade.')
    expect(upgradeResultText('too_old', 'This agent is too old')).toBe('This agent is too old')
    expect(upgradeResultText('failed', null)).toBe('The upgrade could not start.')
  })
})
