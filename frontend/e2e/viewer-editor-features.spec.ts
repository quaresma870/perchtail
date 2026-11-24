import { expect, test } from '@playwright/test'
import * as fs from 'fs'
import * as path from 'path'
import { fileURLToPath } from 'node:url'

// Compare, Beautify/Minify, Follow, and Highlight (docs/design-decisions.md,
// "Live browsing, scratch and the viewer") are exercised here against the
// built-in super-admin log-viewer system source (same one viewer.spec.ts
// already uses), with two small JSON fixture files written directly into
// LOG_DIR -- the `local` connector reads straight off disk with no caching
// (CLAUDE.md: every open is a fresh fetch), so a file dropped in while the
// e2e backend is already running shows up on the next browse/open exactly
// like it would in production. Timestamped names, removed in `afterAll`,
// so a re-run never collides with a leftover file from a previous one.
//
// This file is ESM ("type": "module" in package.json), which has no
// __dirname of its own -- same fileURLToPath(import.meta.url) derivation
// fixture-paths.ts already uses.
const __dirname = path.dirname(fileURLToPath(import.meta.url))
const LOG_DIR = path.join(__dirname, '..', '..', 'backend', 'data', 'e2e', 'logs')
const SYSTEM_SOURCE_NAME = 'PerchTail application logs'
const stamp = Date.now()
const fileA = `editor-features-a-${stamp}.json`
const fileB = `editor-features-b-${stamp}.json`
const pathA = path.join(LOG_DIR, fileA)
const pathB = path.join(LOG_DIR, fileB)
const RAW_A = '{"service":"gateway","message":"connection timeout"}'
// Long enough to scroll, for the tests of each tab keeping its place.
const fileLong = `editor-features-long-${stamp}.log`
const pathLong = path.join(LOG_DIR, fileLong)
const longLine = (n: number) => `entry ${String(n).padStart(4, '0')} of the long fixture`
// A line no severity pattern matches until the test of patterns changed in
// Settings adds one for it.
const fileSeverity = `editor-features-severity-${stamp}.log`
const pathSeverity = path.join(LOG_DIR, fileSeverity)
const severityMarker = `sevrefresh${stamp}`

test.beforeAll(() => {
  fs.writeFileSync(pathA, `${RAW_A}\n`)
  fs.writeFileSync(pathB, '{"service":"gateway","message":"connection refused"}\n')
  fs.writeFileSync(pathLong, `${Array.from({ length: 3000 }, (_, i) => longLine(i + 1)).join('\n')}\n`)
  fs.writeFileSync(pathSeverity, `first plain line\nthe ${severityMarker} line\nlast plain line\n`)
})

test.afterAll(() => {
  fs.rmSync(pathA, { force: true })
  fs.rmSync(pathB, { force: true })
  fs.rmSync(pathLong, { force: true })
  fs.rmSync(pathSeverity, { force: true })
})

// FolderTree renders both a source's own tree entries and (once opened)
// each tab's label as same-named buttons -- scope to `.tree-body` so a
// click always means "pick from the tree," not whichever same-named
// element Playwright's accessible-name matching happens to find first.
// A file's accessible name is "<name>, <size>" (#377).
function treeFile(page: import('@playwright/test').Page, name: string) {
  const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  return page.locator('.tree-body').getByRole('button', { name: new RegExp(`^${escaped}, `) })
}

test.beforeEach(async ({ page }) => {
  await page.goto('/#/viewer')
  // The explorer (#162) expands the source in place instead of navigating.
  await page.locator('.explorer').getByRole('button', { name: SYSTEM_SOURCE_NAME }).click()
})

test('beautify and minify reformat the display without touching the raw content', async ({ page }) => {
  await treeFile(page, fileA).click()
  await expect(page.locator('.cm-content')).toContainText('"service"')

  await page.getByRole('button', { name: 'Beautify' }).click()
  await expect(page.locator('.cm-line').first()).toHaveText('{')
  await expect(page.locator('.cm-content')).toContainText('"service": "gateway"')

  await page.getByRole('button', { name: 'Beautify' }).click() // toggle back off
  await expect(page.locator('.cm-line').first()).toHaveText(RAW_A)

  await page.getByRole('button', { name: 'Minify' }).click()
  await expect(page.locator('.cm-line').first()).toHaveText(RAW_A)
  await page.getByRole('button', { name: 'Minify' }).click()
})

test('highlight adds exactly one mark per pattern, and Clear highlights removes it', async ({ page }) => {
  await treeFile(page, fileA).click()

  await page.getByRole('button', { name: 'Highlight' }).click()
  await page.locator('.mark-input input').fill('timeout')
  await page.locator('.mark-input input').press('Enter')

  await expect(page.locator('.mark-chip')).toHaveCount(1)
  await expect(page.locator('.cm-mark-0')).toHaveCount(1)

  await page.getByRole('button', { name: 'Clear highlights' }).click()
  await expect(page.locator('.mark-chip')).toHaveCount(0)
  await expect(page.locator('.cm-mark-0')).toHaveCount(0)
})

