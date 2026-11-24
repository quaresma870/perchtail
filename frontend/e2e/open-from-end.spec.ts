import { expect, test, type Page } from '@playwright/test'
import { LARGE_FILE_LINES, LARGE_FILE_NAME, largeFileLine, largeFileSource } from './large-file'

// A file over the `open_from_end_mb` System setting opens at its end, with
// Tail off and a bar saying how much of it is loaded: Load earlier adds the
// window before it at the top without moving what is on screen, and Load
// whole file opens it whole in the same tab.

async function openLargeFile(page: Page, sourceId: number) {
  await page.goto(`/#/viewer/${sourceId}`)
  await page.locator('.tree-body').getByRole('button', { name: LARGE_FILE_NAME }).click({ timeout: 20_000 })
  await expect(page.locator('.tab.active .tab-label')).toContainText(LARGE_FILE_NAME)
}

/** The first line CodeMirror shows on screen and where it is. */
async function firstVisibleLine(page: Page): Promise<{ text: string; top: number }> {
  return page.evaluate(() => {
    const scroller = document.querySelector('.cm-scroller') as HTMLElement
    const top = scroller.getBoundingClientRect().top
    for (const line of Array.from(document.querySelectorAll('.cm-content .cm-line'))) {
      const box = line.getBoundingClientRect()
      if (box.bottom > top + 1) return { text: line.textContent ?? '', top: box.top }
    }
    return { text: '', top: 0 }
  })
}

/** Where the line with this text is on screen, or null when it isn't drawn. */
async function lineTop(page: Page, text: string): Promise<number | null> {
  return page.evaluate((wanted) => {
    for (const line of Array.from(document.querySelectorAll('.cm-content .cm-line'))) {
      if (line.textContent === wanted) return line.getBoundingClientRect().top
    }
    return null
  }, text)
}

/** Scrolls the editor to its top or bottom and waits for it to draw. */
async function scrollEditor(page: Page, to: 'top' | 'bottom') {
  await page.evaluate(async (where) => {
    const scroller = document.querySelector('.cm-scroller') as HTMLElement
    scroller.scrollTop = where === 'top' ? 0 : scroller.scrollHeight
    await new Promise((resolve) => setTimeout(resolve, 300))
  }, to)
}

test('a large file opens at its end, loads earlier lines in place and opens whole', async ({ page }) => {
  const { sourceId, cleanUp } = await largeFileSource(page.request, `e2e-from-end-${Date.now()}`)
  try {
    const opens: string[] = []
    page.on('request', (request) => {
      if (request.url().includes('/open?')) opens.push(request.url())
    })
    await openLargeFile(page, sourceId)

    // The end, with no copy of the whole file and Tail off.
    const bar = page.getByTestId('partial-bar')
    await expect(bar).toHaveAttribute('role', 'status')
    await expect(bar).toContainText(/Showing the last 1(\.0)? MB of 3(\.\d)? MB/)
    expect(opens).toEqual([])
    await expect(page.getByRole('button', { name: 'Tail', exact: true })).toHaveAttribute('aria-pressed', 'false')
    const content = page.locator('.cm-content')
    await scrollEditor(page, 'bottom')
    await expect(content).toContainText(largeFileLine(LARGE_FILE_LINES))
    // Line numbers and Go to line wait for the whole file.
    await expect(page.locator('.cm-lineNumbers')).toBeHidden()
    const goToLine = page.getByRole('button', { name: 'Go to line', exact: true })
    await expect(goToLine).toBeDisabled()
    await expect(goToLine).toHaveAttribute('title', 'Line numbers need the whole file')
    const copyLines = page.getByRole('button', { name: 'Copy lines', exact: true })
    await expect(copyLines).toBeDisabled()
    await expect(copyLines).toHaveAttribute('title', 'Line numbers need the whole file')

    // Load earlier is off while Tail is on.
    const tail = page.getByRole('button', { name: 'Tail', exact: true })
    const earlier = page.getByRole('button', { name: 'Load earlier', exact: true })
    await tail.click()
    await expect(earlier).toBeDisabled()
    await expect(earlier).toHaveAttribute('title', 'Stop Tail to load earlier lines')
    await tail.click()
    await expect(earlier).toBeEnabled()

    // Load earlier: the window before is added at the top, and the line at
    // the top of the screen stays where it was.
    await scrollEditor(page, 'top')
    const shownBefore = await firstVisibleLine(page)
    expect(shownBefore.text).toMatch(/^line \d{6} /)
    const firstNumber = Number(shownBefore.text.slice(5, 11))
    expect(firstNumber).toBeGreaterThan(1)
    await earlier.focus()
    await page.keyboard.press('Enter')
    await expect(bar).toContainText(/Showing the last 2(\.0)? MB of 3(\.\d)? MB/)
    await expect
      .poll(async () => {
        const top = await lineTop(page, shownBefore.text)
        return top === null ? Infinity : Math.abs(top - shownBefore.top)
      })
      .toBeLessThan(2)
    // Earlier lines are there above it, whole, in order.
    await scrollEditor(page, 'top')
    const top = await firstVisibleLine(page)
    const topNumber = Number(top.text.slice(5, 11))
    expect(top.text).toBe(largeFileLine(topNumber))
    expect(topNumber).toBeLessThan(firstNumber)
    expect(opens).toEqual([])

    // Load whole file: the file opened whole in the same tab; the bar,
    // line numbers and Go to line come back.
    await page.getByRole('button', { name: 'Load whole file', exact: true }).click()
    await expect(bar).toHaveCount(0)
    expect(opens.length).toBe(1)
    await expect(page.locator('.tab')).toHaveCount(1)
    await scrollEditor(page, 'top')
    await expect(content).toContainText(largeFileLine(1))
    await expect(page.locator('.cm-lineNumbers')).toBeVisible()
    await expect(goToLine).toBeEnabled()
    await expect(copyLines).toBeEnabled()
  } finally {
    await page.goto('/#/viewer')
    await cleanUp()
  }
})

test('with the setting at 0 the same file opens whole', async ({ page }) => {
  const { sourceId, cleanUp } = await largeFileSource(page.request, `e2e-whole-${Date.now()}`)
  try {
    const off = await page.request.patch('/system-settings', { data: { open_from_end_mb: 0 } })
    expect(off.ok()).toBeTruthy()
    const opened = page.waitForResponse((response) => response.url().includes('/open?'))
    await openLargeFile(page, sourceId)
    await opened
    await expect(page.locator('.cm-content')).toContainText(largeFileLine(1))
    await expect(page.getByTestId('partial-bar')).toHaveCount(0)
  } finally {
    await page.goto('/#/viewer')
    await cleanUp()
  }
})
