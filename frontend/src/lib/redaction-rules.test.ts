import { describe, expect, it } from 'vitest'
import {
  DETECTOR_INFO,
  agentRedactionWarning,
  builtinRules,
  customRules,
  draftError,
  draftFromRule,
  draftPayload,
  emptyDraft,
  inheritedRules,
  matchSegments,
  moveRule,
} from './redaction-rules'
import type { RedactionRule } from './types'

function rule(overrides: Partial<RedactionRule>): RedactionRule {
  return {
    id: 1,
    source_id: null,
    name: 'r',
    detector: null,
    pattern: 'x+',
    secret_group: null,
    max_length: 64,
    enabled: true,
    order: 0,
    notes: null,
    ...overrides,
  }
}

describe('DETECTOR_INFO', () => {
  it('describes the eleven built-in detectors', () => {
    expect(Object.keys(DETECTOR_INFO).sort()).toEqual(
      [
        'authorization',
        'bearer',
        'card',
        'email',
        'iban',
        'ipv4',
        'ipv6',
        'jwt',
        'private_key',
        'secret_kv',
        'url_password',
      ].sort(),
    )
    for (const info of Object.values(DETECTOR_INFO)) {
      expect(info.description).not.toBe('')
      expect(info.falsePositives).not.toBe('')
    }
  })
})

describe('builtinRules / customRules', () => {
  it('splits the system rules', () => {
    const rules = [rule({ id: 1, detector: 'email', pattern: null }), rule({ id: 2 })]
    expect(builtinRules(rules).map((r) => r.id)).toEqual([1])
    expect(customRules(rules).map((r) => r.id)).toEqual([2])
  })
})

describe('inheritedRules', () => {
  it('says which system rules run on the source and which it turns off', () => {
    const system = [
      rule({ id: 1, detector: 'email', enabled: true }),
      rule({ id: 2, detector: 'ipv4', enabled: true }),
      rule({ id: 3, detector: 'iban', enabled: false }),
    ]
    const effective = [system[0], rule({ id: 9, source_id: 5 })]
    expect(inheritedRules(system, effective).map(({ rule: r, appliesHere, turnedOffHere }) => [r.id, appliesHere, turnedOffHere])).toEqual([
      [1, true, false],
      [2, false, true],
      // Off system-wide: it runs nowhere, and isn't "turned off here".
      [3, false, false],
    ])
  })
})

describe('agentRedactionWarning', () => {
  it('warns about a connected agent without redaction/1', () => {
    expect(agentRedactionWarning({ protocol: 'agent', agent_version: '0.7.0', agent_features: ['links/1'] })).toContain(
      "can't redact on its host",
    )
  })

  it('says an agent without links/1 must be updated before its files can be read', () => {
    const warning = agentRedactionWarning({ protocol: 'agent', agent_version: '0.6.3', agent_features: [] })
    expect(warning).toContain('must be updated before its files can be read')
    expect(warning).not.toContain('PerchTail redacts what it sends')
    expect(warning).toContain('Settings → Agents')
  })

  it('says nothing for an agent that redacts, one never seen, or another protocol', () => {
    expect(agentRedactionWarning({ protocol: 'agent', agent_version: '0.7.0', agent_features: ['redaction/1', 'links/1'] })).toBeNull()
    expect(agentRedactionWarning({ protocol: 'agent', agent_version: null, agent_features: [] })).toBeNull()
    expect(agentRedactionWarning({ protocol: 'ssh', agent_version: null, agent_features: [] })).toBeNull()
  })
})

describe('draftError / draftPayload', () => {
  const valid = { ...emptyDraft(), name: 'order id', pattern: 'ORD-\\d{8}', maxLength: '12' }

  it('accepts a valid draft and builds its payload', () => {
    expect(draftError(valid)).toBeNull()
    expect(draftPayload({ ...valid, secretGroup: ' ', notes: '  ' })).toEqual({
      name: 'order id',
      pattern: 'ORD-\\d{8}',
      max_length: 12,
      secret_group: null,
      notes: null,
    })
    expect(draftPayload({ ...valid, secretGroup: '1', notes: ' n ' })).toMatchObject({ secret_group: 1, notes: 'n' })
  })

  it('refuses what the server would', () => {
    expect(draftError({ ...valid, name: ' ' })).toMatch(/name/)
    expect(draftError({ ...valid, name: 'x'.repeat(49) })).toMatch(/48/)
    expect(draftError({ ...valid, name: 'a [b]' })).toMatch(/\[/)
    expect(draftError({ ...valid, pattern: '' })).toMatch(/pattern/)
    expect(draftError({ ...valid, maxLength: '0' })).toMatch(/4096/)
    expect(draftError({ ...valid, maxLength: '4097' })).toMatch(/4096/)
    expect(draftError({ ...valid, maxLength: '1.5' })).toMatch(/4096/)
    expect(draftError({ ...valid, secretGroup: '-1' })).toMatch(/Group/)
  })

  it('takes what bound number inputs give (numbers, null when empty)', () => {
    const fromInputs = { ...valid, maxLength: 12, secretGroup: null }
    expect(draftError(fromInputs)).toBeNull()
    expect(draftPayload(fromInputs)).toMatchObject({ max_length: 12, secret_group: null })
    expect(draftPayload({ ...fromInputs, secretGroup: 0 })).toMatchObject({ secret_group: 0 })
    expect(draftError({ ...valid, maxLength: null })).toMatch(/4096/)
  })

  it('round-trips a saved rule', () => {
    const saved = rule({ name: 'n', pattern: 'p', max_length: 9, secret_group: 2, notes: 'x' })
    expect(draftPayload(draftFromRule(saved))).toEqual({
      name: 'n',
      pattern: 'p',
      max_length: 9,
      secret_group: 2,
      notes: 'x',
    })
  })
})

describe('matchSegments', () => {
  it('marks the matched parts of the sample', () => {
    expect(matchSegments('id=ORD-1 and ORD-2', [{ start: 13, end: 18 }, { start: 3, end: 8 }])).toEqual([
      { text: 'id=', match: false },
      { text: 'ORD-1', match: true },
      { text: ' and ', match: false },
      { text: 'ORD-2', match: true },
    ])
  })

  it('clips overlapping and out-of-range matches', () => {
    expect(matchSegments('abcdef', [{ start: 0, end: 3 }, { start: 2, end: 4 }, { start: 5, end: 99 }])).toEqual([
      { text: 'abc', match: true },
      { text: 'd', match: true },
      { text: 'e', match: false },
      { text: 'f', match: true },
    ])
    expect(matchSegments('abc', [])).toEqual([{ text: 'abc', match: false }])
  })
})

describe('moveRule', () => {
  const rules = [rule({ id: 1, order: 11 }), rule({ id: 2, order: 12 }), rule({ id: 3, order: 12 })]

  it('swaps the order of two neighbours', () => {
    expect(moveRule(rules, 1, -1)).toEqual([
      { id: 2, order: 11 },
      { id: 1, order: 12 },
    ])
  })

  it('moves past a neighbour with the same order', () => {
    expect(moveRule(rules, 1, 1)).toEqual([{ id: 2, order: 13 }])
    expect(moveRule(rules, 2, -1)).toEqual([{ id: 3, order: 11 }])
  })

  it('does nothing at an end', () => {
    expect(moveRule(rules, 0, -1)).toEqual([])
    expect(moveRule(rules, 2, 1)).toEqual([])
  })
})
