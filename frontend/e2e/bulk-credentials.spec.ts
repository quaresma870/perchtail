import { expect, test } from '@playwright/test'

// #248: one new credential set on several sources at once. The hosts are
// never reached (nothing answers at .invalid), so the test-first check is
// turned off.
const stamp = Date.now()
const names = [`E2E Bulk Cred A ${stamp}`, `E2E Bulk Cred B ${stamp}`]

test('bulk credential update: select two sources, update both', async ({ page }) => {
  const ids: number[] = []
  try {
    for (const name of names) {
      const created = await page.request.post('/sources', {
        data: { name, protocol: 'ssh', host: 'bulk-cred.invalid', port: 22, base_path: '/var/log' },
      })
      expect(created.ok()).toBeTruthy()
      ids.push((await created.json()).id)
    }

    await page.goto('/#/settings/sources')
    await page.getByLabel('Filter sources').fill(`Bulk Cred ${stamp}`)
    for (const name of names) {
      await page.getByRole('checkbox', { name: `Select ${name}` }).check()
    }
    await page.getByRole('button', { name: 'Update credentials (2)' }).click()

    const dialog = page.getByRole('dialog', { name: 'Update credentials' })
    await dialog.getByLabel('Username').fill('svc-rotated')
    await dialog.getByLabel('New password').fill('rotated-secret-1')
    await dialog.getByLabel(/Test the new credential on each source first/).uncheck()
    await dialog.getByRole('button', { name: 'Update', exact: true }).click()
    await expect(dialog).toContainText('2 updated')
    await expect(dialog).toContainText('0 failed')
    await dialog.getByRole('button', { name: 'Done', exact: true }).click()
    await expect(dialog).toHaveCount(0)

    for (const name of names) {
      await expect(page.getByRole('row', { name: new RegExp(name) })).toContainText('today')
    }
    await page.getByRole('button', { name: 'Clear filters' }).click()
  } finally {
    for (const id of ids) await page.request.delete(`/sources/${id}`)
  }
})
