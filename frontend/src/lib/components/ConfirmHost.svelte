<script lang="ts">
  import { answerConfirm, confirmRequest } from '../confirm'
  import Modal from './Modal.svelte'
</script>

{#if $confirmRequest}
  {#key $confirmRequest}
    <Modal title={$confirmRequest.title} on:close={() => answerConfirm(false)}>
      <p class="message">{$confirmRequest.message}</p>
      <svelte:fragment slot="footer">
        <button class="btn btn-ghost" type="button" on:click={() => answerConfirm(false)}>Cancel</button>
        <button
          class="btn"
          class:btn-primary={!$confirmRequest.danger}
          class:btn-danger-solid={$confirmRequest.danger}
          type="button"
          on:click={() => answerConfirm(true)}>{$confirmRequest.confirmLabel}</button
        >
      </svelte:fragment>
    </Modal>
  {/key}
{/if}

<style>
  .message {
    margin: 0;
    font-size: 0.88rem;
    color: var(--text-muted);
    line-height: 1.5;
  }
</style>
