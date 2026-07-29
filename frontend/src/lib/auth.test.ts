import { beforeEach, describe, expect, it, vi } from 'vitest'
import { get } from 'svelte/store'
import { ApiError } from './api'
import type { CurrentUser } from './types'

vi.mock('./api', async () => {
  const actual = await vi.importActual<typeof import('./api')>('./api')
  return { ...actual, api: { get: vi.fn(), post: vi.fn() } }
})

import { api } from './api'
import { authChecked, currentUser, hasCapability, login, logout, refreshCurrentUser } from './auth'

function user(overrides: Partial<CurrentUser> = {}): CurrentUser {
  return {
    id: 1,
    username: 'alice',
    role_id: 2,
    active: true,
    must_change_password: false,
    is_super_admin: false,
    global_capabilities: [],
    ...overrides,
  }
}

describe('hasCapability', () => {
  it('denies when there is no user', () => {
    expect(hasCapability(null, 'manage_users')).toBe(false)
  })

  it('grants a super-admin every capability regardless of their list', () => {
    expect(hasCapability(user({ is_super_admin: true, global_capabilities: [] }), 'manage_sso')).toBe(
      true,
    )
  })

  it('grants a capability present in global_capabilities', () => {
    expect(hasCapability(user({ global_capabilities: ['manage_users'] }), 'manage_users')).toBe(true)
  })

  it('denies a capability absent from global_capabilities', () => {
    expect(hasCapability(user({ global_capabilities: ['manage_users'] }), 'manage_roles')).toBe(false)
  })
})

describe('login/logout/refreshCurrentUser', () => {
  beforeEach(() => {
    vi.mocked(api.get).mockReset()
    vi.mocked(api.post).mockReset()
    currentUser.set(null)
    authChecked.set(false)
  })

  it('login sets currentUser on success', async () => {
    const u = user()
    vi.mocked(api.post).mockResolvedValue(u)
    const result = await login('alice', 'hunter2')
    expect(result).toEqual(u)
    expect(get(currentUser)).toEqual(u)
  })

  it('logout clears currentUser', async () => {
    currentUser.set(user())
    vi.mocked(api.post).mockResolvedValue(undefined)
    await logout()
    expect(get(currentUser)).toBeNull()
  })

  it('refreshCurrentUser sets currentUser and marks authChecked on success', async () => {
    const u = user()
    vi.mocked(api.get).mockResolvedValue(u)
    const result = await refreshCurrentUser()
    expect(result).toEqual(u)
    expect(get(currentUser)).toEqual(u)
    expect(get(authChecked)).toBe(true)
  })

  it('refreshCurrentUser treats a 401 as "not logged in", not an error', async () => {
    vi.mocked(api.get).mockRejectedValue(new ApiError(401, 'Unauthorized'))
    const result = await refreshCurrentUser()
    expect(result).toBeNull()
    expect(get(currentUser)).toBeNull()
    expect(get(authChecked)).toBe(true)
  })

  it('refreshCurrentUser rethrows non-401 errors but still marks authChecked', async () => {
    vi.mocked(api.get).mockRejectedValue(new ApiError(500, 'boom'))
    await expect(refreshCurrentUser()).rejects.toMatchObject({ status: 500 })
    expect(get(authChecked)).toBe(true)
  })
})
