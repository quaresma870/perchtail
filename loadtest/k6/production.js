// production: PerchTail as an installation in use. People of five kinds
// sign in, work for a while and sign out, through a working day: the
// number signed in rises through the morning, holds at the peak, and
// falls (the stages of production-plan.json, which seed.py writes from
// the profile; see loadtest/seed/production.py). Each session is one k6
// iteration, as one person of a kind chosen by weight, signed in as one of
// that kind's users (each kind has its own role: not everybody sees every
// source). Every request is tagged with the kind (persona) and the
// operation (gop), and every open and download is checked as in
// growing_files (every file of the production trees has checkable lines).
//
// - support: browses, opens files (Find and Find all run in the browser,
//   without requests: reading time stands for them), opens archive members;
// - oncall: follows 2 to 5 growing logs for the whole session;
// - investigation: searches file names, downloads files and folders as
//   ZIPs, opens big files;
// - admin: System settings, sources, rules, roles, users, the audit log;
// - occasional: signs in, opens a file, signs out.
//
// The admins' role needs MFA, and PerchTail takes each TOTP code once per
// account: an admin is only ever used by one virtual user, the n-th admin
// by virtual user n, and a virtual user without an admin of its own draws
// among the other kinds (lib/personas.js).
//
// incident: in the middle of the run (production-plan.json), `users`
// people (investigators, whose role needs no MFA) open and follow the same
// big growing log (an intense source's app-1/current.log), and `zip_users`
// of them download folders of that source as ZIPs, one after another, for
// `duration` seconds. Which folders is the profile's
// production.incident.zip_folder (the plan's zip_paths):
// - "rotated" (by default): the folders without the growing log (app-2 and
//   app-3, in turn: rotated logs, a .gz and archive.zip), as people
//   investigating download the rotated logs;
// - "active" (profiles/production-worst.json): app-1, with the growing log
//   of 1 GB or more. A worst case kept on purpose: every ZIP copies that
//   log whole to scratch, so the result depends on scratch and the
//   source's connector slots, not on a sizing limit.
import exec from 'k6/execution'
import { Counter, Trend } from 'k6/metrics'
import { catalog, mfaSecrets, plan, seeded } from './lib/config.js'
import { browseTo, downloadZip, fetchWhole, FollowTab, followTabs, openWhole } from './lib/growing.js'
import { buildOptions, summary } from './lib/options.js'
import { drawPersona, userFor, withMfa } from './lib/personas.js'
import { between, pick, Ui } from './lib/ui.js'

const SUITE = 'production'
const sessions = new Counter('sessions')
const sessionMinutes = new Trend('session_minutes')
const ui = new Ui()
const personas = plan ? withMfa(plan.personas, mfaSecrets) : {}
let sessionCount = 0

/** The seeded sources this session can see (from GET /sources at sign-in). */
function visibleSources(filter) {
  return seeded.sources.filter(
    (s) => (s.protocol !== 'agent' || s.running) && (!ui.visible || ui.visible.has(s.id)) && (!filter || filter(s)),
  )
}

function treeOf(source) {
  return catalog.sources[source.name]
}

function as(gop, fn) {
  const before = ui.tags.gop
  ui.tags.gop = gop
  try {
    return fn()
  } finally {
    ui.tags.gop = before
  }
}

function read(p, fallback) {
  as('background', () => ui.pause(between(p.read_seconds, fallback)))
}

// --- what each kind of person does ------------------------------------------

function supportVisit(p) {
  const source = pick(visibleSources())
  if (!source) return
  const tree = treeOf(source)
  const count = Math.max(1, Math.round(between(p.files_per_visit, 2)))
  for (let i = 0; i < count; i++) {
    const roll = Math.random()
    if (roll < 0.15 && tree.zip.length) {
      const zip = pick(tree.zip)
      as('browse', () => {
        browseTo(ui, source, zip.path)
        ui.browseArchive(source.id, zip.path)
      })
      openWhole(ui, SUITE, source, zip.path, false, pick(zip.members))
    } else {
      const file = roll < 0.3 && tree.gz.length ? pick(tree.gz) : pick(tree.static)
      as('browse', () => browseTo(ui, source, file.path))
      openWhole(ui, SUITE, source, file.path)
    }
    read(p, 30)
  }
}

