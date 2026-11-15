// daily_log: one file grows to files.daily_size_gb over a "day" compressed
// into files.daily_duration_min, quiet at night and busy in office hours
// with peaks (seed/growing.py, daily_schedule). Some users follow it all
// day, as whoever watches the log does; others now and then list its
// folder, open it whole and download it. Below MAX_FETCH_MB a whole read
// must pass the same checks as in growing_files; above it, it must be
// refused cleanly (413 fetch_too_large, counted in fetch_refusals), while
// Follow keeps up: past 200 MB the followers read the file from its end,
// as the viewer's "Tail the end" does (lib/growing.js, FollowTab). The
// delay of Follow is recorded per hour of the day.
import exec from 'k6/execution'
import { catalog, params, profile, WRITER_START_MS } from './lib/config.js'
import { browseTo, fetchWhole, followChecked, nextSource, openWhole } from './lib/growing.js'
import { buildOptions, summary } from './lib/options.js'
import { between, Ui } from './lib/ui.js'

const SUITE = 'daily_log'
const ui = new Ui()
const ctx = {}

function dayMs() {
  return ((profile.files || {}).daily_duration_min || 60) * 60 * 1000
}

/** Lists the folder, then opens the file whole or downloads it. */
export function daily_log() {
  if (!ui.ensureLogin()) return
  const p = params(SUITE)
  const file = (catalog.shared.daily || [])[0]
  const source = file && nextSource(ctx, 'daily')
  if (!source) return
  browseTo(ui, source, file.path)
  if ((exec.vu.iterationInScenario + 1) % 2 === 0) fetchWhole(ui, SUITE, 'download', source, file.path, true)
  else openWhole(ui, SUITE, source, file.path, true)
  ui.pause(between(p.every_seconds, 30))
}

/** Follows the file until the end of the day. */
export function daily_log_follow() {
  if (!ui.ensureLogin()) return
  const file = (catalog.shared.daily || [])[0]
  const source = file && nextSource(ctx, 'daily')
  if (!source) return
  const start = WRITER_START_MS || exec.scenario.startTime
  const left = (start + dayMs() - Date.now()) / 1000
  if (left <= 5) {
    ui.pause(5)
    return
  }
  const hourMs = dayMs() / 24
  browseTo(ui, source, file.path)
  followChecked(ui, SUITE, source, file.path, left, (now) => {
    const hour = Math.floor((now - start) / hourMs)
    return hour >= 0 && hour < 24 ? { hour: String(hour) } : null
  })
}

export const options = buildOptions(['daily_log'])
export const handleSummary = summary
export default daily_log
