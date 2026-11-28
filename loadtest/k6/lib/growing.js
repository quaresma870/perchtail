// What the growing_files, growing_heavy and daily_log scenarios do with
// the files loadtest/seed/growing.py keeps writing: open them whole,
// download them, download their folder as a ZIP and follow them, checking
// every answer with lib/integrity.js.
//
// Every request is tagged suite (the scenario), gop (open, download,
// download-zip, follow, browse, close) and protocol, and flow:growing, so
// none of it counts in the browse or follow limits. A check that fails
// adds 1 to integrity_errors and logs one line starting with INTEGRITY
// (the file, the protocol, the operation and the first deviation), which
// loadtest/report lists. A request whose k6 timeout passed (before any
// answer, or while the body arrived) isn't an integrity error: it adds 1 to
// transfer_timeouts and logs a TIMEOUT line; one without an answer for
// another reason (status 0: refused, reset, EOF) logs a FAILED line with
// k6's error (lib/failures.js).

import exec from 'k6/execution'
import { Counter, Trend } from 'k6/metrics'
import { catalog, seeded } from './config.js'
import { abortKind, k6Error, logLine } from './failures.js'
import { checkWhole, Stream } from './integrity.js'
import { listZip, readMember } from './zip.js'
import { parentFolder, walkTo } from './flows.js'
import { pick } from './ui.js'

export const integrityErrors = new Counter('integrity_errors')
export const transferTimeouts = new Counter('transfer_timeouts')
export const growingBytes = new Counter('growing_bytes')
export const followLagSeconds = new Trend('follow_lag_seconds')
export const followLagBytes = new Trend('follow_lag_bytes', true)
export const fetchRefusals = new Counter('fetch_refusals')
export const followResets = new Counter('follow_resets')
// A file the viewer can't follow: opened whole (it's below
// LARGE_FILE_BYTES) and refused cleanly as over MAX_FETCH_MB.
export const refusedTooLarge = new Counter('refused_too_large')
export const followEndOnly = new Counter('follow_end_only')

const FOLLOW_ROUNDS_PER_TICK = 8 // as the viewer (lib/ui.js)
const MAX_LOGGED_PER_VU = 20
const MB = 1024 * 1024
// frontend/src/lib/viewer-limits.ts: above LARGE_FILE_BYTES (as listed) the
// viewer offers "Tail the end", which reads the last TAIL_START_BYTES
// through Follow instead of opening the file whole.
export const LARGE_FILE_BYTES = 200 * MB

/** frontend/src/lib/viewer-limits.ts, opensFromEnd: a plain file whose
 * listed size is over `thresholdMb` (the open_from_end_mb System setting,
 * 50 MB by default; 0 opens every file whole) opens at its end; a .gz file
 * always opens whole. */
export function opensFromEnd(size, thresholdMb, name) {
  if (typeof size !== 'number' || !Number.isFinite(size)) return false
  if (typeof thresholdMb !== 'number' || !Number.isFinite(thresholdMb) || thresholdMb <= 0) return false
  if (String(name).toLowerCase().endsWith('.gz')) return false
  return size > thresholdMb * MB
}
export const TAIL_START_BYTES = 1 * MB
// When none of the tabs could be opened, the user reads the message
// before trying again: never a loop of retries without a pause.
export const NOTHING_OPEN_PAUSE_SECONDS = 60

let logged = 0

function enc(value) {
  return encodeURIComponent(value)
}

/** The catalog key of each suite's files. */
export const SUITE_FILES = { growing_files: 'growing_files', growing_heavy: 'heavy', daily_log: 'daily' }

/** Sources that serve `key` files: every shared-tree source, and for
 * growing_files the running agents among the first growing_agents. */
export function growingSources(key) {
  return seeded.sources.filter((s) => {
    if (s.protocol === 'agent') {
      return (
        key === 'growing_files' &&
        s.running &&
        s.agent_index <= (catalog.agent.growing_agents || 0) &&
        (catalog.agent[key] || []).length > 0
      )
    }
    return (catalog.shared[key] || []).length > 0
  })
}

