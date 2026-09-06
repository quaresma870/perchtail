import { expect, test, type Page } from '@playwright/test'

// app/severity_patterns.py's seed_default_global_patterns means this list
// is never actually empty on a fresh deployment -- assertions below target
// this test's own pattern by its unique text, not list emptiness.
const patternText = `e2e-marker-${Date.now()}`

async function patternInputValues(page: Page): Promise<string[]> {
  return page
    .locator('.pattern-row input.pattern-input')
    .evaluateAll((els) => els.map((el) => (el as HTMLInputElement).value))
}

// The pattern text lives in an <input value=...>, which isn't matchable via
// `hasText` (that checks element text content, and an input's current value
// isn't part of it) or any built-in Playwright locator -- read every row's
// current value directly instead, polling since the row only appears once
// addPattern()'s own POST + reload round trip finishes.
async function waitForPatternRowIndex(page: Page, value: string): Promise<number> {
  let foundIndex = -1
  await expect
    .poll(async () => {
      foundIndex = (await patternInputValues(page)).indexOf(value)
      return foundIndex
    })
    .not.toBe(-1)
  return foundIndex
}

test('severity indicators: add a global pattern, toggle a flag, delete it', async ({ page }) => {
  await page.goto('/#/settings/severity-indicators')

  const addRow = page.locator('.add-row')
  await addRow.locator('select.level-select').selectOption('warning')
  await addRow.getByPlaceholder('panic or re:\\berror\\b').fill(patternText)
  await addRow.getByRole('button', { name: 'Add pattern' }).click()

  const row = page.locator('.pattern-row').nth(await waitForPatternRowIndex(page, patternText))
  await expect(row.locator('select.level-select')).toHaveValue('warning')

  await row.locator('label.flag', { hasText: 'line' }).locator('input[type=checkbox]').check()
  await page.reload()
  const rowAfterReload = page
    .locator('.pattern-row')
    .nth(await waitForPatternRowIndex(page, patternText))
  await expect(
    rowAfterReload.locator('label.flag', { hasText: 'line' }).locator('input[type=checkbox]'),
  ).toBeChecked()

  await rowAfterReload.getByRole('button', { name: 'delete' }).click()
  await expect
    .poll(async () => (await patternInputValues(page)).includes(patternText))
    .toBe(false)
})
