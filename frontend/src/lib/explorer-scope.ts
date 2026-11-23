// The explorer's advanced filter (#274, #284): sources, folders inside them
// and folders of sources (customers and organizational folders) picked as
// scopes, and the file-name filter applied inside them. Memory only, like
// the open tabs: kept while the app is open, gone on reload or logout.
import { get, writable } from 'svelte/store'
import type { ConnectionTreeNode } from './connection-tree'

/** A source, or a folder inside it. */
export interface PathScope {
  sourceId: number
  /** "" for the whole source, else a folder path inside it. */
  path: string
}

/** A customer or organizational folder of sources ("customer:3",
 * "folder:12", "unassigned"): every source under it. */
export interface GroupScope {
  groupId: string
  name: string
}

export type Scope = PathScope | GroupScope

export const isGroupScope = (scope: Scope): scope is GroupScope => 'groupId' in scope

export const advancedFilter = writable(false)
export const scopes = writable<Scope[]>([])

/** The filter's options, the same as Find all's. */
export const filterOptions = writable({ regex: false, matchCase: false })

/** Bumped by "Stop" in the filter bar: every running search stops. */
export const stopSearches = writable(0)

/** Where each source sits among the customers and folders, so a ticked
 * group covers what's under it. Built from the connections tree. */
export interface ScopeContext {
  /** Source id -> the groups it sits in, outermost first. */
  sourceGroups: Map<number, string[]>
  /** Group id -> its enclosing groups, outermost first. */
  groupAncestors: Map<string, string[]>
  /** Group id -> every source under it (at any depth), in tree order. */
  groupSources: Map<string, number[]>
}

export const emptyScopeContext = (): ScopeContext => ({
  sourceGroups: new Map(),
  groupAncestors: new Map(),
  groupSources: new Map(),
})

export const scopeContext = writable<ScopeContext>(emptyScopeContext())

export function contextFromTree(nodes: ConnectionTreeNode[]): ScopeContext {
  const ctx = emptyScopeContext()
  const walk = (node: ConnectionTreeNode, ancestors: string[]): number[] => {
    ctx.groupAncestors.set(node.id, ancestors)
    const inside = [...ancestors, node.id]
    const all: number[] = []
    for (const child of node.children) all.push(...walk(child, inside))
    for (const source of node.sources) {
      ctx.sourceGroups.set(source.id, inside)
      all.push(source.id)
    }
    ctx.groupSources.set(node.id, all)
    return all
  }
  for (const node of nodes) walk(node, [])
  return ctx
}

export const scopeKey = (scope: Scope) =>
  isGroupScope(scope) ? `group:${scope.groupId}` : `${scope.sourceId}:${scope.path}`

/** Whether `inner` lies inside `outer` (or is it). */
export function covers(outer: Scope, inner: Scope, ctx: ScopeContext = get(scopeContext)): boolean {
  if (isGroupScope(outer)) {
    if (isGroupScope(inner)) {
      return inner.groupId === outer.groupId || (ctx.groupAncestors.get(inner.groupId) ?? []).includes(outer.groupId)
    }
    return (ctx.sourceGroups.get(inner.sourceId) ?? []).includes(outer.groupId)
  }
  if (isGroupScope(inner) || outer.sourceId !== inner.sourceId) return false
  return outer.path === '' || inner.path === outer.path || inner.path.startsWith(`${outer.path}/`)
}

/** The scope that already covers `scope` from above, if any. */
export function coveringScope(
  list: Scope[],
  scope: Scope,
  ctx: ScopeContext = get(scopeContext),
): Scope | undefined {
  return list.find((s) => scopeKey(s) !== scopeKey(scope) && covers(s, scope, ctx))
}

/** Adds the scope (dropping the ones it now covers) or removes it. */
export function toggleScope(list: Scope[], scope: Scope, ctx: ScopeContext = get(scopeContext)): Scope[] {
  if (list.some((s) => scopeKey(s) === scopeKey(scope))) {
    return list.filter((s) => scopeKey(s) !== scopeKey(scope))
  }
  if (coveringScope(list, scope, ctx)) return list
  return [...list.filter((s) => !covers(scope, s, ctx)), scope]
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

/** The sources a group scope stands for (whole sources), or the scope
 * itself for a source or folder. */
export function sourceScopesOf(scope: Scope, ctx: ScopeContext = get(scopeContext)): PathScope[] {
  if (!isGroupScope(scope)) return [scope]
  return (ctx.groupSources.get(scope.groupId) ?? []).map((sourceId) => ({ sourceId, path: '' }))
}

export interface FilterOptions {
  regex: boolean
  matchCase: boolean
}

export type Matcher = ((name: string, relativePath: string) => boolean) & { error?: undefined }

function globToRegex(glob: string): string {
  let out = ''
  for (let i = 0; i < glob.length; i++) {
    const c = glob[i]
    if (c === '*' && glob[i + 1] === '*') {
      out += '.*'
      i += 1
    } else if (c === '*') out += '[^/]*'
    else if (c === '?') out += '[^/]'
    else out += c.replace(/[.+^${}()|[\]\\]/g, '\\$&')
  }
  return out
}

/**
 * How a file is matched by the filter (#284):
 * - plain text: anywhere in the name;
 * - a glob (`*`, `?`, `**`) when Regex is off: the whole name;
 * - with Regex on, a regular expression found anywhere in the name.
 * A pattern with a `/` is matched against the path relative to the ticked
 * folder instead of the name. Case-insensitive unless Match case.
 * An empty filter matches everything; an invalid regex returns an error.
 */
export function compileMatcher(query: string, options: FilterOptions): Matcher | { error: string } {
  const q = query.trim()
  if (!q) return (() => true) as Matcher
  const flags = options.matchCase ? '' : 'i'
  const byPath = q.includes('/')
  let pattern: RegExp
  if (options.regex) {
    try {
      pattern = new RegExp(q, flags)
    } catch {
      return { error: 'Invalid regex' }
    }
  } else if (/[*?]/.test(q)) {
    pattern = new RegExp(`^${globToRegex(q)}$`, flags)
  } else {
    const needle = options.matchCase ? q : q.toLowerCase()
    return ((name: string, relativePath: string) => {
      const target = byPath ? relativePath : name
      return (options.matchCase ? target : target.toLowerCase()).includes(needle)
    }) as Matcher
  }
  return ((name: string, relativePath: string) => pattern.test(byPath ? relativePath : name)) as Matcher
}

export const isMatcherError = (m: Matcher | { error: string }): m is { error: string } =>
  typeof m !== 'function'

/**
 * What the explorer's search box holds (#300): a regex when written between
 * slashes (`/app\.log\.\d/`), otherwise text or a glob, which
 * compileMatcher tells apart by `*` and `?`. No separate Regex option.
 */
export function parseQuery(query: string): { text: string; regex: boolean } {
  const q = query.trim()
  const slashed = /^\/(.+)\/$/.exec(q)
  return slashed ? { text: slashed[1], regex: true } : { text: q, regex: false }
}

/** Whether a file name matches the filter (text or glob, no options). */
export function nameMatches(name: string, query: string): boolean {
  const matcher = compileMatcher(query, { regex: false, matchCase: false })
  return !isMatcherError(matcher) && matcher(name, name)
}
