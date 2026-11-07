// The advanced filter's recursive search (#284) lists folders live over
// SSH, SMB, WinRM or an agent. A limiter keeps one tick from opening many
// connections at once: at most PER_SOURCE listings at a time per source,
// and GLOBAL across all of them.

export const PER_SOURCE = 2
export const GLOBAL = 6

export interface Limiter {
  run<T>(key: number, task: () => Promise<T>): Promise<T>
}

export function createLimiter(perKey = PER_SOURCE, global = GLOBAL): Limiter {
  let running = 0
  const perKeyRunning = new Map<number, number>()
  const waiting: { key: number; start: () => void }[] = []

  const canStart = (key: number) => running < global && (perKeyRunning.get(key) ?? 0) < perKey

  function pump() {
    for (let i = 0; i < waiting.length; ) {
      const next = waiting[i]
      if (!canStart(next.key)) {
        i += 1
        continue
      }
      waiting.splice(i, 1)
      next.start()
    }
  }

  return {
    run<T>(key: number, task: () => Promise<T>): Promise<T> {
      return new Promise<T>((resolve, reject) => {
        const start = () => {
          running += 1
          perKeyRunning.set(key, (perKeyRunning.get(key) ?? 0) + 1)
          task()
            .then(resolve, reject)
            .finally(() => {
              running -= 1
              perKeyRunning.set(key, (perKeyRunning.get(key) ?? 1) - 1)
              pump()
            })
        }
        if (canStart(key)) start()
        else waiting.push({ key, start })
      })
    },
  }
}

/** Shared by every scope's search in the explorer. */
export const listingLimiter = createLimiter()
