<script lang="ts">
  import { createEventDispatcher, onMount } from 'svelte'

  /** A popup over the page (#301), sized to its content: create, edit and
   * delete forms and confirmations in the settings. Built on <dialog>, so
   * the browser keeps focus inside it, closes it on Escape and returns
   * focus to what opened it. A click outside doesn't close it: a form
   * half-filled shouldn't be lost to a stray click. Mounted to show it;
   * `close` asks the parent to unmount it. */
  export let title: string
  /** Wider for forms with more fields; it still never exceeds the window. */
  export let wide = false

  const dispatch = createEventDispatcher<{ close: void }>()
  let dialog: HTMLDialogElement
  const titleId = `modal-title-${Math.random().toString(36).slice(2)}`

  onMount(() => {
    dialog.showModal()
    // The first field, or else the first button, gets focus.
    const first = dialog.querySelector<HTMLElement>(
      '.modal-body input:not([type=hidden]):not([disabled]), .modal-body select, .modal-body textarea, .modal-footer button',
    )
    first?.focus()
    return () => {
      if (dialog.open) dialog.close()
    }
  })

  function onCancel(event: Event) {
    // Escape: the parent decides (it unmounts the modal).
    event.preventDefault()
    dispatch('close')
  }
</script>

<dialog class="modal" class:wide bind:this={dialog} aria-labelledby={titleId} on:cancel={onCancel}>
  <div class="modal-head">
    <h2 id={titleId}>{title}</h2>
    <button class="modal-close" type="button" aria-label="Close" on:click={() => dispatch('close')}>×</button>
  </div>
  <div class="modal-body">
    <slot />
  </div>
  {#if $$slots.footer}
    <div class="modal-footer">
      <slot name="footer" />
    </div>
  {/if}
</dialog>

<style>
  .modal {
    width: min(30rem, calc(100vw - 2rem));
    max-height: calc(100vh - 3rem);
    padding: 0;
    border: 1px solid var(--border);
    border-radius: var(--radius);
    background: var(--bg-elevated);
    color: var(--text);
    box-shadow: 0 18px 48px rgba(0, 0, 0, 0.5);
    overflow: hidden;
  }
  .modal[open] {
    display: flex;
    flex-direction: column;
  }
  .modal.wide {
    width: min(46rem, calc(100vw - 2rem));
  }
  .modal::backdrop {
    background: rgba(5, 7, 12, 0.6);
  }
  .modal-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    padding: 0.9rem 1.1rem 0.6rem;
  }
  h2 {
    margin: 0;
    font-size: 1.02rem;
  }
  .modal-close {
    border: none;
    background: none;
    color: var(--text-faint);
    font-size: 1.2rem;
    line-height: 1;
    cursor: pointer;
    padding: 0.2rem 0.35rem;
    border-radius: var(--radius-sm);
  }
  .modal-close:hover {
    color: var(--text);
    background: var(--bg-hover);
  }
  .modal-body {
    display: flex;
    flex-direction: column;
    gap: 0.7rem;
    padding: 0.4rem 1.1rem 1rem;
    overflow: auto;
  }
  .modal-footer {
    display: flex;
    justify-content: flex-end;
    gap: 0.5rem;
    padding: 0.75rem 1.1rem;
    border-top: 1px solid var(--border-soft);
  }
</style>
