import { RangeSetBuilder } from '@codemirror/state'
import {
  Decoration,
  type DecorationSet,
  EditorView,
  MatchDecorator,
  ViewPlugin,
  type ViewUpdate,
} from '@codemirror/view'

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

const LEVEL_TOKEN = /\[(info|warn|warning|error|fatal|debug|trace)\]/gi

const LEVEL_CLASS: Record<string, string> = {
  info: 'cm-level-info',
  warn: 'cm-level-warn',
  warning: 'cm-level-warn',
  error: 'cm-level-error',
  fatal: 'cm-level-error',
  debug: 'cm-level-debug',
  trace: 'cm-level-debug',
}

const tokenDecorator = new MatchDecorator({
  regexp: LEVEL_TOKEN,
  decoration: (match) => Decoration.mark({ class: LEVEL_CLASS[match[1].toLowerCase()] }),
})

/** Lightweight, viewport-scoped log-level highlighting — colors `[info]`/
 * `[warn]`/`[error]`-style tokens and tints the whole line for anything
 * that looks like an error, so a scan of a log file reads the same way
 * `grep -i error` would highlight it. Not a real log-format parser: this
 * is a log *viewer*, not a language server, per CLAUDE.md's reasoning for
 * choosing CodeMirror over Monaco in the first place. */
const logLevelTokens = ViewPlugin.fromClass(
  class {
    decorations: DecorationSet
    constructor(view: EditorView) {
      this.decorations = tokenDecorator.createDeco(view)
    }
    update(update: ViewUpdate) {
      this.decorations = tokenDecorator.updateDeco(update, this.decorations)
    }
  },
  { decorations: (v) => v.decorations },
)

const ERROR_LINE = /\b(error|fatal)\b/i

function buildLineDecorations(view: EditorView): DecorationSet {
  const builder = new RangeSetBuilder<Decoration>()
  for (const { from, to } of view.visibleRanges) {
    let pos = from
    while (pos <= to) {
      const line = view.state.doc.lineAt(pos)
      if (ERROR_LINE.test(line.text)) {
        builder.add(line.from, line.from, Decoration.line({ class: 'cm-line-error' }))
      }
      pos = line.to + 1
    }
  }
  return builder.finish()
}

const logLevelLines = ViewPlugin.fromClass(
  class {
    decorations: DecorationSet
    constructor(view: EditorView) {
      this.decorations = buildLineDecorations(view)
    }
    update(update: ViewUpdate) {
      if (update.docChanged || update.viewportChanged) {
        this.decorations = buildLineDecorations(update.view)
      }
    }
  },
  { decorations: (v) => v.decorations },
)

export const logLevelHighlighting = [logLevelLines, logLevelTokens]
