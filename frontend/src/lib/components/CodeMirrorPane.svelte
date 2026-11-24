<script module lang="ts">
  import { Compartment } from '@codemirror/state'
  import { search } from '@codemirror/search'
  import type { EditorView as View } from 'codemirror'
  import type { EditorPart } from '../editor-sync'

  // Saved editor states (lib/editor-states.ts) outlive the pane that made
  // them, so whatever they hold is shared by every pane: the compartments,
  // and the hidden search panel, which finds its pane through the view.
  const compartments: Record<EditorPart, Compartment> = {
    severity: new Compartment(),
    language: new Compartment(),
    wrap: new Compartment(),
    whitespace: new Compartment(),
    bookmarks: new Compartment(),
    marks: new Compartment(),
  }

  interface PanelOwner {
    mounted(): void
    destroyed(): void
  }
  const panelOwners = new WeakMap<View, PanelOwner>()

  // The find bar lives in the viewer, outside the editor: a bar inside it
  // would go with every rebuild or tab change. CodeMirror only highlights
  // matches while its search panel is open, so the panel it opens is an
  // empty, hidden one.
  const hiddenSearchPanel = search({
    createPanel: (view) => {
      const dom = document.createElement('div')
      dom.className = 'find-highlights-only'
      dom.hidden = true
      return {
        dom,
        mount: () => panelOwners.get(view)?.mounted(),
        destroy: () => panelOwners.get(view)?.destroyed(),
      }
    },
  })
</script>

