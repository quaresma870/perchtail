import { expect, test, type Page } from '@playwright/test'
import * as fs from 'fs'
import * as path from 'path'
import { fileURLToPath } from 'node:url'

// Right-click menus on log lines and tabs (#287), on small files written
// into the system log source's folder (as viewer-editor-features.spec.ts).
const __dirname = path.dirname(fileURLToPath(import.meta.url))
const LOG_DIR = path.join(__dirname, '..', '..', 'backend', 'data', 'e2e', 'logs')
const SYSTEM_SOURCE_NAME = 'PerchTail application logs'
const stamp = Date.now()
const files = ['a', 'b', 'c', 'd'].map((n) => `context-menu-${n}-${stamp}.log`)
const lineText = (n: number) => `menu line ${n} of the fixture`

test.beforeAll(() => {
  for (const name of files) {
    fs.writeFileSync(path.join(LOG_DIR, name), `${[1, 2, 3, 4, 5].map(lineText).join('\n')}\n`)
  }
})

test.afterAll(() => {
  for (const name of files) fs.rmSync(path.join(LOG_DIR, name), { force: true })
})

function treeFile(page: Page, name: string) {
  const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  return page.locator('.tree-body').getByRole('button', { name: new RegExp(`^${escaped}, `) })
}

async function openFile(page: Page, name: string) {
  await treeFile(page, name).click()
  await expect(page.locator('.tab.active .tab-label')).toContainText(name)
  await expect(page.locator('.cm-content')).toContainText(lineText(5))
}

const editorLine = (page: Page, n: number) => page.locator('.cm-line', { hasText: lineText(n) })
const menu = (page: Page) => page.getByRole('menu')
const item = (page: Page, name: string) => page.getByRole('menuitem', { name, exact: true })

// Selects line `n` whole (Go to line selects the line it goes to).
async function selectLine(page: Page, n: number) {
  await page.getByRole('button', { name: 'Go to line' }).click()
  await page.getByPlaceholder('Line #').fill(String(n))
  await page.getByPlaceholder('Line #').press('Enter')
  await expect(page.locator('.cm-activeLine')).toHaveText(lineText(n))
}

test.beforeEach(async ({ page, context }) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write'])
  await page.goto('/#/viewer')
  await page.locator('.explorer').getByRole('button', { name: SYSTEM_SOURCE_NAME }).click()
})

test('a right-click on a line opens its menu; Copy with line numbers copies the selection', async ({ page }) => {
  await openFile(page, files[0])
  await selectLine(page, 2)
  await editorLine(page, 2).click({ button: 'right' })
  await expect(menu(page)).toBeVisible()
  await expect(item(page, 'Copy')).toBeFocused()
  for (const name of ['Copy with line numbers', 'Find all of selection', 'Highlight selection', 'Toggle bookmark', 'Go to line…', 'Copy link to line']) {
    await expect(item(page, name)).toBeVisible()
  }
  await item(page, 'Copy with line numbers').click()
  await expect(menu(page)).toHaveCount(0)
  await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toBe(`2: ${lineText(2)}`)
})

test('Highlight selection marks the selected text', async ({ page }) => {
  await openFile(page, files[0])
  await selectLine(page, 3)
  await editorLine(page, 3).click({ button: 'right' })
  await item(page, 'Highlight selection').click()
  await expect(page.locator('.mark-chip')).toHaveCount(1)
  await expect(page.locator('.mark-chip')).toContainText(lineText(3))
  await expect(page.locator('.cm-mark-0')).toHaveCount(1)
})

test("Find all of selection lists the selection's lines, and Find all's own menu uses the same component", async ({ page }) => {
  await openFile(page, files[0])
  await selectLine(page, 5)
  await editorLine(page, 5).click({ button: 'right' })
  await item(page, 'Find all of selection').click()
  const panel = page.locator('.find-all-panel')
  await expect(panel.locator('.title')).toContainText(lineText(5))
  await expect(panel.locator('.count').last()).toHaveText('1 line')

  await panel.locator('.result-row').first().click({ button: 'right' })
  await expect(menu(page)).toHaveAccessibleName('Find all results')
  for (const name of ['Copy selected lines', 'Copy with line numbers', 'Select all', 'Search in these results…', 'Exclude lines matching…', 'Go to line', 'Close']) {
    await expect(item(page, name)).toBeVisible()
  }
  await item(page, 'Copy with line numbers').click()
  await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toBe(`5: ${lineText(5)}`)
  await page.keyboard.press('Shift+F10')
  await item(page, 'Close').click()
  await expect(panel).toHaveCount(0)
})

