/** Right-click menus (#287): what each menu lists, worked out from what
 * applies where it was opened. ContextMenu.svelte only draws the list and
 * says which entry was chosen; the page that opened it runs the action.
 * An entry that doesn't apply isn't listed (there are no disabled ones). */

export interface MenuItem {
  id: string
  label: string
  /** Shown on the right, e.g. the keyboard shortcut that does the same. */
  shortcut?: string
}

export interface MenuSeparator {
  separator: true
}

export type MenuEntry = MenuItem | MenuSeparator

export const SEPARATOR: MenuSeparator = { separator: true }

export function isSeparator(entry: MenuEntry): entry is MenuSeparator {
  return 'separator' in entry
}

/** Drops separators that would sit first, last or next to another, which
 * entries left out can leave behind. */
export function tidy(entries: (MenuEntry | false | null | undefined)[]): MenuEntry[] {
  const out: MenuEntry[] = []
  for (const entry of entries) {
    if (!entry) continue
    if (isSeparator(entry) && (out.length === 0 || isSeparator(out[out.length - 1]))) continue
    out.push(entry)
  }
  while (out.length > 0 && isSeparator(out[out.length - 1])) out.pop()
  return out
}

export interface LogLineMenuContext {
  /** Some text is selected. */
  hasSelection: boolean
  /** The selection is on one line (and not empty). */
  singleLineSelection: boolean
  /** The file's start is loaded: its line numbers are the file's. */
  startLoaded: boolean
  /** The tab is a member of an archive. */
  archiveMember: boolean
  /** Older lines Tail dropped from the top: past 0, the line numbers
   * aren't the file's either. */
  droppedLines: number
}

/** What the editor asks a menu for (CodeMirrorPane's `menu` event): where
 * to open it, the line under the pointer (or the cursor's, from the
 * keyboard), the selected text, and where the focus goes back to. */
export interface LineMenuRequest {
  x: number
  y: number
  line: number
  selection: string
  opener: HTMLElement
}

/** The menu of a line in the viewer's editor. */
export function logLineMenu(ctx: LogLineMenuContext): MenuEntry[] {
  const selected = ctx.hasSelection && ctx.singleLineSelection
  const fileNumbers = ctx.startLoaded && ctx.droppedLines === 0
  return tidy([
    { id: 'copy', label: 'Copy', shortcut: 'Ctrl+C' },
    fileNumbers && { id: 'copy-with-numbers', label: 'Copy with line numbers' },
    SEPARATOR,
    selected && { id: 'find-all-selection', label: 'Find all of selection' },
    selected && { id: 'highlight-selection', label: 'Highlight selection' },
    { id: 'toggle-bookmark', label: 'Toggle bookmark' },
    SEPARATOR,
    fileNumbers && { id: 'go-to-line', label: 'Go to line…', shortcut: 'Ctrl+G' },
    fileNumbers && !ctx.archiveMember && { id: 'copy-link', label: 'Copy link to line' },
  ])
}

export interface TabMenuContext {
  /** The tab is the one shown. */
  active: boolean
  /** Tabs to its left and to its right. */
  tabsLeft: number
  tabsRight: number
  /** The user holds `download` on the tab's source. */
  canDownload: boolean
  /** The download is redacted (said in the label, as on the toolbar). */
  redactedDownload: boolean
}

/** The menu of a tab. */
export function tabMenu(ctx: TabMenuContext): MenuEntry[] {
  return tidy([
    { id: 'close', label: 'Close', shortcut: 'Middle-click' },
    ctx.tabsLeft + ctx.tabsRight > 0 && { id: 'close-others', label: 'Close others' },
    ctx.tabsLeft > 0 && { id: 'close-left', label: 'Close to the left' },
    ctx.tabsRight > 0 && { id: 'close-right', label: 'Close to the right' },
    { id: 'close-all', label: 'Close all' },
    SEPARATOR,
    { id: 'reload', label: 'Reload' },
    ctx.canDownload && { id: 'download', label: ctx.redactedDownload ? 'Download (redacted)' : 'Download' },
    !ctx.active && { id: 'compare', label: 'Compare with active tab' },
    { id: 'copy-path', label: 'Copy path' },
  ])
}

export interface FindAllMenuContext {
  /** Result rows selected. */
  selectedRows: number
  /** Result rows listed. */
  rows: number
}

/** The menu of Find all's results: the entries it always had, without
 * those that don't apply. */
export function findAllMenu(ctx: FindAllMenuContext): MenuEntry[] {
  return tidy([
    ctx.selectedRows > 0 && { id: 'copy', label: 'Copy selected lines' },
    ctx.selectedRows > 0 && { id: 'copy-with-numbers', label: 'Copy with line numbers' },
    { id: 'select-all', label: 'Select all' },
    SEPARATOR,
    ctx.rows > 0 && { id: 'refine', label: 'Search in these results…' },
    ctx.rows > 0 && { id: 'exclude', label: 'Exclude lines matching…' },
    SEPARATOR,
    ctx.selectedRows > 0 && { id: 'go-to-line', label: 'Go to line' },
    { id: 'close', label: 'Close' },
  ])
}

/** A tab's path as Copy path gives it: an archive member after its archive. */
export function tabPathText(path: string, member: string | null): string {
  return member ? `${path} › ${member}` : path
}

/** The address that opens a file of a source at a line (the viewer's deep
 * link, `#/viewer/<id>?path=…&line=…`), from the page's own address. */
export function lineLink(pageUrl: string, sourceId: number, path: string, line: number): string {
  const url = new URL(pageUrl)
  const query = new URLSearchParams({ path, line: String(line) })
  return `${url.origin}${url.pathname}#/viewer/${sourceId}?${query.toString()}`
}

/** Whether a key opens the menu of the focused element: the Menu key or
 * Shift+F10. */
export function isMenuKey(event: Pick<KeyboardEvent, 'key' | 'shiftKey' | 'ctrlKey' | 'altKey' | 'metaKey'>): boolean {
  if (event.ctrlKey || event.altKey || event.metaKey) return false
  return event.key === 'ContextMenu' || (event.shiftKey && event.key === 'F10')
}

/** How long after a key opened a menu the browser's own `contextmenu`
 * event for that key (which some browsers send) is ignored. */
export const KEY_MENU_GRACE_MS = 600

/** Decides what a `contextmenu` event is for: `browser` leaves the
 * browser's own menu (Shift+right-click), `ignore` swallows the event a
 * key that already opened our menu sends too, `ours` opens our menu. */
export function contextMenuIntent(
  event: Pick<MouseEvent, 'shiftKey'>,
  lastKeyOpen: number | null,
  now: number,
): 'ours' | 'browser' | 'ignore' {
  if (lastKeyOpen !== null && now - lastKeyOpen < KEY_MENU_GRACE_MS) return 'ignore'
  if (event.shiftKey) return 'browser'
  return 'ours'
}

/** Where a menu of `width` × `height` opened at (x, y) goes so it stays in a
 * window of `viewWidth` × `viewHeight`, `margin` from its edges. */
export function placeInWindow(
  x: number,
  y: number,
  width: number,
  height: number,
  viewWidth: number,
  viewHeight: number,
  margin = 4,
): { x: number; y: number } {
  const maxX = Math.max(margin, viewWidth - width - margin)
  const maxY = Math.max(margin, viewHeight - height - margin)
  return { x: Math.min(Math.max(x, margin), maxX), y: Math.min(Math.max(y, margin), maxY) }
}
