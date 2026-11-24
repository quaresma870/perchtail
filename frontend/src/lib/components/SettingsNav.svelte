<script lang="ts">
  import { push } from 'svelte-spa-router'
  import { currentUser } from '../auth'
  import { currentHash } from '../hash'
  import { systemSettings } from '../settings'
  import { isSettingsPageActive, settingsNavGroups } from '../settings-nav'
  import SettingsIcon from './SettingsIcon.svelte'

  // The Settings sidebar: a list of links per group, each with its icon, and a
  // line between groups, from 768 px up; a select with a group per section
  // below that (see SettingsLayout.svelte).
  $: groups = settingsNavGroups($currentUser, $systemSettings.audit_view_enabled)
  $: activePath =
    groups
      .flatMap((group) => group.pages)
      .find((page) => isSettingsPageActive($currentHash, page.path))?.path ?? ''

  function navigate(event: Event) {
    const path = (event.currentTarget as HTMLSelectElement).value
    if (path && path !== activePath) push(path)
  }
</script>

<nav class="settings-nav" aria-label="Settings">
  <div class="nav-groups">
    {#each groups as group, index (group.label)}
      {@const titleId = `settings-nav-${group.label.toLowerCase()}`}
      {#if index > 0}
        <hr class="nav-separator" />
      {/if}
      <div class="nav-group">
        <div class="nav-group-title" id={titleId}>{group.label}</div>
        <ul aria-labelledby={titleId}>
          {#each group.pages as page (page.path)}
            <li>
              <a
                href={`#${page.path}`}
                class:active={page.path === activePath}
                aria-current={page.path === activePath ? 'page' : undefined}
                ><SettingsIcon name={page.icon} /><span>{page.label}</span></a
              >
            </li>
          {/each}
        </ul>
      </div>
    {/each}
  </div>

  <select class="input nav-select" aria-label="Settings page" value={activePath} on:change={navigate}>
    {#if !activePath}
      <option value="" disabled>Settings</option>
    {/if}
    {#each groups as group (group.label)}
      <optgroup label={group.label}>
        {#each group.pages as page (page.path)}
          <option value={page.path}>{page.label}</option>
        {/each}
      </optgroup>
    {/each}
  </select>
</nav>

<style>
  .settings-nav {
    background: var(--bg-elevated);
  }
  .nav-groups {
    display: flex;
    flex-direction: column;
    padding: 1.25rem 0.75rem;
  }
  .nav-separator {
    border: 0;
    border-top: 1px solid var(--border-soft);
    margin: 8px 0.6rem 6px;
  }
  .nav-group-title {
    font-size: 0.72rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--text-faint);
    padding: 0 0.6rem 0.35rem;
  }
  ul {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 0.1rem;
  }
  a {
    display: flex;
    align-items: center;
    gap: 0.55rem;
    color: var(--text-muted);
    text-decoration: none;
    font-size: 0.86rem;
    font-weight: 500;
    padding: 0.38rem 0.6rem;
    border-radius: var(--radius-sm, 6px);
    border-left: 2px solid transparent;
  }
  a:hover {
    color: var(--text);
    background: var(--bg-elevated-2);
  }
  a.active {
    color: var(--text);
    background: var(--bg-elevated-2);
    border-left-color: var(--accent);
    font-weight: 600;
  }
  .nav-select {
    display: none;
  }

  /* Below 768 px the sidebar gives way to a select at the top of the page. */
  @media (max-width: 767.98px) {
    .settings-nav {
      padding: 0.75rem 1rem;
      border-bottom: 1px solid var(--border-soft);
    }
    .nav-groups {
      display: none;
    }
    .nav-select {
      display: block;
      width: 100%;
    }
  }
</style>
