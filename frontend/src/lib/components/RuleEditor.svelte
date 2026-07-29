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

<div class="rule-editor card">
  <div class="toolbar">
    <div>
      <h3>Rules</h3>
      <p class="hint">Evaluated in order, last match wins.</p>
    </div>
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
    <p class="hint">Loading…</p>
  {:else if mode === 'rows'}
    <div class="rule-rows">
      {#each rules as rule, index (rule.id)}
        <div class="rule-row">
          <span class="order-num">{index + 1}</span>
          {#if !readOnly}
            <div class="order-btns">
              <button class="tiny" disabled={index === 0} on:click={() => move(index, -1)}>▲</button>
              <button
                class="tiny"
                disabled={index === rules.length - 1}
                on:click={() => move(index, 1)}>▼</button
              >
            </div>
          {/if}
          <select
            class="type-select"
            class:include={rule.type === 'include'}
            class:exclude={rule.type === 'exclude'}
            disabled={readOnly}
            bind:value={rule.type}
            on:change={() => updateRule(rule)}
          >
            <option value="include">include</option>
            <option value="exclude">exclude</option>
          </select>
          <input
            class="input pattern-input mono"
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
          <input
            class="input notes-input"
            placeholder="notes"
            disabled={readOnly}
            bind:value={rule.notes}
            on:change={() => updateRule(rule)}
          />
          {#if !readOnly}
            <button class="link danger" on:click={() => deleteRule(rule)}>delete</button>
          {/if}
        </div>
      {/each}
      {#if rules.length === 0}
        <p class="empty">No rules yet — nothing is visible on this source until one is added.</p>
      {/if}
    </div>

    {#if !readOnly}
      <form class="add-row" on:submit|preventDefault={addRule}>
        <select
          class="input type-select"
          class:include={newType === 'include'}
          class:exclude={newType === 'exclude'}
          bind:value={newType}
        >
          <option value="include">include</option>
          <option value="exclude">exclude</option>
        </select>
        <input
          class="input mono"
          placeholder="**/*.log or re:^access.*\.log$"
          bind:value={newPattern}
        />
        <button class="btn btn-primary" type="submit">Add rule</button>
      </form>
    {/if}
  {:else}
    <p class="hint">
      One rule per line, evaluated top to bottom, last match wins. Prefix a line with
      <code>!</code> to exclude, <code>re:</code> to use regex. Blank lines and
      <code>#</code> comments are ignored.
    </p>
    <textarea class="input mono" rows="10" bind:value={rawText} disabled={readOnly}></textarea>
    {#if !readOnly}
      <button class="btn btn-primary" on:click={applyRaw}>Apply</button>
    {/if}
  {/if}
</div>

<style>
  .rule-editor {
    padding: 1.25rem 1.5rem;
    margin-top: 0;
  }
  .toolbar {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
  }
  h3 {
    margin: 0;
    font-size: 1rem;
    color: var(--text);
  }
  .mode-toggle {
    display: flex;
    gap: 0.3rem;
  }
  .mode-toggle button {
    border: 1px solid var(--border);
    background: var(--bg-elevated-2);
    color: var(--text-muted);
    padding: 0.3rem 0.7rem;
    font-size: 0.78rem;
    cursor: pointer;
    border-radius: var(--radius-sm);
  }
  .mode-toggle button.active {
    background: var(--accent);
    color: #fff;
    border-color: var(--accent);
  }
  .rule-rows {
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
    margin-top: 1rem;
  }
  .rule-row {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    background: var(--bg);
    border: 1px solid var(--border-soft);
    border-radius: var(--radius-sm);
    padding: 0.45rem 0.6rem;
  }
  .order-num {
    color: var(--text-faint);
    font-size: 0.78rem;
    width: 1.2rem;
    text-align: right;
  }
  .order-btns {
    display: flex;
    flex-direction: column;
  }
  button.tiny {
    border: none;
    background: none;
    color: var(--text-faint);
    cursor: pointer;
    padding: 0 0.2rem;
    font-size: 0.6rem;
    line-height: 1.1;
  }
  button.tiny:hover:not(:disabled) {
    color: var(--text);
  }
  button.tiny:disabled {
    opacity: 0.3;
    cursor: default;
  }
  .type-select {
    flex: 0 0 auto;
    width: auto;
    padding: 0.2rem 0.5rem;
    border-radius: 999px;
    font-size: 0.72rem;
    font-weight: 600;
    border: 1px solid transparent;
  }
  .type-select.include {
    background: var(--success-soft);
    color: var(--success);
  }
  .type-select.exclude {
    background: var(--danger-soft);
    color: var(--danger);
  }
  .pattern-input {
    flex: 1;
    min-width: 0;
  }
  .notes-input {
    flex: 0 0 140px;
  }
  button.link {
    border: none;
    background: none;
    color: var(--accent-hover);
    cursor: pointer;
    padding: 0;
    font-size: 0.8rem;
    white-space: nowrap;
  }
  button.link.danger {
    color: var(--danger);
  }
  .add-row {
    display: flex;
    gap: 0.5rem;
    margin-top: 0.9rem;
  }
  .add-row .type-select {
    flex: 0 0 auto;
  }
  .add-row input {
    flex: 1;
  }
  .add-row button {
    flex: 0 0 auto;
  }
  textarea {
    width: 100%;
    font-size: 0.85rem;
    resize: vertical;
  }
  .hint {
    font-size: 0.78rem;
    color: var(--text-faint);
    margin: 0.2rem 0 0;
  }
  .empty {
    text-align: center;
    color: var(--text-faint);
    padding: 1.5rem 0;
    margin: 0;
  }
  .error {
    color: var(--danger);
  }
</style>
