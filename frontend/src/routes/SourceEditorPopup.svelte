<script lang="ts">
  import { push } from 'svelte-spa-router'
  import Modal from '../lib/components/Modal.svelte'
  import { confirmAction } from '../lib/confirm'
  import { currentHash } from '../lib/hash'
  import SourceEditor from './SourceEditor.svelte'
  import SourceOverview from './SourceOverview.svelte'
  import Sources from './Sources.svelte'

  /** #/settings/sources/new and #/settings/sources/:id/edit (#302): the
   * source editor in a popup over the page it was opened from -- the
   * sources list (new, or Edit in the list: ?from=list) or the source's
   * own page. The address stays the editor's, so links, back and reload
   * still work. */
  export let params: { id?: string } = {}

  $: isNew = !params.id
  $: fromList = new URLSearchParams($currentHash.split('?')[1] ?? '').get('from') === 'list'
  $: behind = isNew || fromList ? '/settings/sources' : `/settings/sources/${params.id}`

  let editor: SourceEditor | null = null

  async function close() {
    if (
      editor?.isDirty() &&
      !(await confirmAction({
        title: 'Discard changes',
        message: 'Close without saving? The changes to this source will be lost.',
        confirmLabel: 'Discard',
        danger: true,
      }))
    )
      return
    push(behind)
  }
</script>

{#if isNew || fromList}
  <Sources />
{:else}
  <SourceOverview {params} />
{/if}

{#key params.id}
  <Modal title={isNew ? 'New source' : 'Edit source'} wide on:close={close}>
    <SourceEditor {params} embedded bind:this={editor} on:saved={() => push(behind)} on:cancel={close} />
  </Modal>
{/key}