test('Toggle bookmark marks the line right-clicked, and Copy link to line gives its address', async ({ page }) => {
  await openFile(page, files[0])
  await editorLine(page, 4).click({ button: 'right' })
  // No selection: what needs one isn't offered.
  await expect(item(page, 'Highlight selection')).toHaveCount(0)
  await item(page, 'Toggle bookmark').click()
  await expect(editorLine(page, 4)).toHaveClass(/cm-line-bookmark/)
  await expect(page.locator('.cm-line-bookmark')).toHaveCount(1)

  await editorLine(page, 4).click({ button: 'right' })
  await item(page, 'Copy link to line').click()
  const link = await page.evaluate(() => navigator.clipboard.readText())
  expect(link).toMatch(/#\/viewer\/\d+\?path=.*&line=4$/)
  expect(decodeURIComponent(link)).toContain(files[0])
})

test('Shift+F10 opens the line menu from the keyboard; arrows move, Escape closes and gives the focus back', async ({ page }) => {
  await openFile(page, files[0])
  await editorLine(page, 2).click()
  const scroller = page.locator('.cm-scroller')
  await expect(scroller).toBeFocused()
  await page.keyboard.press('Shift+F10')
  await expect(menu(page)).toBeVisible()
  await expect(item(page, 'Copy')).toBeFocused()
  await page.keyboard.press('ArrowDown')
  await expect(item(page, 'Copy with line numbers')).toBeFocused()
  await page.keyboard.press('End')
  await expect(item(page, 'Copy link to line')).toBeFocused()
  await page.keyboard.press('ArrowDown')
  await expect(item(page, 'Copy')).toBeFocused()
  await page.keyboard.press('ArrowUp')
  await expect(item(page, 'Copy link to line')).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(menu(page)).toHaveCount(0)
  await expect(scroller).toBeFocused()

  // Enter chooses.
  await page.keyboard.press('Shift+F10')
  await expect(item(page, 'Copy')).toBeFocused()
  await page.keyboard.press('ArrowDown')
  await page.keyboard.press('ArrowDown')
  await expect(item(page, 'Toggle bookmark')).toBeFocused()
  await page.keyboard.press('Enter')
  await expect(menu(page)).toHaveCount(0)
  await expect(editorLine(page, 2)).toHaveClass(/cm-line-bookmark/)
})

test('a click outside closes the menu, and Shift+right-click leaves the browser its own', async ({ page }) => {
  await openFile(page, files[0])
  await editorLine(page, 1).click({ button: 'right' })
  await expect(menu(page)).toBeVisible()
  // Below and right of the menu, which covers the lines under the click.
  const pane = (await page.locator('.editor-pane').boundingBox())!
  await page.mouse.click(pane.x + pane.width - 10, pane.y + pane.height - 10)
  await expect(menu(page)).toHaveCount(0)

  await editorLine(page, 1).click({ button: 'right', modifiers: ['Shift'] })
  await expect(menu(page)).toHaveCount(0)
  await page.locator('.tab.active').click({ button: 'right', modifiers: ['Shift'] })
  await expect(menu(page)).toHaveCount(0)
})

test('the tab menu closes others and to the right; a middle-click closes a tab', async ({ page }) => {
  for (const name of files) await openFile(page, name)
  const tab = (name: string) => page.locator('.tab', { hasText: name })
  await expect(page.locator('.tab')).toHaveCount(4)

  // b: Close to the right closes c and d.
  await tab(files[1]).click({ button: 'right' })
  await expect(menu(page)).toBeVisible()
  await expect(item(page, 'Close to the left')).toBeVisible()
  await expect(item(page, 'Compare with active tab')).toBeVisible()
  await item(page, 'Close to the right').click()
  await expect(page.locator('.tab')).toHaveCount(2)
  await expect(tab(files[2])).toHaveCount(0)
  await expect(tab(files[3])).toHaveCount(0)

  // a: Close others leaves only a.
  await tab(files[0]).click({ button: 'right' })
  await item(page, 'Close others').click()
  await expect(page.locator('.tab')).toHaveCount(1)
  await expect(tab(files[0])).toHaveCount(1)
  await expect(page.locator('.tab.active .tab-label')).toContainText(files[0])

  // A single tab has nothing to close around it.
  await tab(files[0]).click({ button: 'right' })
  await expect(item(page, 'Close others')).toHaveCount(0)
  await expect(item(page, 'Close to the right')).toHaveCount(0)
  await expect(item(page, 'Compare with active tab')).toHaveCount(0)
  await page.keyboard.press('Escape')
  await expect(menu(page)).toHaveCount(0)

  await openFile(page, files[1])
  await expect(page.locator('.tab')).toHaveCount(2)
  await tab(files[0]).click({ button: 'middle' })
  await expect(page.locator('.tab')).toHaveCount(1)
  await expect(tab(files[0])).toHaveCount(0)
  await tab(files[1]).click({ button: 'middle' })
  await expect(page.locator('.tab')).toHaveCount(0)
})

test("Try again after a failed Reload from a tab's menu reloads that tab, not the active one", async ({ page }) => {
  await openFile(page, files[0])
  await openFile(page, files[1])
  const opened: string[] = []
  let failNext = true
  await page.route('**/open?**', async (route) => {
    const asked = new URL(route.request().url()).searchParams.get('path') ?? ''
    opened.push(asked)
    if (failNext) {
      failNext = false
      await route.fulfill({ status: 503, contentType: 'application/json', body: '{"detail":"Busy, try again."}' })
      return
    }
    await route.continue()
  })

  // b is active; a's menu reloads a, which fails once.
  await page.locator('.tab', { hasText: files[0] }).click({ button: 'right' })
  await item(page, 'Reload').click()
  const retry = page.getByRole('button', { name: 'Try again' })
  await expect(retry).toBeVisible()
  expect(opened.map((p) => p.endsWith(files[0]))).toEqual([true])

  await retry.click()
  await expect.poll(() => opened.length).toBe(2)
  expect(opened[1].endsWith(files[0])).toBe(true)
  await expect(page.locator('.tab.active .tab-label')).toContainText(files[1])
})

// The explorer's menus (#287): files, folders and sources.

test("a file's menu opens it with Tail on, and copies its path", async ({ page }) => {
  await treeFile(page, files[0]).click({ button: 'right' })
  await expect(page.getByRole('menu', { name: 'File actions' })).toBeVisible()
  await expect(item(page, 'Open')).toBeFocused()
  for (const name of ['Open with Tail', 'Open the end', 'Download', 'Copy path', 'Copy link']) {
    await expect(item(page, name)).toBeVisible()
  }
  // No tab shown yet: nothing to compare with.
  await expect(item(page, 'Compare with active tab')).toHaveCount(0)
  await item(page, 'Open with Tail').click()
  await expect(page.locator('.tab.active .tab-label')).toContainText(files[0])
  await expect(page.locator('.cm-content')).toContainText(lineText(5))
  await expect(page.getByRole('button', { name: 'Tail', exact: true })).toHaveAttribute('aria-pressed', 'true')

  await treeFile(page, files[1]).click({ button: 'right' })
  await expect(item(page, 'Compare with active tab')).toBeVisible()
  await item(page, 'Copy path').click()
  await expect(page.locator('.explorer-note')).toHaveText('Copied')
  const copied = await page.evaluate(() => navigator.clipboard.readText())
  expect(copied.endsWith(files[1])).toBe(true)
})

test("a file's menu opens with Shift+F10 and gives the focus back on Escape", async ({ page }) => {
  const node = treeFile(page, files[2])
  await node.focus()
  await page.keyboard.press('Shift+F10')
  await expect(item(page, 'Open')).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(menu(page)).toHaveCount(0)
  await expect(node).toBeFocused()
})

// A local-disk source under the e2e backend's LOCAL_SOURCE_ROOTS
// (backend/scripts/run_e2e_server.sh), with a folder and a file next to it.
const fixtureRoot = '/tmp/perchtail-e2e-fixtures'

async function folderSource(page: Page) {
  fs.mkdirSync(fixtureRoot, { recursive: true })
  const dir = fs.mkdtempSync(path.join(fixtureRoot, 'menus-'))
  fs.mkdirSync(path.join(dir, 'sub'))
  const inside = `inside-${stamp}.log`
  const outside = `outside-${stamp}.log`
  fs.writeFileSync(path.join(dir, 'sub', inside), 'in the folder\n')
  fs.writeFileSync(path.join(dir, outside), 'next to it\n')
  const name = `e2e-menus-${stamp}-${Math.random().toString(36).slice(2, 8)}`
  const created = await page.request.post('/sources', {
    data: { name, protocol: 'local', host: 'localhost', base_path: dir },
  })
  expect(created.ok()).toBeTruthy()
  const id: number = (await created.json()).id
  const rule = await page.request.post(`/sources/${id}/rules`, { data: { type: 'include', pattern: '**/*' } })
  expect(rule.ok()).toBeTruthy()
  return {
    id,
    name,
    inside,
    outside,
    cleanUp: async () => {
      await page.request.delete(`/sources/${id}`)
      fs.rmSync(dir, { recursive: true, force: true })
    },
  }
}

const sourceRow = (page: Page, name: string) => page.locator('.source-row', { hasText: name })
const folderButton = (page: Page) => page.locator('.tree-body').getByRole('button', { name: 'sub', exact: true })

test("a folder's Search here limits the explorer's search to that folder", async ({ page }) => {
  const source = await folderSource(page)
  try {
    await page.goto(`/#/viewer/${source.id}`)
    await expect(folderButton(page)).toBeVisible({ timeout: 20_000 })
    await folderButton(page).click({ button: 'right' })
    await expect(page.getByRole('menu', { name: 'Folder actions' })).toBeVisible()
    for (const name of ['Search here', 'Download as ZIP', 'Copy path']) await expect(item(page, name)).toBeVisible()
    await item(page, 'Search here').click()
    await expect(page.locator('.scope-chip')).toHaveCount(1)
    await expect(page.locator('.scope-chip')).toContainText(`${source.name} › sub`)
    const box = page.getByRole('searchbox', { name: 'Search sources and files' })
    await expect(box).toBeFocused()
    await box.fill('.log')
    const results = page.locator('.scoped-results')
    await expect(results.getByText(source.inside)).toBeVisible({ timeout: 20_000 })
    await expect(results.getByText(source.outside)).toHaveCount(0)
  } finally {
    await source.cleanUp()
  }
})

test("a source's menu for an admin: Edit source opens the source's page", async ({ page }) => {
  const source = await folderSource(page)
  try {
    await page.goto(`/#/viewer/${source.id}`)
    await expect(folderButton(page)).toBeVisible({ timeout: 20_000 })
    await sourceRow(page, source.name).click({ button: 'right' })
    await expect(page.getByRole('menu', { name: 'Source actions' })).toBeVisible()
    for (const name of ['Refresh', 'Search here', 'Collapse all', 'Check now', 'Copy host', 'Edit source', 'Edit rules']) {
      await expect(item(page, name)).toBeVisible()
    }
    await item(page, 'Edit source').click()
    await expect(page).toHaveURL(new RegExp(`#/settings/sources/${source.id}$`))
  } finally {
    await source.cleanUp()
  }
})

test('with view only, the menus leave out Download, Check now, Edit source and Edit rules', async ({ page }) => {
  const source = await folderSource(page)
  try {
    // The role holds only `view` on every source.
    await page.route(/\/sources(\?.*)?$/, async (route) => {
      if (route.request().method() !== 'GET') return route.continue()
      const response = await route.fetch()
      const body = (await response.json()) as Record<string, unknown>[]
      await route.fulfill({ response, json: body.map((s) => ({ ...s, my_capabilities: ['view'] })) })
    })
    await page.goto(`/#/viewer/${source.id}`)
    await expect(folderButton(page)).toBeVisible({ timeout: 20_000 })

    await sourceRow(page, source.name).click({ button: 'right' })
    await expect(item(page, 'Copy host')).toBeVisible()
    for (const name of ['Check now', 'Index file names now', 'Edit source', 'Edit rules']) {
      await expect(item(page, name)).toHaveCount(0)
    }
    await page.keyboard.press('Escape')

    await folderButton(page).click({ button: 'right' })
    await expect(item(page, 'Search here')).toBeVisible()
    await expect(item(page, 'Download as ZIP')).toHaveCount(0)
    await page.keyboard.press('Escape')

    await treeFile(page, source.outside).click({ button: 'right' })
    await expect(item(page, 'Open')).toBeVisible()
    await expect(item(page, 'Download')).toHaveCount(0)
  } finally {
    await source.cleanUp()
  }
})

test('the tab menu opens with Shift+F10 on a tab', async ({ page }) => {
  await openFile(page, files[0])
  const label = page.locator('.tab.active .tab-label')
  await label.focus()
  await page.keyboard.press('Shift+F10')
  await expect(item(page, 'Close')).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(menu(page)).toHaveCount(0)
  await expect(label).toBeFocused()
  await label.focus()
  await page.keyboard.press('Shift+F10')
  await item(page, 'Close all').click()
  await expect(page.locator('.tab')).toHaveCount(0)
})
