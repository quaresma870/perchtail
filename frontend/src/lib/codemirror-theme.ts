import { javascript } from '@codemirror/lang-javascript'
import { json } from '@codemirror/lang-json'
import { xml } from '@codemirror/lang-xml'
import { type Line, RangeSetBuilder, StateEffect } from '@codemirror/state'
import {
  Decoration,
  type DecorationSet,
  EditorView,
  ViewPlugin,
  type ViewUpdate,
  WidgetType,
} from '@codemirror/view'
import type { FileLanguage } from './file-language'
import { findMarkMatches, type MarkPattern } from './mark-highlighting'
import { findRedactionMarkers } from './redaction-markers'
import {
  LEVEL_CLASS,
  type SeverityMatch,
  type SeverityMatcher,
  severityMatcher,
} from './severity-highlighting'
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
    // "Mark" highlighting palette (Notepad++-style ad hoc pattern
    // highlighting, see mark-highlighting.ts) -- a fixed set of visually
    // distinct colors cycled through as marks are added, separate from
    // severity's level-based classes above.
    '.cm-mark-0': { backgroundColor: 'rgba(56, 189, 248, 0.32)' },
    '.cm-mark-1': { backgroundColor: 'rgba(244, 114, 182, 0.32)' },
    '.cm-mark-2': { backgroundColor: 'rgba(163, 230, 53, 0.32)' },
    '.cm-mark-3': { backgroundColor: 'rgba(192, 132, 252, 0.32)' },
    '.cm-mark-4': { backgroundColor: 'rgba(251, 146, 60, 0.32)' },
    '.cm-mark-5': { backgroundColor: 'rgba(94, 234, 212, 0.32)' },
    // Values the server redacted (#382, #249).
    '.cm-redacted': {
      backgroundColor: 'rgba(148, 163, 184, 0.18)',
      borderRadius: '3px',
      fontStyle: 'italic',
    },
  },
  { dark: true },
)

/** The lines in view, each once, in document order. */
function visibleLines(view: EditorView): Line[] {
  const lines: Line[] = []
  let last = 0
  for (const { from, to } of view.visibleRanges) {
    let pos = from
    while (pos <= to) {
      const line = view.state.doc.lineAt(pos)
      if (line.number > last) {
        lines.push(line)
        last = line.number
      }
      pos = line.to + 1
    }
  }
  return lines
}

// Redraws the editor once the worker's matches have arrived.
const severityMatchesArrived = StateEffect.define<null>()

/** Admin-configurable severity highlighting (see docs/design-decisions.md,
 * "Frontend and UI", and backend/app/api/severity_patterns.py)
 * — colors matched tokens and optionally tints the whole line, driven by
 * whatever pattern set is effective for the currently open source, rather
 * than a fixed set of regexes. The lines in view are matched in a Web
 * Worker (severity-highlighting.ts), so the decorations arrive a moment
 * later; until then, the previous ones stay, moved with any appended text.
 * One request at a time: a change while one is out is matched when it
 * returns. Line tints and token marks are two decoration sets, not one
 * builder: RangeSetBuilder requires strictly ascending position order,
 * and interleaving line-level and mark-level decorations in a single
 * builder would violate that. `patterns` is captured at construction time —
 * when the effective pattern list changes, the caller (CodeMirrorPane)
 * reconfigures the severity compartment with a new instance; the document
 * and the rest of the editor state stay as they are. */
export function severityHighlighting(
  patterns: SeverityPattern[],
  matcher: SeverityMatcher = severityMatcher,
) {
  const enabled = patterns.filter((p) => p.enabled)

  const plugin = ViewPlugin.fromClass(
    class {
      lines: DecorationSet = Decoration.none
      tokens: DecorationSet = Decoration.none
      private busy = false
      private stale = false
      private destroyed = false
      private readonly view: EditorView

      constructor(view: EditorView) {
        this.view = view
        this.request()
      }

      update(update: ViewUpdate) {
        if (update.docChanged) {
          this.lines = this.lines.map(update.changes)
          this.tokens = this.tokens.map(update.changes)
        }
        if (update.docChanged || update.viewportChanged) this.request()
      }

      destroy() {
        this.destroyed = true
      }

      private request() {
        if (enabled.length === 0) return
        if (this.busy) {
          this.stale = true
          return
        }
        this.busy = true
        this.stale = false
        const doc = this.view.state.doc
        const lines = visibleLines(this.view)
        void matcher
          .match(
            lines.map((line) => line.text),
            enabled,
          )
          .then((matches) => {
            this.busy = false
            if (this.destroyed) return
            if (this.stale || this.view.state.doc !== doc) {
              this.request()
              return
            }
            this.show(lines, matches)
          })
      }

      private show(lines: Line[], matches: SeverityMatch[][]) {
        const lineTints = new RangeSetBuilder<Decoration>()
        const tokens = new RangeSetBuilder<Decoration>()
        lines.forEach((line, index) => {
          const found = [...matches[index]].sort((a, b) => a.matchStart - b.matchStart)
          if (found.some((m) => m.highlightLine)) {
            lineTints.add(line.from, line.from, Decoration.line({ class: 'cm-line-error' }))
          }
          for (const match of found) {
            const start = line.from + match.matchStart
            tokens.add(
              start,
              start + match.matchLength,
              Decoration.mark({ class: LEVEL_CLASS[match.level] }),
            )
          }
        })
        this.lines = lineTints.finish()
        this.tokens = tokens.finish()
        this.view.dispatch({ effects: severityMatchesArrived.of(null) })
      }
    },
    {
      provide: (p) => [
        EditorView.decorations.of((view) => view.plugin(p)?.lines ?? Decoration.none),
        EditorView.decorations.of((view) => view.plugin(p)?.tokens ?? Decoration.none),
      ],
    },
  )

  return [plugin]
}

