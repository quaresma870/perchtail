<script lang="ts">
  import { onMount } from 'svelte'
  import { api, ApiError } from '../lib/api'
  import { confirmAction } from '../lib/confirm'
  import RedactionRuleEditor from '../lib/components/RedactionRuleEditor.svelte'
  import SettingsNav from '../lib/components/SettingsNav.svelte'
  import { builtinRules, detectorInfo } from '../lib/redaction-rules'
  import { systemSettings } from '../lib/settings'
  import type { RedactionRule, SystemSettings } from '../lib/types'

  // Redaction (#382, #249): super admins only (the route and the API both
  // refuse anyone else). Everything here applies to every user, super
  // admins included.
  const REDACTION_GUIDE = 'https://github.com/quaresma870/perchtail/blob/main/docs/redaction.md'

  let loading = true
  let error = ''
  let saving = false

  let enabled = false
  let downloads = true
  let suffix = true
  let rules: RedactionRule[] = []

  $: builtins = builtinRules(rules)
  $: anyRuleOn = rules.some((rule) => rule.enabled)

  function apply(settings: SystemSettings) {
    enabled = settings.redaction_enabled
    downloads = settings.redaction_downloads
    suffix = settings.redaction_download_suffix
  }

  async function loadRules() {
    rules = await api.get<RedactionRule[]>('/redaction-rules')
  }

  onMount(async () => {
    try {
      apply(await api.get<SystemSettings>('/system-settings'))
      await loadRules()
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to load the redaction settings'
    } finally {
      loading = false
    }
  })

  /** Saves one setting; whatever the answer, the switch then shows the
   * setting as it is (`current`). */
  async function patch(change: Partial<SystemSettings>, input: HTMLInputElement, current: () => boolean) {
    saving = true
    error = ''
    try {
      const settings = await api.patch<SystemSettings>('/system-settings', change)
      apply(settings)
      systemSettings.set(settings)
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to update the redaction settings'
    } finally {
      input.checked = current()
      saving = false
    }
  }

  async function toggleEnabled(event: Event) {
    const input = event.currentTarget as HTMLInputElement
    const next = input.checked
    input.checked = enabled
    const ok = await confirmAction(
      next
        ? {
            title: 'Turn redaction on',
            message:
              'File content will be redacted with the rules that are on, for every user, super admins included: in the viewer, Tail, alerts and, unless turned off below, downloads.',
            confirmLabel: 'Turn on',
          }
        : {
            title: 'Turn redaction off',
            message: 'Everyone who can open a file will see its original content, and alerts will send it to their webhooks.',
            confirmLabel: 'Turn off',
            danger: true,
          },
    )
    if (!ok) return
    input.checked = next
    await patch({ redaction_enabled: next }, input, () => enabled)
  }

  async function setBuiltin(rule: RedactionRule, input: HTMLInputElement) {
    const next = input.checked
    saving = true
    error = ''
    try {
      await api.patch(`/redaction-rules/${rule.id}`, { enabled: next })
      await loadRules()
    } catch (err) {
      input.checked = rule.enabled
      error = err instanceof ApiError ? err.detail : 'Failed to update the rule'
    } finally {
      saving = false
    }
  }

  async function customChanged() {
    try {
      await loadRules()
    } catch {
      // The editor shows its own error.
    }
  }
</script>

<SettingsNav />

