<script lang="ts">
  import { createEventDispatcher, onMount } from 'svelte'
  import { api, ApiError } from '../api'
  import { confirmAction } from '../confirm'
  import {
    GROUP_ERROR,
    MAX_RULE_LENGTH,
    customRules,
    draftError,
    draftGroup,
    draftFromRule,
    draftPayload,
    emptyDraft,
    matchSegments,
    moveRule,
    type RuleDraft,
    type Segment,
  } from '../redaction-rules'
  import type { RedactionRule, RedactionTestResult } from '../types'

  // Custom redaction rules (#382, #249) of one scope: the system's
  // (rulesUrl="/redaction-rules", whose built-in rules are listed apart) or
  // a source's (`/sources/${id}/redaction-rules`). Super admins only; the
  // server refuses anyone else.
  export let rulesUrl: string
  /** Distinguishes the form fields when two editors share a page. */
  export let idPrefix = 'redaction'

  const dispatch = createEventDispatcher<{ changed: void }>()

  let rules: RedactionRule[] = []
  let loading = true
  let error = ''
  let busy = false

  // The form: a new rule (editingId null) or a rule being edited.
  let editingId: number | null = null
  let draft: RuleDraft = emptyDraft()
  let formError = ''
  let sample = ''
  let testError = ''
  let testing = false
  let testResult: RedactionTestResult | null = null
  let segments: Segment[] = []

  async function load() {
    error = ''
    try {
      rules = customRules(await api.get<RedactionRule[]>(rulesUrl))
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to load the redaction rules'
    } finally {
      loading = false
    }
  }

  async function changed() {
    await load()
    dispatch('changed')
  }

  function resetForm() {
    editingId = null
    draft = emptyDraft()
    formError = ''
    testResult = null
    testError = ''
  }

  function edit(rule: RedactionRule) {
    editingId = rule.id
    draft = draftFromRule(rule)
    formError = ''
    testResult = null
    testError = ''
  }

  async function save() {
    const problem = draftError(draft)
    if (problem) {
      formError = problem
      return
    }
    formError = ''
    busy = true
    try {
      if (editingId === null) await api.post(rulesUrl, { ...draftPayload(draft), enabled: true })
      else await api.patch(`${rulesUrl}/${editingId}`, draftPayload(draft))
      resetForm()
      await changed()
    } catch (err) {
      formError = err instanceof ApiError ? err.detail : 'Failed to save the rule'
    } finally {
      busy = false
    }
  }

  async function setEnabled(rule: RedactionRule, enabled: boolean, input: HTMLInputElement) {
    busy = true
    error = ''
    try {
      await api.patch(`${rulesUrl}/${rule.id}`, { enabled })
      await changed()
    } catch (err) {
      input.checked = rule.enabled
      error = err instanceof ApiError ? err.detail : 'Failed to update the rule'
    } finally {
      busy = false
    }
  }

  async function move(index: number, step: -1 | 1) {
    const updates = moveRule(rules, index, step)
    if (updates.length === 0) return
    busy = true
    error = ''
    try {
      for (const update of updates) await api.patch(`${rulesUrl}/${update.id}`, { order: update.order })
      await changed()
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to move the rule'
    } finally {
      busy = false
    }
  }

  async function remove(rule: RedactionRule) {
    const ok = await confirmAction({
      title: 'Delete redaction rule',
      message: `Delete "${rule.name}"? What it matches is no longer redacted.`,
      confirmLabel: 'Delete',
      danger: true,
    })
    if (!ok) return
    busy = true
    error = ''
    try {
      await api.delete(`${rulesUrl}/${rule.id}`)
      if (editingId === rule.id) resetForm()
      await changed()
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to delete the rule'
    } finally {
      busy = false
    }
  }

  async function test() {
    testError = ''
    testResult = null
    if (!draft.pattern) {
      testError = 'Enter a pattern to test.'
      return
    }
    const group = draftGroup(draft.secretGroup)
    if (Number.isNaN(group)) {
      testError = GROUP_ERROR
      return
    }
    testing = true
    try {
      // Runs on the text typed here, never on a file.
      testResult = await api.post<RedactionTestResult>('/redaction-rules/test', {
        pattern: draft.pattern,
        secret_group: group,
        sample,
      })
      segments = matchSegments(sample, testResult.matches)
    } catch (err) {
      testError = err instanceof ApiError ? err.detail : 'Failed to test the pattern'
    } finally {
      testing = false
    }
  }

  onMount(load)
