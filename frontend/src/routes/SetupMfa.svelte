<script lang="ts">
  import { push } from 'svelte-spa-router'
  import MfaPanel from '../lib/components/MfaPanel.svelte'
</script>

<div class="page">
  <div class="card">
    <h1>Set up two-factor authentication</h1>
    <p class="hint">
      Your account must use two-factor authentication: it administers PerchTail, or your
      administrator requires it for every local account. Set it up to continue.
    </p>
    <MfaPanel onDone={() => push('/viewer')} />
  </div>
</div>

<style>
  .page {
    display: flex;
    align-items: center;
    justify-content: center;
    min-height: 100%;
    padding: 2rem 1rem;
    background: var(--bg);
  }
  .card {
    padding: 2rem 2.25rem;
    width: min(520px, 100%);
    display: flex;
    flex-direction: column;
    gap: 1rem;
  }
  h1 {
    margin: 0;
    font-size: 1.2rem;
    color: var(--text);
  }
  .hint {
    margin: 0;
    font-size: 0.85rem;
    color: var(--text-muted);
  }
</style>
