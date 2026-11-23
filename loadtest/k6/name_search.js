// name_search: the file-name index (GET /files) with plain text, a glob
// and a regex, and sometimes opening a hit, as the Search box does.
import { params, seeded } from './lib/config.js'
import { buildOptions, summary } from './lib/options.js'
import { between, pick, Ui } from './lib/ui.js'

const ui = new Ui()
const QUERIES = [
  { q: 'service', regex: false },
  { q: 'live', regex: false },
  { q: 'app-01/', regex: false },
  { q: '*.gz', regex: false },
  { q: 'service-*k-1.log', regex: false },
  { q: 'service-\\d+k-[12]\\.log$', regex: true },
  { q: '(bundle|large)-\\d+', regex: true },
]
const byId = Object.fromEntries(seeded.sources.map((s) => [s.id, s]))
// Opening a hit is part of searching, but a big file's transfer would
// measure the fetch, not the search: hits are opened only when small.
const OPENABLE_BYTES = 5 * 1024 * 1024

export function name_search() {
  if (!ui.ensureLogin()) return
  const p = params('name_search')
  const query = pick(QUERIES)
  const hits = ui.searchNames(query.q, query.regex)
  const openable = hits.filter(
    (h) => byId[h.source_id] && !/\.(zip|gz)$/.test(h.path) && h.size < OPENABLE_BYTES,
  )
  if (openable.length && Math.random() < 0.3) {
    const hit = pick(openable)
    const opened = ui.open(hit.source_id, hit.path, null, hit.size)
    ui.pause(between(p.read_seconds, 5))
    if (opened.ok) ui.close(hit.source_id, hit.path, null)
  }
  ui.pause(between(p.think_seconds, 5))
}

export const options = buildOptions(['name_search'])
export const handleSummary = summary
export default name_search
