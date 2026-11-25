<script lang="ts">
  import { createEventDispatcher } from 'svelte'
  import { progressValues, statusLabel, type OpenStatus } from '../open-progress'

  /** A file being opened whole, shown once it takes more than a second
   * (or at once while it waits in the viewer's queue): how much was copied
   * from the source, and Cancel. */
  export let name: string
  export let sourceName: string
  export let status: OpenStatus
  /** The time the label is worked out for (ms), so "Finishing…" shows. */
  export let now: number

  const dispatch = createEventDispatcher<{ cancel: void }>()

  $: label = statusLabel(status, now)
  $: values = status.queued || status.receiving ? null : progressValues(status.progress)
  $: percent = values ? Math.round((values.now / values.max) * 100) : null
</script>

<div class="open-progress" data-testid="open-progress" class:queued={status.queued}>
  <span class="name">Opening {name}</span>
  <span class="source">{sourceName}</span>
  <div
    class="bar"
    class:unknown={percent === null}
    role="progressbar"
    aria-label={`Opening ${name} on ${sourceName}`}
    aria-valuemin={values ? 0 : undefined}
    aria-valuenow={values?.now}
    aria-valuemax={values?.max}
    aria-valuetext={label}
  >
    <div class="fill" style:width={percent === null ? undefined : `${percent}%`}></div>
  </div>
  <span class="label">{label}</span>
  <button class="btn btn-ghost cancel" on:click={() => dispatch('cancel')}>Cancel</button>
</div>

<style>
  .open-progress {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 0.5rem 0.9rem;
    padding: 0.4rem 0.9rem;
    border-bottom: 1px solid var(--border-soft);
    background: var(--bg-elevated);
    color: var(--text);
    font-size: 0.82rem;
  }
  .name {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    max-width: 40ch;
  }
  .source {
    color: var(--text-muted);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    max-width: 24ch;
  }
  .bar {
    flex: 1 1 8rem;
    min-width: 8rem;
    height: 0.5rem;
    border-radius: 999px;
    background: var(--border-soft);
    overflow: hidden;
  }
  .fill {
    height: 100%;
    background: var(--accent);
    transition: width 0.3s ease;
  }
  .unknown .fill {
    width: 30%;
    opacity: 0.6;
  }
  .queued .fill {
    width: 0;
  }
  .label {
    color: var(--text-muted);
    white-space: nowrap;
  }
  .cancel {
    padding: 0.2rem 0.7rem;
  }
</style>
