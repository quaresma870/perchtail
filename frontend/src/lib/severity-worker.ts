import { matchLines } from './severity-matching'
import type { SeverityWorkerReply, SeverityWorkerRequest } from './severity-highlighting'

// The Web Worker the viewer's severity patterns run in (severity-matching.ts):
// a pattern whose regex never returns stops only this worker, which
// severity-highlighting.ts then replaces, never the page.
const scope = self as unknown as {
  location: { origin: string }
  onmessage: ((event: MessageEvent<SeverityWorkerRequest>) => void) | null
  postMessage(reply: SeverityWorkerReply): void
}

scope.onmessage = (event) => {
  // Only the page that created this dedicated worker can post to it, and
  // browsers give those messages an empty origin; anything else is ignored.
  if (event.origin !== '' && event.origin !== scope.location.origin) return
  const { id, lines, patterns } = event.data
  const result = matchLines(lines, patterns, (patternId) => {
    scope.postMessage({ id, type: 'start', patternId })
  })
  scope.postMessage({ id, type: 'done', ...result })
}
