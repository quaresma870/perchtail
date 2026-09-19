import { expect, test } from '@playwright/test'
import * as fs from 'fs'
import * as path from 'path'
import { fileURLToPath } from 'node:url'

// Compare, Beautify/Minify, Follow, and Highlight (ROADMAP.md's "Viewer:
// toward an advanced editor" toolbar items) are exercised here against the
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

test.beforeAll(() => {
  fs.writeFileSync(pathA, `${RAW_A}\n`)
  fs.writeFileSync(pathB, '{"service":"gateway","message":"connection refused"}\n')
})

test.afterAll(() => {
  fs.rmSync(pathA, { force: true })
  fs.rmSync(pathB, { force: true })
})

// FolderTree renders both a source's own tree entries and (once opened)
// each tab's label as same-named buttons -- scope to `.tree-body` so a
// click always means "pick from the tree," not whichever same-named
// element Playwright's accessible-name matching happens to find first.
function treeFile(page: import('@playwright/test').Page, name: string) {
  return page.locator('.tree-body').getByRole('button', { name, exact: true })
}

test.beforeEach(async ({ page }) => {
  await page.goto('/#/viewer')
  // "All connections" always lists every source regardless of visit
  // history; "Recent" only gains an entry once a source has actually been
  // opened -- scoping here avoids a strict-mode collision once this
  // spec's own earlier tests have made this source show up in *both*
  // columns.
  await page.locator('.picker-column.all').getByRole('button', { name: SYSTEM_SOURCE_NAME }).click()
  await expect(page).toHaveURL(/#\/viewer\/\d+$/)
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
  await page.getByRole('button', { name: 'Follow' }).click()

  fs.appendFileSync(pathA, 'appended line\n')
  await expect(page.locator('.cm-content')).toContainText('appended line', { timeout: 6000 })

  await page.getByRole('button', { name: 'Follow' }).click() // disable
  await page.waitForTimeout(2500) // longer than one 2s poll interval
  fs.appendFileSync(pathA, 'should not appear once follow is off\n')
  await page.waitForTimeout(2500)
  await expect(page.locator('.cm-content')).not.toContainText('should not appear')
})
