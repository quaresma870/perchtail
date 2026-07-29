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
