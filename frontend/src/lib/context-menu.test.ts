import { describe, expect, it } from 'vitest'
import {
  contextMenuIntent,
  findAllMenu,
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
