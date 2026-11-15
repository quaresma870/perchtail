// growing_files: users open, download and follow files that keep growing
// (and rotate) while they do it, and download their folder as a ZIP, on
// every protocol in turn. Every answer is checked: a whole file from its
// first line, never shorter than announced, never cut off; Follow reads
// that carry on exactly where the last one stopped, through rotations;
// each ZIP member whole. The files: loadtest/seed/growing.py.
import exec from 'k6/execution'
import { params } from './lib/config.js'
import { browseTo, downloadZip, fetchWhole, filesOf, followChecked, nextSource, openWhole } from './lib/growing.js'
import { buildOptions, summary } from './lib/options.js'
import { between, pick, Ui } from './lib/ui.js'

const SUITE = 'growing_files'
const ui = new Ui()
const ctx = {}

/** Opens a file whole, downloads one, or (every zip_every visits) the
 * whole growing/ folder as a ZIP. */
export function growing_files() {
  if (!ui.ensureLogin()) return
  const p = params(SUITE)
  const source = nextSource(ctx, 'growing_files')
  if (!source) return
  const file = pick(filesOf(source, 'growing_files'))
  const visit = exec.vu.iterationInScenario + 1
  browseTo(ui, source, file.path)
  if (p.zip_every && visit % p.zip_every === 0) downloadZip(ui, SUITE, source, 'growing')
  else if (p.download_every && visit % p.download_every === 0) fetchWhole(ui, SUITE, 'download', source, file.path)
  else openWhole(ui, SUITE, source, file.path)
  ui.pause(between(p.think_seconds, 5))
}

/** Follows a growing file for follow_seconds (long enough to see it
 * rotate), at the viewer's pace. */
export function growing_files_follow() {
  if (!ui.ensureLogin()) return
  const p = params(SUITE)
  const source = nextSource(ctx, 'growing_files')
  if (!source) return
  const file = pick(filesOf(source, 'growing_files'))
  browseTo(ui, source, file.path)
  followChecked(ui, SUITE, source, file.path, p.follow_seconds || 120, () => ({}))
  ui.pause(between(p.think_seconds, 3))
}

export const options = buildOptions(['growing_files'])
export const handleSummary = summary
export default growing_files
