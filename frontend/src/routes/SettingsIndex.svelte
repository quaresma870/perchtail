<script lang="ts">
  import { onMount } from 'svelte'
  import { replace } from 'svelte-spa-router'

  // Sources is visible to every authenticated user (same as before this
  // reorganization — it was never capability-gated), so it's always a safe
  // landing tab for a bare "/settings" link or a typed/bookmarked URL.
  onMount(() => {
    replace('/settings/sources')
  })
</script>