test('compare renders a diff against a second file and exits cleanly', async ({ page }) => {
  await treeFile(page, fileA).click()
  await page.getByRole('button', { name: 'Compare' }).click()
  await treeFile(page, fileB).click()

  await expect(page.locator('.cm-mergeView')).toBeVisible()
  await expect(page.locator('.diff-labels')).toContainText(fileA)
  await expect(page.locator('.diff-labels')).toContainText(fileB)

  await page.getByRole('button', { name: 'Exit compare' }).click()
  await expect(page.locator('.cm-mergeView')).toHaveCount(0)
})

test('follow picks up appended content while enabled, and stops once disabled', async ({ page }) => {
  await treeFile(page, fileA).click()
  await page.getByRole('button', { name: 'Tail', exact: true }).click()

  fs.appendFileSync(pathA, 'appended line\n')
  await expect(page.locator('.cm-content')).toContainText('appended line', { timeout: 6000 })

  await page.getByRole('button', { name: 'Tail', exact: true }).click() // disable
  await page.waitForTimeout(2500) // longer than one 2s poll interval
  fs.appendFileSync(pathA, 'should not appear once follow is off\n')
  await page.waitForTimeout(2500)
  await expect(page.locator('.cm-content')).not.toContainText('should not appear')
})

test('one find bar: Find button or Ctrl/Cmd+F, next/previous with a count, Escape closes', async ({ page }) => {
  // #299: one bar for Find and Find all.
  await treeFile(page, fileA).click()
  await expect(page.locator('.cm-content')).toContainText('"service"')
  const find = page.getByRole('textbox', { name: 'Find', exact: true })
  await expect(find).toHaveCount(0)

  await page.getByRole('button', { name: 'Find', exact: true }).click()
  await expect(find).toBeFocused()
  await find.fill('service')
  const status = page.locator('.find-bar .status')
  await expect(status).toHaveText(/^\d+ match(es)?$/)
  await expect(page.locator('.cm-searchMatch').first()).toBeVisible()
  await find.press('Enter')
  await expect(status).toHaveText(/^1 of \d+$/)
  await find.press('Shift+Enter')
  await expect(status).toHaveText(/^\d+ of \d+$/)

  await find.press('Escape')
  await expect(find).toHaveCount(0)
  await expect(page.locator('.cm-searchMatch')).toHaveCount(0)
  await page.locator('.cm-content').click()
  await page.keyboard.press('ControlOrMeta+f')
  await expect(find).toBeFocused()
  await expect(find).toHaveValue('service')
})

// Selects line 1500 of the long file (Go to line), scrolls a little past
// it, and returns where the editor is scrolled to.
async function selectLineAndScroll(page: import('@playwright/test').Page): Promise<number> {
  await page.getByRole('button', { name: 'Go to line' }).click()
  await page.getByPlaceholder('Line #').fill('1500')
  await page.getByPlaceholder('Line #').press('Enter')
  await expect(page.locator('.cm-activeLine')).toHaveText(longLine(1500))
  const scroller = page.locator('.cm-scroller')
  const centered = await scroller.evaluate((el) => el.scrollTop)
  await scroller.hover()
  await page.mouse.wheel(0, 120)
  await expect.poll(() => scroller.evaluate((el) => el.scrollTop)).toBeGreaterThan(centered + 60)
  // The wheel may still be scrolling smoothly: the place is where it stops.
  let top = -1
  await expect
    .poll(async () => {
      const previous = top
      await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))))
      top = await scroller.evaluate((el) => el.scrollTop)
      return top === previous
    })
    .toBe(true)
  expect(top).toBeGreaterThan(1000)
  return top
}

// The long file's tab is back with the same selection (what Find starts
// with) and scrolled to the same place.
async function expectSamePlace(page: import('@playwright/test').Page, top: number) {
  await expect(page.locator('.tab.active .tab-label')).toContainText(fileLong)
  await expect(page.locator('.cm-content')).toContainText('of the long fixture')
  const scroller = page.locator('.cm-scroller')
  // Within a pixel or so: the browser may round a fractional scroll offset.
  await expect.poll(() => scroller.evaluate((el) => el.scrollTop)).toBeGreaterThan(top - 2)
  await expect.poll(() => scroller.evaluate((el) => el.scrollTop)).toBeLessThan(top + 2)
  await page.getByRole('button', { name: 'Find', exact: true }).click()
  await expect(page.getByRole('textbox', { name: 'Find', exact: true })).toHaveValue(longLine(1500))
}

