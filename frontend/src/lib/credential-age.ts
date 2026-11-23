// How old a source's credential is (#248), for the Sources list.
import type { Source } from './types'

const DAY_MS = 24 * 60 * 60 * 1000

type CredentialFields = Pick<Source, 'has_credential' | 'credential_updated_at'>

// The API's datetimes are UTC without a zone.
export function parseUtc(iso: string): Date {
  return new Date(/[zZ]|[+-]\d\d:\d\d$/.test(iso) ? iso : `${iso}Z`)
}

/** Whole days since the credential was set; null when there is none or
 * it was set before PerchTail kept the date. */
export function credentialAgeDays(source: CredentialFields, now = new Date()): number | null {
  if (!source.has_credential || !source.credential_updated_at) return null
  const ms = now.getTime() - parseUtc(source.credential_updated_at).getTime()
  return Math.max(0, Math.floor(ms / DAY_MS))
}

export function formatAgeDays(days: number): string {
  if (days < 1) return 'today'
  if (days < 60) return days === 1 ? '1 day' : `${days} days`
  if (days < 730) return `${Math.round(days / 30.44)} months`
  return `${Math.round(days / 365.25)} years`
}

export function credentialAgeLabel(source: CredentialFields, now = new Date()): string {
  if (!source.has_credential) return '—'
  const days = credentialAgeDays(source, now)
  return days === null ? 'unknown' : formatAgeDays(days)
}

/** Older than the limit in Settings -> System; never when the limit is 0
 * (off) or the date is unknown. */
export function isCredentialStale(
  source: CredentialFields,
  maxAgeDays: number,
  now = new Date(),
): boolean {
  if (maxAgeDays <= 0) return false
  const days = credentialAgeDays(source, now)
  return days !== null && days > maxAgeDays
}

/** Sort key: newest first ascending; an unknown date sorts as the oldest,
 * since it predates the date being kept; no credential sorts last. */
export function credentialSortKey(source: CredentialFields, now = new Date()): number | null {
  if (!source.has_credential) return null
  return credentialAgeDays(source, now) ?? Number.MAX_SAFE_INTEGER
}
