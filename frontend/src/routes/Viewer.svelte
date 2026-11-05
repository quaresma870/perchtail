<script lang="ts">
  import { onDestroy, onMount, tick } from 'svelte'
  import { push, router } from 'svelte-spa-router'
  import { api } from '../lib/api'
  import ConnectionCard from '../lib/components/ConnectionCard.svelte'
  import ConnectionsTree from '../lib/components/ConnectionsTree.svelte'
  import SourceNode from '../lib/components/SourceNode.svelte'
  import CodeMirrorPane from '../lib/components/CodeMirrorPane.svelte'
  import DiffPane from '../lib/components/DiffPane.svelte'
  import FindInDocumentPanel from '../lib/components/FindInDocumentPanel.svelte'
  import { buildConnectionTree } from '../lib/connection-tree'
  import { filterConnections } from '../lib/connection-filter'
  import { readFetchFailure, type FetchProblem } from '../lib/fetch-errors'
  import { canFormat, formatContent } from '../lib/format-content'
  import { languageForFilename } from '../lib/file-language'
  import { MARK_SWATCH_COLORS, nextMarkColorIndex, type MarkPattern } from '../lib/mark-highlighting'
  import { parsePatternInput } from '../lib/rule-format'
  import { systemSettings } from '../lib/settings'
  import { tabKey } from '../lib/tab-key'
  import type { SeverityPattern, Source } from '../lib/types'
  import { explorerExpanded, setExpanded, sourceHue, viewerState, type Tab } from '../lib/viewer-tabs'

  /** The viewer is one explorer across every source the user can see
   * (#162): machines expand in place in the sidebar, and tabs from
   * several sources stay open side by side. Tabs live in `viewerState`
   * (lib/viewer-tabs.ts), so they also survive visiting Search or
   * Settings and coming back. `/viewer/:sourceId` only expands and
   * reveals that source (plus `?path=&line=` to open a file, from
   * Search). */
  export let params: { sourceId?: string } = {}

  const FOLLOW_INTERVAL_MS = 2000
  const VIEW_PREF_KEY = 'perchtail.connectionsView'

  const focusSourceId = params.sourceId ? Number(params.sourceId) : null

  let recentSources: Source[] = []
  let allSources: Source[] = []
  let connectionsQuery = ''
  let loading = true
  let loadError = ''
  // Per-source severity patterns, fetched on first use for that source.
  let severityBySource: Record<number, SeverityPattern[]> = {}

  // A failed open/reload/compare shows here, above the editor, with the
  // server's reason -- it never replaces the explorer (#160).
  let notice: (FetchProblem & { retry?: () => void }) | null = null

  let paneRef: CodeMirrorPane | null = null
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
  let compareArmed = false
  let compareWith: { name: string; content: string } | null = null
  let compareKey = 0

  $: tabs = $viewerState.tabs
  $: activeKey = $viewerState.activeKey
  $: activeKey, (formatError = ''), (compareWith = null), (compareArmed = false)
  $: activeTab = tabs.find((t) => t.key === activeKey) ?? null
  $: activeSource = activeTab ? allSources.find((s) => s.id === activeTab.sourceId) ?? null : null
  $: canDownloadActive = activeSource?.my_capabilities?.includes('download') ?? false
  $: activeSeverityPatterns = activeTab ? (severityBySource[activeTab.sourceId] ?? []) : []
  $: displayContent = activeTab
    ? (formatContent(activeTab.content, languageForFilename(activeTab.name), activeTab.format) ??
      activeTab.content)
    : ''
  $: sourceNames = new Set(tabs.map((t) => t.sourceId)).size > 1

  $: filteredAllSources = filterConnections(allSources, connectionsQuery)

  // List or tree: the admin's default (Settings -> System), and -- when
  // switching is allowed -- each browser remembers its own last choice (#156).
  let connectionsView: 'list' | 'tree' = 'list'
  let viewChosenLocally = false
  $: switchable = $systemSettings.connections_view_switchable
  $: if (!switchable || !viewChosenLocally) connectionsView = $systemSettings.connections_view_default

  function chooseView(view: 'list' | 'tree') {
    connectionsView = view
    viewChosenLocally = true
    try {
      localStorage.setItem(VIEW_PREF_KEY, view)
    } catch {
      // storage unavailable: the choice just isn't remembered
    }
  }

  $: expandedTreeIds = new Set(
    [...$explorerExpanded].filter((k) => k.startsWith('tree:')).map((k) => k.slice(5)),
  )
  $: connectionTree = buildConnectionTree(filteredAllSources)

  function toggleTreeNode(id: string) {
    setExpanded(`tree:${id}`, !expandedTreeIds.has(id))
  }

  function revealInTree(source: Source) {
    setExpanded(`tree:${source.customer_id !== null ? `customer:${source.customer_id}` : 'unassigned'}`, true)
    for (const entry of source.folder_path) setExpanded(`tree:folder:${entry.id}`, true)
  }

  function setState(patch: Partial<{ tabs: Tab[]; activeKey: string | null }>) {
    viewerState.update((state) => ({ ...state, ...patch }))
  }

  async function loadSources() {
    loading = true
    loadError = ''
    try {
      allSources = await api.get<Source[]>('/sources')
    } catch {
      loadError = 'Failed to load sources'
    }
    try {
      recentSources = await api.get<Source[]>('/sources/recent')
    } catch {
      recentSources = []
    }
    loading = false
  }

  async function ensureSeverityPatterns(sourceId: number) {
    if (severityBySource[sourceId] !== undefined) return
    severityBySource = { ...severityBySource, [sourceId]: [] }
    try {
      const patterns = await api.get<SeverityPattern[]>(
        `/sources/${sourceId}/severity-patterns/effective`,
      )
      severityBySource = { ...severityBySource, [sourceId]: patterns }
    } catch {
      // no highlighting for this source; browsing still works
    }
  }

  function openUrl(sourceId: number, path: string, member: string | null): string {
    const fetchParams = new URLSearchParams({ path })
    if (member) fetchParams.set('member', member)
    return `/sources/${sourceId}/open?${fetchParams.toString()}`
  }

  function releaseScratch(sourceId: number, path: string, member: string | null) {
    api.post(`/sources/${sourceId}/close`, { path, member }).catch(() => {})
  }

  async function openFile(
    sourceId: number,
    sourceName: string,
    path: string,
    member: string | null,
    name: string,
  ): Promise<boolean> {
    const key = tabKey(sourceId, path, member)
    if (tabs.some((t) => t.key === key)) {
      setState({ activeKey: key })
      return true
    }
    try {
      const response = await fetch(openUrl(sourceId, path, member), { credentials: 'include' })
      if (!response.ok) {
        const problem = await readFetchFailure(response, name)
        notice = {
          ...problem,
          retry: problem.retryable
            ? () => openFile(sourceId, sourceName, path, member, name)
            : undefined,
        }
        return false
      }
      const content = await response.text()
      const tab: Tab = {
        key,
        sourceId,
        sourceName,
        path,
        member,
        name,
        content,
        scratchKey: response.headers.get('x-scratch-key'),
        wrapEnabled: false,
        showWhitespace: false,
        bookmarks: [],
        markPatterns: [],
        format: 'none',
        following: false,
      }
      notice = null
      ensureSeverityPatterns(sourceId)
      setState({ tabs: [...tabs, tab], activeKey: key })
      return true
    } catch {
      notice = {
        message: `${name}: the server could not be reached.`,
        retryable: true,
        retry: () => openFile(sourceId, sourceName, path, member, name),
      }
      return false
    }
  }

  async function handleOpen(
    event: CustomEvent<{ sourceId: number; sourceName: string; path: string; member: string | null; name: string }>,
  ) {
    const { sourceId, sourceName, path, member, name } = event.detail
    if (compareArmed) {
      await pickCompareTarget(sourceId, path, member, name)
      return
    }
    await openFile(sourceId, sourceName, path, member, name)
  }

  // "Compare": a one-shot snapshot of another file -- from any source, so
  // the same log on two servers can be diffed (#162).
  async function pickCompareTarget(sourceId: number, path: string, member: string | null, name: string) {
    try {
      const response = await fetch(openUrl(sourceId, path, member), { credentials: 'include' })
      if (!response.ok) {
        notice = await readFetchFailure(response, name)
        compareArmed = false
        return
      }
      const content = await response.text()
      const label = `${sourceLabel(sourceId)} › ${name}`
      compareWith = { name: label, content }
      compareArmed = false
      compareKey += 1
      if (response.headers.get('x-scratch-key')) releaseScratch(sourceId, path, member)
    } catch {
      notice = { message: `${name}: the server could not be reached.`, retryable: true }
      compareArmed = false
    }
  }

  function sourceLabel(sourceId: number): string {
    return allSources.find((s) => s.id === sourceId)?.name ?? `source #${sourceId}`
  }

  function handleTabClick(tab: Tab) {
    if (compareArmed) {
      compareArmed = false
      if (tab.key !== activeKey) {
        const label = `${tab.sourceName} › ${tab.name}`
        compareWith = { name: label, content: tab.content }
        compareKey += 1
      }
      return
    }
    setState({ activeKey: tab.key })
  }

  async function openFromDeepLink(sourceId: number, path: string, line: number | null) {
    const name = path.split('/').pop() ?? path
    const opened = await openFile(sourceId, sourceLabel(sourceId), path, null, name)
    if (opened && line !== null) {
      await tick()
      paneRef?.scrollToLine(line)
    }
  }

  function closeTab(tab: Tab) {
    const remaining = tabs.filter((t) => t.key !== tab.key)
    setState({
      tabs: remaining,
      activeKey:
        activeKey === tab.key ? (remaining.length > 0 ? remaining[remaining.length - 1].key : null) : activeKey,
    })
    if (tab.scratchKey) releaseScratch(tab.sourceId, tab.path, tab.member)
  }

  function updateActiveTab(patch: Partial<Tab>) {
    if (!activeTab) return
    const key = activeTab.key
    setState({ tabs: tabs.map((t) => (t.key === key ? { ...t, ...patch } : t)) })
  }

  function toggleWrap() {
    if (activeTab) updateActiveTab({ wrapEnabled: !activeTab.wrapEnabled })
  }

  function toggleShowWhitespace() {
    if (activeTab) updateActiveTab({ showWhitespace: !activeTab.showWhitespace })
  }

  function toggleBookmark() {
    if (!activeTab || !paneRef) return
    const line = paneRef.currentLine()
    const bookmarks = activeTab.bookmarks.includes(line)
      ? activeTab.bookmarks.filter((l) => l !== line)
      : [...activeTab.bookmarks, line].sort((a, b) => a - b)
    updateActiveTab({ bookmarks })
  }

  async function openMarkInput() {
    markInputOpen = true
    markInputValue = ''
    await tick()
    markInput?.focus()
  }

  function submitMarkInput() {
    // Clearing first makes the blur that closing the form fires a no-op,
    // so Enter doesn't add the same mark twice.
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
    if (activeTab) updateActiveTab({ markPatterns: activeTab.markPatterns.filter((m) => m.id !== id) })
  }

  function clearMarks() {
    updateActiveTab({ markPatterns: [] })
  }

  // Bookmarks are line numbers in what's displayed, so any format change
  // clears them rather than letting them drift.
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

  // Re-fetch the active tab in place (Reload, and every Follow tick); the
  // previous scratch reference is released once the new one lands.
  async function reloadActiveTab() {
    if (!activeTab) return
    const tab = activeTab
    try {
      const response = await fetch(openUrl(tab.sourceId, tab.path, tab.member), {
        credentials: 'include',
      })
      if (!response.ok) {
        const problem = await readFetchFailure(response, tab.name)
        notice = { ...problem, retry: problem.retryable ? reloadActiveTab : undefined }
        return
      }
      const content = await response.text()
      const scratchKey = response.headers.get('x-scratch-key')
      setState({ tabs: tabs.map((t) => (t.key === tab.key ? { ...t, content, scratchKey } : t)) })
      if (tab.scratchKey) releaseScratch(tab.sourceId, tab.path, tab.member)
    } catch {
      notice = { message: `${tab.name}: the server could not be reached.`, retryable: true, retry: reloadActiveTab }
    }
  }

  function toggleFollow() {
    if (activeTab) updateActiveTab({ following: !activeTab.following })
  }

  let followTimer: ReturnType<typeof setInterval> | null = null
  let followPolling = false

  function stopFollowTimer() {
    if (followTimer) {
      clearInterval(followTimer)
      followTimer = null
    }
  }

  async function followTick() {
    if (followPolling) return
    followPolling = true
    try {
      await reloadActiveTab()
    } finally {
      followPolling = false
    }
  }

  $: {
    stopFollowTimer()
    if (activeTab?.following) {
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

  onMount(async () => {
    try {
      const stored = localStorage.getItem(VIEW_PREF_KEY)
      if (stored === 'list' || stored === 'tree') {
        connectionsView = stored
        viewChosenLocally = true
      }
    } catch {
      // storage unavailable
    }
    window.addEventListener('keydown', handleKeydown)
    await loadSources()
    for (const tab of tabs) ensureSeverityPatterns(tab.sourceId)
    if (focusSourceId !== null) {
      const focused = allSources.find((s) => s.id === focusSourceId)
      if (focused) revealInTree(focused)
      const deepLink = new URLSearchParams(router.querystring ?? '')
      const deepLinkPath = deepLink.get('path')
      if (deepLinkPath) {
        const line = deepLink.get('line')
        openFromDeepLink(focusSourceId, deepLinkPath, line ? Number(line) : null)
      }
    }
  })

  onDestroy(() => {
    // Tabs stay open in viewerState; only this view's timers stop.
    window.removeEventListener('keydown', handleKeydown)
    if (copyStatusTimer) clearTimeout(copyStatusTimer)
    stopFollowTimer()
  })
</script>

<div class="viewer">
  <aside class="explorer">
    <div class="explorer-header">
      <input
        class="input search-box"
        type="search"
        placeholder="Search by folder, customer, or host…"
        bind:value={connectionsQuery}
      />
      {#if switchable}
        <div class="view-toggle" role="group" aria-label="View">
          <button
            class="view-toggle-btn"
            class:active={connectionsView === 'list'}
            on:click={() => chooseView('list')}
          >
            List
          </button>
          <button
            class="view-toggle-btn"
            class:active={connectionsView === 'tree'}
            on:click={() => chooseView('tree')}
          >
            Tree
          </button>
        </div>
      {/if}
    </div>
    <div class="explorer-body">
      {#if loading}
        <p class="hint">Loading…</p>
      {:else if loadError}
        <p class="error">{loadError}</p>
      {:else if filteredAllSources.length === 0}
        <p class="hint">
          {connectionsQuery.trim() ? 'No connections match that search.' : 'No sources visible to your account.'}
        </p>
      {:else if connectionsView === 'tree'}
        <div class="connections-tree-body">
          {#each connectionTree as node (node.id)}
            <ConnectionsTree
              {node}
              expandedIds={expandedTreeIds}
              forceExpand={connectionsQuery.trim().length > 0}
              {activeKey}
              {focusSourceId}
              on:toggle={(e) => toggleTreeNode(e.detail.id)}
              on:open={handleOpen}
            />
          {/each}
        </div>
      {:else}
        <div class="connections-list">
          {#each filteredAllSources as s (s.id)}
            <SourceNode
              source={s}
              {activeKey}
              initiallyExpanded={focusSourceId === s.id}
              on:open={handleOpen}
            />
          {/each}
        </div>
      {/if}
    </div>
  </aside>

  <section class="editor-area">
    {#if tabs.length > 0}
      <div class="tabs">
        {#each tabs as tab (tab.key)}
          <div class="tab" class:active={tab.key === activeKey} title={`${tab.sourceName} › ${tab.member ? `${tab.path} › ${tab.member}` : tab.path}`}>
            <button class="tab-label" on:click={() => handleTabClick(tab)}>
              <span class="source-dot" style="background: hsl({sourceHue(tab.sourceId)} 65% 55%)"></span>
              {#if sourceNames}
                <span class="tab-source">{tab.sourceName} ›</span>
              {/if}
              {tab.name}
            </button>
            <button class="close" on:click={() => closeTab(tab)} aria-label={`Close ${tab.name}`}
              >×</button
            >
          </div>
        {/each}
      </div>
    {/if}
    {#if notice}
      <div class="notice" role="alert">
        <span>{notice.message}</span>
        <span class="notice-actions">
          {#if notice.retry}
            <button class="link" on:click={() => notice?.retry?.()}>Try again</button>
          {/if}
          <button class="link" aria-label="Dismiss" on:click={() => (notice = null)}>✕</button>
        </span>
      </div>
    {/if}
      {#if activeTab && compareWith}
        <div class="pane-toolbar">
          <div class="toolbar-left">
            <span class="hint">
              Comparing <strong>{activeTab.sourceName} › {activeTab.name}</strong> ↔
              <strong>{compareWith.name}</strong>
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
            leftLabel={`${activeTab.sourceName} › ${activeTab.name}`}
            rightLabel={compareWith.name}
          />
        {/key}
      {:else if activeTab}
        <div class="pane-toolbar">
          <div class="toolbar-left">
            <button
              class="btn-toggle"
              title="Search this file (Ctrl/Cmd+F). Ctrl/Cmd+G goes to a line."
              on:click={() => paneRef?.openSearch()}
            >
              ⌕ Find
            </button>
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
            {#if activeSeverityPatterns.length > 0}
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
            {#if canDownloadActive}
            <a
              class="link"
              href={`/sources/${activeTab.sourceId}/download?${new URLSearchParams({ path: activeTab.path, ...(activeTab.member ? { member: activeTab.member } : {}) }).toString()}`}
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
            {/if}
          </div>
        </div>
        <CodeMirrorPane
          bind:this={paneRef}
          content={displayContent}
          severityPatterns={activeSeverityPatterns}
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
      <div class="empty-state">
        <div class="empty-inner">
          <p>Expand a machine in the explorer and pick a file to view it.</p>
          {#if recentSources.length > 0}
            <h2>Recent</h2>
            <ul class="recent">
              {#each recentSources as s (s.id)}
                <li>
                  <ConnectionCard source={s} on:click={() => push(`/viewer/${s.id}`)} />
                </li>
              {/each}
            </ul>
          {/if}
        </div>
      </div>
    {/if}
  </section>
</div>

<style>
  .viewer {
    flex: 1;
    display: flex;
    min-height: 0;
    height: 100%;
    overflow: hidden;
  }
  .explorer {
    flex: 0 0 320px;
    display: flex;
    flex-direction: column;
    min-height: 0;
    border-right: 1px solid var(--border-soft);
    background: var(--bg-elevated);
  }
  .explorer-header {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    padding: 0.75rem;
    border-bottom: 1px solid var(--border-soft);
  }
  .search-box {
    width: 100%;
  }
  .view-toggle {
    display: flex;
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    overflow: hidden;
    align-self: flex-start;
  }
  .view-toggle-btn {
    border: none;
    background: transparent;
    color: var(--text-muted);
    padding: 0.25rem 0.8rem;
    font-size: 0.78rem;
    cursor: pointer;
  }
  .view-toggle-btn.active {
    background: var(--accent-soft);
    color: var(--accent-hover);
  }
  .explorer-body {
    flex: 1;
    min-height: 0;
    overflow: auto;
    padding: 0.4rem;
  }
  .connections-tree-body,
  .connections-list {
    display: flex;
    flex-direction: column;
  }
  .source-dot {
    flex: 0 0 auto;
    width: 8px;
    height: 8px;
    border-radius: 50%;
  }
  .tab-source {
    color: var(--text-faint);
  }
  .notice {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    padding: 0.5rem 0.9rem;
    background: var(--danger-soft, rgba(248, 113, 113, 0.12));
    border-bottom: 1px solid var(--danger, #f87171);
    color: var(--text);
    font-size: 0.82rem;
  }
  .notice-actions {
    display: flex;
    gap: 0.75rem;
    flex: 0 0 auto;
  }
  .empty-inner {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
    max-width: 30rem;
    width: 100%;
    padding: 1rem;
  }
  .empty-inner h2 {
    font-size: 0.85rem;
    color: var(--text-muted);
    margin: 0.5rem 0 0;
  }
  .recent {
    list-style: none;
    padding: 0;
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
  }
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
