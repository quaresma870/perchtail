// follow: open a file that keeps growing and follow it at the UI's pace
// (GET /follow every tail_interval_seconds) for follow_seconds, then close.
import { params } from './lib/config.js'
import { followVisit } from './lib/flows.js'
import { buildOptions, summary } from './lib/options.js'
import { between, Ui } from './lib/ui.js'

const ui = new Ui()

export function follow() {
  if (!ui.ensureLogin()) return
  const p = params('follow')
  followVisit(ui, p.follow_seconds || 120, p.protocols)
  ui.pause(between(p.think_seconds, 3))
}

export const options = buildOptions(['follow'])
export const handleSummary = summary
export default follow
