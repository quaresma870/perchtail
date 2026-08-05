<script lang="ts">
  import { onMount, onDestroy } from 'svelte'
  import { EditorView, basicSetup } from 'codemirror'
  import { EditorState } from '@codemirror/state'
  import { search, openSearchPanel } from '@codemirror/search'
  import { darkTheme, logLevelHighlighting } from '../codemirror-theme'

  export let content = ''

  let host: HTMLDivElement
  let view: EditorView | null = null

  function extensions() {
    return [
      basicSetup,
      // basicSetup only wires the search *keymap* (Mod-f etc.), not this
      // extension itself — without it the panel has no state to open into.
      search(),
      darkTheme,
      logLevelHighlighting,
      EditorView.editable.of(false),
      EditorState.readOnly.of(true),
    ]
  }

  onMount(() => {
    view = new EditorView({
      state: EditorState.create({ doc: content, extensions: extensions() }),
      parent: host,
    })
  })

  $: if (view && content !== view.state.doc.toString()) {
    view.setState(EditorState.create({ doc: content, extensions: extensions() }))
  }

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
