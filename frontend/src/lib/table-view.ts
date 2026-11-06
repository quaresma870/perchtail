// Sorting and filtering for the Settings lists (#238). Done in the browser:
// the lists are small enough, and the API stays as it is.

export type SortDir = 'asc' | 'desc'
export interface SortState {
  key: string
  dir: SortDir
}

const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' })

// Strings in natural order ("app2" before "app10"), case-insensitive;
// numbers and booleans by value. Empty values (null, undefined, '') are
// handled by sortRows, which keeps them last in either direction.
export function compareValues(a: unknown, b: unknown): number {
  if (typeof a === 'number' && typeof b === 'number') return a - b
  if (typeof a === 'boolean' && typeof b === 'boolean') return Number(a) - Number(b)
  return collator.compare(String(a), String(b))
}

const isEmpty = (value: unknown) => value === null || value === undefined || value === ''

export function sortRows<T>(
  rows: T[],
  sort: SortState | null,
  accessors: Record<string, (row: T) => unknown>,
): T[] {
  const accessor = sort ? accessors[sort.key] : undefined
  if (!sort || !accessor) return rows
  const sign = sort.dir === 'asc' ? 1 : -1
  // Stable: rows that compare equal keep the order the API gave them.
  return rows
    .map((row, index) => ({ row, index, value: accessor(row) }))
    .sort((x, y) => {
      if (isEmpty(x.value) || isEmpty(y.value)) {
        if (isEmpty(x.value) && isEmpty(y.value)) return x.index - y.index
        return isEmpty(x.value) ? 1 : -1
      }
      return sign * compareValues(x.value, y.value) || x.index - y.index
    })
    .map(({ row }) => row)
}

// Clicking a header sorts by it ascending; clicking it again flips the order.
export function nextSort(current: SortState | null, key: string): SortState {
  if (current?.key === key) return { key, dir: current.dir === 'asc' ? 'desc' : 'asc' }
  return { key, dir: 'asc' }
}

// Every word of the query must appear in one of the fields, ignoring case.
export function matchesText(fields: (string | null | undefined)[], query: string): boolean {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean)
  if (words.length === 0) return true
  const haystack = fields.filter(Boolean).join(' ').toLowerCase()
  return words.every((word) => haystack.includes(word))
}

// The sort and filters of a list survive opening a row and coming back.
// sessionStorage can be unavailable (private mode, blocked storage): then
// the list simply starts unsorted and unfiltered.
export function loadViewState<T extends object>(key: string, fallback: T): T {
  try {
    const raw = sessionStorage.getItem(key)
    if (!raw) return fallback
    const parsed = JSON.parse(raw)
    return parsed && typeof parsed === 'object' ? { ...fallback, ...parsed } : fallback
  } catch {
    return fallback
  }
}

export function saveViewState(key: string, value: object): void {
  try {
    sessionStorage.setItem(key, JSON.stringify(value))
  } catch {
    // Not persisted; the list still works.
  }
}
