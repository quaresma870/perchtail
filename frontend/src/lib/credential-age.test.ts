import { describe, expect, it } from 'vitest'
import {
  credentialAgeDays,
  credentialAgeLabel,
  credentialSortKey,
  formatAgeDays,
  isCredentialStale,
} from './credential-age'

const now = new Date('2026-11-08T12:00:00Z')
const set = (iso: string | null, has = true) => ({
  has_credential: has,
  credential_updated_at: iso,
})

describe('credential age (#248)', () => {
  it('counts whole days, reading the API date as UTC', () => {
    expect(credentialAgeDays(set('2026-10-27T12:00:00'), now)).toBe(12)
    expect(credentialAgeDays(set('2026-11-08T11:00:00Z'), now)).toBe(0)
    expect(credentialAgeDays(set(null), now)).toBeNull()
    expect(credentialAgeDays(set('2026-10-27T12:00:00', false), now)).toBeNull()
  })

  it('formats days, then months, then years', () => {
    expect(formatAgeDays(0)).toBe('today')
    expect(formatAgeDays(1)).toBe('1 day')
    expect(formatAgeDays(12)).toBe('12 days')
    expect(formatAgeDays(91)).toBe('3 months')
    expect(formatAgeDays(800)).toBe('2 years')
  })

  it('labels a missing credential and an unknown date', () => {
    expect(credentialAgeLabel(set('2026-10-27T12:00:00'), now)).toBe('12 days')
    expect(credentialAgeLabel(set(null), now)).toBe('unknown')
    expect(credentialAgeLabel(set(null, false), now)).toBe('—')
  })

  it('is stale only past a limit that is on, with a known date', () => {
    const old = set('2026-07-01T00:00:00')
    expect(isCredentialStale(old, 90, now)).toBe(true)
    expect(isCredentialStale(old, 200, now)).toBe(false)
    expect(isCredentialStale(old, 0, now)).toBe(false)
    expect(isCredentialStale(set(null), 1, now)).toBe(false)
  })

  it('sorts an unknown date as the oldest and no credential last', () => {
    expect(credentialSortKey(set('2026-10-27T12:00:00'), now)).toBe(12)
    expect(credentialSortKey(set(null), now)).toBe(Number.MAX_SAFE_INTEGER)
    expect(credentialSortKey(set(null, false), now)).toBeNull()
  })
})
