import { describe, expect, it } from 'vitest'
import { downloadHref } from './download-href'

describe('downloadHref', () => {
  it('zips a plain directory', () => {
    const href = downloadHref(3, { path: '/var/log', is_dir: true, is_archive: false }, null)
    expect(href).toBe('/sources/3/download-zip?path=%2Fvar%2Flog')
  })

  it('downloads a plain file directly', () => {
    const href = downloadHref(3, { path: '/var/log/app.log', is_dir: false, is_archive: false }, null)
    expect(href).toBe('/sources/3/download?path=%2Fvar%2Flog%2Fapp.log')
  })

  it('downloads the archive itself when not inside one', () => {
    const href = downloadHref(3, { path: '/var/log/logs.zip', is_dir: false, is_archive: true }, null)
    expect(href).toBe('/sources/3/download?path=%2Fvar%2Flog%2Flogs.zip')
  })

  it('downloads a member from inside an archive, with the member param set', () => {
    const href = downloadHref(
      3,
      { path: '/var/log/logs.zip/nested/error.log', is_dir: false, is_archive: false },
      '/var/log/logs.zip',
    )
    const url = new URL(href!, 'http://x')
    expect(url.pathname).toBe('/sources/3/download')
    expect(url.searchParams.get('path')).toBe('/var/log/logs.zip')
    expect(url.searchParams.get('member')).toBe('nested/error.log')
  })

  it('returns null for a plain directory nested inside an archive (not expandable/downloadable)', () => {
    const href = downloadHref(
      3,
      { path: '/var/log/logs.zip/nested', is_dir: true, is_archive: false },
      '/var/log/logs.zip',
    )
    expect(href).toBeNull()
  })
})
