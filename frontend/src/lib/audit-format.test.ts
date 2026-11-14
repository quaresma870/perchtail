import { describe, expect, it } from 'vitest'
import { detailPairs, formatAuditTime, targetHref, targetTypeLabel } from './audit-format'
import type { AuditLogEntry } from './types'

const entry = (overrides: Partial<AuditLogEntry> = {}): AuditLogEntry => ({
  id: 1,
  user_id: 1,
  username: 'admin',
  action: 'source.open',
  target_type: 'source',
  target_id: 12,
  timestamp: '2026-11-05T10:00:00Z',
  metadata: null,
  client_ip: '198.51.100.7',
  user_agent: 'Mozilla/5.0',
  session_id: '0123456789abcdef',
  target_name: 'app01',
  target_context: 'Vodacom / prod',
  target_name_is_current: false,
  target_exists: true,
  ...overrides,
})

describe('targetTypeLabel', () => {
  it('names known types and passes unknown ones through', () => {
    expect(targetTypeLabel('role_grant')).toBe('Grant')
    expect(targetTypeLabel('something_new')).toBe('something_new')
  })
})

describe('targetHref', () => {
  it('links to an existing source or role', () => {
    expect(targetHref(entry())).toBe('#/settings/sources/12')
    expect(targetHref(entry({ target_type: 'role', target_id: 3 }))).toBe('#/settings/roles/3')
  })

  it("doesn't link to a target that's gone", () => {
    expect(targetHref(entry({ target_exists: false }))).toBeNull()
  })

  it("doesn't link types without a page", () => {
    expect(targetHref(entry({ target_type: 'rule' }))).toBeNull()
  })
})

describe('formatAuditTime', () => {
  it('shows the time with its zone', () => {
    const text = formatAuditTime('2026-11-05T10:00:00Z', 'UTC')
    expect(text).toContain('10:00:00')
    expect(text).toContain('UTC')
  })

  it('converts to the zone shown', () => {
    // Tokyo is UTC+9, with no daylight saving time.
    expect(formatAuditTime('2026-11-05T10:00:00Z', 'Asia/Tokyo')).toContain('19:00:00')
  })

  it('reads a time without a zone as UTC, never as local time', () => {
    expect(formatAuditTime('2026-11-05T10:00:00', 'Asia/Tokyo')).toContain('19:00:00')
  })
})

describe('detailPairs', () => {
  it('lists metadata as pairs, leaving out what the target column shows', () => {
    expect(
      detailPairs({
        target_name: 'app01',
        target_context: 'Vodacom',
        path: 'app/app.log',
        file_count: 2,
        changes: { host: ['a', 'b'] },
      }),
    ).toEqual([
      ['path', 'app/app.log'],
      ['file_count', '2'],
      ['changes', '{"host":["a","b"]}'],
    ])
  })

  it('is empty without metadata', () => {
    expect(detailPairs(null)).toEqual([])
  })
})
