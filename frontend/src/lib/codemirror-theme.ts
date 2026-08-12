import { RangeSetBuilder } from '@codemirror/state'
import {
  Decoration,
  type DecorationSet,
  EditorView,
  ViewPlugin,
  type ViewUpdate,
  WidgetType,
} from '@codemirror/view'
import { findMatchesInLine, LEVEL_CLASS } from './severity-highlighting'
import { findCrlfLineNumbers, findWhitespaceRuns } from './whitespace-highlighting'
import type { SeverityPattern } from './types'

/** Dark theme matching the app's own design tokens (app.css custom
 * properties) rather than a fixed CodeMirror preset — a log viewer should
 * look like part of this app, not a bolted-on IDE widget. */
export const darkTheme = EditorView.theme(
  {
    '&': { backgroundColor: 'var(--bg)', color: 'var(--text)', height: '100%' },
    '.cm-content': { caretColor: 'var(--accent-hover)' },
    '.cm-scroller': { fontFamily: 'var(--font-mono)' },
    '.cm-gutters': {
      backgroundColor: 'var(--bg)',
      color: 'var(--text-faint)',
      border: 'none',
      borderRight: '1px solid var(--border-soft)',
    },
    '.cm-activeLine': { backgroundColor: 'rgba(255, 255, 255, 0.035)' },
    '.cm-activeLineGutter': { backgroundColor: 'rgba(255, 255, 255, 0.035)' },
    '.cm-selectionBackground, &.cm-focused .cm-selectionBackground, ::selection': {
      backgroundColor: 'rgba(99, 102, 241, 0.3) !important',
    },
    '.cm-cursor': { borderLeftColor: 'var(--accent-hover)' },
    '.cm-searchMatch': {
      backgroundColor: 'rgba(251, 191, 36, 0.3)',
      outline: '1px solid rgba(251, 191, 36, 0.5)',
    },
    '.cm-searchMatch.cm-searchMatch-selected': { backgroundColor: 'rgba(251, 191, 36, 0.55)' },
    '.cm-panels': { backgroundColor: 'var(--bg-elevated)', color: 'var(--text)' },
    '.cm-panels.cm-panels-top': { borderBottom: '1px solid var(--border)' },
    '.cm-panel input, .cm-panel button': {
      backgroundColor: 'var(--bg)',
      color: 'var(--text)',
      border: '1px solid var(--border)',
      borderRadius: '4px',
    },
    '.cm-level-info': { color: 'var(--success)' },
    '.cm-level-warn': { color: 'var(--warning)' },
    '.cm-level-error': { color: 'var(--danger)', fontWeight: '600' },
    '.cm-level-debug': { color: 'var(--text-faint)' },
    '.cm-line-error': {
      backgroundColor: 'var(--danger-soft)',
      borderLeft: '2px solid var(--danger)',
    },
    '.cm-line-bookmark': {
      backgroundColor: 'var(--accent-soft)',
      borderLeft: '2px solid var(--accent)',
    },
    '.cm-ws-glyph': {
      color: 'var(--text-faint)',
      opacity: '0.6',
    },
    '.cm-crlf-glyph': {
      color: 'var(--text-faint)',
      opacity: '0.6',
      fontSize: '0.75em',
      verticalAlign: 'middle',
    },
  },
  { dark: true },
)

function buildTokenDecorations(view: EditorView, patterns: SeverityPattern[]): DecorationSet {
  const builder = new RangeSetBuilder<Decoration>()
  for (const { from, to } of view.visibleRanges) {
    let pos = from
    while (pos <= to) {
      const line = view.state.doc.lineAt(pos)
      const matches = findMatchesInLine(line.text, patterns).sort(
        (a, b) => a.matchStart - b.matchStart,
      )
      for (const match of matches) {
        const start = line.from + match.matchStart
        const end = start + match.matchLength
        builder.add(start, end, Decoration.mark({ class: LEVEL_CLASS[match.level] }))
      }
      pos = line.to + 1
    }
  }
  return builder.finish()
}

function buildLineDecorations(view: EditorView, patterns: SeverityPattern[]): DecorationSet {
  const builder = new RangeSetBuilder<Decoration>()
  for (const { from, to } of view.visibleRanges) {
    let pos = from
    while (pos <= to) {
      const line = view.state.doc.lineAt(pos)
      const hasLineTint = findMatchesInLine(line.text, patterns).some((m) => m.highlightLine)
      if (hasLineTint) {
        builder.add(line.from, line.from, Decoration.line({ class: 'cm-line-error' }))
      }
      pos = line.to + 1
    }
  }
  return builder.finish()
}

/** Admin-configurable severity highlighting (see ROADMAP.md's "Viewer:
 * toward an advanced editor" section and backend/app/api/severity_patterns.py)
 * — colors matched tokens and optionally tints the whole line, driven by
 * whatever pattern set is effective for the currently open source, rather
 * than a fixed set of regexes. Two separate ViewPlugins/builders (line
 * tints vs. token marks), not one combined builder: RangeSetBuilder
 * requires strictly ascending position order, and interleaving line-level
 * and mark-level decorations from independently-sized ranges in a single
 * builder would violate that. `patterns` is captured at construction time —
 * the caller (CodeMirrorPane) rebuilds the whole extension set when the
 * effective pattern list changes, same as it already does for `content`. */
