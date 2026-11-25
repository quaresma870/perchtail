import { describe, expect, it } from 'vitest'
import {
  CHECK_ALL_CONCURRENCY,
  checkableSourceIds,
  insideLink,
  runLimited,
  contextMenuIntent,
  fileLink,
  fileMenu,
  findAllMenu,
  folderExpandKeys,
  folderMenu,
  groupContents,
  groupExpandKeys,
  groupMenu,
  sourceExpandKeys,
  sourceMenu,
  type FileMenuContext,
  type FolderMenuContext,
  type GroupNode,
  type SourceMenuContext,
  isMenuKey,
  isSeparator,
  KEY_MENU_GRACE_MS,
  lineLink,
  logLineMenu,
  placeInWindow,
  SEPARATOR,
  tabMenu,
  tabPathText,
  tidy,
  type LogLineMenuContext,
  type MenuEntry,
  type TabMenuContext,
} from './context-menu'

/** The ids, with `-` for a separator. */
const ids = (entries: MenuEntry[]) => entries.map((e) => (isSeparator(e) ? '-' : e.id))

const line = (over: Partial<LogLineMenuContext> = {}): LogLineMenuContext => ({
  hasSelection: false,
  singleLineSelection: false,
  startLoaded: true,
  archiveMember: false,
  droppedLines: 0,
  ...over,
})

const tab = (over: Partial<TabMenuContext> = {}): TabMenuContext => ({
  active: true,
  tabsLeft: 0,
  tabsRight: 0,
  canDownload: false,
  redactedDownload: false,
  ...over,
})

describe('tidy', () => {
  it('drops entries left out and separators first, last or doubled', () => {
    expect(ids(tidy([SEPARATOR, { id: 'a', label: 'A' }, false, SEPARATOR, null, SEPARATOR, { id: 'b', label: 'B' }, SEPARATOR]))).toEqual([
      'a',
      '-',
      'b',
    ])
  })
})

describe('logLineMenu', () => {
  it('without a selection: copy, bookmark, go to line and link', () => {
    expect(ids(logLineMenu(line()))).toEqual([
      'copy',
      'copy-with-numbers',
      '-',
      'toggle-bookmark',
      '-',
      'go-to-line',
      'copy-link',
    ])
  })

  it('offers Find all and Highlight for a selection on one line', () => {
    const entries = ids(logLineMenu(line({ hasSelection: true, singleLineSelection: true })))
    expect(entries).toContain('find-all-selection')
    expect(entries).toContain('highlight-selection')
  })

  it('leaves them out for a selection over several lines', () => {
    const entries = ids(logLineMenu(line({ hasSelection: true, singleLineSelection: false })))
    expect(entries).not.toContain('find-all-selection')
    expect(entries).not.toContain('highlight-selection')
  })

  it('needs the file start for line numbers, Go to line and the link', () => {
    expect(ids(logLineMenu(line({ startLoaded: false })))).toEqual(['copy', '-', 'toggle-bookmark'])
  })

  it("leaves them out once Tail dropped older lines, as the numbers aren't the file's", () => {
    expect(ids(logLineMenu(line({ droppedLines: 3 })))).toEqual(['copy', '-', 'toggle-bookmark'])
    expect(ids(logLineMenu(line({ droppedLines: 3, hasSelection: true, singleLineSelection: true })))).toEqual([
      'copy',
      '-',
      'find-all-selection',
      'highlight-selection',
      'toggle-bookmark',
    ])
  })

  it('has no link to a line of an archive member', () => {
    expect(ids(logLineMenu(line({ archiveMember: true })))).not.toContain('copy-link')
    expect(ids(logLineMenu(line({ archiveMember: true })))).toContain('go-to-line')
  })

  it('shows the shortcuts of Copy and Go to line', () => {
    const entries = logLineMenu(line()).filter((e) => !isSeparator(e)) as { id: string; shortcut?: string }[]
    expect(entries.find((e) => e.id === 'copy')?.shortcut).toBe('Ctrl+C')
    expect(entries.find((e) => e.id === 'go-to-line')?.shortcut).toBe('Ctrl+G')
  })
})

