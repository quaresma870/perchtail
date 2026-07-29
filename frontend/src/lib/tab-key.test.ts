import { describe, expect, it } from 'vitest'
import { tabKey } from './tab-key'

describe('tabKey', () => {
  it('keys a plain file by its path with an empty member', () => {
    expect(tabKey('/var/log/error.log', null)).toBe('/var/log/error.log::')
  })

  it('keys an archive member by the archive path plus the member name', () => {
    expect(tabKey('/var/log/archive.zip', 'nested/error.log')).toBe(
      '/var/log/archive.zip::nested/error.log',
    )
  })

  it('never collides an archive-member key with the whole-archive key', () => {
    const wholeArchive = tabKey('/var/log/archive.zip', null)
    const oneMember = tabKey('/var/log/archive.zip', 'inner.log')
    expect(wholeArchive).not.toBe(oneMember)
  })
})
