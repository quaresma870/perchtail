// large_files: open files close to MAX_FETCH_MB in parallel and keep them
// open for a while, to measure scratch space and memory. Uses the remote
// protocols only: local-disk files are read in place, without scratch.
import { params } from './lib/config.js'
import { parentFolder, sourcesFor, treeOf, walkTo } from './lib/flows.js'
import { buildOptions, summary } from './lib/options.js'
import { between, pick, Ui } from './lib/ui.js'

const ui = new Ui()

export function large_files() {
  if (!ui.ensureLogin()) return
  const p = params('large_files')
  const protocols = p.protocols || ['ssh', 'smb', 'winrm']
  const sources = sourcesFor(protocols).filter((s) => treeOf(s).large.length)
  if (!sources.length) return
  const source = pick(sources)
  const file = pick(treeOf(source).large)
  ui.collapseAll()
  walkTo(ui, source, parentFolder(file.path))
  const opened = ui.open(source.id, file.path, null, file.size)
  ui.pause(between(p.hold_seconds, 20))
  if (opened.ok) ui.close(source.id, file.path, null)
  ui.pause(between(p.think_seconds, 2))
}

export const options = buildOptions(['large_files'])
export const handleSummary = summary
export default large_files
