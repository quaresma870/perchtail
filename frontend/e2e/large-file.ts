import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import path from 'node:path'
import { expect, type APIRequestContext } from '@playwright/test'

// A local-disk source under the e2e backend's LOCAL_SOURCE_ROOTS
// (backend/scripts/run_e2e_server.sh) holding one file of about 3 MB, with
// the `open_from_end_mb` System setting lowered to 1 so it opens at its
// end. `cleanUp` puts the setting back and removes the source and folder.
const fixtureRoot = '/tmp/perchtail-e2e-fixtures'

export const LARGE_FILE_NAME = 'big.log'
export const LARGE_FILE_LINES = 40_000

/** Line `n` (1-based) of the file: 80 bytes with its newline. */
export function largeFileLine(n: number): string {
  return `line ${String(n).padStart(6, '0')} ${'.'.repeat(67)}`
}

export interface LargeFileSource {
  sourceId: number
  cleanUp: () => Promise<void>
}

export async function largeFileSource(request: APIRequestContext, name: string): Promise<LargeFileSource> {
  mkdirSync(fixtureRoot, { recursive: true })
  const folder = mkdtempSync(path.join(fixtureRoot, 'large-'))
  const lines: string[] = []
  for (let n = 1; n <= LARGE_FILE_LINES; n++) lines.push(largeFileLine(n))
  writeFileSync(path.join(folder, LARGE_FILE_NAME), lines.join('\n') + '\n')

  const before = (await (await request.get('/system-settings')).json()).open_from_end_mb as number
  let sourceId: number | null = null
  const cleanUp = async () => {
    await request.patch('/system-settings', { data: { open_from_end_mb: before } })
    if (sourceId !== null) await request.delete(`/sources/${sourceId}`)
    rmSync(folder, { recursive: true, force: true })
  }
  try {
    const lowered = await request.patch('/system-settings', { data: { open_from_end_mb: 1 } })
    expect(lowered.ok()).toBeTruthy()
    // The path as the backend sees it, whatever the separator here.
    const basePath = `${fixtureRoot}/${path.basename(folder)}`
    const created = await request.post('/sources', {
      data: { name, protocol: 'local', host: 'localhost', base_path: basePath },
    })
    expect(created.ok()).toBeTruthy()
    sourceId = (await created.json()).id as number
    const rule = await request.post(`/sources/${sourceId}/rules`, { data: { type: 'include', pattern: '*.log' } })
    expect(rule.ok()).toBeTruthy()
    return { sourceId, cleanUp }
  } catch (error) {
    await cleanUp()
    throw error
  }
}
