<script lang="ts">
  import { onMount } from 'svelte'
  import { api, ApiError } from '../api'
  import { parsePatternInput } from '../rule-format'
  import type { SeverityLevel, SeverityPattern } from '../types'

  // Global scope: baseUrl="/severity-patterns". Per-source override scope:
  // baseUrl={`/sources/${sourceId}/severity-patterns`}. Same component,
  // same row-based UX either way -- the backend enforces which capability
  // gates writes to each (manage_system_settings vs. manage_rules).
  export let baseUrl: string
  export let readOnly = false

  const LEVELS: SeverityLevel[] = ['error', 'warning', 'info', 'debug']

  let patterns: SeverityPattern[] = []
  let loading = true
  let error = ''

  let newLevel: SeverityLevel = 'error'
  let newPattern = ''
  let newHighlightLine = false

  async function load() {
    loading = true
    error = ''
    try {
      patterns = await api.get<SeverityPattern[]>(baseUrl)
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to load severity patterns'
    } finally {
      loading = false
    }
  }

  async function addPattern() {
    if (!newPattern.trim()) return
    try {
      await api.post(baseUrl, {
        level: newLevel,
        pattern: newPattern.trim(),
        highlight_line: newHighlightLine,
      })
      newPattern = ''
      newHighlightLine = false
      await load()
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to add pattern'
    }
  }

  async function updatePattern(pattern: SeverityPattern) {
    try {
      await api.patch(`${baseUrl}/${pattern.id}`, {
        level: pattern.level,
        pattern: pattern.pattern_kind === 'regex' ? `re:${pattern.pattern}` : pattern.pattern,
        enabled: pattern.enabled,
        highlight_line: pattern.highlight_line,
        include_in_navigation: pattern.include_in_navigation,
      })
      await load()
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to update pattern'
    }
  }

  async function deletePattern(pattern: SeverityPattern) {
    try {
      await api.delete(`${baseUrl}/${pattern.id}`)
      await load()
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to delete pattern'
    }
  }

  onMount(load)
</script>

<div class="severity-editor card">
  <div class="toolbar">
    <div>
      <h3>Severity indicators</h3>
      <p class="hint">
        Highlights matching text in the Viewer — display-only, never affects what's fetchable.
        Prefix a pattern with <code>re:</code> for regex; otherwise it's matched as plain text
        anywhere in a line, case-insensitive.
      </p>
    </div>
  </div>

  {#if error}
    <p class="error">{error}</p>
  {/if}

  {#if loading}
    <p class="hint">Loading…</p>
  {:else}
    <div class="pattern-rows">
      {#each patterns as pattern (pattern.id)}
        <div class="pattern-row">
          <select
            class="level-select"
            class:error={pattern.level === 'error'}
            class:warning={pattern.level === 'warning'}
            class:info={pattern.level === 'info'}
            class:debug={pattern.level === 'debug'}
            disabled={readOnly}
            bind:value={pattern.level}
            on:change={() => updatePattern(pattern)}
          >
            {#each LEVELS as level}
              <option value={level}>{level}</option>
            {/each}
          </select>
          <input
            class="input pattern-input mono"
            disabled={readOnly}
            value={pattern.pattern_kind === 'regex' ? `re:${pattern.pattern}` : pattern.pattern}
            on:change={(e) => {
              const parsed = parsePatternInput((e.target as HTMLInputElement).value)
              pattern.pattern = parsed.pattern
              pattern.pattern_kind = parsed.pattern_kind
              updatePattern(pattern)
            }}
          />
          <label class="flag" title="Tint the whole line, not just the matched text">
            <input
              type="checkbox"
              disabled={readOnly}
              bind:checked={pattern.highlight_line}
              on:change={() => updatePattern(pattern)}
            />
            line
          </label>
          <label class="flag" title="Included when stepping through next/previous problem">
            <input
              type="checkbox"
              disabled={readOnly}
              bind:checked={pattern.include_in_navigation}
              on:change={() => updatePattern(pattern)}
            />
            nav
          </label>
          <label class="flag">
            <input
              type="checkbox"
              disabled={readOnly}
              bind:checked={pattern.enabled}
              on:change={() => updatePattern(pattern)}
            />
            on
          </label>
          {#if !readOnly}
            <button class="link danger" on:click={() => deletePattern(pattern)}>delete</button>
          {/if}
        </div>
      {/each}
      {#if patterns.length === 0}
        <p class="empty">No patterns yet — nothing is highlighted.</p>
      {/if}
    </div>

    {#if !readOnly}
      <form class="add-row" on:submit|preventDefault={addPattern}>
        <select
          class="input level-select"
          class:error={newLevel === 'error'}
          class:warning={newLevel === 'warning'}
          class:info={newLevel === 'info'}
          class:debug={newLevel === 'debug'}
          bind:value={newLevel}
        >
          {#each LEVELS as level}
            <option value={level}>{level}</option>
          {/each}
        </select>
        <input
          class="input mono"
          placeholder="panic or re:\berror\b"
          bind:value={newPattern}
        />
        <label class="flag" title="Tint the whole line, not just the matched text">
          <input type="checkbox" bind:checked={newHighlightLine} />
          line
        </label>
        <button class="btn btn-primary" type="submit">Add pattern</button>
      </form>
    {/if}
  {/if}
</div>

<style>
  .severity-editor {
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
  .pattern-rows {
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
    margin-top: 1rem;
  }
  .pattern-row {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    background: var(--bg);
    border: 1px solid var(--border-soft);
    border-radius: var(--radius-sm);
    padding: 0.45rem 0.6rem;
  }
  .level-select {
    flex: 0 0 auto;
    width: auto;
    padding: 0.2rem 0.5rem;
    border-radius: 999px;
    font-size: 0.72rem;
    font-weight: 600;
    border: 1px solid transparent;
    text-transform: capitalize;
  }
  .level-select.error {
    background: var(--danger-soft);
    color: var(--danger);
  }
  .level-select.warning {
    background: var(--warning-soft);
    color: var(--warning);
  }
  .level-select.info {
    background: var(--success-soft);
    color: var(--success);
  }
  .level-select.debug {
    background: var(--muted-badge-bg);
    color: var(--muted-badge-text);
  }
  .pattern-input {
    flex: 1;
    min-width: 0;
  }
  .flag {
    display: inline-flex;
    align-items: center;
    gap: 0.25rem;
    font-size: 0.72rem;
    color: var(--text-faint);
    white-space: nowrap;
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
    align-items: center;
    gap: 0.5rem;
    margin-top: 0.9rem;
  }
  .add-row .level-select {
    flex: 0 0 auto;
  }
  .add-row input.mono {
    flex: 1;
  }
  .add-row button {
    flex: 0 0 auto;
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
