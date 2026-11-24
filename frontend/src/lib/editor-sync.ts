/** What the viewer's editor shows, as CodeMirrorPane last applied it or is
 * asked to show now. Everything but `content` and `format` lives in its own
 * CodeMirror compartment and can change without rebuilding the document;
 * those are compared by identity, as the viewer hands out a new value only
 * when one changed. */
export interface EditorInputs {
  content: string
  format: string
  severityPatterns: unknown
  filename: string
  wrapEnabled: boolean
  showWhitespace: boolean
  bookmarks: unknown
  markPatterns: unknown
}

/** The editor's compartments: each is reconfigured on its own. `language`
 * carries the file name (syntax and the editor's accessible name);
 * `whitespace` also depends on the text, which it scans for CRLF endings. */
export type EditorPart = 'severity' | 'language' | 'wrap' | 'whitespace' | 'bookmarks' | 'marks'

export interface SyncPlan {
  /** `none`: nothing to do. `append`: text was added at the end only, one
   * transaction. `prepend`: text was added at the start only ("Load
   * earlier"), one transaction. `reconfigure`: only compartments change.
   * `rebuild`: a new editor state from the whole text. */
  action: 'none' | 'append' | 'prepend' | 'reconfigure' | 'rebuild'
  /** The compartments to reconfigure, with `append`, `prepend` or
   * `reconfigure`. */
  reconfigure: EditorPart[]
}

const REBUILD: SyncPlan = { action: 'rebuild', reconfigure: [] }

/** Decides how the editor goes from `applied` (null: nothing yet) to
 * `next`. Only a change of the text that isn't text added at the end or at
 * the start, or of the format, rebuilds the state; anything else keeps the
 * document, its selection and its place. */
export function planSync(applied: EditorInputs | null, next: EditorInputs): SyncPlan {
  if (applied === null || applied.format !== next.format) return REBUILD
  let added: 'append' | 'prepend' | null = null
  if (next.content !== applied.content) {
    if (next.content.length <= applied.content.length) return REBUILD
    if (next.content.startsWith(applied.content)) added = 'append'
    else if (next.content.endsWith(applied.content)) added = 'prepend'
    else return REBUILD
  }
  const appended = added !== null
  const reconfigure: EditorPart[] = []
  if (next.severityPatterns !== applied.severityPatterns) reconfigure.push('severity')
  if (next.filename !== applied.filename) reconfigure.push('language')
  if (next.wrapEnabled !== applied.wrapEnabled) reconfigure.push('wrap')
  if (next.showWhitespace !== applied.showWhitespace || (appended && next.showWhitespace)) {
    reconfigure.push('whitespace')
  }
  if (next.bookmarks !== applied.bookmarks) reconfigure.push('bookmarks')
  if (next.markPatterns !== applied.markPatterns) reconfigure.push('marks')
  if (added !== null) return { action: added, reconfigure }
  return { action: reconfigure.length > 0 ? 'reconfigure' : 'none', reconfigure }
}
