// browse: sign in, list the sources, walk folders, open files of several
// sizes (plain, .gz, archive members), read them, close them.
import { params } from './lib/config.js'
import { browseVisit } from './lib/flows.js'
import { buildOptions, summary } from './lib/options.js'
import { between, Ui } from './lib/ui.js'

const ui = new Ui()

export function browse() {
  if (!ui.ensureLogin()) return
  const p = params('browse')
  browseVisit(ui, p, p.protocols)
  ui.pause(between(p.think_seconds, 5))
}

export const options = buildOptions(['browse'])
export const handleSummary = summary
export default browse
