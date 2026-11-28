// k6 options for a set of scenarios: executors, durations and thresholds
// from the profile, and the JSON summary every run writes.

import { params, phase, PHASE_NAME, plan, profile, RESULTS_DIR } from './config.js'
import { growingProtocols, SUITE_FILES } from './growing.js'

const DEFAULT_DURATION = '60s'

function seconds(value) {
  const match = /^(\d+(?:\.\d+)?)(ms|s|m|h)?$/.exec(String(value))
  if (!match) throw new Error(`bad duration ${value}`)
  const n = Number(match[1])
  return { ms: n / 1000, s: n, m: n * 60, h: n * 3600 }[match[2] || 's']
}

export const RAMP_UP_SECONDS = 10

/** The ramp's steps: each ramps up for 10 s, then holds. The first
 * warmup_seconds of the hold (by default 30 s, at most half of it) are the
 * new users' first sign-ins and connections: requests then are tagged
 * stage:warmup and left out of the step's numbers; the rest of the hold is
 * stage:steady. Times are seconds from the scenario's start. */
export function rampSteps(p) {
  const steps = []
  const hold = p.step_seconds || 60
  const warmup = Math.min(p.warmup_seconds == null ? 30 : p.warmup_seconds, hold / 2)
  let start = 0
  for (let i = 0; i < (p.steps || 5); i++) {
    const end = start + RAMP_UP_SECONDS + hold
    steps.push({
      vus: (p.start_vus || 1) + i * (p.step_vus || 1),
      hold,
      start,
      steadyFrom: start + RAMP_UP_SECONDS + warmup,
      end,
    })
    start = end
  }
  return steps
}

// The growing-file scenarios: the operations each one measures, and the
// k6 scenario of its followers (VUs that only follow, follow_vus of them).
const GROWING = {
  growing_files: { ops: ['open', 'download', 'download-zip', 'follow'], follow: 'growing_files_follow' },
  growing_heavy: { ops: ['open', 'follow'] },
  daily_log: { ops: ['open', 'download', 'follow'], follow: 'daily_log_follow' },
}

function or(value, fallback) {
  return value === undefined || value === null ? fallback : value
}

// production's operations, with their default p95 limit (ms).
const PRODUCTION_OPS = {
  page: 1000,
  browse: 1000,
  open: 2000,
  'open-large': 30000,
  follow: 1000,
  search: 500,
  download: 15000,
  'download-zip': 60000,
  admin: 1000,
  close: 1000,
  logout: 1000,
}

/** production's k6 scenarios: the sessions, as many at once as the
 * plan's stages say, and the incident at its start time. */
function productionScenarios() {
  if (!plan) throw new Error('production-plan.json is missing: run seed.py apply with this profile')
  const out = {
    production: {
      executor: 'ramping-vus',
      startVUs: 0,
      stages: plan.stages.map((s) => ({ duration: `${s.duration}s`, target: s.target })),
      gracefulRampDown: '120s',
      gracefulStop: '120s',
      exec: 'production',
    },
  }
  const inc = plan.incident
  if (inc && inc.users > 0 && inc.source) {
    out.incident = {
      executor: 'constant-vus',
      vus: inc.users,
      duration: `${inc.duration}s`,
      startTime: `${inc.start}s`,
      gracefulStop: '60s',
      exec: 'incident',
    }
  }
  return out
}

/** production's limits: no integrity error, errors under error_rate, p95
 * per operation under its limit (scenarios.production.thresholds.ops), and
 * the numbers per kind of person and operation, for the summary. */
function productionThresholds(thresholds) {
  const t = params('production').thresholds || {}
  const limits = { ...PRODUCTION_OPS, ...(t.ops || {}) }
  thresholds['integrity_errors{suite:production}'] = ['count<1']
  thresholds['http_req_failed{scenario:production}'] = [`rate<${or(t.error_rate, 0.01)}`]
  thresholds['http_req_failed{scenario:incident}'] = [`rate<${or(t.error_rate, 0.01)}`]
  for (const [op, ms] of Object.entries(limits)) {
    thresholds[`http_req_duration{scenario:production,gop:${op}}`] = [`p(95)<${ms}`]
    for (const persona of [...Object.keys(plan.personas), 'incident']) {
      const tags = `persona:${persona},gop:${op}`
      thresholds[`http_req_duration{${tags}}`] = ['max>=0']
      thresholds[`http_req_failed{${tags}}`] = ['rate>=0']
    }
  }
  for (const op of ['open', 'follow', 'download-zip']) {
    thresholds[`http_req_duration{scenario:incident,gop:${op}}`] = ['max>=0']
  }
  thresholds['transfer_timeouts{suite:production}'] = ['count>=0']
  for (const op of ['open', 'open-large', 'download', 'download-zip', 'follow']) {
    thresholds[`integrity_errors{suite:production,gop:${op}}`] = ['count>=0']
    thresholds[`transfer_timeouts{suite:production,gop:${op}}`] = ['count>=0']
    thresholds[`growing_bytes{suite:production,gop:${op}}`] = ['count>=0']
  }
  for (const persona of Object.keys(plan.personas)) {
    thresholds[`sessions{persona:${persona}}`] = ['count>=0']
    thresholds[`session_minutes{persona:${persona}}`] = ['max>=0']
  }
  thresholds['http_req_duration{gop:login}'] = ['max>=0']
  thresholds['refused_too_large{suite:production}'] = ['count>=0']
  thresholds['follow_end_only{suite:production}'] = ['count>=0']
}

