// The password policy (#245) in words, for the list shown next to password
// fields. The server is what enforces it (app/password_policy.py).
import type { PasswordPolicy } from './types'

export function describePolicy(policy: PasswordPolicy): string[] {
  const rules = [`${policy.min_length} to ${policy.max_length} characters`]
  if (policy.required_classes > 0) {
    rules.push(
      `at least ${policy.required_classes} of: lowercase, uppercase, digits, symbols`,
    )
  }
  if (policy.reject_simple) {
    rules.push('not too simple: no repeats, sequences, keyboard runs or common passwords (aaaa, 1234, qwerty, iloveyou)')
  }
  if (policy.reject_personal) {
    rules.push('nothing from the username, email or name')
  }
  // Always refused, whatever the other rules (app/password_policy.py); the
  // words themselves aren't public, they may name the organization.
  rules.push('nothing containing "perchtail" or the words your administrator listed')
  if (policy.history === 1) {
    rules.push('different from the current password')
  } else if (policy.history > 1) {
    rules.push(`different from the last ${policy.history} passwords`)
  }
  if (policy.max_age_days > 0) {
    rules.push(`expires after ${policy.max_age_days} days`)
  }
  return rules
}
