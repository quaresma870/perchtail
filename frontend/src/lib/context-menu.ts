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

export interface FileMenuContext {
  /** A member of an archive (opened from inside it). */
  archiveMember: boolean
  /** A plain .gz file, decompressed on the server: no Tail, no reading
   * from its end. */
  gzip: boolean
  /** A tab is shown, and it isn't this file. */
  otherTabActive: boolean
  /** The user holds `download` on the source. */
  canDownload: boolean
}

/** The menu of a file in the explorer. */
export function fileMenu(ctx: FileMenuContext): MenuEntry[] {
  const fromOffset = !ctx.archiveMember && !ctx.gzip
  return tidy([
    { id: 'open', label: 'Open' },
    fromOffset && { id: 'open-tail', label: 'Open with Tail' },
    fromOffset && { id: 'open-end', label: 'Open the end' },
    ctx.otherTabActive && { id: 'compare', label: 'Compare with active tab' },
    SEPARATOR,
    ctx.canDownload && { id: 'download', label: 'Download' },
    { id: 'copy-path', label: 'Copy path' },
    !ctx.archiveMember && { id: 'copy-link', label: 'Copy link' },
  ])
}

export interface FolderMenuContext {
  /** A folder inside an archive (listed from the archive's members). */
  inArchive: boolean
  /** The node is an archive, shown as a folder of its members. */
  archive: boolean
  /** Expanded, with its listing on screen (what Refresh lists again). */
  listed: boolean
  /** Expanded (what Collapse all closes). */
  expanded: boolean
  /** The user holds `download` on the source. */
  canDownload: boolean
}

/** The menu of a folder in the explorer (a source's root folders too), of a
 * folder inside an archive, and of an archive shown as a folder. */
export function folderMenu(ctx: FolderMenuContext): MenuEntry[] {
  const plainFolder = !ctx.inArchive && !ctx.archive
  return tidy([
    ctx.listed && { id: 'refresh', label: 'Refresh' },
    plainFolder && { id: 'search-here', label: 'Search here' },
    ctx.expanded && { id: 'collapse-all', label: 'Collapse all' },
    SEPARATOR,
    plainFolder && ctx.canDownload && { id: 'download', label: 'Download as ZIP' },
    ctx.archive && !ctx.inArchive && ctx.canDownload && { id: 'download', label: 'Download' },
    { id: 'copy-path', label: 'Copy path' },
  ])
}

export interface SourceMenuContext {
  /** Expanded, with its listing on screen. */
  listed: boolean
  expanded: boolean
  /** The system log source. */
  system: boolean
  /** The user holds `manage_source` on it (the connection check). */
  canManageSource: boolean
  /** The user holds `run_now` on it and its file-name index is on. */
  canIndexNow: boolean
  /** The user holds `manage_rules` on it. */
  canManageRules: boolean
}

/** The menu of a source in the explorer. */
export function sourceMenu(ctx: SourceMenuContext): MenuEntry[] {
  return tidy([
    ctx.listed && { id: 'refresh', label: 'Refresh' },
    { id: 'search-here', label: 'Search here' },
    ctx.expanded && { id: 'collapse-all', label: 'Collapse all' },
    SEPARATOR,
    ctx.canManageSource && { id: 'check-now', label: 'Check now' },
    ctx.canIndexNow && { id: 'index-now', label: 'Index file names now' },
    !ctx.system && { id: 'copy-host', label: 'Copy host' },
    SEPARATOR,
    ctx.canManageSource && !ctx.system && { id: 'edit-source', label: 'Edit source' },
    ctx.canManageRules && !ctx.system && { id: 'edit-rules', label: 'Edit rules' },
  ])
}

export interface GroupMenuContext {
  expanded: boolean
  /** Sources under the group on which the user holds `manage_source`. */
  checkableSources: number
}

/** The menu of a customer or folder of sources in the tree view. */
export function groupMenu(ctx: GroupMenuContext): MenuEntry[] {
  return tidy([
    { id: 'search-here', label: 'Search here' },
    ctx.checkableSources > 0 && { id: 'check-all', label: 'Check all now' },
    { id: 'expand-all', label: 'Expand all' },
    ctx.expanded && { id: 'collapse-all', label: 'Collapse all' },
  ])
}

/** The ids of a group and of every group below it, and of every source
 * below it, at any depth. */
export function groupContents(node: GroupNode): { groupIds: string[]; sources: GroupNode['sources'] } {
  const groupIds: string[] = [node.id]
  const sources: GroupNode['sources'] = [...node.sources]
  for (const child of node.children) {
    const inner = groupContents(child)
    groupIds.push(...inner.groupIds)
    sources.push(...inner.sources)
  }
  return { groupIds, sources }
}

