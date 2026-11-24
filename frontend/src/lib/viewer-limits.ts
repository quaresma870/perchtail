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

/** "Load earlier" stops once a tab opened at its end holds this much of
 * the file: past it, Load whole file. */
export const MAX_LOADED_EARLIER_BYTES = 32 * 1024 * 1024

const MB = 1024 * 1024

/** Whether a file opens at its end (its last TAIL_START_BYTES, through
 * Follow) rather than whole: a plain file whose listed size is over
 * `thresholdMb` (the `open_from_end_mb` System setting; 0 opens every file
 * whole). A .gz file or an archive member is decompressed on the server and
 * can't be read from an offset: it always opens whole. */
export function opensFromEnd(
  size: number | null | undefined,
  thresholdMb: number | null | undefined,
  name: string,
  member: string | null,
): boolean {
  if (typeof size !== 'number' || !Number.isFinite(size)) return false
  if (typeof thresholdMb !== 'number' || !Number.isFinite(thresholdMb) || thresholdMb <= 0) return false
  if (member !== null || name.toLowerCase().endsWith('.gz')) return false
  return size > thresholdMb * MB
}

/** The window "Load earlier" reads before `start` (the byte where what a
 * tab holds begins): at most TAIL_START_BYTES, never before 0. */
export function earlierWindow(start: number, bytes = TAIL_START_BYTES): { offset: number; end: number } {
  return { offset: Math.max(0, start - bytes), end: start }
}

/** Why "Load earlier" can't run for a tab now, or '' when it can: Tail
 * drops the oldest lines (TAIL_MAX_LINES), and what a tab loads this way
 * stops at MAX_LOADED_EARLIER_BYTES. Lines Tail already dropped can't be
 * placed either. */
export function loadEarlierBlocked(
  tab: { following: boolean; byteSize: number; startOffset?: number; droppedLines?: number },
  max = MAX_LOADED_EARLIER_BYTES,
): string {
  if (tab.following) return 'Stop Tail to load earlier lines'
  if ((tab.droppedLines ?? 0) > 0 || tab.byteSize - (tab.startOffset ?? 0) >= max) {
    return 'Load the whole file to see more'
  }
  return ''
}

/** Whether a tab holds only part of its file from the end: its start isn't
 * loaded, so line numbers aren't the file's. */
export function startNotLoaded(tab: { startOffset?: number } | null | undefined): boolean {
  return (tab?.startOffset ?? 0) > 0
}

/** Whether a tab's line numbers aren't the file's: its start isn't loaded,
 * or Tail dropped older lines from the top. Copy lines, Go to line and the
 * link to a line need them. */
export function lineNumbersNotFiles(
  tab: { startOffset?: number; droppedLines?: number } | null | undefined,
): boolean {
  return startNotLoaded(tab) || (tab?.droppedLines ?? 0) > 0
}

/** Moves bookmarks (1-indexed lines) down by the `added` lines put above
 * them. */
export function shiftBookmarksDown(bookmarks: number[], added: number): number[] {
  if (added <= 0) return bookmarks
  return bookmarks.map((line) => line + added)
}

/** Where what a window read from `offset` shows starts, when it isn't
 * redacted: past its first, cut line unless it starts the file. `bytes`
 * keeps everything when no line ends in it (a line longer than a window:
 * the next window, which ends where this one starts, holds its start). A
 * newline that is only the window's last byte ends that long line, not one
 * that starts in the window, so it is kept too. */
export function cutToFirstLine(bytes: Uint8Array, offset: number): { bytes: Uint8Array; start: number } {
  if (offset <= 0) return { bytes, start: 0 }
  const newline = bytes.indexOf(10)
  if (newline === -1 || newline === bytes.length - 1) return { bytes, start: offset }
  return { bytes: bytes.subarray(newline + 1), start: offset + newline + 1 }
}

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
