import { expect, test } from '@playwright/test'

// Every other spec in this suite generates real AuditLog rows (source/rule/
// role/user/SSO/severity-pattern CRUD) just by running, so by the time this
// spec runs there's plenty of history to page/filter over -- no seeding
// needed. This test instead creates one entry with a distinctive value of
// its own (an unusual retention-days number), so it can find *that* row
// specifically rather than asserting on the table being non-empty in
// general, then restores the setting it changed. Derived from Date.now(),
// not a fixed literal (same convention as severity-indicators.spec.ts's
// patternText) -- a fixed value would collide with a leftover row from a
// previous run of this same spec against a backend that wasn't restarted
// in between, a real local workflow this suite's own webServer.
// reuseExistingServer setting explicitly supports.
const marker = Date.now()

test('audit log: retention control writes a system_settings.update entry, filter by type finds it', async ({
  page,
}) => {
  await page.goto('/#/settings/system')

  const retentionInput = page.locator('.retention-input')
  const originalValue = await retentionInput.inputValue()
  await retentionInput.fill(String(marker))
  await page.locator('.retention-row').getByRole('button', { name: 'Save' }).click()
  await expect(retentionInput).toHaveValue(String(marker))

  await page.goto('/#/settings/audit-log')

  // "Type" is a chip-style checkbox list built from whatever target_types
  // are actually in the table (see backend/app/api/audit.py's GET
  // /audit/filters) -- "system_settings" is guaranteed present since the
  // retention save above just wrote one.
  await page.locator('.chip', { hasText: 'system_settings' }).click()

  const row = page.locator('tbody tr', { hasText: `"audit_retention_days":${marker}` })
  await expect(row).toBeVisible()
  await expect(row.locator('td').nth(2)).toContainText('system_settings.update')

  // Restore the setting so other specs (and a re-run of this one) see the
  // documented default rather than this test's leftover value.
  await page.goto('/#/settings/system')
  await page.locator('.retention-input').fill(originalValue)
  await page.locator('.retention-row').getByRole('button', { name: 'Save' }).click()
  await expect(page.locator('.retention-input')).toHaveValue(originalValue)
})

test('audit log: action multi-select and date-range filters narrow the table', async ({
  page,
}) => {
  await page.goto('/#/settings/audit-log')

  const actionSelect = page.locator('.action-select')
  await expect(actionSelect).toBeVisible()
  await actionSelect.selectOption('system_settings.update')
  await expect(page.locator('tbody tr').first()).toBeVisible()
  await expect(
    page.locator('tbody tr td:nth-child(3)').first(),
  ).toContainText('system_settings.update')

  // A since filter set well into the future excludes every existing row --
  // proves the date-range control actually round-trips to the backend
  // instead of being cosmetic. +25h (not +1m) so no plausible local-vs-UTC
  // offset in a datetime-local input (interpreted as wall-clock local time,
  // no timezone) could accidentally still land in the past.
  const future = new Date(Date.now() + 25 * 60 * 60 * 1000).toISOString().slice(0, 16)
  await page.locator('input[type=datetime-local]').first().fill(future)
  await expect(page.getByText('No matching audit log entries.')).toBeVisible()

  await page.getByRole('button', { name: 'Clear filters' }).click()
  await expect(page.locator('tbody tr').first()).toBeVisible()
})
