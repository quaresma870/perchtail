import { expect, type APIRequestContext } from '@playwright/test'

// A WinRM source over the e2e backend's fake connector
// (backend/app/testing/fake_winrm.py), whose slow.log (about 9 MB) is read
// in 40 answers a quarter of a second apart: long enough to watch the
// progress of opening it whole and to cancel it.
const FIXTURE_ROOT = 'C:\\Logs\\e2e-fixture'
export const SLOW_FILE_NAME = 'slow.log'

export interface SlowSource {
  sourceId: number
  cleanUp: () => Promise<void>
}

export async function slowSource(request: APIRequestContext, name: string): Promise<SlowSource> {
  const created = await request.post('/sources', {
    data: {
      name,
      protocol: 'winrm',
      host: 'e2e-fake-winrm-host',
      base_path: FIXTURE_ROOT,
      credential: { username: 'svc', password: 'unused-fake-connector-ignores-this' },
    },
  })
  expect(created.ok()).toBeTruthy()
  const sourceId = (await created.json()).id as number
  const cleanUp = async () => {
    await request.delete(`/sources/${sourceId}`)
  }
  const rule = await request.post(`/sources/${sourceId}/rules`, { data: { type: 'include', pattern: '*.log' } })
  if (!rule.ok()) await cleanUp()
  expect(rule.ok()).toBeTruthy()
  return { sourceId, cleanUp }
}