/** What groupContents needs of a connections-tree node. */
export interface GroupNode {
  id: string
  children: GroupNode[]
  sources: { id: number; enabled?: boolean; my_capabilities?: string[] }[]
}

/** The sources Check all now checks: those on which the user holds
 * `manage_source`, leaving out the disabled ones (the server refuses to
 * check them). */
export function checkableSourceIds(sources: GroupNode['sources']): number[] {
  return sources
    .filter((s) => s.enabled !== false && s.my_capabilities?.includes('manage_source'))
    .map((s) => s.id)
}

/** How many connection checks Check all now runs at once. */
export const CHECK_ALL_CONCURRENCY = 3

/** Runs the jobs, at most `limit` at once, each starting when one ends; a
 * failure doesn't stop the others. The results are in the jobs' order. */
export async function runLimited<T>(
  jobs: (() => Promise<T>)[],
  limit: number,
): Promise<PromiseSettledResult<T>[]> {
  const results: PromiseSettledResult<T>[] = new Array(jobs.length)
  let next = 0
  async function worker() {
    while (next < jobs.length) {
      const i = next++
      try {
        results[i] = { status: 'fulfilled', value: await jobs[i]() }
      } catch (reason) {
        results[i] = { status: 'rejected', reason }
      }
    }
  }
  await Promise.all(Array.from({ length: Math.min(limit, jobs.length) }, worker))
  return results
}

/** Whether a right-click landed inside a link (the download icon and other
 * links on a node): there the browser keeps its own menu. */
export function insideLink(target: EventTarget | null): boolean {
  const closest = (target as { closest?: unknown } | null)?.closest
  return typeof closest === 'function' && (target as Element).closest('a[href]') !== null
}

/** Which keys of the explorer's expanded set (lib/viewer-tabs.ts,
 * `explorerExpanded`) Collapse all drops: a folder's own key and those of
 * everything below it, archives under it included. Folder keys are
 * `dir:<source id>:<archive path, or empty>:<path>`. */
export function folderExpandKeys(
  keys: Iterable<string>,
  sourceId: number,
  archiveRoot: string | null,
  path: string,
): string[] {
  const own = `dir:${sourceId}:${archiveRoot ?? ''}:${path}`
  // Outside an archive, the node may be an archive itself, or a folder with
  // archives below it: their members' keys start with the archive's path.
  const members = archiveRoot === null ? [`dir:${sourceId}:${path}:`, `dir:${sourceId}:${path}/`] : []
  return [...keys].filter(
    (key) => key === own || key.startsWith(`${own}/`) || members.some((prefix) => key.startsWith(prefix)),
  )
}

/** The keys Collapse all drops for a source: its own and every folder's. */
export function sourceExpandKeys(keys: Iterable<string>, sourceId: number): string[] {
  return [...keys].filter((key) => key === `source:${sourceId}` || key.startsWith(`dir:${sourceId}:`))
}

/** The keys Collapse all drops for a group: its own, every group's below it
 * and those of every source below it. */
export function groupExpandKeys(keys: Iterable<string>, groupIds: string[], sourceIds: number[]): string[] {
  const groups = new Set(groupIds.map((id) => `tree:${id}`))
  const all = [...keys]
  return [
    ...all.filter((key) => groups.has(key)),
    ...sourceIds.flatMap((id) => sourceExpandKeys(all, id)),
  ]
}

/** The address that opens a file of a source in the viewer
 * (`#/viewer/<id>?path=…`), from the page's own address. */
export function fileLink(pageUrl: string, sourceId: number, path: string): string {
  const url = new URL(pageUrl)
  return `${url.origin}${url.pathname}#/viewer/${sourceId}?${new URLSearchParams({ path }).toString()}`
}

/** What a node of the explorer asks for when its menu opens: the menu's
 * name, its entries and what to do with the one chosen. */
export interface ExplorerMenu {
  label: string
  entries: MenuEntry[]
  run: (id: string) => void
}

/** Given by the Viewer to the explorer's nodes (Svelte context, under
 * EXPLORER_MENUS): opens a node's menu, and does what the explorer can't
 * do on its own. */
export interface ExplorerMenus {
  /** A right-click on a node; `opener` gets the focus back. */
  mouse: (event: MouseEvent, opener: HTMLElement, build: () => ExplorerMenu) => void
  /** A key on a node's button: the Menu key or Shift+F10 opens its menu. */
  key: (event: KeyboardEvent, build: () => ExplorerMenu) => void
  /** Search here: the explorer's search limited to this scope. */
  searchHere: (scope: { sourceId: number; path: string } | { groupId: string; name: string }) => void
  /** Copies text, saying in the explorer whether it worked. */
  copy: (text: string) => void
  /** Check all now: the connection check of these sources. */
  checkSources: (sourceIds: number[]) => void
}

export const EXPLORER_MENUS = Symbol('explorer-menus')

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