/** The k6 scenarios of a profile scenario: {k6 name: [exec, vus]}. */
function k6Scenarios(name) {
  const p = params(name)
  if (name === 'growing_heavy') {
    const files = or((profile.files || {}).heavy_count, 0)
    return { growing_heavy: ['growing_heavy', files * or(p.followers_per_file, 1)] }
  }
  if (GROWING[name]) {
    return { [name]: [name, or(p.vus, 1)], [GROWING[name].follow]: [GROWING[name].follow, or(p.follow_vus, 1)] }
  }
  return { [name]: [name, null] }
}

function executor(name, duration) {
  const p = params(name)
  if (name === 'ramp') {
    const stages = []
    for (const step of rampSteps(p)) {
      stages.push({ duration: `${RAMP_UP_SECONDS}s`, target: step.vus })
      stages.push({ duration: `${step.hold}s`, target: step.vus })
    }
    return { executor: 'ramping-vus', startVUs: 0, stages, gracefulRampDown: '20s', gracefulStop: '30s' }
  }
  return {
    executor: 'constant-vus',
    vus: name === 'background' ? 1 : p.vus || 1,
    duration,
    gracefulStop: ['follow', 'large_files', ...Object.keys(GROWING)].includes(name) ? '60s' : '30s',
  }
}

/** The limits of a growing-file scenario: no integrity error, errors
 * under error_rate, p95 per operation under its own limit; and, only to
 * get them into the summary, the numbers per protocol and operation and
 * Follow's delay per rate (growing_heavy) or per hour (daily_log). */
function growingThresholds(thresholds, name) {
  const t = params(name).thresholds || {}
  const limits = {
    open: or(t.open_p95_ms, 15000),
    download: or(t.download_p95_ms, 15000),
    'download-zip': or(t.zip_p95_ms, 60000),
    follow: or(t.follow_p95_ms, 2000),
  }
  const ops = GROWING[name].ops
  thresholds[`integrity_errors{suite:${name}}`] = ['count<1']
  thresholds[`transfer_timeouts{suite:${name}}`] = ['count>=0']
  thresholds[`http_req_failed{suite:${name}}`] = [`rate<${or(t.error_rate, 0.01)}`]
  for (const op of ops) thresholds[`http_req_duration{suite:${name},gop:${op}}`] = [`p(95)<${limits[op]}`]
  for (const protocol of growingProtocols(SUITE_FILES[name])) {
    for (const op of ops) {
      const tags = `suite:${name},gop:${op},protocol:${protocol}`
      thresholds[`http_req_duration{${tags}}`] = ['max>=0']
      thresholds[`http_req_failed{${tags}}`] = ['rate>=0']
      thresholds[`growing_bytes{${tags}}`] = ['count>=0']
      thresholds[`integrity_errors{${tags}}`] = ['count>=0']
      thresholds[`transfer_timeouts{${tags}}`] = ['count>=0']
    }
  }
  if (name === 'growing_heavy') {
    for (const mb of (profile.files || {}).heavy_rates_mb_s || [1, 5, 10]) {
      for (const part of ['early', 'late']) {
        thresholds[`follow_lag_seconds{suite:growing_heavy,rate:${mb},part:${part}}`] = ['max>=0']
        thresholds[`follow_lag_bytes{suite:growing_heavy,rate:${mb},part:${part}}`] = ['max>=0']
      }
    }
  }
  if (name === 'daily_log') {
    for (let hour = 0; hour < 24; hour++) {
      thresholds[`follow_lag_seconds{suite:daily_log,hour:${hour}}`] = ['max>=0']
      thresholds[`follow_lag_bytes{suite:daily_log,hour:${hour}}`] = ['max>=0']
    }
    thresholds['fetch_refusals{suite:daily_log}'] = ['count>=0']
  }
  thresholds[`follow_resets{suite:${name}}`] = ['count>=0']
  thresholds[`refused_too_large{suite:${name}}`] = ['count>=0']
  thresholds[`follow_end_only{suite:${name}}`] = ['count>=0']
}

