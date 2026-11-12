import AxeBuilder from '@axe-core/playwright'
import { expect, test, type Page } from '@playwright/test'
import { confirmPopup } from './popups'

// Runs axe-core on the main pages (#377) and fails on serious or critical
// WCAG 2.0/2.1 A and AA violations. A new page should get a test here too.
// Only the system source is used, never the SSH/SMB test servers, so this
// runs everywhere the e2e suite does.

const SYSTEM_SOURCE_NAME = 'PerchTail application logs'
const WCAG_TAGS = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']
const FAILING_IMPACTS = new Set(['serious', 'critical'])

async function expectNoSeriousViolations(page: Page, configure?: (axe: AxeBuilder) => AxeBuilder) {
  let axe = new AxeBuilder({ page }).withTags(WCAG_TAGS)
  if (configure) axe = configure(axe)
  const results = await axe.analyze()
  // One line per violation: rule id, impact and the selectors of its nodes.
  const failing = results.violations
    .filter((v) => v.impact && FAILING_IMPACTS.has(v.impact))
    .map((v) => `${v.id} (${v.impact}): ${v.nodes.map((node) => node.target.join(' ')).join(', ')}`)
  expect(failing, 'serious or critical accessibility violations').toEqual([])
}

/** Waits for a page's heading and for its "Loading…" hint to go. */
async function settled(page: Page, heading: string) {
  await expect(page.getByRole('heading', { level: 1, name: heading, exact: true })).toBeVisible()
  await expect(page.getByText('Loading…', { exact: true })).toHaveCount(0)
}

test.describe('logged out', () => {
  test.use({ storageState: { cookies: [], origins: [] } })

  test('accessibility: login', async ({ page }) => {
    await page.goto('/#/login')
    await expect(page.getByRole('button', { name: 'Sign in' })).toBeVisible()
    await expectNoSeriousViolations(page)
  })
})

test('accessibility: viewer with a file open', async ({ page }) => {
  await page.goto('/#/viewer')
  await page.locator('.explorer').getByRole('button', { name: SYSTEM_SOURCE_NAME }).click()
  await page.locator('.tree-body').getByRole('button', { name: 'perchtail.log' }).click()
  await expect(page.locator('.tab.active .tab-label')).toContainText('perchtail.log')
  await expect(page.locator('.cm-content')).not.toBeEmpty()
  // CodeMirror's scroller (tabindex="-1") fails scrollable-region-focusable:
  // the read-only editor's content isn't focusable, and making it so would
  // change keyboard behaviour. Only the scroller node itself is excluded:
  // its children (gutters, content) are included again and still checked.
  await expectNoSeriousViolations(page, (axe) =>
    axe.include('html').exclude('.cm-scroller').include('.cm-scroller > *'),
  )
})

test('accessibility: Settings -> Sources', async ({ page }) => {
  await page.goto('/#/settings/sources')
  await settled(page, 'Sources')
  await expect(page.getByRole('row', { name: new RegExp(SYSTEM_SOURCE_NAME) })).toBeVisible()
  await expectNoSeriousViolations(page)
})

test('accessibility: new source popup', async ({ page }) => {
  await page.goto('/#/settings/sources')
  await settled(page, 'Sources')
  await page.getByRole('button', { name: '+ Add source' }).click()
  const dialog = page.getByRole('dialog', { name: 'New source' })
  await expect(dialog).toBeVisible()
  await expect(dialog.getByLabel('Customer')).toBeVisible()
  await expectNoSeriousViolations(page)
})

test('accessibility: Settings -> Folders', async ({ page }) => {
  await page.goto('/#/settings/folders')
  await settled(page, 'Folders')
  await expectNoSeriousViolations(page)
})

test('accessibility: Settings -> Roles', async ({ page }) => {
  await page.goto('/#/settings/roles')
  await settled(page, 'Roles')
  await expect(page.locator('.role-name', { hasText: /^No Access$/ })).toBeVisible()
  await expectNoSeriousViolations(page)
})

test('accessibility: role editor', async ({ page }) => {
  // A role of its own, so the "Add grant" form (hidden on built-in roles)
  // is on the page too.
  const roleName = `E2E A11y Role ${Date.now()}`
  await page.goto('/#/settings/roles')
  await settled(page, 'Roles')
  await page.getByRole('button', { name: '+ New role' }).click()
  await page.getByRole('dialog', { name: 'New role' }).getByLabel('Name', { exact: true }).fill(roleName)
  await page.getByRole('button', { name: 'Save' }).click()
  await expect(page).toHaveURL(/#\/settings\/roles\/\d+$/)

  const editor = page.getByRole('dialog')
  await expect(editor).toBeVisible()
  await expect(editor.getByText('Loading…', { exact: true })).toHaveCount(0)
  await expect(editor.locator('.add-grant')).toBeVisible()
  await expect(editor.getByRole('button', { name: 'Add grant' })).toBeVisible()
  await expectNoSeriousViolations(page)

  // Cleanup.
  await page.goto('/#/settings/roles')
  await settled(page, 'Roles')
  await page.getByRole('row', { name: new RegExp(roleName) }).getByRole('button', { name: 'Delete' }).click()
  await confirmPopup(page, 'Delete')
  await expect(page.getByRole('row', { name: new RegExp(roleName) })).toHaveCount(0)
})

test('accessibility: Settings -> Users', async ({ page }) => {
  await page.goto('/#/settings/users')
  await settled(page, 'Users')
  await expect(page.getByLabel('Filter users')).toBeVisible()
  await expectNoSeriousViolations(page)
})

test('accessibility: Settings -> System', async ({ page }) => {
  await page.goto('/#/settings/system')
  await settled(page, 'System settings')
  await expect(page.getByLabel('Alert check interval in seconds')).toBeVisible()
  await expectNoSeriousViolations(page)
})

test('accessibility: Settings -> Audit log', async ({ page }) => {
  await page.goto('/#/settings/audit-log')
  await settled(page, 'Audit log')
  await expectNoSeriousViolations(page)
})

test('accessibility: Alerts', async ({ page }) => {
  await page.goto('/#/alerts')
  await settled(page, 'Alerts')
  await expectNoSeriousViolations(page)
})

test('accessibility: My profile', async ({ page }) => {
  await page.goto('/#/settings/sessions')
  await settled(page, 'My profile')
  await expectNoSeriousViolations(page)
})
