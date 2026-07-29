import { writable } from 'svelte/store'
import { api, ApiError } from './api'
import type { CurrentUser, GlobalCapability } from './types'

export const currentUser = writable<CurrentUser | null>(null)
export const authChecked = writable(false)

export async function refreshCurrentUser(): Promise<CurrentUser | null> {
  try {
    const user = await api.get<CurrentUser>('/auth/me')
    currentUser.set(user)
    return user
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) {
      currentUser.set(null)
      return null
    }
    throw err
  } finally {
    authChecked.set(true)
  }
}

export async function login(username: string, password: string): Promise<CurrentUser> {
  const user = await api.post<CurrentUser>('/auth/login', { username, password })
  currentUser.set(user)
  return user
}

export async function logout(): Promise<void> {
  await api.post('/auth/logout')
  currentUser.set(null)
}

export function hasCapability(user: CurrentUser | null, capability: GlobalCapability): boolean {
  if (!user) return false
  return user.is_super_admin || user.global_capabilities.includes(capability)
}
