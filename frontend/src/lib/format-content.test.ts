import { describe, expect, it } from 'vitest'
import { canFormat, formatContent } from './format-content'

describe('canFormat', () => {
  it('supports json and xml', () => {
    expect(canFormat('json')).toBe(true)
    expect(canFormat('xml')).toBe(true)
  })

  it('does not support javascript or no language', () => {
    expect(canFormat('javascript')).toBe(false)
    expect(canFormat(null)).toBe(false)
  })
})

describe('formatContent — mode none', () => {
  it('returns the content unchanged regardless of language', () => {
    expect(formatContent('anything at all', null, 'none')).toBe('anything at all')
    expect(formatContent('{"a":1}', 'json', 'none')).toBe('{"a":1}')
  })
})

describe('formatContent — json', () => {
  it('beautifies compact JSON with 2-space indentation', () => {
    const result = formatContent('{"a":1,"b":[1,2]}', 'json', 'pretty')
    expect(result).toBe('{\n  "a": 1,\n  "b": [\n    1,\n    2\n  ]\n}')
  })

  it('minifies pretty-printed JSON', () => {
    const result = formatContent('{\n  "a": 1\n}', 'json', 'minified')
    expect(result).toBe('{"a":1}')
  })

  it('returns null for invalid/truncated JSON rather than throwing', () => {
    expect(formatContent('{"a": 1,', 'json', 'pretty')).toBeNull()
    expect(formatContent('{"a": 1,', 'json', 'minified')).toBeNull()
  })
})

describe('formatContent — xml', () => {
  it('beautifies a compact nested document with indentation by depth', () => {
    const input = '<root><a>1</a><b><c/></b></root>'
    const result = formatContent(input, 'xml', 'pretty')
    expect(result).toBe('<root>\n  <a>1</a>\n  <b>\n    <c/>\n  </b>\n</root>')
  })

  it('keeps an already-indented document\'s structure equivalent after round-tripping through minify then beautify', () => {
    const input = '<root>\n  <a attr="x">text</a>\n</root>'
    const minified = formatContent(input, 'xml', 'minified')
    expect(minified).toBe('<root><a attr="x">text</a></root>')
    const beautified = formatContent(minified as string, 'xml', 'pretty')
    expect(beautified).toBe('<root>\n  <a attr="x">text</a>\n</root>')
  })

  it('does not indent past a declaration or comment', () => {
    const input = '<?xml version="1.0"?><!-- note --><root><a/></root>'
    const result = formatContent(input, 'xml', 'pretty')
    expect(result).toBe('<?xml version="1.0"?>\n<!-- note -->\n<root>\n  <a/>\n</root>')
  })

  it('minifies by collapsing inter-tag whitespace only', () => {
    const input = '<root>\n  <a>1</a>\n  <b>2</b>\n</root>'
    expect(formatContent(input, 'xml', 'minified')).toBe('<root><a>1</a><b>2</b></root>')
  })

  it('returns null for malformed XML rather than throwing', () => {
    expect(formatContent('<root><a></root>', 'xml', 'pretty')).toBeNull()
    expect(formatContent('<root><a></root>', 'xml', 'minified')).toBeNull()
  })
})

describe('formatContent — unsupported language', () => {
  it('returns null for a non-formattable language', () => {
    expect(formatContent('const x = 1;', 'javascript', 'pretty')).toBeNull()
    expect(formatContent('plain log line', null, 'pretty')).toBeNull()
  })
})
