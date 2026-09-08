<script lang="ts">
  import SettingsNav from '../lib/components/SettingsNav.svelte'
  import SeverityPatternEditor from '../lib/components/SeverityPatternEditor.svelte'
</script>

<SettingsNav />

<div class="page">
  <h1>Severity indicators</h1>
  <p class="hint">
    The global default pattern set used to highlight matching text in the Viewer, for every
    source that doesn't define its own override (set from that source's editor page). Display-only
    — never affects what's fetchable, only how it's colored.
  </p>

  <SeverityPatternEditor baseUrl="/severity-patterns" />
</div>

<style>
  .page {
    padding: 1.75rem 2rem;
    max-width: 760px;
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
</style>
