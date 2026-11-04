import { writable } from 'svelte/store'
import type { FormatMode } from './format-content'
import type { MarkPattern } from './mark-highlighting'

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
}

export interface ViewerState {
  tabs: Tab[]
  activeKey: string | null
}

/** Kept outside the Viewer component so open tabs survive navigating to
 * Search or Settings and back (#162) -- in memory only, gone on reload,
 * and closed on logout (closeAllTabs). Content is never persisted. */
export const viewerState = writable<ViewerState>({ tabs: [], activeKey: null })

/** Stable, distinct hue per source for tab badges. */
export function sourceHue(sourceId: number): number {
  return (sourceId * 137.508) % 360
}

export async function closeAllTabs(): Promise<void> {
  let tabs: Tab[] = []
  viewerState.update((state) => {
    tabs = state.tabs
    return { tabs: [], activeKey: null }
  })
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
