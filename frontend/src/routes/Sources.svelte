<script lang="ts">
  import { onMount } from 'svelte'
  import { push } from 'svelte-spa-router'
  import { api, ApiError } from '../lib/api'
  import { currentUser, hasCapability } from '../lib/auth'
  import type { Customer, Source } from '../lib/types'

  let sources: Source[] = []
  let customers: Customer[] = []
  let loading = true
  let error = ''
  let checkResults: Record<number, { ok: boolean; detail: string } | 'checking'> = {}

  const customerName = (id: number | null) =>
    id === null ? '—' : customers.find((c) => c.id === id)?.name ?? `#${id}`

  function checkResultOk(id: number): boolean | null {
    const result = checkResults[id]
    return result && result !== 'checking' ? result.ok : null
  }
  function checkResultDetail(id: number): string {
    const result = checkResults[id]
    return result && result !== 'checking' ? result.detail : ''
  }

  async function load() {
    loading = true
    error = ''
    try {
      sources = await api.get<Source[]>('/sources')
      if (hasCapability($currentUser, 'create_source')) {
        customers = await api.get<Customer[]>('/customers')
      }
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to load sources'
    } finally {
      loading = false
    }
  }

  async function checkConnection(source: Source) {
    checkResults = { ...checkResults, [source.id]: 'checking' }
    try {
      const result = await api.post<{ ok: boolean; detail: string }>(
        `/sources/${source.id}/check`,
      )
      checkResults = { ...checkResults, [source.id]: result }
    } catch (err) {
      checkResults = {
        ...checkResults,
        [source.id]: { ok: false, detail: err instanceof ApiError ? err.detail : 'Check failed' },
      }
    }
  }

  async function removeSource(source: Source) {
    if (!confirm(`Delete source "${source.name}"? This cannot be undone.`)) return
    try {
      await api.delete(`/sources/${source.id}`)
      await load()
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to delete source'
    }
  }

  onMount(load)
</script>

<div class="page">
  <div class="header">
    <h1>Sources</h1>
    {#if hasCapability($currentUser, 'create_source')}
      <button on:click={() => push('/sources/new')}>New source</button>
    {/if}
  </div>

  {#if loading}
    <p>Loading…</p>
  {:else if error}
    <p class="error">{error}</p>
  {:else}
    <table>
      <thead>
        <tr>
          <th>Name</th>
          <th>Customer</th>
          <th>Protocol</th>
          <th>Host</th>
          <th>Rules</th>
          <th>Status</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        {#each sources as source (source.id)}
          <tr>
            <td>
              {source.name}
              {#if source.is_system}
                <span class="badge">system</span>
              {/if}
              {#if !source.enabled}
                <span class="badge muted">disabled</span>
              {/if}
            </td>
            <td>{customerName(source.customer_id)}</td>
            <td>{source.protocol}</td>
            <td>{source.host}</td>
            <td>{source.rule_count}</td>
            <td>
              {#if checkResults[source.id] === 'checking'}
                checking…
              {:else if checkResultOk(source.id) !== null}
                <span class={checkResultOk(source.id) ? 'ok' : 'fail'}>
                  {checkResultOk(source.id) ? 'reachable' : checkResultDetail(source.id)}
                </span>
              {:else}
                <button class="link" on:click={() => checkConnection(source)}>check</button>
              {/if}
            </td>
            <td class="actions">
              <button class="link" on:click={() => push(`/viewer/${source.id}`)}>browse</button>
              {#if !source.is_system && hasCapability($currentUser, 'create_source')}
                <button class="link" on:click={() => push(`/sources/${source.id}`)}>edit</button>
                <button class="link danger" on:click={() => removeSource(source)}>delete</button>
              {/if}
            </td>
          </tr>
        {/each}
        {#if sources.length === 0}
          <tr>
            <td colspan="7" class="empty">No sources visible to your account.</td>
          </tr>
        {/if}
      </tbody>
    </table>
  {/if}
</div>

<style>
  .page {
    padding: 1.5rem;
  }
  .header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 1rem;
  }
  h1 {
    font-size: 1.3rem;
    margin: 0;
  }
  table {
    width: 100%;
    border-collapse: collapse;
    background: #fff;
    border-radius: 6px;
    overflow: hidden;
  }
  th,
  td {
    text-align: left;
    padding: 0.55rem 0.75rem;
    border-bottom: 1px solid #eee;
    font-size: 0.9rem;
  }
  th {
    background: #f0f1f4;
    font-weight: 600;
  }
  .badge {
    display: inline-block;
    margin-left: 0.4rem;
    padding: 0.05rem 0.4rem;
    font-size: 0.7rem;
    border-radius: 3px;
    background: #2f6fed;
    color: #fff;
  }
  .badge.muted {
    background: #999;
  }
  .actions {
    white-space: nowrap;
  }
  button.link {
    border: none;
    background: none;
    color: #2f6fed;
    cursor: pointer;
    padding: 0;
    margin-right: 0.6rem;
    font-size: 0.85rem;
  }
  button.link.danger {
    color: #c0392b;
  }
  .ok {
    color: #1a8a41;
  }
  .fail {
    color: #c0392b;
  }
  .empty {
    text-align: center;
    color: #888;
  }
  .error {
    color: #c0392b;
  }
</style>
