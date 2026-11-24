import { EditorState, type EditorStateConfig } from '@codemirror/state'
import type { EditorView } from '@codemirror/view'
import { get, writable } from 'svelte/store'
import type { EditorInputs } from './editor-sync'
import type { SeverityPattern } from './types'

/** One tab's editor as it was when it was last shown: its CodeMirror
 * state (document, selection, compartments), the text it holds, where it
 * was scrolled to, and what it was showing (for the compartments that
 * changed while it was hidden). */
export interface SavedEditorState {
  state: EditorState
  content: string
  scrollTop: number
  /** The same place as a document position (`EditorView.scrollSnapshot`),
   * which holds once a new view has measured its lines. */
  scrollSnapshot?: ReturnType<EditorView['scrollSnapshot']>
  atBottom: boolean
  inputs: EditorInputs
}

/** Each open tab's editor state, by tab key, kept outside the components
 * so switching tabs or coming back to the Viewer from another page doesn't
 * build the document again from the whole text (seconds with a large
 * file). Memory of this page only, like the tabs' content: forgotten when
 * the tab closes and on logout (closeAllTabs). Not reactive. */
const editorStates = new Map<string, SavedEditorState>()

// Each tab's text as displayed (JSON/XML formatting), with the text and
// the format it was made from.
const formatted = new Map<string, { content: string; format: string; text: string }>()

export function getEditorState(key: string): SavedEditorState | undefined {
  return editorStates.get(key)
}

export function saveEditorState(key: string, saved: SavedEditorState): void {
  editorStates.set(key, saved)
}

export function forgetEditorState(key: string): void {
  editorStates.delete(key)
  formatted.delete(key)
}

export function forgetAllEditorStates(): void {
  editorStates.clear()
  formatted.clear()
}

/** A tab's displayed text: `compute()` runs only when the tab's text or
 * format changed since the last call. */
export function formattedContent(key: string, content: string, format: string, compute: () => string): string {
  const cached = formatted.get(key)
  if (cached && cached.content === content && cached.format === format) return cached.text
  const text = compute()
  formatted.set(key, { content, format, text })
  return text
}

/** `EditorState.create`, counted in development builds only, to see how
 * often the document is built from the whole text. */
export function createEditorState(config: EditorStateConfig): EditorState {
  if (import.meta.env.DEV) {
    const counted = globalThis as { __perchtailEditorStateCreates?: number }
    counted.__perchtailEditorStateCreates = (counted.__perchtailEditorStateCreates ?? 0) + 1
  }
  return EditorState.create(config)
}

/** The severity patterns of a source with none (yet, or none at all): one
 * shared, frozen list, so the editor sees the same value and has nothing to
 * redo. */
export const NO_PATTERNS = Object.freeze([]) as unknown as SeverityPattern[]

/** Each source's effective severity patterns, fetched on first use for
 * that source and kept like the tabs, so a file opened again or the Viewer
 * shown again uses them at once; asked for again in the background when the
 * Viewer is shown (refreshSeverityPatterns), so a change in Settings reaches
 * the open tabs. A source's entry goes when its last tab closes
 * (lib/viewer-tabs.ts, closeTabs), and all of them on logout (closeAllTabs). */
export const severityBySource = writable<Record<number, SeverityPattern[]>>({})

// What the highlighting reads from a pattern (lib/codemirror-theme.ts,
// severityHighlighting, and the problem navigation): two lists that agree
// on these highlight the same.
function highlightingOf(patterns: SeverityPattern[]): string {
  return JSON.stringify(
    patterns.map((p) => [p.level, p.pattern, p.pattern_kind, p.enabled, p.highlight_line, p.include_in_navigation]),
  )
}

/** The list to keep for a source: the one it had when the fetched list
 * highlights the same (the same reference, so the editor has nothing to
 * redo), the fetched one otherwise (`NO_PATTERNS` when empty). */
export function replacedPatterns(
  current: SeverityPattern[] | undefined,
  fetched: SeverityPattern[],
): SeverityPattern[] {
  const next = fetched.length === 0 ? NO_PATTERNS : fetched
  if (current !== undefined && highlightingOf(current) === highlightingOf(next)) return current
  return next
}

/** Asks for a source's patterns and keeps them in `severityBySource`
 * (replacedPatterns). The entry must already be there: a source whose
 * entry went meanwhile (logout) isn't put back. A failed request keeps the
 * list the source had. */
export async function refreshSeverityPatterns(
  sourceId: number,
  fetchPatterns: () => Promise<SeverityPattern[]>,
): Promise<void> {
  let fetched: SeverityPattern[]
  try {
    fetched = await fetchPatterns()
  } catch {
    return // the patterns the source had stay; browsing still works
  }
  const all = get(severityBySource)
  const current = all[sourceId]
  if (current === undefined) return
  const next = replacedPatterns(current, fetched)
  if (next !== current) severityBySource.set({ ...all, [sourceId]: next })
}
