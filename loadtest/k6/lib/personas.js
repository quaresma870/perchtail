// Which kind of person a production session is, and as which user it signs
// in. No k6 module is imported, so node can test this file.
//
// PerchTail takes each TOTP step's code once per account, and every refused
// code counts toward the lockout of the client's address (all of k6's
// sign-ins come from one address). Virtual users don't share memory, so a
// user with MFA is never shared: virtual user n only takes a kind whose
// users need MFA (`mfa`, the admins) as that kind's n-th user, and a
// virtual user without one of its own draws among the other kinds.

/** The kinds this virtual user may be: those with users, without the
 * kinds with MFA unless it has a user of its own there. */
export function available(personas, vuId) {
  return Object.entries(personas).filter(([, p]) => p.users.length > 0 && (!p.mfa || p.users[vuId - 1] !== undefined))
}

/** A kind by weight among `available` (`roll` in [0, 1)), or null. */
export function drawPersona(personas, vuId, roll) {
  const entries = available(personas, vuId)
  if (!entries.length) return null
  const total = entries.reduce((n, [, p]) => n + p.weight, 0)
  let left = roll * total
  for (const [name, p] of entries) {
    left -= p.weight
    if (left < 0) return name
  }
  return entries[entries.length - 1][0]
}

/** The user a session of kind `p` signs in as: with MFA, only the virtual
 * user's own (`users[vuId - 1]`); otherwise one in turn. */
export function userFor(p, vuId, sessionCount) {
  if (p.mfa) return p.users[vuId - 1] || null
  return p.users[(vuId + sessionCount) % p.users.length]
}

/** The plan's kinds with `mfa` set also when the plan predates it: a kind
 * whose users have a TOTP secret in users-mfa.json. */
export function withMfa(personas, secrets) {
  const out = {}
  for (const [name, p] of Object.entries(personas)) {
    out[name] = { ...p, mfa: !!p.mfa || p.users.some((u) => !!secrets[u]) }
  }
  return out
}
