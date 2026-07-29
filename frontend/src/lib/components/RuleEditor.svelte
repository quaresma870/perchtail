<script lang="ts">
  import { onMount } from 'svelte'
  import { api, ApiError } from '../api'
  import type { Rule, RuleType } from '../types'

  export let sourceId: number
  export let readOnly = false

  let rules: Rule[] = []
  let loading = true
  let error = ''
  let mode: 'rows' | 'raw' = 'rows'
  let rawText = ''
  let newType: RuleType = 'include'
  let newPattern = ''

  async function load() {
    loading = true
    error = ''
    try {
      rules = await api.get<Rule[]>(`/sources/${sourceId}/rules`)
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to load rules'
    } finally {
      loading = false
    }
  }

  function toRawLine(rule: Rule): string {
    const pattern = rule.pattern_kind === 'regex' ? `re:${rule.pattern}` : rule.pattern
    return rule.type === 'exclude' ? `!${pattern}` : pattern
  }

  function enterRawMode() {
    rawText = rules.map(toRawLine).join('\n')
    mode = 'raw'
  }

  async function addRule() {
    if (!newPattern.trim()) return
    try {
      await api.post(`/sources/${sourceId}/rules`, { type: newType, pattern: newPattern.trim() })
      newPattern = ''
      await load()
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to add rule'
    }
  }

  async function updateRule(rule: Rule) {
    try {
      await api.patch(`/sources/${sourceId}/rules/${rule.id}`, {
        type: rule.type,
        pattern: rule.pattern_kind === 'regex' ? `re:${rule.pattern}` : rule.pattern,
        notes: rule.notes,
      })
      await load()
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to update rule'
    }
  }

  async function deleteRule(rule: Rule) {
    try {
      await api.delete(`/sources/${sourceId}/rules/${rule.id}`)
      await load()
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to delete rule'
    }
  }

  async function move(index: number, direction: -1 | 1) {
    const target = index + direction
    if (target < 0 || target >= rules.length) return
    const ids = rules.map((r) => r.id)
    ;[ids[index], ids[target]] = [ids[target], ids[index]]
    try {
      await api.post(`/sources/${sourceId}/rules/reorder`, { rule_ids: ids })
      await load()
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to reorder rules'
    }
  }

  async function applyRaw() {
    try {
      await api.put(`/sources/${sourceId}/rules/raw`, { text: rawText })
      mode = 'rows'
      await load()
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to save rules'
    }
  }

  onMount(load)
</script>

<div class="rule-editor">
  <div class="toolbar">
    <h3>Rules</h3>
    {#if !readOnly}
      <div class="mode-toggle">
        <button class:active={mode === 'rows'} on:click={() => (mode = 'rows')}>Rows</button>
        <button class:active={mode === 'raw'} on:click={enterRawMode}>Raw text</button>
      </div>
    {/if}
  </div>

  {#if readOnly}
    <p class="hint">This source's rules are managed by the system and cannot be edited.</p>
  {/if}

  {#if error}
    <p class="error">{error}</p>
  {/if}

  {#if loading}
    <p>Loading…</p>
  {:else if mode === 'rows'}
    <table>
      <thead>
        <tr>
          <th>#</th>
          <th>Type</th>
          <th>Pattern</th>
          <th>Notes</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        {#each rules as rule, index (rule.id)}
          <tr>
            <td class="order">
              {#if !readOnly}
                <button class="tiny" disabled={index === 0} on:click={() => move(index, -1)}
                  >▲</button
                >
                <button class="tiny" disabled={index === rules.length - 1} on:click={() => move(index, 1)}
                  >▼</button
                >
              {/if}
            </td>
            <td>
              <select disabled={readOnly} bind:value={rule.type} on:change={() => updateRule(rule)}>
                <option value="include">include</option>
                <option value="exclude">exclude</option>
              </select>
            </td>
            <td>
              <input
                disabled={readOnly}
                value={rule.pattern_kind === 'regex' ? `re:${rule.pattern}` : rule.pattern}
                on:change={(e) => {
                  const raw = (e.target as HTMLInputElement).value
                  if (raw.startsWith('re:')) {
                    rule.pattern = raw.slice(3)
                    rule.pattern_kind = 'regex'
                  } else {
                    rule.pattern = raw
                    rule.pattern_kind = 'glob'
                  }
                  updateRule(rule)
                }}
              />
            </td>
            <td>
              <input disabled={readOnly} bind:value={rule.notes} on:change={() => updateRule(rule)} />
            </td>
            <td>
              {#if !readOnly}
                <button class="link danger" on:click={() => deleteRule(rule)}>delete</button>
              {/if}
            </td>
          </tr>
        {/each}
        {#if rules.length === 0}
          <tr>
            <td colspan="5" class="empty">
              No rules yet — nothing is visible on this source until one is added.
            </td>
          </tr>
        {/if}
      </tbody>
    </table>

    {#if !readOnly}
      <form class="add-row" on:submit|preventDefault={addRule}>
        <select bind:value={newType}>
          <option value="include">include</option>
          <option value="exclude">exclude</option>
        </select>
        <input placeholder="**/*.log or re:^access.*\.log$" bind:value={newPattern} />
        <button type="submit">Add rule</button>
      </form>
    {/if}
  {:else}
    <p class="hint">
      One rule per line, evaluated top to bottom, last match wins. Prefix a line with
      <code>!</code> to exclude, <code>re:</code> to use regex. Blank lines and
      <code>#</code> comments are ignored.
    </p>
    <textarea rows="10" bind:value={rawText} disabled={readOnly}></textarea>
    {#if !readOnly}
      <button on:click={applyRaw}>Apply</button>
    {/if}
  {/if}
</div>

<style>
  .rule-editor {
    background: #fff;
    border-radius: 6px;
    padding: 1rem 1.25rem;
  }
  .toolbar {
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  h3 {
    margin: 0;
    font-size: 1rem;
  }
  .mode-toggle button {
    border: 1px solid #ccc;
    background: #f5f5f5;
    padding: 0.25rem 0.6rem;
    font-size: 0.8rem;
    cursor: pointer;
  }
  .mode-toggle button.active {
    background: #2f6fed;
    color: #fff;
    border-color: #2f6fed;
  }
  table {
    width: 100%;
    border-collapse: collapse;
    margin-top: 0.75rem;
  }
  th,
  td {
    text-align: left;
    padding: 0.35rem 0.5rem;
    font-size: 0.85rem;
    border-bottom: 1px solid #eee;
  }
  input,
  select {
    width: 100%;
    padding: 0.3rem;
    border: 1px solid #ccc;
    border-radius: 3px;
  }
  .order {
    white-space: nowrap;
  }
  button.tiny {
    border: none;
    background: none;
    cursor: pointer;
    padding: 0 0.2rem;
  }
  button.link {
    border: none;
    background: none;
    color: #2f6fed;
    cursor: pointer;
    padding: 0;
  }
  button.link.danger {
    color: #c0392b;
  }
  .add-row {
    display: flex;
    gap: 0.5rem;
    margin-top: 0.75rem;
  }
  .add-row select {
    flex: 0 0 110px;
  }
  .add-row button {
    flex: 0 0 auto;
    padding: 0.4rem 0.8rem;
  }
  textarea {
    width: 100%;
    font-family: ui-monospace, Consolas, monospace;
    font-size: 0.85rem;
    padding: 0.5rem;
    border: 1px solid #ccc;
    border-radius: 4px;
  }
  .hint {
    font-size: 0.8rem;
    color: #666;
  }
  .empty {
    text-align: center;
    color: #888;
  }
  .error {
    color: #c0392b;
  }
</style>