function oncallSession(p, end) {
  const count = Math.max(1, Math.round(between(p.follow_files, 3)))
  const sources = visibleSources((s) => treeOf(s).current.length > 0)
  const tabs = []
  for (let i = 0; i < count && sources.length; i++) {
    const source = sources.splice(Math.floor(Math.random() * sources.length), 1)[0]
    const file = treeOf(source).current[0]
    as('browse', () => browseTo(ui, source, file.path))
    tabs.push(new FollowTab(ui, SUITE, source, file.path, () => ({})))
  }
  followTabs(ui, tabs, Math.max(10, (end - Date.now()) / 1000))
}

const SEARCHES = ['service.log', 'current', 'archive', '*.gz', 'big.log', 're:service\\.log\\.[0-9]']

function investigationVisit(p) {
  const roll = Math.random()
  if (roll < 0.35) {
    const q = pick(SEARCHES)
    const regex = q.startsWith('re:')
    as('search', () => ui.searchNames(regex ? q.slice(3) : q, regex))
    return
  }
  const withLarge = visibleSources((s) => treeOf(s).large.length > 0)
  if (roll < 0.5 && withLarge.length) {
    const source = pick(withLarge)
    const file = treeOf(source).large[0]
    as('browse', () => browseTo(ui, source, file.path))
    ui.tags.gopAs = 'open-large'
    try {
      openWhole(ui, SUITE, source, file.path)
    } finally {
      delete ui.tags.gopAs
    }
    read(p, 30)
    return
  }
  const source = pick(visibleSources())
  if (!source) return
  const tree = treeOf(source)
  if (roll < 0.75) {
    const file = pick(tree.static)
    as('browse', () => browseTo(ui, source, file.path))
    fetchWhole(ui, SUITE, 'download', source, file.path)
  } else {
    const folder = pick(tree.folders.filter((f) => f !== 'large'))
    as('browse', () => browseTo(ui, source, `${folder}/x`))
    downloadZip(ui, SUITE, source, folder)
  }
}

/** Reads and saves pages of Settings without changing anything that
 * matters: settings saved with their current values, a source saved with
 * its own name, a rule that matches nothing added and removed, the kit's
 * occasional role saved with its own name, a user's display name set. */
function adminVisit() {
  const roll = Math.random()
  const g = (path, name) => ui.get(path, name)
  if (roll < 0.25) {
    as('admin', () => {
      const res = g('/system-settings', 'GET /system-settings')
      if (res.status === 200) {
        const current = res.json()
        ui.request('PATCH', '/system-settings', { explorer_refresh_seconds: current.explorer_refresh_seconds }, 'PATCH /system-settings')
      }
    })
  } else if (roll < 0.5) {
    as('admin', () => {
      g('/audit?limit=50&offset=0&sort=time&order=desc', 'GET /audit')
      g('/audit/filters', 'GET /audit/filters')
      g('/audit/integrity', 'GET /audit/integrity')
    })
  } else if (roll < 0.75) {
    const source = pick(visibleSources((s) => s.protocol !== 'agent'))
    if (!source) return
    as('admin', () => {
      g(`/sources/${source.id}`, 'GET source')
      ui.request('PATCH', `/sources/${source.id}`, { name: source.name }, 'PATCH source')
      const rule = ui.request('POST', `/sources/${source.id}/rules`, { type: 'exclude', pattern: 'lt-never-matches-*.none' }, 'POST rule')
      if (rule.status === 201) ui.request('DELETE', `/sources/${source.id}/rules/${rule.json().id}`, null, 'DELETE rule')
    })
  } else {
    as('admin', () => {
      const roles = g('/roles', 'GET /roles')
      const users = g('/users', 'GET /users')
      if (roles.status === 200 && plan.admin_role_target) {
        const role = roles.json().find((r) => r.name === plan.admin_role_target)
        if (role) ui.request('PATCH', `/roles/${role.id}`, { name: role.name }, 'PATCH role')
      }
      if (users.status === 200) {
        const mine = users.json().filter((u) => plan.user_names.includes(u.username))
        const user = mine.length ? pick(mine) : null
        if (user) ui.request('PATCH', `/users/${user.id}`, { display_name: `Load test ${user.username}` }, 'PATCH user')
      }
    })
  }
}

