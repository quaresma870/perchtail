<!--
  An outline icon from Tabler Icons (MIT License, see THIRD-PARTY-NOTICES.md),
  drawn from its paths in settings-icon-paths.ts. Decorative only: the link it
  sits in carries the name. It takes the text colour of its parent.
-->
<script lang="ts">
  import { SETTINGS_ICON_PATHS, type SettingsIconName } from '../settings-icon-paths'

  export let name: SettingsIconName
</script>

<svg
  class="settings-icon"
  width="16"
  height="16"
  viewBox="0 0 24 24"
  fill="none"
  stroke="currentColor"
  stroke-width="2"
  stroke-linecap="round"
  stroke-linejoin="round"
  aria-hidden="true"
  focusable="false"
>
  {#each SETTINGS_ICON_PATHS[name] as d}
    <path {d} />
  {/each}
</svg>

<style>
  .settings-icon {
    flex: none;
    width: 16px;
    height: 16px;
  }
</style>
