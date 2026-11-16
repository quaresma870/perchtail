import { writable } from 'svelte/store'

// A message the login page shows once, set by the page that sends the user
// there (a finished password reset, #244). Kept in memory, not in the URL.
export const loginNotice = writable('')
