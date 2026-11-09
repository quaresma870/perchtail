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
      return { message: `${name}: ${serverMessage ?? 'the file is locked on the source.'}`, retryable: true }
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
  let body: ErrorBody | null = null
  try {
    body = (await response.json()) as ErrorBody
  } catch {
    body = null
  }
  return describeFetchFailure(response.status, body, name)
}
