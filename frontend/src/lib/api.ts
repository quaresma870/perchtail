export class ApiError extends Error {
  status: number
  detail: string
  // Set only for endpoints that send a structured `detail` object (e.g.
  // login's mfa_required/mfa_invalid_code) instead of the usual plain
  // string — callers that don't care can ignore it.
  errorCode?: string

  constructor(status: number, detail: string, errorCode?: string) {
    super(detail)
    this.status = status
    this.detail = detail
    this.errorCode = errorCode
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    credentials: 'include',
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
    ...init,
  })

  if (!response.ok) {
    let detail = response.statusText
    let errorCode: string | undefined
    try {
      const body = await response.json()
      if (body.detail && typeof body.detail === 'object') {
        detail = body.detail.message ?? detail
        errorCode = body.detail.error_code
      } else {
        detail = body.detail ?? detail
      }
    } catch {
      // no JSON body — keep the status text
    }
    throw new ApiError(response.status, detail, errorCode)
  }

  if (response.status === 204) {
    return undefined as T
  }
  return (await response.json()) as T
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'POST', body: body !== undefined ? JSON.stringify(body) : undefined }),
  patch: <T>(path: string, body: unknown) =>
    request<T>(path, { method: 'PATCH', body: JSON.stringify(body) }),
  put: <T>(path: string, body: unknown) =>
    request<T>(path, { method: 'PUT', body: JSON.stringify(body) }),
  delete: <T>(path: string) => request<T>(path, { method: 'DELETE' }),
}
