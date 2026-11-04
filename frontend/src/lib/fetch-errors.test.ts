import { describe, expect, it } from 'vitest'
import { describeFetchFailure } from './fetch-errors'

describe('describeFetchFailure', () => {
  it('passes the size/limit message through for a 413', () => {
    const problem = describeFetchFailure(
      413,
      { detail: { message: "This file is 100.0 MB, over this server's 64 MB limit (MAX_FETCH_MB).", error_code: 'fetch_too_large' } },
      '100mb.log',
    )
    expect(problem.message).toContain('100.0 MB')
    expect(problem.message).toContain('64 MB')
    expect(problem.retryable).toBe(false)
  })

  it('explains a 404 as missing or hidden by rules', () => {
    expect(describeFetchFailure(404, { detail: 'not found' }, 'x.log').message).toMatch(/hidden by this source's rules/)
  })

  it('offers retry for source-side failures', () => {
    const problem = describeFetchFailure(502, { detail: "Can't reach the source" }, 'x.log')
    expect(problem.retryable).toBe(true)
    expect(problem.message).toContain("Can't reach the source")
  })

  it('falls back to a generic message without a body', () => {
    expect(describeFetchFailure(500, null, 'x.log').message).toContain('could not be reached')
  })
})
