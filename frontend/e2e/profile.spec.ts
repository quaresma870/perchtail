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
  await expect(page.locator('nav .user-menu-name')).toHaveText(name)
  await expect(page.getByLabel('Email')).toHaveValue(/^e2e-\d+@example\.com$/)

  await page.reload()
  await expect(page.getByLabel('Display name')).toHaveValue(name)

  // Passes the browser's own check (type=email), not the server's.
  await page.getByLabel('Email').fill('e2e-admin@example')
  await page.getByRole('button', { name: 'Save', exact: true }).click()
  await expect(page.getByText('Enter a valid email address.')).toBeVisible()

  // Back to how it was, for the other specs.
  await page.getByLabel('Display name').fill('')
  await page.getByLabel('Email').fill('')
  await page.getByRole('button', { name: 'Save', exact: true }).click()
  await expect(page.locator('nav .user-menu-name')).toHaveText(/e2e-admin/)
})

test('user menu: opens from the keyboard, goes to My profile, closes on Escape', async ({ page }) => {
  await page.goto('/#/viewer')
  const button = page.locator('nav').getByRole('button', { name: /^User menu/ })
  await expect(button).toHaveAttribute('aria-expanded', 'false')
  await button.focus()
  await page.keyboard.press('Enter')
  await expect(page.getByRole('menuitem', { name: 'My profile' })).toBeFocused()
  await page.keyboard.press('ArrowDown')
  await expect(page.getByRole('menuitem', { name: 'Log out' })).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(page.getByRole('menu')).toHaveCount(0)
  await expect(button).toBeFocused()

  await button.click()
  await page.getByRole('menuitem', { name: 'My profile' }).click()
  await expect(page).toHaveURL(/#\/profile$/)
  await expect(page.getByRole('menu')).toHaveCount(0)
  // A click outside closes it.
  await button.click()
  await page.getByRole('heading', { name: 'My profile' }).click()
  await expect(page.getByRole('menu')).toHaveCount(0)
})
