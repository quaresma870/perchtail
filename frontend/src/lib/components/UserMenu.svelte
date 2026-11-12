<script lang="ts">
  import { createEventDispatcher, tick } from 'svelte'
  import { userInitials } from '../user-initials'

  /** The top bar's user menu (#298): initials, name and a chevron, opening
   * My profile and Log out. Menu-button pattern: Enter/Space/ArrowDown open
   * it on the first item, arrows move, Escape closes and refocuses the
   * button, Tab or a click outside closes it. */
  export let name: string
  export let active = false

  const dispatch = createEventDispatcher<{ logout: void }>()

  let open = false
  let root: HTMLDivElement
  let button: HTMLButtonElement
  let menu: HTMLUListElement

  const items = () => Array.from(menu?.querySelectorAll<HTMLElement>('[role="menuitem"]') ?? [])

  async function show(focusLast = false) {
    open = true
    await tick()
    const all = items()
    all[focusLast ? all.length - 1 : 0]?.focus()
  }

  function close(refocus = false) {
    open = false
    if (refocus) button?.focus()
  }

  function onButtonKeydown(event: KeyboardEvent) {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault()
      show(event.key === 'ArrowUp')
    }
  }

  function onMenuKeydown(event: KeyboardEvent) {
    const all = items()
    const i = all.indexOf(document.activeElement as HTMLElement)
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault()
      const step = event.key === 'ArrowDown' ? 1 : -1
      all[(i + step + all.length) % all.length]?.focus()
    } else if (event.key === 'Home' || event.key === 'End') {
      event.preventDefault()
      all[event.key === 'Home' ? 0 : all.length - 1]?.focus()
    } else if (event.key === 'Escape') {
      event.preventDefault()
      close(true)
    } else if (event.key === 'Tab') {
      close()
    }
  }

  function onWindowClick(event: MouseEvent) {
    if (open && !root.contains(event.target as Node)) close()
  }
</script>

<svelte:window on:click={onWindowClick} />

<div class="user-menu" bind:this={root}>
  <button
    class="user-menu-button"
    class:active
    bind:this={button}
    aria-haspopup="menu"
    aria-expanded={open}
    aria-label="User menu: {userInitials(name)} {name}"
    title={name}
    on:click={() => (open ? close() : show())}
    on:keydown={onButtonKeydown}
  >
    <span class="avatar" aria-hidden="true">{userInitials(name)}</span>
    <span class="user-menu-name">{name}</span>
    <svg class="chevron" class:open viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
      <path d="M4 6l4 4 4-4" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" />
    </svg>
  </button>
  {#if open}
    <ul class="menu" role="menu" aria-label="User menu" bind:this={menu} on:keydown={onMenuKeydown}>
      <li role="none">
        <a role="menuitem" tabindex="-1" href="#/profile" on:click={() => close()}>My profile</a>
      </li>
      <li role="none" class="divider"></li>
      <li role="none">
        <button
          role="menuitem"
          tabindex="-1"
          on:click={() => {
            close()
            dispatch('logout')
          }}>Log out</button
        >
      </li>
    </ul>
  {/if}
</div>

<style>
  .user-menu {
    position: relative;
  }
  .user-menu-button {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.25rem 0.55rem 0.25rem 0.3rem;
    border: 1px solid var(--border);
    border-radius: 999px;
    background: var(--bg-elevated-2);
    color: var(--text);
    font: inherit;
    font-size: 0.82rem;
    cursor: pointer;
    max-width: 16rem;
  }
  .user-menu-button:hover,
  .user-menu-button[aria-expanded='true'] {
    background: var(--bg-hover);
    border-color: var(--accent-border);
  }
  .user-menu-button.active {
    border-color: var(--accent);
  }
  .user-menu-button:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
  }
  .avatar {
    flex: 0 0 auto;
    display: grid;
    place-items: center;
    width: 1.6rem;
    height: 1.6rem;
    border-radius: 50%;
    background: var(--accent-soft);
    color: var(--accent-hover);
    font-size: 0.7rem;
    font-weight: 700;
    letter-spacing: 0.02em;
  }
  .user-menu-name {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .chevron {
    flex: 0 0 auto;
    color: var(--text-muted);
    transition: transform 0.15s;
  }
  .chevron.open {
    transform: rotate(180deg);
  }
  .menu {
    position: absolute;
    right: 0;
    top: calc(100% + 0.35rem);
    z-index: 50;
    min-width: 11rem;
    margin: 0;
    padding: 0.3rem;
    list-style: none;
    background: var(--bg-elevated);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.35);
  }
  .menu [role='menuitem'] {
    display: block;
    width: 100%;
    padding: 0.45rem 0.65rem;
    border: 0;
    border-radius: var(--radius-sm);
    background: none;
    color: var(--text);
    font: inherit;
    font-size: 0.85rem;
    text-align: left;
    text-decoration: none;
    cursor: pointer;
  }
  .menu [role='menuitem']:hover,
  .menu [role='menuitem']:focus {
    background: var(--bg-hover);
    outline: none;
  }
  .menu [role='menuitem']:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: -2px;
  }
  .divider {
    height: 1px;
    margin: 0.3rem 0.2rem;
    background: var(--border-soft);
  }
</style>
