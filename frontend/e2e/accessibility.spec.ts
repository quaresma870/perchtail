import AxeBuilder from '@axe-core/playwright'
import { expect, test, type Page } from '@playwright/test'
import { LARGE_FILE_NAME, largeFileSource } from './large-file'
import { confirmPopup } from './popups'
import { SLOW_FILE_NAME, slowSource } from './slow-open'

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
    // With "Forgot password?" shown (#244).
    await page.route('**/auth/login-options', (route) =>
      route.fulfill({ json: { password_reset: true } }),
    )
    await page.goto('/#/login')
    await expect(page.getByRole('button', { name: 'Sign in' })).toBeVisible()
    await expect(page.getByRole('link', { name: 'Forgot password?' })).toBeVisible()
    await expectNoSeriousViolations(page)
  })

  // Password recovery (#244), with the recovery API stubbed as on.
  test('accessibility: forgot password', async ({ page }) => {
    await page.route('**/auth/login-options', (route) =>
      route.fulfill({ json: { password_reset: true } }),
    )
    await page.goto('/#/forgot-password')
    await settled(page, 'Reset your password')
    await expect(page.getByRole('button', { name: 'Send the link' })).toBeVisible()
    await expectNoSeriousViolations(page)
  })

  test('accessibility: reset password', async ({ page }) => {
    await page.route('**/auth/password-reset/check', (route) =>
      route.fulfill({
        json: {
          valid: true,
          policy: {
            min_length: 12,
            max_length: 128,
            reject_simple: true,
            reject_personal: true,
            history: 0,
            required_classes: 0,
            max_age_days: 0,
          },
        },
      }),
    )
    await page.goto('/#/reset-password/a11y-token')
    await settled(page, 'Choose a new password')
    await expect(page.getByRole('button', { name: 'Set the password' })).toBeVisible()
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

// The right-click menu of a log line (#287), open over the viewer.
test('accessibility: viewer with a line menu open', async ({ page }) => {
  await page.goto('/#/viewer')
  await page.locator('.explorer').getByRole('button', { name: SYSTEM_SOURCE_NAME }).click()
  await page.locator('.tree-body').getByRole('button', { name: 'perchtail.log' }).click()
  await expect(page.locator('.tab.active .tab-label')).toContainText('perchtail.log')
  await expect(page.locator('.cm-content')).not.toBeEmpty()
  await page.locator('.cm-line').first().click({ button: 'right' })
  await expect(page.getByRole('menu', { name: 'Line actions' })).toBeVisible()
  await expectNoSeriousViolations(page, (axe) =>
    axe.include('html').exclude('.cm-scroller').include('.cm-scroller > *'),
  )
})

// A file opened at its end, with the bar that offers Load earlier and Load
// whole file. A local-disk source, not the SSH/SMB test servers.
test('accessibility: viewer with a file opened at its end', async ({ page }) => {
  const { sourceId, cleanUp } = await largeFileSource(page.request, `e2e-a11y-end-${Date.now()}`)
  try {
    await page.goto(`/#/viewer/${sourceId}`)
    await page.locator('.tree-body').getByRole('button', { name: LARGE_FILE_NAME }).click({ timeout: 20_000 })
    await expect(page.getByTestId('partial-bar')).toBeVisible()
    await expect(page.getByRole('button', { name: 'Load earlier', exact: true })).toBeEnabled()
    await expectNoSeriousViolations(page, (axe) =>
      axe.include('html').exclude('.cm-scroller').include('.cm-scroller > *'),
    )
  } finally {
    await page.goto('/#/viewer')
    await cleanUp()
  }
})

// The progress of a slow open, with Cancel, over the fake WinRM connector.
test('accessibility: viewer while a file opens slowly', async ({ page }) => {
  const { sourceId, cleanUp } = await slowSource(page.request, `e2e-a11y-slow-${Date.now()}`)
  try {
    await page.goto(`/#/viewer/${sourceId}`)
    await page.locator('.tree-body').getByRole('button', { name: SLOW_FILE_NAME }).click({ timeout: 20_000 })
    await expect(page.getByRole('progressbar', { name: `Opening ${SLOW_FILE_NAME}` })).toBeVisible({ timeout: 5_000 })
    await expectNoSeriousViolations(page)
    await page.getByTestId('open-progress').getByRole('button', { name: 'Cancel', exact: true }).click()
    await expect(page.getByTestId('open-progress')).toHaveCount(0)
  } finally {
    await page.goto('/#/viewer')
    await cleanUp()
  }
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

// The pages the System page was split into, each with one of its fields.
const SYSTEM_SETTINGS_PAGES: [string, string, string][] = [
  ['source-defaults', 'Source defaults', 'Background check interval in minutes'],
  ['sign-in-security', 'Sign-in security', 'Require MFA for local accounts'],
  ['explorer', 'Explorer and limits', 'Connections view'],
  ['alerts', 'Alerts', 'Alert check interval in seconds'],
  ['backups', 'Backups and updates', 'Backup interval in hours'],
  ['monitoring', 'Monitoring', ''],
]

for (const [path, title, field] of SYSTEM_SETTINGS_PAGES) {
  test(`accessibility: Settings -> ${title}`, async ({ page }) => {
    await page.goto(`/#/settings/${path}`)
    await settled(page, title)
    if (field) await expect(page.getByLabel(field, { exact: true })).toBeVisible()
    else await expect(page.getByRole('button', { name: /Generate token|Regenerate token/ })).toBeVisible()
    await expectNoSeriousViolations(page)
  })
}

test('accessibility: Settings sidebar at phone width', async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 800 })
  await page.goto('/#/settings/explorer')
  await settled(page, 'Explorer and limits')
  await expect(page.getByLabel('Settings page')).toBeVisible()
  await expectNoSeriousViolations(page)
})

test('accessibility: Settings -> Redaction', async ({ page }) => {
  await page.goto('/#/settings/redaction')
  await settled(page, 'Redaction')
  await expect(page.getByLabel('Redact file content')).toBeVisible()
  // With a pattern tested, so its result is checked too. Nothing is saved.
  await page.getByLabel('Pattern (RE2)').fill('ORD-\\d{8}')
  await page.getByLabel('Try it on sample text').fill('order ORD-12345678 shipped')
  await page.getByRole('button', { name: 'Test pattern' }).click()
  await expect(page.getByTestId('redaction-test-output')).toBeVisible()
  await expectNoSeriousViolations(page)
})

test("accessibility: a source's Redaction section", async ({ page }) => {
  // The system source's editor, which a super admin sees with the section.
  const sources: { id: number; is_system: boolean }[] = await (await page.request.get('/sources')).json()
  const system = sources.find((source) => source.is_system)
  expect(system).toBeDefined()
  await page.goto(`/#/settings/sources/${system?.id}/edit`)
  const section = page.getByTestId('source-redaction')
  await expect(section).toBeVisible()
  await expect(section.getByText('Loading…', { exact: true })).toHaveCount(0)
  await expect(section.getByRole('button', { name: 'Add rule' })).toBeVisible()
  await expectNoSeriousViolations(page)
})

test('accessibility: Settings -> SSO', async ({ page }) => {
  // A provider and two mappings, one to a role outside the viewer's access
  // (as a delegated admin sees it), stubbed so every part of the page is
  // checked. Nothing is saved.
  await page.route('**/sso', (route) =>
    route.fulfill({
      json: [
        {
          id: 1,
          protocol: 'oidc',
          name: 'Corporate SSO',
          enabled: true,
          issuer: 'https://login.example.test/tenant',
          client_id: 'perchtail',
          scopes: 'openid profile email',
          group_claim: 'groups',
          secret_unreadable: false,
        },
      ],
    }),
  )
  await page.route('**/sso/group-mappings', (route) =>
    route.fulfill({
      json: [
        { id: 1, order: 0, group_name: 'log-readers', role_id: 9001, role_name: 'Log readers', role_outside_authority: false },
        { id: 2, order: 1, group_name: 'platform-admins', role_id: 9002, role_name: null, role_outside_authority: true },
      ],
    }),
  )
  await page.goto('/#/settings/sso')
  await settled(page, 'SSO settings')
  await expect(page.getByLabel('Issuer URL')).toHaveValue('https://login.example.test/tenant')
  await expect(page.getByText('A role outside your access')).toBeVisible()
  await expectNoSeriousViolations(page)
})

test('accessibility: Settings -> Email', async ({ page }) => {
  await page.goto('/#/settings/email')
  await settled(page, 'Email settings')
  await expect(page.getByLabel('SMTP server')).toBeVisible()
  await expectNoSeriousViolations(page)
})

test('accessibility: Settings -> Audit log', async ({ page }) => {
  await page.goto('/#/settings/audit-log')
  await settled(page, 'Audit log')
  // The viewer and, below it, the audit log's settings.
  await expect(page.getByLabel('Audit log retention in days')).toBeVisible()
  await expectNoSeriousViolations(page)
})

test('accessibility: Settings -> Access review', async ({ page }) => {
  await page.goto('/#/settings/access-review')
  await settled(page, 'Access review')
  await expect(page.getByText(/^Generated at /)).toBeVisible()
  // A user's access opened, so the customer/folder tree is checked too.
  await page.locator('tr.user-row').first().getByRole('button', { name: /^Show access of / }).click()
  await expect(page.locator('tr.access-row')).toBeVisible()
  await expectNoSeriousViolations(page)
  await page.getByRole('button', { name: 'By source' }).click()
  await expect(page.getByLabel('Search sources')).toBeVisible()
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

test('accessibility: Set up two-factor authentication', async ({ page }) => {
  // The page an account that must use MFA is held on. The seeded admin has
  // MFA on already, so it shows the "on" state; the layout is the same.
  await page.goto('/#/setup-mfa')
  await settled(page, 'Set up two-factor authentication')
  await expect(
    page.getByRole('heading', { name: 'Two-factor authentication', exact: true }),
  ).toBeVisible()
  await expectNoSeriousViolations(page)
})