export function growingProtocols(key) {
  return [...new Set(growingSources(key).map((s) => s.protocol))].sort()
}

export function filesOf(source, key) {
  return catalog[source.tree][key] || []
}

/** A source for this turn: the protocols in turn, so that each VU goes
 * through all of them. */
export function nextSource(ctx, key) {
  const protocols = growingProtocols(key)
  if (!protocols.length) return null
  ctx.turn = (ctx.turn || 0) + 1
  const protocol = protocols[(exec.vu.idInTest + ctx.turn) % protocols.length]
  return pick(growingSources(key).filter((s) => s.protocol === protocol))
}

function tagsFor(suite, op, source, extra) {
  return { suite, gop: op, protocol: source.protocol, flow: 'growing', ...(extra || {}) }
}

/** Counts and logs a failed check. */
export function failed(suite, op, source, path, error) {
  integrityErrors.add(1, { suite, gop: op, protocol: source.protocol })
  if (logged < MAX_LOGGED_PER_VU) {
    logged++
    console.warn(
      `INTEGRITY ${JSON.stringify({ suite, op, protocol: source.protocol, source: source.name, path, error })}`,
    )
  }
}

function bytesOf(res) {
  return res.body ? new Uint8Array(res.body) : new Uint8Array(0)
}

function text(bytes) {
  let s = ''
  for (let i = 0; i < Math.min(bytes.length, 4096); i++) s += String.fromCharCode(bytes[i])
  return s
}

/** True when the transfer ended early, after counting it: k6's timeout
 * passing in transfer_timeouts with a TIMEOUT line; no answer for another
 * reason with a FAILED line (status 0, k6's error); a 2xx body that
 * couldn't be read to the end for another reason as an integrity error. */
function aborted(ui, res, suite, op, source, path, name) {
  const kind = abortKind(res)
  if (!kind) return false
  if (kind === 'cut') {
    failed(suite, op, source, path, `transfer cut short: ${res.error}`)
    return true
  }
  if (kind === 'timeout') transferTimeouts.add(1, { suite, gop: op, protocol: source.protocol })
  logLine(kind === 'timeout' ? 'TIMEOUT' : 'FAILED', {
    scenario: exec.scenario.name,
    persona: (ui.tags && ui.tags.persona) || null,
    suite,
    op,
    protocol: source.protocol,
    source: source.name,
    path,
    name,
    status: res.status,
    error: k6Error(res),
  })
  return true
}

const TRANSFERS = ['open', 'download', 'download-zip']

function get(ui, suite, op, source, url, name, extra = {}) {
  // An open the caller counts apart (production's big files: `gopAs`).
  const tagged = op === 'open' && ui.tags.gopAs ? ui.tags.gopAs : op
  return ui.request('GET', url, null, name, {
    responseType: extra.responseType || 'binary',
    transfer: TRANSFERS.includes(op),
    tags: tagsFor(suite, tagged, source, extra.tags),
    accept: extra.accept,
  })
}

export function close(ui, suite, source, path, member = null) {
  ui.request('POST', `/sources/${source.id}/close`, { path, member }, 'POST close', {
    tags: tagsFor(suite, 'close', source),
  })
}

/** Expands the folders down to `path`'s, as the explorer does before a
 * file is opened (these requests carry no suite tag). */
export function browseTo(ui, source, path) {
  ui.collapseAll()
  walkTo(ui, source, parentFolder(path))
}

/**
 * GET /open or /download of a whole file, checked. `refusable`: a 413
 * fetch_too_large is a correct answer (daily_log past MAX_FETCH_MB), when
 * the server says the file is over its limit. Returns
 * {res, body, result} for a checked 200, {refused: true} for a clean
 * refusal (counted in `refusals`), or null.
 */