describe('tabMenu', () => {
  it('a single active tab: close, close all, reload and copy path', () => {
    expect(ids(tabMenu(tab()))).toEqual(['close', 'close-all', '-', 'reload', 'copy-path'])
  })

  it('offers closing to each side only with tabs there, and the others with any', () => {
    expect(ids(tabMenu(tab({ tabsLeft: 1 })))).toEqual(
      expect.arrayContaining(['close-others', 'close-left']),
    )
    expect(ids(tabMenu(tab({ tabsLeft: 1 })))).not.toContain('close-right')
    expect(ids(tabMenu(tab({ tabsRight: 2 })))).toEqual(
      expect.arrayContaining(['close-others', 'close-right']),
    )
    expect(ids(tabMenu(tab({ tabsRight: 2 })))).not.toContain('close-left')
  })

  it('offers Download only with the download capability, labelled as on the toolbar', () => {
    expect(ids(tabMenu(tab()))).not.toContain('download')
    const plain = tabMenu(tab({ canDownload: true })).find((e) => !isSeparator(e) && e.id === 'download')
    expect(plain).toMatchObject({ label: 'Download' })
    const redacted = tabMenu(tab({ canDownload: true, redactedDownload: true })).find(
      (e) => !isSeparator(e) && e.id === 'download',
    )
    expect(redacted).toMatchObject({ label: 'Download (redacted)' })
  })

  it('offers Compare with active tab only on another tab', () => {
    expect(ids(tabMenu(tab({ active: true })))).not.toContain('compare')
    expect(ids(tabMenu(tab({ active: false })))).toContain('compare')
  })

  it('says a middle-click closes', () => {
    expect(tabMenu(tab())[0]).toMatchObject({ id: 'close', shortcut: 'Middle-click' })
  })
})

describe('findAllMenu', () => {
  it('lists every entry with rows selected', () => {
    expect(ids(findAllMenu({ selectedRows: 2, rows: 5 }))).toEqual([
      'copy',
      'copy-with-numbers',
      'select-all',
      '-',
      'refine',
      'exclude',
      '-',
      'go-to-line',
      'close',
    ])
  })

  it('leaves out what needs a selection or results', () => {
    expect(ids(findAllMenu({ selectedRows: 0, rows: 5 }))).toEqual(['select-all', '-', 'refine', 'exclude', '-', 'close'])
    expect(ids(findAllMenu({ selectedRows: 0, rows: 0 }))).toEqual(['select-all', '-', 'close'])
  })
})

describe('tabPathText', () => {
  it('gives the path, or the archive then the member', () => {
    expect(tabPathText('/var/log/app.log', null)).toBe('/var/log/app.log')
    expect(tabPathText('logs/old.zip', 'app/2024.log')).toBe('logs/old.zip › app/2024.log')
  })
})

describe('lineLink', () => {
  it('builds the viewer deep link from the page address', () => {
    expect(lineLink('https://perchtail.example/app/#/viewer/3?x=1', 7, 'logs/a b.log', 42)).toBe(
      'https://perchtail.example/app/#/viewer/7?path=logs%2Fa+b.log&line=42',
    )
  })
})

describe('isMenuKey', () => {
  const key = (k: string, mods: Partial<KeyboardEvent> = {}) => ({
    key: k,
    shiftKey: false,
    ctrlKey: false,
    altKey: false,
    metaKey: false,
    ...mods,
  })

  it('is the Menu key or Shift+F10', () => {
    expect(isMenuKey(key('ContextMenu'))).toBe(true)
    expect(isMenuKey(key('F10', { shiftKey: true }))).toBe(true)
    expect(isMenuKey(key('F10'))).toBe(false)
    expect(isMenuKey(key('F10', { shiftKey: true, ctrlKey: true }))).toBe(false)
    expect(isMenuKey(key('a', { shiftKey: true }))).toBe(false)
  })
})

describe('contextMenuIntent', () => {
  it('opens ours on a right-click', () => {
    expect(contextMenuIntent({ shiftKey: false }, null, 1000)).toBe('ours')
  })

  it('leaves the browser its menu on Shift+right-click', () => {
    expect(contextMenuIntent({ shiftKey: true }, null, 1000)).toBe('browser')
  })

  it("ignores the browser's event for a key that already opened ours", () => {
    expect(contextMenuIntent({ shiftKey: true }, 1000, 1000 + KEY_MENU_GRACE_MS - 1)).toBe('ignore')
    expect(contextMenuIntent({ shiftKey: false }, 1000, 1000 + KEY_MENU_GRACE_MS)).toBe('ours')
  })
})

