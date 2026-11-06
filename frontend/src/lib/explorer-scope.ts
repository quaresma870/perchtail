// The explorer's advanced filter (#274): sources and folders picked as
// scopes, and the file-name filter applied inside them. Memory only, like
// the open tabs: kept while the app is open, gone on reload or logout.
import { writable } from 'svelte/store'

export interface Scope {
  sourceId: number
  /** "" for the whole source, else a folder path inside it. */
  path: string
}

export const advancedFilter = writable(false)
export const scopes = writable<Scope[]>([])

export const scopeKey = (scope: Scope) => `${scope.sourceId}:${scope.path}`

/** Whether `inner` lies inside `outer` (or is it). */
export function covers(outer: Scope, inner: Scope): boolean {
  if (outer.sourceId !== inner.sourceId) return false
  return outer.path === '' || inner.path === outer.path || inner.path.startsWith(`${outer.path}/`)
}

/** The scope that already covers `scope` from above, if any. */
export function coveringScope(list: Scope[], scope: Scope): Scope | undefined {
  return list.find((s) => scopeKey(s) !== scopeKey(scope) && covers(s, scope))
}

/** Adds the scope (dropping the ones it now covers) or removes it. */
export function toggleScope(list: Scope[], scope: Scope): Scope[] {
  if (list.some((s) => scopeKey(s) === scopeKey(scope))) {
    return list.filter((s) => scopeKey(s) !== scopeKey(scope))
  }
  if (coveringScope(list, scope)) return list
  return [...list.filter((s) => !covers(scope, s)), scope]
}

export function toggleScopeIn(scope: Scope): void {
  scopes.update((list) => toggleScope(list, scope))
}

export function removeScope(scope: Scope): void {
  scopes.update((list) => list.filter((s) => scopeKey(s) !== scopeKey(scope)))
}

export function clearScopes(): void {
  scopes.set([])
}

/**
 * Whether a file name matches the filter: a glob when it has `*` or `?`
 * (matched against the whole name), otherwise a case-insensitive
 * substring. An empty filter matches everything.
 */
export function nameMatches(name: string, query: string): boolean {
  const q = query.trim()
  if (!q) return true
  if (/[*?]/.test(q)) {
    const pattern = q
      .split('')
      .map((c) => (c === '*' ? '.*' : c === '?' ? '.' : c.replace(/[.+^${}()|[\]\\]/g, '\\$&')))
      .join('')
    return new RegExp(`^${pattern}$`, 'i').test(name)
  }
  return name.toLowerCase().includes(q.toLowerCase())
}
