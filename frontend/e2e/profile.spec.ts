import { expect, test } from '@playwright/test'

// My profile (#243): name and email, and the sessions list moved from
// Settings -> Sessions. Runs as the seeded admin and puts the profile back.
test('profile: set name and email, see sessions, old links land here', async ({ page }) => {
  await page.goto('/#/settings/sessions')
  await expect(page).toHaveURL(/#\/profile$/)
  await expect(page.getByRole('heading', { name: 'My profile' })).toBeVisible()

  const sessions = page.locator('section.panel', {
    has: page.getByRole('heading', { name: 'Sessions' }),
  })
  const currentSessionRow = sessions.locator('.session-row', { hasText: 'This device' })
  await expect(currentSessionRow.getByRole('button', { name: 'Log out' })).toBeVisible()

  const name = `E2E Admin ${Date.now()}`
  await page.getByLabel('Display name').fill(name)
  await page.getByLabel('Email').fill(`E2E-${Date.now()}@Example.com`)
  await page.getByRole('button', { name: 'Save', exact: true }).click()
  await expect(page.getByText('Saved.')).toBeVisible()
  await expect(page.locator('nav a.username')).toHaveText(name)
  await expect(page.getByLabel('Email')).toHaveValue(/^e2e-\d+@example\.com$/)

  await page.reload()
  await expect(page.getByLabel('Display name')).toHaveValue(name)

  await page.getByLabel('Email').fill('not an email')
  await page.getByRole('button', { name: 'Save', exact: true }).click()
  await expect(page.getByText('Enter a valid email address.')).toBeVisible()

  // Back to how it was, for the other specs.
  await page.getByLabel('Display name').fill('')
  await page.getByLabel('Email').fill('')
  await page.getByRole('button', { name: 'Save', exact: true }).click()
  await expect(page.locator('nav a.username')).toHaveText(/e2e-admin/)
})
