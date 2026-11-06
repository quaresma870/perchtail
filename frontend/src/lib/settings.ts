import { writable } from 'svelte/store'
import { api } from './api'
import type { SystemSettings } from './types'

// Readable by any authenticated user (see backend/app/api/system_settings.py)
// -- needed to decide whether to render the Search nav link at all, for
// everyone, not just whoever can change it. Defaults keep every feature on
// until the real value loads, so nothing flashes hidden then visible.
export const systemSettings = writable<SystemSettings>({
  search_view_enabled: true,
  audit_view_enabled: true,
  audit_retention_days: 365,
  source_max_roots: 20,
  connections_view_switchable: true,
  connections_view_default: 'list',
  update_check_enabled: true,
})

let loadedAt = 0

export async function refreshSystemSettings(): Promise<void> {
  try {
    const settings = await api.get<SystemSettings>('/system-settings')
    systemSettings.set(settings)
    loadedAt = Date.now()
  } catch {
    // Non-fatal -- keep the optimistic default rather than blocking the app
    // on a settings fetch failing.
  }
}

// Settings an admin changes reach users who already have the app open on
// their next navigation or when they come back to the tab, instead of only
// after a reload (#196). At most one request per `maxAgeMs`.
export function refreshSystemSettingsIfStale(maxAgeMs = 30_000): Promise<void> | undefined {
  if (Date.now() - loadedAt < maxAgeMs) return undefined
  loadedAt = Date.now()
  return refreshSystemSettings()
}
