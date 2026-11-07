import { writable } from 'svelte/store'

/** A question asked in a popup instead of the browser's confirm() (#301). */
export interface ConfirmRequest {
  title: string
  message: string
  confirmLabel: string
  /** Shown as a destructive action (delete, revoke...). */
  danger: boolean
  resolve: (ok: boolean) => void
}

export const confirmRequest = writable<ConfirmRequest | null>(null)

/** Asks in the app's confirmation popup (ConfirmHost, mounted once in
 * App.svelte); resolves to true only if the user confirms. A new question
 * while one is open answers the open one with false. */
export function confirmAction(options: {
  title: string
  message: string
  confirmLabel?: string
  danger?: boolean
}): Promise<boolean> {
  return new Promise((resolve) => {
    confirmRequest.update((open) => {
      open?.resolve(false)
      return {
        title: options.title,
        message: options.message,
        confirmLabel: options.confirmLabel ?? 'Confirm',
        danger: options.danger ?? false,
        resolve,
      }
    })
  })
}

/** Answers the open question and closes the popup. */
export function answerConfirm(ok: boolean) {
  confirmRequest.update((open) => {
    open?.resolve(ok)
    return null
  })
}
