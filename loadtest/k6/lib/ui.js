// A virtual user that makes the requests PerchTail's web UI makes, at the
// UI's own pace (frontend/src/lib/api.ts, routes/Viewer.svelte,
// lib/follow.ts, lib/explorer-refresh.ts):
//
// - login: POST /auth/login sets the session cookie (SameSite=Strict, no
//   CSRF token); state-changing requests carry the page's Origin, which
//   the server compares with Host.
// - page load: /auth/me, /system-settings, /sources, /sources/recent.
// - explorer: GET /sources/{id}/browse?path=... per expanded folder;
//   auto-refresh re-lists every expanded folder (&refresh=true) every
//   explorer_refresh_seconds, and the source list is re-read on the same
//   interval (60 s when auto-refresh is off).
// - viewer: GET /open, POST /close when the tab closes; Follow polls
//   GET /follow every tail_interval_seconds, up to 8 reads per tick while
//   X-Follow-More says more is waiting.
//
// While a user "reads" (pause), the background requests fall due at their
// real times, so the load includes the polling a browser left open makes.
//
// Only 2xx answers count as success. PerchTail's API never redirects these
// requests, so redirects aren't followed and a 3xx (say, a proxy sending
// the request to a sign-in page) counts as an error, not as a fast answer.
// A 401 after signing in (the session expired or was revoked) signs in
// again, as the UI's login page would, and repeats the request once.

import http from 'k6/http'
import { check, sleep } from 'k6'
import exec from 'k6/execution'
import { Counter } from 'k6/metrics'
import { BASE_URL, ORIGIN, users, usernames } from './config.js'

http.setResponseCallback(http.expectedStatuses({ min: 200, max: 299 }))
const relogins = new Counter('session_relogins')

const FOLLOW_ROUNDS_PER_TICK = 8
const MAX_EXPANDED_FOLDERS = 6
const TIMEOUT = '180s'

export function between(range, fallback) {
  const [lo, hi] = Array.isArray(range) ? range : [fallback, fallback]
  return lo + Math.random() * (hi - lo)
}

export function pick(list) {
  return list[Math.floor(Math.random() * list.length)]
}

function enc(value) {
  return encodeURIComponent(value)
}

function sizeClass(bytes) {
  if (bytes == null) return 'unknown'
  if (bytes < 100 * 1024) return 'lt100k'
  if (bytes < 10 * 1024 * 1024) return 'lt10m'
  if (bytes < 100 * 1024 * 1024) return 'lt100m'
  return 'ge100m'
}

export class Ui {
  constructor() {
    this.loggedIn = false
    this.settings = { tail_interval_seconds: 2, explorer_refresh_seconds: 0 }
    this.expanded = [] // [{sourceId, path}]
    this.lastRefresh = 0
    this.lastStatus = 0
    this.following = null // {sourceId, path, offset, head, started}
    this.lastFollow = 0
    this.beforeRequest = null
    this.severityLoaded = {} // source id -> true, for the page's lifetime
    this.signingIn = false
  }

  // --- requests ----------------------------------------------------------

  /** k6 empties a VU's cookie jar at every iteration; a browser keeps its
   * session cookie, so it goes back in at the start of each iteration. */
  restoreCookies() {
    const iteration = exec.vu.iterationInInstance
    if (!this.cookies || this.cookieIteration === iteration) return
    const jar = http.cookieJar()
    for (const [name, value] of Object.entries(this.cookies)) jar.set(BASE_URL, name, value)
    this.cookieIteration = iteration
  }

