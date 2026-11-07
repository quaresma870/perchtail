<script lang="ts">
  import { onDestroy, onMount, tick } from 'svelte'
  import { push, router } from 'svelte-spa-router'
  import { api } from '../lib/api'
  import ConnectionCard from '../lib/components/ConnectionCard.svelte'
  import ConnectionsTree from '../lib/components/ConnectionsTree.svelte'
  import SourceNode from '../lib/components/SourceNode.svelte'
  import ScopeResults from '../lib/components/ScopeResults.svelte'
  import {
    advancedFilter,
    clearScopes,
    removeScope,
    scopeKey,
    scopes,
  } from '../lib/explorer-scope'
  import CodeMirrorPane from '../lib/components/CodeMirrorPane.svelte'
  import DiffPane from '../lib/components/DiffPane.svelte'
  import FindInDocumentPanel from '../lib/components/FindInDocumentPanel.svelte'
  import { buildConnectionTree } from '../lib/connection-tree'
  import { filterConnections } from '../lib/connection-filter'
  import { readFetchFailure, type FetchProblem } from '../lib/fetch-errors'
  import { trackedFetch } from '../lib/server-status'
  import { parentPath, refreshFolder, startAutoRefresh, stopAutoRefresh } from '../lib/explorer-refresh'
  import { connectionsViewPreference, migrateLegacyViewChoice, savePreference } from '../lib/preferences'
  import {
    countLines,
    FOLLOW_INTERVALS_MS,
    followUrl,
    formatInterval,
    loadFollowInterval,
    readFollowHeaders,
    saveFollowInterval,
  } from '../lib/follow'
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

  // How often Follow polls (#254): 1 s to 30 s, remembered per browser.
  let followIntervalMs = loadFollowInterval()

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

  // Advanced filter (#274): the box filters files in the ticked sources and
  // folders instead of connections; the explorer lists every source for
  // picking, and shows the matching files once something is ticked.
  let fileQuery = ''
  // While ticking, the tree stays; the files show on "Show the files" or as
  // soon as something is typed in the filter.
  let pickingScopes = true
  $: showScoped = $advancedFilter && $scopes.length > 0 && !pickingScopes
  $: if ($scopes.length === 0) pickingScopes = true
  const scopeLabel = (sourceId: number, path: string) =>
    `${sourceLabel(sourceId)}${path ? ` › ${path}` : ''}`

  $: filteredAllSources = filterConnections(allSources, $advancedFilter ? '' : connectionsQuery)

  // List or tree: the admin's default (Settings -> System), and -- when
  // switching is allowed -- each user's own choice, kept on the server so
  // it follows them (#156, #273).
  $: switchable = $systemSettings.connections_view_switchable
  $: connectionsView = switchable ? $connectionsViewPreference : $systemSettings.connections_view_default

  function chooseView(view: 'list' | 'tree') {
    savePreference('connections_view', view)
  }

  // Open folders re-list themselves (#272); the interval is a System setting.
  $: startAutoRefresh($systemSettings.explorer_refresh_seconds)

  // Source statuses (#276) are re-read with the same rhythm (every minute
  // when auto-refresh is off), quietly: no "Loading…", and nothing while
  // the browser tab is hidden.
  let statusTimer: ReturnType<typeof setInterval> | null = null
  let statusSeconds = 0
  $: restartStatusTimer($systemSettings.explorer_refresh_seconds || 60)
  function restartStatusTimer(seconds: number) {
    if (statusTimer && seconds === statusSeconds) return
    if (statusTimer) clearInterval(statusTimer)
    statusSeconds = seconds
    statusTimer = setInterval(async () => {
      if (document.hidden) return
      try {
        allSources = await api.get<Source[]>('/sources')
      } catch {
        // the banner (server-status) says when PerchTail doesn't answer
      }
    }, seconds * 1000)
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
      const response = await trackedFetch(openUrl(sourceId, path, member), { credentials: 'include' })
      if (!response.ok) {
        const problem = await readFetchFailure(response, name)
        // Gone from the source (#272): its folder is listed again, so it
        // leaves the tree.
        if (problem.missing) refreshFolder(sourceId, parentPath(path))
        notice = {
          ...problem,
          retry: problem.retryable
            ? () => openFile(sourceId, sourceName, path, member, name)
            : undefined,
        }
        return false
      }
      const bytes = await response.arrayBuffer()
      // Streaming, so a character the file's end splits is completed by
      // the first Follow answer (#254) instead of turning into two U+FFFD.
      const decoder = new TextDecoder()
      const content = decoder.decode(bytes, { stream: true })
      followDecoders.set(key, decoder)
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
        byteSize: bytes.byteLength,
        followHead: response.headers.get('x-follow-head'),
        // A .gz file or an archive member is decompressed on the server: only
        // the whole of it can be reloaded.
        followMode: member || name.toLowerCase().endsWith('.gz') ? 'reload' : 'incremental',
        newLines: 0,
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
      const response = await trackedFetch(openUrl(sourceId, path, member), { credentials: 'include' })
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
    followDecoders.delete(tab.key)
    followStarted.delete(tab.key)
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

  // Re-fetch a tab in place (Reload; Follow when only the whole file can be
  // fetched, or after a rotation); the previous scratch reference is
  // released once the new one lands.
  async function reloadActiveTab() {
    if (activeTab) await reloadTab(activeTab)
  }

  async function reloadTab(tab: Tab) {
    try {
      const response = await trackedFetch(openUrl(tab.sourceId, tab.path, tab.member), {
        credentials: 'include',
      })
      if (!response.ok) {
        const problem = await readFetchFailure(response, tab.name)
        if (problem.missing) {
          markMissing(tab)
          return
        }
        notice = { ...problem, retry: problem.retryable ? reloadActiveTab : undefined }
        return
      }
      const bytes = await response.arrayBuffer()
      const decoder = new TextDecoder()
      const content = decoder.decode(bytes, { stream: true })
      const scratchKey = response.headers.get('x-scratch-key')
      const followHead = response.headers.get('x-follow-head')
      followDecoders.set(tab.key, decoder)
      setState({
        tabs: tabs.map((t) =>
          t.key === tab.key
            ? { ...t, content, scratchKey, byteSize: bytes.byteLength, followHead, newLines: 0, missing: false }
            : t,
        ),
      })
      if (tab.scratchKey) releaseScratch(tab.sourceId, tab.path, tab.member)
    } catch {
      notice = { message: `${tab.name}: the server could not be reached.`, retryable: true, retry: reloadActiveTab }
    }
  }

  function toggleFollow() {
    if (!activeTab) return
    if (activeTab.following) followStarted.delete(activeTab.key)
    updateActiveTab({ following: !activeTab.following, newLines: 0 })
  }

  function setFollowInterval(ms: number) {
    followIntervalMs = ms
    saveFollowInterval(ms)
  }

  // Incremental follow (#254). Per-tab streaming decoders, so a character
  // split across two answers decodes whole, and the tabs whose follow was
  // already announced (audited once, on the first poll). Not reactive.
  const followDecoders = new Map<string, TextDecoder>()
  const followStarted = new Set<string>()
  // A burst bigger than one answer is read in a few more right away.
  const FOLLOW_ROUNDS_PER_TICK = 8

  // The file left the source (#272): Follow stops, the tab keeps what it
  // had and is marked, its folder is listed again, and a note says so once.
  function markMissing(tab: Tab) {
    updateTab(tab.key, { following: false, missing: true })
    followStarted.delete(tab.key)
    refreshFolder(tab.sourceId, parentPath(tab.path))
    notice = {
      message: `${tab.name} no longer exists on the source. The tab keeps what it had; Tail stopped.`,
      retryable: false,
    }
  }

  function updateTab(key: string, patch: Partial<Tab>) {
    setState({ tabs: tabs.map((t) => (t.key === key ? { ...t, ...patch } : t)) })
  }

  async function followIncrementally(tab: Tab) {
    let byteSize = tab.byteSize
    let followHead = tab.followHead
    let appended = ''
    for (let round = 0; round < FOLLOW_ROUNDS_PER_TICK; round++) {
      const start = !followStarted.has(tab.key)
      const response = await trackedFetch(followUrl(tab.sourceId, tab.path, byteSize, followHead, start), {
        credentials: 'include',
      })
      if (response.status === 409) {
        // A .gz file, an archive member or an agent older than 0.6: this
        // tab reloads the whole file from now on.
        followStarted.add(tab.key)
        updateTab(tab.key, { followMode: 'reload' })
        await reloadTab(tab)
        return
      }
      if (!response.ok) {
        const problem = await readFetchFailure(response, tab.name)
        if (problem.missing) markMissing(tab)
        else notice = problem
        return
      }
      followStarted.add(tab.key)
      const answer = readFollowHeaders(response.headers, byteSize)
      if (answer.reset) {
        await reloadTab(tab)
        notice = { message: `${tab.name} was rotated or truncated: reloaded from the start.`, retryable: false }
        return
      }
      const bytes = new Uint8Array(await response.arrayBuffer())
      let decoder = followDecoders.get(tab.key)
      if (!decoder) {
        decoder = new TextDecoder()
        followDecoders.set(tab.key, decoder)
      }
      appended += decoder.decode(bytes, { stream: true })
      byteSize = answer.offset
      followHead = answer.head
      if (!answer.more) break
    }
    // A Reload while this was in flight replaced the content: drop the answer.
    const current = tabs.find((t) => t.key === tab.key)
    if (!current || current.byteSize !== tab.byteSize) return
    const readingAbove = appended !== '' && tab.key === activeKey && !(paneRef?.isAtBottom() ?? true)
    updateTab(tab.key, {
      content: current.content + appended,
      byteSize,
      followHead,
      newLines: readingAbove ? current.newLines + countLines(appended) : current.newLines,
    })
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
    // A hidden browser tab doesn't poll, to spare the sources (#254).
    if (followPolling || document.hidden || !activeTab?.following) return
    followPolling = true
    const tab = activeTab
    try {
      if (tab.followMode === 'incremental') await followIncrementally(tab)
      else await reloadTab(tab)
    } catch {
      notice = { message: `${tab.name}: the server could not be reached.`, retryable: true }
    } finally {
      followPolling = false
    }
  }

  // Restarted only when the followed tab or the interval changes, not on
  // every append.
  $: followedKey = activeTab?.following ? activeTab.key : null
  $: restartFollow(followedKey, followIntervalMs)

  function restartFollow(key: string | null, intervalMs: number) {
    stopFollowTimer()
    if (key === null) return
    followTimer = setInterval(followTick, intervalMs)
    followTick()
  }

  function jumpToEnd() {
    paneRef?.scrollToEnd()
    if (activeTab) updateActiveTab({ newLines: 0 })
  }

  function onReachedBottom() {
    if (activeTab && activeTab.newLines > 0) updateActiveTab({ newLines: 0 })
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
    migrateLegacyViewChoice()
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
    stopAutoRefresh()
    if (statusTimer) clearInterval(statusTimer)
  })