<script lang="ts">
  import { createEventDispatcher, onMount, onDestroy } from 'svelte'
  import { EditorView, basicSetup } from 'codemirror'
  import { EditorState, type Extension } from '@codemirror/state'
  import {
    closeSearchPanel,
    findNext as cmFindNext,
    findPrevious as cmFindPrevious,
    getSearchQuery,
    openSearchPanel,
    searchPanelOpen,
    SearchQuery,
    setSearchQuery,
  } from '@codemirror/search'
  import {
    bookmarkHighlighting,
    darkTheme,
    languageExtension,
    markHighlighting,
    redactionHighlighting,
    severityHighlighting,
    whitespaceHighlighting,
  } from '../codemirror-theme'
  import { formatLinesWithNumbers } from '../copy-lines'
  import { createEditorState, getEditorState, saveEditorState, type SavedEditorState } from '../editor-states'
  import { planSync, type EditorInputs, type SyncPlan } from '../editor-sync'
  import { languageForFilename } from '../file-language'
  import { nextLine, previousLine } from '../line-cycle'
  import type { MarkPattern } from '../mark-highlighting'
  import { findProblemLines, nextProblemLine, previousProblemLine } from '../severity-highlighting'
  import type { SeverityPattern } from '../types'
  import { compileFind, type FindSpec } from '../find-in-document'
  import { contextMenuIntent, isMenuKey, type LineMenuRequest } from '../context-menu'

  export let content = ''
  /** The tab whose file this is: its editor state is kept under this key
   * (lib/editor-states.ts) while another tab is shown or the pane is gone. */
  export let stateKey = ''
  /** The display format (`content` is already formatted): a change
   * rebuilds the document. */
  export let format = 'none'
  export let severityPatterns: SeverityPattern[] = []
  export let filename = ''
  export let wrapEnabled = false
  export let showWhitespace = false
  export let bookmarks: number[] = []
  export let markPatterns: MarkPattern[] = []
  /** The find bar's search (#299), or null while it's closed: highlighted
   * in the editor and used by next/previous (Enter, F3). */
  export let find: FindSpec | null = null
  /** False while the tab holds only the end of its file: its line numbers
   * wouldn't be the file's, so the gutter hides them. */
  export let lineNumbers = true

  let host: HTMLDivElement
  let view: EditorView | null = null
  // `bottom`: the reader scrolled back to the end (the viewer clears its
  // "N new lines" count, #254).
  // `findclosed`: the editor closed its search (Escape in the editor).
  // `menu`: a right-click, the Menu key or Shift+F10 in the editor (#287):
  // where to open the menu and the line it is for.
  const dispatch = createEventDispatcher<{ bottom: void; findclosed: void; menu: LineMenuRequest }>()

  let lastKeyMenu: number | null = null

  // A right-click on a line outside the selection puts the cursor on that
  // line first, so the menu's actions are for the line clicked.
  function onContextMenu(event: MouseEvent) {
    if (!view) return
    const intent = contextMenuIntent(event, lastKeyMenu, Date.now())
    if (intent === 'browser') return
    event.preventDefault()
    if (intent === 'ignore') return
    const pos = view.posAtCoords({ x: event.clientX, y: event.clientY })
    const at = pos === null ? view.state.selection.main.head : pos
    const line = view.state.doc.lineAt(at)
    const { from, to } = view.state.selection.main
    const covers = from !== to && from <= line.to && to >= line.from
    if (!covers) view.dispatch({ selection: { anchor: line.from } })
    view.scrollDOM.focus({ preventScroll: true })
    dispatch('menu', menuRequest(event.clientX, event.clientY, line.number))
  }

  // The browser's right mousedown would move the text caret to the click,
  // and the read-only editor takes the selection from it: the selection a
  // menu action is for would be gone. Shift+right-click is the browser's.
  function onMouseDown(event: MouseEvent) {
    if (event.button === 2 && !event.shiftKey) event.preventDefault()
  }

  // The read-only content can't take the focus, and CodeMirror takes it
  // from what had it on a click: the scroller gets it, so the Menu key and
  // Shift+F10 reach the editor.
  function onMouseUp(event: MouseEvent) {
    if (!view || event.button !== 0) return
    if (!view.dom.contains(document.activeElement)) view.scrollDOM.focus({ preventScroll: true })
  }

  function onKeydown(event: KeyboardEvent) {
    if (!view || !isMenuKey(event)) return
    event.preventDefault()
    lastKeyMenu = Date.now()
    const head = view.state.selection.main.head
    const coords = view.coordsAtPos(head)
    const box = view.contentDOM.getBoundingClientRect()
    const x = coords ? coords.left : box.left
    const y = coords ? coords.bottom : box.top
    dispatch('menu', menuRequest(x, y, view.state.doc.lineAt(head).number))
  }

  function menuRequest(x: number, y: number, line: number): LineMenuRequest {
    const { from, to } = view!.state.selection.main
    const text = view!.state.sliceDoc(from, to)
    return {
      x,
      y,
      line,
      selection: text,
      // The read-only content can't take the focus; its scroller does
      // (clicking the text focuses it).
      opener: view!.scrollDOM,
    }
  }

  let applyingFind = false
  const panelOwner: PanelOwner = {
    // CodeMirror's own keys (Mod-f, F3) can open it with no search in the
    // bar; it closes again rather than highlight a stray query.
    mounted: () => {
      if (!applyingFind && !find) queueMicrotask(closeStraySearch)
    },
    destroyed: () => {
      if (!applyingFind && !destroying) dispatch('findclosed')
    },
  }

  // What each compartment holds, from the current props.
  function partExtension(part: EditorPart): Extension {
    switch (part) {
      case 'severity':
        return severityHighlighting(severityPatterns)
      case 'language':
        return [
          languageExtension(languageForFilename(filename)),
          // The content element has role="textbox" and needs a name (#377).
          EditorView.contentAttributes.of({
            'aria-label': filename ? `Contents of ${filename}` : 'File contents',
          }),
        ]
      case 'wrap':
        return wrapEnabled ? EditorView.lineWrapping : []
      case 'whitespace':
        return showWhitespace ? whitespaceHighlighting(content) : []
      case 'bookmarks':
        return bookmarkHighlighting(bookmarks)
      case 'marks':
        return markHighlighting(markPatterns)
    }
  }

  function extensions(): Extension[] {
    return [
      basicSetup,
      // basicSetup only wires the search *keymap* (Mod-f etc.), not this
      // extension itself — without it the panel has no state to open into.
      hiddenSearchPanel,
      darkTheme,
      compartments.severity.of(partExtension('severity')),
      compartments.bookmarks.of(partExtension('bookmarks')),
      compartments.marks.of(partExtension('marks')),
      redactionHighlighting,
      compartments.language.of(partExtension('language')),
      compartments.wrap.of(partExtension('wrap')),
      compartments.whitespace.of(partExtension('whitespace')),
      EditorView.editable.of(false),
      EditorState.readOnly.of(true),
    ]
  }

  function currentInputs(): EditorInputs {
    return { content, format, severityPatterns, filename, wrapEnabled, showWhitespace, bookmarks, markPatterns }
  }

  // What the editor shows now, and for which tab.
  let applied: EditorInputs | null = null
  let appliedKey: string | null = null

  // Distance from the bottom (px) within which the view still counts as
  // "at the bottom" -- exact 0 would require pixel-perfect scrolling to
  // ever re-trigger, which a real user's scroll wheel/trackpad rarely
  // lands on.
  const BOTTOM_THRESHOLD_PX = 48

  function isNearBottom(v: EditorView): boolean {
    const { scrollHeight, clientHeight, scrollTop } = v.scrollDOM
    return scrollHeight - clientHeight - scrollTop < BOTTOM_THRESHOLD_PX
  }

  // Text added at the end or at the start and compartments that changed,
  // in one transaction: the document, the reader's place and selection
  // stay. Text put above what the reader sees ("Load earlier") pushes it
  // down, even from the very top: the line at the top of the screen is
  // noted before, and the scroll moves by as much as that line moved.
  function applyPlan(v: EditorView, plan: SyncPlan, from: EditorInputs) {
    const effects = plan.reconfigure.map((part) => compartments[part].reconfigure(partExtension(part)))
    let changes: { from: number; insert: string } | undefined
    if (plan.action === 'append') changes = { from: v.state.doc.length, insert: content.slice(from.content.length) }
    else if (plan.action === 'prepend') changes = { from: 0, insert: content.slice(0, content.length - from.content.length) }
    if (!changes && effects.length === 0) return
    if (plan.action !== 'prepend' || !changes || !v.dom.isConnected) {
      v.dispatch({ changes, effects })
      return
    }
    const scrollTop = v.scrollDOM.scrollTop
    const line = v.lineBlockAtHeight(scrollTop)
    const moved = line.from + changes.insert.length
    v.dispatch({ changes, effects })
    v.requestMeasure({
      read: (view) => view.lineBlockAt(moved).top - line.top,
      write: (shift, view) => {
        view.scrollDOM.scrollTop = scrollTop + shift
      },
    })
  }

  // Scrolls now and again once CodeMirror has measured the document, which
  // a state just put in place hasn't been yet.
  function restoreScroll(v: EditorView, top: number | 'bottom') {
    const apply = () => {
      if (v.dom.isConnected) v.scrollDOM.scrollTop = top === 'bottom' ? v.scrollDOM.scrollHeight : top
    }
    apply()
    v.requestMeasure({ read: () => null, write: apply })
  }

  // Where the shown tab was last scrolled to. When the Viewer leaves the
  // page, its elements are gone before the pane is destroyed and can't say
  // any more.
  type ScrollPlace = Pick<SavedEditorState, 'scrollTop' | 'scrollSnapshot' | 'atBottom'>
  let lastPlace: ScrollPlace | null = null
  function placeOf(v: EditorView): ScrollPlace {
    return { scrollTop: v.scrollDOM.scrollTop, scrollSnapshot: v.scrollSnapshot(), atBottom: isNearBottom(v) }
  }

  // Keeps the shown tab's editor for when it comes back.
  function saveShown() {
    if (!view || appliedKey === null || applied === null) return
    const place = view.dom.isConnected
      ? placeOf(view)
      : (lastPlace ?? getEditorState(appliedKey) ?? { scrollTop: 0, atBottom: false })
    saveEditorState(appliedKey, {
      state: view.state,
      content: applied.content,
      scrollTop: place.scrollTop,
      scrollSnapshot: place.scrollSnapshot,
      atBottom: place.atBottom,
      inputs: applied,
    })
  }

  // Shows the tab `stateKey`: the editor state it had when it was last
  // shown, brought up to date (appended text, compartments that changed
  // meanwhile), or a new one only when there is none or its text changed
  // otherwise.
  function showTab() {
    const saved = getEditorState(stateKey)
    const next = currentInputs()
    const plan = saved ? planSync(saved.inputs, next) : null
    const reuse = saved !== undefined && plan !== null && plan.action !== 'rebuild'
    const state = reuse ? saved.state : createEditorState({ doc: content, extensions: extensions() })
    // Swapping the state drops the search panel; that's not the reader
    // closing it.
    applyingFind = true
    try {
      if (view) {
        view.setState(state)
      } else {
        view = new EditorView({ state, parent: host })
        panelOwners.set(view, panelOwner)
      }
      if (reuse) applyPlan(view, plan, saved.inputs)
    } finally {
      applyingFind = false
    }
    applied = next
    appliedKey = stateKey
    lastPlace = null
    appliedFind = undefined
    applyFind()
    if (!saved) {
      // A new file starts at the top. As a CodeMirror scroll, so a jump
      // asked for right after (a search hit's line) wins.
      view.dispatch({ effects: EditorView.scrollIntoView(0, { y: 'start' }) })
    } else if (saved.atBottom) {
      restoreScroll(view, 'bottom')
    } else if (saved.scrollSnapshot) {
      // As a document position: the lines' heights are only known once the
      // view has measured them.
      view.dispatch({ effects: saved.scrollSnapshot })
    } else {
      restoreScroll(view, saved.scrollTop)
    }
  }

  // Brings the editor up to date with the props. The document is built
  // from the whole text only when it's new or its text changed other than
  // by text appended at the end (lib/editor-sync.ts); everything else --
  // severity patterns, language, Wrap, Show chars, bookmarks, highlights --
  // is a compartment reconfigured in place, keeping the reader's place.
  function syncView() {
    if (!view) return
    if (stateKey !== appliedKey) {
      saveShown()
      showTab()
      return
    }
    const next = currentInputs()
    const plan = planSync(applied, next)
    if (plan.action === 'none') return
    const wasAtBottom = isNearBottom(view)
    if (plan.action === 'rebuild') {
      // A Reload, a Tail that dropped lines from the top, a format change:
      // the place is kept, or the end if the reader was there.
      const previousScrollTop = view.scrollDOM.scrollTop
      applyingFind = true
      try {
        view.setState(createEditorState({ doc: content, extensions: extensions() }))
      } finally {
        applyingFind = false
      }
      appliedFind = undefined
      applyFind()
      view.scrollDOM.scrollTop = wasAtBottom ? view.scrollDOM.scrollHeight : previousScrollTop
    } else {
      // Incremental follow (#254): appended text goes in as one transaction.
      applyPlan(view, plan, applied ?? next)
      if (plan.action === 'append' && wasAtBottom) view.scrollDOM.scrollTop = view.scrollDOM.scrollHeight
    }
    applied = next
  }

  onMount(() => {
    showTab()
    if (!view) return
    let wasAtBottom = true
    view.scrollDOM.addEventListener('scroll', () => {
      if (view?.dom.isConnected) lastPlace = placeOf(view)
      const atBottom = view !== null && isNearBottom(view)
      if (atBottom && !wasAtBottom) dispatch('bottom')
      wasAtBottom = atBottom
    })
  })

  // Applies `find` to the editor: opens the hidden panel and sets the
  // query, or closes it. Also run after every state swap, which drops both
  // or brings back a tab's own.
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
    stateKey,
    format,
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

  // "Next/previous problem" step command (docs/design-decisions.md,
  // "Frontend and UI", severity patterns): steps through lines matching a navigation-eligible
  // severity pattern, wrapping around at either end. Uses the cursor's
  // current line as the starting point, same as an IDE's "next diagnostic".
  // The lines are found in the severity worker, so the step lands a moment
  // later, from wherever the cursor is by then.
  async function jumpToProblem(step: typeof nextProblemLine | typeof previousProblemLine) {
    if (!view) return
    const problemLines = await findProblemLines(view.state.doc.toString(), severityPatterns)
    if (!view) return
    const currentLine = view.state.doc.lineAt(view.state.selection.main.head).number
    const target = step(problemLines, currentLine)
    if (target !== null) scrollToLine(target)
  }

  export function jumpToNextProblem() {
    void jumpToProblem(nextProblemLine)
  }

  export function jumpToPreviousProblem() {
    void jumpToProblem(previousProblemLine)
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
  /** The menu's Copy: the selection, or the cursor's line when nothing is
   * selected. */
  export async function copySelectionOrLine(): Promise<void> {
    if (!view) return
    const { from, to, head } = view.state.selection.main
    const text = from === to ? view.state.doc.lineAt(head).text : view.state.sliceDoc(from, to)
    await navigator.clipboard.writeText(text)
  }

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

  // The editor going away (Compare, another page, the last tab closing)
  // isn't the reader closing the search. The shown tab's state is kept for
  // when it comes back.
  let destroying = false
  onDestroy(() => {
    destroying = true
    saveShown()
    view?.destroy()
  })
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div
  class="cm-host"
  class:no-line-numbers={!lineNumbers}
  bind:this={host}
  on:contextmenu={onContextMenu}
  on:mousedown={onMouseDown}
  on:mouseup={onMouseUp}
  on:keydown={onKeydown}
></div>

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
  /* CodeMirror's own gutter style is display: flex !important. */
  .no-line-numbers :global(.cm-lineNumbers) {
    display: none !important;
  }
</style>