export function fetchWhole(ui, suite, op, source, path, refusable = false, member = null, refusals = fetchRefusals) {
  const route = op === 'download' ? 'download' : 'open'
  let url = `/sources/${source.id}/${route}?path=${enc(path)}`
  if (member) url += `&member=${enc(member)}`
  const res = get(ui, suite, op, source, url, `GET ${route}`, { accept: refusable ? [413] : [] })
  if (aborted(ui, res, suite, op, source, path, `GET ${route}`)) return null
  if (res.status === 413 && refusable) {
    let detail = null
    try {
      detail = JSON.parse(text(bytesOf(res))).detail
    } catch (e) {
      detail = null
    }
    const clean =
      detail &&
      detail.error_code === 'fetch_too_large' &&
      (detail.size_bytes == null || detail.size_bytes > detail.limit_bytes)
    if (!clean) {
      failed(suite, op, source, path, `refused without a clean fetch_too_large answer: ${text(bytesOf(res)).slice(0, 200)}`)
      return null
    }
    refusals.add(1, { suite, gop: op, protocol: source.protocol })
    return { refused: true, detail }
  }
  if (res.status !== 200) return null
  const body = bytesOf(res)
  growingBytes.add(body.length, { suite, gop: op, protocol: source.protocol })
  const declared = Number(res.headers['Content-Length'])
  const result = checkWhole(body, Number.isFinite(declared) ? declared : null)
  if (!result.ok) failed(suite, op, source, path, result.error)
  return { res, body, result }
}

/** Opens a file whole, checks it, and closes it like the viewer's tab. */
export function openWhole(ui, suite, source, path, refusable = false, member = null) {
  const got = fetchWhole(ui, suite, 'open', source, path, refusable, member)
  if (got && got.res) close(ui, suite, source, path, member)
  return got
}

/** GET /download-zip of a folder; each member that is a log (`*.log`, or
 * rotated `*.log.N`) must be a whole file from its first line, and nothing
 * may be skipped. */
export function downloadZip(ui, suite, source, folder) {
  const res = get(ui, suite, 'download-zip', source, `/sources/${source.id}/download-zip?path=${enc(folder)}`, 'GET download-zip')
  if (aborted(ui, res, suite, 'download-zip', source, folder, 'GET download-zip')) return
  if (res.status !== 200) return
  const body = bytesOf(res)
  growingBytes.add(body.length, { suite, gop: 'download-zip', protocol: source.protocol })
  let entries
  try {
    entries = listZip(body)
  } catch (e) {
    failed(suite, 'download-zip', source, folder, `not a whole ZIP: ${e.message}`)
    return
  }
  for (const entry of entries) {
    const base = entry.name.split('/').pop()
    if (base === 'SKIPPED.txt') {
      let note = ''
      readMember(body, entry, (piece) => {
        note += text(piece)
      })
      failed(suite, 'download-zip', source, `${folder}/${base}`, `files skipped: ${note.slice(0, 300)}`)
      continue
    }
    // The growing files, their rotated copies (.1, .2, ...) and the other
    // logs written with checkable lines; archives inside the folder aren't.
    if (!/\.log(\.\d+)?$/.test(base)) continue
    const stream = new Stream()
    const problem = readMember(body, entry, (piece) => stream.push(piece))
    if (problem || stream.error) {
      failed(suite, 'download-zip', source, `${folder}/${entry.name}`, problem || stream.error)
    }
  }
}

/**
 * One followed tab, opened as the viewer opens it: whole and checked, or,
 * when the explorer listed the file above LARGE_FILE_BYTES, only its end
 * (the viewer's "Tail the end", frontend/src/routes/Viewer.svelte,
 * openEnd). Then read tick after tick at the viewer's pace, each read
 * checked to carry on exactly where the last one stopped (after an
 * X-Follow-Reset, the reloaded file must be the same generation or a
 * later one). After each tick it records how far behind the writer the
 * viewer is: in seconds, from the time stamp of the last line received,
 * and in bytes, from the size the server reports (X-File-Size).
 * `lagTags(now, stream)` gives a lag sample's tags, or null to skip it.
 */
export class FollowTab {
  constructor(ui, suite, source, path, lagTags) {
    this.ui = ui
    this.suite = suite
    this.source = source
    this.path = path
    this.lagTags = lagTags
    this.stream = new Stream()
    this.ok = false
    this.endOnly = false
  }

