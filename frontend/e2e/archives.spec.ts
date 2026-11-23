import { expect, test } from '@playwright/test'
import { confirmPopup } from './popups'
import { loadFixturePaths } from './fixture-paths'

// Real sshd and smbd (backend/scripts/setup_e2e_test_servers.sh), each with
// a logs.zip: expanding it reads only the archive's end and opening its
// member only that member's bytes (ranged reads), through the real SFTP and
// SMB clients rather than a mocked one.
for (const protocol of ['ssh', 'smb'] as const) {
  test(`${protocol.toUpperCase()} source: a .zip expands and its member opens`, async ({ page }) => {
    const fixture = loadFixturePaths()[protocol]
    const sourceName = `E2E ${protocol.toUpperCase()} Zip ${Date.now()}`

    await page.goto('/#/settings/sources/new')
    await page.getByLabel('Name', { exact: true }).fill(sourceName)
    await page.getByRole('dialog').getByLabel('Protocol').selectOption(protocol)
    await page.getByLabel('Host').fill(fixture.host)
    await page.getByLabel('Port').fill(String(fixture.port))
    await page.getByLabel('Base path').fill(fixture.base_path)
    await page.getByLabel('Username').fill(fixture.username)
    await page.getByLabel('Password', { exact: true }).fill(fixture.password)
    await page.getByRole('button', { name: 'Save' }).click()

    await expect(page).toHaveURL(/#\/settings\/sources\/\d+$/)
    const sourceId = Number(page.url().split('/').pop())

    // The archive and its member both have to be visible.
    const addRow = page.locator('.rule-editor .add-row')
    await addRow.getByRole('combobox').selectOption('include')
    await addRow.getByPlaceholder('**/*.log or re:^access.*\\.log$').fill('**/*')
    await addRow.getByRole('button', { name: 'Add rule' }).click()
    await expect(page.locator('.rule-row')).toHaveCount(1)

    // Real network round trips: the same generous timeouts as the source specs.
    await page.goto(`/#/viewer/${sourceId}`)
    const explorer = page.locator('.explorer')
    await explorer.getByRole('button', { name: 'logs.zip' }).click({ timeout: 20_000 })
    await expect(explorer.getByRole('button', { name: 'app.log' })).toBeVisible({ timeout: 20_000 })
    await explorer.getByRole('button', { name: 'app.log' }).click()
    await expect(page.locator('.cm-content')).toContainText(`${protocol} zipped log line 1`, {
      timeout: 20_000,
    })

    await page.goto('/#/settings/sources')
    await page
      .getByRole('row', { name: new RegExp(sourceName) })
      .getByRole('button', { name: 'Delete' })
      .click()
    await confirmPopup(page, 'Delete')
    await expect(page.getByRole('row', { name: new RegExp(sourceName) })).toHaveCount(0)
  })
}
