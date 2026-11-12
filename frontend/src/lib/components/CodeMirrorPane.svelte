<script lang="ts">
  import { createEventDispatcher, onMount, onDestroy } from 'svelte'
  import { EditorView, basicSetup } from 'codemirror'
  import { EditorState } from '@codemirror/state'
  import {
    closeSearchPanel,
    findNext as cmFindNext,
    findPrevious as cmFindPrevious,
    getSearchQuery,
    openSearchPanel,
    search,
    searchPanelOpen,
    SearchQuery,
    setSearchQuery,
  } from '@codemirror/search'
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
  import { compileFind, type FindSpec } from '../find-in-document'

  export let content = ''
  export let severityPatterns: SeverityPattern[] = []
  export let filename = ''
  export let wrapEnabled = false
  export let showWhitespace = false
  export let bookmarks: number[] = []
  export let markPatterns: MarkPattern[] = []
  /** The find bar's search (#299), or null while it's closed: highlighted
   * in the editor and used by next/previous (Enter, F3). */
  export let find: FindSpec | null = null

  let host: HTMLDivElement
  let view: EditorView | null = null
  // `bottom`: the reader scrolled back to the end (the viewer clears its
  // "N new lines" count, #254).
  // `findclosed`: the editor closed its search (Escape in the editor).
  const dispatch = createEventDispatcher<{ bottom: void; findclosed: void }>()

  // The find bar lives in the viewer, outside the editor: the editor is
  // rebuilt on every Wrap, Highlight or tab change, which would close a bar
  // inside it. CodeMirror only highlights matches while its search panel is
  // open, so the panel it opens is an empty, hidden one.
  let applyingFind = false
  const hiddenSearchPanel = search({
    createPanel: () => {
      const dom = document.createElement('div')
      dom.className = 'find-highlights-only'
      dom.hidden = true
      return {
        dom,
        // CodeMirror's own keys (Mod-f, F3) can open it with no search in
        // the bar; it closes again rather than highlight a stray query.
        mount: () => {
          if (!applyingFind && !find) queueMicrotask(closeStraySearch)
        },
        destroy: () => {
          if (!applyingFind && !destroying) dispatch('findclosed')
        },
      }
    },
  })

  function extensions() {
    return [
      basicSetup,
      // basicSetup only wires the search *keymap* (Mod-f etc.), not this
      // extension itself — without it the panel has no state to open into.
      hiddenSearchPanel,
      darkTheme,
      severityHighlighting(severityPatterns),
      bookmarkHighlighting(bookmarks),
      markHighlighting(markPatterns),
      languageExtension(languageForFilename(filename)),
      ...(wrapEnabled ? [EditorView.lineWrapping] : []),
      ...(showWhitespace ? [whitespaceHighlighting(content)] : []),
      EditorView.editable.of(false),
      EditorState.readOnly.of(true),
      // The content element has role="textbox" and needs a name (#377).
      EditorView.contentAttributes.of({
        'aria-label': filename ? `Contents of ${filename}` : 'File contents',
      }),
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
  // True when the only change is text appended to the content already shown
  // (and Show whitespace, which scans the content, is off).
  function appendOnly(): boolean {
    return (
      appliedContent !== null &&
      content.length > appliedContent.length &&
      severityPatterns === appliedPatterns &&
      filename === appliedFilename &&
      wrapEnabled === appliedWrap &&
      !showWhitespace &&
      showWhitespace === appliedShowWhitespace &&
      bookmarks === appliedBookmarks &&
      markPatterns === appliedMarkPatterns &&
      content.startsWith(appliedContent)
    )
  }

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
    if (appendOnly()) {
      // Incremental follow (#254): only text was added at the end, so it
      // goes in as one transaction -- no rebuild, the reader's place and
      // selection stay where they are.
      view.dispatch({
        changes: { from: view.state.doc.length, insert: content.slice((appliedContent ?? '').length) },
      })
      if (wasAtBottom) view.scrollDOM.scrollTop = view.scrollDOM.scrollHeight
      appliedContent = content
      return
    }
    const previousScrollTop = view.scrollDOM.scrollTop
    // The rebuild drops the search panel; that's not the reader closing it.
    applyingFind = true
    try {
      view.setState(EditorState.create({ doc: content, extensions: extensions() }))
    } finally {
      applyingFind = false
    }
    appliedFind = undefined
    applyFind()
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
    applyFind()
    let wasAtBottom = true
    view.scrollDOM.addEventListener('scroll', () => {
      const atBottom = view !== null && isNearBottom(view)
      if (atBottom && !wasAtBottom) dispatch('bottom')
      wasAtBottom = atBottom
    })
    appliedContent = content
    appliedPatterns = severityPatterns
    appliedFilename = filename
    appliedWrap = wrapEnabled
    appliedShowWhitespace = showWhitespace
    appliedBookmarks = bookmarks
    appliedMarkPatterns = markPatterns
  })

  // Applies `find` to the editor: opens the hidden panel and sets the
  // query, or closes it. Also run after every rebuild, which drops both.
  let appliedFind: FindSpec | null | undefined = undefined
  function closeStraySearch() {
    if (!view || find || !searchPanelOpen(view.state)) return
    applyingFind = true
    try {
      closeSearchPanel(view)
    } finally {
      applyingFind = false
    }
  }
  function applyFind() {
    if (!view || find === appliedFind) return
    applyingFind = true
    try {
      if (find) {
        if (!searchPanelOpen(view.state)) openSearchPanel(view)
        const query = new SearchQuery({
          // The same regex the bar counts and Find all lists (compileFind):
          // text and whole word are already turned into it, so CodeMirror
          // highlights exactly what's counted. An invalid one highlights
          // nothing, as the bar says "Invalid regex".
          search: compileFind(find.query, find)?.source ?? '(',
          caseSensitive: find.caseSensitive,
          regexp: true,
        })
        if (!query.eq(getSearchQuery(view.state))) view.dispatch({ effects: setSearchQuery.of(query) })
      } else if (searchPanelOpen(view.state)) {
        closeSearchPanel(view)
      }
    } finally {
      applyingFind = false
    }
    appliedFind = find
  }
  $: find, applyFind()

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

  export function isAtBottom(): boolean {
    return view === null || isNearBottom(view)
  }

  export function scrollToEnd() {
    if (!view) return
    view.scrollDOM.scrollTop = view.scrollDOM.scrollHeight
  }

  /** Moves to the next / previous match of `find`, wrapping around;
   * false when there is none. */
  export function findNext(): boolean {
    return view !== null && cmFindNext(view)
  }

  export function findPrevious(): boolean {
    return view !== null && cmFindPrevious(view)
  }

  /** Where the selection starts (1-based line, 0-based column), for the
   * find bar's "3 of 120". */
  export function selectionStart(): { line: number; column: number } | null {
    if (!view) return null
    const from = view.state.selection.main.from
    const line = view.state.doc.lineAt(from)
    return { line: line.number, column: from - line.from }
  }

  /** The selected text when it's on one line: what the find bar starts with. */
  export function selectedText(): string {
    if (!view) return ''
    const { from, to } = view.state.selection.main
    const text = view.state.sliceDoc(from, to)
    return text.includes('\n') ? '' : text
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

  // The editor going away (Compare, closing the tab) isn't the reader
  // closing the search.
  let destroying = false
  onDestroy(() => {
    destroying = true
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
