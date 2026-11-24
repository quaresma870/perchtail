import { describe, expect, it } from 'vitest'
import { NO_PATTERNS } from './editor-states'
import { planSync, type EditorInputs } from './editor-sync'
import type { SeverityPattern } from './types'

const pattern = { id: 1, pattern: 'ERROR', enabled: true } as unknown as SeverityPattern

function inputs(over: Partial<EditorInputs> = {}): EditorInputs {
  return {
    content: 'one\ntwo\n',
    format: 'none',
    severityPatterns: NO_PATTERNS,
    filename: 'app.log',
    wrapEnabled: false,
    showWhitespace: false,
    bookmarks: [],
    markPatterns: [],
    ...over,
  }
}

describe('planSync', () => {
  it('rebuilds when nothing is shown yet', () => {
    expect(planSync(null, inputs())).toEqual({ action: 'rebuild', reconfigure: [] })
  })

  it('does nothing when nothing changed', () => {
    const shown = inputs()
    expect(planSync(shown, { ...shown })).toEqual({ action: 'none', reconfigure: [] })
  })

  it('reconfigures only severity when the same text gets its patterns', () => {
    const shown = inputs()
    expect(planSync(shown, { ...shown, severityPatterns: [pattern] })).toEqual({
      action: 'reconfigure',
      reconfigure: ['severity'],
    })
  })

  it('never rebuilds for a source with no patterns', () => {
    const shown = inputs({ severityPatterns: NO_PATTERNS })
    expect(planSync(shown, { ...shown, severityPatterns: NO_PATTERNS }).action).toBe('none')
    expect(planSync(shown, { ...shown, severityPatterns: [pattern] }).action).toBe('reconfigure')
    expect(planSync(inputs({ severityPatterns: [pattern] }), shown).action).toBe('reconfigure')
  })

  it('appends text added at the end', () => {
    const shown = inputs()
    expect(planSync(shown, { ...shown, content: 'one\ntwo\nthree\n' })).toEqual({
      action: 'append',
      reconfigure: [],
    })
  })

  it('appends and reconfigures what changed with it', () => {
    const shown = inputs({ showWhitespace: true })
    const bookmarks = [2]
    expect(planSync(shown, { ...shown, content: 'one\ntwo\nthree\n', bookmarks })).toEqual({
      action: 'append',
      reconfigure: ['whitespace', 'bookmarks'],
    })
  })

  it('rebuilds when the text changed other than at the end', () => {
    const shown = inputs()
    expect(planSync(shown, { ...shown, content: 'two\nthree\n' }).action).toBe('rebuild')
    expect(planSync(shown, { ...shown, content: 'one\n' }).action).toBe('rebuild')
    expect(planSync(shown, { ...shown, content: 'ONE\ntwo\nthree\n' }).action).toBe('rebuild')
  })

  it('rebuilds when the format changed', () => {
    const shown = inputs()
    expect(planSync(shown, { ...shown, format: 'pretty' }).action).toBe('rebuild')
  })

  it('reconfigures each toggle on its own', () => {
    const shown = inputs()
    expect(
      planSync(shown, {
        ...shown,
        filename: 'app.json',
        wrapEnabled: true,
        showWhitespace: true,
        bookmarks: [1],
        markPatterns: [],
      }),
    ).toEqual({ action: 'reconfigure', reconfigure: ['language', 'wrap', 'whitespace', 'bookmarks', 'marks'] })
  })
})

describe('planSync with text added at the start ("Load earlier")', () => {
  it('prepends text that ends with the text shown', () => {
    const shown = inputs()
    expect(planSync(shown, { ...shown, content: 'zero\none\ntwo\n' })).toEqual({
      action: 'prepend',
      reconfigure: [],
    })
  })

  it('prepends and reconfigures what changed with it', () => {
    const shown = inputs({ showWhitespace: true })
    const bookmarks = [3]
    expect(planSync(shown, { ...shown, content: 'zero\none\ntwo\n', bookmarks })).toEqual({
      action: 'prepend',
      reconfigure: ['whitespace', 'bookmarks'],
    })
  })

  it('appends rather than prepends when the text was added at the end', () => {
    // 'a\na\n' -> 'a\na\na\n' both starts and ends with the old text.
    const shown = inputs({ content: 'a\na\n' })
    expect(planSync(shown, { ...shown, content: 'a\na\na\n' }).action).toBe('append')
  })

  it('rebuilds when the text changed at both ends or in the middle', () => {
    const shown = inputs()
    expect(planSync(shown, { ...shown, content: 'zero\none\ntwo\nthree\n' }).action).toBe('rebuild')
    expect(planSync(shown, { ...shown, content: 'one\nand\ntwo\n' }).action).toBe('rebuild')
  })

  it('rebuilds a prepend when the format changed too', () => {
    const shown = inputs()
    expect(planSync(shown, { ...shown, content: 'zero\none\ntwo\n', format: 'pretty' }).action).toBe('rebuild')
  })
})
