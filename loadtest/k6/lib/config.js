// Run configuration for every scenario: the target, the profile, the
// seeded state and the phase being run, all read once in the init context.
//
// Environment (the runner, loadtest/run.py, sets all of them):
//   LOADTEST_BASE_URL       the instance, required, never defaulted
//   LOADTEST_ALLOWED_HOSTS  comma-separated host names the kit may load
//   LOADTEST_PROFILE        path to the profile JSON
//   LOADTEST_STATE_DIR      the instance's state folder (users, sources, catalog)
//   LOADTEST_STATE_URL      the URL the state was written for, when k6 reaches
//                           the instance under another name (Docker); by
//                           default LOADTEST_BASE_URL
//   LOADTEST_PHASE          the profile phase to run (optional for a single scenario)
//   LOADTEST_RESULTS_DIR    where handleSummary writes its JSON (optional)
//   LOADTEST_WRITER_START_MS when the phase's writer of growing files started
//                           (ms since the epoch): the steps of growing_heavy
//                           and the hours of daily_log count from it

function required(name) {
  const value = (__ENV[name] || '').trim()
  if (!value) throw new Error(`${name} is required`)
  return value
}

function parseUrl(url) {
  const match = /^(https?):\/\/(\[[^\]]+\]|[^/:?#]+)(?::(\d+))?([/?#].*)?$/i.exec(url)
  if (!match) throw new Error(`LOADTEST_BASE_URL must be an http(s) URL, got ${url}`)
  const scheme = match[1].toLowerCase()
  return {
    scheme,
    host: match[2].replace(/^\[|\]$/g, '').toLowerCase(),
    port: match[3] ? Number(match[3]) : scheme === 'https' ? 443 : 80,
    path: (match[4] || '').replace(/[?#].*$/, '').replace(/\/+$/, ''),
  }
}

/** The same normalized form as loadtest/seed/common.py's normalized_url. */
function normalizedUrl(url) {
  const u = parseUrl(url)
  const host = u.host.includes(':') ? `[${u.host}]` : u.host
  return `${u.scheme}://${host}:${u.port}${u.path}`
}

export const BASE_URL = required('LOADTEST_BASE_URL').replace(/\/+$/, '')

const allowed = (__ENV.LOADTEST_ALLOWED_HOSTS || '')
  .split(',')
  .map((h) => h.trim().toLowerCase())
  .filter(Boolean)
if (allowed.length === 0) throw new Error('LOADTEST_ALLOWED_HOSTS is required')
if (!allowed.includes(parseUrl(BASE_URL).host)) {
  throw new Error(`refusing to run against ${parseUrl(BASE_URL).host}: not in LOADTEST_ALLOWED_HOSTS`)
}

/** The Origin a browser on the instance's own page sends. */
export const ORIGIN = /^(https?:\/\/[^/]+)/i.exec(BASE_URL)[1]

const STATE_DIR = required('LOADTEST_STATE_DIR').replace(/[\\/]+$/, '')
const STATE_URL = normalizedUrl(__ENV.LOADTEST_STATE_URL || BASE_URL)

/** A state file of this instance; refused when written for another URL. */
function readState(name) {
  const payload = JSON.parse(open(`${STATE_DIR}/${name}`))
  if (!payload || payload.base_url !== STATE_URL || !('data' in payload)) {
    throw new Error(`${name} was written for ${payload && payload.base_url}, not ${STATE_URL}: refusing`)
  }
  return payload.data
}

export const profile = JSON.parse(open(required('LOADTEST_PROFILE')))
export const users = readState('users.json')
export const seeded = readState('seeded.json')
export const catalog = readState('catalog.json')

/** A state file that only some profiles have (production-plan.json). */
function readOptionalState(name) {
  try {
    return readState(name)
  } catch (e) {
    if (/written for/.test(String(e))) throw e
    return null
  }
}

export const plan = readOptionalState('production-plan.json')
/** The TOTP secrets of the kit's users that must use MFA (users-mfa.json). */
export const mfaSecrets = readOptionalState('users-mfa.json') || {}
export const RESULTS_DIR = (__ENV.LOADTEST_RESULTS_DIR || '').replace(/[\\/]+$/, '')

export const WRITER_START_MS = Number(__ENV.LOADTEST_WRITER_START_MS || 0)

export const PHASE_NAME = __ENV.LOADTEST_PHASE || ''
export const phase = PHASE_NAME ? profile.phases.find((p) => p.name === PHASE_NAME) : null
if (PHASE_NAME && !phase) throw new Error(`phase ${PHASE_NAME} isn't in profile ${profile.name}`)

/** Usernames of this profile's users, in a stable order. */
export const usernames = seeded.users.filter((u) => users[u])

/** A scenario's parameters from the profile. */
export function params(name) {
  return (profile.scenarios && profile.scenarios[name]) || {}
}
