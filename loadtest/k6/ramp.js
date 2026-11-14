// ramp: browsing users added in steps. Every step runs to the end, nothing
// aborts: each request is tagged with its step and with stage:warmup (the
// step's ramp-up and the new users' first sign-ins and connections) or
// stage:steady, and loadtest/report finds the breaking point from the
// steady part of each complete step.
import exec from 'k6/execution'
import { params } from './lib/config.js'
import { browseVisit } from './lib/flows.js'
import { buildOptions, rampSteps, summary } from './lib/options.js'
import { between, Ui } from './lib/ui.js'

const ui = new Ui()
const p = params('ramp')
const steps = rampSteps(p)

function position() {
  const elapsed = (Date.now() - exec.scenario.startTime) / 1000
  for (let i = 0; i < steps.length; i++) {
    if (elapsed < steps[i].end) {
      return { step: i + 1, stage: elapsed >= steps[i].steadyFrom ? 'steady' : 'warmup' }
    }
  }
  // The graceful ramp-down after the last step: not part of any step.
  return { step: steps.length, stage: 'warmup' }
}

ui.beforeRequest = () => {
  const { step, stage } = position()
  exec.vu.metrics.tags.step = String(step)
  exec.vu.metrics.tags.stage = stage
}

export function ramp() {
  if (!ui.ensureLogin()) return
  browseVisit(ui, p, p.protocols)
  ui.pause(between(p.think_seconds, 2))
}

export const options = buildOptions(['ramp'])
export const handleSummary = summary
export default ramp
