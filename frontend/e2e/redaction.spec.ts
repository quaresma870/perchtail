import { appendFileSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import path from 'node:path'
import { expect, test, type APIRequestContext, type Browser, type Locator, type Page } from '@playwright/test'
import { confirmPopup } from './popups'
import { setUpMfa } from './totp'

// Redaction in the viewer (#382, #249): with redaction on, an opened file
// shows `[redacted: ...]` in place of its secrets, the viewer says how many
// values were replaced, and Tail sends the new lines redacted too --
// including a line that was still being written when the file was opened.
// A local-disk source under the e2e backend's LOCAL_SOURCE_ROOTS
// (backend/scripts/run_e2e_server.sh). Redaction is deployment-wide, so the
// test turns it back off (and the detectors it turned on) whatever happens.
const fixtureRoot = '/tmp/perchtail-e2e-fixtures'
const stamp = Date.now()
let folder = ''
const fileName = 'app.log'

interface RedactionRule {
  id: number
  name: string
  detector: string | null
  enabled: boolean
}

async function setDetectors(request: APIRequestContext, ids: number[], enabled: boolean) {
  for (const id of ids) {
    const response = await request.patch(`/redaction-rules/${id}`, { data: { enabled } })
    expect(response.ok()).toBeTruthy()
  }
}

async function setRedaction(request: APIRequestContext, enabled: boolean) {
  const response = await request.patch('/system-settings', { data: { redaction_enabled: enabled } })
  expect(response.ok()).toBeTruthy()
}

test('redacted content in the viewer and in Tail', async ({ page }) => {
  mkdirSync(fixtureRoot, { recursive: true })
  // A private folder with an unpredictable name (0700), not a fixed path in /tmp.
  folder = mkdtempSync(path.join(fixtureRoot, 'redaction-'))
  const filePath = path.join(folder, fileName)
  writeFileSync(
    filePath,
    'login user=alice@example.com from 10.1.2.3\n' +
      'charge card 4111 1111 1111 1111 ok\n' +
      'dsn=postgres://app:Sup3rS3cret@db/x password=hunter2\n' +
      // Still being written: it shows as a marker until it is whole.
      'token=abc',
  )

  const rules: RedactionRule[] = await (await page.request.get('/redaction-rules')).json()
  const turnedOn = rules.filter((rule) => rule.detector !== null && !rule.enabled).map((rule) => rule.id)
  let sourceId: number | null = null
  try {
    const created = await page.request.post('/sources', {
      data: { name: `e2e-redaction-${stamp}`, protocol: 'local', host: 'localhost', base_path: folder },
    })
    expect(created.ok()).toBeTruthy()
    sourceId = (await created.json()).id
    const rule = await page.request.post(`/sources/${sourceId}/rules`, {
      data: { type: 'include', pattern: '*.log' },
    })
    expect(rule.ok()).toBeTruthy()
    await setDetectors(page.request, turnedOn, true)
    await setRedaction(page.request, true)

    await page.goto(`/#/viewer/${sourceId}`)
    await page.locator('.tree-body').getByRole('button', { name: fileName }).click({ timeout: 20_000 })
    const content = page.locator('.cm-content')
    await expect(content).toContainText('user=[redacted: email]')
    await expect(content).toContainText('[redacted: card ****1111]')
    await expect(content).toContainText('[redacted: incomplete line]')
    for (const secret of ['alice@example.com', '10.1.2.3', '4111 1111', 'Sup3rS3cret', 'hunter2', 'token=abc']) {
      await expect(content).not.toContainText(secret)
    }
    await expect(content.locator('.cm-redacted').first()).toBeVisible()
    const count = page.getByTestId('redaction-count')
    await expect(count).toHaveText(/^\d+ values redacted$/)
    const before = Number((await count.textContent())?.split(' ')[0])
    expect(before).toBeGreaterThanOrEqual(5)

    // The unfinished line is completed, and a new secret line follows.
    await page.getByRole('button', { name: 'Tail', exact: true }).click()
    appendFileSync(filePath, 'def123\nnext login bob@example.org\n')
    await expect(content).toContainText('next login [redacted: email]', { timeout: 20_000 })
    await expect(content).toContainText('token=[redacted: token]')
    await expect(content).not.toContainText('[redacted: incomplete line]')
    await expect(content).not.toContainText('bob@example.org')
    await expect(content).not.toContainText('abcdef123')
    await expect(count).toHaveText(`${before + 2} values redacted`)
    await page.getByRole('button', { name: 'Tail', exact: true }).click()

    // The download is redacted too, says so, and is named for it.
    const download = page.getByRole('link', { name: 'Download (redacted)', exact: true })
    await expect(download).toBeVisible()
    const href = await download.getAttribute('href')
    expect(href).not.toBeNull()
    const downloaded = await page.request.get(href as string)
    expect(downloaded.ok()).toBeTruthy()
    expect(downloaded.headers()['x-perchtail-redacted']).toBe('1')
    expect(downloaded.headers()['content-disposition']).toContain('app.redacted.log')
    const body = await downloaded.text()
    expect(body).toContain('token=[redacted: token]')
    for (const secret of ['alice@example.com', '4111 1111', 'Sup3rS3cret', 'hunter2', 'abcdef123', 'bob@example.org']) {
      expect(body).not.toContain(secret)
    }
  } finally {
    await setRedaction(page.request, false)
    await setDetectors(page.request, turnedOn, false)
    if (sourceId !== null) {
      const deleted = await page.request.delete(`/sources/${sourceId}`)
      expect(deleted.ok()).toBeTruthy()
    }
    if (folder) rmSync(folder, { recursive: true, force: true })
  }
})

// The settings screens (Settings -> Redaction and the source editor's
// Redaction section), for super admins only.

const emailRule = 'Email addresses'

/** Flips a switch: its track covers the hidden checkbox, so the click goes
 * to the wrapping <label class="switch">, as a user's would. */
const flip = (input: Locator) => input.locator('xpath=ancestor::label[1]').click()

test('a super admin configures redaction from the settings screens', async ({ page }) => {
  // Two settings screens and the viewer in one go.
  test.slow()
  mkdirSync(fixtureRoot, { recursive: true })
  const dir = mkdtempSync(path.join(fixtureRoot, 'redaction-ui-'))
  writeFileSync(path.join(dir, 'orders.log'), 'buyer alice@example.com order ORD-12345678 ref SRC-999\n')
  const systemRuleName = `e2e-ord-${stamp}`
  const sourceRuleName = `e2e-src-${stamp}`
  const before: RedactionRule[] = await (await page.request.get('/redaction-rules')).json()
  const email = before.find((rule) => rule.detector === 'email')
  expect(email).toBeDefined()
  let sourceId: number | null = null
  try {
    // Settings -> Redaction, from the settings navigation.
    await page.goto('/#/settings/sources')
    await page.locator('.settings-nav').getByRole('link', { name: 'Redaction' }).click()
    await expect(page).toHaveURL(/#\/settings\/redaction$/)
    await expect(page.getByRole('heading', { level: 1, name: 'Redaction' })).toBeVisible()

    // Turns redaction and the email detector on.
    const onSwitch = page.getByLabel('Redact file content')
    await expect(onSwitch).not.toBeChecked()
    await flip(onSwitch)
    await confirmPopup(page, 'Turn on')
    await expect(onSwitch).toBeChecked()
    const emailSwitch = page.getByLabel(`${emailRule} on`)
    if (!(await emailSwitch.isChecked())) await flip(emailSwitch)
    await expect(emailSwitch).toBeChecked()

    // Tests, then adds, a custom rule for every source.
    await page.getByLabel('Name', { exact: true }).fill(systemRuleName)
    await page.getByLabel('Pattern (RE2)').fill('ORD-\\d{8}')
    await page.getByLabel('Longest match (bytes)').fill('12')
    await page.getByLabel('Try it on sample text').fill('order ORD-12345678 shipped')
    await page.getByRole('button', { name: 'Test pattern' }).click()
    // The test endpoint names every replacement "test".
    await expect(page.getByTestId('redaction-test-output')).toHaveText('order [redacted: test] shipped')
    await expect(page.getByTestId('redaction-test-result').locator('mark')).toHaveText('ORD-12345678')
    await page.getByRole('button', { name: 'Add rule' }).click()
    await expect(page.getByTestId('redaction-custom-rule').filter({ hasText: systemRuleName })).toBeVisible()

    // A source: it turns the email detector off and adds a rule of its own.
    const created = await page.request.post('/sources', {
      data: { name: `e2e-redaction-ui-${stamp}`, protocol: 'local', host: 'localhost', base_path: dir },
    })
    expect(created.ok()).toBeTruthy()
    sourceId = (await created.json()).id
    const visible = await page.request.post(`/sources/${sourceId}/rules`, {
      data: { type: 'include', pattern: '*.log' },
    })
    expect(visible.ok()).toBeTruthy()

    await page.goto(`/#/settings/sources/${sourceId}/edit`)
    const section = page.getByTestId('source-redaction')
    await expect(section).toBeVisible()
    const emailHere = section.getByLabel(`Apply ${emailRule} on this source`)
    await expect(emailHere).toBeChecked()
    await flip(emailHere)
    await expect(emailHere).not.toBeChecked()
    await expect(section.getByTestId('source-redaction-rule-email')).toContainText('turned off on this source')
    await section.getByLabel('Name', { exact: true }).fill(sourceRuleName)
    await section.getByLabel('Pattern (RE2)').fill('SRC-\\d{3}')
    await section.getByLabel('Longest match (bytes)').fill('7')
    await section.getByRole('button', { name: 'Add rule' }).click()
    await expect(section.getByTestId('redaction-custom-rule').filter({ hasText: sourceRuleName })).toBeVisible()
    const effective: RedactionRule[] = await (
      await page.request.get(`/sources/${sourceId}/redaction-rules/effective`)
    ).json()
    expect(effective.map((rule) => rule.detector)).not.toContain('email')

    // The viewer shows the file with both rules applied and the email kept.
    await page.goto(`/#/viewer/${sourceId}`)
    await page.locator('.tree-body').getByRole('button', { name: 'orders.log' }).click({ timeout: 20_000 })
    const content = page.locator('.cm-content')
    await expect(content).toContainText(`order [redacted: ${systemRuleName}] ref [redacted: ${sourceRuleName}]`)
    await expect(content).toContainText('alice@example.com')
    await expect(content).not.toContainText('ORD-12345678')
    await expect(content).not.toContainText('SRC-999')
  } finally {
    await setRedaction(page.request, false)
    const after: RedactionRule[] = await (await page.request.get('/redaction-rules')).json()
    for (const rule of after.filter((r) => r.detector === null && r.name === systemRuleName)) {
      expect((await page.request.delete(`/redaction-rules/${rule.id}`)).ok()).toBeTruthy()
    }
    if (email) await setDetectors(page.request, [email.id], email.enabled)
    if (sourceId !== null) expect((await page.request.delete(`/sources/${sourceId}`)).ok()).toBeTruthy()
    rmSync(dir, { recursive: true, force: true })
  }
})

// Within the default password policy (#245): nothing from the username.
const tempPassword = 'Lantern-Orbit-temp-58!'
const newPassword = 'Tagus-River-new-73!'

/** Signs in as `username` in its own browser context (the shared page keeps
 * the super admin's session); a role with a global capability sets up MFA. */
async function signIn(browser: Browser, username: string): Promise<Page> {
  const context = await browser.newContext({ storageState: { cookies: [], origins: [] } })
  const userPage = await context.newPage()
  await userPage.goto('/#/login')
  await userPage.getByLabel('Username').fill(username)
  await userPage.getByLabel('Password').fill(tempPassword)
  await userPage.getByRole('button', { name: 'Sign in' }).click()
  await expect(userPage).toHaveURL(/#\/change-password$/)
  await userPage.getByLabel('Current (temporary) password').fill(tempPassword)
  await userPage.getByLabel('New password').fill(newPassword)
  await userPage.getByRole('button', { name: 'Save' }).click()
  await expect(userPage).toHaveURL(/#\/setup-mfa$/)
  await setUpMfa(userPage, newPassword)
  await expect(userPage).toHaveURL(/#\/viewer$/)
  return userPage
}

test("redaction settings are hidden from everyone but super admins", async ({ page, browser }) => {
  mkdirSync(fixtureRoot, { recursive: true })
  const dir = mkdtempSync(path.join(fixtureRoot, 'redaction-hidden-'))
  writeFileSync(path.join(dir, 'app.log'), 'hello\n')
  const post = async (url: string, data: unknown) => {
    const response = await page.request.post(url, { data })
    expect(response.ok(), await response.text()).toBeTruthy()
    return (await response.json()) as { id: number }
  }
  // A delegated admin: system settings, sources, and every capability on its customer's sources.
  const customer = await post('/customers', { name: `E2E Redaction ${stamp}` })
  const role = await post('/roles', {
    name: `E2E Redaction admin ${stamp}`,
    global_capabilities: ['manage_system_settings', 'create_source'],
  })
  await post(`/roles/${role.id}/grants`, {
    scope_type: 'customer',
    scope_id: customer.id,
    capabilities: ['view', 'download', 'manage_rules', 'run_now', 'manage_source'],
  })
  const username = `e2e-redaction-${stamp}`
  const user = await post('/users', { username, password: tempPassword, role_id: role.id })
  const source = await post('/sources', {
    name: `e2e-redaction-hidden-${stamp}`,
    protocol: 'local',
    host: 'localhost',
    base_path: dir,
    customer_id: customer.id,
  })
  try {
    const userPage = await signIn(browser, username)
    await userPage.goto('/#/settings/sources')
    await expect(userPage.getByRole('heading', { level: 1, name: 'Sources' })).toBeVisible()
    await expect(userPage.locator('.settings-nav').getByRole('link', { name: 'Redaction' })).toHaveCount(0)

    // A typed address leads away, and the API refuses.
    await userPage.goto('/#/settings/redaction')
    await expect(userPage).toHaveURL(/#\/settings\/sources$/)
    expect((await userPage.request.get('/redaction-rules')).status()).toBe(403)

    // The source editor has no Redaction section.
    await userPage.goto(`/#/settings/sources/${source.id}/edit`)
    await expect(userPage.getByRole('button', { name: 'Save' })).toBeVisible()
    await expect(userPage.getByText('Loading…', { exact: true })).toHaveCount(0)
    await expect(userPage.getByTestId('source-redaction')).toHaveCount(0)
    await userPage.context().close()
  } finally {
    expect((await page.request.delete(`/users/${user.id}`)).ok()).toBeTruthy()
    expect((await page.request.delete(`/sources/${source.id}`)).ok()).toBeTruthy()
    expect((await page.request.delete(`/customers/${customer.id}`)).ok()).toBeTruthy()
    rmSync(dir, { recursive: true, force: true })
  }
})
