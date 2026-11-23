import { describe, expect, it } from 'vitest'
import { describeFetchFailure, describeFollowFailure, tailStoppedNotice } from './fetch-errors'

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

describe('describeFetchFailure for a 409', () => {
  it('offers to try again on a file locked on the source', () => {
    const problem = describeFetchFailure(409, { detail: 'The file is locked by another process on the source' }, 'app.log')
    expect(problem).toEqual({ message: 'app.log: The file is locked by another process on the source', retryable: true })
  })

  it.each([
    // A SourceError: the code is next to the detail.
    [{ detail: "This source's agent must be updated before its files can be read.", error_code: 'agent_update_required' }],
    // An HTTPException: the code is inside the detail.
    [{ detail: { message: 'This archive member is encrypted.', error_code: 'archive_member_unreadable' } }],
    [{ detail: { message: 'UTF-32', error_code: 'redaction_unsupported_encoding' } }],
    [{ detail: { message: 'compressed', error_code: 'redaction_unsupported_format' } }],
    [{ detail: { message: 'Compressed files and archives can only be reloaded whole', error_code: 'follow_unsupported' } }],
  ])('passes on a permanent refusal without a retry (%j)', (body) => {
    const problem = describeFetchFailure(409, body, 'app.log')
    expect(problem.retryable).toBe(false)
    const message = typeof body.detail === 'string' ? body.detail : body.detail.message
    expect(problem.message).toBe(`app.log: ${message}`)
  })
})

describe('describeFollowFailure', () => {
  it('reloads whole only a file that can only be followed by reloading it', () => {
    const body = { detail: { message: 'Compressed files and archives can only be reloaded whole', error_code: 'follow_unsupported' } }
    expect(describeFollowFailure(409, body, 'app.log.gz').reloadWhole).toBe(true)
  })

  it.each([
    [{ detail: "This source's agent must be updated before its files can be read.", error_code: 'agent_update_required' }],
    [{ detail: { message: 'UTF-32', error_code: 'redaction_unsupported_encoding' } }],
    [{ detail: { message: 'compressed', error_code: 'redaction_unsupported_format' } }],
    [{ detail: 'The file is locked by another process on the source' }],
  ])('shows the message of any other 409 (%j)', (body) => {
    const failure = describeFollowFailure(409, body, 'app.log')
    expect(failure.reloadWhole).toBe(false)
    const message = typeof body.detail === 'string' ? body.detail : body.detail.message
    expect(failure.message).toBe(`app.log: ${message}`)
    expect(failure.message).not.toContain('0.6')
  })

  it('stops Tail on a permanent refusal, not on one that may pass', () => {
    expect(describeFollowFailure(409, { detail: 'x', error_code: 'agent_update_required' }, 'a').stopFollowing).toBe(true)
    expect(describeFollowFailure(409, { detail: 'locked' }, 'a').stopFollowing).toBe(false)
    expect(describeFollowFailure(503, { detail: 'busy' }, 'a').stopFollowing).toBe(false)
  })

  it('stops Tail and offers no Try again for a disabled source', () => {
    const body = {
      detail: { message: 'This source is disabled. An admin can turn it back on in its settings.', error_code: 'source_disabled' },
    }
    const failure = describeFollowFailure(409, body, 'app.log')
    expect(failure.stopFollowing).toBe(true)
    expect(failure.retryable).toBe(false)
    expect(describeFetchFailure(409, body, 'app.log').retryable).toBe(false)
  })

  it('says Tail stopped after the reason', () => {
    const failure = describeFollowFailure(409, { detail: 'Agent too old.', error_code: 'agent_update_required' }, 'a')
    expect(tailStoppedNotice(failure).message).toBe('a: Agent too old. The tab keeps what it had; Tail stopped.')
    expect(tailStoppedNotice({ message: 'a: no stop', retryable: false }).message).toBe(
      'a: no stop. The tab keeps what it had; Tail stopped.',
    )
  })
})
