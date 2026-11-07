import { get } from 'svelte/store'
import { describe, expect, it } from 'vitest'
import { answerConfirm, confirmAction, confirmRequest } from './confirm'

describe('confirmAction', () => {
  it('opens a request and resolves with the answer', async () => {
    const answer = confirmAction({ title: 'Delete role', message: 'Delete "ops"?', confirmLabel: 'Delete', danger: true })
    expect(get(confirmRequest)).toMatchObject({ title: 'Delete role', confirmLabel: 'Delete', danger: true })
    answerConfirm(true)
    await expect(answer).resolves.toBe(true)
    expect(get(confirmRequest)).toBeNull()
  })

  it('defaults the label and answers an open question with false when another one comes', async () => {
    const first = confirmAction({ title: 'A', message: 'a' })
    const second = confirmAction({ title: 'B', message: 'b' })
    await expect(first).resolves.toBe(false)
    expect(get(confirmRequest)).toMatchObject({ title: 'B', confirmLabel: 'Confirm', danger: false })
    answerConfirm(false)
    await expect(second).resolves.toBe(false)
  })
})