  request(method, path, body, name, extra = {}, retried = false) {
    if (this.beforeRequest) this.beforeRequest()
    this.restoreCookies()
    // Sign-in (an argon2 hash, once per virtual user) is measured on its
    // own, so it doesn't skew the latency of what a scenario is about; the
    // name searches too, so name_search's limit is about the search.
    const flow = name === 'POST /auth/login' ? 'login' : name.startsWith('GET files') ? 'search' : 'ui'
    const params = {
      tags: { name, flow, ...(extra.tags || {}) },
      timeout: TIMEOUT,
      headers: { Accept: 'application/json' },
      responseType: extra.responseType || 'text',
    }
    let payload = null
    if (body !== undefined && body !== null) {
      payload = JSON.stringify(body)
      params.headers['Content-Type'] = 'application/json'
    }
    if (method !== 'GET') params.headers.Origin = ORIGIN
    const res = http.request(method, `${BASE_URL}${path}`, payload, params)
    check(res, { [`${name} ok`]: (r) => r.status >= 200 && r.status < 300 })
    if (res.status === 401 && this.loggedIn && !this.signingIn && !retried && flow !== 'login') {
      relogins.add(1)
      this.resetSession()
      if (this.ensureLogin()) return this.request(method, path, body, name, extra, true)
    }
    return res
  }

  /** What a page reload after signing in again starts from: no session
   * cookie, no expanded folders, nothing followed, settings read again. */
  resetSession() {
    this.loggedIn = false
    this.cookies = null
    http.cookieJar().clear(BASE_URL)
    this.expanded = []
    this.following = null
    this.severityLoaded = {}
  }

  get(path, name, extra) {
    return this.request('GET', path, null, name, extra)
  }

  // --- session -----------------------------------------------------------

  /** Signs in once per virtual user (each VU has its own cookie jar), as
   * one of the profile's users, then loads the page. */
  ensureLogin() {
    if (this.loggedIn) return true
    const username = usernames[(exec.vu.idInTest - 1) % usernames.length]
    const res = this.request('POST', '/auth/login', { username, password: users[username] }, 'POST /auth/login')
    if (res.status !== 200) {
      sleep(5)
      return false
    }
    this.cookies = {}
    for (const [cookieName, values] of Object.entries(res.cookies)) {
      if (values.length) this.cookies[cookieName] = values[0].value
    }
    this.cookieIteration = exec.vu.iterationInInstance
    this.loggedIn = true
    this.signingIn = true
    try {
      this.pageLoad()
    } finally {
      this.signingIn = false
    }
    return true
  }

  pageLoad() {
    this.get('/auth/me', 'GET /auth/me')
    const settings = this.get('/system-settings', 'GET /system-settings')
    if (settings.status === 200) {
      const body = settings.json()
      this.settings.tail_interval_seconds = body.tail_interval_seconds || 2
      this.settings.explorer_refresh_seconds = body.explorer_refresh_seconds || 0
    }
    this.get('/sources', 'GET /sources')
    this.get('/sources/recent', 'GET /sources/recent')
    const now = Date.now()
    this.lastRefresh = now
    this.lastStatus = now
  }

  // --- explorer ----------------------------------------------------------

  /** Expands a folder (or a source's root, path ""), keeping it registered
   * for auto-refresh like an open tree node. Returns the entries. */
  browse(sourceId, path) {
    const res = this.get(`/sources/${sourceId}/browse?path=${enc(path)}`, 'GET browse')
    this.expanded = this.expanded.filter((f) => !(f.sourceId === sourceId && f.path === path))
    this.expanded.push({ sourceId, path })
    if (this.expanded.length > MAX_EXPANDED_FOLDERS) this.expanded.shift()
    if (res.status !== 200) return []
    return res.json()
  }

  /** Expands an archive: the UI lists all its members at once (&all=true)
   * and, unlike a folder, never re-lists it on auto-refresh. */
  browseArchive(sourceId, path) {
    const res = this.get(`/sources/${sourceId}/browse?path=${enc(path)}&all=true`, 'GET browse archive')
    return res.status === 200 ? res.json() : []
  }

  /** The viewer asks for a source's severity patterns once per page. */
  severityPatterns(sourceId) {
    if (this.severityLoaded[sourceId]) return
    this.severityLoaded[sourceId] = true
    this.get(`/sources/${sourceId}/severity-patterns/effective`, 'GET severity-patterns')
  }

  collapseAll() {
    this.expanded = []
  }

  // --- viewer ------------------------------------------------------------