</script>

<div class="viewer">
  <aside class="explorer">
    <div class="explorer-header">
      {#if $advancedFilter}
        <input
          class="input search-box"
          type="search"
          aria-label="Filter files"
          placeholder="Filter files in the ticked sources and folders (text, or a glob like app-*.log)"
          bind:value={fileQuery}
          on:input={() => { if (fileQuery.trim() && $scopes.length > 0) pickingScopes = false }}
        />
      {:else}
        <input
          class="input search-box"
          type="search"
          placeholder="Search by folder, customer, or host…"
          bind:value={connectionsQuery}
        />
      {/if}
      <button
        class="view-toggle-btn advanced-toggle"
        class:active={$advancedFilter}
        aria-pressed={$advancedFilter}
        title="Advanced filter: tick sources and folders, then filter their files by name"
        on:click={() => advancedFilter.update((on) => !on)}
      >
        Advanced filter
      </button>
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
    {#if $advancedFilter}
      <div class="scope-bar">
        {#if $scopes.length === 0}
          <span class="hint">Tick the sources and folders to filter files in.</span>
        {:else}
          {#each $scopes as scope (scopeKey(scope))}
            <span class="scope-chip">
              {scopeLabel(scope.sourceId, scope.path)}
              <button
                class="link"
                aria-label="Remove {scopeLabel(scope.sourceId, scope.path)} from the filter"
                on:click={() => removeScope(scope)}>✕</button
              >
            </span>
          {/each}
          <button class="link" on:click={clearScopes}>Clear all</button>
          <button class="link" on:click={() => (pickingScopes = !pickingScopes)}>
            {pickingScopes ? `Show the files (${$scopes.length})` : 'Add or change'}
          </button>
        {/if}
      </div>
    {/if}
    <div class="explorer-body">
      {#if loading}
        <p class="hint">Loading…</p>
      {:else if loadError}
        <p class="error">{loadError}</p>
      {:else if showScoped}
        <div class="scoped-results">
          {#each $scopes as scope (scopeKey(scope))}
            <ScopeResults
              {scope}
              sourceName={sourceLabel(scope.sourceId)}
              query={fileQuery}
              on:open={handleOpen}
            />
          {/each}
        </div>
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
            <button class="tab-label" class:missing={tab.missing} on:click={() => handleTabClick(tab)}>
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
              aria-pressed={activeTab.following}
              title={activeTab.followMode === 'incremental'
                ? "Show new lines as they're written, like tail -f: fetches only what was appended"
                : "Show new lines as they're written: reloads the whole file (compressed files, archive members, agents older than 0.6)"}
              on:click={toggleFollow}
            >
              Tail
            </button>
            {#if activeTab.following}
              <!-- Only while Tail is on (#283). -->
              <select
                class="follow-interval"
                aria-label="Tail interval"
                title="How often Tail checks for new lines"
                value={followIntervalMs}
                on:change={(e) => setFollowInterval(Number(e.currentTarget.value))}
              >
                {#each FOLLOW_INTERVALS_MS as ms}
                  <option value={ms}>{formatInterval(ms)}</option>
                {/each}
              </select>
            {/if}
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
          on:bottom={onReachedBottom}
        />
        {#if activeTab.newLines > 0}
          <button class="jump-to-end" on:click={jumpToEnd}>
            {activeTab.newLines} new {activeTab.newLines === 1 ? 'line' : 'lines'} — jump to end ↓
          </button>
        {/if}
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
  .advanced-toggle {
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    white-space: nowrap;
  }
  .scope-bar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.35rem 0.5rem;
    padding: 0.4rem 0.75rem;
    border-bottom: 1px solid var(--border-soft);
    font-size: 0.78rem;
  }
  .scope-chip {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    padding: 0.1rem 0.45rem;
    border-radius: 999px;
    background: var(--accent-soft);
    color: var(--text);
    max-width: 100%;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
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
    position: relative;
  }
  .jump-to-end {
    position: absolute;
    right: 1.5rem;
    bottom: 1.25rem;
    z-index: 5;
    padding: 0.4rem 0.8rem;
    border: 1px solid var(--accent);
    border-radius: 999px;
    background: var(--bg-elevated);
    color: var(--text);
    font-size: 0.8rem;
    cursor: pointer;
    box-shadow: 0 4px 14px rgba(0, 0, 0, 0.35);
  }
  .follow-interval {
    background: var(--bg-elevated-2);
    color: var(--text-muted);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    font-size: 0.75rem;
    padding: 0.15rem 0.25rem;
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
  .tab-label.missing {
    text-decoration: line-through;
    opacity: 0.7;
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
