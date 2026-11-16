<script lang="ts">
  import { viaText, type AccessGroup } from '../access-report'
  import { CAPABILITY_HINTS, CAPABILITY_LABELS } from '../capabilities'

  /** One customer or folder of a user's access in Settings -> Access review
   * (#384), collapsed unless `expand` (set while searching), with the
   * folders below it and the sources in it. */
  export let group: AccessGroup
  export let expand = false

  const KIND_LABELS: Record<AccessGroup['kind'], string> = {
    customer: 'Customer',
    folder: 'Folder',
    system: 'System',
  }
</script>

<details class="group" open={expand}>
  <summary>
    <span class="kind">{KIND_LABELS[group.kind]}</span>
    <span class="label"
      >{#if group.truncated}<span
          class="truncated"
          title="The folders above this one are outside your scope">… /</span
        >
      {/if}{group.label}</span
    >
    <span class="count">{group.count} {group.count === 1 ? 'source' : 'sources'}</span>
  </summary>
  <div class="children">
    {#each group.groups as child (child.key)}
      <svelte:self group={child} {expand} />
    {/each}
    {#each group.sources as entry (entry.source_id)}
      <div class="source">
        <span class="source-name">{entry.source_name}</span>
        <span class="caps">
          {#each entry.capabilities as cap (cap)}
            <span class="badge badge-accent" title={CAPABILITY_HINTS[cap]}>{CAPABILITY_LABELS[cap]}</span>
          {/each}
        </span>
        <span class="via">
          {#each entry.via as v, i (i)}
            <span>{viaText(v)}</span>
          {/each}
        </span>
      </div>
    {/each}
  </div>
</details>

<style>
  .group {
    margin: 0.15rem 0;
  }
  summary {
    cursor: pointer;
    display: flex;
    align-items: baseline;
    gap: 0.5rem;
    padding: 0.25rem 0;
    font-size: 0.84rem;
    color: var(--text);
  }
  .kind {
    font-size: 0.68rem;
    color: var(--text-faint);
    text-transform: uppercase;
    letter-spacing: 0.03em;
    min-width: 4.5rem;
  }
  .label {
    font-weight: 500;
  }
  .truncated {
    color: var(--text-faint);
    margin-right: 0.2rem;
  }
  .count {
    font-size: 0.75rem;
    color: var(--text-muted);
  }
  .children {
    padding-left: 1.25rem;
    border-left: 1px solid var(--border-soft);
    margin-left: 0.35rem;
  }
  .source {
    display: grid;
    grid-template-columns: minmax(10rem, 1fr) minmax(10rem, 1.2fr) minmax(12rem, 2fr);
    gap: 0.75rem;
    align-items: baseline;
    padding: 0.3rem 0;
    font-size: 0.82rem;
    border-bottom: 1px solid var(--border-soft);
  }
  .source:last-child {
    border-bottom: none;
  }
  .source-name {
    color: var(--text);
    font-weight: 500;
  }
  .caps {
    display: flex;
    gap: 0.3rem;
    flex-wrap: wrap;
  }
  .via {
    display: flex;
    flex-direction: column;
    font-size: 0.75rem;
    color: var(--text-muted);
  }
  .badge-accent {
    background: var(--accent-soft);
    color: var(--accent-hover);
  }
</style>
