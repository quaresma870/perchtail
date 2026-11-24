import { describe, expect, it } from 'vitest'
import {
  newRequestId,
  progressLabel,
  progressValues,
  PROGRESS_DELAY_MS,
  showsProgress,
  withRequestId,
} from './open-progress'

const MB = 1024 * 1024

describe('the progress of an open', () => {
  it('says how much of how much was copied', () => {
    expect(progressLabel({ expected: 300 * MB, written: 128 * MB, phase: 'copying' })).toBe('128 MB of 300 MB')
  })

  it('says only how much when the size is unknown', () => {
    expect(progressLabel({ expected: null, written: 128 * MB, phase: 'copying' })).toBe('128 MB')
  })

  it('says Redacting… once redaction runs', () => {
    expect(progressLabel({ expected: 300 * MB, written: 300 * MB, phase: 'redacting' })).toBe('Redacting…')
  })

  it('says Starting… before the first answer', () => {
    expect(progressLabel(null)).toBe('Starting…')
  })

  it('gives the bar a value only while copying a file of known size', () => {
    expect(progressValues({ expected: 300, written: 128, phase: 'copying' })).toEqual({ now: 128, max: 300 })
    expect(progressValues({ expected: 300, written: 400, phase: 'copying' })).toEqual({ now: 300, max: 300 })
    expect(progressValues({ expected: 0, written: 0, phase: 'copying' })).toEqual({ now: 0, max: 1 })
    expect(progressValues({ expected: null, written: 128, phase: 'copying' })).toBeNull()
    expect(progressValues({ expected: 300, written: 300, phase: 'redacting' })).toBeNull()
    expect(progressValues(null)).toBeNull()
  })
})

describe('showing the bar', () => {
  it('shows nothing for an open that answers within the first second', () => {
    expect(PROGRESS_DELAY_MS).toBe(1000)
    expect(showsProgress(0)).toBe(false)
    expect(showsProgress(999)).toBe(false)
    expect(showsProgress(1000)).toBe(true)
  })
})

describe('the request id', () => {
  it('is 32 lowercase hex characters from 16 random bytes', () => {
    const id = newRequestId((bytes) => bytes.map((_, i) => (i * 17) % 256))
    expect(id).toBe('00112233445566778899aabbccddeeff')
    expect(newRequestId()).toMatch(/^[0-9a-f]{32}$/)
    expect(newRequestId()).not.toBe(newRequestId())
  })

  it('is added to the /open URL', () => {
    expect(withRequestId('/sources/1/open?path=a.log', 'ab'.repeat(16))).toBe(
      `/sources/1/open?path=a.log&request_id=${'ab'.repeat(16)}`,
    )
  })
})
