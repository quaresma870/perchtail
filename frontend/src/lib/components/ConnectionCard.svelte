<script lang="ts">
  import type { Protocol, Source } from '../types'

  export let source: Source

  const PROTOCOL_LABEL: Record<Protocol, string> = {
    ssh: 'SSH',
    smb: 'SMB',
    winrm: 'WinRM',
    local: 'Local',
    agent: 'Agent',
  }

  const subtitle = [source.customer_name, source.folder_name].filter(Boolean).join(' / ')
</script>

<button class="card" on:click>
  <div class="top">
    <span class="name">{source.name}</span>
    {#if source.is_system}
      <span class="badge badge-accent">system</span>
    {/if}
  </div>
  {#if subtitle}
    <div class="subtitle">{subtitle}</div>
  {/if}
  <div class="bottom">
    <span class="badge protocol-{source.protocol}">{PROTOCOL_LABEL[source.protocol]}</span>
    <span class="host">{source.host}</span>
  </div>
</button>

<style>
  .card {
    display: flex;
    flex-direction: column;
    gap: 0.3rem;
    width: 100%;
    text-align: left;
    padding: 0.75rem 0.9rem;
    cursor: pointer;
  }
  .top {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }
  .name {
    font-weight: 600;
    color: var(--text);
    font-size: 0.92rem;
  }
  .subtitle {
    font-size: 0.78rem;
    color: var(--text-faint);
  }
  .bottom {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin-top: 0.15rem;
  }
  .host {
    font-size: 0.78rem;
    color: var(--text-muted);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .badge-accent {
    background: var(--accent-soft);
    color: var(--accent-hover);
  }
  .protocol-ssh {
    background: var(--protocol-ssh-bg);
    color: var(--protocol-ssh-text);
  }
  .protocol-smb {
    background: var(--protocol-smb-bg);
    color: var(--protocol-smb-text);
  }
  .protocol-winrm {
    background: var(--protocol-winrm-bg);
    color: var(--protocol-winrm-text);
  }
  .protocol-local {
    background: var(--protocol-local-bg);
    color: var(--protocol-local-text);
  }
  .protocol-agent {
    background: var(--protocol-agent-bg);
    color: var(--protocol-agent-text);
  }
</style>
