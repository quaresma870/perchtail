// What the dot before a source in the explorer shows (#276).
import type { Source } from './types'

export type StatusState = 'ok' | 'failing' | 'disconnected' | 'unknown'

export interface StatusView {
  state: StatusState
  tooltip: string
}

function ago(iso: string | null, now: Date): string {
  if (!iso) return ''
  // The API's datetimes are UTC without a zone.
  const when = new Date(/[zZ]|[+-]\d\d:\d\d$/.test(iso) ? iso : `${iso}Z`)
  const seconds = Math.max(0, Math.round((now.getTime() - when.getTime()) / 1000))
  if (seconds < 60) return 'just now'
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${minutes} min ago`
  const hours = Math.round(minutes / 60)
  if (hours < 48) return `${hours} h ago`
  return `${Math.round(hours / 24)} days ago`
}

/**
 * The shared status from the server, unless "Check now" just found
 * something newer; "unknown" while PerchTail itself doesn't answer, since
 * nothing below can be trusted then.
 */
export function sourceStatusView(
  source: Pick<Source, 'protocol' | 'status' | 'status_checked_at' | 'status_message'>,
  checked: { ok: boolean; detail: string; at: string } | null,
  serverReachable: boolean,
  now = new Date(),
): StatusView {
  if (!serverReachable) {
    return { state: 'unknown', tooltip: "Unknown: PerchTail isn't answering" }
  }
  if (checked) {
    return checked.ok
      ? { state: 'ok', tooltip: `Reachable (checked ${ago(checked.at, now)})` }
      : { state: 'failing', tooltip: `${checked.detail} (checked ${ago(checked.at, now)})` }
  }
  const when = ago(source.status_checked_at, now)
  if (source.protocol === 'agent') {
    return source.status === 'ok'
      ? { state: 'ok', tooltip: 'Agent connected' }
      : {
          state: 'disconnected',
          tooltip: `Agent not connected${when ? ` (last seen ${when})` : ''}`,
        }
  }
  if (source.status === 'ok') return { state: 'ok', tooltip: `Reachable (${when})` }
  if (source.status === 'failing') {
    return { state: 'failing', tooltip: `${source.status_message || 'Not reachable'} (${when})` }
  }
  return { state: 'unknown', tooltip: 'Not checked yet' }
}
