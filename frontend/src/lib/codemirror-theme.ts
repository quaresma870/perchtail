import { javascript } from '@codemirror/lang-javascript'
import { json } from '@codemirror/lang-json'
import { xml } from '@codemirror/lang-xml'
import { RangeSetBuilder } from '@codemirror/state'
import {
  Decoration,
  type DecorationSet,
  EditorView,
  ViewPlugin,
  type ViewUpdate,
} from '@codemirror/view'
import type { FileLanguage } from './file-language'
import { findMatchesInLine, LEVEL_CLASS } from './severity-highlighting'
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

/** Per-file-type syntax highlighting (ROADMAP.md's "Viewer: toward an
 * advanced editor" section), picked from the open file's own extension
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
