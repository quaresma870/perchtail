import { writable } from 'svelte/store'
import { api } from './api'
import type { SystemSettings } from './types'

// Readable by any authenticated user (see backend/app/api/system_settings.py)
// -- needed to decide whether to render the Search nav link at all, for
// everyone, not just whoever can change it. Defaults keep every feature on
// until the real value loads, so nothing flashes hidden then visible.
export const systemSettings = writable<SystemSettings>({ search_view_enabled: true })

export async function refreshSystemSettings(): Promise<void> {
  try {
    const settings = await api.get<SystemSettings>('/system-settings')
    systemSettings.set(settings)
  } catch {
    // Non-fatal -- keep the optimistic default rather than blocking the app
    // on a settings fetch failing.
  }
}