</script>

<div class="redaction-editor">
  {#if error}
    <p class="error" role="alert">{error}</p>
  {/if}

  {#if loading}
    <p class="hint">Loading…</p>
  {:else}
    {#if rules.length === 0}
      <p class="empty">No custom rules.</p>
    {:else}
      <ul class="rule-list">
        {#each rules as rule, index (rule.id)}
          <li class="rule-row" data-testid="redaction-custom-rule">
            <div class="rule-main">
              <div class="rule-name">{rule.name}</div>
              <code class="rule-pattern">{rule.pattern}</code>
              <div class="rule-details">
                {rule.secret_group ? `replaces group ${rule.secret_group}` : 'replaces the whole match'} ·
                longest match {rule.max_length} bytes
              </div>
              {#if rule.notes}
                <div class="rule-notes">{rule.notes}</div>
              {/if}
            </div>
            <div class="rule-actions">
              <label class="switch" title={rule.enabled ? 'On' : 'Off'}>
                <input
                  type="checkbox"
                  aria-label={`Rule ${rule.name} on`}
                  checked={rule.enabled}
                  disabled={busy}
                  on:change={(e) => setEnabled(rule, e.currentTarget.checked, e.currentTarget)}
                />
                <span class="switch-track"></span>
              </label>
              <button
                type="button"
                class="btn btn-ghost btn-sm"
                aria-label={`Move ${rule.name} up`}
                disabled={busy || index === 0}
                on:click={() => move(index, -1)}>↑</button
              >
              <button
                type="button"
                class="btn btn-ghost btn-sm"
                aria-label={`Move ${rule.name} down`}
                disabled={busy || index === rules.length - 1}
                on:click={() => move(index, 1)}>↓</button
              >
              <button type="button" class="btn btn-ghost btn-sm" disabled={busy} on:click={() => edit(rule)}
                >Edit</button
              >
              <button type="button" class="btn btn-danger btn-sm" disabled={busy} on:click={() => remove(rule)}
                >Delete</button
              >
            </div>
          </li>
        {/each}
      </ul>
    {/if}

    <form class="rule-form" on:submit|preventDefault={save}>
      <h4>{editingId === null ? 'Add a custom rule' : 'Edit rule'}</h4>
      <div class="form-grid">
        <label for={`${idPrefix}-name`}>Name</label>
        <input
          id={`${idPrefix}-name`}
          class="input"
          maxlength="48"
          placeholder="order id"
          bind:value={draft.name}
        />
        <label for={`${idPrefix}-pattern`}>Pattern (RE2)</label>
        <input
          id={`${idPrefix}-pattern`}
          class="input mono"
          maxlength="1000"
          placeholder={'ORD-\\d{8}'}
          spellcheck="false"
          autocomplete="off"
          bind:value={draft.pattern}
        />
        <label for={`${idPrefix}-group`}>Group to replace</label>
        <input
          id={`${idPrefix}-group`}
          class="input narrow"
          type="number"
          min="0"
          step="1"
          placeholder="whole match"
          bind:value={draft.secretGroup}
        />
        <label for={`${idPrefix}-max-length`}>Longest match (bytes)</label>
        <input
          id={`${idPrefix}-max-length`}
          class="input narrow"
          type="number"
          min="1"
          max={MAX_RULE_LENGTH}
          step="1"
          bind:value={draft.maxLength}
        />
        <label for={`${idPrefix}-notes`}>Notes</label>
        <input id={`${idPrefix}-notes`} class="input" bind:value={draft.notes} />
      </div>
      <p class="hint">
        The name goes into each replacement (<code>[redacted: name]</code>). The pattern is RE2 (no
        backreferences or lookaround), runs line by line and can't match empty text. Group to replace keeps the
        rest of the match, e.g. a key before its value. Longest match: the most bytes the pattern can match, from 1
        to {MAX_RULE_LENGTH}; a pattern with a bounded length must fit in it.
      </p>

      <label class="sample-label" for={`${idPrefix}-sample`}>Try it on sample text</label>
      <textarea
        id={`${idPrefix}-sample`}
        class="input mono"
        rows="3"
        spellcheck="false"
        placeholder="Type or paste a sample line, never real secrets"
        bind:value={sample}
      ></textarea>
      <div class="form-actions">
        <button type="button" class="btn btn-ghost" disabled={testing} on:click={test}>
          {testing ? 'Testing…' : 'Test pattern'}
        </button>
        <button type="submit" class="btn btn-primary" disabled={busy}>
          {editingId === null ? 'Add rule' : 'Save rule'}
        </button>
        {#if editingId !== null}
          <button type="button" class="btn btn-ghost" on:click={resetForm}>Cancel</button>
        {/if}
      </div>
      {#if formError}
        <p class="error" role="alert">{formError}</p>
      {/if}
      {#if testError}
        <p class="error" role="alert">{testError}</p>
      {/if}
      {#if testResult}
        <div class="test-result" data-testid="redaction-test-result" role="status">
          <div class="hint">
            {testResult.matches.length === 1 ? '1 match' : `${testResult.matches.length} matches`}
          </div>
          <pre class="mono">{#each segments as segment}{#if segment.match}<mark>{segment.text}</mark
                >{:else}{segment.text}{/if}{/each}</pre>
          <div class="hint">Redacted:</div>
          <pre class="mono" data-testid="redaction-test-output">{testResult.redacted}</pre>
        </div>
      {/if}
    </form>
  {/if}
</div>

<style>
  .redaction-editor {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
  }
  .rule-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
  }
  .rule-row {
    display: flex;
    justify-content: space-between;
    gap: 0.75rem;
    background: var(--bg);
    border: 1px solid var(--border-soft);
    border-radius: var(--radius-sm);
    padding: 0.55rem 0.7rem;
  }
  .rule-main {
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
  }
  .rule-name {
    font-weight: 600;
    color: var(--text);
    font-size: 0.88rem;
  }
  .rule-pattern {
    font-family: var(--font-mono);
    font-size: 0.8rem;
    color: var(--text);
    word-break: break-all;
  }
  .rule-details,
  .rule-notes {
    font-size: 0.76rem;
    color: var(--text-faint);
  }
  .rule-actions {
    display: flex;
    align-items: center;
    gap: 0.35rem;
    flex: 0 0 auto;
  }
  .rule-form {
    display: flex;
    flex-direction: column;
    gap: 0.6rem;
    border-top: 1px solid var(--border-soft);
    padding-top: 0.75rem;
  }
  h4 {
    margin: 0;
    font-size: 0.9rem;
    color: var(--text);
  }
  .form-grid {
    display: grid;
    grid-template-columns: 11rem 1fr;
    gap: 0.45rem 0.75rem;
    align-items: center;
    font-size: 0.85rem;
    color: var(--text);
  }
  .narrow {
    width: 9rem;
  }
  .sample-label {
    font-size: 0.85rem;
    color: var(--text);
  }
  .form-actions {
    display: flex;
    gap: 0.5rem;
    align-items: center;
  }
  .test-result {
    display: flex;
    flex-direction: column;
    gap: 0.3rem;
  }
  .test-result pre {
    margin: 0;
    padding: 0.5rem 0.6rem;
    background: var(--bg-elevated-2);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    white-space: pre-wrap;
    word-break: break-all;
    font-size: 0.8rem;
    color: var(--text);
  }
  mark {
    background: var(--warning-soft);
    color: var(--text);
  }
  .mono {
    font-family: var(--font-mono);
  }
  .hint {
    font-size: 0.78rem;
    color: var(--text-faint);
    margin: 0;
    line-height: 1.5;
  }
  .empty {
    color: var(--text-faint);
    margin: 0;
    font-size: 0.85rem;
  }
  .error {
    color: var(--danger);
    margin: 0;
  }
</style>
