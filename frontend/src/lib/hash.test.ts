import { describe, expect, it } from 'vitest'
import { get } from 'svelte/store'
import { currentHash } from './hash'

describe('currentHash', () => {
  it('reads the initial hash on module load', () => {
    // jsdom's default location is about:blank with no hash, and hash.ts
    // computes its initial value at import time — so with nothing set,
    // it must fall back to the root path rather than an empty string.
    expect(get(currentHash)).toBe('/')
  })

  it('updates when the hash changes', () => {
    window.location.hash = '#/sources'
    window.dispatchEvent(new HashChangeEvent('hashchange'))
    expect(get(currentHash)).toBe('/sources')
  })

  it('falls back to / when the hash is cleared to just "#"', () => {
    window.location.hash = '#/roles/3'
    window.dispatchEvent(new HashChangeEvent('hashchange'))
    expect(get(currentHash)).toBe('/roles/3')

    window.location.hash = ''
    window.dispatchEvent(new HashChangeEvent('hashchange'))
    expect(get(currentHash)).toBe('/')
  })
})