describe('placeInWindow', () => {
  it('keeps the place when the menu fits', () => {
    expect(placeInWindow(100, 50, 200, 150, 1000, 800)).toEqual({ x: 100, y: 50 })
  })

  it('moves it back inside at the right and bottom edges', () => {
    expect(placeInWindow(950, 780, 200, 150, 1000, 800)).toEqual({ x: 796, y: 646 })
  })

  it('never above or left of the margin', () => {
    expect(placeInWindow(-10, -5, 200, 150, 1000, 800)).toEqual({ x: 4, y: 4 })
    expect(placeInWindow(50, 50, 2000, 2000, 1000, 800)).toEqual({ x: 4, y: 4 })
  })
})

const file = (over: Partial<FileMenuContext> = {}): FileMenuContext => ({
  archiveMember: false,
  gzip: false,
  otherTabActive: false,
  canDownload: true,
  ...over,
})

describe('fileMenu', () => {
  it('lists every entry for a plain file, with download and another tab shown', () => {
    expect(ids(fileMenu(file({ otherTabActive: true })))).toEqual([
      'open',
      'open-tail',
      'open-end',
      'compare',
      '-',
      'download',
      'copy-path',
      'copy-link',
    ])
  })

  it('offers Download only with the download capability', () => {
    expect(ids(fileMenu(file({ canDownload: false })))).toEqual([
      'open',
      'open-tail',
      'open-end',
      '-',
      'copy-path',
      'copy-link',
    ])
  })

  it('offers Compare only while another tab is shown', () => {
    expect(ids(fileMenu(file()))).not.toContain('compare')
    expect(ids(fileMenu(file({ otherTabActive: true })))).toContain('compare')
  })

  it('leaves Tail and the end out for a .gz file', () => {
    expect(ids(fileMenu(file({ gzip: true })))).toEqual(['open', '-', 'download', 'copy-path', 'copy-link'])
  })

  it('leaves Tail, the end and the link out for an archive member', () => {
    expect(ids(fileMenu(file({ archiveMember: true, otherTabActive: true })))).toEqual([
      'open',
      'compare',
      '-',
      'download',
      'copy-path',
    ])
    expect(ids(fileMenu(file({ archiveMember: true, canDownload: false })))).toEqual(['open', '-', 'copy-path'])
  })
})

const folder = (over: Partial<FolderMenuContext> = {}): FolderMenuContext => ({
  inArchive: false,
  archive: false,
  listed: true,
  expanded: true,
  canDownload: true,
  ...over,
})

describe('folderMenu', () => {
  it('lists every entry for an open folder with download', () => {
    expect(ids(folderMenu(folder()))).toEqual(['refresh', 'search-here', 'collapse-all', '-', 'download', 'copy-path'])
    expect(folderMenu(folder()).find((e) => !isSeparator(e) && e.id === 'download')).toMatchObject({
      label: 'Download as ZIP',
    })
  })

  it('offers Download as ZIP only with the download capability', () => {
    expect(ids(folderMenu(folder({ canDownload: false })))).toEqual(['refresh', 'search-here', 'collapse-all', '-', 'copy-path'])
  })

  it('offers Refresh and Collapse all only on an open folder', () => {
    expect(ids(folderMenu(folder({ listed: false, expanded: false })))).toEqual(['search-here', '-', 'download', 'copy-path'])
    expect(ids(folderMenu(folder({ listed: false })))).toEqual(['search-here', 'collapse-all', '-', 'download', 'copy-path'])
  })

  it('inside an archive, only Refresh, Collapse all and Copy path', () => {
    expect(ids(folderMenu(folder({ inArchive: true })))).toEqual(['refresh', 'collapse-all', '-', 'copy-path'])
    expect(ids(folderMenu(folder({ inArchive: true, canDownload: false })))).toEqual(['refresh', 'collapse-all', '-', 'copy-path'])
  })

  it('an archive shown as a folder downloads as itself, and has no Search here', () => {
    const entries = folderMenu(folder({ archive: true }))
    expect(ids(entries)).toEqual(['refresh', 'collapse-all', '-', 'download', 'copy-path'])
    expect(entries.find((e) => !isSeparator(e) && e.id === 'download')).toMatchObject({ label: 'Download' })
    expect(ids(folderMenu(folder({ archive: true, canDownload: false })))).toEqual(['refresh', 'collapse-all', '-', 'copy-path'])
  })
})

