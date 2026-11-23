/** Turns a failed open/reload/compare response into a message that says
 * *why* (#160) -- the backend already sends a reason (`detail`, a string or
 * a `{message, error_code, ...}` object), the viewer used to discard it and
 * show "Failed to open x". */

export interface FetchProblem {
  message: string
  /** Worth offering a "Try again" button: the cause may be transient. */
  retryable: boolean
  /** The file isn't on the source any more (#272) -- as opposed to hidden
   * by the rules, which looks the same as never having existed. */
  missing?: boolean
}

interface ErrorBody {
  detail?: string | { message?: string; error_code?: string }
  error_code?: string
}

/** The error code of an answer: next to `detail` (a SourceError, such as
 * agent_update_required) or inside it (an HTTPException with a
 * `{message, error_code}` detail). */
export function errorCode(body: ErrorBody | null): string | undefined {
  const detail = body?.detail
  return body?.error_code ?? (typeof detail === 'string' ? undefined : detail?.error_code)
}

// The 409s the server sends with an error code are refusals that asking
// again won't change: a disabled source (source_disabled), an agent to
// update (agent_update_required), an archive or .gz file that can't be read
// (archive_unreadable), an encrypted or unreadable archive member
// (archive_member_unreadable), content redaction can't read
// (redaction_unsupported_encoding, redaction_unsupported_format,
// redaction_unreadable), a file Tail can only reload whole
// (follow_unsupported). A 409 without one (the file is locked on the
// source) may pass on a later try.
const isPermanentConflict = (status: number, body: ErrorBody | null): boolean =>
  status === 409 && errorCode(body) !== undefined

export function describeFetchFailure(status: number, body: ErrorBody | null, name: string): FetchProblem {
  const detail = body?.detail
  const serverMessage = typeof detail === 'string' ? detail : detail?.message
  switch (status) {
    case 413:
      return { message: serverMessage ? `${name}: ${serverMessage}` : `${name} is too large to open.`, retryable: false }
    case 404:
      if (body?.error_code === 'not_found_on_source') {
        return { message: `${name} no longer exists on the source.`, retryable: false, missing: true }
      }
      return { message: `${name} wasn't found, or is hidden by this source's rules.`, retryable: false }
    case 403:
      return {
        message: serverMessage && serverMessage !== 'Forbidden'
          ? `${name}: ${serverMessage}`
          : `You don't have permission to view ${name}.`,
        retryable: false,
      }
    case 409:
      return {
        message: `${name}: ${serverMessage ?? 'the file is locked on the source.'}`,
        retryable: !isPermanentConflict(status, body),
      }
    case 503:
      // The server's limits (#344): busy, try again in a few seconds.
      return {
        message: `${name}: ${serverMessage ?? 'PerchTail is busy right now. Try again in a few seconds.'}`,
        retryable: true,
      }
    case 507:
      // Scratch or its disk is full (#345): retrying soon won't help.
      return {
        message: `${name}: ${serverMessage ?? "the server is out of space for this file right now."}`,
        retryable: false,
      }
    default:
      if (status >= 500) {
        return {
          message: `${name}: ${serverMessage ?? 'the source could not be reached.'}`,
          retryable: true,
        }
      }
      return { message: `${name}: ${serverMessage ?? `request failed (${status}).`}`, retryable: true }
  }
}

export async function readFetchFailure(response: Response, name: string): Promise<FetchProblem> {
  return describeFetchFailure(response.status, await readErrorBody(response), name)
}

export const FOLLOW_UNSUPPORTED = 'follow_unsupported'

export interface FollowFailure extends FetchProblem {
  /** The file can't be followed from an offset (a .gz file, an archive
   * member): Tail reloads it whole instead. */
  reloadWhole: boolean
  /** Polling again won't help: Tail stops and the message stays. */
  stopFollowing: boolean
}

/** What Tail does with a failed /follow answer: reload the whole file only
 * for follow_unsupported; otherwise show what the server says, and stop
 * for a refusal that won't change on the next poll. */
export function describeFollowFailure(status: number, body: ErrorBody | null, name: string): FollowFailure {
  if (status === 409 && errorCode(body) === FOLLOW_UNSUPPORTED) {
    return { ...describeFetchFailure(status, body, name), reloadWhole: true, stopFollowing: false }
  }
  return {
    ...describeFetchFailure(status, body, name),
    reloadWhole: false,
    stopFollowing: isPermanentConflict(status, body),
  }
}

/** The notice of a tab whose Tail stopped on a refusal (stopFollowing):
 * the server's reason, then that Tail stopped, as when the file is gone. */
export function tailStoppedNotice(problem: FetchProblem): FetchProblem {
  const reason = /[.!?]$/.test(problem.message) ? problem.message : `${problem.message}.`
  return { ...problem, message: `${reason} The tab keeps what it had; Tail stopped.` }
}

export async function readFollowFailure(response: Response, name: string): Promise<FollowFailure> {
  return describeFollowFailure(response.status, await readErrorBody(response), name)
}

async function readErrorBody(response: Response): Promise<ErrorBody | null> {
  try {
    return (await response.json()) as ErrorBody
  } catch {
    return null
  }
}
