// Bounds on what one browser tab holds (#347): open tabs, the lines Tail
// keeps, and a warning before loading a very large file whole.

/** The most files open at once in the viewer. */
export const MAX_OPEN_TABS = 30

/** Files above this size are opened only after a warning that offers
 * Tail (the end of the file) or Download instead. */
export const LARGE_FILE_BYTES = 200 * 1024 * 1024

/** The most lines a tab keeps while Tail appends: older lines are dropped
 * from the top, with a notice. */
export const TAIL_MAX_LINES = 200_000

/** How much of a large file's end "Tail" loads first: one follow answer
 * (RANGE_MAX_BYTES on the server). */
export const TAIL_START_BYTES = 1024 * 1024

export function tabLimitReached(openTabs: number, max = MAX_OPEN_TABS): boolean {
  return openTabs >= max
}

export function tabLimitMessage(max = MAX_OPEN_TABS): string {
  return `${max} files are open, the most the viewer keeps at once. Close a tab to open another.`
}

export function isLargeFile(size: number | null | undefined, threshold = LARGE_FILE_BYTES): boolean {
  return typeof size === 'number' && Number.isFinite(size) && size > threshold
}

export function countNewlines(text: string): number {
  let count = 0
  for (let i = text.indexOf('\n'); i !== -1; i = text.indexOf('\n', i + 1)) count++
  return count
}

export interface CappedBuffer {
  content: string
  /** Newlines left in `content`. */
  newlines: number
  /** Lines dropped from the top. */
  dropped: number
}

/** Keeps the last `maxLines` lines of `content` (which holds `newlines`
 * newlines), dropping whole lines from the top. */
export function capTailBuffer(content: string, newlines: number, maxLines = TAIL_MAX_LINES): CappedBuffer {
  if (newlines <= maxLines) return { content, newlines, dropped: 0 }
  const drop = newlines - maxLines
  let cut = -1
  for (let i = 0; i < drop; i++) {
    cut = content.indexOf('\n', cut + 1)
    if (cut === -1) return { content, newlines, dropped: 0 } // miscounted: keep it all
  }
  return { content: content.slice(cut + 1), newlines: maxLines, dropped: drop }
}

/** Bookmarks (1-indexed lines) once `dropped` lines left the top: the
 * ones that went are removed, the rest move up. */
export function shiftBookmarks(bookmarks: number[], dropped: number): number[] {
  if (dropped <= 0) return bookmarks
  return bookmarks.map((line) => line - dropped).filter((line) => line >= 1)
}

/** Where to start reading a file of `size` bytes to show its end. */
export function tailStartOffset(size: number, bytes = TAIL_START_BYTES): number {
  return Math.max(0, size - bytes)
}

/** Text read from `offset` bytes into a file starts mid-line (maybe mid
 * character) unless `offset` is 0: drop up to the first newline. */
export function dropPartialFirstLine(text: string, offset: number): string {
  if (offset <= 0) return text
  const newline = text.indexOf('\n')
  return newline === -1 ? text : text.slice(newline + 1)
}
