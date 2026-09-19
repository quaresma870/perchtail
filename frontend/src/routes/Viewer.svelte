<script lang="ts">
  import { onDestroy, onMount, tick } from 'svelte'
  import { push, router } from 'svelte-spa-router'
  import { api, ApiError } from '../lib/api'
  import ConnectionCard from '../lib/components/ConnectionCard.svelte'
  import FolderTree from '../lib/components/FolderTree.svelte'
  import CodeMirrorPane from '../lib/components/CodeMirrorPane.svelte'
  import DiffPane from '../lib/components/DiffPane.svelte'
  import FindInDocumentPanel from '../lib/components/FindInDocumentPanel.svelte'
  import { filterConnections } from '../lib/connection-filter'
  import { canFormat, formatContent, type FormatMode } from '../lib/format-content'
  import { languageForFilename } from '../lib/file-language'
  import { MARK_SWATCH_COLORS, nextMarkColorIndex, type MarkPattern } from '../lib/mark-highlighting'
  import { parsePatternInput } from '../lib/rule-format'
  import { tabKey } from '../lib/tab-key'
  import type { BrowseEntry, SeverityPattern, Source } from '../lib/types'

  export let params: { sourceId?: string } = {}

  interface Tab {
    key: string
    path: string
    member: string | null
    name: string
    content: string
    scratchKey: string | null
    wrapEnabled: boolean
    showWhitespace: boolean
    // Pure client-side/session state (ROADMAP.md: never written to the
    // file, never persisted server-side) -- 1-indexed line numbers, kept
    // sorted ascending so the bookmark-nav wrap-around cycling and the
    // CodeMirror decoration builder can both rely on that order.
    bookmarks: number[]
    // Notepad++-style "Mark" highlighting (ROADMAP.md): ad hoc patterns the
    // viewer types in on the spot, each its own color. Same
    // never-persisted, per-tab-state shape as bookmarks above.
    markPatterns: MarkPattern[]
    // Display-only reformat (ROADMAP.md: "never touches the file on disk")
    // for JSON/XML tabs -- 'none' shows the raw fetched content as-is.
    format: FormatMode
    // "Follow" (tail -f, ROADMAP.md): client-side polling, not a
    // persistent connection -- see the reactive block below for why. Only
    // ever driven while this tab is the active one; the flag itself
    // persists on the tab so switching away and back resumes it.
    following: boolean
  }

  const FOLLOW_INTERVAL_MS = 2000

  const sourceId = params.sourceId ? Number(params.sourceId) : null

  let recentSources: Source[] = []
  let allSources: Source[] = []
  let connectionsQuery = ''
  let source: Source | null = null
  let rootEntries: BrowseEntry[] = []
  let severityPatterns: SeverityPattern[] = []
  let loading = true
  let error = ''

  let tabs: Tab[] = []
  let activeKey: string | null = null
  let paneRef: CodeMirrorPane | null = null
  // Deliberately not reset on tab switch -- if it's open, it stays open and
  // re-searches whatever tab becomes active against the same query, rather
  // than forcing it closed and reopened for every file.
  let findAllOpen = false
  let goToLineOpen = false
  let goToLineValue = ''
  let goToLineInput: HTMLInputElement | null = null
  let markInputOpen = false
  let markInputValue = ''
  let markInput: HTMLInputElement | null = null
  let copyStatus: '' | 'copied' | 'empty' = ''
  let copyStatusTimer: ReturnType<typeof setTimeout> | null = null
  let formatError = ''
  // "Compare" (ROADMAP.md's diff-view item): `compareArmed` is the
  // "waiting for a second file/tab to be picked" state; `compareWith` is
  // the resulting one-shot snapshot (name + already-fetched content) to
  // diff the active tab against. Neither is part of `Tab` -- a comparison
  // is contextual to whichever file is active right now, not a persisted
  // per-tab mode, so it's cleared whenever the active tab itself changes.
  let compareArmed = false
  let compareWith: { name: string; content: string } | null = null
  let compareKey = 0
  $: activeKey, (formatError = ''), (compareWith = null), (compareArmed = false)
  $: activeTab = tabs.find((t) => t.key === activeKey) ?? null
  // Display-only reformat (never mutates activeTab.content itself, so
  // reloading or turning the toggle back off always recovers the exact
  // raw fetched text) -- falls back to the raw content if formatting the
  // *current* content fails, which can only happen after a reload changed
  // the underlying bytes out from under an active 'pretty'/'minified' mode
  // (setFormat itself refuses to turn the mode on for invalid content).
  $: displayContent = activeTab
    ? (formatContent(activeTab.content, languageForFilename(activeTab.name), activeTab.format) ??
      activeTab.content)
    : ''

  $: filteredAllSources = filterConnections(allSources, connectionsQuery)

  async function loadSourcePicker() {
    loading = true
    error = ''
    try {
      allSources = await api.get<Source[]>('/sources')
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to load sources'
    }
    try {
      // Kept independent of the call above -- an issue fetching recent
      // history shouldn't block the all-connections column, which is the
      // more essential of the two lists.
      recentSources = await api.get<Source[]>('/sources/recent')
    } catch {
      recentSources = []
    }
    loading = false
  }

  async function loadTree() {
    if (sourceId === null) return
    loading = true
    error = ''
    try {
      source = await api.get<Source>(`/sources/${sourceId}`)
      rootEntries = await api.get<BrowseEntry[]>(`/sources/${sourceId}/browse?path=`)
    } catch (err) {
      error = err instanceof ApiError ? err.detail : 'Failed to load source'
    } finally {
      loading = false
    }
    try {
      // Independent of the calls above -- a failure here shouldn't block
      // browsing/opening files, it just means no highlighting.
      severityPatterns = await api.get<SeverityPattern[]>(
        `/sources/${sourceId}/severity-patterns/effective`,
      )
    } catch {
      severityPatterns = []
    }
  }

  async function openFile(path: string, member: string | null, name: string): Promise<boolean> {
    if (sourceId === null) return false
    const key = tabKey(path, member)
    const existing = tabs.find((t) => t.key === key)
    if (existing) {
      activeKey = key
      return true
    }

    const fetchParams = new URLSearchParams({ path })
    if (member) fetchParams.set('member', member)
    try {
      const response = await fetch(`/sources/${sourceId}/open?${fetchParams.toString()}`, {
        credentials: 'include',
      })
      if (!response.ok) {
        error = `Failed to open ${name}`
        return false
      }
      const content = await response.text()
      const scratchKey = response.headers.get('x-scratch-key')
      const tab: Tab = {
        key,
        path,
        member,
        name,
        content,
        scratchKey,
        wrapEnabled: false,
        showWhitespace: false,
        bookmarks: [],
        markPatterns: [],
        format: 'none',
        following: false,
      }
      tabs = [...tabs, tab]
      activeKey = key
      return true
    } catch {
      error = `Failed to open ${name}`
      return false
    }
  }

  async function handleOpen(event: CustomEvent<{ path: string; member: string | null; name: string }>) {
    const { path, member, name } = event.detail
    if (compareArmed) {
      await pickCompareTarget(path, member, name)
      return
    }
    await openFile(path, member, name)
  }

  // "Compare" (ROADMAP.md): fetches the picked file fresh, same as opening
  // it, but doesn't add a tab for it -- it's a one-shot snapshot to diff
  // against the active tab, not something meant to stay open on its own.
  // Releases its scratch reference immediately after fetching, same
  // best-effort close as every other one-shot fetch in this file.
  async function pickCompareTarget(path: string, member: string | null, name: string) {
    if (sourceId === null) return
    const fetchParams = new URLSearchParams({ path })
    if (member) fetchParams.set('member', member)
    try {
      const response = await fetch(`/sources/${sourceId}/open?${fetchParams.toString()}`, {
        credentials: 'include',
      })
      if (!response.ok) {
        error = `Failed to open ${name} for comparison`
        compareArmed = false
        return
      }
      const content = await response.text()
      const scratchKey = response.headers.get('x-scratch-key')
      compareWith = { name, content }
      compareArmed = false
      compareKey += 1
      if (scratchKey) {
        api.post(`/sources/${sourceId}/close`, { path, member }).catch(() => {})
      }
    } catch {
      error = `Failed to open ${name} for comparison`
      compareArmed = false
    }
  }

  // Picking another already-open tab as the compare target, instead of
  // switching to it, while "Compare" is armed.
  function handleTabClick(tab: Tab) {
    if (compareArmed) {
      compareArmed = false
      if (tab.key !== activeKey) {
        compareWith = { name: tab.name, content: tab.content }
        compareKey += 1
      }
      return
    }
    activeKey = tab.key
  }

  // Search click-through (Search.svelte pushes here with ?path=...&line=...)
  // — opens the file same as clicking it in the tree, then scrolls the
  // CodeMirror pane to the matched line once its content has rendered.
  async function openFromDeepLink(path: string, line: number | null) {
    const name = path.split('/').pop() ?? path
    const opened = await openFile(path, null, name)
    if (opened && line !== null) {
      await tick()
      paneRef?.scrollToLine(line)
    }
  }

  async function closeTab(tab: Tab) {
    tabs = tabs.filter((t) => t.key !== tab.key)
    if (activeKey === tab.key) {
      activeKey = tabs.length > 0 ? tabs[tabs.length - 1].key : null
    }
    if (tab.scratchKey && sourceId !== null) {
      try {
        await api.post(`/sources/${sourceId}/close`, { path: tab.path, member: tab.member })
      } catch {
        // best-effort — the idle-sweep backstop will clean this up regardless
      }
    }
  }

  function updateActiveTab(patch: Partial<Tab>) {
    if (!activeTab) return
    const key = activeTab.key
    tabs = tabs.map((t) => (t.key === key ? { ...t, ...patch } : t))
  }

  function toggleWrap() {
    if (!activeTab) return
    updateActiveTab({ wrapEnabled: !activeTab.wrapEnabled })
  }

  function toggleShowWhitespace() {
    if (!activeTab) return
    updateActiveTab({ showWhitespace: !activeTab.showWhitespace })
  }

  function toggleBookmark() {
    if (!activeTab || !paneRef) return
    const line = paneRef.currentLine()
    const bookmarks = activeTab.bookmarks.includes(line)
      ? activeTab.bookmarks.filter((l) => l !== line)
      : [...activeTab.bookmarks, line].sort((a, b) => a - b)
    updateActiveTab({ bookmarks })
  }

  // "Mark" highlighting (Notepad++ feature, ROADMAP.md) -- reuses Rule's
  // `re:`-prefix convention (parsePatternInput) so a viewer can switch a
  // mark to regex the same way an admin switches a rule, without a
  // separate glob/regex toggle control cluttering this small inline form.
  async function openMarkInput() {
    markInputOpen = true
    markInputValue = ''
    await tick()
    markInput?.focus()
  }

  function submitMarkInput() {
    // Closing the form (both branches below) removes the input from the
    // DOM, which fires its own blur -- and the input's on:blur also calls
    // this function, so a plain Enter keypress would otherwise run this
    // twice (submit, then the blur it triggers) and add the same mark
    // twice. Clearing the value up front before doing anything else makes
    // that second call a harmless no-op (trimmed === '').
    const trimmed = markInputValue.trim()
    markInputValue = ''
    markInputOpen = false
    if (trimmed && activeTab) {
      const { pattern, pattern_kind } = parsePatternInput(trimmed)
      const colorIndex = nextMarkColorIndex(activeTab.markPatterns)
      const mark: MarkPattern = { id: crypto.randomUUID(), pattern, pattern_kind, colorIndex }
      updateActiveTab({ markPatterns: [...activeTab.markPatterns, mark] })
    }
  }

  function removeMark(id: string) {
    if (!activeTab) return
    updateActiveTab({ markPatterns: activeTab.markPatterns.filter((m) => m.id !== id) })
  }

  function clearMarks() {
    updateActiveTab({ markPatterns: [] })
  }

  // "Beautify"/"Minify" toggle buttons (ROADMAP.md): clicking the active
  // mode again turns it back off (-> 'none'), same toggle-button UX as
  // Wrap/Show chars. Validates against the tab's raw content before
  // switching modes -- a failed beautify/minify (invalid JSON/malformed
  // XML) leaves the tab exactly as it was and surfaces `formatError`,
  // rather than silently switching to a mode that immediately falls back
  // to unformatted content with no explanation.
  // Bookmarks are 1-indexed *line numbers* against whatever's currently
  // displayed (codemirror-theme.ts's buildBookmarkDecorations) -- any
  // format change reflows the document into a different line count, so an
  // old bookmark's line number no longer points at the same content (or
  // any content at all). Cleared on every format transition rather than
  // left to silently drift or vanish out-of-range.
  function setFormat(mode: 'pretty' | 'minified') {
    if (!activeTab) return
    if (activeTab.format === mode) {
      updateActiveTab({ format: 'none', bookmarks: [] })
      formatError = ''
      return
    }
    const language = languageForFilename(activeTab.name)
    if (formatContent(activeTab.content, language, mode) === null) {
      formatError = `Could not ${mode === 'pretty' ? 'beautify' : 'minify'}: invalid ${language ?? 'file'} content.`
      return
    }
    formatError = ''
    updateActiveTab({ format: mode, bookmarks: [] })
  }

  // Manual re-fetch of the currently open file's content in place, without
  // closing and reopening the tab -- the always-fresh-fetch model already
  // exists for a fresh *open*, this just exposes a fetch-again action for a
  // tab that's already open. Releases the previous scratch reference after
  // the new one lands, same "path/member, not the scratch key itself"
  // release call closeTab already makes.
  async function reloadActiveTab() {
    if (!activeTab || sourceId === null) return
    const tab = activeTab
    const fetchParams = new URLSearchParams({ path: tab.path })
    if (tab.member) fetchParams.set('member', tab.member)
    try {
      const response = await fetch(`/sources/${sourceId}/open?${fetchParams.toString()}`, {
        credentials: 'include',
      })
      if (!response.ok) {
        error = `Failed to reload ${tab.name}`
        return
      }
      const content = await response.text()
      const scratchKey = response.headers.get('x-scratch-key')
      const oldScratchKey = tab.scratchKey
      // Deliberately keeps whatever `format` was active -- Follow (tail -f)
      // calls this every couple of seconds, and resetting it on every
      // fetch would silently fight a Beautify/Minify toggle into
      // uselessness the moment Follow is also on. `displayContent`'s own
      // `formatContent(...) ?? content` fallback already degrades
      // gracefully to raw content if a reload's new bytes no longer parse
      // under the active mode, so nothing here needs to force it off.
      tabs = tabs.map((t) => (t.key === tab.key ? { ...t, content, scratchKey } : t))
      if (oldScratchKey) {
        api
          .post(`/sources/${sourceId}/close`, { path: tab.path, member: tab.member })
          .catch(() => {})
      }
    } catch {
      error = `Failed to reload ${tab.name}`
    }
  }

  function toggleFollow() {
    if (!activeTab) return
    updateActiveTab({ following: !activeTab.following })
  }

  // "Follow" (tail -f, ROADMAP.md): client-side polling was picked over
  // extending the agent-mode WebSocket with a "watch" command -- this
  // needs to work uniformly across every protocol (SSH/SMB/WinRM/local),
  // not just agent-linked sources, and there's no persistent connection to
  // extend for the other three. The accepted cost is a full re-fetch of
  // the whole file on every tick (same always-fresh, no-partial-read
  // architecture as a manual Reload -- see CLAUDE.md's ephemeral-fetch
  // rule) rather than a byte-range/tail-only read.
  let followTimer: ReturnType<typeof setInterval> | null = null
  let followPolling = false

  function stopFollowTimer() {
    if (followTimer) {
      clearInterval(followTimer)
      followTimer = null
    }
  }

  async function followTick() {
    // Skips a tick rather than queuing it -- if a fetch is still in
    // flight when the next interval fires (a slow SSH/SMB round trip),
    // overlapping reloads could land out of order.
    if (followPolling) return
    followPolling = true
    try {
      await reloadActiveTab()
    } finally {
      followPolling = false
    }
  }

  // Re-evaluated whenever the active tab (a tab switch swaps in a
  // different object) or that tab's own `following` flag changes: only
  // ever one timer running, and only for whichever tab is both active and
  // following -- switching away pauses it (a background tab doesn't keep
  // polling), switching back resumes it, since the flag itself lives on
  // the tab, not on this component.
  $: {
    stopFollowTimer()
    if (activeTab?.following && sourceId !== null) {
      followTimer = setInterval(followTick, FOLLOW_INTERVAL_MS)
    }
  }

  async function doCopySelectedLines() {
    if (!paneRef) return
    const copied = await paneRef.copySelectedLines()
    copyStatus = copied ? 'copied' : 'empty'
    if (copyStatusTimer) clearTimeout(copyStatusTimer)
    copyStatusTimer = setTimeout(() => (copyStatus = ''), 1500)
  }

  async function openGoToLine() {
    goToLineOpen = true
    goToLineValue = ''
    await tick()
    goToLineInput?.focus()
  }

  function submitGoToLine() {
    const line = Number(goToLineValue)
    if (Number.isFinite(line) && line > 0) {
      paneRef?.scrollToLine(line)
    }
    goToLineOpen = false
  }

  // Ctrl/Cmd+F is meant to search the open file via CodeMirror's own search
  // panel, not the browser's find bar — but a plain keydown on the editor
  // DOM isn't reliable (see CodeMirrorPane's openSearch doc comment), so
  // it's intercepted here at the window level instead, while a tab is open.
  // Ctrl/Cmd+G (go to line) is intercepted the same way and for the same
  // reason.
  function handleKeydown(event: KeyboardEvent) {
    if (!activeTab) return
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'f') {
      event.preventDefault()
      paneRef?.openSearch()
    } else if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'g') {
      event.preventDefault()
      openGoToLine()
    }
  }

  onMount(() => {
    if (sourceId === null) {
      loadSourcePicker()
    } else {
      loadTree()
      const deepLink = new URLSearchParams(router.querystring ?? '')
      const deepLinkPath = deepLink.get('path')
      if (deepLinkPath) {
        const line = deepLink.get('line')
        openFromDeepLink(deepLinkPath, line ? Number(line) : null)
      }
    }
    window.addEventListener('keydown', handleKeydown)
  })

  onDestroy(() => {
    window.removeEventListener('keydown', handleKeydown)
    if (copyStatusTimer) clearTimeout(copyStatusTimer)
    stopFollowTimer()
    for (const tab of tabs) {
      if (tab.scratchKey && sourceId !== null) {
        api.post(`/sources/${sourceId}/close`, { path: tab.path, member: tab.member }).catch(() => {})
      }
    }
  })
