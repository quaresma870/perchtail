import { describe, expect, it } from 'vitest'
import { languageForFilename } from './file-language'

describe('languageForFilename', () => {
  it('maps .json to json', () => {
    expect(languageForFilename('config.json')).toBe('json')
  })

  it('maps xml-family extensions to xml', () => {
    expect(languageForFilename('data.xml')).toBe('xml')
    expect(languageForFilename('page.html')).toBe('xml')
    expect(languageForFilename('page.htm')).toBe('xml')
    expect(languageForFilename('icon.svg')).toBe('xml')
  })

  it('maps js-family extensions to javascript', () => {
    expect(languageForFilename('app.js')).toBe('javascript')
    expect(languageForFilename('app.mjs')).toBe('javascript')
    expect(languageForFilename('app.cjs')).toBe('javascript')
    expect(languageForFilename('component.jsx')).toBe('javascript')
    expect(languageForFilename('app.ts')).toBe('javascript')
    expect(languageForFilename('component.tsx')).toBe('javascript')
  })

  it('is case-insensitive on the extension', () => {
    expect(languageForFilename('CONFIG.JSON')).toBe('json')
  })

  it('returns null for a plain log file', () => {
    expect(languageForFilename('app.log')).toBeNull()
  })

  it('returns null for a file with no extension', () => {
    expect(languageForFilename('README')).toBeNull()
  })

  it('uses the last extension for a multi-dot filename', () => {
    expect(languageForFilename('app.log.2026-08-06.json')).toBe('json')
  })

  it('returns null for an unrecognized extension', () => {
    expect(languageForFilename('archive.tar.gz')).toBeNull()
  })
})
