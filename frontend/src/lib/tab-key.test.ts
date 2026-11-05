import { describe, expect, it } from 'vitest'
import { memberPath, tabKey } from './tab-key'

describe('tabKey', () => {
  it('keys a plain file by its path with an empty member', () => {
    expect(tabKey(3, '/var/log/error.log', null)).toBe('3::/var/log/error.log::')
  })

  it('keys an archive member by the archive path plus the member name', () => {
    expect(tabKey(3, '/var/log/archive.zip', 'nested/error.log')).toBe(
      '3::/var/log/archive.zip::nested/error.log',
    )
  })

  it('never collides an archive-member key with the whole-archive key', () => {
    const wholeArchive = tabKey(3, '/var/log/archive.zip', null)
    const oneMember = tabKey(3, '/var/log/archive.zip', 'inner.log')
    expect(wholeArchive).not.toBe(oneMember)
  })
})

describe('tabKey across sources', () => {
  it('keeps the same path on two sources as two different tabs', () => {
    expect(tabKey(1, 'app.log', null)).not.toBe(tabKey(2, 'app.log', null))
  })
})

describe('memberPath', () => {
  it('strips the archive-root prefix and the separating slash', () => {
    expect(memberPath('/var/log/archive.zip/nested/error.log', '/var/log/archive.zip')).toBe(
      'nested/error.log',
    )
  })

  it('returns just the top-level member name for a non-nested member', () => {
    expect(memberPath('/var/log/archive.zip/error.log', '/var/log/archive.zip')).toBe('error.log')
  })

  it('is the exact inverse of concatenating archiveRoot + "/" + member', () => {
    const archiveRoot = '/logs/bundle.tar.gz'
    const member = 'app/error.log'
    const entryPath = `${archiveRoot}/${member}`
    expect(memberPath(entryPath, archiveRoot)).toBe(member)
  })
})
