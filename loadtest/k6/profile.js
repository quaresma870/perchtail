// Runs one phase of a profile (LOADTEST_PHASE): the phase's scenarios at
// once, each with its executor, duration and thresholds from the profile.
// loadtest/run.py runs the phases one after another.
import { buildOptions, summary } from './lib/options.js'

export { browse } from './browse.js'
export { follow } from './follow.js'
export { download } from './download.js'
export { name_search } from './name_search.js'
export { agents } from './agents.js'
export { background } from './background.js'
export { large_files } from './large_files.js'
export { ramp } from './ramp.js'
export { growing_files, growing_files_follow } from './growing_files.js'
export { growing_heavy } from './growing_heavy.js'
export { daily_log, daily_log_follow } from './daily_log.js'
export { production, incident } from './production.js'

export const options = buildOptions([])
export const handleSummary = summary
