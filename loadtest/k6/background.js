// background: no users at all. The instance runs only its own background
// work (source checks, file-name index, alerts, scratch sweeps) at the
// intervals in Settings (Source defaults, Alerts); loadtest/collect measures its cost. This
// scenario only keeps k6 running for the phase's duration.
import { sleep } from 'k6'
import { buildOptions, summary } from './lib/options.js'

export function background() {
  // Short, so the phase ends on time instead of waiting out a long sleep.
  sleep(5)
}

export const options = buildOptions(['background'])
export const handleSummary = summary
export default background