  /** Opens the tab; false when it couldn't be opened or checked. A file
   * below LARGE_FILE_BYTES but over MAX_FETCH_MB is refused with a clean
   * 413: the viewer can't follow it, and refused_too_large counts it
   * (not as an error). */
  open() {
    const listed = this.ui.listedSize ? this.ui.listedSize(this.source.id, this.path) : null
    if (opensFromEnd(listed, this.ui.settings ? this.ui.settings.open_from_end_mb : null, this.path)) {
      this.endOnly = true
      followEndOnly.add(1, { suite: this.suite, protocol: this.source.protocol })
      return this.readEnd(listed) && this.ok
    }
    const opened = fetchWhole(this.ui, this.suite, 'open', this.source, this.path, true, null, refusedTooLarge)
    if (!opened || !opened.res) return false
    if (!this.stream.restart(opened.body, null)) return this.close()
    this.stream.writerStart = this.stream.firstTs
    this.offset = opened.body.length
    this.head = opened.res.headers['X-Follow-Head'] || null
    this.started = false
    this.ok = true
    return true
  }

  /** The viewer's readEnd: Follow from TAIL_START_BYTES before `size`,
   * starting the follow; after a reset, once more from the size the
   * server reports. True when the end was read and checked. */
  readEnd(size, retried = false) {
    const { ui, suite, source, path } = this
    const offset = Math.max(0, size - TAIL_START_BYTES)
    const url = `/sources/${source.id}/follow?path=${enc(path)}&offset=${offset}&start=true`
    const res = get(ui, suite, 'follow', source, url, 'GET follow')
    if (aborted(ui, res, suite, 'follow', source, path, 'GET follow')) {
      this.ok = false
      return false
    }
    if (res.status !== 200) return false
    if (res.headers['X-Follow-Reset'] === '1') {
      const now = Number(res.headers['X-File-Size'])
      if (!retried && Number.isFinite(now)) return this.readEnd(now, true)
      return false
    }
    const piece = bytesOf(res)
    growingBytes.add(piece.length, { suite, gop: 'follow', protocol: source.protocol })
    const next = Number(res.headers['X-Follow-Offset'])
    if (Number.isFinite(next) && next - offset !== piece.length) {
      this.fail(`${piece.length} bytes received from ${offset} but the offset moved to ${next}`)
      return false
    }
    if (!this.stream.restartAt(piece, offset)) {
      this.fail(this.stream.error)
      return false
    }
    this.offset = offset + piece.length
    this.head = res.headers['X-Follow-Head'] || null
    this.started = true
    this.ok = true
    return true
  }

  fail(error) {
    failed(this.suite, 'follow', this.source, this.path, error)
    this.ok = false
  }

  /** Stops the tab after a read that didn't come back (already counted,
   * see `aborted`): what was read can't be continued from. */
  stop() {
    this.ok = false
  }

