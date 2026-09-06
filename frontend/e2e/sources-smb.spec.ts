import { expect, test } from '@playwright/test'
import { loadFixturePaths } from './fixture-paths'

// Real smbd (backend/scripts/setup_e2e_test_servers.sh), not a mocked
// client. sources-ssh.spec.ts already covers the raw-paste rule mode end
// to end, so this one sticks to the row-based UI and instead checks
// last-match-wins across two separate row rules (include-all, then a more
// specific exclude added after it).
const sourceName = `E2E SMB Source ${Date.now()}`

test('SMB source: create, row-based rules (last-match-wins), browse, delete', async ({ page }) => {
  const fixtures = loadFixturePaths()
  const smb = fixtures.smb

  await page.goto('/#/settings/sources/new')
  await page.getByLabel('Name', { exact: true }).fill(sourceName)
  await page.getByLabel('Protocol').selectOption('smb')
  await page.getByLabel('Host').fill(smb.host)
  await page.getByLabel('Port').fill(String(smb.port))
  await page.getByLabel('Base path').fill(smb.base_path)
  await page.getByLabel('Username').fill(smb.username)
  await page.getByLabel('Password', { exact: true }).fill(smb.password)
  await page.getByRole('button', { name: 'Save' }).click()

  await expect(page).toHaveURL(/#\/settings\/sources\/\d+$/)
  const sourceId = Number(page.url().split('/').pop())

  const addRow = page.locator('.rule-editor .add-row')
  await addRow.getByRole('combobox').selectOption('include')
  await addRow.getByPlaceholder('**/*.log or re:^access.*\\.log$').fill('**/*.log')
  await addRow.getByRole('button', { name: 'Add rule' }).click()
  await expect(page.locator('.rule-row')).toHaveCount(1)

  await page.goto(`/#/viewer/${sourceId}`)
  await expect(page.getByRole('button', { name: 'hello.log' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'other.log' })).toBeVisible()
  await page.getByRole('button', { name: 'hello.log' }).click()
  await expect(page.locator('.cm-content')).toContainText('smb hello world log line 1')

  // A later, more specific exclude added after the broad include should
  // win -- last-match-wins, same semantics as .gitignore.
  await page.goto(`/#/settings/sources/${sourceId}`)
  const addRowAgain = page.locator('.rule-editor .add-row')
  await addRowAgain.getByRole('combobox').selectOption('exclude')
  await addRowAgain.getByPlaceholder('**/*.log or re:^access.*\\.log$').fill('hello.log')
  await addRowAgain.getByRole('button', { name: 'Add rule' }).click()
  await expect(page.locator('.rule-row')).toHaveCount(2)

  await page.goto(`/#/viewer/${sourceId}`)
  await expect(page.getByRole('button', { name: 'other.log' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'hello.log' })).toHaveCount(0)

  await page.goto('/#/settings/sources')
  page.once('dialog', (dialog) => dialog.accept())
  await page
    .getByRole('row', { name: new RegExp(sourceName) })
    .getByRole('button', { name: 'delete' })
    .click()
  await expect(page.getByRole('row', { name: new RegExp(sourceName) })).toHaveCount(0)
})
