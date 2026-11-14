// k6 options for a set of scenarios: executors, durations and thresholds
// from the profile, and the JSON summary every run writes.

import { params, phase, PHASE_NAME, profile, RESULTS_DIR } from './config.js'

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
    gracefulStop: name === 'follow' || name === 'large_files' ? '60s' : '30s',
  }
}

function addThresholds(thresholds, name) {
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
    scenarios[name] = { ...executor(name, duration), exec: name, tags: { phase: PHASE_NAME || name } }
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
  out.stdout = [lines[0], ...lines.slice(1).sort()].join('\n') + '\n'
  return out
}
