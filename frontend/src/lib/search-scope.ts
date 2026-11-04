// What a search is narrowed to (#163): any mix of customers, folders (with
// everything below them) and single sources, plus an optional path filter.
// Kept in the URL so a scoped search can be shared or bookmarked, and in
// localStorage so it survives leaving the page.

export type ScopeKind = 'customer' | 'folder' | 'source'

export interface ScopeItem {
  kind: ScopeKind
  id: number
  name: string
}

export interface SearchScope {
  items: ScopeItem[]
  path: string
}

export const EMPTY_SCOPE: SearchScope = { items: [], path: '' }
const STORAGE_KEY = 'perchtail.searchScope'

export function scopeKey(item: Pick<ScopeItem, 'kind' | 'id'>): string {
  return `${item.kind}:${item.id}`
}

/** `customer:1,folder:4` → items (names are filled in later from the
 * source list; unknown or malformed entries are dropped). */
export function parseScopeParam(value: string | null): Pick<ScopeItem, 'kind' | 'id'>[] {
  if (!value) return []
  const out: Pick<ScopeItem, 'kind' | 'id'>[] = []
  const seen = new Set<string>()
  for (const part of value.split(',')) {
    const match = /^(customer|folder|source):(\d+)$/.exec(part.trim())
    if (!match) continue
    const item = { kind: match[1] as ScopeKind, id: Number(match[2]) }
    if (seen.has(scopeKey(item))) continue
    seen.add(scopeKey(item))
    out.push(item)
  }
  return out
}

export function scopeParam(items: Pick<ScopeItem, 'kind' | 'id'>[]): string {
  return items.map(scopeKey).join(',')
}

/** Query string for GET /search. */
export function searchQueryString(query: string, scope: SearchScope): string {
  const params = new URLSearchParams({ q: query })
  for (const item of scope.items) params.append(`${item.kind}_id`, String(item.id))
  if (scope.path.trim()) params.set('path', scope.path.trim())
  return params.toString()
}

export function toggleScopeItem(items: ScopeItem[], item: ScopeItem): ScopeItem[] {
  const key = scopeKey(item)
  return items.some((i) => scopeKey(i) === key)
    ? items.filter((i) => scopeKey(i) !== key)
    : [...items, item]
}

export function loadStoredScope(): SearchScope {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return EMPTY_SCOPE
    const parsed = JSON.parse(raw)
    const items = Array.isArray(parsed.items)
      ? parsed.items.filter(
          (i: ScopeItem) =>
            ['customer', 'folder', 'source'].includes(i?.kind) &&
            Number.isInteger(i?.id) &&
            typeof i?.name === 'string',
        )
      : []
    return { items, path: typeof parsed.path === 'string' ? parsed.path : '' }
  } catch {
    return EMPTY_SCOPE
  }
}

export function storeScope(scope: SearchScope): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(scope))
  } catch {
    // Private mode or blocked storage: the URL still carries the scope.
  }
}