  /** One tick: up to 8 reads while X-Follow-More says more is waiting. */
  tick() {
    if (!this.ok) return
    const { ui, suite, source, path, stream } = this
    let got = 0
    let size = null
    for (let round = 0; round < FOLLOW_ROUNDS_PER_TICK; round++) {
      let url = `/sources/${source.id}/follow?path=${enc(path)}&offset=${this.offset}`
      if (this.head) url += `&head=${enc(this.head)}`
      if (!this.started) url += '&start=true'
      const res = get(ui, suite, 'follow', source, url, 'GET follow')
      this.started = true
      if (aborted(ui, res, suite, 'follow', source, path, 'GET follow')) return this.stop()
      if (res.status !== 200) break
      if (res.headers['X-Follow-Reset'] === '1') {
        followResets.add(1, { suite, protocol: source.protocol })
        if (this.endOnly) {
          // The viewer's reloadEnd: a probe for the size now, then the end
          // from there. When it can't, the tab stays and tries again on the
          // next tick, as the viewer does.
          let probeUrl = `/sources/${source.id}/follow?path=${enc(path)}&offset=${this.offset}`
          if (this.head) probeUrl += `&head=${enc(this.head)}`
          const probe = get(ui, suite, 'follow', source, probeUrl, 'GET follow')
          if (aborted(ui, probe, suite, 'follow', source, path, 'GET follow')) return this.stop()
          const sizeNow = Number(probe.headers['X-File-Size'])
          if (!this.readEnd(Number.isFinite(sizeNow) ? sizeNow : this.offset)) {
            if (!this.ok) return
            break
          }
          size = this.offset
          break
        }
        const again = fetchWhole(ui, suite, 'open', source, path, true, null, refusedTooLarge)
        if (!again || !again.res) break
        if (!stream.restart(again.body, null)) return this.fail(`after a reset: ${stream.error}`)
        if (stream.writerStart == null) stream.writerStart = stream.firstTs
        this.offset = again.body.length
        this.head = again.res.headers['X-Follow-Head'] || null
        size = this.offset
        break
      }
      const piece = bytesOf(res)
      got += piece.length
      growingBytes.add(piece.length, { suite, gop: 'follow', protocol: source.protocol })
      const next = Number(res.headers['X-Follow-Offset'])
      if (Number.isFinite(next) && next - this.offset !== piece.length) {
        return this.fail(`${piece.length} bytes received but the offset moved by ${next - this.offset}`)
      }
      if (!stream.push(piece)) return this.fail(stream.error)
      if (stream.writerStart == null) stream.writerStart = stream.firstTs
      this.offset += piece.length
      this.head = res.headers['X-Follow-Head'] || this.head
      const reported = Number(res.headers['X-File-Size'])
      if (Number.isFinite(reported)) size = reported
      if (res.headers['X-Follow-More'] !== '1') break
    }
    if (stream.lastTs == null) return
    const now = Date.now()
    const tags = this.lagTags ? this.lagTags(now, stream) : {}
    if (!tags) return
    const behind = size == null ? 0 : Math.max(0, size - this.offset)
    // Nothing new and nothing waiting: caught up with a writer that is
    // idle, not behind it.
    const lag = got === 0 && behind === 0 ? 0 : Math.max(0, (now - stream.lastTs) / 1000)
    const all = { suite, protocol: source.protocol, ...tags }
    followLagSeconds.add(lag, all)
    followLagBytes.add(behind, all)
  }

  close() {
    // A tab holding only the end of its file has nothing in scratch: the
    // viewer sends no /close for it.
    if (!this.endOnly) close(this.ui, this.suite, this.source, this.path)
    this.ok = false
    return false
  }
}

/** Follows several tabs at once for `seconds`, one tick every
 * tail_interval_seconds, while the page's background polling goes on.
 * When none of them opens (a clean refusal, an error), it waits
 * NOTHING_OPEN_PAUSE_SECONDS (or `seconds`, if shorter) before returning,
 * so the caller never retries in a loop without a pause. */
export function followTabs(ui, tabs, seconds) {
  const open = tabs.filter((t) => t.open())
  if (!open.length) {
    ui.pause(Math.max(1, Math.min(seconds, NOTHING_OPEN_PAUSE_SECONDS)))
    return open
  }
  const tick = (ui.settings.tail_interval_seconds || 2) * 1000
  const end = Date.now() + seconds * 1000
  while (open.some((t) => t.ok) && Date.now() < end) {
    ui.pause(Math.min(tick, end - Date.now()) / 1000)
    if (Date.now() >= end) break
    for (const t of open) t.tick()
  }
  for (const t of open) if (t.ok) t.close()
  return open
}

/** Follows one file for `seconds` (see FollowTab). */
export function followChecked(ui, suite, source, path, seconds, lagTags) {
  const tab = new FollowTab(ui, suite, source, path, lagTags)
  followTabs(ui, [tab], seconds)
  return tab.stream
}

export function mbTag(bytesPerSecond) {
  return String(Math.round((bytesPerSecond / MB) * 100) / 100)
}
