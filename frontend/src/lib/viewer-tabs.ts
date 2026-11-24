import { tick } from 'svelte'
import { get, writable } from 'svelte/store'
import { forgetAllEditorStates, forgetEditorState, severityBySource } from './editor-states'
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
  // Pure client-side/session state (docs/design-decisions.md, "Scope and
  // principles": never written to the file, never persisted server-side) -- 1-indexed line numbers, sorted.
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
  // whether only what was appended can be fetched ('reload': .gz files
  // and archive members reload the whole file).
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
  // Where `content` starts in the file, in bytes of the original, for a
  // tab opened at its end: above 0, the start isn't loaded ("Load
  // earlier" reads the window before it, and line numbers aren't the
  // file's).
  startOffset?: number
  // Redaction (#382, #249): the server redacted the content, how many
  // values it replaced in what the tab holds, and how many characters at
  // the end of `content` stand for an unfinished last line that the next
  // Follow answer sends again.
  redacted?: boolean
  redactionCount?: number
  partialChars?: number
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

/** Closes the tabs with these keys: they leave `viewerState` (the active
 * tab stays if it isn't one of them, the last tab left becomes active
 * otherwise) and their editor states and formatted text are forgotten,
 * again once an editor still showing one has let go of it (CodeMirrorPane
 * keeps the state of the tab it shows when it switches or goes). A source
 * left with no open tab loses its entry in `severityBySource`. Returns the
 * closed tabs. */
export function closeTabs(keys: Iterable<string>): Tab[] {
  const closing = new Set(keys)
  let closed: Tab[] = []
  let remaining: Tab[] = []
  viewerState.update((state) => {
    closed = state.tabs.filter((t) => closing.has(t.key))
    if (closed.length === 0) return state
    remaining = state.tabs.filter((t) => !closing.has(t.key))
    const activeKey = remaining.some((t) => t.key === state.activeKey)
      ? state.activeKey
      : remaining.length > 0
        ? remaining[remaining.length - 1].key
        : null
    return { ...state, tabs: remaining, activeKey }
  })
  const forget = () => {
    for (const key of closing) forgetEditorState(key)
  }
  forget()
  void tick().then(forget)
  // A source left with no tab drops its severity patterns too: the next
  // file opened from it asks for them again.
  const stillOpen = new Set(remaining.map((t) => t.sourceId))
  const leaving = new Set(closed.map((t) => t.sourceId).filter((id) => !stillOpen.has(id)))
  if (leaving.size > 0) {
    severityBySource.update((all) => {
      const next = { ...all }
      for (const id of leaving) delete next[id]
      return next
    })
  }
  return closed
}

/** Forgets a source's severity patterns when no tab of it is open: an
 * open that failed before its tab was made leaves none, and the next file
 * opened from that source asks for them again. */
export function forgetSeverityWithoutTabs(sourceId: number): void {
  if (get(viewerState).tabs.some((t) => t.sourceId === sourceId)) return
  severityBySource.update((all) => {
    if (!(sourceId in all)) return all
    const next = { ...all }
    delete next[sourceId]
    return next
  })
}

/** Closes the tabs of every source not in `shown` (disabled, deleted or no
 * longer granted), so nothing of it stays on screen or in memory. Returns
 * the closed tabs. */
export function closeTabsOfHiddenSources(shown: ReadonlySet<number>): Tab[] {
  const hidden = get(viewerState).tabs.filter((t) => !shown.has(t.sourceId))
  if (hidden.length === 0) return []
  return closeTabs(hidden.map((t) => t.key))
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
  // The editor states and severity patterns go with the tabs; again once
  // an editor still on screen has let go of its tab (CodeMirrorPane keeps
  // the state of the tab it shows when it goes).
  forgetAllEditorStates()
  severityBySource.set({})
  await tick()
  forgetAllEditorStates()
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
