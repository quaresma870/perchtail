import { describe, expect, it } from 'vitest'
import { darkTheme, severityHighlighting } from './codemirror-theme'

// The actual matching logic (which pattern matches which substring, glob vs
// regex, enabled/highlight_line handling) is pure and unit-tested
// independently in severity-highlighting.test.ts. This file only smoke-tests
// the CodeMirror glue, since exercising a ViewPlugin's decorations for real
// needs a live EditorView/DOM rather than something worth mocking here.

describe('darkTheme', () => {
  it('is a CodeMirror theme extension', () => {
    expect(darkTheme).toBeDefined()
  })
})

describe('severityHighlighting', () => {
  it('returns a line-tint plugin and a token-mark plugin', () => {
    const extensions = severityHighlighting([])
    expect(extensions).toHaveLength(2)
  })

  it('does not throw when given an empty or a populated pattern list', () => {
    expect(() => severityHighlighting([])).not.toThrow()
    expect(() =>
      severityHighlighting([
        {
          id: 1,
          source_id: null,
          level: 'error',
          pattern: 'error',
          pattern_kind: 'glob',
          enabled: true,
          highlight_line: true,
          include_in_navigation: true,
        },
      ]),
    ).not.toThrow()
  })
})
