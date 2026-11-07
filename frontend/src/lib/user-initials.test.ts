import { describe, expect, it } from 'vitest'
import { userInitials } from './user-initials'

describe('userInitials', () => {
  it('takes the first letters of the first two words', () => {
    expect(userInitials('Ana Maria Costa')).toBe('AM')
    expect(userInitials('joao.silva')).toBe('JS')
    expect(userInitials('e2e-admin')).toBe('EA')
  })

  it('takes two letters of a single word', () => {
    expect(userInitials('admin')).toBe('AD')
    expect(userInitials('x')).toBe('X')
  })

  it('handles accents, symbols and empty names', () => {
    expect(userInitials('Élia Órfão')).toBe('ÉÓ')
    expect(userInitials('  ')).toBe('?')
    expect(userInitials('---')).toBe('?')
  })
})
