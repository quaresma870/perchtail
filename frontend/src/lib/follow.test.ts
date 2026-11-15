import { describe, expect, it } from 'vitest'
import {
  appendReplacingPartial,
  countLines,
  decodeWithPartial,
  DEFAULT_TAIL_INTERVAL_SECONDS,
  followUrl,
  readFollowHeaders,
  readRedactionHeaders,
  tailIntervalMs,
} from './follow'

describe('readRedactionHeaders', () => {
  it('reads a redacted answer', () => {
    const headers = new Headers({
      'x-redacted': '1',
      'x-redaction-count': '7',
      'x-source-offset': '1200',
      'x-follow-partial': '27',
      'x-follow-aligned': '1',
    })
    expect(readRedactionHeaders(headers)).toEqual({
      redacted: true,
      count: 7,
      sourceOffset: 1200,
      partialBytes: 27,
      aligned: true,
    })
  })

  it('reads an answer that was not redacted', () => {
    expect(readRedactionHeaders(new Headers())).toEqual({
      redacted: false,
      count: 0,
      sourceOffset: null,
      partialBytes: 0,
      aligned: false,
    })
  })
})

describe('decodeWithPartial', () => {
  it('says how many characters the unfinished line takes', () => {
    const bytes = new TextEncoder().encode('é line\n[redacted: incomplete line]')
    const { text, partialChars } = decodeWithPartial(new TextDecoder(), bytes, 27)
    expect(text).toBe('é line\n[redacted: incomplete line]')
    expect(text.slice(text.length - partialChars)).toBe('[redacted: incomplete line]')
  })

  it('counts characters, not bytes', () => {
    const bytes = new TextEncoder().encode('a\nélan')
    expect(decodeWithPartial(new TextDecoder(), bytes, 5).partialChars).toBe(4)
  })

  it('takes nothing when there is no unfinished line', () => {
    const bytes = new TextEncoder().encode('a\n')
    expect(decodeWithPartial(new TextDecoder(), bytes, 0)).toEqual({ text: 'a\n', partialChars: 0 })
  })
})

describe('appendReplacingPartial', () => {
  it('replaces the unfinished line at the end of what was appended', () => {
    expect(appendReplacingPartial('a\n[partial]', 0, 9, 'b done\n')).toEqual({
      appended: 'a\nb done\n',
      dropFromContent: 0,
    })
  })

  it('drops it from the content when it came before this tick', () => {
    expect(appendReplacingPartial('', 0, 9, 'b done\n')).toEqual({
      appended: 'b done\n',
      dropFromContent: 9,
    })
  })
})

describe('followUrl', () => {
  it('asks from the offset, with the head once known', () => {
    expect(followUrl(3, 'app/x.log', 120, null, true)).toBe(
      '/sources/3/follow?path=app%2Fx.log&offset=120&start=true',
    )
    expect(followUrl(3, 'x.log', 150, 'abc', false)).toBe(
      '/sources/3/follow?path=x.log&offset=150&head=abc',
    )
  })
})

describe('readFollowHeaders', () => {
  it('reads an ordinary answer', () => {
    const headers = new Headers({
      'x-follow-offset': '200',
      'x-follow-head': 'h1',
      'x-follow-more': '1',
    })
    expect(readFollowHeaders(headers, 100)).toEqual({
      reset: false,
      offset: 200,
      head: 'h1',
      more: true,
    })
  })

  it('reads a reset, keeping the previous offset', () => {
    const headers = new Headers({ 'x-follow-reset': '1', 'x-file-size': '10' })
    expect(readFollowHeaders(headers, 100)).toEqual({
      reset: true,
      offset: 100,
      head: null,
      more: false,
    })
  })
})

describe('countLines', () => {
  it('counts appended lines, a partial last one included', () => {
    expect(countLines('')).toBe(0)
    expect(countLines('a\n')).toBe(1)
    expect(countLines('a\nb\n')).toBe(2)
    expect(countLines('a\nb')).toBe(2)
  })
})

describe('the Tail interval', () => {
  it('uses the System setting when it is one of the offered values', () => {
    expect(tailIntervalMs(1)).toBe(1000)
    expect(tailIntervalMs(10)).toBe(10000)
  })

  it('falls back to the default for anything else', () => {
    expect(tailIntervalMs(undefined)).toBe(DEFAULT_TAIL_INTERVAL_SECONDS * 1000)
    expect(tailIntervalMs(30)).toBe(DEFAULT_TAIL_INTERVAL_SECONDS * 1000)
    expect(tailIntervalMs(0)).toBe(DEFAULT_TAIL_INTERVAL_SECONDS * 1000)
  })
})
