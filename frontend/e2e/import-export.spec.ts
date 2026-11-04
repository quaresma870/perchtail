import { expect, test } from '@playwright/test'
import { loadFixturePaths } from './fixture-paths'

// Bulk import (#179): preview shows the plan without writing, apply
// creates the customer and source, and a second preview finds nothing to
// change. A local source keeps it free of credentials and network.
const stamp = Date.now()
const customer = `E2E Import Customer ${stamp}`
const sourceName = `E2E Imported Source ${stamp}`

test('bulk import: preview, apply, idempotent re-preview', async ({ page }) => {
  const fixtures = loadFixturePaths()
  const yaml = [
    'version: 1',
    'customers:',
    `  - name: ${customer}`,
    '    sources:',
    `      - name: ${sourceName}`,
    '        protocol: local',
    '        host: localhost',
    `        base_path: ${fixtures.ssh.base_path}`,
    '        rules:',
    '          - include **/*.log',
    '',
  ].join('\n')

  await page.goto('/#/settings/import-export')
  await page.getByLabel('Import YAML').fill(yaml)
  await page.getByRole('button', { name: 'Preview' }).click()
  await expect(page.getByText(`${customer} / ${sourceName}`)).toBeVisible()

  await page.goto('/#/settings/sources')
  await expect(page.getByRole('row', { name: new RegExp(sourceName) })).toHaveCount(0)

  await page.goto('/#/settings/import-export')
  await page.getByLabel('Import YAML').fill(yaml)
  await page.getByRole('button', { name: 'Preview' }).click()
  await page.getByRole('button', { name: /^Apply 2 changes$/ }).click()
  await expect(page.getByText('Imported. 2 changes applied.')).toBeVisible()

  await page.getByRole('button', { name: 'Preview' }).click()
  await expect(page.getByText('Nothing — everything already matches the file.')).toBeVisible()

  await page.goto('/#/settings/sources')
  page.once('dialog', (dialog) => dialog.accept())
  await page
    .getByRole('row', { name: new RegExp(sourceName) })
    .getByRole('button', { name: 'delete' })
    .click()
  await expect(page.getByRole('row', { name: new RegExp(sourceName) })).toHaveCount(0)
})
