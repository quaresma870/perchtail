<script lang="ts">
  import { createEventDispatcher, onDestroy, onMount, tick } from 'svelte'
  import { isSeparator, placeInWindow, type MenuEntry } from '../context-menu'

  /** The one right-click menu (#287), drawn from a list worked out in
   * lib/context-menu.ts. It opens at (x, y), stays inside the window and
   * takes the focus; arrows (wrapping), Home and End move, Enter or Space
   * choose, Escape or Tab close. A click outside it, a scroll or the window
   * losing the focus close it too. On Escape or a choice the focus goes
   * back to `opener`, before `select` is sent, so an action that moves the
   * focus (an input it opens) keeps it. */
  export let entries: MenuEntry[]
  export let x: number
  export let y: number
  export let label: string
  /** Where the focus goes back to when the menu closes. */
  export let opener: HTMLElement | null = null

  const dispatch = createEventDispatcher<{ select: string; close: void }>()

  let menu: HTMLUListElement
  let left = x
  let top = y
  let closed = false

  const items = () => Array.from(menu?.querySelectorAll<HTMLElement>('[role="menuitem"]') ?? [])

  onMount(async () => {
    const rect = menu.getBoundingClientRect()
    ;({ x: left, y: top } = placeInWindow(x, y, rect.width, rect.height, window.innerWidth, window.innerHeight))
    await tick()
    items()[0]?.focus()
    // Added once the event that opened the menu is over.
    setTimeout(() => {
      if (closed) return
      window.addEventListener('pointerdown', onPointerDown, true)
      window.addEventListener('scroll', onScroll, true)
      window.addEventListener('blur', onBlur)
      window.addEventListener('resize', onBlur)
    })
  })

  onDestroy(removeListeners)

  function removeListeners() {
    window.removeEventListener('pointerdown', onPointerDown, true)
    window.removeEventListener('scroll', onScroll, true)
    window.removeEventListener('blur', onBlur)
    window.removeEventListener('resize', onBlur)
  }

  function close(refocus: boolean, chosen?: string) {
    if (closed) return
    closed = true
    removeListeners()
    if (refocus && opener?.isConnected) opener.focus({ preventScroll: true })
    if (chosen !== undefined) dispatch('select', chosen)
    dispatch('close')
  }

  function choose(id: string) {
    close(true, id)
  }

  function onPointerDown(event: PointerEvent) {
    if (!menu.contains(event.target as Node)) close(false)
  }

  function onScroll(event: Event) {
    if (!menu.contains(event.target as Node)) close(false)
  }

  function onBlur() {
    close(false)
  }

  function onKeydown(event: KeyboardEvent) {
    const all = items()
    const i = all.indexOf(document.activeElement as HTMLElement)
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault()
      const step = event.key === 'ArrowDown' ? 1 : -1
      all[(i + step + all.length) % all.length]?.focus()
    } else if (event.key === 'Home' || event.key === 'End') {
      event.preventDefault()
      all[event.key === 'Home' ? 0 : all.length - 1]?.focus()
    } else if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault()
      const id = all[i]?.dataset.id
      if (id) choose(id)
    } else if (event.key === 'Escape') {
      event.preventDefault()
      event.stopPropagation()
      close(true)
    } else if (event.key === 'Tab') {
      event.preventDefault()
      close(true)
    }
  }
</script>

<ul
  class="context-menu"
  role="menu"
  aria-label={label}
  style="left: {left}px; top: {top}px"
  bind:this={menu}
  on:keydown={onKeydown}
  on:contextmenu|preventDefault
>
  {#each entries as entry, i (i)}
    {#if isSeparator(entry)}
      <li role="separator" class="divider"></li>
    {:else}
      <li role="none">
        <button role="menuitem" tabindex="-1" data-id={entry.id} on:click={() => choose(entry.id)}>
          <span>{entry.label}</span>
          {#if entry.shortcut}
            <span class="shortcut" aria-hidden="true">{entry.shortcut}</span>
          {/if}
        </button>
      </li>
    {/if}
  {/each}
</ul>

<style>
  .context-menu {
    position: fixed;
    z-index: 60;
    list-style: none;
    margin: 0;
    padding: 0.25rem 0;
    min-width: 14rem;
    max-width: calc(100vw - 8px);
    background: var(--bg-elevated);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.35);
  }
  .context-menu button {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 1.5rem;
    width: 100%;
    text-align: left;
    border: none;
    background: none;
    padding: 0.35rem 0.85rem;
    font: inherit;
    font-size: 0.8rem;
    color: var(--text);
    cursor: pointer;
    white-space: nowrap;
  }
  .context-menu button:hover,
  .context-menu button:focus {
    background: var(--bg-hover);
    outline: none;
  }
  .context-menu button:focus-visible {
    outline: 2px solid var(--accent-border);
    outline-offset: -2px;
  }
  .shortcut {
    color: var(--text-muted);
    font-size: 0.74rem;
  }
  .divider {
    height: 1px;
    margin: 0.25rem 0;
    background: var(--border-soft);
  }
</style>
