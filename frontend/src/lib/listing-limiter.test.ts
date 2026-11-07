import { describe, expect, it } from 'vitest'
import { createLimiter } from './listing-limiter'

function deferred() {
  let resolve!: () => void
  const promise = new Promise<void>((r) => (resolve = r))
  return { promise, resolve }
}

describe('createLimiter', () => {
  it('runs at most perKey tasks at once for one key, and global overall', async () => {
    const limiter = createLimiter(2, 3)
    const gates = Array.from({ length: 6 }, deferred)
    const started: string[] = []
    const keys = [1, 1, 1, 2, 2, 3]
    const done = keys.map((key, i) =>
      limiter.run(key, async () => {
        started.push(`${key}:${i}`)
        await gates[i].promise
        return i
      }),
    )
    await Promise.resolve()
    // Source 1 gets two, source 2 one more (global 3); the rest wait.
    expect(started).toEqual(['1:0', '1:1', '2:3'])

    gates[0].resolve()
    await done[0]
    await new Promise((r) => setTimeout(r, 0))
    // Source 1 is down to one running, so its third starts; 2:4 and 3:5
    // still wait for the global limit.
    expect(started).toEqual(['1:0', '1:1', '2:3', '1:2'])

    for (const gate of gates) gate.resolve()
    expect(await Promise.all(done)).toEqual([0, 1, 2, 3, 4, 5])
  })

  it('passes a failure through and frees the slot', async () => {
    const limiter = createLimiter(1, 1)
    await expect(limiter.run(1, () => Promise.reject(new Error('boom')))).rejects.toThrow('boom')
    expect(await limiter.run(1, async () => 'next')).toBe('next')
  })
})
