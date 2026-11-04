import { writable } from 'svelte/store'

/** Set by the source editor right after a create, so the overview it lands
 * on can say "Source created" without putting a flag in the URL. */
export const justCreatedSourceId = writable<number | null>(null)
