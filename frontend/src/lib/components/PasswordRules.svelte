<script lang="ts">
  // The password policy's rules (#245), next to a password field. Loads
  // them itself; shows nothing if they can't be loaded (the server still
  // enforces the policy and says which rule failed).
  import { onMount } from 'svelte'
  import { api } from '../api'
  import { describePolicy } from '../password-policy'
  import type { PasswordPolicy } from '../types'

  export let policy: PasswordPolicy | null = null

  onMount(async () => {
    if (policy) return
    try {
      policy = await api.get<PasswordPolicy>('/auth/password-policy')
    } catch {
      policy = null
    }
  })
</script>

{#if policy}
  <ul class="rules" aria-label="Password rules">
    {#each describePolicy(policy) as rule}
      <li>{rule}</li>
    {/each}
  </ul>
{/if}

<style>
  .rules {
    margin: 0;
    padding-left: 1.1rem;
    font-size: 0.8rem;
    line-height: 1.5;
    color: var(--text-faint);
  }
</style>
