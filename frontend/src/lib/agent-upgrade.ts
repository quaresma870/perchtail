import type { AgentUpgrade, AgentUpgradeResult, Source } from './types'

/** Push-agent upgrades from the portal (#319): what the UI shows and offers.
 * The server checks everything again; this only decides what to show. */

type UpgradeFields = Pick<
  Source,
  'protocol' | 'enabled' | 'is_system' | 'agent_connected' | 'agent_outdated' | 'agent_upgrade'
>

/** An upgrade the agent is still working on. */
export function upgradeInProgress(upgrade: AgentUpgrade | null | undefined): boolean {
  return upgrade?.status === 'requested' || upgrade?.status === 'running' || upgrade?.status === 'restarting'
}

/** Why the source's agent can't be upgraded now, or null when it can. */
export function upgradeBlocker(source: UpgradeFields, enabled: boolean): string | null {
  if (source.protocol !== 'agent' || source.is_system) return 'Not a push-agent source'
  if (!enabled) return 'Upgrading agents from the portal is off (Settings → System)'
  if (!source.enabled) return 'The source is disabled'
  if (!source.agent_connected) return "The agent isn't connected"
  if (!source.agent_outdated) return 'The agent is up to date'
  if (upgradeInProgress(source.agent_upgrade)) return 'An upgrade is in progress'
  return null
}

/** The sources whose agents the bulk action would upgrade. */
export function upgradableAgents<T extends UpgradeFields>(sources: T[], enabled: boolean): T[] {
  return sources.filter((s) => upgradeBlocker(s, enabled) === null)
}

const STAGES: Record<string, string> = {
  downloading: 'Downloading',
  installing: 'Installing',
  restarting: 'Restarting with the new version',
}

/** One line describing an upgrade's state, for the source page. */
export function upgradeStatusText(upgrade: AgentUpgrade): string {
  const target = upgrade.to_version
  switch (upgrade.status) {
    case 'requested':
      return `Asking the agent to upgrade to ${target}…`
    case 'running':
    case 'restarting': {
      const stage = upgrade.stage ? (STAGES[upgrade.stage] ?? upgrade.stage) : 'Upgrade accepted'
      const detail = upgrade.detail ? ` ${upgrade.detail}` : ''
      return `Upgrading to ${target}: ${stage}${detail}…`
    }
    case 'succeeded':
      return `Upgraded from ${upgrade.from_version ?? 'an unknown version'} to ${target}.`
    case 'failed':
      return `The upgrade to ${target} failed: ${upgrade.detail ?? 'no reason given'}. The agent keeps running its current version.`
  }
}

/** The answer to an upgrade request, for a notice. */
export function upgradeResultText(result: AgentUpgradeResult, detail: string | null): string {
  switch (result) {
    case 'accepted':
      return 'The agent accepted the upgrade.'
    case 'busy':
      return detail ?? 'An upgrade is already in progress.'
    default:
      return detail ?? 'The upgrade could not start.'
  }
}
