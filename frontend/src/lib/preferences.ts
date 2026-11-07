// Per-user display preferences (#273), stored on the server so they follow
// the user; anything not chosen follows the System default.
import { derived, get } from 'svelte/store'
import { api } from './api'
import { currentUser } from './auth'
import { systemSettings } from './settings'
import type { CurrentUser, ExplorerSort } from './types'
import { EXPLORER_SORTS } from './explorer-sort'

type PreferenceKey = 'explorer_sort' | 'connections_view'

// The order folders are listed in. The explorer has no sort control (#282):
// the System default decides, and a user's own `explorer_sort` (kept on the
// server) is ignored until it comes back as "Sort by" in the folder menu,
// so a choice made in 0.6.0-rc.1 can't get stuck with no way to change it.
export const explorerSort = derived(systemSettings, ($settings) => {
  const chosen = $settings.explorer_sort_default
  return (EXPLORER_SORTS.some((s) => s.value === chosen) ? chosen : 'name_asc') as ExplorerSort
})

export const connectionsViewPreference = derived(
  [currentUser, systemSettings],
  ([$user, $settings]) => {
    const chosen = $user?.preferences?.connections_view
    return chosen === 'list' || chosen === 'tree' ? chosen : $settings.connections_view_default
  },
)

/** Sets (or, with null, clears) one preference. Applied at once; if the
 * server refuses, the previous value comes back. */
export async function savePreference(key: PreferenceKey, value: string | null): Promise<void> {
  const before = get(currentUser)
  if (!before) return
  const next = { ...before.preferences }
  if (value === null) delete next[key]
  else next[key] = value
  currentUser.set({ ...before, preferences: next })
  try {
    currentUser.set(await api.patch<CurrentUser>('/auth/preferences', { [key]: value }))
  } catch {
    // Back to what the server has (another save may have landed since).
    try {
      currentUser.set(await api.get<CurrentUser>('/auth/me'))
    } catch {
      currentUser.set(before)
    }
  }
}

// Before 0.6 the list/tree choice lived in this browser only (#156).
const LEGACY_VIEW_KEY = 'perchtail.connectionsView'

/** Moves a list/tree choice this browser remembered to the server, once,
 * unless the user already has one there. */
export async function migrateLegacyViewChoice(): Promise<void> {
  const user = get(currentUser)
  if (!user) return // not signed in yet: try again next time
  let stored: string | null = null
  try {
    stored = localStorage.getItem(LEGACY_VIEW_KEY)
    localStorage.removeItem(LEGACY_VIEW_KEY)
  } catch {
    return
  }
  if ((stored === 'list' || stored === 'tree') && !user.preferences?.connections_view) {
    await savePreference('connections_view', stored)
  }
}
