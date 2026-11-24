import { rmSync, writeFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { expect, test, type APIRequestContext, type Page } from '@playwright/test'
import { loadFixturePaths } from './fixture-paths'

// Remakes the README's screenshots (docs/images/screenshot-*.png) from
// demo data this spec creates: fictional customers and hosts, never real
// ones. Not part of the normal suite: it runs only with README_SCREENSHOTS
// set, against the usual e2e backend (see CONTRIBUTING.md):
//
//   README_SCREENSHOTS=1 npx playwright test e2e/readme-screenshots.spec.ts
//
// SSH and SMB sources point at the e2e test servers. With README_HOST_NAMES
// set, each of them uses its own name (web-01, web-02, files-01, files-02, edge-01)
// as its host instead of 127.0.0.1, for when the machine running the
// backend resolves those names to the test servers (in its /etc/hosts), so
// the pictures show host names. The push-agent source has no agent
// connected.

test.skip(!process.env.README_SCREENSHOTS, 'only with README_SCREENSHOTS set')
test.use({ viewport: { width: 1440, height: 900 }, colorScheme: 'dark' })

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const imagesDir = path.join(__dirname, '..', '..', 'docs', 'images')

// What the spec made, deleted again at the end (newest first), so it can
// run more than once against the same backend.
const made: string[] = []

async function created(request: APIRequestContext, url: string, data: object): Promise<number> {
  const response = await request.post(url, { data })
  expect(response.ok(), `${url}: ${await response.text()}`).toBeTruthy()
  const id = (await response.json()).id as number
  if (!url.includes('/rules') && !url.includes('/grants')) made.push(`${url}/${id}`)
  return id
}

async function shoot(page: Page, name: string) {
  // No caret, hover or animation in the picture.
  await page.mouse.move(0, 0)
  await page.screenshot({ path: path.join(imagesDir, name), animations: 'disabled', caret: 'hide' })
}

/** A log with a few errors and warnings among ordinary lines. */
function demoLog(): string {
  const lines: string[] = []
  const services = ['checkout', 'catalog', 'auth', 'orders', 'payments']
  for (let n = 0; n < 2400; n++) {
    const time = new Date(Date.UTC(2026, 10, 24, 8, 0, n * 3)).toISOString().replace('T', ' ').slice(0, 19)
    const service = services[n % services.length]
    if (n % 37 === 11) {
      lines.push(`${time} ERROR [${service}] request ${10_000 + n} failed: upstream timed out after 30 s`)
    } else if (n % 23 === 5) {
      lines.push(`${time} WARN  [${service}] slow response: ${400 + (n % 9) * 50} ms for /api/${service}`)
    } else {
      lines.push(`${time} INFO  [${service}] GET /api/${service}/${n % 97} 200 in ${12 + (n % 40)} ms`)
    }
  }
  return lines.join('\n') + '\n'
}

test('README screenshots', async ({ page }) => {
  test.setTimeout(180_000)
  const request = page.request
  const fixtures = loadFixturePaths()
  const log = path.join(fixtures.ssh.base_path, 'app.log')
  writeFileSync(log, demoLog())
  try {
    await screenshots(page, request, fixtures)
  } finally {
    for (const url of made.reverse()) await request.delete(url)
    made.length = 0
    rmSync(log, { force: true })
  }
})

async function screenshots(
  page: Page,
  request: APIRequestContext,
  fixtures: ReturnType<typeof loadFixturePaths>,
) {
  const named = !!process.env.README_HOST_NAMES

  // Customers, folders and sources.
  const northwind = await created(request, '/customers', { name: 'Northwind' })
  const contoso = await created(request, '/customers', { name: 'Contoso' })
  const fabrikam = await created(request, '/customers', { name: 'Fabrikam' })
  const production = await created(request, '/folders', { name: 'Production', customer_id: northwind })
  const ssh = {
    protocol: 'ssh',
    host: fixtures.ssh.host,
    port: fixtures.ssh.port,
    base_path: fixtures.ssh.base_path,
    credential: { username: fixtures.ssh.username, password: fixtures.ssh.password },
  }
  const smb = {
    protocol: 'smb',
    host: fixtures.smb.host,
    port: fixtures.smb.port,
    base_path: fixtures.smb.base_path,
    credential: { username: fixtures.smb.username, password: fixtures.smb.password },
  }
  const winrm = {
    protocol: 'winrm',
    host: 'app-01',
    base_path: 'C:\\Logs\\e2e-fixture',
    credential: { username: 'svc-logs', password: 'unused-by-the-fake-connector' },
  }
  const sources = [
    { name: 'web-01', customer_id: northwind, folder_id: production, ...ssh },
    { name: 'web-02', customer_id: northwind, folder_id: production, ...ssh },
    { name: 'db-01', customer_id: northwind, protocol: 'agent', host: 'db-01', base_path: '/var/log' },
    { name: 'files-01', customer_id: contoso, ...smb },
    { name: 'app-01', customer_id: contoso, ...winrm },
    { name: 'files-02', customer_id: fabrikam, ...smb },
    { name: 'edge-01', customer_id: fabrikam, ...ssh },
  ]
  const ids: Record<string, number> = {}
  for (const source of sources) {
    if (named && (source.protocol === 'ssh' || source.protocol === 'smb')) {
      source.host = source.name
    }
    ids[source.name] = await created(request, '/sources', source)
    await created(request, `/sources/${ids[source.name]}/rules`, { type: 'include', pattern: '**/*.log' })
    if (source.protocol !== 'agent') await request.post(`/sources/${ids[source.name]}/check`)
  }

  // The built-in log source's status too.
  const all = (await (await request.get('/sources')).json()) as { id: number; is_system: boolean }[]
  for (const source of all.filter((s) => s.is_system)) await request.post(`/sources/${source.id}/check`)

  // A role with global capabilities and grants on customer, folder and source.
  const role = await created(request, '/roles', {
    name: 'Support engineers',
    global_capabilities: ['view_audit_log', 'manage_alerts'],
  })
  await created(request, `/roles/${role}/grants`, {
    scope_type: 'customer',
    scope_id: northwind,
    capabilities: ['view', 'download'],
  })
  await created(request, `/roles/${role}/grants`, {
    scope_type: 'folder',
    scope_id: production,
    capabilities: ['view', 'download', 'manage_rules'],
  })
  await created(request, `/roles/${role}/grants`, {
    scope_type: 'customer',
    scope_id: contoso,
    capabilities: ['view'],
  })
  await created(request, `/roles/${role}/grants`, {
    scope_type: 'source',
    scope_id: ids['files-01'],
    capabilities: ['view', 'download'],
  })

  // 1. Sources.
  await page.goto('/#/settings/sources')
  await expect(page.getByRole('heading', { level: 1, name: 'Sources' })).toBeVisible()
  await expect(page.getByRole('row', { name: /edge-01/ })).toBeVisible()
  await expect(page.getByText('Loading…', { exact: true })).toHaveCount(0)
  await shoot(page, 'screenshot-sources.png')

  // 2. The viewer: a log open, the find bar and Find all.
  await page.goto(`/#/viewer/${ids['web-01']}`)
  await page.locator('.tree-body').getByRole('button', { name: 'app.log' }).click({ timeout: 20_000 })
  await expect(page.locator('.tab.active .tab-label')).toContainText('app.log')
  await page.getByRole('button', { name: 'Find', exact: true }).click()
  await page.getByRole('textbox', { name: 'Find', exact: true }).fill('ERROR')
  await page.getByRole('button', { name: 'Find all', exact: true }).click()
  await expect(page.getByText(/\d+ lines?$/).first()).toBeVisible()
  // The first match selected: "1 of N" in the find bar.
  await page.getByRole('textbox', { name: 'Find', exact: true }).press('Enter')
  await expect(page.getByText(/^1 of \d+$/)).toBeVisible()
  await shoot(page, 'screenshot-viewer.png')

  // 3. The role editor.
  // A taller window for this one, so the popup shows every grant.
  await page.setViewportSize({ width: 1440, height: 1200 })
  await page.goto(`/#/settings/roles/${role}`)
  const editor = page.getByRole('dialog')
  await expect(editor).toBeVisible()
  for (const target of ['Northwind', 'Contoso', 'Production', 'files-01']) {
    await expect(editor.getByRole('cell', { name: target, exact: true })).toBeInViewport()
  }
  await shoot(page, 'screenshot-role-editor.png')
  await page.setViewportSize({ width: 1440, height: 900 })

  // 4. Settings: the sidebar with its sections, on a settings page.
  await page.goto('/#/settings/explorer')
  await expect(page.getByRole('heading', { level: 1, name: 'Explorer and limits' })).toBeVisible()
  await expect(page.getByText('Loading…', { exact: true })).toHaveCount(0)
  await shoot(page, 'screenshot-settings.png')
}
