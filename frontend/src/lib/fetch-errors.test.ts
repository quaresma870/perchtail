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

  it('says a file that left the source no longer exists (#272)', () => {
    const problem = describeFetchFailure(
      404,
      { detail: 'Not found on the source', error_code: 'not_found_on_source' },
      'app.log',
    )
    expect(problem).toEqual({
      message: 'app.log no longer exists on the source.',
      retryable: false,
      missing: true,
    })
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

describe('describeFetchFailure for the server limits (#344, #345)', () => {
  it('says the server is busy and offers to try again on a 503', () => {
    const problem = describeFetchFailure(
      503,
      { detail: { message: 'This source is busy with other requests right now. Try again in a few seconds.', error_code: 'busy' } },
      'app.log',
    )
    expect(problem.message).toBe('app.log: This source is busy with other requests right now. Try again in a few seconds.')
    expect(problem.retryable).toBe(true)
    expect(describeFetchFailure(503, null, 'app.log').message).toMatch(/busy/)
  })

  it("passes on why there's no room on a 507, without a retry", () => {
    const problem = describeFetchFailure(
      507,
      { detail: { message: "The server's disk is almost full.", error_code: 'disk_low' } },
      'app.log',
    )
    expect(problem).toEqual({ message: "app.log: The server's disk is almost full.", retryable: false })
  })
})
