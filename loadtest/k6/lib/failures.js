// What the kit logs about requests that didn't get the answer they should
// have, in classes the summary (loadtest/report) keeps apart:
//
// - TIMEOUT: k6's timeout passed (before any answer, or while a 2xx body
//   was still arriving). The server may still have been sending a correct
//   answer: not an integrity error.
// - FAILED: an answer outside 2xx that the request didn't expect (503
//   busy, 507, 429, 404...), with PerchTail's error_code and scope when
//   the body was read (never the body itself); or no answer for another
//   reason than k6's timeout (status 0: the connection was refused, reset
//   or closed, EOF), with k6's error.
// - LOGIN_FAILED: a refused sign-in, with the user's kit name (never a
//   password or a code).
//
// INTEGRITY lines (lib/growing.js) remain for a 2xx with a wrong or
// incomplete body. Each class logs at most MAX_LOGGED_PER_VU lines per
// virtual user. No k6 module is imported, so node can test this file.

export const MAX_LOGGED_PER_VU = 20
const MAX_ERROR_BODY = 4096
const CODE = /^[a-z0-9_]{1,64}$/

const logged = {}

/** k6's error code for a request that passed its timeout. */
const K6_REQUEST_TIMEOUT = 1050

/** True when k6 gave up waiting for this request. */
export function timedOut(res) {
  return res.error_code === K6_REQUEST_TIMEOUT || /timeout/i.test(String(res.error || ''))
}

/**
 * How a transfer ended early: 'timeout' when k6's timeout passed (with no
 * answer, or cutting a 2xx body); 'failed' when there was no answer for
 * another reason (status 0: refused, reset, EOF); 'cut' when a 2xx body
 * couldn't be read to the end for another reason (an integrity error);
 * null when the answer arrived whole.
 */
export function abortKind(res) {
  if (res.status === 0) return timedOut(res) ? 'timeout' : 'failed'
  if (res.status >= 200 && res.status < 300 && res.error) return timedOut(res) ? 'timeout' : 'cut'
  return null
}

/** The line a request without an answer (status 0) logs: TIMEOUT when
 * k6's timeout passed, FAILED otherwise. */
export function noAnswerKind(res) {
  return timedOut(res) ? 'TIMEOUT' : 'FAILED'
}

/** k6's error for a request without an answer, short enough to log. */
export function k6Error(res) {
  return String(res.error || 'no answer').slice(0, 200)
}

function asText(body) {
  if (body == null) return ''
  if (typeof body === 'string') return body.slice(0, MAX_ERROR_BODY)
  let bytes = null
  if (body instanceof ArrayBuffer) bytes = new Uint8Array(body, 0, Math.min(body.byteLength, MAX_ERROR_BODY))
  else if (ArrayBuffer.isView(body)) bytes = new Uint8Array(body.buffer, body.byteOffset, Math.min(body.byteLength, MAX_ERROR_BODY))
  if (!bytes) return ''
  let s = ''
  for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i])
  return s
}

function code(value) {
  return typeof value === 'string' && CODE.test(value) ? value : null
}

/** PerchTail's error_code and scope from an error answer's JSON
 * ({"detail": {"error_code": ..., "scope": ...}}), or nulls. Only short
 * lower-case codes are kept: nothing else of the body is ever logged. */
export function errorFields(body) {
  try {
    const detail = JSON.parse(asText(body)).detail
    if (detail && typeof detail === 'object') return { error_code: code(detail.error_code), scope: code(detail.scope) }
  } catch (e) {
    // not JSON (a proxy's page, a body not read): no code
  }
  return { error_code: null, scope: null }
}

/** Logs `KIND {record}` (as a k6 warning, so it lands in k6.log), at most
 * MAX_LOGGED_PER_VU times per kind in this virtual user. True if logged. */
export function logLine(kind, record) {
  const count = logged[kind] || 0
  if (count >= MAX_LOGGED_PER_VU) return false
  logged[kind] = count + 1
  console.warn(`${kind} ${JSON.stringify(record)}`)
  return true
}
