<script lang="ts">
  import { onMount, onDestroy } from 'svelte'
  import { EditorView, basicSetup } from 'codemirror'
  import { EditorState } from '@codemirror/state'
  import { darkTheme, logLevelHighlighting } from '../codemirror-theme'

  export let content = ''

  let host: HTMLDivElement
  let view: EditorView | null = null

  function extensions() {
    return [
      basicSetup,
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
