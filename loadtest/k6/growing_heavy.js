// growing_heavy: several files written at rising rates (files.heavy_rates_mb_s,
// files.heavy_step_seconds per rate), each followed at the viewer's pace
// for the whole phase. Measures how far behind the writer Follow is at each
// rate (seconds and bytes), in the first and second half of each step, so
// the report can tell from which rate the delay keeps growing instead of
// staying level. Reads are checked as in growing_files.
import exec from 'k6/execution'
import { catalog, profile, WRITER_START_MS } from './lib/config.js'
import { browseTo, followChecked, mbTag, nextSource } from './lib/growing.js'
import { buildOptions, summary } from './lib/options.js'
import { Ui } from './lib/ui.js'

const SUITE = 'growing_heavy'
const ui = new Ui()
const ctx = {}

export function heavySteps() {
  const files = profile.files || {}
  return {
    rates: (files.heavy_rates_mb_s || [1, 5, 10]).map((mb) => mb * 1024 * 1024),
    stepMs: (files.heavy_step_seconds || 120) * 1000,
  }
}

export function growing_heavy() {
  if (!ui.ensureLogin()) return
  const files = catalog.shared.heavy || []
  if (!files.length) return
  const { rates, stepMs } = heavySteps()
  const start = WRITER_START_MS || exec.scenario.startTime
  const left = (start + rates.length * stepMs - Date.now()) / 1000
  if (left <= 5) {
    ui.pause(5)
    return
  }
  const file = files[exec.vu.idInTest % files.length]
  const source = nextSource(ctx, 'heavy')
  if (!source) return
  browseTo(ui, source, file.path)
  followChecked(ui, SUITE, source, file.path, left, (now) => {
    const step = Math.floor((now - start) / stepMs)
    if (step < 0 || step >= rates.length) return null
    const into = now - start - step * stepMs
    return { rate: mbTag(rates[step]), part: into < stepMs / 2 ? 'early' : 'late' }
  })
}

export const options = buildOptions(['growing_heavy'])
export const handleSummary = summary
export default growing_heavy
