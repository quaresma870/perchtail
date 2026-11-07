<script lang="ts">
  import { push } from 'svelte-spa-router'
  import Modal from '../lib/components/Modal.svelte'
  import { confirmAction } from '../lib/confirm'
  import RoleEditor from './RoleEditor.svelte'
  import Roles from './Roles.svelte'

  /** #/settings/roles/new and #/settings/roles/:id (#302): the role editor
   * in a popup over the roles list. The address stays the editor's, so
   * links, back and reload still work; creating a role moves on to its
   * own address, to add its grants. */
  export let params: { id?: string } = {}

  $: isNew = !params.id
  let editor: RoleEditor | null = null

  async function close() {
    if (
      editor?.isDirty() &&
      !(await confirmAction({
        title: 'Discard changes',
        message: 'Close without saving? The changes to this role will be lost.',
        confirmLabel: 'Discard',
        danger: true,
      }))
    )
      return
    push('/settings/roles')
  }
</script>

<!-- Reloaded when the popup moves to another role (created or duplicated). -->
{#key params.id}
  <Roles />
{/key}

{#key params.id}
  <Modal title={isNew ? 'New role' : 'Edit role'} wide on:close={close}>
    <RoleEditor {params} embedded bind:this={editor} on:saved={() => push('/settings/roles')} on:cancel={close} />
  </Modal>
{/key}
