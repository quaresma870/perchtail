<script lang="ts">
  import { onDestroy, onMount, setContext, tick } from 'svelte'
  import { get } from 'svelte/store'
  import { push, router } from 'svelte-spa-router'
  import { api } from '../lib/api'
  import ConnectionCard from '../lib/components/ConnectionCard.svelte'
  import ConnectionsTree from '../lib/components/ConnectionsTree.svelte'
  import SourceNode from '../lib/components/SourceNode.svelte'
  import ScopeResults from '../lib/components/ScopeResults.svelte'
  import SearchResults from '../lib/components/SearchResults.svelte'
  import {
    advancedFilter,
    clearScopes,
    compileMatcher,
    contextFromTree,
    parseQuery,
    isGroupScope,
    isMatcherError,
    removeScope,
    scopeContext,
    scopeKey,
    scopes,
    sourceScopesOf,
    stopSearches,
    type Scope,
  } from '../lib/explorer-scope'
  import CodeMirrorPane from '../lib/components/CodeMirrorPane.svelte'
  import ContextMenu from '../lib/components/ContextMenu.svelte'
  import {
    CHECK_ALL_CONCURRENCY,
    contextMenuIntent,
    EXPLORER_MENUS,
    insideLink,
    isMenuKey,
    runLimited,
    type ExplorerMenu,
    type ExplorerMenus,
    lineLink,
    logLineMenu,
    tabMenu,
    tabPathText,
    type LineMenuRequest,
    type MenuEntry,
  } from '../lib/context-menu'
  import DiffPane from '../lib/components/DiffPane.svelte'
  import FindInDocumentPanel from '../lib/components/FindInDocumentPanel.svelte'
  import FindBar from '../lib/components/FindBar.svelte'
  import OpenProgress from '../lib/components/OpenProgress.svelte'
  import {
    fetchProgress,
    newRequestId,
    nextStatus,
    openInTurn,
    openQueue,
    PROGRESS_DELAY_MS,
    PROGRESS_POLL_MS,
    queuedStatus,
    receivingStatus,
    startedStatus,
    withRequestId,
    type OpenStatus,
  } from '../lib/open-progress'
  import { compileFind, type FindSpec } from '../lib/find-in-document'
  import { countAppended, countMatches, matchIndexAt, type MatchCount } from '../lib/find-count'
  import ToolbarIcon from '../lib/components/ToolbarIcon.svelte'
  import { buildConnectionTree } from '../lib/connection-tree'
  import { readFetchFailure, readFollowFailure, tailStoppedNotice, type FetchProblem } from '../lib/fetch-errors'
  import { trackedFetch } from '../lib/server-status'
  import { parentPath, refreshFolder, startAutoRefresh, stopAutoRefresh } from '../lib/explorer-refresh'
  import { connectionsViewPreference, migrateLegacyViewChoice, savePreference } from '../lib/preferences'
  import {
    appendReplacingPartial,
    countLines,
    decodeWithPartial,
    followUrl,
    readFollowHeaders,
    readFollowStart,
    readRedactionHeaders,
    tailIntervalMs,
  } from '../lib/follow'
  import {
    forgetEditorState,
    formattedContent,
    NO_PATTERNS,
    refreshSeverityPatterns,
    severityBySource,
  } from '../lib/editor-states'
  import { canFormat, formatContent } from '../lib/format-content'
  import { languageForFilename } from '../lib/file-language'
  import { MARK_SWATCH_COLORS, nextMarkColorIndex, type MarkPattern } from '../lib/mark-highlighting'
  import { redactionCountAfterCap, redactionCountLabel } from '../lib/redaction-markers'
  import { slowSeverityPatterns } from '../lib/severity-highlighting'
  import { parsePatternInput } from '../lib/rule-format'
  import { systemSettings } from '../lib/settings'
  import { tabKey } from '../lib/tab-key'
  import type { SeverityPattern, Source } from '../lib/types'
  import {
    closeTabs,
    closeTabsOfHiddenSources as closeHiddenSourceTabs,
    explorerExpanded,
    forgetSeverityWithoutTabs,
    setExpanded,
    sourceHue,
    viewerState,
    type Tab,
  } from '../lib/viewer-tabs'
  import { formatSize } from '../lib/file-details'
  import {
    capTailBuffer,
    countNewlines,
    cutToFirstLine,
    earlierWindow,
    isLargeFile,
    lineNumbersNotFiles,
    loadEarlierBlocked,
    opensFromEnd,
    shiftBookmarks,
    shiftBookmarksDown,
    startNotLoaded,
    tabLimitMessage,
    tabLimitReached,
    TAIL_MAX_LINES,
    tailStartOffset,
  } from '../lib/viewer-limits'

  /** The viewer is one explorer across every source the user can see
   * (#162): machines expand in place in the sidebar, and tabs from
   * several sources stay open side by side. Tabs live in `viewerState`
   * (lib/viewer-tabs.ts), so they also survive visiting Search or
   * Settings and coming back. `/viewer/:sourceId` only expands and
   * reveals that source (plus `?path=&line=` to open a file, from
   * Search). */
  export let params: { sourceId?: string } = {}

  // How often Tail polls (#254, #283): a System setting, 1 to 10 s.
  $: followIntervalMs = tailIntervalMs($systemSettings.tail_interval_seconds)

  const focusSourceId = params.sourceId ? Number(params.sourceId) : null

  let recentSources: Source[] = []
  let allSources: Source[] = []
  let loading = true
  let loadError = ''
  // Severity patterns turned off for taking too long (severity-highlighting.ts);
  // the warning shows those not dismissed yet.
  let dismissedSlowPatterns = 0
  $: newSlowPatterns = $slowSeverityPatterns.slice(dismissedSlowPatterns)

  // A failed open/reload/compare shows here, above the editor, with the
  // server's reason -- it never replaces the explorer (#160).
  let notice: (FetchProblem & { retry?: () => void }) | null = null

  let paneRef: CodeMirrorPane | null = null
  // One find bar for Find and Find all (#299). Its search is highlighted in
  // the editor; Find all lists the matching lines below, for the search as
  // it was when Find all was pressed.
  let findOpen = false
  let findSpec: FindSpec = { query: '', caseSensitive: false, useRegex: false, wholeWord: false }
  let findBar: FindBar | null = null
  let findAllSpec: FindSpec | null = null
  $: findAllOpen = findAllSpec !== null
  $: editorFind = findOpen && findSpec.query ? findSpec : null
  $: findPattern = editorFind ? compileFind(findSpec.query, findSpec) : null

  // "3 of 120": every match counted, per line, in the background.
  let findCount: MatchCount | null = null
  let currentMatch = 0
  let findLines: string[] = []
  let countedText = ''
  let countedPattern: RegExp | null = null
  let stopCount: (() => void) | null = null
  let countTimer: ReturnType<typeof setTimeout> | null = null
  $: restartCount(findPattern, findPattern ? displayContent : '')
  function restartCount(pattern: RegExp | null, text: string) {
    if (countTimer) clearTimeout(countTimer)
    // Tail only adds lines at the end: count those, not the whole file again.
    if (
      pattern !== null &&
      pattern === countedPattern &&
      findCount?.done &&
      text.length > countedText.length &&
      text.startsWith(countedText)
    ) {
      const result = countAppended(findCount, findLines, text.slice(countedText.lastIndexOf('\n') + 1), pattern)
      findLines = result.lines
      findCount = result.count
      countedText = text
      return
    }
    stopCount?.()
    stopCount = null
    findCount = null
    currentMatch = 0
    countedText = ''
    countedPattern = null
    if (!pattern) return
    // Waits for a pause in typing before counting a big file.
    countTimer = setTimeout(() => {
      findLines = text.split('\n')
      countedText = text
      countedPattern = pattern
      stopCount = countMatches(findLines, pattern, (c) => (findCount = { ...c }))
    }, 120)
  }

  async function openFind() {
    // The selection only starts a new search: with the bar open it's
    // usually the current match, which would replace a regex.
    const selected = findOpen ? '' : (paneRef?.selectedText() ?? '')
    if (selected) findSpec = { ...findSpec, query: selected }
    findOpen = true
    await tick()
    findBar?.focus()
  }

  function closeFind() {
    findOpen = false
    restartCount(null, '')
  }

  // The position ("3 of 120") shows once the reader moves to a match, and
  // is kept up to date as the count changes; a new search starts without it.
  let navigated = false
  $: findSpec, (navigated = false)

  function stepFind(forward: boolean) {
    if (!paneRef || !findPattern) return
    if (forward) paneRef.findNext()
    else paneRef.findPrevious()
    navigated = true
    updateCurrentMatch()
  }

  function updateCurrentMatch() {
    const at = paneRef?.selectionStart()
    currentMatch =
      navigated && at && findCount?.done && findPattern
        ? matchIndexAt(findCount, findLines, findPattern, at.line, at.column)
        : 0
  }
  $: findCount, updateCurrentMatch()

  function runFindAll() {
    if (!findSpec.query || !findPattern) return
    findAllSpec = { ...findSpec }
  }
  let goToLineOpen = false
  let goToLineValue = ''
  let goToLineInput: HTMLInputElement | null = null
  let markInputOpen = false
  let markInputValue = ''
  let markInput: HTMLInputElement | null = null
  let copyStatus: '' | 'copied' | 'empty' | 'failed' = ''
  let copyStatusTimer: ReturnType<typeof setTimeout> | null = null
  let formatError = ''
  let compareArmed = false
  // `partial`: another tab that holds only the end of its file.
  let compareWith: { name: string; content: string; partial?: boolean } | null = null
  let compareKey = 0

  $: tabs = $viewerState.tabs
  $: activeKey = $viewerState.activeKey
  $: activeKey, (formatError = ''), (compareWith = null), (compareArmed = false)
  $: activeTab = tabs.find((t) => t.key === activeKey) ?? null
  $: activeSource = activeTab ? allSources.find((s) => s.id === activeTab.sourceId) ?? null : null
  $: canDownloadActive = activeSource?.my_capabilities?.includes('download') ?? false
  // Redaction (#382, #249): a file the viewer got redacted is downloaded
  // redacted too, unless downloads are left out of redaction.
  $: downloadLabel = activeTab?.redacted && $systemSettings.redaction_downloads ? 'Download (redacted)' : 'Download'
  // The same list for as long as the source's patterns don't change, so
  // patterns arriving for another source leave the editor alone.
  $: activeSeverityPatterns = activeTab ? ($severityBySource[activeTab.sourceId] ?? NO_PATTERNS) : NO_PATTERNS
  // Formatted again only when the tab's text or format changed, not on
  // every tab switch.
  $: displayContent = activeTab ? displayedContent(activeTab) : ''
  function displayedContent(tab: Tab): string {
    return formattedContent(
      tab.key,
      tab.content,
      tab.format,
      () => formatContent(tab.content, languageForFilename(tab.name), tab.format) ?? tab.content,
    )
  }
  $: sourceNames = new Set(tabs.map((t) => t.sourceId)).size > 1

  // Advanced filter (#274): the box filters files in the ticked sources and
  // folders instead of connections; the explorer lists every source for
  // picking, and shows the matching files once something is ticked.
  let fileQuery = ''
  // While ticking, the tree stays; the files show on "Show the files" or as
  // soon as something is typed in the filter.
  let pickingScopes = true
  // One search (#300): with nothing ticked it searches sources and file
  // names everywhere (SearchResults); with sources or folders ticked, their
  // files live, as before. `$advancedFilter` now means "Select" is on (the
  // checkboxes show).
  $: showScoped = $scopes.length > 0 && !pickingScopes
  $: showSearch = fileQuery.trim().length > 0 && $scopes.length === 0 && !$advancedFilter
  $: if ($scopes.length === 0) pickingScopes = true
  const scopeLabel = (scope: Scope) =>
    isGroupScope(scope) ? scope.name : `${sourceLabel(scope.sourceId)}${scope.path ? ` › ${scope.path}` : ''}`
  // Which sources sit under each customer and folder (#284), from every
  // source the user can see, not only the ones the search box shows.
  $: scopeContext.set(contextFromTree(buildConnectionTree(allSources)))
  $: parsedQuery = parseQuery(fileQuery)
  $: compiledFilter = compileMatcher(parsedQuery.text, { regex: parsedQuery.regex, matchCase: false })
  $: fileMatcher = isMatcherError(compiledFilter) ? null : compiledFilter

  $: filteredAllSources = allSources

  function revealSource(source: Source) {
    fileQuery = ''
    revealInTree(source)
    setExpanded(`source:${source.id}`, true)
  }

  function startSelecting() {
    advancedFilter.set(true)
    pickingScopes = true
  }

  // List or tree: the admin's default (Settings -> Explorer and limits), and -- when
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
        allSources = viewable(await api.get<Source[]>('/sources'))
        closeTabsOfHiddenSources()
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

  // A disabled source keeps its configuration (Settings → Sources) but is
  // off: it isn't shown here at all, and the server refuses to read it.
  function viewable(sources: Source[]): Source[] {
    return sources.filter((s) => s.enabled)
  }

  async function loadSources() {
    loading = true
    loadError = ''
    try {
      allSources = viewable(await api.get<Source[]>('/sources'))
      closeTabsOfHiddenSources()
    } catch {
      loadError = 'Failed to load sources'
    }
    try {
      recentSources = viewable(await api.get<Source[]>('/sources/recent'))
    } catch {
      recentSources = []
    }
    loading = false
  }

  // Per-source severity patterns, fetched on first use for that source and
  // kept across pages (lib/editor-states.ts). Asked for as the file is, so
  // they're usually there when it opens. `refresh` asks again for a source
  // already kept (the Viewer shown again), using the kept list meanwhile
  // and replacing it only if it changed.
  async function ensureSeverityPatterns(sourceId: number, refresh = false) {
    if (get(severityBySource)[sourceId] !== undefined) {
      if (!refresh) return
    } else {
      severityBySource.update((all) => ({ ...all, [sourceId]: NO_PATTERNS }))
    }
    await refreshSeverityPatterns(sourceId, () =>
      api.get<SeverityPattern[]>(`/sources/${sourceId}/severity-patterns/effective`),
    )
  }

  function openUrl(sourceId: number, path: string, member: string | null): string {
    const fetchParams = new URLSearchParams({ path })
    if (member) fetchParams.set('member', member)
    return `/sources/${sourceId}/open?${fetchParams.toString()}`
  }

  function releaseScratch(sourceId: number, path: string, member: string | null) {
    api.post(`/sources/${sourceId}/close`, { path, member }).catch(() => {})
  }

  // Files being opened whole (lib/open-progress.ts): each /open carries a
  // request id; at most three run at once (openQueue), the others wait
  // here, shown at once as Queued. A running one shows its bar after a
  // second, and one GET /open-progress a second asks about all of them.
  interface PendingOpen {
    requestId: string
    sourceId: number
    sourceName: string
    name: string
    shown: boolean
    status: OpenStatus
    controller: AbortController
    cancelled: boolean
    timer: ReturnType<typeof setTimeout> | null
  }
  let pendingOpens: PendingOpen[] = []
  let progressNow = Date.now()
  let progressTimer: ReturnType<typeof setTimeout> | null = null
  let progressAsking = false
  let viewerGone = false
  $: shownOpens = pendingOpens.filter((p) => p.shown)

  const touchOpens = () => (pendingOpens = [...pendingOpens])

  function askingProgress(): PendingOpen[] {
    return pendingOpens.filter((p) => p.shown && !p.status.queued && !p.status.receiving && !p.cancelled)
  }

  /** Asks for the progress in `delay` ms, unless a request is already
   * planned or on its way (which plans the next one when it ends). */
  function scheduleProgress(delay = PROGRESS_POLL_MS) {
    if (viewerGone || progressTimer !== null || progressAsking) return
    progressTimer = setTimeout(askProgress, delay)
  }

  async function askProgress() {
    progressTimer = null
    const asked = askingProgress()
    if (asked.length === 0) return
    progressAsking = true
    const answers = await fetchProgress(asked.map((p) => p.requestId))
    progressAsking = false
    progressNow = Date.now()
    if (answers !== null) {
      for (const p of asked) p.status = nextStatus(p.status, answers[p.requestId] ?? null, progressNow)
    }
    touchOpens()
    if (askingProgress().length > 0) scheduleProgress()
  }

  /** GET of an /open URL with progress and Cancel, its body already read;
   * null when the reader cancelled it (nothing to say about it). Waits its
   * turn in openQueue first; the turn ends once the whole body was read
   * (openInTurn), as the browser connection is busy until then. */
  async function fetchOpen(url: string, sourceId: number, sourceName: string, name: string): Promise<Response | null> {
    const queued = openQueue.active >= openQueue.limit
    const pending: PendingOpen = {
      requestId: newRequestId(),
      sourceId,
      sourceName,
      name,
      shown: queued,
      status: queued ? queuedStatus() : startedStatus(),
      controller: new AbortController(),
      cancelled: false,
      timer: null,
    }
    pendingOpens = [...pendingOpens, pending]
    const signal = pending.controller.signal
    try {
      const response = await openInTurn(
        openQueue,
        signal,
        () => trackedFetch(withRequestId(url, pending.requestId), { credentials: 'include', signal }),
        {
          started: () => {
            if (pending.status.queued) {
              pending.status = startedStatus()
              touchOpens()
              scheduleProgress()
            } else {
              pending.timer = setTimeout(() => {
                pending.timer = null
                pending.shown = true
                touchOpens()
                scheduleProgress(0)
              }, PROGRESS_DELAY_MS)
            }
          },
          receiving: () => {
            pending.status = receivingStatus(pending.status)
            touchOpens()
          },
        },
      )
      // null: cancelled while queued, nothing was asked of the server.
      return pending.cancelled ? null : response
    } catch (error) {
      if (pending.cancelled) return null
      throw error
    } finally {
      if (pending.timer) clearTimeout(pending.timer)
      pendingOpens = pendingOpens.filter((p) => p !== pending)
    }
  }

  /** Cancel: the server stops reading the source and frees what it held;
   * the open ends here at once, without a message. A queued open only
   * leaves the queue: the server never heard of it. */
  function cancelOpen(pending: PendingOpen) {
    pending.cancelled = true
    if (pending.timer) clearTimeout(pending.timer)
    if (!pending.status.queued && !pending.status.receiving) {
      fetch(`/sources/${pending.sourceId}/open-cancel?${new URLSearchParams({ request_id: pending.requestId })}`, {
        method: 'POST',
        credentials: 'include',
      }).catch(() => undefined)
    }
    pending.controller.abort()
    pendingOpens = pendingOpens.filter((p) => p !== pending)
  }

  async function openFile(
    sourceId: number,
    sourceName: string,
    path: string,
    member: string | null,
    name: string,
    size?: number,
    force = false,
    fromEnd = true,
  ): Promise<boolean> {
    const key = tabKey(sourceId, path, member)
    if (tabs.some((t) => t.key === key)) {
      setState({ activeKey: key })
      return true
    }
    // Bounds on one browser tab (#347): so many files open at once, and a
    // very large file only after a warning.
    if (tabLimitReached(tabs.length)) {
      notice = { message: tabLimitMessage(), retryable: false }
      return false
    }
    // Over the `open_from_end_mb` System setting, a plain file opens at its
    // end, without Tail and without copying it whole first; `fromEnd` is
    // false when that was refused (follow_unsupported).
    if (fromEnd && size !== undefined && opensFromEnd(size, $systemSettings.open_from_end_mb, name, member)) {
      largeFile = null
      return openEnd(sourceId, sourceName, path, name, size, false)
    }
    if (!force && size !== undefined && isLargeFile(size)) {
      largeFile = { sourceId, sourceName, path, member, name, size }
      notice = null
      return false
    }
    largeFile = null
    ensureSeverityPatterns(sourceId)
    try {
      const response = await fetchOpen(openUrl(sourceId, path, member), sourceId, sourceName, name)
      if (response === null) {
        // Cancelled: no tab, and nothing to say.
        forgetSeverityWithoutTabs(sourceId)
        return false
      }
      if (!response.ok) {
        const problem = await readFetchFailure(response, name)
        // Gone from the source (#272): its folder is listed again, so it
        // leaves the tree.
        if (problem.missing) refreshFolder(sourceId, parentPath(path))
        notice = {
          ...problem,
          retry: problem.retryable
            ? () => openFile(sourceId, sourceName, path, member, name, size, true, fromEnd)
            : undefined,
        }
        forgetSeverityWithoutTabs(sourceId)
        return false
      }
      const bytes = new Uint8Array(await response.arrayBuffer())
      // Streaming, so a character the file's end splits is completed by
      // the first Follow answer (#254) instead of turning into two U+FFFD.
      const decoder = new TextDecoder()
      const redaction = readRedactionHeaders(response.headers)
      const { text: content, partialChars } = decodeWithPartial(decoder, bytes, redaction.partialBytes)
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
        // Redacted content isn't as long as the file: the Tail goes on from
        // the offset in the original the server gives.
        byteSize: redaction.sourceOffset ?? bytes.byteLength,
        followHead: response.headers.get('x-follow-head'),
        // A .gz file or an archive member is decompressed on the server: only
        // the whole of it can be reloaded.
        followMode: member || name.toLowerCase().endsWith('.gz') ? 'reload' : 'incremental',
        newLines: 0,
        redacted: redaction.redacted,
        redactionCount: redaction.count,
        partialChars,
      }
      notice = null
      forgetEditorState(key)
      setState({ tabs: [...tabs, tab], activeKey: key })
      return true
    } catch {
      notice = {
        message: `${name}: the server could not be reached.`,
        retryable: true,
        retry: () => openFile(sourceId, sourceName, path, member, name, size, true, fromEnd),
      }
      forgetSeverityWithoutTabs(sourceId)
      return false
    }
  }

  // A file over LARGE_FILE_BYTES waiting for the reader's choice (#347):
  // its end with Tail, a download, or the whole file anyway.
  // `replaceKey`: Load whole file of that tab, which holds only the end of
  // the file, rather than a new tab.
  let largeFile: {
    sourceId: number
    sourceName: string
    path: string
    member: string | null
    name: string
    size: number
    replaceKey?: string
  } | null = null
  $: largeFileSource = largeFile ? (allSources.find((s) => s.id === largeFile?.sourceId) ?? null) : null
  $: largeFileCanTail =
    largeFile !== null &&
    largeFile.replaceKey === undefined &&
    largeFile.member === null &&
    !largeFile.name.toLowerCase().endsWith('.gz')
  $: largeFileCanDownload = largeFileSource?.my_capabilities?.includes('download') ?? false
  $: largeFileDownloadHref = largeFile
    ? `/sources/${largeFile.sourceId}/download?${new URLSearchParams({ path: largeFile.path, ...(largeFile.member ? { member: largeFile.member } : {}) }).toString()}`
    : ''

  function openLargeFileAnyway() {
    if (!largeFile) return
    const { sourceId, sourceName, path, member, name, size, replaceKey } = largeFile
    largeFile = null
    if (replaceKey !== undefined) {
      const tab = tabs.find((t) => t.key === replaceKey)
      if (tab) loadWholeFile(tab, true)
      return
    }
    openFile(sourceId, sourceName, path, member, name, size, true, false)
  }

  function tailLargeFile() {
    if (!largeFile) return
    const { sourceId, sourceName, path, name, size } = largeFile
    largeFile = null
    openEnd(sourceId, sourceName, path, name, size)
  }

  interface EndRead {
    content: string
    byteSize: number
    followHead: string | null
    redacted: boolean
    redactionCount: number
    partialChars: number
    // Where `content` starts in the file (bytes of the original).
    startOffset: number
    // Decodes what Tail appends after `content`.
    decoder: TextDecoder
  }

  // How many more answers the end of a file is read in when the first one
  // didn't reach it (redacted, the context read before the offset counts).
  const END_MORE_ROUNDS = 8

  /** Reads the end of a file (its last TAIL_START_BYTES) through Follow,
   * from `size` as listed; once more from the size the server reports when
   * the file shrank since it was listed. `reloadWhole`: the file can't be
   * read from an offset (follow_unsupported). */
  async function readEnd(
    sourceId: number,
    path: string,
    name: string,
    size: number,
    retried = false,
  ): Promise<EndRead | (FetchProblem & { reloadWhole?: boolean })> {
    const offset = tailStartOffset(size)
    let response = await trackedFetch(followUrl(sourceId, path, offset, null, true), { credentials: 'include' })
    if (!response.ok) {
      // What the server says (an agent to update, content redaction can't
      // read...); only a file Tail can't follow from an offset gets this.
      const failure = await readFollowFailure(response, name)
      if (failure.reloadWhole) {
        return {
          message: `${name} can't be read from its end. Open it whole or download it.`,
          retryable: false,
          reloadWhole: true,
        }
      }
      return { message: failure.message, retryable: failure.retryable, missing: failure.missing }
    }
    let answer = readFollowHeaders(response.headers, offset)
    if (answer.reset) {
      const now = Number(response.headers.get('x-file-size'))
      if (!retried && Number.isFinite(now)) return readEnd(sourceId, path, name, now, true)
      return { message: `${name} changed while it was being read. Try again.`, retryable: true }
    }
    let bytes: Uint8Array = new Uint8Array(await response.arrayBuffer())
    let redaction = readRedactionHeaders(response.headers)
    let startOffset: number
    if (redaction.redacted) {
      // The server already started at the next whole line, and says where.
      startOffset = readFollowStart(response.headers) ?? offset
    } else {
      // Bytes from the middle of a line (maybe of a character): from the
      // next line on.
      const cut = cutToFirstLine(bytes, offset)
      bytes = cut.bytes
      startOffset = cut.start
    }
    const decoder = new TextDecoder()
    let { text: content, partialChars } = decodeWithPartial(decoder, bytes, redaction.partialBytes)
    let redacted = redaction.redacted
    let redactionCount = redaction.count
    let head = answer.head
    for (let round = 0; answer.more && round < END_MORE_ROUNDS; round++) {
      response = await trackedFetch(followUrl(sourceId, path, answer.offset, head, false), { credentials: 'include' })
      if (!response.ok) break // what was read shows; Reload or Tail reads the rest
      const next = readFollowHeaders(response.headers, answer.offset)
      if (next.reset) return { message: `${name} changed while it was being read. Try again.`, retryable: true }
      redaction = readRedactionHeaders(response.headers)
      const decoded = decodeWithPartial(decoder, new Uint8Array(await response.arrayBuffer()), redaction.partialBytes)
      content = content.slice(0, content.length - partialChars) + decoded.text
      partialChars = decoded.partialChars
      redacted = redacted || redaction.redacted
      redactionCount += redaction.count
      answer = next
      head = next.head
    }
    return {
      content,
      byteSize: answer.offset,
      followHead: head,
      redacted,
      redactionCount,
      partialChars,
      startOffset,
      decoder,
    }
  }

  /** Opens only the end of a file: a large one with Tail on (#347, "Tail
   * the end"), or, without Tail, one over the `open_from_end_mb` setting,
   * which opens whole instead when it can't be read from its end. */
  async function openEnd(
    sourceId: number,
    sourceName: string,
    path: string,
    name: string,
    size: number,
    tail = true,
  ): Promise<boolean> {
    const key = tabKey(sourceId, path, null)
    if (tabLimitReached(tabs.length)) {
      notice = { message: tabLimitMessage(), retryable: false }
      return false
    }
    ensureSeverityPatterns(sourceId)
    try {
      const read = await readEnd(sourceId, path, name, size)
      if ('message' in read) {
        if (read.reloadWhole && !tail) return openFile(sourceId, sourceName, path, null, name, size, false, false)
        const { reloadWhole: _whole, ...problem } = read
        if (problem.missing) refreshFolder(sourceId, parentPath(path))
        notice = problem.retryable
          ? { ...problem, retry: () => openEnd(sourceId, sourceName, path, name, size, tail) }
          : problem
        forgetSeverityWithoutTabs(sourceId)
        return false
      }
      followDecoders.set(key, read.decoder)
      if (tail) followStarted.add(key)
      const tab: Tab = {
        key,
        sourceId,
        sourceName,
        path,
        member: null,
        name,
        content: read.content,
        scratchKey: null,
        wrapEnabled: false,
        showWhitespace: false,
        bookmarks: [],
        markPatterns: [],
        format: 'none',
        following: tail,
        byteSize: read.byteSize,
        followHead: read.followHead,
        followMode: 'incremental',
        newLines: 0,
        endOnly: read.startOffset > 0,
        startOffset: read.startOffset,
        redacted: read.redacted,
        redactionCount: read.redactionCount,
        partialChars: read.partialChars,
      }
      notice = null
      forgetEditorState(key)
      setState({ tabs: [...tabs.filter((t) => t.key !== key), tab], activeKey: key })
      return true
    } catch {
      notice = {
        message: `${name}: the server could not be reached.`,
        retryable: true,
        retry: () => openEnd(sourceId, sourceName, path, name, size, tail),
      }
      forgetSeverityWithoutTabs(sourceId)
      return false
    }
  }

  // The tab whose "Load earlier" is reading, so it isn't asked twice.
  let earlierLoading: string | null = null
  $: earlierBlocked = activeTab ? loadEarlierBlocked(activeTab) : ''

  /** "Load earlier" on a tab opened at its end: the window before what it
   * holds, through Follow up to `startOffset`, added at the top. Redacted,
   * the server sends whole lines and says where they start; otherwise the
   * window's first, cut line is left for the next window, which ends where
   * this one's whole lines start. */
  async function loadEarlier(tab: Tab) {
    if (!startNotLoaded(tab) || loadEarlierBlocked(tab) || earlierLoading !== null) return
    const { offset, end } = earlierWindow(tab.startOffset ?? 0)
    earlierLoading = tab.key
    try {
      const response = await trackedFetch(followUrl(tab.sourceId, tab.path, offset, tab.followHead, false, end), {
        credentials: 'include',
      })
      if (!response.ok) {
        const { reloadWhole: _whole, stopFollowing: _stop, ...problem } = await readFollowFailure(response, tab.name)
        if (problem.missing) {
          markMissing(tab)
          return
        }
        notice = { ...problem, retry: problem.retryable ? () => retryLoadEarlier(tab.key) : undefined }
        return
      }
      if (readFollowHeaders(response.headers, end).reset) {
        notice = { message: `${tab.name} changed since it was opened. Reload it to read it again.`, retryable: false }
        return
      }
      const bytes = new Uint8Array(await response.arrayBuffer())
      const redaction = readRedactionHeaders(response.headers)
      let text: string
      let start: number
      if (redaction.redacted) {
        text = new TextDecoder().decode(bytes)
        start = readFollowStart(response.headers) ?? offset
      } else {
        const cut = cutToFirstLine(bytes, offset)
        text = new TextDecoder().decode(cut.bytes)
        start = cut.start
      }
      // A Reload or Load whole file meanwhile replaced what the tab holds.
      const current = tabs.find((t) => t.key === tab.key)
      if (!current || current.startOffset !== end) return
      const added = countNewlines(text)
      updateTab(tab.key, {
        content: text + current.content,
        startOffset: start,
        // The whole file is loaded: Reload reads all of it again.
        endOnly: start > 0,
        newlineCount: current.newlineCount === undefined ? undefined : current.newlineCount + added,
        bookmarks: shiftBookmarksDown(current.bookmarks, added),
        redacted: (current.redacted ?? false) || redaction.redacted,
        redactionCount: (current.redactionCount ?? 0) + redaction.count,
      })
      notice = null
    } catch {
      notice = {
        message: `${tab.name}: the server could not be reached.`,
        retryable: true,
        retry: () => retryLoadEarlier(tab.key),
      }
    } finally {
      earlierLoading = null
    }
  }

  // "Showing the last 1 MB of 300 MB", in bytes of the original file.
  function partialLabel(tab: Tab): string {
    return `Showing the last ${formatSize(tab.byteSize - (tab.startOffset ?? 0))} of ${formatSize(tab.byteSize)}`
  }

  function retryLoadEarlier(key: string) {
    const tab = tabs.find((t) => t.key === key)
    if (tab) loadEarlier(tab)
  }

  /** "Load whole file" on a tab opened at its end: the file opened whole,
   * as any file (after the large-file warning), in place of what the tab
   * holds. */
  async function loadWholeFile(tab: Tab, force = false) {
    if (!force && isLargeFile(tab.byteSize)) {
      largeFile = {
        sourceId: tab.sourceId,
        sourceName: tab.sourceName,
        path: tab.path,
        member: tab.member,
        name: tab.name,
        size: tab.byteSize,
        replaceKey: tab.key,
      }
      notice = null
      return
    }
    largeFile = null
    await reloadTab(tab, true)
  }

  async function handleOpen(
    event: CustomEvent<{
      sourceId: number
      sourceName: string
      path: string
      member: string | null
      name: string
      size?: number
      // From a file's right-click menu (#287).
      mode?: 'open' | 'tail' | 'end' | 'compare'
    }>,
  ) {
    const { sourceId, sourceName, path, member, name, size, mode } = event.detail
    if (mode === 'compare' || (mode === undefined && compareArmed)) {
      compareArmed = false
      await pickCompareTarget(sourceId, path, member, name)
      return
    }
    const key = tabKey(sourceId, path, member)
    if (mode === 'end' && member === null && !tabs.some((t) => t.key === key)) {
      // Its last TAIL_START_BYTES, as a file over open_from_end_mb opens.
      largeFile = null
      await openEnd(sourceId, sourceName, path, name, size ?? 0, false)
      return
    }
    const opened = await openFile(sourceId, sourceName, path, member, name, size)
    if (opened && mode === 'tail') startTail(key)
  }

  /** Open with Tail: Tail on in the tab just opened (or already open). */
  function startTail(key: string) {
    viewerState.update((state) => ({
      ...state,
      tabs: state.tabs.map((t) =>
        t.key === key && !t.following && t.followMode === 'incremental' ? { ...t, following: true, newLines: 0 } : t,
      ),
    }))
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
        compareWith = { name: label, content: tab.content, partial: startNotLoaded(tab) }
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
    // Its editor state goes too (lib/viewer-tabs.ts).
    closeTabs([tab.key])
    if (tab.scratchKey) releaseScratch(tab.sourceId, tab.path, tab.member)
  }

  // Tabs of a source that isn't in the list any more (disabled, deleted, or
  // no longer granted) close, so nothing of it stays on screen. Only after
  // a list was actually loaded: an empty list from a failed request mustn't
  // close everything.
  function closeTabsOfHiddenSources() {
    const hidden = closeHiddenSourceTabs(new Set(allSources.map((s) => s.id)))
    for (const tab of hidden) {
      followDecoders.delete(tab.key)
      followStarted.delete(tab.key)
      if (tab.scratchKey) releaseScratch(tab.sourceId, tab.path, tab.member)
    }
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

  /** On `line`, or the cursor's line. */
  function toggleBookmark(at?: number) {
    if (!activeTab || !paneRef) return
    const line = at ?? paneRef.currentLine()
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

  /** `whole`: Load whole file of a tab that holds only the end of its
   * file; it then holds the whole file, as any tab opened whole. */
  async function reloadTab(tab: Tab, whole = false) {
    if (tab.endOnly && !whole) {
      await reloadEnd(tab)
      return
    }
    try {
      const url = openUrl(tab.sourceId, tab.path, tab.member)
      // Load whole file shows its progress, with Cancel, which keeps what
      // the tab had.
      const response = whole
        ? await fetchOpen(url, tab.sourceId, tab.sourceName, tab.name)
        : await trackedFetch(url, { credentials: 'include' })
      if (response === null) return
      if (!response.ok) {
        const problem = await readFetchFailure(response, tab.name)
        if (problem.missing) {
          markMissing(tab)
          return
        }
        const retry = whole ? () => retryLoadWhole(tab.key) : () => retryReload(tab.key)
        notice = { ...problem, retry: problem.retryable ? retry : undefined }
        return
      }
      const bytes = new Uint8Array(await response.arrayBuffer())
      const decoder = new TextDecoder()
      const redaction = readRedactionHeaders(response.headers)
      const { text: content, partialChars } = decodeWithPartial(decoder, bytes, redaction.partialBytes)
      const scratchKey = response.headers.get('x-scratch-key')
      const followHead = response.headers.get('x-follow-head')
      followDecoders.set(tab.key, decoder)
      setState({
        tabs: tabs.map((t) =>
          t.key === tab.key
            ? {
                ...t,
                content,
                scratchKey,
                byteSize: redaction.sourceOffset ?? bytes.byteLength,
                followHead,
                newLines: 0,
                missing: false,
                newlineCount: undefined,
                droppedLines: 0,
                redacted: redaction.redacted,
                redactionCount: redaction.count,
                partialChars,
                // The whole file now: its own line numbers, which the
                // bookmarks of the end weren't.
                ...(whole ? { endOnly: false, startOffset: undefined, bookmarks: [] } : {}),
              }
            : t,
        ),
      })
      if (tab.scratchKey) releaseScratch(tab.sourceId, tab.path, tab.member)
    } catch {
      notice = {
        message: `${tab.name}: the server could not be reached.`,
        retryable: true,
        retry: whole ? () => retryLoadWhole(tab.key) : () => retryReload(tab.key),
      }
    }
  }

  function retryLoadWhole(key: string) {
    const tab = tabs.find((t) => t.key === key)
    if (tab) reloadTab(tab, true)
  }

  // Try again reloads the tab that failed (a tab's menu reloads any tab),
  // as it is now.
  function retryReload(key: string) {
    const tab = tabs.find((t) => t.key === key)
    if (tab) reloadTab(tab)
  }

  /** Reload of a tab that holds only the end of its file: the end again,
   * from the file's size now. */
  async function reloadEnd(tab: Tab) {
    try {
      const probe = await trackedFetch(followUrl(tab.sourceId, tab.path, tab.byteSize, tab.followHead, false), {
        credentials: 'include',
      })
      const size = Number(probe.headers.get('x-file-size'))
      const read = await readEnd(tab.sourceId, tab.path, tab.name, Number.isFinite(size) ? size : tab.byteSize)
      if ('message' in read) {
        notice = read
        return
      }
      followDecoders.set(tab.key, read.decoder)
      updateTab(tab.key, {
        content: read.content,
        byteSize: read.byteSize,
        followHead: read.followHead,
        newLines: 0,
        missing: false,
        newlineCount: undefined,
        droppedLines: 0,
        bookmarks: [],
        startOffset: read.startOffset,
        redacted: read.redacted,
        redactionCount: read.redactionCount,
        partialChars: read.partialChars,
      })
    } catch {
      notice = {
        message: `${tab.name}: the server could not be reached.`,
        retryable: true,
        retry: () => retryReload(tab.key),
      }
    }
  }

  function toggleFollow() {
    if (!activeTab) return
    if (activeTab.following) followStarted.delete(activeTab.key)
    updateActiveTab({ following: !activeTab.following, newLines: 0 })
  }

  // Incremental follow (#254). Per-tab streaming decoders, so a character
  // split across two answers decodes whole, and the tabs whose follow was
  // already announced (audited once, on the first poll). Not reactive.
  const followDecoders = new Map<string, TextDecoder>()
  const followStarted = new Set<string>()
  // A burst bigger than one answer is read in a few more right away.
  const FOLLOW_ROUNDS_PER_TICK = 8

  // The file left the source or is hidden by its rules (#272): Follow stops,
  // the tab keeps what it had and is marked, its folder is listed again, and
  // a note says so once.
  function markMissing(tab: Tab) {
    updateTab(tab.key, { following: false, missing: true })
    followStarted.delete(tab.key)
    refreshFolder(tab.sourceId, parentPath(tab.path))
    notice = {
      message: `${tab.name} wasn't found, or is hidden by this source's rules. The tab keeps what it had; Tail stopped.`,
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
    // Redacted (#382, #249): an unfinished last line is shown as a marker
    // that the next answer replaces. `partialChars` is that marker's length
    // at the end of what the tab holds plus `appended`; what of it was
    // already in the content before this tick is dropped from it.
    let partialChars = tab.partialChars ?? 0
    let dropFromContent = 0
    let redacted = tab.redacted ?? false
    let redactionCount = tab.redactionCount ?? 0
    for (let round = 0; round < FOLLOW_ROUNDS_PER_TICK; round++) {
      const start = !followStarted.has(tab.key)
      const response = await trackedFetch(followUrl(tab.sourceId, tab.path, byteSize, followHead, start), {
        credentials: 'include',
      })
      if (!response.ok) {
        const { reloadWhole, stopFollowing, ...problem } = await readFollowFailure(response, tab.name)
        if (reloadWhole) {
          // A .gz file or an archive member (follow_unsupported): this tab
          // reloads the whole file from now on.
          followStarted.add(tab.key)
          updateTab(tab.key, { followMode: 'reload' })
          await reloadTab(tab)
          return
        }
        if (problem.missing) {
          markMissing(tab)
          return
        }
        // A refusal the next poll won't change (an agent to update, content
        // redaction can't read): Tail stops and the tab keeps what it had.
        if (stopFollowing) {
          updateTab(tab.key, { following: false })
          followStarted.delete(tab.key)
          notice = tailStoppedNotice(problem)
          return
        }
        notice = problem
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
      const redaction = readRedactionHeaders(response.headers)
      const decoded = decodeWithPartial(decoder, bytes, redaction.partialBytes)
      ;({ appended, dropFromContent } = appendReplacingPartial(
        appended,
        dropFromContent,
        partialChars,
        decoded.text,
      ))
      partialChars = decoded.partialChars
      redacted = redacted || redaction.redacted
      redactionCount += redaction.count
      byteSize = answer.offset
      followHead = answer.head
      if (!answer.more) break
    }
    // A Reload while this was in flight replaced the content: drop the answer.
    const current = tabs.find((t) => t.key === tab.key)
    if (!current || current.byteSize !== tab.byteSize) return
    const readingAbove = appended !== '' && tab.key === activeKey && !(paneRef?.isAtBottom() ?? true)
    // The unfinished line the answers replaced holds no newline, so the
    // newline count stays right.
    const kept = dropFromContent > 0 ? current.content.slice(0, current.content.length - dropFromContent) : current.content
    // Tail keeps at most TAIL_MAX_LINES lines (#347): older ones go from
    // the top, and the toolbar says so.
    const newlines = (current.newlineCount ?? countNewlines(current.content)) + countNewlines(appended)
    const held = kept + appended
    const capped = capTailBuffer(held, newlines)
    // The count is of what the tab shows: the values in the lines that
    // left the top go with them.
    if (capped.dropped > 0) redactionCount = redactionCountAfterCap(held, capped.content, redactionCount)
    updateTab(tab.key, {
      content: capped.content,
      newlineCount: capped.newlines,
      droppedLines: (current.droppedLines ?? 0) + capped.dropped,
      bookmarks: shiftBookmarks(current.bookmarks, capped.dropped),
      byteSize,
      followHead,
      // Redacted, only whole lines are new: the marker of an unfinished
      // one comes back on every poll until the line is written.
      newLines: readingAbove
        ? current.newLines + (redacted ? countNewlines(appended) : countLines(appended))
        : current.newLines,
      redacted,
      redactionCount,
      partialChars,
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

  function showCopyStatus(status: typeof copyStatus) {
    copyStatus = status
    if (copyStatusTimer) clearTimeout(copyStatusTimer)
    copyStatusTimer = setTimeout(() => (copyStatus = ''), 1500)
  }

  async function doCopySelectedLines() {
    // Line numbers aren't the file's while its start isn't loaded or once
    // Tail dropped older lines.
    if (!paneRef || lineNumbersNotFiles(activeTab)) return
    try {
      const copied = await paneRef.copySelectedLines()
      showCopyStatus(copied ? 'copied' : 'empty')
    } catch {
      showCopyStatus('failed')
    }
  }

  async function copyToClipboard(write: () => Promise<void>) {
    try {
      await write()
      showCopyStatus('copied')
    } catch {
      showCopyStatus('failed')
    }
  }

  // Right-click menus (#287): one menu at a time, on a log line or a tab.
  // Each entry does what its button or shortcut does.
  type OpenMenu =
    | { kind: 'line'; x: number; y: number; opener: HTMLElement; entries: MenuEntry[]; line: number; selection: string }
    | { kind: 'tab'; x: number; y: number; opener: HTMLElement; entries: MenuEntry[]; tabKey: string }
    | ({ kind: 'explorer'; x: number; y: number; opener: HTMLElement } & ExplorerMenu)
  let contextMenu: OpenMenu | null = null
  let lastTabKeyMenu: number | null = null

  // The explorer's menus (files, folders, sources and groups of sources):
  // each node works out its own entries and runs them; what it can't do on
  // its own comes from here.
  let lastExplorerKeyMenu: number | null = null
  let searchBox: HTMLInputElement | null = null
  // What an explorer action (a copy, Check all now) came to, for a moment.
  let explorerNote = ''
  let explorerNoteTimer: ReturnType<typeof setTimeout> | null = null

  function showExplorerNote(note: string) {
    explorerNote = note
    if (explorerNoteTimer) clearTimeout(explorerNoteTimer)
    explorerNoteTimer = setTimeout(() => (explorerNote = ''), 3000)
  }

  function openExplorerMenu(x: number, y: number, opener: HTMLElement, menu: ExplorerMenu) {
    if (menu.entries.length === 0) return
    contextMenu = { kind: 'explorer', x, y, opener, ...menu }
  }

  async function checkSources(sourceIds: number[]) {
    showExplorerNote(`Checking ${sourceIds.length} ${sourceIds.length === 1 ? 'source' : 'sources'}…`)
    // A few at a time: each check holds a server thread until the source
    // answers or times out.
    const results = await runLimited(
      sourceIds.map((id) => () => api.post(`/sources/${id}/check`)),
      CHECK_ALL_CONCURRENCY,
    )
    const failed = results.filter((r) => r.status === 'rejected').length
    showExplorerNote(
      failed === 0
        ? `Checked ${sourceIds.length} ${sourceIds.length === 1 ? 'source' : 'sources'}`
        : `Checked ${sourceIds.length - failed} of ${sourceIds.length}; the others couldn't be checked`,
    )
    // Each check is the source's status now: the dots show it.
    try {
      allSources = viewable(await api.get<Source[]>('/sources'))
      closeTabsOfHiddenSources()
    } catch {
      // the next periodic read brings it
    }
  }

  const explorerMenus: ExplorerMenus = {
    mouse(event, opener, build) {
      // On a node's links (the download icon) the browser's menu stays.
      if (insideLink(event.target)) return
      const intent = contextMenuIntent(event, lastExplorerKeyMenu, Date.now())
      if (intent === 'browser') return
      event.preventDefault()
      if (intent === 'ignore') return
      openExplorerMenu(event.clientX, event.clientY, opener, build())
    },
    key(event, build) {
      if (!isMenuKey(event)) return
      event.preventDefault()
      lastExplorerKeyMenu = Date.now()
      const opener = event.currentTarget as HTMLElement
      const rect = opener.getBoundingClientRect()
      openExplorerMenu(rect.left, rect.bottom, opener, build())
    },
    searchHere(scope) {
      // The search, limited to this one scope; the files show once
      // something is typed (or at once, with text already there).
      scopes.set([scope])
      pickingScopes = fileQuery.trim() === ''
      void tick().then(() => searchBox?.focus())
    },
    copy(text) {
      navigator.clipboard
        .writeText(text)
        .then(() => showExplorerNote('Copied'))
        .catch(() => showExplorerNote('Copy needs a secure page (HTTPS or localhost)'))
    },
    checkSources(sourceIds) {
      void checkSources(sourceIds)
    },
  }
  setContext(EXPLORER_MENUS, explorerMenus)

  function openLineMenu(request: LineMenuRequest) {
    if (!activeTab) return
    const entries = logLineMenu({
      hasSelection: request.selection !== '',
      singleLineSelection: request.selection !== '' && !request.selection.includes('\n'),
      startLoaded: !startNotLoaded(activeTab),
      archiveMember: activeTab.member !== null,
      droppedLines: activeTab.droppedLines ?? 0,
    })
    contextMenu = { kind: 'line', ...request, entries }
  }

  function tabMenuEntries(tab: Tab): MenuEntry[] {
    const index = tabs.findIndex((t) => t.key === tab.key)
    const source = allSources.find((s) => s.id === tab.sourceId)
    return tabMenu({
      active: tab.key === activeKey,
      tabsLeft: index,
      tabsRight: tabs.length - index - 1,
      canDownload: source?.my_capabilities?.includes('download') ?? false,
      redactedDownload: !!tab.redacted && $systemSettings.redaction_downloads,
    })
  }

  function onTabContextMenu(tab: Tab, event: MouseEvent) {
    const intent = contextMenuIntent(event, lastTabKeyMenu, Date.now())
    if (intent === 'browser') return
    event.preventDefault()
    if (intent === 'ignore') return
    const opener = (event.currentTarget as HTMLElement).querySelector<HTMLElement>('.tab-label')
    if (!opener) return
    contextMenu = { kind: 'tab', x: event.clientX, y: event.clientY, opener, entries: tabMenuEntries(tab), tabKey: tab.key }
  }

  function onTabKeydown(tab: Tab, event: KeyboardEvent) {
    if (!isMenuKey(event)) return
    event.preventDefault()
    lastTabKeyMenu = Date.now()
    const opener = event.currentTarget as HTMLElement
    const rect = opener.getBoundingClientRect()
    contextMenu = { kind: 'tab', x: rect.left, y: rect.bottom, opener, entries: tabMenuEntries(tab), tabKey: tab.key }
  }

  // A middle-click closes a tab; its mousedown would start the browser's
  // autoscroll.
  function onTabMouseDown(event: MouseEvent) {
    if (event.button === 1) event.preventDefault()
  }

  function onTabAuxClick(tab: Tab, event: MouseEvent) {
    if (event.button !== 1) return
    event.preventDefault()
    closeTab(tab)
  }

  async function runLineMenu(id: string, line: number, selection: string) {
    if (!activeTab || !paneRef) return
    switch (id) {
      case 'copy':
        return copyToClipboard(() => paneRef!.copySelectionOrLine())
      case 'copy-with-numbers':
        return doCopySelectedLines()
      case 'find-all-selection':
        findSpec = { ...findSpec, query: selection, useRegex: false, wholeWord: false }
        findOpen = true
        findAllSpec = { ...findSpec }
        return
      case 'highlight-selection': {
        // Literal text: a plain (glob-kind) mark matches it as written.
        const mark: MarkPattern = {
          id: crypto.randomUUID(),
          pattern: selection,
          pattern_kind: 'glob',
          colorIndex: nextMarkColorIndex(activeTab.markPatterns),
        }
        updateActiveTab({ markPatterns: [...activeTab.markPatterns, mark] })
        return
      }
      case 'toggle-bookmark':
        return toggleBookmark(line)
      case 'go-to-line':
        return openGoToLine()
      case 'copy-link': {
        const link = lineLink(window.location.href, activeTab.sourceId, activeTab.path, line)
        return copyToClipboard(() => navigator.clipboard.writeText(link))
      }
    }
  }

  function closeTabKeys(keys: string[]) {
    const closing = tabs.filter((t) => keys.includes(t.key))
    for (const tab of closing) {
      followDecoders.delete(tab.key)
      followStarted.delete(tab.key)
    }
    closeTabs(keys)
    for (const tab of closing) if (tab.scratchKey) releaseScratch(tab.sourceId, tab.path, tab.member)
  }

  function runTabMenu(id: string, key: string) {
    const index = tabs.findIndex((t) => t.key === key)
    const tab = tabs[index]
    if (!tab) return
    switch (id) {
      case 'close':
        return closeTab(tab)
      case 'close-others':
        return closeTabKeys(tabs.filter((t) => t.key !== key).map((t) => t.key))
      case 'close-left':
        return closeTabKeys(tabs.slice(0, index).map((t) => t.key))
      case 'close-right':
        return closeTabKeys(tabs.slice(index + 1).map((t) => t.key))
      case 'close-all':
        return closeTabKeys(tabs.map((t) => t.key))
      case 'reload':
        return void reloadTab(tab)
      case 'download':
        window.open(downloadHref(tab), '_blank', 'noreferrer')
        return
      case 'compare':
        if (key !== activeKey && activeTab) {
          compareArmed = false
          compareWith = { name: `${tab.sourceName} › ${tab.name}`, content: tab.content, partial: startNotLoaded(tab) }
          compareKey += 1
        }
        return
      case 'copy-path': {
        const text = tabPathText(tab.path, tab.member)
        return copyToClipboard(() => navigator.clipboard.writeText(text))
      }
    }
  }

  function chooseFromMenu(id: string) {
    const menu = contextMenu
    contextMenu = null
    if (!menu) return
    if (menu.kind === 'line') void runLineMenu(id, menu.line, menu.selection)
    else if (menu.kind === 'tab') runTabMenu(id, menu.tabKey)
    else menu.run(id)
  }

  function downloadHref(tab: Tab): string {
    const query = new URLSearchParams({ path: tab.path, ...(tab.member ? { member: tab.member } : {}) })
    return `/sources/${tab.sourceId}/download?${query.toString()}`
  }

  async function openGoToLine() {
    // Line numbers are the file's only once its start is loaded and while
    // Tail hasn't dropped older lines.
    if (lineNumbersNotFiles(activeTab)) return
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
      openFind()
    } else if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'g') {
      event.preventDefault()
      openGoToLine()
    }
  }

  onMount(async () => {
    migrateLegacyViewChoice()
    window.addEventListener('keydown', handleKeydown)
    await loadSources()
    // Patterns changed in Settings meanwhile reach the open tabs.
    for (const sourceId of new Set(tabs.map((t) => t.sourceId))) ensureSeverityPatterns(sourceId, true)
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
    // Tabs stay open in viewerState; only this view's timers stop. An open
    // still in progress, or queued, ends on its own, without asking for its
    // progress.
    viewerGone = true
    for (const pending of pendingOpens) if (pending.timer) clearTimeout(pending.timer)
    if (progressTimer) clearTimeout(progressTimer)
    pendingOpens = []
    window.removeEventListener('keydown', handleKeydown)
    stopCount?.()
    if (countTimer) clearTimeout(countTimer)
    if (copyStatusTimer) clearTimeout(copyStatusTimer)
    if (explorerNoteTimer) clearTimeout(explorerNoteTimer)
    stopFollowTimer()
    stopAutoRefresh()
    if (statusTimer) clearInterval(statusTimer)
  })
</script>

<div class="viewer">
  <aside class="explorer">
    <div class="explorer-header">
      <input
        class="input search-box"
        type="search"
        aria-label="Search sources and files"
        placeholder="Search sources and files: text, app-*.log, or /regex/"
        bind:this={searchBox}
        bind:value={fileQuery}
        on:input={() => { if (fileQuery.trim() && $scopes.length > 0) pickingScopes = false }}
      />
      <div class="header-row">
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
        <button
          class="view-toggle-btn advanced-toggle"
          class:active={$advancedFilter}
          aria-pressed={$advancedFilter}
          title="Tick sources and folders to search their files live"
          on:click={() => advancedFilter.update((on) => !on)}
        >
          Filter sources
        </button>
      </div>
    </div>
    {#if isMatcherError(compiledFilter)}
      <p class="filter-error search-error" role="status">{compiledFilter.error}</p>
    {/if}
    {#if explorerNote}
      <p class="hint explorer-note" role="status">{explorerNote}</p>
    {/if}
    {#if $advancedFilter || $scopes.length > 0}
    <div class="scope-bar">
        {#if $scopes.length === 0}
          <span class="hint">Tick the sources and folders to search their files live.</span>
        {:else}
          {#each $scopes as scope (scopeKey(scope))}
            <span class="scope-chip">
              {scopeLabel(scope)}
              <button
                class="link"
                aria-label="Remove {scopeLabel(scope)} from the filter"
                on:click={() => removeScope(scope)}>✕</button
              >
            </span>
          {/each}
          <button class="link" on:click={clearScopes}>Clear all</button>
          <button class="link" on:click={() => (pickingScopes = !pickingScopes)}>
            {pickingScopes ? `Show the files (${$scopes.length})` : 'Add or change'}
          </button>
          {#if showScoped}
            <button class="link" title="Stop searching folders in every selection" on:click={() => stopSearches.update((n) => n + 1)}>
              Stop all
            </button>
          {/if}
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
            {#if isGroupScope(scope)}
              {@const members = sourceScopesOf(scope, $scopeContext)}
              <div class="group-scope">
                <div class="group-scope-head">
                  <span>{scope.name}</span>
                  <span class="count">{members.length} {members.length === 1 ? 'source' : 'sources'}</span>
                  <button
                    class="link"
                    aria-label="Remove {scope.name} from the filter"
                    on:click={() => removeScope(scope)}>✕</button
                  >
                </div>
                {#each members as member (scopeKey(member))}
                  <ScopeResults
                    scope={member}
                    sourceName={sourceLabel(member.sourceId)}
                    matcher={fileMatcher}
                    partOfGroup
                    on:open={handleOpen}
                  />
                {/each}
              </div>
            {:else}
              <ScopeResults
                {scope}
                sourceName={sourceLabel(scope.sourceId)}
                matcher={fileMatcher}
                on:open={handleOpen}
              />
            {/if}
          {/each}
        </div>
      {:else if showSearch}
        <SearchResults
          query={parsedQuery.text}
          regex={parsedQuery.regex}
          sources={allSources}
          on:open={handleOpen}
          on:reveal={(e) => revealSource(e.detail.source)}
          on:select={startSelecting}
        />
      {:else if filteredAllSources.length === 0}
        <p class="hint">No sources visible to your account.</p>
      {:else if connectionsView === 'tree'}
        <div class="connections-tree-body">
          {#each connectionTree as node (node.id)}
            <ConnectionsTree
              {node}
              expandedIds={expandedTreeIds}
              forceExpand={false}
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
          <!-- svelte-ignore a11y_no_static_element_interactions -->
          <div
            class="tab"
            class:active={tab.key === activeKey}
            title={`${tab.sourceName} › ${tabPathText(tab.path, tab.member)}`}
            on:contextmenu={(e) => onTabContextMenu(tab, e)}
            on:mousedown={onTabMouseDown}
            on:auxclick={(e) => onTabAuxClick(tab, e)}
          >
            <button
              class="tab-label"
              class:missing={tab.missing}
              on:click={() => handleTabClick(tab)}
              on:keydown={(e) => onTabKeydown(tab, e)}
            >
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
    {#if shownOpens.length > 0}
      <div class="open-progress-list">
        {#each shownOpens as pending (pending.requestId)}
          <OpenProgress
            name={pending.name}
            sourceName={pending.sourceName}
            status={pending.status}
            now={progressNow}
            on:cancel={() => cancelOpen(pending)}
          />
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
    {#if newSlowPatterns.length > 0}
      <div class="notice" role="alert">
        <span>
          {newSlowPatterns.length === 1 ? 'A severity indicator' : 'Some severity indicators'} took too
          long to match and {newSlowPatterns.length === 1 ? 'is' : 'are'} turned off until you reload the
          page: {newSlowPatterns.map((p) => p.pattern).join(', ')}
        </span>
        <span class="notice-actions">
          <button
            class="link"
            aria-label="Dismiss"
            on:click={() => (dismissedSlowPatterns = $slowSeverityPatterns.length)}>✕</button
          >
        </span>
      </div>
    {/if}
    {#if largeFile}
      <div class="notice" role="alert">
        <span>
          {largeFile.name} is {formatSize(largeFile.size)}. Opening it loads the whole file into this browser tab,
          which can be slow.
        </span>
        <span class="notice-actions">
          {#if largeFileCanTail}
            <button class="link" on:click={tailLargeFile}>Tail the end</button>
          {/if}
          {#if largeFileCanDownload}
            <a class="link" href={largeFileDownloadHref} target="_blank" rel="noreferrer" on:click={() => (largeFile = null)}>
              Download
            </a>
          {/if}
          <button class="link" on:click={openLargeFileAnyway}>Open anyway</button>
          <button class="link" aria-label="Cancel" on:click={() => (largeFile = null)}>✕</button>
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
            {#if startNotLoaded(activeTab) || compareWith.partial}
              <span class="hint" role="status">In the loaded part only</span>
            {/if}
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
              title="Find in this file, or list every matching line (Ctrl/Cmd+F)"
              class:active={findOpen}
              aria-pressed={findOpen}
              on:click={() => (findOpen ? closeFind() : openFind())}
            >
              Find
            </button>
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
              <button
                class="btn-toggle"
                title={lineNumbersNotFiles(activeTab) ? 'Line numbers need the whole file' : 'Go to a line (Ctrl/Cmd+G)'}
                disabled={lineNumbersNotFiles(activeTab)}
                on:click={openGoToLine}>Go to line</button
              >
            {/if}
            <span class="tb-sep" aria-hidden="true"></span>
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
              class:active={activeTab.wrapEnabled}
              aria-pressed={activeTab.wrapEnabled}
              on:click={toggleWrap}
            >
              Wrap
            </button>
            <button
              class="btn-toggle"
              class:active={activeTab.showWhitespace}
              aria-pressed={activeTab.showWhitespace}
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
            <span class="tb-sep" aria-hidden="true"></span>
            <button
              class="btn-toggle tail"
              class:active={activeTab.following}
              aria-pressed={activeTab.following}
              title={activeTab.followMode === 'incremental'
                ? "Show new lines as they're written, like tail -f: fetches only what was appended"
                : "Show new lines as they're written: reloads the whole file (compressed files and archive members)"}
              on:click={toggleFollow}
            >
              {#if activeTab.following}<span class="live-dot" aria-hidden="true"></span>{/if}
              Tail
            </button>
            {#if activeTab.following}
              <!-- Only while Tail is on (#283); set under Settings -> Explorer and limits. -->
              <span class="follow-interval" title="How often Tail checks for new lines (Settings → Explorer and limits)">
                every {followIntervalMs / 1000} s
              </span>
            {/if}
            {#if activeTab.droppedLines}
              <span class="hint" role="status" title="Reload to read the file again from the source">
                Older lines dropped: Tail keeps the last {TAIL_MAX_LINES.toLocaleString('en')} lines
              </span>
            {/if}
            {#if activeTab.redacted}
              <span
                class="hint redaction-count"
                role="status"
                data-testid="redaction-count"
                title="Redaction is on: the server replaced these values before sending the file"
              >
                {redactionCountLabel(activeTab.redactionCount ?? 0)}
              </span>
            {/if}
          </div>
          <div class="toolbar-right">
            {#if copyStatus}
              <span class="hint copy-status" role="status"
                >{copyStatus === 'copied'
                  ? 'Copied'
                  : copyStatus === 'failed'
                    ? 'Copy needs a secure page (HTTPS or localhost)'
                    : 'Nothing selected'}</span
              >
            {/if}
            <button
              class="icon-btn"
              title={lineNumbersNotFiles(activeTab)
                ? 'Line numbers need the whole file'
                : 'Copy selected lines with line numbers'}
              aria-label="Copy lines"
              disabled={lineNumbersNotFiles(activeTab)}
              on:click={doCopySelectedLines}
            >
              <ToolbarIcon name="copy" />
            </button>
            <button
              class="icon-btn"
              class:active={activeTab.bookmarks.length > 0}
              title="Toggle bookmark on current line"
              aria-label="Bookmark"
              on:click={() => toggleBookmark()}
            >
              <ToolbarIcon name="bookmark" />
            </button>
            {#if activeTab.bookmarks.length > 0}
              <button
                class="icon-btn"
                title="Previous bookmark"
                aria-label="Previous bookmark"
                on:click={() => paneRef?.jumpToPreviousBookmark()}
              >
                <ToolbarIcon name="chevron-up" />
              </button>
              <button
                class="icon-btn"
                title="Next bookmark"
                aria-label="Next bookmark"
                on:click={() => paneRef?.jumpToNextBookmark()}
              >
                <ToolbarIcon name="chevron-down" />
              </button>
            {/if}
            {#if activeSeverityPatterns.length > 0}
              <span class="tb-sep" aria-hidden="true"></span>
              <span class="problem-nav" title="Problems (severity patterns)">
                <ToolbarIcon name="alert" />
              </span>
              <button
                class="icon-btn"
                title="Previous problem"
                aria-label="Previous problem"
                on:click={() => paneRef?.jumpToPreviousProblem()}
              >
                <ToolbarIcon name="chevron-up" />
              </button>
              <button
                class="icon-btn"
                title="Next problem"
                aria-label="Next problem"
                on:click={() => paneRef?.jumpToNextProblem()}
              >
                <ToolbarIcon name="chevron-down" />
              </button>
            {/if}
            <span class="tb-sep" aria-hidden="true"></span>
            <button class="icon-btn" title="Reload from source" aria-label="Reload" on:click={reloadActiveTab}>
              <ToolbarIcon name="refresh" />
            </button>
            {#if canDownloadActive}
              <a
                class="icon-btn"
                title={downloadLabel}
                aria-label={downloadLabel}
                href={downloadHref(activeTab)}
                target="_blank"
                rel="noreferrer"
              >
                <ToolbarIcon name="download" />
              </a>
            {/if}
          </div>
        </div>
        {#if findOpen}
          <FindBar
            bind:this={findBar}
            bind:spec={findSpec}
            total={findCount?.total ?? 0}
            counting={findPattern !== null && !findCount?.done}
            current={currentMatch}
            invalid={findSpec.query !== '' && findPattern === null}
            on:next={() => stepFind(true)}
            on:previous={() => stepFind(false)}
            on:findall={runFindAll}
            on:close={closeFind}
          />
        {/if}
        {#if startNotLoaded(activeTab)}
          <!-- A file opened at its end: how much of it is loaded, and the
               way to the rest. -->
          <div class="partial-bar" role="status" data-testid="partial-bar">
            <span>{partialLabel(activeTab)}</span>
            <span class="partial-actions">
              <button
                class="btn-toggle"
                disabled={earlierBlocked !== '' || earlierLoading !== null}
                title={earlierBlocked || 'Read the 1 MB before what is shown and add it above'}
                on:click={() => activeTab && loadEarlier(activeTab)}
              >
                {earlierLoading === activeTab.key ? 'Loading…' : 'Load earlier'}
              </button>
              <button
                class="btn-toggle"
                title="Open the whole file in this tab"
                on:click={() => activeTab && loadWholeFile(activeTab)}
              >
                Load whole file
              </button>
            </span>
          </div>
        {/if}
        <!-- The Tail notice is anchored to the editor, not the whole area, so
             it stays over the log when Find all is open below (#298). -->
        <div class="editor-pane">
          <CodeMirrorPane
            bind:this={paneRef}
            content={displayContent}
            stateKey={activeTab.key}
            format={activeTab.format}
            severityPatterns={activeSeverityPatterns}
            filename={activeTab.name}
            wrapEnabled={activeTab.wrapEnabled}
            showWhitespace={activeTab.showWhitespace}
            bookmarks={activeTab.bookmarks}
            markPatterns={activeTab.markPatterns}
            find={editorFind}
            lineNumbers={!startNotLoaded(activeTab)}
            on:bottom={onReachedBottom}
            on:findclosed={closeFind}
            on:menu={(e) => openLineMenu(e.detail)}
          />
          {#if activeTab.newLines > 0}
            <button class="jump-to-end" on:click={jumpToEnd}>
              {activeTab.newLines} new {activeTab.newLines === 1 ? 'line' : 'lines'} — jump to end ↓
            </button>
          {/if}
        </div>
        {#if findAllSpec}
          <FindInDocumentPanel
            content={displayContent}
            spec={findAllSpec}
            note={startNotLoaded(activeTab) ? 'In the loaded part only' : ''}
            on:jump={(e) => paneRef?.scrollToLine(e.detail.line)}
            on:close={() => (findAllSpec = null)}
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
    {#if contextMenu}
      {#key contextMenu}
        <ContextMenu
          entries={contextMenu.entries}
          x={contextMenu.x}
          y={contextMenu.y}
          label={contextMenu.kind === 'explorer'
            ? contextMenu.label
            : contextMenu.kind === 'line'
              ? 'Line actions'
              : 'Tab actions'}
          opener={contextMenu.opener}
          on:select={(e) => chooseFromMenu(e.detail)}
          on:close={() => (contextMenu = null)}
        />
      {/key}
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
  .header-row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
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
  .filter-error {
    color: var(--danger);
  }
  .search-error,
  .explorer-note {
    margin: 0 0.75rem 0.4rem;
    font-size: 0.8rem;
  }
  .group-scope {
    border-bottom: 1px solid var(--border-soft);
  }
  .group-scope-head {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.45rem 0.65rem 0.1rem;
    font-size: 0.8rem;
    font-weight: 600;
    color: var(--text);
  }
  .group-scope-head span:first-child {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .group-scope-head .count {
    font-weight: 400;
    font-size: 0.72rem;
    color: var(--text-faint);
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
  /* About three bars high; more scroll inside, so the editor stays put. */
  .open-progress-list {
    flex-shrink: 0;
    max-height: 7.5rem;
    overflow-y: auto;
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
  .partial-bar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 0.5rem 1rem;
    padding: 0.35rem 0.9rem;
    background: var(--accent-soft);
    border-bottom: 1px solid var(--border-soft);
    color: var(--text);
    font-size: 0.82rem;
  }
  .partial-actions {
    display: flex;
    gap: 0.5rem;
    flex: 0 0 auto;
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
  .editor-area {
    flex: 1;
    display: flex;
    flex-direction: column;
    min-width: 0;
    min-height: 0;
    background: var(--bg);
    position: relative;
  }
  .editor-pane {
    flex: 1;
    display: flex;
    flex-direction: column;
    min-height: 0;
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
    color: var(--text-faint);
    font-size: 0.72rem;
    white-space: nowrap;
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
    gap: 0.15rem;
  }
  .pane-toolbar .hint {
    padding: 0;
    color: var(--text-faint);
    white-space: nowrap;
  }
  /* Toolbar (#286): quiet buttons, no borders; only what's on gets a fill. */
  .btn-toggle,
  .icon-btn {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    border: none;
    background: transparent;
    color: var(--text-muted);
    border-radius: var(--radius-sm);
    padding: 0.22rem 0.45rem;
    font: inherit;
    font-size: 0.75rem;
    cursor: pointer;
    white-space: nowrap;
    text-decoration: none;
  }
  .btn-toggle:hover,
  .icon-btn:hover {
    background: var(--bg-hover);
    color: var(--text);
  }
  .btn-toggle:focus-visible,
  .icon-btn:focus-visible {
    outline: 1px solid var(--accent-border);
    outline-offset: 1px;
  }
  .btn-toggle.active,
  .icon-btn.active {
    background: var(--accent-soft);
    color: var(--text);
  }
  .icon-btn {
    padding: 0.28rem;
  }
  .live-dot {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: var(--success);
  }
  .tb-sep {
    width: 1px;
    height: 16px;
    background: var(--border);
    margin: 0 0.15rem;
    flex: 0 0 auto;
  }
  .problem-nav {
    display: inline-flex;
    color: var(--warning);
    padding: 0 0.1rem 0 0.2rem;
  }
  .toolbar-right {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    row-gap: 0.4rem;
    gap: 0.15rem;
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
