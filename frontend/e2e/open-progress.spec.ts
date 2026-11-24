import { expect, test } from '@playwright/test'
import { SLOW_FILE_NAME, slowSource } from './slow-open'

// Opening a file whole that takes more than a second shows how much of it
// was copied from the source, with Cancel, which stops the read and closes
// what was opening without a message.

test('a slow open shows its progress, and Cancel stops it', async ({ page }) => {
  const { sourceId, cleanUp } = await slowSource(page.request, `e2e-slow-open-${Date.now()}`)
  try {
    await page.goto(`/#/viewer/${sourceId}`)
    const tree = page.locator('.tree-body')

    // A file that opens at once shows nothing.
    await tree.getByRole('button', { name: 'hello.log' }).click({ timeout: 20_000 })
    await expect(page.locator('.tab.active .tab-label')).toContainText('hello.log')
    await expect(page.getByTestId('open-progress')).toHaveCount(0)

    const cancels: string[] = []
    page.on('request', (request) => {
      if (request.url().includes('/open-cancel?')) cancels.push(request.method())
    })
    await tree.getByRole('button', { name: SLOW_FILE_NAME }).click()
    const bar = page.getByRole('progressbar', { name: `Opening ${SLOW_FILE_NAME}` })
    await expect(bar).toBeVisible({ timeout: 5_000 })
    await expect(bar).toHaveAttribute('aria-valuetext', /^[\d.]+ (B|KB|MB) of 9(\.\d)? MB$/, { timeout: 5_000 })
    const first = Number(await bar.getAttribute('aria-valuenow'))
    await expect.poll(async () => Number(await bar.getAttribute('aria-valuenow')), { timeout: 8_000 }).toBeGreaterThan(first)
    expect(Number(await bar.getAttribute('aria-valuemax'))).toBeGreaterThan(9_000_000)

    // Cancel, by keyboard: the bar goes, no tab opens, nothing is said.
    const cancel = page.getByTestId('open-progress').getByRole('button', { name: 'Cancel', exact: true })
    await cancel.focus()
    await page.keyboard.press('Enter')
    await expect(page.getByTestId('open-progress')).toHaveCount(0)
    expect(cancels).toEqual(['POST'])
    await expect(page.locator('.tab-label', { hasText: SLOW_FILE_NAME })).toHaveCount(0)
    await expect(page.getByRole('alert')).toHaveCount(0)
    await expect(page.locator('.tab.active .tab-label')).toContainText('hello.log')
  } finally {
    await page.goto('/#/viewer')
    await cleanUp()
  }
})
