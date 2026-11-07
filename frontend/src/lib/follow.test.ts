import { describe, expect, it } from 'vitest'
import { countLines, DEFAULT_TAIL_INTERVAL_SECONDS, followUrl, readFollowHeaders, tailIntervalMs } from './follow'

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
