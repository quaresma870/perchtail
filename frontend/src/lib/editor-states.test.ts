import { EditorState } from '@codemirror/state'
import { get } from 'svelte/store'
import { describe, expect, it } from 'vitest'
import {
  NO_PATTERNS,
  forgetAllEditorStates,
  forgetEditorState,
  formattedContent,
  getEditorState,
  refreshSeverityPatterns,
  replacedPatterns,
  saveEditorState,
  severityBySource,
  type SavedEditorState,
} from './editor-states'
import type { EditorInputs } from './editor-sync'
import type { SeverityPattern } from './types'

function saved(content: string, scrollTop = 0): SavedEditorState {
  const inputs: EditorInputs = {
    content,
    format: 'none',
    severityPatterns: NO_PATTERNS,
    filename: 'app.log',
    wrapEnabled: false,
    showWhitespace: false,
    bookmarks: [],
    markPatterns: [],
  }
  return { state: EditorState.create({ doc: content }), content, scrollTop, atBottom: false, inputs }
}

describe('editor states', () => {
  it('keeps a saved state under its tab key', () => {
    forgetAllEditorStates()
    const a = saved('a\n', 120)
    saveEditorState('1::a.log::', a)
    expect(getEditorState('1::a.log::')).toBe(a)
    expect(getEditorState('1::b.log::')).toBeUndefined()
  })

  it('replaces a tab saved again', () => {
    forgetAllEditorStates()
    saveEditorState('k', saved('a\n'))
    const later = saved('a\nb\n', 40)
    saveEditorState('k', later)
    expect(getEditorState('k')).toBe(later)
  })

  it('forgets one tab', () => {
    forgetAllEditorStates()
    saveEditorState('a', saved('a\n'))
    saveEditorState('b', saved('b\n'))
    forgetEditorState('a')
    expect(getEditorState('a')).toBeUndefined()
    expect(getEditorState('b')).toBeDefined()
  })

  it('forgets every tab', () => {
    saveEditorState('a', saved('a\n'))
    saveEditorState('b', saved('b\n'))
    forgetAllEditorStates()
    expect(getEditorState('a')).toBeUndefined()
    expect(getEditorState('b')).toBeUndefined()
  })

  it('formats a tab again only when its text or format changed', () => {
    forgetAllEditorStates()
    let runs = 0
    const compute = (text: string) => () => {
      runs += 1
      return text.toUpperCase()
    }
    expect(formattedContent('k', 'abc', 'pretty', compute('abc'))).toBe('ABC')
    expect(formattedContent('k', 'abc', 'pretty', compute('abc'))).toBe('ABC')
    expect(runs).toBe(1)
    expect(formattedContent('k', 'abcd', 'pretty', compute('abcd'))).toBe('ABCD')
    expect(formattedContent('k', 'abcd', 'none', compute('abcd'))).toBe('ABCD')
    expect(runs).toBe(3)
    forgetEditorState('k')
    formattedContent('k', 'abcd', 'none', compute('abcd'))
    expect(runs).toBe(4)
  })

  it('shares one frozen empty pattern list', () => {
    expect(NO_PATTERNS).toHaveLength(0)
    expect(Object.isFrozen(NO_PATTERNS)).toBe(true)
  })
})

function pattern(id: number, text: string, extra: Partial<SeverityPattern> = {}): SeverityPattern {
  return {
    id,
    source_id: null,
    level: 'error',
    pattern: text,
    pattern_kind: 'glob',
    enabled: true,
    highlight_line: false,
    include_in_navigation: true,
    ...extra,
  }
}

describe('severity patterns kept per source', () => {
  it('keeps the same list when the fetched one highlights the same', () => {
    const current = [pattern(1, 'panic')]
    expect(replacedPatterns(current, [pattern(1, 'panic')])).toBe(current)
    expect(replacedPatterns(NO_PATTERNS, [])).toBe(NO_PATTERNS)
  })

  it('takes the fetched list when it highlights differently', () => {
    const current = [pattern(1, 'panic')]
    const changed = [pattern(1, 'panic', { highlight_line: true })]
    expect(replacedPatterns(current, changed)).toBe(changed)
    const added = [pattern(1, 'panic'), pattern(2, 're:FATAL')]
    expect(replacedPatterns(current, added)).toBe(added)
    expect(replacedPatterns(current, [])).toBe(NO_PATTERNS)
    expect(replacedPatterns(undefined, [])).toBe(NO_PATTERNS)
  })

  it('refreshes a kept source only when its patterns changed', async () => {
    const current = [pattern(1, 'panic')]
    severityBySource.set({ 7: current })
    await refreshSeverityPatterns(7, async () => [pattern(1, 'panic')])
    expect(get(severityBySource)[7]).toBe(current)
    const changed = [pattern(1, 'panic'), pattern(2, 're:FATAL')]
    await refreshSeverityPatterns(7, async () => changed)
    expect(get(severityBySource)[7]).toBe(changed)
  })

  it('keeps the patterns a source had when the request fails', async () => {
    const current = [pattern(1, 'panic')]
    severityBySource.set({ 7: current })
    await refreshSeverityPatterns(7, async () => {
      throw new Error('offline')
    })
    expect(get(severityBySource)[7]).toBe(current)
  })

  it("doesn't put back a source forgotten while its patterns were on the way", async () => {
    severityBySource.set({})
    await refreshSeverityPatterns(7, async () => [pattern(1, 'panic')])
    expect(get(severityBySource)[7]).toBeUndefined()
  })
})