function buildMarkDecorations(view: EditorView, marks: MarkPattern[]): DecorationSet {
  const builder = new RangeSetBuilder<Decoration>()
  for (const { from, to } of view.visibleRanges) {
    let pos = from
    while (pos <= to) {
      const line = view.state.doc.lineAt(pos)
      const matches = findMarkMatches(line.text, marks).sort((a, b) => a.matchStart - b.matchStart)
      for (const match of matches) {
        const start = line.from + match.matchStart
        const end = start + match.matchLength
        builder.add(start, end, Decoration.mark({ class: `cm-mark-${match.colorIndex}` }))
      }
      pos = line.to + 1
    }
  }
  return builder.finish()
}

/** Notepad++'s "Mark" feature: one or more ad hoc, session-only patterns
 * highlighted at once, each in its own color (mark-highlighting.ts) --
 * distinct from severityHighlighting's admin-configured, level-based set.
 * Same single-ViewPlugin/token-only shape as severityHighlighting's `tokens`
 * plugin (marks never tint a whole line). The marks are captured at
 * construction time, like every other content-dependent extension here:
 * when the active mark list changes, CodeMirrorPane reconfigures the marks
 * compartment with a new instance. */
export function markHighlighting(marks: MarkPattern[]) {
  return ViewPlugin.fromClass(
    class {
      decorations: DecorationSet
      constructor(view: EditorView) {
        this.decorations = buildMarkDecorations(view, marks)
      }
      update(update: ViewUpdate) {
        if (update.docChanged || update.viewportChanged) {
          this.decorations = buildMarkDecorations(update.view, marks)
        }
      }
    },
    { decorations: (v) => v.decorations },
  )
}

function buildRedactionDecorations(view: EditorView): DecorationSet {
  const builder = new RangeSetBuilder<Decoration>()
  const mark = Decoration.mark({ class: 'cm-redacted' })
  for (const { from, to } of view.visibleRanges) {
    let pos = from
    while (pos <= to) {
      const line = view.state.doc.lineAt(pos)
      for (const match of findRedactionMarkers(line.text)) {
        builder.add(line.from + match.start, line.from + match.end, mark)
      }
      pos = line.to + 1
    }
  }
  return builder.finish()
}

/** Shows the `[redacted: ...]` markers the server put in place of
 * redacted values (#382, #249) apart from the log's own text. Display
 * only: the values were removed on the server. */
export const redactionHighlighting = ViewPlugin.fromClass(
  class {
    decorations: DecorationSet
    constructor(view: EditorView) {
      this.decorations = buildRedactionDecorations(view)
    }
    update(update: ViewUpdate) {
      if (update.docChanged || update.viewportChanged) {
        this.decorations = buildRedactionDecorations(update.view)
      }
    }
  },
  { decorations: (v) => v.decorations },
)

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
 * at construction time" pattern as severityHighlighting's patterns (when
 * the text or the toggle changes, CodeMirrorPane reconfigures the
 * whitespace compartment with a new instance). A
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

/** Pure client-side/session bookmarks (docs/design-decisions.md, "Scope
 * and principles": never written to the file or persisted server-side) -- `bookmarks` is a
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

/** Per-file-type syntax highlighting (docs/design-decisions.md, "Live
 * browsing, scratch and the viewer"), picked from the open file's own extension
 * (see file-language.ts) -- `@codemirror/lang-json`/`lang-xml`/
 * `lang-javascript` were already-installed dependencies, unused anywhere
 * in the codebase until now. Returns `[]` for anything unrecognized (most
 * log files), same "highlighting is additive, never required" spirit as
 * severityHighlighting -- a file with no matching language still opens
 * and displays exactly as before. */
export function languageExtension(language: FileLanguage) {
  switch (language) {
    case 'json':
      return [json()]
    case 'xml':
      return [xml()]
    case 'javascript':
      return [javascript()]
    default:
      return []
  }
}
