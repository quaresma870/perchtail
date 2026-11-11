import { parseUtc } from './credential-age'
import type {
  AgentRow,
  AgentUpgradeOverview,
  AgentUpgradePolicy,
  AgentUpgradeResult,
  AgentUpgradeRun,
  AgentUpgradeStatus,
  AgentUpgradeSystemPolicy,
  BulkAgentUpgradeResult,
} from './types'

/** Push-agent upgrades from the portal (#319), Settings → Agents: what the
 * page shows and offers. The server checks everything again; this only
 * decides what to show. */

export const DAY_NAMES = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

export const SYSTEM_POLICY_LABELS: Record<AgentUpgradeSystemPolicy, string> = {
  off: 'Off',
  manual: 'Manual',
  automatic: 'Automatic',
}

/** The labels of a source's own policy; "inherit" names what it follows. */
export function sourcePolicyLabel(policy: AgentUpgradePolicy, system: AgentUpgradeSystemPolicy): string {
  if (policy === 'inherit') return `Follow the system (${SYSTEM_POLICY_LABELS[system]})`
  return SYSTEM_POLICY_LABELS[policy]
}

/** A source's own policy, or the system's when it follows it. */
export function effectivePolicy(
  policy: AgentUpgradePolicy,
  system: AgentUpgradeSystemPolicy,
): AgentUpgradeSystemPolicy {
  return policy === 'inherit' ? system : policy
}

const ACTIVE: AgentUpgradeStatus[] = ['requested', 'accepted', 'downloading', 'installing', 'restarting']

/** An attempt the agent is still working on. */
export function runActive(run: AgentUpgradeRun | null | undefined): boolean {
  return !!run && ACTIVE.includes(run.status)
}

/** Whether the page should keep polling: something is under way. */
export function anyUpgrading(rows: AgentRow[]): boolean {
  return rows.some((row) => row.upgrading || runActive(row.last_run))
}

/** The agents "Upgrade all outdated agents" asks: those that could be
 * upgraded now, except suspended ones (and, through `blocker`, those whose
 * policy is off). */
export function bulkTargets(rows: AgentRow[]): AgentRow[] {
  return rows.filter((row) => row.blocker === null && !row.suspended)
}

const STATUS_LABELS: Record<AgentUpgradeStatus, string> = {
  requested: 'Asking the agent',
  accepted: 'Accepted',
  downloading: 'Downloading',
  installing: 'Installing',
  restarting: 'Restarting',
  succeeded: 'Upgraded',
  failed: 'Failed',
  refused: 'Refused',
  too_old: 'Too old to upgrade remotely',
  timeout: "Didn't come back",
}

export function statusLabel(status: AgentUpgradeStatus): string {
  return STATUS_LABELS[status]
}

export type StatusTone = 'ok' | 'fail' | 'pending'

export function statusTone(status: AgentUpgradeStatus): StatusTone {
  if (status === 'succeeded') return 'ok'
  if (ACTIVE.includes(status)) return 'pending'
  return 'fail'
}

/** One line describing an attempt. */
export function runStatusText(run: AgentUpgradeRun): string {
  const target = run.to_version
  switch (run.status) {
    case 'requested':
      return `Asking the agent to upgrade to ${target}…`
    case 'accepted':
    case 'downloading':
    case 'installing':
    case 'restarting': {
      const detail = run.message ? ` ${run.message}` : ''
      return `Upgrading to ${target}: ${STATUS_LABELS[run.status]}${detail}…`
    }
    case 'succeeded':
      return `Upgraded from ${run.from_version ?? 'an unknown version'} to ${target}.`
    default:
      return `The upgrade to ${target} didn't work (${STATUS_LABELS[run.status].toLowerCase()}): ${run.message ?? 'no reason given'}.`
  }
}

/** How long an attempt took, or has been running: "45 s", "3 min 5 s". */
export function runDuration(run: AgentUpgradeRun, now = new Date()): string {
  const end = run.finished_at ? parseUtc(run.finished_at) : now
  const seconds = Math.max(0, Math.round((end.getTime() - parseUtc(run.started_at).getTime()) / 1000))
  if (seconds < 60) return `${seconds} s`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return seconds % 60 ? `${minutes} min ${seconds % 60} s` : `${minutes} min`
  return `${Math.floor(minutes / 60)} h ${minutes % 60} min`
}

/** Who asked for an attempt. */
export function runRequester(run: AgentUpgradeRun): string {
  if (run.trigger === 'automatic') return 'Schedule'
  return run.requested_by ?? 'A deleted user'
}

/** The days of the window: "Every day", "Mon–Fri", "Mon, Wed, Sat". */
export function formatDays(days: number[]): string {
  const sorted = [...new Set(days)].filter((d) => d >= 0 && d < 7).sort((a, b) => a - b)
  if (sorted.length === 7) return 'Every day'
  if (sorted.length === 0) return 'No day'
  const contiguous = sorted.length >= 3 && sorted.every((d, i) => i === 0 || d === sorted[i - 1] + 1)
  if (contiguous) return `${DAY_NAMES[sorted[0]]}–${DAY_NAMES[sorted[sorted.length - 1]]}`
  return sorted.map((d) => DAY_NAMES[d]).join(', ')
}

/** The window in one line, in the server's time zone. */
export function windowText(overview: Pick<AgentUpgradeOverview, 'window_days' | 'window_start' | 'window_hours' | 'timezone'>): string {
  const hours = overview.window_hours === 1 ? '1 hour' : `${overview.window_hours} hours`
  return `${formatDays(overview.window_days)}, from ${overview.window_start} for ${hours} (${overview.timezone})`
}

/** "HH:MM", as the server takes it; null when it isn't. */
export function normalizeStart(text: string): string | null {
  const match = /^(\d{1,2}):(\d{2})$/.exec(text.trim())
  if (!match) return null
  const hours = Number(match[1])
  const minutes = Number(match[2])
  if (hours > 23 || minutes > 59) return null
  return `${String(hours).padStart(2, '0')}:${match[2]}`
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

/** What "Upgrade all outdated agents" did, in one line, and the agents to
 * name because they weren't asked or refused. */
export function bulkSummary(results: BulkAgentUpgradeResult[]): {
  text: string
  problems: BulkAgentUpgradeResult[]
} {
  const accepted = results.filter((r) => r.result === 'accepted').length
  const problems = results.filter((r) => r.result !== 'accepted')
  const text =
    results.length === 0
      ? 'No outdated agent to upgrade.'
      : `${accepted} agent${accepted === 1 ? '' : 's'} upgrading${problems.length ? `, ${problems.length} not` : ''}.`
  return { text, problems }
}
