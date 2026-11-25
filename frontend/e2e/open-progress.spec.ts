import { expect, test } from '@playwright/test'
import { SLOW_FILE_NAME, SLOW_FILE_NAMES, slowSource } from './slow-open'

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

test('several slow opens: three at a time with one progress request, the others queued, and all open', async ({
  page,
}) => {
  test.setTimeout(120_000)
  const sourceName = `e2e-slow-opens-${Date.now()}`
  const { sourceId, cleanUp } = await slowSource(page.request, sourceName)
  try {
    const progress: URL[] = []
    const opens: string[] = []
    const cancels: string[] = []
    page.on('request', (request) => {
      const url = new URL(request.url())
      if (url.pathname.endsWith('/open-progress')) progress.push(url)
      if (url.pathname.endsWith('/open')) opens.push(url.searchParams.get('path') ?? '')
      if (url.pathname.endsWith('/open-cancel')) cancels.push(url.searchParams.get('request_id') ?? '')
    })
    await page.goto(`/#/viewer/${sourceId}`)
    const tree = page.locator('.tree-body')
    await tree.getByRole('button', { name: 'hello.log' }).waitFor({ timeout: 20_000 })
    for (const name of SLOW_FILE_NAMES) await tree.getByRole('button', { name }).click()

    // Five bars, in the order the files were opened, each with its source.
    const bars = page.getByTestId('open-progress')
    await expect(bars).toHaveCount(5, { timeout: 5_000 })
    await expect(bars.locator('.name')).toHaveText(SLOW_FILE_NAMES.map((name) => `Opening ${name}`))
    await expect(bars.locator('.source')).toHaveText(SLOW_FILE_NAMES.map(() => sourceName))
    const bar = (name: string) => page.getByRole('progressbar', { name: `Opening ${name} on ${sourceName}` })

    // Three run and move on; the last two wait in the viewer, never sent.
    for (const name of SLOW_FILE_NAMES.slice(3)) await expect(bar(name)).toHaveAttribute('aria-valuetext', 'Queued')
    for (const name of SLOW_FILE_NAMES.slice(0, 3)) {
      await expect(bar(name)).toHaveAttribute('aria-valuetext', /^[\d.]+ (B|KB|MB) of 9(\.\d)? MB$/, {
        timeout: 8_000,
      })
    }
    const first = Number(await bar(SLOW_FILE_NAME).getAttribute('aria-valuenow'))
    await expect
      .poll(async () => Number(await bar(SLOW_FILE_NAME).getAttribute('aria-valuenow')), { timeout: 8_000 })
      .toBeGreaterThan(first)
    expect(opens.sort()).toEqual(SLOW_FILE_NAMES.slice(0, 3).sort())
    // One request a second asks about the three running opens at once, never
    // one per source.
    expect(progress.some((url) => url.searchParams.getAll('request_id').length === 3)).toBe(true)
    expect(progress.every((url) => url.pathname === '/open-progress')).toBe(true)
    // The bars stay about three high, and scroll.
    const list = page.locator('.open-progress-list')
    expect(await list.evaluate((el) => el.scrollHeight > el.clientHeight)).toBe(true)

    // Cancel of a queued open: it leaves, and the server never hears of it.
    const last = SLOW_FILE_NAMES[4]
    await bars.filter({ hasText: `Opening ${last}` }).getByRole('button', { name: 'Cancel', exact: true }).click()
    await expect(bars).toHaveCount(4)
    expect(cancels).toEqual([])

    // The other four all open, the fourth once one of the first three is done.
    for (const name of SLOW_FILE_NAMES.slice(0, 4)) {
      await expect(page.locator('.tab-label', { hasText: name })).toHaveCount(1, { timeout: 60_000 })
    }
    await expect(bars).toHaveCount(0)
    await expect(page.locator('.tab-label', { hasText: last })).toHaveCount(0)
    expect(opens).not.toContain(last)
    await expect(page.getByRole('alert')).toHaveCount(0)
  } finally {
    await page.goto('/#/viewer')
    await cleanUp()
  }
})