test('each tab keeps its selection and place when switching tabs', async ({ page }) => {
  await treeFile(page, fileLong).click()
  await expect(page.locator('.cm-content')).toContainText(longLine(1))
  await treeFile(page, fileA).click()
  await expect(page.locator('.cm-content')).toContainText('"service"')
  await page.locator('.tab-label', { hasText: fileLong }).click()
  await expect(page.locator('.cm-content')).toContainText(longLine(1))
  const top = await selectLineAndScroll(page)

  await page.locator('.tab-label', { hasText: fileA }).click()
  await expect(page.locator('.cm-content')).toContainText('"service"')
  await page.locator('.tab-label', { hasText: fileLong }).click()

  await expectSamePlace(page, top)
})

test('a tab keeps its selection and place after a visit to another page', async ({ page }) => {
  await treeFile(page, fileLong).click()
  await expect(page.locator('.cm-content')).toContainText(longLine(1))
  const top = await selectLineAndScroll(page)

  await page.goto('/#/settings/sources')
  await expect(page.locator('.cm-content')).toHaveCount(0)
  await page.goto('/#/viewer')

  await expectSamePlace(page, top)
})

test('a file closed and opened again starts from the top, without the old selection', async ({ page }) => {
  await treeFile(page, fileLong).click()
  await expect(page.locator('.cm-content')).toContainText(longLine(1))
  await selectLineAndScroll(page)

  await page.getByRole('button', { name: `Close ${fileLong}` }).click()
  await expect(page.locator('.tab-label', { hasText: fileLong })).toHaveCount(0)
  await treeFile(page, fileLong).click()

  await expect(page.locator('.tab.active .tab-label')).toContainText(fileLong)
  await expect(page.locator('.cm-content')).toContainText(longLine(1))
  expect(await page.locator('.cm-scroller').evaluate((el) => el.scrollTop)).toBe(0)
  await page.getByRole('button', { name: 'Find', exact: true }).click()
  await expect(page.getByRole('textbox', { name: 'Find', exact: true })).toHaveValue('')
})

test('a severity pattern added in Settings tints the open tab once back in the Viewer', async ({ page }) => {
  await treeFile(page, fileSeverity).click()
  const markedLine = page.locator('.cm-line', { hasText: severityMarker })
  await expect(markedLine).toBeVisible()
  await expect(markedLine).not.toHaveClass(/cm-line-error/)

  await page.goto('/#/settings/severity-indicators')
  const addRow = page.locator('.add-row')
  await addRow.locator('select.level-select').selectOption('error')
  await addRow.getByPlaceholder('panic or re:\\berror\\b').fill(severityMarker)
  await addRow.locator('label.flag', { hasText: 'line' }).locator('input[type=checkbox]').check()
  await addRow.getByRole('button', { name: 'Add pattern' }).click()
  const values = () =>
    page
      .locator('.pattern-row input.pattern-input')
      .evaluateAll((els) => els.map((el) => (el as HTMLInputElement).value))
  await expect.poll(async () => (await values()).includes(severityMarker)).toBe(true)

  try {
    await page.goto('/#/viewer')
    await expect(page.locator('.tab.active .tab-label')).toContainText(fileSeverity)
    await expect(markedLine).toHaveClass(/cm-line-error/)
    await expect(page.locator('.cm-line.cm-line-error')).toHaveCount(1)
  } finally {
    await page.goto('/#/settings/severity-indicators')
    await expect.poll(async () => (await values()).includes(severityMarker)).toBe(true)
    const row = page.locator('.pattern-row').nth((await values()).indexOf(severityMarker))
    await row.getByRole('button', { name: 'delete' }).click()
    await expect.poll(async () => (await values()).includes(severityMarker)).toBe(false)
  }
})

test('find all can be resized by its handle', async ({ page }) => {
  // #285: the handle on top of the panel resizes it (here by keyboard).
  await treeFile(page, fileA).click()
  await page.getByRole('button', { name: 'Find', exact: true }).click()
  await page.getByRole('textbox', { name: 'Find', exact: true }).fill('service')
  // Alt+Enter is Find all, same as its button.
  await page.getByRole('textbox', { name: 'Find', exact: true }).press('Alt+Enter')
  const panel = page.locator('.find-all-panel')
  await expect(panel).toBeVisible()
  const before = (await panel.boundingBox())!.height
  const handle = page.getByRole('separator', { name: 'Resize find all' })
  await handle.focus()
  await handle.press('ArrowUp')
  await handle.press('ArrowUp')
  await expect.poll(async () => (await panel.boundingBox())!.height).toBeGreaterThan(before + 30)
  await expect(panel.locator('.title')).toContainText('service')
  await page.getByRole('button', { name: 'Close find all' }).click()
  await expect(panel).toHaveCount(0)
})
