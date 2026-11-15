import { appendFileSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import path from 'node:path'
import { expect, test, type APIRequestContext } from '@playwright/test'

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
