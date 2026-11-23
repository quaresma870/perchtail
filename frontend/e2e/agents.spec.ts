import { expect, test, type Page } from '@playwright/test'

// Settings -> Agents (#319). No agent connects in e2e, so nothing is
// upgraded: this covers the settings, a source's own policy and the table.
const stamp = Date.now()
const name = `e2e-agent-${stamp}`

const savedSettings = (page: Page) =>
  page.waitForResponse(
    (response) =>
      response.url().endsWith('/agent-upgrades/settings') && response.request().method() === 'PATCH',
  )

test('agents: policy, window and a source of its own', async ({ page }) => {
  const created = await page.request.post('/sources', {
    data: { name, protocol: 'agent', host: 'dmz-host', base_path: '/var/log' },
  })
  expect(created.ok()).toBeTruthy()
  const id = (await created.json()).id
  try {
    await page.goto('/#/settings/sources')
    await page.locator('.settings-nav').getByRole('link', { name: 'Agents' }).click()
    await expect(page).toHaveURL(/#\/settings\/agents$/)
    await expect(page.getByRole('heading', { name: 'Agents' })).toBeVisible()

    // Off by default, with the server's time zone shown.
    const policy = page.getByLabel('Remote upgrades')
    await expect(policy).toHaveValue('off')
    await expect(page.locator('legend')).toContainText('Automatic window (')

    // Automatic, on Saturdays and Sundays from 22:30 for 6 hours.
    await policy.selectOption('automatic')
    for (const day of ['Mon', 'Tue', 'Wed', 'Thu', 'Fri']) {
      await page.getByRole('checkbox', { name: day, exact: true }).uncheck()
    }
    await page.getByLabel('Window start').fill('22:30')
    await page.getByLabel('Window length in hours').fill('6')
    await Promise.all([savedSettings(page), page.getByRole('button', { name: 'Save' }).click()])
    await page.reload()
    await expect(policy).toHaveValue('automatic')
    await expect(page.getByLabel('Window start')).toHaveValue('22:30')
    await expect(page.getByText(/Automatic window: Sat, Sun, from 22:30 for 6 hours/)).toBeVisible()
    await expect(page.getByRole('button', { name: /Upgrade all outdated agents \(0\)/ })).toBeDisabled()

    // The source's row: follows the system until it gets its own policy.
    const row = page.locator('tr', { has: page.getByRole('link', { name }) })
    await expect(row).toContainText('not connected')
    const sourcePolicy = row.getByLabel(`Upgrade policy of ${name}`)
    await expect(sourcePolicy).toHaveValue('inherit')
    await sourcePolicy.selectOption('off')
    await page.reload()
    await expect(page.locator('tr', { has: page.getByRole('link', { name }) }).getByLabel(`Upgrade policy of ${name}`)).toHaveValue('off')
  } finally {
    await page.request.delete(`/sources/${id}`)
    // Back to the defaults for the other specs.
    await page.request.patch('/agent-upgrades/settings', {
      data: { policy: 'off', window_days: [0, 1, 2, 3, 4, 5, 6], window_start: '02:00', window_hours: 4 },
    })
  }
})
