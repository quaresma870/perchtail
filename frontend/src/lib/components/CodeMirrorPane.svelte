<script lang="ts">
  import { onMount, onDestroy } from 'svelte'
  import { EditorView, basicSetup } from 'codemirror'
  import { EditorState } from '@codemirror/state'
  import { search, openSearchPanel } from '@codemirror/search'
  import { darkTheme, severityHighlighting } from '../codemirror-theme'
  import { findProblemLines, nextProblemLine, previousProblemLine } from '../severity-highlighting'
  import type { SeverityPattern } from '../types'

  export let content = ''
  export let severityPatterns: SeverityPattern[] = []

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
      EditorView.editable.of(false),
      EditorState.readOnly.of(true),
    ]
  }

  let appliedContent: string | null = null
  let appliedPatterns: SeverityPattern[] | null = null

  // Rebuilds the editor state whenever either the open file's content or
  // the effective severity-pattern set changes -- the two can change
  // independently (e.g. the pattern set finishes loading after the file is
  // already open), so both are tracked rather than only reacting to content.
  function syncView() {
    if (!view) return
    if (content === appliedContent && severityPatterns === appliedPatterns) return
    view.setState(EditorState.create({ doc: content, extensions: extensions() }))
    appliedContent = content
    appliedPatterns = severityPatterns
  }

  onMount(() => {
    view = new EditorView({
      state: EditorState.create({ doc: content, extensions: extensions() }),
      parent: host,
    })
    appliedContent = content
    appliedPatterns = severityPatterns
  })

  $: content, severityPatterns, syncView()

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
