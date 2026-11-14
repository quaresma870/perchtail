// What a person does in a visit, shared by the scenarios: pick a source,
// walk to a folder, open files and read them, follow a growing file.

import { catalog, seeded } from './config.js'
import { between, pick } from './ui.js'

const SHARED_PROTOCOLS = ['ssh', 'smb', 'local', 'winrm']

// Browsing opens files of up to 10 MB by default (a scenario's
// max_open_mb changes it): the bigger ones belong to the large_files
// scenario and its own limit, since over SSH the transfer of a bigger file
// alone takes a good part of the browse limit, and p95 would measure the
// transfer instead of the browsing (the UI asks before opening a file
// over 200 MB).
const DEFAULT_MAX_OPEN_MB = 10

/** Seeded sources a user can reach: running agents only (a stopped
 * agent's source is configured but answers 502, which isn't load). */
export function sourcesFor(protocols) {
  const wanted = protocols || SHARED_PROTOCOLS
  return seeded.sources.filter(
    (s) => wanted.includes(s.protocol) && (s.protocol !== 'agent' || s.running),
  )
}

export function treeOf(source) {
  return catalog[source.tree]
}

export function parentFolder(path) {
  return path.includes('/') ? path.slice(0, path.lastIndexOf('/')) : ''
}

/** Expands the folders from the source's root down to `folder`. */
export function walkTo(ui, source, folder) {
  ui.browse(source.id, '')
  let path = ''
  for (const part of folder ? folder.split('/') : []) {
    path = path ? `${path}/${part}` : part
    ui.browse(source.id, path)
  }
}

/** Something to open: a plain log (most of the time), a .gz or an archive
 * member, as the explorer offers them, never over `maxBytes`. */
function pickFile(tree, maxBytes) {
  const roll = Math.random()
  if (roll < 0.1 && tree.zip.length) {
    const zip = pick(tree.zip)
    return { path: zip.path, member: pick(zip.members), size: null, archive: true }
  }
  const gz = tree.gz.filter((f) => f.plain_size <= maxBytes)
  if (roll < 0.25 && gz.length) {
    const file = pick(gz)
    return { path: file.path, member: null, size: file.plain_size }
  }
  let sized = tree.sized.filter((f) => f.size <= maxBytes)
  if (!sized.length) {
    const smallest = Math.min(...tree.sized.map((f) => f.size))
    sized = tree.sized.filter((f) => f.size === smallest)
  }
  const file = pick(sized)
  return { path: file.path, member: null, size: file.size }
}

/** A browsing visit: a source, a folder, a few files opened, read and
 * closed. Tabs stay open while the next file is opened, like a viewer. */
export function browseVisit(ui, p, protocols) {
  const sources = sourcesFor(protocols)
  if (!sources.length) return
  const source = pick(sources)
  const tree = treeOf(source)
  const count = Math.max(1, Math.round(between(p.files_per_visit, 2)))
  const maxBytes = (p.max_open_mb || DEFAULT_MAX_OPEN_MB) * 1024 * 1024
  ui.collapseAll()
  ui.severityPatterns(source.id)
  const open = []
  for (let i = 0; i < count; i++) {
    const file = pickFile(tree, maxBytes)
    walkTo(ui, source, parentFolder(file.path))
    if (file.archive) ui.browseArchive(source.id, file.path)
    const result = ui.open(source.id, file.path, file.member, file.size)
    if (result.ok) open.push(file)
    ui.pause(between(p.read_seconds, 10))
  }
  for (const file of open) ui.close(source.id, file.path, file.member)
}

/** Opens a growing file and follows it for `seconds`, then closes it. */
export function followVisit(ui, seconds, protocols) {
  const sources = sourcesFor(protocols).filter((s) => treeOf(s).growing.length)
  if (!sources.length) return
  const source = pick(sources)
  const file = pick(treeOf(source).growing)
  ui.collapseAll()
  walkTo(ui, source, parentFolder(file.path))
  const opened = ui.open(source.id, file.path, null, null)
  if (opened.ok) {
    ui.startFollow(source.id, file.path, opened.size, opened.head)
    ui.pause(seconds)
    ui.stopFollow()
    ui.close(source.id, file.path, null)
  }
}