function occasionalSession(p) {
  const source = pick(visibleSources())
  if (!source) return
  const file = pick(treeOf(source).static)
  as('browse', () => browseTo(ui, source, file.path))
  openWhole(ui, SUITE, source, file.path)
  read(p, 30)
}

// --- sessions -----------------------------------------------------------------

export function production() {
  const persona = drawPersona(personas, exec.vu.idInTest, Math.random())
  if (!persona) {
    as('background', () => ui.pause(60))
    return
  }
  const p = personas[persona]
  sessionCount++
  const username = userFor(p, exec.vu.idInTest, sessionCount)
  ui.resetSession()
  ui.tags = { persona, gop: 'login' }
  if (!ui.ensureLogin(username)) return
  ui.tags.gop = 'page'
  sessions.add(1, { persona })
  const began = Date.now()
  const end = began + between(p.session_minutes, 10) * 60 * 1000
  try {
    if (persona === 'oncall') oncallSession(p, end)
    else if (persona === 'occasional') occasionalSession(p)
    else {
      while (Date.now() < end) {
        if (persona === 'support') supportVisit(p)
        else if (persona === 'investigation') investigationVisit(p)
        else adminVisit()
        as('background', () => ui.pause(Math.min(between(p.think_seconds, 20), Math.max(0, (end - Date.now()) / 1000))))
      }
    }
  } finally {
    as('logout', () => ui.logout())
    sessionMinutes.add((Date.now() - began) / 60000, { persona })
  }
}

export function incident() {
  const inc = plan.incident
  if (!inc || !inc.source) return
  const source = seeded.sources.find((s) => s.name === inc.source)
  const users = plan.incident_users
  ui.resetSession()
  ui.tags = { persona: 'incident', gop: 'login' }
  if (!ui.ensureLogin(users[exec.vu.idInTest % users.length])) return
  ui.tags.gop = 'page'
  const left = (exec.scenario.startTime + inc.duration * 1000 - Date.now()) / 1000
  const zipper = exec.vu.idInTest % Math.max(1, Math.round(inc.users / Math.max(1, inc.zip_users))) === 0
  try {
    as('browse', () => browseTo(ui, source, inc.path))
    if (zipper) {
      // The plan's folders in turn, each ZIP user starting at its own; a
      // plan written before zip_paths existed zips the growing log's folder.
      const folders = inc.zip_paths && inc.zip_paths.length ? inc.zip_paths : [inc.path.split('/')[0]]
      let turn = exec.vu.idInTest
      const end = Date.now() + left * 1000
      while (Date.now() < end) {
        const folder = folders[turn++ % folders.length]
        as('browse', () => browseTo(ui, source, `${folder}/x`))
        downloadZip(ui, SUITE, source, folder)
        as('background', () => ui.pause(Math.min(30, Math.max(0, (end - Date.now()) / 1000))))
      }
    } else {
      let listed = ui.listedSize(source.id, inc.path)
      for (let attempt = 0; listed == null && attempt < 3; attempt++) {
        as('background', () => ui.pause(5))
        as('browse', () => browseTo(ui, source, inc.path))
        listed = ui.listedSize(source.id, inc.path)
      }
      const remaining = (exec.scenario.startTime + inc.duration * 1000 - Date.now()) / 1000
      if (listed != null && remaining > 0) {
        followTabs(ui, [new FollowTab(ui, SUITE, source, inc.path, () => ({}))], remaining)
      }
    }
  } finally {
    as('logout', () => ui.logout())
  }
}

export const options = buildOptions(['production'])
export const handleSummary = summary
export default production
