<script lang="ts">
  import { onMount } from 'svelte'
  import { push } from 'svelte-spa-router'
  import { api, ApiError } from '../lib/api'
  import { logout } from '../lib/auth'
  import SettingsNav from '../lib/components/SettingsNav.svelte'
  import type { AuthSessionInfo } from '../lib/types'

  let sessions: AuthSessionInfo[] = []
  let loading = true
  let error = ''
  let revoking: Record<number, boolean> = {}

  const formatDate = (iso: string | null) => (iso ? new Date(iso).toLocaleString() : 'never')

  async function load() {
    loading = true
    error = ''
    try {
      sessions = await api.get<AuthSessionInfo[]>('/auth/sessions')
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to load sessions'
    } finally {
      loading = false
    }
  }

  async function revoke(s: AuthSessionInfo) {
    if (s.is_current) {
      // This IS the session serving this very page -- log out properly
      // (clears the browser cookie too, not just the server-side row) and
      // land back on the login screen, same as clicking "Log out" in the
      // header.
      await logout()
      push('/login')
      return
    }
    if (!confirm('Revoke this session? That device or browser will be signed out.')) return
    revoking = { ...revoking, [s.id]: true }
    try {
      await api.delete(`/auth/sessions/${s.id}`)
      await load()
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to revoke session'
    } finally {
      revoking = { ...revoking, [s.id]: false }
    }
  }

  onMount(load)
</script>

<SettingsNav />

<div class="page">
  <div class="header">
    <h1>Sessions</h1>
    <p class="hint">
      Every device or browser currently signed in as you. If you don't recognize one, revoke it.
    </p>
  </div>

  {#if error}
    <p class="error">{error}</p>
  {:else if loading}
    <p class="hint">Loading…</p>
  {:else}
    <ul class="session-list">
      {#each sessions as s (s.id)}
        <li class="card session-row">
          <div class="session-main">
            <div class="session-name">
              {s.user_agent ?? 'Unknown device'}
              {#if s.is_current}
                <span class="badge badge-accent">This device</span>
              {/if}
            </div>
            <div class="session-sub">
              Signed in {formatDate(s.created_at)} · last active {formatDate(s.last_seen_at)}
            </div>
          </div>
          <button
            class="btn btn-ghost danger"
            disabled={revoking[s.id]}
            on:click={() => revoke(s)}
          >
            {s.is_current ? 'Log out' : revoking[s.id] ? 'Revoking…' : 'Revoke'}
          </button>
        </li>
      {/each}
    </ul>
  {/if}
</div>

<style>
  .page {
    padding: 1.75rem 2rem;
    max-width: 820px;
    display: flex;
    flex-direction: column;
    gap: 1.25rem;
  }
  h1 {
    font-size: 1.4rem;
    margin: 0;
    color: var(--text);
  }
  .hint {
    font-size: 0.85rem;
    color: var(--text-faint);
    margin: 0.35rem 0 0;
    line-height: 1.5;
  }
  .session-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 0.6rem;
  }
  .session-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1.5rem;
    padding: 0.9rem 1.2rem;
  }
  .session-name {
    font-weight: 600;
    color: var(--text);
    margin-bottom: 0.3rem;
    display: flex;
    align-items: center;
    gap: 0.6rem;
  }
  .session-sub {
    font-size: 0.78rem;
    color: var(--text-faint);
  }
  .badge-accent {
    background: var(--accent-soft);
    color: var(--accent-hover);
  }
  .btn-ghost.danger {
    color: var(--danger);
    flex: 0 0 auto;
  }
  .error {
    color: var(--danger);
    margin: 0;
  }
</style>