<div class="page">
  <h1>Redaction</h1>
  <p class="hint">
    Replaces secrets, card numbers and personal data in file content with <code>[redacted: …]</code> before it
    reaches anyone: on the server, or on a push agent's host when the agent can. It applies to every user, super
    admins included, and every change here is audited. Redaction is best effort: a rule only finds what it was written
    to find, so check it on your own logs. File names aren't redacted.
    <a href={REDACTION_GUIDE} target="_blank" rel="noopener noreferrer">How redaction works</a>
  </p>

  {#if loading}
    <p class="hint">Loading…</p>
  {:else}
    {#if error}
      <p class="error" role="alert">{error}</p>
    {/if}

    <section class="card">
      <h2 class="section-title">Redaction</h2>
      <div class="setting-row">
        <div>
          <div class="setting-name">Redact file content</div>
          <p class="hint">
            The viewer, Tail and alerts get file content with the rules below applied, for everyone, super admins
            included. A source's own rules and the system rules it turns off are set in that source's editor.
          </p>
          {#if enabled && !anyRuleOn}
            <p class="warning" role="status">
              On, but no system rule is on: only sources with rules of their own are redacted.
            </p>
          {/if}
        </div>
        <label class="switch">
          <input
            type="checkbox"
            aria-label="Redact file content"
            checked={enabled}
            disabled={saving}
            on:change={toggleEnabled}
          />
          <span class="switch-track"></span>
        </label>
      </div>
      <div class="setting-row">
        <div>
          <div class="setting-name">Redact downloads too</div>
          <p class="hint">
            Single files, archives and their members, and folder ZIPs are redacted as they're sent. Off, downloads
            carry the original content; the viewer, Tail and alerts stay redacted.
          </p>
        </div>
        <label class="switch">
          <input
            type="checkbox"
            aria-label="Redact downloads too"
            checked={downloads}
            disabled={saving}
            on:change={(e) => patch({ redaction_downloads: e.currentTarget.checked }, e.currentTarget, () => downloads)}
          />
          <span class="switch-track"></span>
        </label>
      </div>
      <div class="setting-row">
        <div>
          <div class="setting-name">Say so in the file name</div>
          <p class="hint">
            A redacted download is named for it (<code>app.redacted.log</code>). It always carries the
            <code>X-PerchTail-Redacted</code> header.
          </p>
        </div>
        <label class="switch">
          <input
            type="checkbox"
            aria-label="Say so in the file name"
            checked={suffix}
            disabled={saving || !downloads}
            on:change={(e) => patch({ redaction_download_suffix: e.currentTarget.checked }, e.currentTarget, () => suffix)}
          />
          <span class="switch-track"></span>
        </label>
      </div>
    </section>

    <section class="card">
      <h2 class="section-title">Built-in rules</h2>
      <p class="hint section-hint">
        Each one applies to every source unless the source turns it off. All are off on a new install.
      </p>
      <ul class="builtin-list">
        {#each builtins as rule (rule.id)}
          {@const info = detectorInfo(rule.detector ?? '')}
          <li class="setting-row" data-testid={`redaction-builtin-${rule.detector}`}>
            <div>
              <div class="setting-name">{info.title} <code class="detector">{rule.name}</code></div>
              <p class="hint">{info.description}</p>
              <p class="hint"><strong>False positives:</strong> {info.falsePositives}</p>
            </div>
            <label class="switch">
              <input
                type="checkbox"
                aria-label={`${info.title} on`}
                checked={rule.enabled}
                disabled={saving}
                on:change={(e) => setBuiltin(rule, e.currentTarget)}
              />
              <span class="switch-track"></span>
            </label>
          </li>
        {/each}
      </ul>
    </section>

    <section class="card custom">
      <h2 class="section-title">Custom rules for every source</h2>
      <p class="hint section-hint">
        Your own patterns, applied to every source unless it turns them off, after the built-in rules.
      </p>
      <RedactionRuleEditor rulesUrl="/redaction-rules" idPrefix="system-redaction" on:changed={customChanged} />
    </section>
  {/if}
</div>

<style>
  .page {
    padding: 1.75rem 2rem;
    max-width: 860px;
    display: flex;
    flex-direction: column;
    gap: 1.1rem;
  }
  h1 {
    font-size: 1.4rem;
    margin: 0;
    color: var(--text);
  }
  .hint {
    font-size: 0.85rem;
    color: var(--text-faint);
    margin: 0;
    line-height: 1.5;
  }
  .card {
    display: flex;
    flex-direction: column;
  }
  .card.custom {
    padding-bottom: 1.1rem;
  }
  .card.custom :global(.redaction-editor) {
    padding: 0 1.5rem;
  }
  .section-title {
    font-size: 0.8rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--text-muted);
    margin: 0.9rem 1.1rem 0.2rem;
  }
  .section-hint {
    margin: 0 1.5rem 0.6rem;
  }
  .builtin-list {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .setting-row {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 1.5rem;
    padding: 1rem 1.5rem;
    border-bottom: 1px solid var(--border-soft);
  }
  .setting-row:last-child {
    border-bottom: none;
  }
  .setting-row .hint + .hint {
    margin-top: 0.25rem;
  }
  .setting-name {
    font-size: 0.92rem;
    font-weight: 600;
    color: var(--text);
    margin-bottom: 0.25rem;
  }
  .detector {
    font-family: var(--font-mono);
    font-size: 0.75rem;
    font-weight: 400;
    color: var(--text-faint);
    margin-left: 0.35rem;
  }
  .setting-row .switch {
    flex: 0 0 auto;
    margin-top: 0.1rem;
  }
  .warning {
    color: var(--warning);
    margin: 0.35rem 0 0;
    font-size: 0.85rem;
  }
  .error {
    color: var(--danger);
    margin: 0;
  }
</style>
