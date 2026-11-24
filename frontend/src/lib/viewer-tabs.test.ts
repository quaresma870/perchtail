import { EditorState } from '@codemirror/state'
import { tick } from 'svelte'
import { get } from 'svelte/store'
import { beforeEach, describe, expect, it } from 'vitest'
import {
  NO_PATTERNS,
  forgetAllEditorStates,
  formattedContent,
  getEditorState,
  saveEditorState,
  severityBySource,
  type SavedEditorState,
} from './editor-states'
import type { SeverityPattern } from './types'
import {
  closeTabs,
  closeTabsOfHiddenSources,
  forgetSeverityWithoutTabs,
  viewerState,
  type Tab,
} from './viewer-tabs'

function tab(sourceId: number, path: string): Tab {
  return {
    key: `${sourceId}::${path}::`,
    sourceId,
    sourceName: `source ${sourceId}`,
    path,
    member: null,
    name: path,
    content: `${path}\n`,
    scratchKey: null,
    wrapEnabled: false,
    showWhitespace: false,
    bookmarks: [],
    markPatterns: [],
    format: 'none',
    following: false,
    byteSize: 0,
    followHead: null,
    followMode: 'incremental',
    newLines: 0,
  }
}

function saved(content: string): SavedEditorState {
  return {
    state: EditorState.create({ doc: content }),
    content,
    scrollTop: 0,
    atBottom: false,
    inputs: {
      content,
      format: 'none',
      severityPatterns: NO_PATTERNS,
      filename: 'app.log',
      wrapEnabled: false,
      showWhitespace: false,
      bookmarks: [],
      markPatterns: [],
    },
  }
}

// Opens the tabs as the Viewer would have them: each with a saved editor
// state and its formatted text.
function open(tabs: Tab[], activeKey: string) {
  forgetAllEditorStates()
  viewerState.set({ tabs, activeKey })
  for (const t of tabs) {
    saveEditorState(t.key, saved(t.content))
    formattedContent(t.key, t.content, 'none', () => t.content)
  }
}

// Whether the tab's formatted text is still kept (no new formatting run).
function stillFormatted(t: Tab): boolean {
  let ran = false
  formattedContent(t.key, t.content, 'none', () => {
    ran = true
    return t.content
  })
  return !ran
}

describe('closing tabs', () => {
  const a = tab(1, 'a.log')
  const b = tab(1, 'b.log')
  const c = tab(2, 'c.log')

  beforeEach(() => open([a, b, c], b.key))

  it('forgets the editor state and formatted text of a closed tab', async () => {
    expect(closeTabs([a.key])).toEqual([a])
    expect(get(viewerState)).toEqual({ tabs: [b, c], activeKey: b.key })
    expect(getEditorState(a.key)).toBeUndefined()
    expect(getEditorState(b.key)).toBeDefined()
    expect(stillFormatted(b)).toBe(true)
    expect(stillFormatted(a)).toBe(false)
  })

  it('forgets it again once the editor that showed it has let go of it', async () => {
    closeTabs([b.key])
    expect(get(viewerState).activeKey).toBe(c.key)
    // The editor keeps the tab it was showing as it switches to the next.
    saveEditorState(b.key, saved(b.content))
    await tick()
    expect(getEditorState(b.key)).toBeUndefined()
  })

  it("forgets the tabs of a source that isn't shown any more", async () => {
    const closed = closeTabsOfHiddenSources(new Set([2]))
    expect(closed).toEqual([a, b])
    expect(get(viewerState)).toEqual({ tabs: [c], activeKey: c.key })
    saveEditorState(b.key, saved(b.content))
    await tick()
    expect(getEditorState(a.key)).toBeUndefined()
    expect(getEditorState(b.key)).toBeUndefined()
    expect(stillFormatted(a)).toBe(false)
    expect(getEditorState(c.key)).toBeDefined()
  })

  it('closes nothing while every source is shown', () => {
    expect(closeTabsOfHiddenSources(new Set([1, 2]))).toEqual([])
    expect(get(viewerState).tabs).toEqual([a, b, c])
    expect(getEditorState(a.key)).toBeDefined()
  })
})

describe("closing a source's tabs", () => {
  const a = tab(1, 'a.log')
  const b = tab(1, 'b.log')
  const c = tab(2, 'c.log')
  const patterns1 = [{ id: 1 }] as unknown as SeverityPattern[]
  const patterns2 = [{ id: 2 }] as unknown as SeverityPattern[]

  beforeEach(() => {
    open([a, b, c], b.key)
    severityBySource.set({ 1: patterns1, 2: patterns2 })
  })

  it('forgets the severity patterns of a source when its last tab closes', () => {
    closeTabs([c.key])
    expect(get(severityBySource)).toEqual({ 1: patterns1 })
  })

  it('keeps them while another tab of the source is open', () => {
    closeTabs([a.key])
    expect(get(severityBySource)[1]).toBe(patterns1)
    closeTabs([b.key])
    expect(get(severityBySource)).toEqual({ 2: patterns2 })
  })

  it("forgets them when the source isn't shown any more", () => {
    closeTabsOfHiddenSources(new Set([2]))
    expect(get(severityBySource)).toEqual({ 2: patterns2 })
  })

  it('keeps the same entries when nothing closes', () => {
    const before = get(severityBySource)
    closeTabs(['9::none.log::'])
    expect(get(severityBySource)).toBe(before)
  })
})

describe('an open that failed before its tab was made', () => {
  const pattern = { id: 1, pattern: 'ERROR' } as unknown as SeverityPattern

  beforeEach(() => {
    viewerState.set({ tabs: [], activeKey: null })
    severityBySource.set({})
  })

  it("forgets the source's patterns when it has no tab", () => {
    severityBySource.set({ 1: [pattern], 2: [pattern] })
    viewerState.set({ tabs: [tab(2, 'b.log')], activeKey: '2::b.log::' })
    forgetSeverityWithoutTabs(1)
    expect(get(severityBySource)).toEqual({ 2: [pattern] })
  })

  it('keeps them while a tab of the source is open', () => {
    severityBySource.set({ 1: [pattern] })
    viewerState.set({ tabs: [tab(1, 'a.log')], activeKey: '1::a.log::' })
    forgetSeverityWithoutTabs(1)
    expect(get(severityBySource)).toEqual({ 1: [pattern] })
  })

  it('leaves the store alone for a source it never had', () => {
    const all = { 2: [pattern] }
    severityBySource.set(all)
    forgetSeverityWithoutTabs(1)
    expect(get(severityBySource)).toBe(all)
  })
})