const source = (over: Partial<SourceMenuContext> = {}): SourceMenuContext => ({
  listed: true,
  expanded: true,
  system: false,
  canManageSource: true,
  canIndexNow: true,
  canManageRules: true,
  ...over,
})

describe('sourceMenu', () => {
  it('lists every entry for an admin on an open source', () => {
    expect(ids(sourceMenu(source()))).toEqual([
      'refresh',
      'search-here',
      'collapse-all',
      '-',
      'check-now',
      'index-now',
      'copy-host',
      '-',
      'edit-source',
      'edit-rules',
    ])
  })

  it('with view only: no Check now, Index now, Edit source nor Edit rules', () => {
    expect(
      ids(sourceMenu(source({ canManageSource: false, canIndexNow: false, canManageRules: false }))),
    ).toEqual(['refresh', 'search-here', 'collapse-all', '-', 'copy-host'])
  })

  it('needs manage_source for Check now and Edit source', () => {
    const entries = ids(sourceMenu(source({ canManageSource: false })))
    expect(entries).not.toContain('check-now')
    expect(entries).not.toContain('edit-source')
    expect(entries).toContain('edit-rules')
  })

  it('needs run_now (and the index on) for Index file names now', () => {
    expect(ids(sourceMenu(source({ canIndexNow: false })))).not.toContain('index-now')
  })

  it('needs manage_rules for Edit rules', () => {
    const entries = ids(sourceMenu(source({ canManageRules: false })))
    expect(entries).not.toContain('edit-rules')
    expect(entries).toContain('edit-source')
  })

  it('never offers Copy host, Edit source nor Edit rules on the system source', () => {
    expect(ids(sourceMenu(source({ system: true })))).toEqual([
      'refresh',
      'search-here',
      'collapse-all',
      '-',
      'check-now',
      'index-now',
    ])
  })

  it('drops a separator with nothing after it', () => {
    expect(
      ids(
        sourceMenu(
          source({ system: true, canManageSource: false, canIndexNow: false, listed: false, expanded: false }),
        ),
      ),
    ).toEqual(['search-here'])
  })
})

describe('groupMenu', () => {
  it('offers Check all now only with a source to check, Collapse all only when open', () => {
    expect(ids(groupMenu({ expanded: true, checkableSources: 2 }))).toEqual([
      'search-here',
      'check-all',
      'expand-all',
      'collapse-all',
    ])
    expect(ids(groupMenu({ expanded: false, checkableSources: 0 }))).toEqual(['search-here', 'expand-all'])
  })
})

describe('groupContents', () => {
  it('collects the groups and sources below a group, at any depth', () => {
    const node: GroupNode = {
      id: 'customer:1',
      sources: [{ id: 1 }],
      children: [
        { id: 'folder:2', sources: [{ id: 2 }], children: [{ id: 'folder:3', sources: [{ id: 3 }], children: [] }] },
      ],
    }
    const { groupIds, sources } = groupContents(node)
    expect(groupIds).toEqual(['customer:1', 'folder:2', 'folder:3'])
    expect(sources.map((s) => s.id)).toEqual([1, 2, 3])
  })
})

