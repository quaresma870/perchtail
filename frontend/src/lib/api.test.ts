import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError } from './api'

function mockFetchOnce(response: Partial<Response> & { json?: () => Promise<unknown> }) {
  const fullResponse = {
    ok: response.ok ?? true,
    status: response.status ?? 200,
    statusText: response.statusText ?? '',
    json: response.json ?? (async () => ({})),
  } as Response
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(fullResponse))
  return fullResponse
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('api.get', () => {
  it('returns the parsed JSON body on success', async () => {
    mockFetchOnce({ ok: true, status: 200, json: async () => ({ id: 1, name: 'vodacom' }) })
    const result = await api.get('/customers/1')
    expect(result).toEqual({ id: 1, name: 'vodacom' })
  })

  it('sends credentials and a JSON content-type header', async () => {
    mockFetchOnce({ ok: true, status: 200, json: async () => ({}) })
    await api.get('/customers')
    const [, init] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(init.credentials).toBe('include')
    expect(init.headers['Content-Type']).toBe('application/json')
  })

  it('returns undefined for a 204 No Content response without reading a body', async () => {
    const bodyReader = vi.fn()
    mockFetchOnce({ ok: true, status: 204, json: bodyReader })
    const result = await api.get('/sources/1/close')
    expect(result).toBeUndefined()
    expect(bodyReader).not.toHaveBeenCalled()
  })

  it('throws an ApiError with the server-provided detail on a non-ok JSON response', async () => {
    mockFetchOnce({
      ok: false,
      status: 403,
      statusText: 'Forbidden',
      json: async () => ({ detail: "you don't have access to this source" }),
    })
    await expect(api.get('/sources/1')).rejects.toMatchObject({
      status: 403,
      detail: "you don't have access to this source",
    })
  })

  it('falls back to statusText when the error response has no JSON body', async () => {
    mockFetchOnce({
      ok: false,
      status: 502,
      statusText: 'Bad Gateway',
      json: async () => {
        throw new SyntaxError('not json')
      },
    })
    await expect(api.get('/sources/1')).rejects.toMatchObject({
      status: 502,
      detail: 'Bad Gateway',
    })
  })

  it('rejects with an actual ApiError instance', async () => {
    mockFetchOnce({ ok: false, status: 401, statusText: 'Unauthorized', json: async () => ({}) })
    await expect(api.get('/auth/me')).rejects.toBeInstanceOf(ApiError)
  })
})

describe('api.post', () => {
  it('JSON-encodes the body and sets method POST', async () => {
    mockFetchOnce({ ok: true, status: 200, json: async () => ({ id: 5 }) })
    await api.post('/sources', { name: 'db01' })
    const [, init] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(init.method).toBe('POST')
    expect(init.body).toBe(JSON.stringify({ name: 'db01' }))
  })

  it('omits the body entirely when none is given', async () => {
    mockFetchOnce({ ok: true, status: 200, json: async () => ({}) })
    await api.post('/auth/logout')
    const [, init] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(init.body).toBeUndefined()
  })
})

describe('api.delete', () => {
  it('sets method DELETE', async () => {
    mockFetchOnce({ ok: true, status: 204, json: async () => ({}) })
    await api.delete('/sources/1')
    const [, init] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(init.method).toBe('DELETE')
  })
})
