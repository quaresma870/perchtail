import { expect, test } from '@playwright/test'
import { loadFixturePaths } from './fixture-paths'

// A `local`-protocol source needs no credentials and reads straight off
// disk -- pointed at the same real fixture directory sources-ssh.spec.ts's
// sshd serves, so this doesn't need its own separate fixture files.
// run_e2e_server.sh sets SEARCH_INDEX_INTERVAL_SECONDS=2 so the background
// indexer (app/search_index.py) picks this up quickly instead of the
// production 300s default.
const sourceName = `E2E Search Source ${Date.now()}`

test('full-text search: indexes a source and deep-links a hit into the viewer', async ({
  page,
}) => {
  const fixtures = loadFixturePaths()

  await page.goto('/#/settings/sources/new')
  await page.getByLabel('Name', { exact: true }).fill(sourceName)
  await page.getByLabel('Protocol').selectOption('local')
  await page.getByLabel('Host').fill('localhost')
  await page.getByLabel('Base path').fill(fixtures.ssh.base_path)
  await page.getByLabel('Include in full-text search').check()
  await page.getByRole('button', { name: 'Save' }).click()

  await expect(page).toHaveURL(/#\/settings\/sources\/\d+$/)
  const sourceId = Number(page.url().split('/').pop())

  const addRow = page.locator('.rule-editor .add-row')
  await addRow.getByRole('combobox').selectOption('include')
  await addRow.getByPlaceholder('**/*.log or re:^access.*\\.log$').fill('**/*.log')
  await addRow.getByRole('button', { name: 'Add rule' }).click()

  await page.goto('/#/search')
  await page.getByPlaceholder('Search indexed log content…').fill('hello world log line 2')

  // Scoped by this test's own (uniquely timestamped) source name, not just
  // the content snippet -- a database that still has an earlier attempt's
  // source (e.g. a prior local run whose cleanup step never got reached)
  // would otherwise satisfy an unscoped "some result exists" wait before
  // *this* run's own source is actually indexed.
  const hit = page
    .locator('button', { hasText: sourceName })
    .filter({ hasText: 'hello world log line 2' })

  // Poll until the background sweep has indexed it -- generous timeout
  // covers the up-to-2s indexing interval plus read/index time.
  // exact: true on the button below -- the source name ("E2E Search
  // Source ...") shows up as its own result once indexed, and contains
  // "Search" as a substring, which would otherwise match this same query
  // too. Not "Matching content" -- Search.svelte only renders that heading
  // when a source's name/host *also* matches the query (which this one
  // doesn't); a content hit's own .result button still renders without it.
  await expect(async () => {
    await page.getByRole('button', { name: 'Search', exact: true }).click()
    await expect(hit).toBeVisible()
  }).toPass({ timeout: 30_000, intervals: [1000] })

  await hit.click()
  await expect(page).toHaveURL(new RegExp(`#/viewer/${sourceId}\\?path=`))
  await expect(page.locator('.cm-content')).toContainText('hello world log line 2')

  // Cleanup.
  await page.goto('/#/settings/sources')
  page.once('dialog', (dialog) => dialog.accept())
  await page
    .getByRole('row', { name: new RegExp(sourceName) })
    .getByRole('button', { name: 'delete' })
    .click()
  await expect(page.getByRole('row', { name: new RegExp(sourceName) })).toHaveCount(0)
})
