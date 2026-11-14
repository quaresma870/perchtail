// download: download single files, and every few visits a whole folder as
// a ZIP (built and streamed by the server from fresh fetches).
import exec from 'k6/execution'
import { params } from './lib/config.js'
import { parentFolder, sourcesFor, treeOf, walkTo } from './lib/flows.js'
import { buildOptions, summary } from './lib/options.js'
import { between, pick, Ui } from './lib/ui.js'

const ui = new Ui()

export function download() {
  if (!ui.ensureLogin()) return
  const p = params('download')
  const sources = sourcesFor(p.protocols)
  if (!sources.length) return
  const source = pick(sources)
  const tree = treeOf(source)
  ui.collapseAll()
  const iteration = exec.vu.iterationInScenario + 1
  if (p.zip_every && iteration % p.zip_every === 0) {
    // The rotated/ folders: a bundle of compressed logs, the usual thing to
    // zip, and small enough to stay under MAX_FETCH_MB in every profile.
    const rotated = tree.folders.filter((f) => f.endsWith('/rotated'))
    const folder = pick(rotated.length ? rotated : tree.folders.filter((f) => !f.includes('/')))
    walkTo(ui, source, folder)
    ui.downloadZip(source.id, folder)
  } else {
    const file = pick(tree.sized)
    walkTo(ui, source, parentFolder(file.path))
    ui.download(source.id, file.path, file.size)
  }
  ui.pause(between(p.think_seconds, 10))
}

export const options = buildOptions(['download'])
export const handleSummary = summary
export default download
