import { writable } from 'svelte/store'
import type { FormatMode } from './format-content'
import type { MarkPattern } from './mark-highlighting'
import { advancedFilter, filterOptions, scopes } from './explorer-scope'

/** One open file in the viewer. Tabs from several sources live side by
 * side (#162), so each carries its own source -- every reload, follow,
 * close or download goes to `tab.sourceId`, never to "the current source". */
export interface Tab {
  key: string
  sourceId: number
  sourceName: string
  path: string
  member: string | null
  name: string
  content: string
  scratchKey: string | null
  wrapEnabled: boolean
  showWhitespace: boolean
  // Pure client-side/session state (ROADMAP.md: never written to the file,
  // never persisted server-side) -- 1-indexed line numbers, sorted.
  bookmarks: number[]
  markPatterns: MarkPattern[]
  // Display-only reformat for JSON/XML tabs; 'none' = raw content.
  format: FormatMode
  // "Follow" (tail -f) -- polled only while this tab is active.
  following: boolean
  // The file is no longer on the source (#272): the tab keeps what it
  // had, marked, and Follow stops.
  missing?: boolean
  // Incremental follow (#254): how many bytes of the file `content` holds,
  // the server's identity of its start (from the last follow answer), and
  // whether only what was appended can be fetched ('reload': .gz files,
  // archive members and agents older than 0.6 reload the whole file).
  byteSize: number
  followHead: string | null
  followMode: 'incremental' | 'reload'
  // Lines appended while the reader was scrolled up, for "jump to end".
  newLines: number
  // Tail's buffer cap (#347): newlines in `content` (counted once, then
  // kept up to date as Tail appends) and lines dropped from the top so far.
  newlineCount?: number
  droppedLines?: number
  // Opened at its end only (a large file, #347): Reload reads the end
  // again rather than the whole file.
  endOnly?: boolean
}

export interface ViewerState {
  tabs: Tab[]
  activeKey: string | null
}

/** Kept outside the Viewer component so open tabs survive navigating to
 * Search or Settings and back (#162) -- in memory only, gone on reload,
 * and closed on logout (closeAllTabs). Content is never persisted. */
export const viewerState = writable<ViewerState>({ tabs: [], activeKey: null })

/** Which explorer nodes are open -- machines (`source:<id>`), their folders
 * (`dir:<id>:<archive>:<path>`) and connection-tree groups -- kept like the
 * tabs, so going to Search and back doesn't collapse everything (#196).
 * Memory only; a node reopened this way lists its folder live again. */
export const explorerExpanded = writable<Set<string>>(new Set())

export function setExpanded(key: string, open: boolean): void {
  explorerExpanded.update((current) => {
    if (current.has(key) === open) return current
    const next = new Set(current)
    if (open) next.add(key)
    else next.delete(key)
    return next
  })
}

/** Stable, distinct hue per source for tab badges. */
export function sourceHue(sourceId: number): number {
  return (sourceId * 137.508) % 360
}

export async function closeAllTabs(): Promise<void> {
  // The advanced filter's scopes go with the tabs (#274).
  scopes.set([])
  advancedFilter.set(false)
  filterOptions.set({ regex: false, matchCase: false })
  let tabs: Tab[] = []
  viewerState.update((state) => {
    tabs = state.tabs
    return { tabs: [], activeKey: null }
  })
  explorerExpanded.set(new Set())
  await Promise.all(
    tabs
      .filter((t) => t.scratchKey)
      .map((t) =>
        fetch(`/sources/${t.sourceId}/close`, {
          method: 'POST',
          credentials: 'include',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ path: t.path, member: t.member }),
        }).catch(() => undefined),
      ),
  )
}
