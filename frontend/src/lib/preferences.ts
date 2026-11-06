// Per-user display preferences (#273), stored on the server so they follow
// the user; anything not chosen follows the System default.
import { derived, get } from 'svelte/store'
import { api } from './api'
import { currentUser } from './auth'
import { systemSettings } from './settings'
import type { CurrentUser, ExplorerSort } from './types'
import { EXPLORER_SORTS } from './explorer-sort'

type PreferenceKey = 'explorer_sort' | 'connections_view'

export const explorerSort = derived([currentUser, systemSettings], ([$user, $settings]) => {
  const chosen = $user?.preferences?.explorer_sort
  return (EXPLORER_SORTS.some((s) => s.value === chosen) ? chosen : $settings.explorer_sort_default) as ExplorerSort
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
    currentUser.set(before)
  }
}

// Before 0.6 the list/tree choice lived in this browser only (#156).
const LEGACY_VIEW_KEY = 'perchtail.connectionsView'

/** Moves a list/tree choice this browser remembered to the server, once,
 * unless the user already has one there. */
export async function migrateLegacyViewChoice(): Promise<void> {
  let stored: string | null = null
  try {
    stored = localStorage.getItem(LEGACY_VIEW_KEY)
    localStorage.removeItem(LEGACY_VIEW_KEY)
  } catch {
    return
  }
  const user = get(currentUser)
  if ((stored === 'list' || stored === 'tree') && user && !user.preferences?.connections_view) {
    await savePreference('connections_view', stored)
  }
}
