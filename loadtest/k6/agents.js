// agents: the browse and follow traffic over push-agent sources only, with
// the profile's agents connected (and the stopped ones configured).
import { params } from './lib/config.js'
import { browseVisit, followVisit } from './lib/flows.js'
import { buildOptions, summary } from './lib/options.js'
import { between, Ui } from './lib/ui.js'

const ui = new Ui()

export function agents() {
  if (!ui.ensureLogin()) return
  const p = params('agents')
  const followShare = p.follow_share == null ? 0.3 : p.follow_share
  if (Math.random() < followShare) {
    followVisit(ui, p.follow_seconds || 60, ['agent'])
  } else {
    browseVisit(ui, p, ['agent'])
  }
  ui.pause(between(p.think_seconds, 5))
}

export const options = buildOptions(['agents'])
export const handleSummary = summary
export default agents