  /** Opens a file in a tab; returns {size, head} for Follow. */
  open(sourceId, path, member, knownSize) {
    let url = `/sources/${sourceId}/open?path=${enc(path)}`
    if (member) url += `&member=${enc(member)}`
    const res = this.get(url, 'GET open', {
      responseType: 'none',
      tags: { size_class: sizeClass(knownSize) },
    })
    const length = Number(res.headers['Content-Length'])
    return {
      ok: res.status === 200,
      size: Number.isFinite(length) ? length : 0,
      head: res.headers['X-Follow-Head'] || null,
    }
  }

  close(sourceId, path, member) {
    this.request('POST', `/sources/${sourceId}/close`, { path, member: member || null }, 'POST close')
  }

  download(sourceId, path, knownSize) {
    return this.get(`/sources/${sourceId}/download?path=${enc(path)}`, 'GET download', {
      responseType: 'none',
      tags: { size_class: sizeClass(knownSize) },
    })
  }

  downloadZip(sourceId, path) {
    return this.get(`/sources/${sourceId}/download-zip?path=${enc(path)}`, 'GET download-zip', {
      responseType: 'none',
    })
  }

  searchNames(query, regex) {
    let url = `/files?q=${enc(query)}&limit=200&after=0`
    if (regex) url += '&regex=true'
    const res = this.get(url, regex ? 'GET files regex' : /[*?]/.test(query) ? 'GET files glob' : 'GET files text')
    return res.status === 200 ? res.json().hits : []
  }

  // --- follow ------------------------------------------------------------

  startFollow(sourceId, path, size, head) {
    this.following = { sourceId, path, offset: size, head, started: false }
    this.lastFollow = 0
  }

  stopFollow() {
    this.following = null
  }

  followTick() {
    const f = this.following
    for (let round = 0; round < FOLLOW_ROUNDS_PER_TICK; round++) {
      let url = `/sources/${f.sourceId}/follow?path=${enc(f.path)}&offset=${f.offset}`
      if (f.head) url += `&head=${enc(f.head)}`
      if (!f.started) url += '&start=true'
      const res = this.get(url, 'GET follow', { responseType: 'none' })
      f.started = true
      if (res.status !== 200) return
      if (res.headers['X-Follow-Reset'] === '1') {
        // Rotated or truncated: the viewer reloads the whole file.
        const reopened = this.open(f.sourceId, f.path, null, null)
        f.offset = reopened.size
        f.head = reopened.head
        return
      }
      const next = Number(res.headers['X-Follow-Offset'])
      if (Number.isFinite(next)) f.offset = next
      f.head = res.headers['X-Follow-Head'] || f.head
      if (res.headers['X-Follow-More'] !== '1') return
    }
  }

  // --- time passing ------------------------------------------------------

  /** Waits `seconds` like a person reading, while the polling the open
   * page does in the background falls due. */
  pause(seconds) {
    const end = Date.now() + seconds * 1000
    for (;;) {
      const now = Date.now()
      const refreshMs = (this.settings.explorer_refresh_seconds || 0) * 1000
      const statusMs = (this.settings.explorer_refresh_seconds || 60) * 1000
      const followMs = this.settings.tail_interval_seconds * 1000
      const due = [end]
      if (refreshMs > 0) due.push(this.lastRefresh + refreshMs)
      if (this.loggedIn) due.push(this.lastStatus + statusMs)
      if (this.following) due.push(this.lastFollow + followMs)
      const next = Math.min(...due)
      if (next > now) sleep((next - now) / 1000)
      const t = Date.now()
      if (this.following && t >= this.lastFollow + followMs) {
        this.lastFollow = t
        this.followTick()
      }
      if (refreshMs > 0 && t >= this.lastRefresh + refreshMs) {
        this.lastRefresh = t
        for (const folder of this.expanded) {
          this.get(
            `/sources/${folder.sourceId}/browse?path=${enc(folder.path)}&refresh=true`,
            'GET browse refresh',
          )
        }
      }
      if (this.loggedIn && t >= this.lastStatus + statusMs) {
        this.lastStatus = t
        this.get('/sources', 'GET /sources')
      }
      if (Date.now() >= end) return
    }
  }
}