export function severityHighlighting(patterns: SeverityPattern[]) {
  const enabled = patterns.filter((p) => p.enabled)

  const lines = ViewPlugin.fromClass(
    class {
      decorations: DecorationSet
      constructor(view: EditorView) {
        this.decorations = buildLineDecorations(view, enabled)
      }
      update(update: ViewUpdate) {
        if (update.docChanged || update.viewportChanged) {
          this.decorations = buildLineDecorations(update.view, enabled)
        }
      }
    },
    { decorations: (v) => v.decorations },
  )

  const tokens = ViewPlugin.fromClass(
    class {
      decorations: DecorationSet
      constructor(view: EditorView) {
        this.decorations = buildTokenDecorations(view, enabled)
      }
      update(update: ViewUpdate) {
        if (update.docChanged || update.viewportChanged) {
          this.decorations = buildTokenDecorations(update.view, enabled)
        }
      }
    },
    { decorations: (v) => v.decorations },
  )

  return [lines, tokens]
}

class GlyphWidget extends WidgetType {
  constructor(
    readonly text: string,
    readonly className: string,
  ) {
    super()
  }
  toDOM(): HTMLElement {
    const span = document.createElement('span')
    span.className = this.className
    span.textContent = this.text
    return span
  }
  eq(other: GlyphWidget): boolean {
    return other.text === this.text && other.className === this.className
  }
  ignoreEvent(): boolean {
    return true
  }
}

const SPACE_GLYPH = '·'
const TAB_GLYPH = '→'
const CRLF_GLYPH = '␍'

function buildWhitespaceDecorations(view: EditorView, crlfLines: Set<number>): DecorationSet {
  const builder = new RangeSetBuilder<Decoration>()
  for (const { from, to } of view.visibleRanges) {
    let pos = from
    while (pos <= to) {
      const line = view.state.doc.lineAt(pos)
      for (const run of findWhitespaceRuns(line.text)) {
        const glyph = run.char === ' ' ? SPACE_GLYPH : TAB_GLYPH
        const start = line.from + run.start
        const end = start + run.length
        builder.add(
          start,
          end,
          Decoration.replace({
            widget: new GlyphWidget(glyph.repeat(run.length), 'cm-ws-glyph'),
          }),
        )
      }
      if (crlfLines.has(line.number)) {
        // Unlike the whitespace runs above, there's no `\r` character left
        // in `line.text` to replace -- CodeMirror's own line-separator
        // matching already consumed it while splitting the document into
        // lines (see findCrlfLineNumbers's doc comment). A zero-width
        // widget appended right after the line's last character is the
        // only way left to mark it.
        builder.add(line.to, line.to, Decoration.widget({ widget: new GlyphWidget(CRLF_GLYPH, 'cm-crlf-glyph'), side: 1 }))
      }
      pos = line.to + 1
    }
  }
  return builder.finish()
}

/** "Show all characters" toggle (Notepad++'s View -> Show Symbol): reveals
 * whitespace and CRLF-vs-LF line endings as visible glyphs. Relevant given
 * this tool spans both Linux and Windows sources (CLAUDE.md) -- spotting a
 * CRLF/LF mismatch or trailing whitespace is a real, recurring forensic
 * need here, not a generic editor nicety. `content` is the raw fetched
 * text (needed to detect CRLF before CodeMirror's own parsing consumes the
 * `\r`, see findCrlfLineNumbers) -- computed once per call, same "captured
 * at construction time" pattern as severityHighlighting's patterns. A
 * single ViewPlugin is enough here (unlike severityHighlighting's two):
 * every decoration is a `Decoration.replace`/`Decoration.widget` over a
 * distinct, non-overlapping position in one pass, so there's no
 * ascending-order conflict to split across builders. */
export function whitespaceHighlighting(content: string) {
  const crlfLines = findCrlfLineNumbers(content)

  return ViewPlugin.fromClass(
    class {
      decorations: DecorationSet
      constructor(view: EditorView) {
        this.decorations = buildWhitespaceDecorations(view, crlfLines)
      }
      update(update: ViewUpdate) {
        if (update.docChanged || update.viewportChanged) {
          this.decorations = buildWhitespaceDecorations(update.view, crlfLines)
        }
      }
    },
    { decorations: (v) => v.decorations },
  )
}

function buildBookmarkDecorations(view: EditorView, bookmarks: number[]): DecorationSet {
  const builder = new RangeSetBuilder<Decoration>()
  const marker = Decoration.line({ class: 'cm-line-bookmark' })
  // RangeSetBuilder requires strictly ascending position order -- sort
  // defensively rather than relying on the caller to hand these in order.
  for (const lineNumber of [...bookmarks].sort((a, b) => a - b)) {
    if (lineNumber < 1 || lineNumber > view.state.doc.lines) continue
    const line = view.state.doc.line(lineNumber)
    builder.add(line.from, line.from, marker)
  }
  return builder.finish()
}

/** Pure client-side/session bookmarks (ROADMAP.md: "never written to the
 * file, doesn't need to be persisted server-side") -- `bookmarks` is a
 * plain list of 1-indexed line numbers, owned and toggled by Viewer.svelte
 * per open tab, rendered here the same way severity line-tints are. */
export function bookmarkHighlighting(bookmarks: number[]) {
  return ViewPlugin.fromClass(
    class {
      decorations: DecorationSet
      constructor(view: EditorView) {
        this.decorations = buildBookmarkDecorations(view, bookmarks)
      }
      update(update: ViewUpdate) {
        if (update.docChanged || update.viewportChanged) {
          this.decorations = buildBookmarkDecorations(update.view, bookmarks)
        }
      }
    },
    { decorations: (v) => v.decorations },
  )
}
