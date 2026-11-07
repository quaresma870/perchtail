import { expect, test } from '@playwright/test'

// Incremental follow (#254) on the built-in log source: the first poll is
// audited, which writes a file.follow line to the very log being followed,
// so the next poll has something new to append -- fetched alone, not by
// reloading the whole file.
const SYSTEM_SOURCE_NAME = 'PerchTail application logs'

test('follow appends only the new lines of a growing log', async ({ page }) => {
  await page.goto('/#/viewer')
  await page.locator('.explorer').getByRole('button', { name: SYSTEM_SOURCE_NAME }).click()
  await page.locator('.tree-body').getByRole('button', { name: 'perchtail.log' }).click()
  await expect(page.locator('.tab.active .tab-label')).toContainText('perchtail.log')
  await expect(page.locator('.cm-content')).not.toBeEmpty()
  // Read from the end, so new lines scroll into view as they arrive.
  await page.locator('.cm-content').click()
  await page.keyboard.press('Control+End')

  const reloads: string[] = []
  page.on('request', (request) => {
    if (request.url().includes('/open?')) reloads.push(request.url())
  })
  const appended = page.waitForResponse(
    async (response) =>
      response.url().includes('/follow?') &&
      response.status() === 200 &&
      (await response.text()).includes('file.follow'),
    { timeout: 20_000 },
  )

  await page.getByRole('button', { name: 'Tail', exact: true }).click()
  // The interval comes from Settings -> System (#283) and shows while Tail is on.
  await expect(page.getByText('every 2 s', { exact: true })).toBeVisible()
  await appended

  await expect(page.locator('.cm-content')).toContainText('file.follow', { timeout: 10_000 })
  expect(reloads).toEqual([])

  await page.getByRole('button', { name: 'Tail', exact: true }).click()
})

test('the new-lines notice stays over the log with Find all open', async ({ page }) => {
  // A short window, so even a short log is taller than the editor and Tail
  // raises the notice instead of scrolling.
  await page.setViewportSize({ width: 1280, height: 560 })
  await page.goto('/#/viewer')
  await page.locator('.explorer').getByRole('button', { name: SYSTEM_SOURCE_NAME }).click()
  await page.locator('.tree-body').getByRole('button', { name: 'perchtail.log' }).click()
  await expect(page.locator('.cm-content')).not.toBeEmpty()
  // Away from the end, so new lines raise the notice instead of scrolling.
  // Clicked at the left edge: the middle of a long line scrolls sideways.
  await page.locator('.cm-content').click({ position: { x: 5, y: 5 } })
  await page.keyboard.press('Control+Home')

  await page.getByRole('button', { name: 'Find all', exact: true }).click()
  const panel = page.locator('.find-all-panel')
  await expect(panel).toBeVisible()
  await page.getByRole('button', { name: 'Tail', exact: true }).click()

  const notice = page.locator('.jump-to-end')
  await expect(notice).toBeVisible({ timeout: 20_000 })
  const noticeBox = await notice.boundingBox()
  const panelBox = await panel.boundingBox()
  expect(noticeBox!.y + noticeBox!.height).toBeLessThanOrEqual(panelBox!.y)

  await page.getByRole('button', { name: 'Tail', exact: true }).click()
})
