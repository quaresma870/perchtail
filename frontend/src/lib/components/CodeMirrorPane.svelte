<script lang="ts">
  import { onMount, onDestroy } from 'svelte'
  import { EditorView, basicSetup } from 'codemirror'
  import { EditorState } from '@codemirror/state'
  import { search, openSearchPanel } from '@codemirror/search'
  import {
    bookmarkHighlighting,
    darkTheme,
    languageExtension,
    markHighlighting,
    severityHighlighting,
    whitespaceHighlighting,
  } from '../codemirror-theme'
  import { formatLinesWithNumbers } from '../copy-lines'
  import { languageForFilename } from '../file-language'
  import { nextLine, previousLine } from '../line-cycle'
  import type { MarkPattern } from '../mark-highlighting'
  import { findProblemLines, nextProblemLine, previousProblemLine } from '../severity-highlighting'
  import type { SeverityPattern } from '../types'

  export let content = ''
  export let severityPatterns: SeverityPattern[] = []
  export let filename = ''
  export let wrapEnabled = false
  export let showWhitespace = false
  export let bookmarks: number[] = []
  export let markPatterns: MarkPattern[] = []

  let host: HTMLDivElement
  let view: EditorView | null = null

  function extensions() {
    return [
      basicSetup,
      // basicSetup only wires the search *keymap* (Mod-f etc.), not this
      // extension itself — without it the panel has no state to open into.
      search(),
      darkTheme,
      severityHighlighting(severityPatterns),
      bookmarkHighlighting(bookmarks),
      markHighlighting(markPatterns),
      languageExtension(languageForFilename(filename)),
      ...(wrapEnabled ? [EditorView.lineWrapping] : []),
      ...(showWhitespace ? [whitespaceHighlighting(content)] : []),
      EditorView.editable.of(false),
      EditorState.readOnly.of(true),
    ]
  }

  let appliedContent: string | null = null
  let appliedPatterns: SeverityPattern[] | null = null
  let appliedFilename: string | null = null
  let appliedWrap: boolean | null = null
  let appliedShowWhitespace: boolean | null = null
  let appliedBookmarks: number[] | null = null
  let appliedMarkPatterns: MarkPattern[] | null = null

  // Distance from the bottom (px) within which the view still counts as
  // "at the bottom" -- exact 0 would require pixel-perfect scrolling to
  // ever re-trigger, which a real user's scroll wheel/trackpad rarely
  // lands on.
  const BOTTOM_THRESHOLD_PX = 48

  function isNearBottom(v: EditorView): boolean {
    const { scrollHeight, clientHeight, scrollTop } = v.scrollDOM
    return scrollHeight - clientHeight - scrollTop < BOTTOM_THRESHOLD_PX
  }

  // Rebuilds the editor state whenever any of content, the effective
  // severity-pattern set, the filename (-> language), wrap/show-whitespace
  // toggles, the bookmark list, or the mark-pattern list changes -- these
  // can all change independently of each other (e.g. the pattern set
  // finishes loading after the file is already open), so each is tracked
  // rather than only reacting to content.
  //
  // `view.setState` is a wholesale replacement, not an incremental
  // transaction, so it resets scroll to the top by default -- harmless for
  // a one-off toggle, but it would make "Follow" (tail -f) unusable: every
  // poll would yank the view back to line 1 instead of tracking new
  // content. Recorded before the swap and restored after: back to the
  // bottom if the view was already there (the common "watching it grow"
  // case), otherwise the same absolute scroll offset, so an ordinary
  // toggle (Wrap, a mark, etc.) no longer disturbs a reader's place either.
  function syncView() {
    if (!view) return
    if (
      content === appliedContent &&
      severityPatterns === appliedPatterns &&
      filename === appliedFilename &&
      wrapEnabled === appliedWrap &&
      showWhitespace === appliedShowWhitespace &&
      bookmarks === appliedBookmarks &&
      markPatterns === appliedMarkPatterns
    ) {
      return
    }
    const wasAtBottom = isNearBottom(view)
    const previousScrollTop = view.scrollDOM.scrollTop
    view.setState(EditorState.create({ doc: content, extensions: extensions() }))
    view.scrollDOM.scrollTop = wasAtBottom ? view.scrollDOM.scrollHeight : previousScrollTop
    appliedContent = content
    appliedPatterns = severityPatterns
    appliedFilename = filename
    appliedWrap = wrapEnabled
    appliedShowWhitespace = showWhitespace
    appliedBookmarks = bookmarks
    appliedMarkPatterns = markPatterns
  }

  onMount(() => {
    view = new EditorView({
      state: EditorState.create({ doc: content, extensions: extensions() }),
      parent: host,
    })
    appliedContent = content
    appliedPatterns = severityPatterns
    appliedFilename = filename
    appliedWrap = wrapEnabled
    appliedShowWhitespace = showWhitespace
    appliedBookmarks = bookmarks
    appliedMarkPatterns = markPatterns
  })

  $: content,
    severityPatterns,
    filename,
    wrapEnabled,
    showWhitespace,
    bookmarks,
    markPatterns,
    syncView()

  // Exposed for search click-through (Search.svelte -> Viewer.svelte): jump
  // to and select a specific line, e.g. after opening a file from a search
  // hit. Called imperatively via bind:this rather than a reactive prop,
  // since it only needs to fire once per open, not on every render.
  export function scrollToLine(lineNumber: number) {
    if (!view) return
    const clamped = Math.min(Math.max(lineNumber, 1), view.state.doc.lines)
    const line = view.state.doc.line(clamped)
    view.dispatch({
      selection: { anchor: line.from, head: line.to },
      effects: EditorView.scrollIntoView(line.from, { y: 'center' }),
    })
    view.focus()
  }

  // Called from a page-level Ctrl/Cmd+F handler (Viewer.svelte) rather than
  // relying on the browser reaching CodeMirror's own searchKeymap: clicking
  // into .cm-content doesn't reliably focus it (it's not contentEditable,
  // since the pane is read-only), so a plain browser keydown on Mod-F often
  // never reaches the editor at all and falls through to the browser's own
  // find bar instead. Calling this directly sidesteps that regardless of
  // where focus currently is.
  export function openSearch() {
    if (!view) return
    openSearchPanel(view)
  }

  // "Next/previous problem" step command (ROADMAP.md's severity-indicators
  // navigation item): steps through lines matching a navigation-eligible
  // severity pattern, wrapping around at either end. Uses the cursor's
  // current line as the starting point, same as an IDE's "next diagnostic".
  function jumpToProblem(step: typeof nextProblemLine | typeof previousProblemLine) {
    if (!view) return
    const problemLines = findProblemLines(view.state.doc.toString(), severityPatterns)
    const currentLine = view.state.doc.lineAt(view.state.selection.main.head).number
    const target = step(problemLines, currentLine)
    if (target !== null) scrollToLine(target)
  }

  export function jumpToNextProblem() {
    jumpToProblem(nextProblemLine)
  }

  export function jumpToPreviousProblem() {
    jumpToProblem(previousProblemLine)
  }

  // Exposed so Viewer.svelte can toggle a bookmark on whatever line the
  // cursor is currently on, and for the "go to line" input to validate
  // against the document's actual line count.
  export function currentLine(): number {
    if (!view) return 1
    return view.state.doc.lineAt(view.state.selection.main.head).number
  }

  function jumpToBookmark(step: typeof nextLine | typeof previousLine) {
    if (!view) return
    const target = step(bookmarks, currentLine())
    if (target !== null) scrollToLine(target)
  }

  export function jumpToNextBookmark() {
    jumpToBookmark(nextLine)
  }

  export function jumpToPreviousBookmark() {
    jumpToBookmark(previousLine)
  }

  // "Copy selected lines (with line numbers)" toolbar action: every full
  // line touched by the current selection, prefixed with its line number.
  // No-op (returns false) when there's nothing selected -- the browser's
  // own Ctrl+C already handles a plain-text copy of an actual selection,
  // so this is specifically for the "with line numbers" case, not a
  // general-purpose copy replacement.
  export async function copySelectedLines(): Promise<boolean> {
    if (!view) return false
    const { from, to } = view.state.selection.main
    if (from === to) return false

    const fromLine = view.state.doc.lineAt(from).number
    const toLine = view.state.doc.lineAt(to).number
    const lines = []
    for (let n = fromLine; n <= toLine; n += 1) {
      const line = view.state.doc.line(n)
      lines.push({ number: n, text: line.text })
    }
    await navigator.clipboard.writeText(formatLinesWithNumbers(lines))
    return true
  }

  onDestroy(() => {
    view?.destroy()
  })
</script>

<div class="cm-host" bind:this={host}></div>

<style>
  .cm-host {
    flex: 1;
    min-height: 0;
    overflow: auto;
  }
  .cm-host :global(.cm-editor) {
    height: 100%;
    font-size: 0.85rem;
  }
</style>
