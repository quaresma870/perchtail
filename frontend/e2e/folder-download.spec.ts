import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import path from 'node:path'
import { gunzipSync, gzipSync, inflateRawSync } from 'node:zlib'
import { expect, test, type APIRequestContext } from '@playwright/test'

// A folder downloaded as a ZIP from the explorer's download link, without
// and with redaction: the ZIP holds the folder's files in order, a .gz file
// as it is (redacted, when downloads are), and says when it was redacted.
// A local-disk source under the e2e backend's LOCAL_SOURCE_ROOTS
// (backend/scripts/run_e2e_server.sh). Redaction is deployment-wide, so the
// test turns it back off (and the detectors it turned on) whatever happens.
const fixtureRoot = '/tmp/perchtail-e2e-fixtures'
const stamp = Date.now()

interface RedactionRule {
  id: number
  detector: string | null
  enabled: boolean
}

/** The members of a ZIP, by name in the order of its central directory,
 * read from that directory (a streamed ZIP's local headers carry no sizes). */
function unzip(data: Buffer): Map<string, Buffer> {
  const eocd = data.lastIndexOf(Buffer.from([0x50, 0x4b, 0x05, 0x06]))
  expect(eocd).toBeGreaterThanOrEqual(0)
  const count = data.readUInt16LE(eocd + 10)
  let at = data.readUInt32LE(eocd + 16)
  const members = new Map<string, Buffer>()
  for (let i = 0; i < count; i++) {
    expect(data.readUInt32LE(at)).toBe(0x02014b50)
    const method = data.readUInt16LE(at + 10)
    const compressedSize = data.readUInt32LE(at + 20)
    const nameLength = data.readUInt16LE(at + 28)
    const extraLength = data.readUInt16LE(at + 30)
    const commentLength = data.readUInt16LE(at + 32)
    const local = data.readUInt32LE(at + 42)
    const name = data.subarray(at + 46, at + 46 + nameLength).toString('utf8')
    const start = local + 30 + data.readUInt16LE(local + 26) + data.readUInt16LE(local + 28)
    const raw = data.subarray(start, start + compressedSize)
    members.set(name, method === 8 ? inflateRawSync(raw) : Buffer.from(raw))
    at += 46 + nameLength + extraLength + commentLength
  }
  return members
}

async function setRedaction(request: APIRequestContext, enabled: boolean) {
  const response = await request.patch('/system-settings', { data: { redaction_enabled: enabled } })
  expect(response.ok()).toBeTruthy()
}

async function setDetectors(request: APIRequestContext, ids: number[], enabled: boolean) {
  for (const id of ids) {
    const response = await request.patch(`/redaction-rules/${id}`, { data: { enabled } })
    expect(response.ok()).toBeTruthy()
  }
}

test('a folder downloads as a ZIP, without and with redaction', async ({ page }) => {
  mkdirSync(fixtureRoot, { recursive: true })
  // A private folder with an unpredictable name (0700), not a fixed path in /tmp.
  const dir = mkdtempSync(path.join(fixtureRoot, 'folder-zip-'))
  const logs = path.join(dir, 'zipme')
  mkdirSync(logs)
  const plain = 'login user=alice@example.com ok\n'
  const rotated = 'old login user=bob@example.org ok\n'
  writeFileSync(path.join(logs, '1-app.log'), plain)
  writeFileSync(path.join(logs, '2-old.log.gz'), gzipSync(rotated))
  writeFileSync(path.join(logs, '3-last.log'), 'last line\n')

  const rules: RedactionRule[] = await (await page.request.get('/redaction-rules')).json()
  const turnedOn = rules.filter((rule) => rule.detector !== null && !rule.enabled).map((rule) => rule.id)
  let sourceId: number | null = null
  try {
    const created = await page.request.post('/sources', {
      data: { name: `e2e-folder-zip-${stamp}`, protocol: 'local', host: 'localhost', base_path: dir },
    })
    expect(created.ok()).toBeTruthy()
    sourceId = (await created.json()).id
    const rule = await page.request.post(`/sources/${sourceId}/rules`, {
      data: { type: 'include', pattern: '**/*' },
    })
    expect(rule.ok()).toBeTruthy()

    // The explorer's download link on the folder.
    await page.goto(`/#/viewer/${sourceId}`)
    await expect(page.locator('.tree-body').getByRole('button', { name: 'zipme' })).toBeVisible({ timeout: 20_000 })
    const link = page.locator('.tree-body a.download[href*="download-zip"][href*="path=zipme"]')
    await expect(link).toHaveCount(1)
    const href = await link.getAttribute('href')
    expect(href).not.toBeNull()

    const original = await page.request.get(href as string)
    expect(original.ok()).toBeTruthy()
    expect(original.headers()['content-type']).toBe('application/zip')
    expect(original.headers()['content-disposition']).toContain('zipme.zip')
    expect(original.headers()['x-perchtail-redacted']).toBeUndefined()
    const members = unzip(await original.body())
    expect([...members.keys()]).toEqual(['1-app.log', '2-old.log.gz', '3-last.log'])
    expect(members.get('1-app.log')?.toString()).toBe(plain)
    expect(gunzipSync(members.get('2-old.log.gz') as Buffer).toString()).toBe(rotated)

    await setDetectors(page.request, turnedOn, true)
    await setRedaction(page.request, true)
    const redacted = await page.request.get(href as string)
    expect(redacted.ok()).toBeTruthy()
    expect(redacted.headers()['x-perchtail-redacted']).toBe('1')
    expect(redacted.headers()['content-disposition']).toContain('zipme.redacted.zip')
    const redactedMembers = unzip(await redacted.body())
    expect([...redactedMembers.keys()]).toEqual(['1-app.log', '2-old.log.gz', '3-last.log'])
    const app = redactedMembers.get('1-app.log')?.toString() ?? ''
    expect(app).toContain('user=[redacted: email]')
    expect(app).not.toContain('alice@example.com')
    const old = gunzipSync(redactedMembers.get('2-old.log.gz') as Buffer).toString()
    expect(old).toContain('user=[redacted: email]')
    expect(old).not.toContain('bob@example.org')
    expect(redactedMembers.get('3-last.log')?.toString()).toBe('last line\n')
  } finally {
    await setRedaction(page.request, false)
    await setDetectors(page.request, turnedOn, false)
    if (sourceId !== null) {
      const deleted = await page.request.delete(`/sources/${sourceId}`)
      expect(deleted.ok()).toBeTruthy()
    }
    rmSync(dir, { recursive: true, force: true })
  }
})