function addThresholds(thresholds, name) {
  if (name === 'production') {
    productionThresholds(thresholds)
    return
  }
  if (GROWING[name]) {
    growingThresholds(thresholds, name)
    return
  }
  const t = params(name).thresholds
  if (!t || name === 'background') return
  if (name === 'ramp') {
    // One threshold per step, on its steady part only, and nothing aborts:
    // every step runs to the end, and loadtest/report decides the breaking
    // point from the complete steps with enough requests.
    rampSteps(params(name)).forEach((_, i) => {
      thresholds[`http_req_duration{scenario:ramp,step:${i + 1},stage:steady,flow:ui}`] = [`p(95)<${t.p95_ms}`]
      thresholds[`http_req_failed{scenario:ramp,step:${i + 1},stage:steady}`] = [`rate<${t.error_rate}`]
    })
    return
  }
  // Latency without the sign-in: the searches for name_search, the UI's
  // requests for the others; errors over every request.
  const flow = name === 'name_search' ? 'search' : 'ui'
  thresholds[`http_req_duration{scenario:${name},flow:${flow}}`] = [`p(95)<${t.p95_ms}`]
  thresholds[`http_req_failed{scenario:${name}}`] = [`rate<${t.error_rate}`]
}

/** Options for `names`, or for the phase's scenarios when a phase is set. */
export function buildOptions(names) {
  const list = phase ? phase.scenarios : names
  const duration = (phase && phase.duration) || __ENV.LOADTEST_DURATION || DEFAULT_DURATION
  seconds(duration)
  const scenarios = {}
  const thresholds = {}
  for (const name of list) {
    if (name === 'production') {
      Object.assign(scenarios, productionScenarios())
      for (const config of Object.values(scenarios)) config.tags = { phase: PHASE_NAME || name }
      addThresholds(thresholds, name)
      continue
    }
    for (const [k6name, [fn, vus]] of Object.entries(k6Scenarios(name))) {
      if (vus === 0) continue
      const config = { ...executor(name, duration), exec: fn, tags: { phase: PHASE_NAME || name } }
      if (vus !== null) config.vus = vus
      scenarios[k6name] = config
    }
    addThresholds(thresholds, name)
  }
  if (list.some((name) => name !== 'background')) {
    // Reported on its own: one argon2 verification per virtual user.
    thresholds['http_req_duration{flow:login}'] = ['p(95)<3000']
  }
  return {
    scenarios,
    thresholds,
    summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max', 'count'],
    discardResponseBodies: false,
    noConnectionReuse: false,
    maxRedirects: 0,
    userAgent: `perchtail-loadtest/${profile.name}`,
    setupTimeout: '60s',
    teardownTimeout: '60s',
  }
}

function fmt(ms) {
  return ms == null ? '-' : `${Math.round(ms)} ms`
}

/** Writes the full summary as JSON for loadtest/report, and a short text
 * summary to stdout (no remote jslib: nothing is fetched from outside). */
export function summary(data) {
  const out = {}
  const label = PHASE_NAME || 'run'
  if (RESULTS_DIR) out[`${RESULTS_DIR}/k6-${label}.json`] = JSON.stringify(data, null, 2)
  const lines = [`\nphase ${label} (profile ${profile.name})`]
  for (const [key, metric] of Object.entries(data.metrics)) {
    const m = /^http_req_duration\{scenario:([^,}]+)(?:,step:(\d+),stage:steady)?,flow:(?:ui|search)\}$/.exec(key)
    if (!m) continue
    const failed = data.metrics[key.replace('http_req_duration', 'http_req_failed').replace(/,flow:\w+/, '')]
    const v = metric.values
    const rate = failed ? (failed.values.rate * 100).toFixed(2) : '-'
    const ok = Object.values(metric.thresholds || {}).every((t) => t.ok)
    lines.push(
      `  ${m[1]}${m[2] ? ` step ${m[2]}` : ''}: p50 ${fmt(v.med)} p95 ${fmt(v['p(95)'])} p99 ${fmt(v['p(99)'])} ` +
        `requests ${v.count} errors ${rate}%${ok ? '' : '  THRESHOLD FAILED'}`,
    )
  }
  for (const [key, metric] of Object.entries(data.metrics)) {
    const m = /^http_req_duration\{suite:([^,}]+),gop:([^,}]+)\}$/.exec(key)
    if (!m) continue
    const v = metric.values
    const integrity = data.metrics[`integrity_errors{suite:${m[1]}}`]
    const bad = integrity ? integrity.values.count : 0
    const timeouts = data.metrics[`transfer_timeouts{suite:${m[1]}}`]
    const gaveUp = timeouts ? timeouts.values.count : 0
    const ok = Object.values(metric.thresholds || {}).every((t) => t.ok)
    lines.push(
      `  ${m[1]} ${m[2]}: p50 ${fmt(v.med)} p95 ${fmt(v['p(95)'])} requests ${v.count}` +
        ` (integrity errors in ${m[1]}: ${bad}, transfer timeouts: ${gaveUp})${ok ? '' : '  THRESHOLD FAILED'}`,
    )
  }
  out.stdout = [lines[0], ...lines.slice(1).sort()].join('\n') + '\n'
  return out
}