</script>

{#if sourceId === null}
  <div class="picker page">
    {#if loading}
      <p class="hint">Loading…</p>
    {:else if error}
      <p class="error">{error}</p>
    {:else}
      <div class="picker-columns">
        <section class="picker-column recent">
          <h1>Recent</h1>
          {#if recentSources.length === 0}
            <p class="hint">Sources you open will show up here.</p>
          {:else}
            <ul>
              {#each recentSources as s (s.id)}
                <li>
                  <ConnectionCard source={s} on:click={() => push(`/viewer/${s.id}`)} />
                </li>
              {/each}
            </ul>
          {/if}
        </section>

        <section class="picker-column all">
          <div class="all-header">
            <h1>All connections</h1>
            <input
              class="input search-box"
              type="search"
              placeholder="Search by folder, customer, or host…"
              bind:value={connectionsQuery}
            />
          </div>
          {#if filteredAllSources.length === 0}
            <p class="hint">
              {connectionsQuery.trim() ? 'No connections match that search.' : 'No sources visible to your account.'}
            </p>
          {:else}
            <ul>
              {#each filteredAllSources as s (s.id)}
                <li>
                  <ConnectionCard source={s} on:click={() => push(`/viewer/${s.id}`)} />
                </li>
              {/each}
            </ul>
          {/if}
        </section>
      </div>
    {/if}
  </div>
{:else}
  <div class="viewer">
    <aside class="tree">
      <div class="tree-header">
        <button class="link" on:click={() => push('/viewer')}>← sources</button>
        <strong>{source?.name ?? ''}</strong>
      </div>
      {#if loading}
        <p class="hint">Loading…</p>
      {:else if error}
        <p class="error">{error}</p>
      {:else}
        <div class="tree-body">
          {#each rootEntries as entry (entry.path)}
            <FolderTree {sourceId} {entry} {activeKey} on:open={handleOpen} />
          {/each}
          {#if rootEntries.length === 0}
            <p class="hint">Nothing visible here — check the source's rules.</p>
          {/if}
        </div>
      {/if}
    </aside>

    <section class="editor-area">
      <div class="tabs">
        {#each tabs as tab (tab.key)}
          <div class="tab" class:active={tab.key === activeKey}>
            <button class="tab-label" on:click={() => handleTabClick(tab)}>
              <svg class="file-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
                <path d="M14.5 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7.5Z" />
                <path d="M14 2v4a2 2 0 0 0 2 2h4" />
              </svg>
              {tab.name}
            </button>
            <button class="close" on:click={() => closeTab(tab)} aria-label={`Close ${tab.name}`}
              >×</button
            >
          </div>
        {/each}
      </div>
      {#if activeTab && compareWith}
        <div class="pane-toolbar">
          <div class="toolbar-left">
            <span class="hint">
              Comparing <strong>{activeTab.name}</strong> ↔ <strong>{compareWith.name}</strong>
            </span>
          </div>
          <div class="toolbar-right">
            <button class="link" on:click={() => (compareWith = null)}>✕ Exit compare</button>
          </div>
        </div>
        {#key compareKey}
          <DiffPane
            leftContent={activeTab.content}
            rightContent={compareWith.content}
            leftLabel={activeTab.name}
            rightLabel={compareWith.name}
          />
        {/key}
      {:else if activeTab}
        <div class="pane-toolbar">
          <div class="toolbar-left">
            <span class="hint" title="Ctrl/Cmd+F to search, Ctrl/Cmd+G to go to line">⌕ Find</span>
            <button
              class="btn-toggle"
              class:active={findAllOpen}
              on:click={() => (findAllOpen = !findAllOpen)}
            >
              Find All
            </button>
            <button
              class="btn-toggle"
              class:active={compareArmed}
              title="Pick a second file (from the tree, or another open tab) to diff against this one"
              on:click={() => (compareArmed = !compareArmed)}
            >
              Compare
            </button>
            {#if compareArmed}
              <span class="hint">Pick a file in the tree or another tab to compare against…</span>
            {/if}
            <button
              class="btn-toggle"
              class:active={activeTab.following}
              title="Follow new content like tail -f -- polls and re-fetches the whole file every few seconds"
              on:click={toggleFollow}
            >
              Follow
            </button>
            <button
              class="btn-toggle"
              class:active={activeTab.wrapEnabled}
              on:click={toggleWrap}
            >
              Wrap
            </button>
            <button
              class="btn-toggle"
              class:active={activeTab.showWhitespace}
              on:click={toggleShowWhitespace}
            >
              Show chars
            </button>
            {#if canFormat(languageForFilename(activeTab.name))}
              <button
                class="btn-toggle"
                class:active={activeTab.format === 'pretty'}
                title="Reformat for readability -- display only, never writes to the file"
                on:click={() => setFormat('pretty')}
              >
                Beautify
              </button>
              <button
                class="btn-toggle"
                class:active={activeTab.format === 'minified'}
                title="Collapse to a single line -- display only, never writes to the file"
                on:click={() => setFormat('minified')}
              >
                Minify
              </button>
              {#if formatError}
                <span class="hint format-error">{formatError}</span>
              {/if}
            {/if}
            {#if markInputOpen}
              <form class="mark-input" on:submit|preventDefault={submitMarkInput}>
                <input
                  class="input"
                  type="text"
                  placeholder="Highlight text (or re:regex)"
                  bind:value={markInputValue}
                  bind:this={markInput}
                  on:keydown={(e) => e.key === 'Escape' && ((markInputValue = ''), (markInputOpen = false))}
                  on:blur={submitMarkInput}
                />
              </form>
            {:else}
              <button
                class="btn-toggle"
                class:active={activeTab.markPatterns.length > 0}
                title="Highlight one or more ad hoc patterns, each in its own color"
                on:click={openMarkInput}
              >
                Highlight
              </button>
            {/if}
            {#each activeTab.markPatterns as mark (mark.id)}
              <span class="mark-chip">
                <span class="mark-dot" style="background: {MARK_SWATCH_COLORS[mark.colorIndex]}"></span>
                <span class="mark-text">{mark.pattern}</span>
                <button class="mark-remove" title="Remove" on:click={() => removeMark(mark.id)}>×</button>
              </span>
            {/each}
            {#if activeTab.markPatterns.length > 0}
              <button class="link" on:click={clearMarks}>Clear highlights</button>
            {/if}
            {#if goToLineOpen}
              <form class="go-to-line" on:submit|preventDefault={submitGoToLine}>
                <input
                  class="input"
                  type="number"
                  min="1"
                  placeholder="Line #"
                  bind:value={goToLineValue}
                  bind:this={goToLineInput}
                  on:keydown={(e) => e.key === 'Escape' && (goToLineOpen = false)}
                  on:blur={() => (goToLineOpen = false)}
                />
              </form>
            {:else}
              <button class="btn-toggle" on:click={openGoToLine}>Go to line</button>
            {/if}
          </div>
          <div class="toolbar-right">
            {#if copyStatus}
              <span class="hint copy-status">{copyStatus === 'copied' ? 'Copied' : 'Nothing selected'}</span>
            {/if}
            <button class="link" title="Copy selected lines with line numbers" on:click={doCopySelectedLines}>
              Copy lines
            </button>
            <button class="link" title="Reload from source" on:click={reloadActiveTab}>
              ⟳ Reload
            </button>
            <button
              class="link"
              class:active-marker={activeTab.bookmarks.length > 0}
              title="Toggle bookmark on current line"
              on:click={toggleBookmark}
            >
              ⚑ Bookmark
            </button>
            {#if activeTab.bookmarks.length > 0}
              <button
                class="link"
                title="Previous bookmark"
                on:click={() => paneRef?.jumpToPreviousBookmark()}
              >
                ‹ mark
              </button>
              <button class="link" title="Next bookmark" on:click={() => paneRef?.jumpToNextBookmark()}>
                mark ›
              </button>
            {/if}
            {#if severityPatterns.length > 0}
              <button
                class="link"
                title="Previous problem"
                on:click={() => paneRef?.jumpToPreviousProblem()}
              >
                ‹ problem
              </button>
              <button
                class="link"
                title="Next problem"
                on:click={() => paneRef?.jumpToNextProblem()}
              >
                problem ›
              </button>
            {/if}
            <a
              class="link"
              href={`/sources/${sourceId}/download?${new URLSearchParams({ path: activeTab.path, ...(activeTab.member ? { member: activeTab.member } : {}) }).toString()}`}
              target="_blank"
              rel="noreferrer"
            >
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M12 3v12" />
                <path d="m6 11 6 6 6-6" />
                <path d="M5 21h14" />
              </svg>
              Download
            </a>
          </div>
        </div>
        <CodeMirrorPane
          bind:this={paneRef}
          content={displayContent}
          {severityPatterns}
          filename={activeTab.name}
          wrapEnabled={activeTab.wrapEnabled}
          showWhitespace={activeTab.showWhitespace}
          bookmarks={activeTab.bookmarks}
          markPatterns={activeTab.markPatterns}
        />
        {#if findAllOpen}
          <FindInDocumentPanel
            content={displayContent}
            on:jump={(e) => paneRef?.scrollToLine(e.detail.line)}
            on:close={() => (findAllOpen = false)}
          />
        {/if}
      {:else}
        <div class="empty-state">Select a file from the tree to view it.</div>
      {/if}
    </section>
  </div>
{/if}

<style>
  .page {
    padding: 1.75rem 2rem;
  }
  .picker-columns {
    display: flex;
    gap: 2rem;
    align-items: flex-start;
  }
  .picker-column {
    display: flex;
    flex-direction: column;
    gap: 0.9rem;
    min-width: 0;
  }
  .picker-column.recent {
    flex: 0 0 320px;
  }
  .picker-column.all {
    flex: 1;
    min-width: 0;
  }
  .picker-column h1 {
    font-size: 1.1rem;
    margin: 0;
    color: var(--text);
  }
  .all-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    flex-wrap: wrap;
  }
  .search-box {
    width: 280px;
    max-width: 100%;
  }
  .picker-column ul {
    list-style: none;
    padding: 0;
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }
  .picker-column :global(.card) {
    border: 1px solid var(--border-soft);
    border-radius: var(--radius-sm);
    background: var(--bg-elevated);
  }
  .picker-column :global(.card:hover) {
    border-color: var(--accent-border);
  }
  @media (max-width: 900px) {
    .picker-columns {
      flex-direction: column;
    }
    .picker-column.recent {
      flex: 0 0 auto;
      width: 100%;
    }
  }
  .viewer {
    flex: 1;
    display: flex;
    min-height: 0;
  }
  .tree {
    width: 280px;
    flex: 0 0 280px;
    border-right: 1px solid var(--border-soft);
    background: var(--bg-elevated);
    display: flex;
    flex-direction: column;
    min-height: 0;
  }
  .tree-header {
    padding: 0.7rem 0.9rem;
    border-bottom: 1px solid var(--border-soft);
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
    font-size: 0.85rem;
  }
  .tree-header strong {
    color: var(--text);
  }
  .tree-body {
    flex: 1;
    overflow: auto;
    padding: 0.4rem 0;
  }
  button.link,
  a.link {
    border: none;
    background: none;
    color: var(--accent-hover);
    cursor: pointer;
    padding: 0;
    font-size: 0.8rem;
    text-align: left;
    text-decoration: none;
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    white-space: nowrap;
  }
  .editor-area {
    flex: 1;
    display: flex;
    flex-direction: column;
    min-width: 0;
    min-height: 0;
    background: var(--bg);
  }
  .tabs {
    display: flex;
    background: var(--bg-elevated);
    border-bottom: 1px solid var(--border-soft);
    overflow-x: auto;
  }
  .tab {
    display: flex;
    align-items: center;
    gap: 0.3rem;
    border-right: 1px solid var(--border-soft);
    white-space: nowrap;
  }
  .tab.active {
    background: var(--bg);
  }
  .tab-label {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    border: none;
    background: none;
    padding: 0.5rem 0 0.5rem 0.9rem;
    font-size: 0.8rem;
    cursor: pointer;
    color: var(--text-muted);
  }
  .tab.active .tab-label {
    color: var(--text);
  }
  .file-icon {
    width: 13px;
    height: 13px;
    flex: 0 0 auto;
    color: var(--text-faint);
  }
  .tab .close {
    border: none;
    background: none;
    padding: 0.5rem 0.9rem 0.5rem 0;
    font-size: 0.85rem;
    cursor: pointer;
    color: var(--text-faint);
  }
  .tab .close:hover {
    color: var(--danger);
  }
  .pane-toolbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    row-gap: 0.4rem;
    padding: 0.4rem 0.9rem;
    background: var(--bg-elevated);
    border-bottom: 1px solid var(--border-soft);
    font-size: 0.78rem;
  }
  .toolbar-left {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    row-gap: 0.4rem;
    gap: 0.75rem;
  }
  .pane-toolbar .hint {
    padding: 0;
    color: var(--text-faint);
    white-space: nowrap;
  }
  .btn-toggle {
    border: 1px solid var(--border);
    background: transparent;
    color: var(--text-muted);
    border-radius: 999px;
    padding: 0.15rem 0.65rem;
    font-size: 0.72rem;
    font-weight: 600;
    cursor: pointer;
    white-space: nowrap;
  }
  .btn-toggle:hover {
    border-color: var(--accent-border);
    color: var(--text);
  }
  .btn-toggle.active {
    background: var(--accent-soft);
    border-color: var(--accent-border);
    color: var(--accent-hover);
  }
  .pane-toolbar .link svg {
    width: 13px;
    height: 13px;
  }
  .toolbar-right {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    row-gap: 0.4rem;
    gap: 1rem;
  }
  .toolbar-right button.link {
    font: inherit;
  }
  .toolbar-right button.link.active-marker {
    color: var(--accent-hover);
  }
  .copy-status {
    padding: 0;
    color: var(--text-faint);
    font-style: italic;
  }
  .go-to-line .input {
    width: 5.5rem;
    padding: 0.15rem 0.5rem;
    font-size: 0.75rem;
  }
  .mark-input .input {
    width: 11rem;
    padding: 0.15rem 0.5rem;
    font-size: 0.75rem;
  }
  .mark-chip {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    border: 1px solid var(--border);
    border-radius: 999px;
    padding: 0.1rem 0.2rem 0.1rem 0.5rem;
    font-size: 0.72rem;
    color: var(--text-muted);
    max-width: 12rem;
  }
  .mark-dot {
    flex: 0 0 auto;
    width: 8px;
    height: 8px;
    border-radius: 50%;
  }
  .mark-text {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .mark-remove {
    flex: 0 0 auto;
    border: none;
    background: none;
    color: var(--text-faint);
    cursor: pointer;
    font-size: 0.85rem;
    padding: 0 0.35rem;
    line-height: 1;
  }
  .mark-remove:hover {
    color: var(--danger);
  }
  .format-error {
    color: var(--danger);
  }
  .empty-state {
    flex: 1;
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--text-faint);
    font-size: 0.9rem;
  }
  .hint {
    padding: 0.5rem 0.9rem;
    font-size: 0.8rem;
    color: var(--text-faint);
  }
  .error {
    color: var(--danger);
    padding: 0.5rem 0.9rem;
  }
</style>