describe('expand keys dropped by Collapse all', () => {
  const keys = [
    'source:1',
    'dir:1::logs',
    'dir:1::logs/app',
    'dir:1::logs/old.zip',
    'dir:1:logs/old.zip:logs/old.zip/inner',
    'dir:1::logsother',
    'dir:1::other',
    'dir:2::logs',
    'source:2',
    'tree:customer:1',
    'tree:folder:2',
    'tree:folder:9',
  ]

  it('a folder: itself and everything below, archives included', () => {
    expect(folderExpandKeys(keys, 1, null, 'logs')).toEqual([
      'dir:1::logs',
      'dir:1::logs/app',
      'dir:1::logs/old.zip',
      'dir:1:logs/old.zip:logs/old.zip/inner',
    ])
  })

  it('an archive: itself and its folders', () => {
    expect(folderExpandKeys(keys, 1, null, 'logs/old.zip')).toEqual([
      'dir:1::logs/old.zip',
      'dir:1:logs/old.zip:logs/old.zip/inner',
    ])
  })

  it('a folder inside an archive', () => {
    expect(folderExpandKeys(keys, 1, 'logs/old.zip', 'logs/old.zip/inner')).toEqual([
      'dir:1:logs/old.zip:logs/old.zip/inner',
    ])
  })

  it('a source: itself and all of its folders, no other source', () => {
    expect(sourceExpandKeys(keys, 1)).toEqual([
      'source:1',
      'dir:1::logs',
      'dir:1::logs/app',
      'dir:1::logs/old.zip',
      'dir:1:logs/old.zip:logs/old.zip/inner',
      'dir:1::logsother',
      'dir:1::other',
    ])
  })

  it('a group: its groups and its sources', () => {
    expect(groupExpandKeys(keys, ['customer:1', 'folder:2'], [2])).toEqual([
      'tree:customer:1',
      'tree:folder:2',
      'dir:2::logs',
      'source:2',
    ])
  })
})

describe('fileLink', () => {
  it("opens the file in the viewer, from the page's address", () => {
    expect(fileLink('https://pt.example.test/app/#/viewer?x=1', 4, 'logs/a b.log')).toBe(
      'https://pt.example.test/app/#/viewer/4?path=logs%2Fa+b.log',
    )
  })
})

describe('checkableSourceIds', () => {
  it('keeps the enabled sources on which the user holds manage_source', () => {
    expect(
      checkableSourceIds([
        { id: 1, enabled: true, my_capabilities: ['view', 'manage_source'] },
        { id: 2, enabled: false, my_capabilities: ['manage_source'] },
        { id: 3, enabled: true, my_capabilities: ['view'] },
        { id: 4, my_capabilities: ['manage_source'] },
      ]),
    ).toEqual([1, 4])
  })
})

describe('runLimited', () => {
  function deferred() {
    let resolve!: () => void
    let reject!: (e: Error) => void
    const promise = new Promise<void>((res, rej) => {
      resolve = res
      reject = rej
    })
    return { promise, resolve, reject }
  }

  it('never runs more than the limit at once, and runs every job', async () => {
    const pending = Array.from({ length: 7 }, deferred)
    let running = 0
    let most = 0
    const started: number[] = []
    const jobs = pending.map((d, i) => async () => {
      started.push(i)
      running += 1
      most = Math.max(most, running)
      try {
        await d.promise
      } finally {
        running -= 1
      }
      return i
    })
    const all = runLimited(jobs, CHECK_ALL_CONCURRENCY)
    await Promise.resolve()
    expect(CHECK_ALL_CONCURRENCY).toBe(3)
    expect(started).toEqual([0, 1, 2])
    for (const d of pending) {
      d.resolve()
      await new Promise((r) => setTimeout(r, 0))
    }
    const results = await all
    expect(most).toBe(3)
    expect(started).toEqual([0, 1, 2, 3, 4, 5, 6])
    expect(results.map((r) => (r.status === 'fulfilled' ? r.value : null))).toEqual([0, 1, 2, 3, 4, 5, 6])
  })

  it('goes on after a failure', async () => {
    const jobs = [0, 1, 2, 3, 4].map((i) => async () => {
      if (i === 1) throw new Error('unreachable')
      return i
    })
    const results = await runLimited(jobs, 3)
    expect(results.map((r) => r.status)).toEqual(['fulfilled', 'rejected', 'fulfilled', 'fulfilled', 'fulfilled'])
  })
})

describe('insideLink', () => {
  const element = (inLink: boolean) => ({ closest: (sel: string) => (sel === 'a[href]' && inLink ? {} : null) })

  it('is true inside a link, so the browser keeps its own menu', () => {
    expect(insideLink(element(true) as unknown as EventTarget)).toBe(true)
  })

  it('is false elsewhere on a node, and without a target', () => {
    expect(insideLink(element(false) as unknown as EventTarget)).toBe(false)
    expect(insideLink(null)).toBe(false)
  })
})
