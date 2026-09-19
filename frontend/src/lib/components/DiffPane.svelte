<script lang="ts">
  import { onDestroy, onMount } from 'svelte'
  import { basicSetup } from 'codemirror'
  import { EditorState } from '@codemirror/state'
  import { EditorView } from '@codemirror/view'
  import { MergeView } from '@codemirror/merge'
  import { darkTheme, languageExtension } from '../codemirror-theme'
  import { languageForFilename } from '../file-language'

  export let leftContent = ''
  export let rightContent = ''
  export let leftLabel = ''
  export let rightLabel = ''

  let host: HTMLDivElement
  let view: MergeView | null = null

  // Read-only on both sides -- ROADMAP.md's "Compare files" item is a
  // viewer feature, not an editor; this tool never writes back to a
  // source, and a diff of two already-fetched snapshots is no exception.
  function sideExtensions(filename: string) {
    return [
      basicSetup,
      darkTheme,
      languageExtension(languageForFilename(filename)),
      EditorView.lineWrapping,
      EditorView.editable.of(false),
      EditorState.readOnly.of(true),
    ]
  }

  // Content/labels are a one-shot snapshot from whenever "Compare" picked
  // its target (Viewer.svelte re-mounts this component via `{#key}` for a
  // new comparison rather than reactively updating props), so this only
  // ever needs to build the view once.
  onMount(() => {
    view = new MergeView({
      a: { doc: leftContent, extensions: sideExtensions(leftLabel) },
      b: { doc: rightContent, extensions: sideExtensions(rightLabel) },
      parent: host,
      gutter: true,
      highlightChanges: true,
    })
  })

  onDestroy(() => {
    view?.destroy()
  })
</script>

<div class="diff-labels">
  <span class="diff-label">{leftLabel}</span>
  <span class="diff-label">{rightLabel}</span>
</div>
<div class="diff-host" bind:this={host}></div>

<style>
  .diff-labels {
    display: flex;
  }
  .diff-label {
    flex: 1;
    min-width: 0;
    padding: 0.3rem 0.9rem;
    font-size: 0.78rem;
    color: var(--text-muted);
    background: var(--bg-elevated);
    border-bottom: 1px solid var(--border-soft);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .diff-host {
    flex: 1;
    min-height: 0;
    overflow: auto;
  }
  .diff-host :global(.cm-mergeView) {
    height: 100%;
  }
  .diff-host :global(.cm-editor) {
    height: 100%;
    font-size: 0.85rem;
  }
</style>
