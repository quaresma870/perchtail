// Unit tests of how the k6 code classes failed requests (k6/lib/failures.js)
// and chooses a production session's kind and user (k6/lib/personas.js).
// Run with:
//   node --test loadtest/tests/failures.test.mjs
// (test_growing.py runs it too when node is on the PATH).
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { abortKind, errorFields, k6Error, logLine, MAX_LOGGED_PER_VU, noAnswerKind } from '../k6/lib/failures.js'
import { available, drawPersona, userFor, withMfa } from '../k6/lib/personas.js'

test("only k6's timeout is a timeout, no answer otherwise is a failure, a cut 2xx body is an integrity error", () => {
  assert.equal(abortKind({ status: 0, error: 'request timeout', error_code: 1050 }), 'timeout')
  assert.equal(abortKind({ status: 0, error: 'EOF' }), 'failed')
  assert.equal(abortKind({ status: 0, error: 'dial tcp: connection refused', error_code: 1212 }), 'failed')
  assert.equal(abortKind({ status: 0, error: 'read: connection reset by peer' }), 'failed')
  assert.equal(abortKind({ status: 200, error: 'request timeout', error_code: 1050 }), 'timeout')
  assert.equal(abortKind({ status: 200, error: 'unexpected EOF' }), 'cut')
  assert.equal(abortKind({ status: 200, error: '' }), null)
  // An error answer is a FAILED request, not an abort.
  assert.equal(abortKind({ status: 503, error: 'some error' }), null)
})

test("a request without an answer logs TIMEOUT only when k6's timeout passed", () => {
  assert.equal(noAnswerKind({ status: 0, error: 'request timeout', error_code: 1050 }), 'TIMEOUT')
  assert.equal(noAnswerKind({ status: 0, error_code: 1050 }), 'TIMEOUT')
  assert.equal(noAnswerKind({ status: 0, error: 'EOF' }), 'FAILED')
  assert.equal(noAnswerKind({ status: 0, error: '' }), 'FAILED')
  assert.equal(k6Error({ status: 0, error: 'EOF' }), 'EOF')
  assert.equal(k6Error({ status: 0, error: '' }), 'no answer')
  assert.equal(k6Error({ status: 0, error: 'x'.repeat(500) }).length, 200)
})

test("PerchTail's error_code and scope are read from the error answer, nothing else", () => {
  const busy = JSON.stringify({ detail: { message: 'Busy', error_code: 'busy', scope: 'source' } })
  assert.deepEqual(errorFields(busy), { error_code: 'busy', scope: 'source' })
  const bytes = new TextEncoder().encode(JSON.stringify({ detail: { error_code: 'mfa_invalid_code' } }))
  assert.deepEqual(errorFields(bytes.buffer), { error_code: 'mfa_invalid_code', scope: null })
  assert.deepEqual(errorFields(JSON.stringify({ detail: 'Too many failed login attempts' })), {
    error_code: null,
    scope: null,
  })
  assert.deepEqual(errorFields('<html>proxy error</html>'), { error_code: null, scope: null })
  assert.deepEqual(errorFields(null), { error_code: null, scope: null })
  // Only short lower-case codes: never free text from the body.
  assert.deepEqual(errorFields(JSON.stringify({ detail: { error_code: 'Secret value 123!' } })), {
    error_code: null,
    scope: null,
  })
})

test('each kind of line is logged at most MAX_LOGGED_PER_VU times per virtual user', () => {
  const warn = console.warn
  const lines = []
  console.warn = (line) => lines.push(line)
  try {
    for (let i = 0; i < MAX_LOGGED_PER_VU + 5; i++) logLine('FAILED', { status: 503, n: i })
    assert.equal(logLine('TIMEOUT', { status: 0 }), true)
  } finally {
    console.warn = warn
  }
  assert.equal(lines.filter((l) => l.startsWith('FAILED ')).length, MAX_LOGGED_PER_VU)
  assert.equal(lines[0], 'FAILED {"status":503,"n":0}')
  assert.equal(lines[lines.length - 1], 'TIMEOUT {"status":0}')
})

const PLAN = {
  support: { weight: 40, users: ['lt-user-001', 'lt-user-002'] },
  investigation: { weight: 20, users: ['lt-user-003'] },
  admin: { weight: 10, mfa: true, users: ['lt-user-004', 'lt-user-005'] },
  occasional: { weight: 5, users: [] },
}

test('a user with MFA is only ever used by one virtual user', () => {
  const seen = new Map()
  for (let vu = 1; vu <= 30; vu++) {
    for (let session = 1; session <= 20; session++) {
      const user = userFor(PLAN.admin, vu, session)
      if (user === null) continue
      assert.equal(seen.get(user) ?? vu, vu)
      seen.set(user, vu)
    }
  }
  assert.deepEqual([...seen.entries()], [['lt-user-004', 1], ['lt-user-005', 2]])
  // The other kinds take their users in turn, shared between virtual users.
  assert.equal(userFor(PLAN.support, 3, 1), 'lt-user-001')
  assert.equal(userFor(PLAN.support, 3, 2), 'lt-user-002')
})

test('a virtual user without an admin of its own draws among the other kinds', () => {
  assert.deepEqual(available(PLAN, 2).map(([n]) => n), ['support', 'investigation', 'admin'])
  assert.deepEqual(available(PLAN, 3).map(([n]) => n), ['support', 'investigation'])
  for (let i = 0; i < 100; i++) assert.notEqual(drawPersona(PLAN, 3, i / 100), 'admin')
  // By weight: support 40, investigation 20, admin 10 of 70 for VU 1.
  assert.equal(drawPersona(PLAN, 1, 0), 'support')
  assert.equal(drawPersona(PLAN, 1, 0.6), 'investigation')
  assert.equal(drawPersona(PLAN, 1, 0.99), 'admin')
  // Without admin, the same rolls spread over 60.
  assert.equal(drawPersona(PLAN, 9, 0.99), 'investigation')
  assert.equal(drawPersona({ admin: PLAN.admin }, 9, 0.5), null)
})

test('a plan written before the mfa flag gets it from the users with a TOTP secret', () => {
  const old = { admin: { weight: 10, users: ['lt-user-004'] }, support: PLAN.support }
  const marked = withMfa(old, { 'lt-user-004': 'JBSWY3DPEHPK3PXP' })
  assert.equal(marked.admin.mfa, true)
  assert.equal(marked.support.mfa, false)
})
