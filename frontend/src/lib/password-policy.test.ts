import { describe, expect, it } from 'vitest'
import { describePolicy } from './password-policy'
import type { PasswordPolicy } from './types'

const DEFAULT: PasswordPolicy = {
  min_length: 12,
  max_length: 128,
  reject_simple: true,
  reject_personal: true,
  history: 0,
  required_classes: 0,
  max_age_days: 0,
}

describe('describePolicy', () => {
  it('lists the default rules', () => {
    expect(describePolicy(DEFAULT)).toEqual([
      '12 to 128 characters',
      'not too simple: no repeats, sequences or keyboard runs (aaaa, 1234, qwerty)',
      'nothing from the username, email or name',
    ])
  })

  it('adds the optional rules when they are on', () => {
    const rules = describePolicy({
      ...DEFAULT,
      reject_simple: false,
      reject_personal: false,
      history: 5,
      required_classes: 3,
      max_age_days: 90,
    })
    expect(rules).toEqual([
      '12 to 128 characters',
      'at least 3 of: lowercase, uppercase, digits, symbols',
      'different from the last 5 passwords',
      'expires after 90 days',
    ])
  })

  it('says "current password" for a history of one', () => {
    expect(describePolicy({ ...DEFAULT, history: 1 })).toContain(
      'different from the current password',
    )
  })
})
