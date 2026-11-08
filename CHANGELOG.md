# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project uses [Semantic Versioning](https://semver.org/) once a first
release ships (0.x releases may include breaking changes between minors).

## [Unreleased]

### Upgrading
- **The content index and the Search page are gone** (#303). The migration drops their tables, so the space they took comes back; nothing can be kept. The `SEARCH_INDEX_*` environment variables are ignored.
- **Alerts read new lines live** (#305). Existing alerts keep their name, query, source and webhook, and start again at the end of each file they watch. Their query is now plain text (anywhere in a line, any letter case) or a regular expression, not full-text search syntax: an alert that used operators like `AND` or quotes needs its query rewritten.
- **Webhook calls changed shape**: each hit has `source_name`, `file_path`, `offset` (byte position in the file) and `line` (the matching line, plain text), in place of `line_number` and `snippet_html`. Receivers that read `snippet_html` need updating.
- A YAML import that still sets `search_indexing_enabled` applies with a warning; from 1.0 it's refused.

### Added
- **Live alerts** (#305): a pass every 60 seconds (Settings → System) reads only what was appended to the files each alert watches, the way Tail does, and keeps nothing of their content. An alert watches a source, a folder and its subfolders, a customer, or every source its owner can view, and picks files with a glob (`**/*.log` by default) among those the file-name index lists; compressed files and archives are never read. Queries are text or RE2 regular expressions. A rotated or rewritten file is read from the top. Each source gets at most 20 files and 15 seconds per alert per pass; files that don't fit are read in the next passes and counted in `/monitoring/health`.
- **File-name index** (#304), on by default. PerchTail lists each source's rule-visible files in the background (a few sources at a time, each again once its index is older than 15 minutes) and keeps their names, sizes and modification times, never their content. `GET /files` searches them across every source the user can view, as text, glob or regex, with no cap on results (paged); the explorer's single search builds on it (#300). Settings → System turns it on or off and sets the interval (5 minutes to a day); each source can be left out, and its page shows how many files were found, when, why a pass stopped short, and an **Index now** button. Folders that can't be listed, or that are deeper than 32 levels, are skipped without dropping what was indexed below them. Search checks each source's current rules, and a rule change drops the names it now hides straight away.
- Settings → Folders shows each customer's sources inside their folders, and folders and sources move by **drag and drop**: onto a folder, or onto the customer's top level. A folder can't be dropped into itself or below itself; the Move button still does the same from the keyboard (#355).
- Settings → Users has an **Edit** popup per user (click the username or Edit): display name and email for local accounts, role and active, with the username read-only (#356).
- Every background job has a stable id and name, and its last start, finish, result, duration and error are stored in the database and shown per job in `/monitoring/health` (#327). Stored errors never include URLs or credential-looking values.
- `REMOTE_READ_TIMEOUT_SECONDS` (60 by default): how long a connected SSH/SFTP or SMB source may stay silent before the call fails (#343).
- `SHUTDOWN_TIMEOUT_SECONDS` (20 by default): how long a stop waits for running background jobs and the file-name index and source-check workers to finish (#335). `docker-compose.yml` now sets `stop_grace_period: 30s`; see docs/high-availability.md for Kubernetes.

### Changed
- **One search in the explorer** (#300). The box finds sources (by name, host, customer or folder) and file names in every source the user can view, from the file-name index, without picking a mode; results replace the tree while there is text in the box, each with its source and folder, and say when the index last listed the files. Text matches anywhere, `*` and `?` make a glob, and a regex goes between slashes (`/app\.log\.\d/`); the Regex and Match case options and the Advanced filter button are gone (always case-insensitive). **Filter sources**, next to List/Tree, shows the checkboxes: with sources or folders ticked, the search runs live in those, as before, including sources left out of the index.
- Settings → System groups its settings into cards (Explorer, Sources, Audit log, Password policy, Monitoring, Updates) laid out in columns across the page width, one column on narrow screens (#357).
- The audit log uses the full width of the browser. Its filters are dropdowns with a search box on top: Action, and one per target type (customer, folder, role, source, user...) to pick single targets by name or every target of the type, including ones deleted since. Clicking the Time, User, Action or Target header sorts it; the server sorts, so the order covers every matching entry and not only the page on screen (#358).
- Background source checks run four sources at a time, each with a deadline, so a source that stalls only holds its own worker and is shown as failing (#343).
- A source whose background check fails three times in a row backs off: its background checks and file-name index passes are skipped for a growing, jittered time (up to 30 minutes), and it stays failing until any operation on it works. The count survives restarts; browsing the source is never blocked by it (#343).
- Folder ZIP downloads and remote copies of archives are built under `SCRATCH_DIR` instead of the system temp directory, and scratch usage in `/monitoring/health` and the size guard is measured on disk, so they count too (#329).
- A disabled source is off: it keeps its configuration in Settings → Sources, but it no longer appears in the Viewer, open tabs from it close, and nothing can be listed, opened, followed, downloaded or checked on it (the API answers 409), whatever the user's grants. Before, only background work skipped it and it could still be browsed.

### Removed
- The full-text content index, the Search page, `GET /search`, the per-source "indexed for search" option and the Search setting (#303). Searching file names across sources is the file-name index (#304); watching for lines is live alerts (#305).

### Security
- Regenerating a push agent's token, or disabling or deleting its source, now closes the agent's open connection. Before, an agent that had connected with the old token kept answering for the source until it disconnected on its own, so regenerating a leaked token didn't cut off whoever was using it.

### Fixed
- An alert whose webhook failed lost those matches: it moved on and only logged a warning. A failed call is now retried with backoff for up to an hour, then counted on the alert with its error, shown on the Alerts page and in `/monitoring/health` (#328). While a call is retried, its matching lines (at most 50) wait in the database; they're dropped when it succeeds, after the hour, or as soon as the alert is turned off or its owner may no longer alert, and the stored error never includes the webhook URL.
- Long-interval background jobs (audit purge, audit integrity check, update check, expired-session purge) counted their interval from each start, so a container restarted more often than that never ran them. One that's overdue by its stored record now runs 30 seconds after startup (#327).
- An SSH/SFTP source that stalled after login blocked a worker thread forever (no read timeout or keepalive), and a stalled SMB server held a request for about 20 minutes. Both now fail after `REMOTE_READ_TIMEOUT_SECONDS` (#343).
- Shutdown cut running background jobs and file-name index passes off mid-run; it now lets them finish within `SHUTDOWN_TIMEOUT_SECONDS` (#335).
- Scratch files left by a crash or a killed container were never deleted or counted; `SCRATCH_DIR` is now emptied at startup (refused if it holds the database or the logs) (#329).
- Each connected push agent kept a database connection until it disconnected, so with about 15 agents that had answered a command every other request waited 30 seconds and failed (#341).
- Escape on a new source or role popup, before its lists finished loading, asked to discard changes although nothing was typed (#315, a known issue in 0.6.0).

## [0.6.0] - 2026-11-07

The release after 0.5.3. It carries everything from the pre-releases
0.6.0-rc.1, rc.2 and rc.3, whose sections in
[CHANGELOG.md](https://github.com/quaresma870/perchtail/blob/v0.6.0/CHANGELOG.md)
list every change; the code is the one retested in the lab as rc.3, on
Linux, Windows (SMB, WinRM, JEA) and push agent sources.

### Upgrading from 0.5.3
- **Back up `DATA_DIR` first**, with PerchTail stopped. Six migrations
  run on start; they only add columns and tables (update check state,
  agent version, password history, display name and email, user
  preferences, source status, the WinRM JEA endpoint).
- **Upgrade the push agents** to get incremental Tail, file times and
  the agent version in the source overview. Older agents keep working:
  Tail reloads the whole file, and their files show no time.
- **The update check makes one outbound request a day** to the GitHub
  releases API (nothing about the installation is sent). Set
  `UPDATE_CHECK=false` to turn it off entirely, for example on an
  air-gapped install.
- **A background check connects to every source every 10 minutes** (one
  connection, not audited) to show its status. Change the interval or
  turn it off (0) under Settings → System.
- From source: reinstall `backend/requirements.txt`, which adds `pypsrp`
  for JEA endpoints.
- Settings → Security and Settings → Sessions moved to **My profile**, in
  the user menu at the top right; the old links lead there.
- New under Settings → System: password policy (NIST defaults, stricter
  rules off), Tail interval (2 s), open-folder refresh (30 s), default
  file order, source check interval, update check.

### Highlights
- **Viewer:** Tail fetches only the new lines (like `tail -f`), at an
  interval set under Settings → System. One find bar for Find and Find
  all, with Match case, Whole word, Regex and "3 of 120"; Find all lists
  every matching line, with no limit, even in logs of 100 MB. A quieter
  toolbar.
- **Explorer:** each file shows its size (the time on hover); open
  folders refresh themselves; a status dot on each source says whether
  it answers; an advanced filter searches file names across ticked
  sources, folders of sources and whole customers, recursively, as
  text, glob or regex.
- **WinRM without a remote shell:** a read-only JEA endpoint
  (`docs/examples/jea`) that lists and reads only the allowed folders,
  as an account with no administrator rights.
- **Update check:** super admins see the running and the latest version,
  with the upgrade steps, under Settings → System → Updates.
- **Accounts:** a password policy (NIST SP 800-63B defaults); My profile
  with name, email, password, two-factor authentication and sessions; a
  user menu in the top bar.
- **Settings:** row actions are buttons; creating, editing and deleting
  open in popups, the source and role editors included.

### Deprecated
- **Content indexing, the Search page and the current Alerts go away in
  0.7** (#303). The content index is the only place PerchTail keeps text
  from the logs, against its promise that nothing from the sources is
  kept. In 0.7 the upgrade deletes the indexed text; searching inside a
  file stays (Find and Find all), file names get an index of their own,
  on by default and with no content (#304), and Alerts watch new lines
  live instead of the index (#305). PostgreSQL support comes in 0.7 too
  (#306).

### Known issues
- Pressing Escape on a new, untouched source or role popup while its
  lists are still loading asks to discard changes; choose Discard, or
  wait a moment. Fixed in 0.7 (#315).

## [0.6.0-rc.3] - 2026-11-07

Third release candidate for 0.6.0, from the rc.2 lab review. Upgrade
notes, from 0.6.0-rc.2: no migrations, no new settings, and push agents
don't need upgrading. From earlier versions, follow the rc.2 and rc.1
notes below as well.

### Changed
- **Advanced filter: no limit on folders or time** (#284). A ticked
  source or folder is searched to the end, still a few listings at a
  time per source, with Stop. When it's done it says how many folders it
  searched; it says so as well when it was stopped, when folders couldn't
  be listed, and when it didn't go below 32 levels (a guard against links
  that loop back to a folder above). The refresh of the results no longer
  stops at 50 folders.
- **A user menu in the top bar** (#298): your initials, name and an arrow,
  opening My profile and Log out (the separate Log out button goes). It
  works from the keyboard too.
- **The explorer shows each file's size** ("3.5 MB") instead of the
  time it was last modified; hovering a file shows the exact size and
  the time. Also in the advanced filter's results.
- **One find bar for Find and Find all** (#299), above the editor:
  text, Match case, Whole word and Regex, previous/next with "3 of 120"
  (every match counted, in the background), and Find all, which lists
  the matching lines in the panel below. Enter / Shift+Enter move between
  matches, Alt+Enter runs Find all, Escape closes the bar, Ctrl/Cmd+F
  opens it with the selected text. The toolbar keeps one Find button.
- **Settings: buttons and popups** (#301). Row actions in the settings
  tables are buttons; creating and editing users, folders and SSO group
  mappings opens a popup; deleting, revoking, deactivating and resetting
  a password ask in a popup instead of the browser's confirmation. A
  password reset shows the temporary password in a popup with Copy.
- **Source and role editors in a popup** (#302) over the list (or the
  source's own page) they were opened from, keeping their address, so
  links, back and reload still work. Closing with unsaved changes asks
  first.

### Deprecated
- **Content indexing, the Search page and the current Alerts go away in
  0.7** (#303). The content index is the only place PerchTail keeps text
  from the logs, against its promise that nothing from the sources is
  kept. In 0.7 the upgrade deletes the indexed text; searching inside a
  file stays (Find and Find all), file names get an index of their own,
  on by default and with no content (#304), and Alerts watch new lines
  live instead of the index (#305). PostgreSQL support comes in 0.7 too
  (#306).

### Fixed
- The Tail notice ("N new lines — jump to end") stays in the log's
  corner instead of covering the Find all results (#298).
- **Find all: no limit, and no missing rows.** Results stopped at 5,000
  matches, and a long list of long lines (debug logs) drew blank or
  missing rows while scrolling. Every matching line is now listed; only
  the rows in view are drawn, each showing at most 10,000 characters of
  its line (cut around the first match, marked with "…"). Copy and go to
  line still use the whole line. A search for a common letter in a
  100 MB log no longer runs the tab out of memory.

## [0.6.0-rc.2] - 2026-11-07

Second release candidate for 0.6.0, with the changes from the rc.1 lab
review. Upgrade notes, from 0.6.0-rc.1:
- No migrations.
- New System setting **Tail interval** (Settings → System): 1, 2, 5 or
  10 seconds, 2 by default. The interval each browser remembered in rc.1
  is no longer used.
- The per-user file order chosen in rc.1 is no longer used; the order
  set under Settings → System applies to everyone.
- Push agents don't need upgrading from rc.1.

From 0.5.3, follow the 0.6.0-rc.1 notes below as well.

### Changed
- **Follow is now called Tail** (#283). How often it checks for new
  lines is a System setting (Settings → System → Tail interval: 1, 2, 5
  or 10 seconds, 2 by default) instead of each user's choice, so sources
  see a known load; the viewer shows it next to the button while Tail is
  on. 30 seconds is no longer offered.
- **No sort control in the explorer** (#282): folders are listed in the
  order set under Settings → System. The per-user choice made in
  0.6.0-rc.1 is kept but not used for now; it comes back as a "Sort by"
  action on folders.
- **Advanced filter searches subfolders and whole customers, with regex**
  (#284). Ticking a source or folder now searches the folders below it
  right away (up to 6 levels, 300 folders or 90 seconds per source, two
  listings at a time per source and six overall), with progress and a
  Stop button. In the Tree view, customers and folders of sources can be
  ticked too: every source under them is searched. Regex and Match case
  options, as in Find all; a pattern with `/` matches the path below the
  ticked folder (`app/*.log`, `**/2026-11/*.gz`). An invalid regex says
  so. Results refresh by listing again the ticked folder and the folders
  that had files, not every folder searched.
- **Quieter viewer toolbar** (#286): buttons without borders, grouped
  (find, view, Tail) with thin dividers; only what's on gets a fill, and
  Tail shows a green dot while it runs. Copy lines, bookmarks, problems,
  Reload and Download are icons on the right, with tooltips.
- **Find all can be resized** (#285): drag the line on top of the panel,
  or focus it and use the arrow keys. This browser remembers the height.

### Fixed
- **Files that disappear, on Windows** (#272). Over WinRM (without a JEA
  endpoint), a file that was gone opened as an empty file, and Tail
  on it said "The source sent an unexpected answer"; a Windows push agent
  answered "The source returned an error". Both now say the file is no
  longer on the source, as SSH, SMB and Linux agents do.

## [0.6.0-rc.1] - 2026-11-07

First release candidate for 0.6.0. Upgrade notes:
- **Back up `DATA_DIR` first**, as for every release. Six migrations run
  on start; they only add columns and tables (update check state, agent
  version, password history, display name and email, user preferences,
  source status, the WinRM JEA endpoint).
- **Upgrade the push agents** to get incremental Follow, file times and
  the agent version in the source overview. Older agents keep working:
  Follow reloads the whole file, and their files show no time.
- **The update check makes one outbound request a day** to the GitHub
  releases API (nothing about the installation is sent). Set
  `UPDATE_CHECK=false` to turn it off entirely, for example on an
  air-gapped install.
- **A background check now connects to every source every 10 minutes**
  (one connection, not audited) to show its status. Change the interval
  or turn it off (0) under Settings → System.
- From source: reinstall `backend/requirements.txt`, which adds `pypsrp`
  for JEA endpoints.
- Settings → Security and Settings → Sessions moved to **My profile**;
  the old links lead there.
- New under Settings → System: password policy (NIST defaults, stricter
  rules off), open-folder refresh (30 s), default file order, source
  check interval, update check.

### Added
- **Update check** (#215). Once a day PerchTail asks the GitHub releases
  API for the latest release, and super admins see under
  **Settings → System → Updates** the running version, the latest one,
  a link to its release notes and the upgrade steps for a Docker or a
  from-source install. A "Check now" button checks on demand. On by
  default: turn it off with the switch, or with `UPDATE_CHECK=false`,
  which wins over the switch and means no outbound request at all. Only
  full releases are offered, never release candidates, and nothing about
  the installation is sent.
- **Push agent version.** Agents from this release report their version
  when they connect, and the source overview shows it, flagging an agent
  older than the server. Older agents show as "not reported yet".
- **Password policy** (#245), under **Settings → System**, for local
  accounts. Checked whenever a password is set: a user's own change, the
  change at first login, temporary passwords an admin picks, and resets
  (whose random password now meets the policy too). The rules show next
  to the password fields, and an error names the rule that failed. The
  defaults follow NIST SP 800-63B: 12 to 128 characters, no passwords
  that are too simple (repeats, sequences, keyboard runs, the username
  with small changes) and none containing the username or name.
  Optional, off by default: character classes, not reusing the last N
  passwords (only previous argon2id hashes are kept, and only while the
  rule is on) and expiry after N days, which makes the user change it at
  the next sign-in. Existing passwords are checked at their next change;
  with expiry on, they count from the upgrade.
- **My profile** (#243), opened by clicking your name in the top bar: your
  display name and email, password change, two-factor authentication and
  sessions on one page. Settings → Security and Settings → Sessions moved
  there (their old links still lead to it). The email is checked for
  format and must be unique; changing it is audited
  (`user.email_change`). SSO accounts get their name and email from the
  IdP's claims on each sign-in, read-only, and have no password section.
  Admins see name and email in the Users list, can set them when
  creating a user and edit them for local accounts. The password policy
  also refuses passwords containing the name or email.
- **Explorer: open folders refresh themselves** (#272). The folders you
  have open are listed again every 30 seconds (Settings → System, 0 to
  turn it off): only the list of files, never their content, one folder
  at a time per source, not while the browser tab is hidden, and less
  often for a source that just failed. New files are highlighted for a
  moment; files that are gone disappear; open subfolders stay open. A ⟳
  button on each open source and folder lists it right away, and
  re-opening a folder lists it again.
- **Files that disappear** (#272). Opening a file that's no longer on the
  source says so (no longer "wasn't found, or is hidden by the rules")
  and drops it from its folder. A file you're following that disappears
  stops Follow; its tab stays open with what it had, marked, with a note.
- **Explorer: file times and order** (#273). Each file shows when it was
  last modified, and a sort button orders folders by name (A→Z, Z→A),
  newest or oldest first, or size; folders come first. The default is
  set under Settings → System; each user's own choice is kept on the
  server and follows them, as does the list/tree choice for connections
  (a choice this browser remembered moves there on first use). Push
  agents older than this release don't report times: their files show
  none and sort last by time.
- **Source status** (#276). A dot before each source in the explorer
  says whether it answers: green reachable, red failing (with the reason
  and how long ago), amber for a disconnected push agent, grey not
  checked yet. The status is kept on the server and shared by everyone:
  a background check runs every 10 minutes (Settings → System, 0 = off,
  one connection per source, not audited), and every real operation
  (browse, open, Follow, search indexing) updates it too; an error about
  one file doesn't count. "Check now" on the source re-checks it, and the
  Sources list shows the same status. When PerchTail itself stops
  answering, a banner says so and retries until it's back.
- **Advanced filter in the explorer** (#274). An "Advanced filter" button
  next to the search box: tick sources and folders, on any servers, and
  the box then filters their files by name (shown with "Show the files"
  or as soon as you type), as text or as a glob
  (`app-2026-11*`, `*.log`). Each ticked one shows its matching files,
  in your sort order and with their times, and opens them on a click;
  chips list the selection, with "Clear all". It filters what's listed
  in those folders; "Search subfolders too" lists the folders below on
  request (up to 3 levels and 50 folders per selection). The selection
  lasts while the app is open, like the tabs.
- **WinRM without a remote shell: a read-only JEA endpoint** (#169). A
  WinRM source can name a PowerShell endpoint in **JEA endpoint**;
  PerchTail then speaks PSRP to it and only calls the functions of
  PerchTail's reference role (`docs/examples/jea`), by name and with
  typed parameters. The role runs in `NoLanguage` mode, lists and reads
  only under the folders its administrator allows, refuses junctions and
  links, and runs as the connecting account, so that account needs no
  administrator rights, no Remote Management Users membership and no
  `RootSDDL` change. `Register-PerchTailJea.ps1` installs it. Sources
  without an endpoint keep the WinRS shell as before.

### Changed
- **Follow fetches only the new lines** (#254). Instead of re-fetching
  the whole file every 2 seconds, Follow asks the source for what was
  appended after the bytes the viewer holds (SFTP and SMB seek, WinRM
  reads a range, the push agent has a new `read_range` command), and
  appends it to the editor, which keeps your place and selection. While
  you're at the end it scrolls with the new lines; scrolled up, a
  "N new lines — jump to end" button appears instead. A rotated or
  truncated file is detected (it shrank, or starts differently) and
  reloaded from the start, with a note. The interval can be set from
  1 to 30 seconds, and Follow pauses while the browser tab is hidden.
  Every poll still reads fresh from the source; following is audited
  once (`file.follow`), not on every poll. `.gz` files, archive members
  and push agents older than this release still reload the whole file.

## [0.5.3] - 2026-11-06

The release after 0.5.2. It carries everything from the pre-release
0.5.3-rc.1, whose section in
[CHANGELOG.md](https://github.com/quaresma870/perchtail/blob/v0.5.3/CHANGELOG.md)
lists every change. Since the rc, only the push agent changed: its read
buffer is now a fixed 1 MiB, so the chunk size PerchTail asks for can't
make it allocate more. The rc was retested in the lab on Linux and
Windows sources, including an audit log with old race breaks.

### Upgrading from 0.5.2
- **Back up `DATA_DIR` first**, with PerchTail stopped.
- One migration runs on start (`audit known breaks`). It records the
  newest audit row at the time of the upgrade, which is how 0.5.3 tells
  the old race breaks apart (see below).
- **Upgrade the push agents.** Agents older than 0.5.3 keep working with
  this version, but send each file in one message, which fails above
  about 12 MiB.
- Optional new setting: `SEARCH_INDEX_SOURCE_BUDGET_SECONDS` (default
  120).

### Highlights
- **Audit log:** if "Verify now" reported "Tampering detected" because of
  the race fixed in 0.5.2, it now shows those breaks apart, and a super
  admin can mark them as known (Settings → Audit log). Any other break is
  still tampering. Entries now name the source, user or object they're
  about, and keep that name after a rename or a delete.
- **Push agent:** files come in 1 MiB chunks, written as they arrive, so
  large logs open without the agent holding them in memory. A file over
  `MAX_FETCH_MB` is refused before it's read. The agent answers commands
  in parallel, so a long download doesn't hold up browsing, and stops
  reading when PerchTail gives up on a file.
- **Search indexing:** each source gets a time budget per sweep, so one
  large source can't hold up the others; files that can't be indexed are
  retried only when their size changes.
- **Settings:** the Sources and Users lists sort by any column and filter.
- **Viewer:** "⌕ Find" is a button that opens the search panel.

## [0.5.3-rc.1] - 2026-11-05

First release candidate for 0.5.3. Upgrade notes:
- **Back up `DATA_DIR` first**, as for every release. One migration
  runs on start (`audit known breaks`): it adds columns to the audit
  chain state and records the newest audit row at the time of the
  upgrade, which is how 0.5.3 tells old race breaks apart (see below).
- **Upgrade the push agents** to get chunked fetches, parallel commands
  and cancel. Agents older than 0.5.3 keep working with this backend, but
  send whole files in one message, which fails above about 12 MiB.
- Optional new setting: `SEARCH_INDEX_SOURCE_BUDGET_SECONDS` (default 120).
- If "Verify now" reports "Tampering detected" because of the race fixed
  in 0.5.2, it now shows those breaks apart, and a super admin can mark
  them as known (see the 0.5.2 known issue).

### Added
- Audit log: entries name what they're about (#239). Each event now
  records the target's name and where it sits (a source's "customer /
  folder", a rule's source, a grant's role), and the names next to ids it
  already held (`source_name`, `role_name`, `customer_name`,
  `scope_name`), so an entry still makes sense after the object is
  renamed or deleted. The table shows a readable Target column, linked
  when the object still exists, and the details as key/value pairs.
  Entries from before this release can't be changed (the hash chain
  covers them), so they show the object's current name, marked as such.
- Audit log: breaks in the hash chain left by the #213 race (concurrent
  writes before 0.5.2) are no longer reported as tampering forever (#217).
  "Verify now" reports them apart, as "N breaks match the #213 race", when
  each row is valid under the chain's key, chains from an existing earlier
  row, and was written before the upgrade to 0.5.3. A super admin can
  "Mark as known": this writes a chained `audit.chain.known_breaks_recorded`
  entry listing those rows and the fork's other rows, and verification
  then reports the chain as intact, with N known breaks. Any other break is
  still tampering, and so is a recorded row that later goes missing or
  changes.

### Fixed
- Push agent: files are fetched in 1 MiB chunks, written to the scratch
  file as they arrive (#205). Before, the agent read the whole file and
  sent it base64-encoded in one WebSocket message: a ~100 MB log took
  hundreds of MB of agent memory, and anything over about 12 MiB was
  larger than the backend's 16 MiB message limit, so the open failed and
  the agent's connection dropped. A file over `MAX_FETCH_MB` is now
  refused by the agent before it reads anything (HTTP 413, as for the
  other connectors). The fetch only times out when no chunk arrives for
  30 seconds, not after 30 seconds in all. **Upgrade the agents** to get
  this: an agent older than 0.5.3 still works with this backend, but
  sends whole files as before, and a 0.5.3 agent works with an older
  backend the old way. The agent also answers commands in parallel, so a
  long download no longer holds up browsing on the same agent, and stops
  reading a file when the backend gives up on it.
- Search indexing: one large source no longer holds the whole sweep (#218).
  A first index of a source with thousands of files took minutes, and
  meanwhile every other source's index, and the alerts that ride on it,
  went stale. Each source now gets at most
  `SEARCH_INDEX_SOURCE_BUDGET_SECONDS` (default 120) of reading per sweep
  and carries on where it left off in the next one. New files are read
  first, then the least recently indexed, so a source with many growing
  logs still works through all of them. Files that can't be indexed
  (binary, unreadable) are retried only when their size changes.
- Viewer: "⌕ Find" in the toolbar is now a button that opens the search
  panel. It looked clickable before but was only a hint for Ctrl/Cmd+F
  (#218).

### Changed
- Settings → Sources and Settings → Users can be sorted by any column,
  both ways, and filtered (#238): by text (name, host, customer or folder;
  username or role) and by protocol and status, or role, status and
  sign-in method. "N of M" shows while a filter is on. The sort and
  filters are kept while you open a row and come back. The Sources list
  gains a Customer column and shows each source's host; the Users list
  shows the sign-in method and the last login.
- Dependencies: docker/setup-qemu-action and setup-buildx-action 4,
  docker/metadata-action 6, docker/build-push-action 7, docker/login-action
  4, actions/upload-artifact 7 and download-artifact 8 (all actions in the
  CI and Release workflows now run on Node 24), @codemirror/commands
  6.11.1, @codemirror/autocomplete 6.20.4, @sveltejs/vite-plugin-svelte
  7.3.1, @playwright/test 1.64.1 and black 26.10.1.
- Dependabot no longer proposes sqlmodel 0.0.45 or later: those versions
  reject the naive datetimes PerchTail stores, so `requirements.in` keeps
  it below 0.0.45.

## [0.5.2] - 2026-11-05

The release after 0.5.0. There is no 0.5.1: its tag was used by mistake
for a pre-release of older code, and releases here are immutable. This
release carries everything from the pre-releases 0.5.1-rc.1 and
0.5.2-rc.2 (0.5.2-rc.1 was tagged but never released). Their sections in
[CHANGELOG.md](https://github.com/quaresma870/perchtail/blob/v0.5.2/CHANGELOG.md)
list every change. Both were retested in the lab on Linux and Windows
sources.

### Upgrading from 0.5.0
- **Back up `DATA_DIR` first**, with PerchTail stopped: copy the whole
  directory.
- A migration adds `source.agent_os`; it runs on start.
- The database switches to SQLite's WAL mode: `perchtail.db-wal` and
  `perchtail.db-shm` appear next to it. Keep `DATA_DIR` on a local disk,
  not a network share.
- **Upgrade the push agents** too: Windows agents need this version for
  their rules to ignore case, and all agents get the SIGTERM and link
  fixes.
- Installed from source? `/opt/perchtail` was owned by your build
  account; run `sudo chown -R root:root /opt/perchtail` once (see
  `docs/install-from-source.md`).

### Highlights
- **Security:** SMB and WinRM no longer follow junctions or symlinks out
  of the base path; SMB sessions are no longer shared between sources;
  changing a source's host or port requires its credential again; rules
  for Windows agents ignore case; `DATA_DIR` is readable by PerchTail's
  user only.
- **Search indexing** no longer locks the database, and skips a file it
  can't read instead of failing the whole source.
- **Audit log:** two requests writing at the same moment can no longer
  break the hash chain.
- **Push agent:** stops at once on SIGTERM, and leaves out links that
  lead outside its folder.
- **Releases** are built as a draft with the agent binaries,
  `SHA256SUMS` and an SBOM attached, then published.

### Known issue: audit chain breaks from before this release
Before 0.5.2, two requests writing audit entries at the same moment could
both chain from the same previous entry (fixed by #213). An install where
that happened keeps showing "Tampering detected" in Settings → Audit log
→ "Verify now", at the first such row. A break of this kind has a
recognisable signature in the database (`auditlog` table): the reported
row and the row before it have the **same `prev_hash`**, timestamps
**within the same second**, and are dated **before your upgrade to
0.5.2**. Treat any other break, or one dated after the upgrade, as
tampering. 0.5.3 will let a super admin record these breaks as known
(#217).

### Fixed (since 0.5.2-rc.2)
- Installing from source left `/opt/perchtail` owned by the account that
  built it, not root, so that account could change the code the service
  runs. Both `rsync` commands in `docs/install-from-source.md` now pass
  `--chown=root:root`, and the guide shows how to check and how to fix an
  existing install.
- A release's target commit is the tagged one; before, GitHub recorded the
  default branch for a tag on `dev`.

## [0.5.2-rc.2] - 2026-11-04

Second release candidate for 0.5.2. `v0.5.2-rc.1` was tagged but never
released: its Release workflow run failed before creating the draft, and
the e2e tests then found the audit log race below. Everything listed
under 0.5.2-rc.1 is in this release too; the upgrade notes there apply
(back up `DATA_DIR`, upgrade push agents).

### Fixed
- The audit log could report "Tampering detected" when two requests wrote
  audit entries at the same moment: both could chain from the same
  previous entry. Each writer now takes the database's write lock before
  reading the last entry, so the second waits for the first. More likely
  since the database moved to WAL mode in 0.5.2-rc.1.
- The Release workflow creates the SBOM's directory before writing it,
  and the draft job downloads only the agent binaries and the SBOM, not
  the build record that `docker/build-push-action` uploads.

## [0.5.2-rc.1] - 2026-11-04

Release candidate for 0.5.2, from the lab retest of 0.5.1-rc.1. There is
no 0.5.1 release: the `v0.5.1` tag was used by mistake for an earlier
pre-release of older code, and releases here are immutable, so 0.5.1-rc.1
is followed by 0.5.2. This is also the first release built by the new
draft-first Release workflow. Back up `DATA_DIR` before upgrading, and
upgrade push agents too (SIGTERM, link handling).

### Fixed
- Releases get their assets again. Releases in this repository are
  immutable, and the Release workflow attached the agent binaries,
  `SHA256SUMS` and SBOM after publication, which an immutable release
  refuses, so `v0.5.1-rc.1` has none. The workflow now builds everything
  into a **draft** release, from a tag push or a manual run, and a
  maintainer publishes it; `X.Y` and `latest` move when a full release is
  published. It also refuses a tag whose code carries another version
  (see CONTRIBUTING.md, "Releasing").
- WinRM TLS errors are decided by OpenSSL's verify code. A certificate
  issued for another IP address ("IP address mismatch") is now reported
  as a name mismatch, not as an unknown issuer.
- Search indexing skips a file it can't read (no permission, rotated away
  since the listing, grown past the limit) and indexes the rest. Before,
  one such file failed the whole source's sweep, every sweep.
- The push agent exits right away on SIGTERM. A connected agent used to
  wait for the next command, so `systemctl stop` took 90 s and ended in
  SIGKILL.
- The push agent leaves out links that lead outside its folder (or are
  broken), as SSH and local sources do, and browsing a file answers 404
  instead of 502.
- "Test connection" and browsing a source with no credential say so
  instead of showing a Python error.
- WinRM logs `winrm.path_outside_base` when a later block of a read is
  refused, not only the first check.
- Import points a port-only change without a credential at `.port`.
- The source editor shows a save error next to the Save button.
- The SSH key field on the import page wraps in narrow windows.
- System settings say that open sessions pick up a Connections view
  change within 30 seconds.
- The source page writes agent OS names properly ("Linux", "Windows").
- `DATA_DIR/winrm_ca` is `0700`, like the rest of `DATA_DIR`.

### Documentation
- `docs/install-from-source.md` installs the latest release tag rather
  than a fixed one, builds as an ordinary user and installs with `sudo`,
  lists `python3.12-venv`, gets Node.js 22 from nodejs.org with a checksum
  check, and keeps settings in `/etc/perchtail/perchtail.env`.

### Changed
- Dependencies: actions/checkout, setup-go, setup-node and setup-python 7,
  trivy-action 0.36.0, @codemirror/language 6.13.1, @codemirror/merge
  6.12.3, svelte 5.57.3, svelte-check 4.7.8, ruff 0.17.3 and pytest 9.2.0.
- CI installs the e2e SSH/SMB test servers in their own step, before
  Playwright starts.

## [0.5.1-rc.1] - 2026-11-04

Release candidate for 0.5.1: fixes for what the Hyper-V lab test of 0.5.0
found, for the lab to retest before the final release. Back up `DATA_DIR`
before upgrading; the database switches to WAL mode on first start (see
"Fixed"). Upgrade Windows push agents so their rules ignore case.

### Security
- SMB and WinRM sources no longer follow junctions or symlinks out of the
  base path. Before, anyone able to write to the log folder on the Windows
  host could create a junction (no special privilege needed) and expose
  any folder the service account can read to users with `view` on the
  source. Junctions and symlinks are now left out of listings, and a path
  through one answers 404 (`smb.path_outside_base` /
  `winrm.path_outside_base`). WinRM re-checks with every block it reads.
- SMB sessions are no longer shared between sources. smbclient's
  process-wide pool is keyed by host and username, so a source with the
  wrong password could read with another source's authenticated session,
  and a passing connection test didn't prove its credential was right.
  Every SMB request now authenticates with its own connection, which is
  closed afterwards.
- Changing a source's host or port now requires entering the credential
  again (in the editor, the API and bulk import), as the folder picker
  already did. Before, editing could send the saved credential (for SMB
  and WinRM, an NTLM exchange) to any new address.
- Rules for an agent running on Windows are now matched case-insensitively,
  as for SMB and WinRM sources. Before, `exclude secret/**` didn't hide a
  folder named `SECRET` on disk, though NTFS treats both as the same
  folder. The agent now reports its OS when it connects (shown as **Agent
  OS** on the source's page). Upgrade Windows agents to get this: older
  agents don't report it and keep case-sensitive rules.
- `DATA_DIR` is now readable by PerchTail's user only: on startup the
  directory is set to `0700`, and the database (with its WAL/SHM files),
  the credential salt and `known_hosts` to `0600`. Earlier versions left
  them `0644`, readable by any local user on a host install.

### Fixed
- SMB: a directory symlink in a folder no longer makes the whole listing
  fail with `SMBLinkRedirectionError`.
- WinRM: browsing a path that is a file answers 404 instead of listing the
  file under a doubled path.
- Search indexing no longer locks the database. The first index of a
  large source kept one write transaction open while it fetched every
  file, and for those minutes logins and anything else that writes failed
  with `database is locked` (HTTP 500). Each file is now fetched first and
  committed on its own, the database uses SQLite's WAL mode so reads don't
  wait for writes, and a write waits up to 30 s for another to finish
  instead of failing. Indexing also stops when the source is disabled or
  taken out of search mid-sweep.
- Scheduled jobs that start late (a busy worker) now run instead of being
  skipped, and runs that piled up collapse into one. A skipped indexing
  sweep also delayed alerts.
- Import refuses a rule whose pattern alone is in quotes. In an inline
  list (`rules: [include **/*, exclude "*/secret/**"]`) the quotes became
  part of the pattern, so the rule never matched.
- A folder ZIP over `MAX_FETCH_MB` is refused before anything is
  downloaded, using the sizes in the listing, and the 413 now gives the
  folder's size. Before, an SMB folder downloaded up to the limit first.
- The source page reloads when the source id in the URL changes.
- Paths the push-agent refuses (a link out of its folder) answer 404, as
  over SSH, instead of 502.
- TLS errors say whether the certificate is for another name, has expired
  or comes from an unknown issuer, instead of always "isn't trusted".
- Import keeps offering to set a credential for sources that have none,
  also after they've been imported, and takes an SSH private key as well
  as a password.
- Compare shows which source each side comes from.
- Machines and folders expanded in the explorer stay expanded after
  visiting Search or Settings, like the tabs.
- A change to the Connections view (System settings) reaches users who
  already have PerchTail open on their next navigation, without a reload.
- The source page shows the plain-HTTP warning for WinRM sources, as the
  editor does.
- `startup.data_layout.moved` is logged as a structured event in Docker
  too, and `logs.retention.size_limit` has the numbers as fields instead
  of in the event name.

### Added
- [docs/install-from-source.md](docs/install-from-source.md): running
  PerchTail on a Linux host without Docker, with a systemd unit.
- Releases now carry what's needed to install them (`.github/workflows/release.yml`):
  - push-agent binaries for Linux (amd64, arm64) and Windows (amd64), with `SHA256SUMS`;
  - the Docker image on `ghcr.io/quaresma870/perchtail`, for amd64 and arm64, with provenance and an SBOM;
  - the SBOM, also attached to the release.
  `latest` and `X.Y` image tags move only with full releases. The workflow can also be run by hand for an existing tag, to add these to a release published earlier.

### Changed
- `backend/requirements.txt` now pins every Python package, transitive
  ones included, so the image and a source install get exactly the same
  versions. The version ranges moved to `backend/requirements.in`; see
  CONTRIBUTING.md for how to regenerate the pins.

## [0.5.0] - 2026-11-04

Fixes and features from the Hyper-V lab test of 0.4.0. The viewer
becomes one explorer across every source. Sources can read several
folders, and their Base path can be picked by browsing the host. Searches
can be narrowed by customer, folder, source and path. The whole
configuration can be bulk imported and exported as YAML. Logs are capped
by size, and the database moves to its own directory. Folder visibility
now follows the rules (a security fix), and the WinRM docs match what
the connector really needs.

### Upgrading from 0.4.x

Database migrations run automatically on startup. Back up the `/data`
volume first.

- **The database, credential salt and SSH `known_hosts` move to
  `/data/db`** (#172). On the first start of 0.5.0 they are moved from
  `/data` automatically, salt first and database last, and the move is
  logged (`startup.data_layout.moved`). Nothing to do with the stock
  `docker-compose.yml`. If you set `DATABASE_URL`, `CREDENTIAL_SALT_PATH`
  or `SSH_KNOWN_HOSTS_PATH` yourself, those paths are kept as they are.
  If a database is still found at `/data/perchtail.db` while another one is
  in use in `/data/db`, startup logs `startup.data_layout.legacy_database_left`:
  stop PerchTail and move the right one into place. Update your backups to
  include `/data/db`.
- **Logs** now rotate at `LOG_MAX_FILE_MB` (100) as well as at midnight, and
  all logs together are capped at `LOG_MAX_TOTAL_MB` (2048). Archives are
  named `perchtail.log.<day>.<n>.gz`; existing `perchtail.log.<day>.gz`
  archives keep expiring under `LOG_RETENTION_DAYS`.
- **WinRM sources:** the documented JEA setup could never work with this
  connector, which needs an account with remote-shell rights. The docs now
  say so; prefer SMB or the push agent where possible (#169). Existing
  WinRM sources keep inferring HTTPS or HTTP from the port until they are
  saved in the editor, which stores the scheme explicitly.
- **SMB and WinRM base paths** are now validated when they change: an SMB
  source pointing at `C$` or a drive path, or a WinRM source with a
  relative path, keeps working but must be corrected the next time its
  path is edited (#159).

### Added
- **One explorer across every source** (#162):
  - machines expand in place in the viewer's sidebar, in list or tree
    view, and their folders load lazily;
  - tabs from several sources stay open side by side, each marked with
    its source, so the same `app.log` on two servers is two tabs;
  - Compare works across sources;
  - open tabs survive visiting Search or Settings and coming back (in
    memory only, closed on logout);
  - a source that fails to load shows its error on its own node;
  - `/viewer/<id>` now expands and reveals that source.
- **Find all, Notepad++-style** (#155):
  - one row per matching line, showing the whole line with every match
    highlighted and a hit count;
  - rows can be selected with click, Ctrl/Cmd+click, Shift+click or
    Ctrl/Cmd+A;
  - a right-click menu copies the selected lines (with or without line
    numbers), searches within the results, excludes lines matching a
    pattern (both chainable, with breadcrumbs to go back), and jumps to
    the line.
- **Source overview** (#157): creating a source lands on a read-only
  overview instead of back on its edit form. It shows the details, a
  prominent "No rules yet" callout, the rules, a connection test and, for
  agent sources, the enrollment token. Editing moved to
  `/settings/sources/<id>/edit`.
- **Settings → System → Connections view** (#156): a default list or tree
  view for everyone, and whether users may switch between them. When they
  may, each browser remembers its choice.
- **Pick the Base path by browsing the host** (#159): **Browse…** next to
  the field lists folders over SSH, drives then folders over WinRM, and the
  host's shares then folders over SMB (share listing via SRVSVC, with no
  new dependency). Folders only, never file names; limited to users who
  can manage sources and audited. A saved credential is only reused for
  the host and port it was saved for.
- **Several folders in one source** (#161): labelled folders on the same
  host, each shown at the top level of the source's tree. Rules see the
  labelled paths (`**/*.log` for all, `IIS/**` for one), and the connection
  test checks each folder. Push-agent sources stay single-folder. How many
  folders a source may have is set in Settings → System → Folders per
  source: 20 by default, 1 turns the feature off, 100 at most. Lowering it
  never locks an existing source out: it keeps its folders but can't add
  more.
- **Search scope** (#163): narrow a search to any mix of customers,
  folders (with their sub-folders) and sources, and to file paths
  containing some text or matching a glob. The scope is applied before the
  result limit, kept in the URL and remembered per browser. The API takes
  `customer_id`, `folder_id`, `source_id` (repeatable) and `path`.
- **WinRM scheme and CA certificate per source** (#169): an explicit
  HTTPS/HTTP choice instead of guessing from the port, with each scheme's
  default port, and an optional CA certificate (PEM) trusted for that
  source only, for internal PKI or self-signed listeners, without a
  server-wide `REQUESTS_CA_BUNDLE`.
- **Log size limits** (#172): `LOG_MAX_FILE_MB` rotates the log before
  midnight when it grows past it, and `LOG_MAX_TOTAL_MB` caps all logs,
  deleting the oldest archives first. When that cap deletes logs that
  `LOG_RETENTION_DAYS` would have kept, a warning is logged and
  `/monitoring/health` reports `logs.size_limit_deleted_early`.
  `LOG_QUIET_THIRD_PARTY=true` keeps PerchTail's own DEBUG logging while
  capping protocol libraries (smbprotocol, spnego, paramiko, ...) at INFO.
- **Bulk import and export** (#179): Settings → Import / export (super
  admins) creates and updates customers, folders, sources and rules from
  one YAML file, with a preview of every change, credentials from the
  file, `${env:PERCHTAIL_IMPORT_*}` or typed in, and an all-or-nothing
  apply. Importing the same file again changes nothing; nothing is ever
  deleted. Export writes the same format, without credentials. See
  `docs/bulk-import.md` and `docs/examples/perchtail-import.example.yaml`.

### Changed
- State is split by kind (#172): `DATA_DIR` (`/data/db` in the image)
  holds the database, credential salt and SSH `known_hosts`, apart from
  `LOG_DIR` (`/data/logs`) and `SCRATCH_DIR` (`/data/scratch`), so each can
  be its own volume and a log burst can't fill the database's disk. See
  the upgrade notes above.
- Base path is checked per protocol when a source is created or its path
  changes (#159): SMB takes a share name (no drive paths like `C:\Logs`,
  no `C$`/`ADMIN$`), WinRM an absolute Windows path on any drive. The
  editor shows a protocol-specific placeholder and hint. Existing sources
  aren't re-checked until their path is edited.
- Changing a source's host, port, base path or folders clears its search
  index and alert positions, since the files are no longer the same, and
  the `source.update` audit entry now records old and new values (the
  credential only as "replaced").
- The window itself no longer scrolls (#154). The navigation stays in
  place, and in the viewer only the log pane, the explorer and the Find
  all panel scroll. Before, a large log stretched the whole page.
- A failed open, reload or compare now shows a banner above the editor
  with the server's reason, e.g. "This file is 100.0 MB, over this
  server's 64 MB limit (MAX_FETCH_MB)", plus a Try again action when
  useful. The explorer is no longer wiped. The 413 body now includes
  `size_bytes` and `limit_bytes` (#160).
- `/open` is served inline as text instead of as an attachment.
  Download icons and actions are hidden from users without the
  `download` capability on that source, using the new
  `my_capabilities` field of each source (#174).

### Fixed
- WinRM sources whose base path is a drive root (`D:\`) listed the
  current directory on that drive instead of its root.
- Source editor and role editor form controls: checkboxes and toggles
  sit next to their labels again (#158).
- Secret fields (SSH private key, passwords, SSO client secret) opt out
  of browser spellcheck and grammar extensions such as Grammarly, which
  could otherwise send their contents to third-party servers. The
  private key field is monospace without soft wrapping (#158).

### Security
- Rules now decide which **folders** are visible, not only which files.
  Before, every folder was always listed. An excluded folder, and through
  direct API calls its whole tree of sub-folder names, stayed visible, and
  a source with no rules showed its complete directory skeleton. A folder
  is now shown and browsable only if some file below it could be visible
  under the same ordered, last-match-wins rules. Hidden folders answer 404
  on browse and folder ZIP, and the search indexer no longer walks them
  (#164). **Upgrade note:** folders that only contain files your rules
  don't show now disappear from the tree. Root-level patterns such as
  `*.log` never cross `/`, so they show no folders at all: use `**/*.log`
  to include matching files at any depth. Regex rules are analysed
  conservatively; to hide a folder reliably, use a glob exclude such as
  `folder/**`.
- Failed logins, account lockouts and login attempts while locked out are
  now recorded in the audit log and the app log (`user.login_failed`,
  `user.locked_out`, `user.login_throttled`), with the attempted username,
  client IP and reason, never the password. Repeated identical failures
  within 60 s are folded into one row (#171).
- Connection checks no longer show raw library errors (UNC paths, host
  keys, internals) to users who can only view a source. They get an
  actionable message instead, and users who can manage sources also see
  the underlying error (#170).

### Fixed
- Push-agent sources can be browsed and opened again. Browse, open and
  download answered 501 "protocol agent not yet supported" while the
  connection check passed. All code paths now share one connector
  registry (#165).
- SMB: the live log of a Windows service, held open for writing by
  another process, can now be opened instead of failing with
  `STATUS_SHARING_VIOLATION` (#166). A file over `MAX_FETCH_MB` is refused
  before transferring anything, instead of after downloading up to the
  limit, which took minutes (#167).
- WinRM: a folder containing a file with an accented or other non-ASCII
  name can now be listed. Before, the whole listing failed with a decode
  error (#168).
- Predictable source-side failures get their own status and a clear
  message instead of a bare 500:
  - no read permission: 403;
  - not found: 404;
  - file locked: 409;
  - unreachable, authentication, SSH host key mismatch or untrusted TLS
    certificate: 502.

  An SMB Base path that isn't a share now says so (#170).
- Archives (`.zip`, `.tar.gz`) are browsed as a folder tree instead of a
  flat list of every member. Members with odd names (`../`, absolute) are
  shown normalized and still open (#175).

### Documentation
- WinRM setup now states what the connector actually needs: an account
  with remote-shell rights (Administrators, or `GXGR` in the WinRM
  `RootSDDL`). The JEA instructions, which couldn't work, are gone; moving
  to PSRP so a JEA endpoint can constrain the account is tracked in #169.
  SMB or the push agent is recommended where possible.
- New: base path per protocol, the folder picker and several folders per
  source (`docs/source-setup.md`), bulk import (`docs/bulk-import.md`),
  the data directories (README), and the `logs` health fields
  (`docs/monitoring.md`).
- SSH/SFTP setup: `ForceCommand internal-sftp` is required with a
  `nologin` shell; chroot is optional hardening, with the sshd ownership
  rule and a bind-mount example for Debian/Ubuntu; with a chroot, Base path
  is relative to it (#173). Folder visibility and SSH rule case-sensitivity
  are documented with the rule principles.

## [0.4.1] - 2026-11-03

A security fix release. SSH/SFTP sources no longer follow symlinks out
of the base path. **Upgrade now if you use SSH sources without a
`ChrootDirectory`.** No migrations and no configuration changes: replace
the container.

### Security
- SSH/SFTP sources no longer follow symlinks out of the base path. A
  symlink inside the base path (for example `escape -> /etc`) let anyone
  with `view` on an SSH source that isn't chrooted browse the link target
  and read any file the SSH account could read, such as `/etc/passwd`.
  Paths are now resolved on the server (SFTP `realpath`) before every
  listing and read, and anything that resolves outside the resolved base
  path gets the same 404 as a path hidden by the rules. Outward and
  dangling symlinks are left out of listings. Symlinks that stay inside
  the base path still work, and are shown as the file or folder they
  point to. Refusals are logged as `ssh.path_outside_base`. Found in the
  Hyper-V lab tests. The push agent got the equivalent fix in 0.3.0;
  local sources were already contained.

## [0.4.0] - 2026-11-03

A security-hardening release: fixes from the internal review that
prepares the external pentest (#124). It includes one database migration
(applied automatically on start) and several changes that need action
when upgrading. **Before upgrading, check:**

1. **Container user:** the container now runs as uid/gid 10001, not root.
   Host directories mounted for local-disk sources (`LOCAL_SOURCE_ROOTS`)
   must be readable by uid 10001. `/data` is fixed up automatically.
2. **Alerts:** creating or editing alerts needs the new `manage_alerts`
   global capability, and an alert only fires while its owner holds it.
   Grant it to the roles that use alerts.
3. **Regex rules:** `re:` rules now use RE2. A rule that uses lookaround
   or backreferences fails closed (logged as `rules.invalid_regex`) and
   must be rewritten.
4. **Push agent:** a `ws://` server URL is refused unless
   `PERCHTAIL_ALLOW_INSECURE=true`. Use `wss://`.
5. **SSO:** accounts are now bound to the IdP's issuer. Existing accounts
   are bound on first start when exactly one OIDC provider is configured
   or enabled. Otherwise watch for a `sso.issuer_backfill_skipped`
   warning.
6. **Delegated SSO admins** (`manage_sso` without super admin) can no
   longer change the trusted IdP or group claim while a group mapping
   grants a role above their own.

New setting: `MAX_FETCH_MB` (default 2048) caps each remote fetch and
each folder ZIP.

### Security
- The push agent now refuses to start with a plain `ws://` server URL,
  because its enrollment token and every file it serves travel over that
  connection. Use `wss://`, or set `PERCHTAIL_ALLOW_INSECURE=true` to
  opt in for testing or a fully trusted network. **Upgrade note:** an
  agent configured with `ws://` and no opt-in exits on start with an
  error saying so.
- A delegated SSO admin (`manage_sso` without super admin) could escalate
  to any role a group mapping grants, including Super Admin, by pointing
  the provider at an IdP or group claim they control and signing in as a
  member of the mapped group. Creating a provider, enabling one, or
  changing its issuer, client id or group claim now returns 403 unless
  every group mapping's role is within the admin's own authority, the
  same bar as creating such a mapping directly. Renaming, rotating the
  client secret and disabling stay open to them.
- Regex rules (`re:`) are now compiled with RE2 (`google-re2`), which
  always matches in linear time. A catastrophic pattern such as `(a+)+$`
  can no longer stall every browse of a source (ReDoS). RE2 has no
  backreferences or lookaround, so a rule using them is rejected with a
  400 that says why. **Upgrade note:** an existing rule that uses them
  fails closed (a broken include matches nothing, a broken exclude hides
  everything) and is logged once as `rules.invalid_regex`; rewrite it.
  Severity patterns run in the browser, not on the server, so they keep
  full regex syntax.
- SSO accounts are now identified by the IdP's issuer and subject
  (`iss` + `sub`), not by `sub` alone. `sub` is only unique within one
  IdP, so after the SSO provider was pointed at a different issuer, a user
  there whose `sub` matched an existing account's would have signed in as
  that account, with its role. **Upgrade note:** on first start, existing
  SSO accounts are bound to the enabled OIDC provider's issuer (or the
  only one configured). If that is ambiguous, they are left unbound and a
  `sso.issuer_backfill_skipped` warning is logged: those users then get a
  new account at their next login, which clashes on username until an
  admin renames or deletes the old one.
- Turning off full-text search for a source, or disabling the source, now
  deletes the search snippets already stored for it, along with its
  alerts' per-file progress. Before, they stayed in the database
  indefinitely. Each indexing sweep also purges leftovers from sources
  that are no longer active, which covers anything left by earlier
  versions.
- Remote fetches are capped by a new `MAX_FETCH_MB` setting (default
  2048). The cap applies per file and to a folder ZIP's total. Over the
  limit, the request gets HTTP 413 and no partial file is left behind. SSH
  checks the size before transferring anything; SMB streams in chunks and
  stops at the cap. WinRM no longer reads a whole file into memory in one
  PowerShell call: it checks the size first, then reads byte ranges, and
  opens the file with shared read/write access, so a log that is still
  being written can be read. Folder ZIPs stop as soon as their total
  passes the cap, instead of growing outside the scratch size guard.
- Opening a file (`file.open`) and downloading a folder as ZIP
  (`folder.download`, with its file count) are now recorded in the audit
  log, like single-file downloads already were. Viewing a file necessarily
  sends its full content to the browser, so `view` can read whole files.
  `download` gates bulk export on top of that, and the role editor now
  says so on each capability.
- Alerts are now treated as the export channel they are. Creating,
  editing, enabling or testing an alert needs a new global capability,
  `manage_alerts`. Anyone can still list and delete their own alerts.
  An alert only fires while its owner holds `manage_alerts`, re-checked at
  every evaluation like source access. Webhook targets must be publicly
  routable (an `is_global` allowlist), which also rules out carrier-grade
  NAT, documentation and benchmarking ranges and IPv4-mapped IPv6.
  Creating, editing and deleting an alert is audited; the audit log
  records only the webhook's host, never its path or query, which often
  carry a token. **Upgrade note:** grant `manage_alerts` to the roles that
  should keep using alerts. Until you do, their existing alerts stop
  firing.
- The Docker image now runs PerchTail as an unprivileged `perchtail` user
  (uid/gid 10001) with no Linux capabilities, instead of as root. The
  entrypoint starts as root only long enough to give `/data` to that user,
  so existing root-owned volumes keep working, then drops privileges.
  **Upgrade note:** host directories mounted for local-disk sources
  (`LOCAL_SOURCE_ROOTS`) must be readable by uid 10001, for example
  `setfacl -R -m u:10001:rX /var/log/app` on the host.
- CI: `aquasecurity/trivy-action` is pinned to `v0.35.0` instead of
  `@master`. That action's tags were force-pushed to credential-stealing
  code in March 2026, and a branch reference would have run whatever was
  pushed there. `actions/checkout` and `actions/setup-python` move to their
  Node 24 majors, and Dependabot now also keeps the workflow's actions up
  to date.
- Login lockout can no longer be reset by flooding the throttle with other
  usernames. An entry only leaves the tracker once it's idle (unlocked,
  with no failure inside the lockout window). If the tracker fills with
  live entries, new usernames are refused with 429 instead of evicting
  someone else's count.
- A login with an unknown, inactive or SSO-only username now costs the
  same argon2 verify as a wrong password, so response timing no longer
  reveals which usernames exist.
- `/auth/change-password` is throttled like login. Changing your password
  signs out every other session. An admin password reset signs the user
  out everywhere.

### Fixed
- An agent that reconnected before the server noticed its old connection
  had dropped showed as disconnected, and every browse of its source
  failed, until it reconnected again. The old connection's cleanup was
  removing the new one. Now a new connection replaces the old one
  cleanly: the old socket is closed, commands still waiting on it fail at
  once instead of timing out, and late replies from it are ignored.
- Expired login sessions are now deleted hourly instead of piling up
  forever, and a session's `last_seen_at` is written at most once a
  minute instead of on every authenticated request.

## [0.3.1] - 2026-11-03

A bug-fix release: SSH sources now accept ed25519 and ECDSA private keys,
not only RSA. No migrations and no configuration changes; upgrade by
replacing the container.

### Fixed
- SSH sources only accepted RSA private keys, even though
  `docs/source-setup.md` tells you to generate an ed25519 one. Pasting an
  ed25519 or ECDSA key failed on every connection. All three key types
  now work, in OpenSSH or PEM format. A passphrase-protected key, or text
  that isn't a key, now fails with a clear message instead of a parser
  error.

## [0.3.0] - 2026-11-02

A security release on top of new viewer and navigation features. A full
security and bug review of the codebase, plus a CodeQL code-scanning pass,
found and fixed the following:
- delegated admins could escalate themselves to super-admin;
- admin-created local-disk sources could read anywhere on the PerchTail
  host;
- exclude rules could be bypassed with a non-canonical path;
- deleted ids could be reused and inherit their old access;
- WinRM path quoting missed the Unicode quote characters;
- archives had no decompression limit (zip bombs);
- the Go push-agent could follow a symlink out of its folder;
- first-use SSH host-key trust was silent: every pin is now logged with
  its fingerprint, and a strict mode refuses unknown hosts.

Also new since 0.2.0:
- optional TOTP/MFA for local accounts;
- tamper-evident (HMAC hash-chained) audit log;
- viewer Compare, Beautify/Minify, Follow and multi-pattern Highlight;
- a tree view of all connections, with a Folders admin page.

Bugs fixed: concurrent opens of one file corrupting each other, invalid
regex rules breaking a whole source, and alerts that missed new matches
and repeated old ones.

### Upgrading from 0.2.0

Database migrations run automatically on startup. Back up the `/data`
volume first. Before you restart, check the following:

- **Local-disk sources** (other than the built-in application-log source)
  are now off by default. Set `LOCAL_SOURCE_ROOTS` to the directories they
  read from, comma-separated. Until you do, those sources answer 403.
- **Docker deployments, do this before you recreate the container:** the
  credential salt and SSH `known_hosts` now live on the `/data` volume.
  While the *old* container is still running, copy them there:

  ```sh
  docker compose exec perchtail sh -c 'cp -n /app/backend/data/credential_salt /data/ 2>/dev/null; cp -n /app/backend/data/ssh_known_hosts /data/ 2>/dev/null; ls -l /data/credential_salt'
  ```

  If you skip this step, the new version refuses to start rather than
  silently losing every stored credential, SSO secret and MFA secret.
- **SMB/WinRM rules** now match case-insensitively, as Windows does.
- **Regex rules** that don't compile are now rejected when saved. Any
  already stored fail closed: a broken include matches nothing, and a
  broken exclude hides everything. Check the ERROR log for
  `rules.invalid_regex` after upgrading.
- New optional settings: `MAX_DECOMPRESSED_MB` (default 2048) and
  `SSH_HOST_KEY_POLICY` (`tofu`, the default, or `strict`).

### Added
- Viewer: Compare (a read-only side-by-side diff against a second file
  picked from the tree or another open tab, via `@codemirror/merge`),
  Beautify/Minify for JSON and XML tabs (display-only, never touches the
  file on disk), Follow (a `tail -f`-style toggle that polls and re-fetches
  the open file every few seconds), and multi-pattern "Highlight" (ad hoc,
  session-only pattern highlighting, Notepad++'s Mark feature, each
  pattern in its own color). See ROADMAP.md's notes for design decisions,
  including two real bugs (a scroll-to-top regression on Follow, a
  double-add on Enter in the highlight input) found and fixed while
  building these.
- Connections home: a "List"/"Tree" toggle for "All connections", showing
  the real nested customer → folder → … → source hierarchy (built
  client-side from a new `folder_path` field on each source), alongside
  the existing flat list. A new "Folders" admin page (Settings → Folders)
  covers create/rename/move/delete for folders. See ROADMAP.md's notes,
  including a real RBAC information-disclosure issue (a narrowly-scoped
  folder/source grant could leak ancestor folder names above its own
  scope) found via code review and fixed before this shipped.

### Security
- SSH host keys are now pinned through PerchTail's own trust-on-first-use
  policy instead of paramiko's `AutoAddPolicy`. The behaviour is the
  same, but every newly pinned key is now logged at WARNING
  (`ssh.host_key_pinned`) with its OpenSSH-style `SHA256:` fingerprint,
  so you can check it against the server. The new
  `SSH_HOST_KEY_POLICY=strict` setting refuses any server that isn't
  already in `SSH_KNOWN_HOSTS_PATH`. Resolves the CodeQL alert
  `py/paramiko-missing-host-key-validation`.
- The Go push-agent now reads through `os.Root`, so a symlink inside its
  watched folder can no longer point a list or fetch at any other file on
  that machine. The lexical `..` check stays as the first line of
  defence. Found by CodeQL (`go/path-injection`).
- The CI workflow's `GITHUB_TOKEN` is limited to `contents: read`
  (CodeQL `actions/missing-workflow-permissions`).
- Local-disk path containment uses `os.path.realpath` and a
  separator-terminated prefix check, which static analysis recognises.
  The behaviour is the same, and an allowlist entry of `/` still works.
  Opening or downloading with an empty path is now a 400. Before, an
  empty path named the source's base directory itself and failed with a
  500.
- Opening a `.gz` file or a `.zip`/`.tar.gz` member is now capped at
  `MAX_DECOMPRESSED_MB` of decompressed output (default 2048). Previously a
  small crafted archive (a "zip bomb") could expand without limit and
  fill the scratch disk. The cap counts the bytes actually produced, not
  the size the archive header claims. Going over it returns HTTP 413 and
  deletes the partial file. The search indexer now stops decompressing at
  its own per-file limit instead of decompressing in full first.
- Delegated admin capabilities can no longer be used to reach
  super-admin. A non-super-admin can now only assign, edit, grant on, or
  map an SSO group to a role that isn't super-admin and carries no global
  capability they don't hold themselves, and can't reset the password of,
  deactivate, or edit a user more privileged than themselves. Built-in
  roles ("Super Admin", "No Access") are now immutable, the last active
  super-admin can't be deactivated or demoted, and role/grant updates and
  deletions are now recorded in the audit log.
- Admin-created "Local disk" sources are now off by default and can only
  point under directories the server operator lists in the new
  `LOCAL_SOURCE_ROOTS` environment setting. Previously anyone with
  `create_source` could choose any base path on the PerchTail host itself.
  Local reads are also confined to the source's base path, so a symlink
  can't lead outside it. **Upgrade note:** existing local-disk sources stop
  working (403) until `LOCAL_SOURCE_ROOTS` covers their base path. The
  built-in application-log source is unaffected.
- Exclude rules can no longer be sidestepped by requesting a
  non-canonical spelling of the same path. Browse/open/download/close now
  reject `.` and empty path segments, backslashes, `:` and control
  characters. SMB and WinRM sources also reject trailing dots/spaces and
  8.3 short names, and match rules case-insensitively, as Windows does.
  **Behavior change** for SMB/WinRM sources: rules that previously relied
  on case to tell files apart now match regardless of case.
- Deleting a customer, folder, source or role can no longer hand its access
  to whatever gets created next. SQLite reused the highest deleted id, and
  the role grants, search-index snippets, alerts and severity patterns left
  pointing at the old row silently attached to the new one, potentially
  under a different customer. Those tables now use `AUTOINCREMENT`,
  deletes remove everything that pointed at the deleted row, a role can't
  be deleted while an SSO group mapping still targets it, and a migration
  cleans up rows already orphaned this way and starts every id sequence
  past any id the audit log has ever recorded.
- WinRM sources now escape every character PowerShell accepts as a
  single-quote string delimiter (the ASCII apostrophe and the Unicode
  curly-quote variants U+2018–U+201B) when embedding a path in a command.
  Previously only the ASCII apostrophe was escaped, so a crafted path could
  end the quoted string early on the Windows host.
- Optional TOTP/MFA for local accounts (`pyotp`). Self-service only, via a
  new Settings → Security page: enroll by scanning a QR code (or entering
  the secret manually), confirm with a live code before it's actually
  turned on, and get 10 one-time backup codes shown exactly once. Once
  enabled, `/auth/login` requires a second factor (a live TOTP code or an
  unused backup code) after the password; a wrong code counts against the
  existing login-throttle lockout, a missing one doesn't. A live TOTP code
  is single-use, not just time-limited — replaying an already-accepted code
  within its normal validity window is rejected. Enrolling, disabling, and
  regenerating backup codes all require re-confirming the current password,
  throttled the same way as login itself, so a stolen session token alone
  isn't enough to take over an account's second factor. The TOTP secret
  is Fernet-encrypted at rest (like `Source.credential_ref`); backup codes
  are one-way argon2 hashes. See ROADMAP.md's security-hardening notes for
  the full design, including why login finalization (`last_login_at` /
  the `user.login` audit event) had to move behind the MFA check rather
  than firing on password-correct alone.
- Audit log tamper-evidence: `AuditLog` rows are now HMAC-SHA256
  hash-chained (`app/audit_hash_chain.py`), keyed by a second key derived
  from `CREDENTIAL_ENCRYPTION_KEY` (domain-separated from the credential
  Fernet key, `app.crypto.audit_chain_key`) so the chain can't be silently
  recomputed by anyone who only has the SQLite file itself. Altering or
  deleting a row anywhere changes every hash after it. A new periodic
  verification job (`app/audit_integrity.py`) walks the chain on its own
  configurable cadence (`audit_integrity_check_interval_days`, default 365
  — explicitly independent of the retention window and purge cadence), and
  a manual, cooldown-throttled "Verify now" action on the Audit Log page
  runs the same check on demand. Existing history is backfilled
  automatically on first startup after upgrading; the retention purge
  sweep now deletes only a genuine id-prefix of the table (not just
  whatever matches the timestamp filter, which could rarely diverge from
  id order under concurrent writes) and advances a persisted chain anchor,
  so routine purging doesn't itself look like tampering to the next
  verification pass. See ROADMAP.md's security-hardening notes for the
  full design, including its one documented, accepted limitation around a
  narrow concurrent-write race.

### Fixed
- Alerts missed new matches and repeated old ones. A new matching line
  could be missed when more than 200 older matches ranked above it,
  because only one top-200 search across the whole index was checked.
  Every append to a file also re-sent all of that file's old matching
  lines. Each changed file is now searched on its own, starting after the
  last line that alert already reported. A rotated or rewritten file
  reports from the top. Adds the `alert_file_state` table (migration
  `c5e1a7d93b20`).
- Two viewers opening the same file at the same moment could corrupt each
  other's view. Both fetches wrote into one shared scratch file, so the
  second fetch truncated and rewrote it while the first response was
  still streaming. Each fetch now gets its own scratch file, and the
  reference-counted purge removes them all once the last viewer closes.
- A rule (or severity pattern) with an invalid regex, such as an
  unbalanced `(`, used to save fine and then make every browse, open and
  download of that source fail with a server error. Invalid regexes are
  now rejected with a 400 when saved, and a raw-text paste names the bad
  line and leaves the existing rule chain untouched. Rules saved before
  this change fail closed: a broken include rule matches nothing, and a
  broken exclude rule hides everything rather than exposing what it was
  meant to hide. Each broken pattern is logged once at ERROR.
- The Docker image now keeps the credential-encryption salt and SSH
  `known_hosts` on the `/data` volume (`CREDENTIAL_SALT_PATH`,
  `SSH_KNOWN_HOSTS_PATH`). Previously both defaulted to a path inside the
  container's own filesystem, so recreating or upgrading the container
  silently generated a new salt, which made every stored credential, SSO
  client secret and MFA secret undecryptable, made the audit-log integrity
  check report the whole chain as broken, and reset SSH host-key pinning.
  Startup now also refuses to generate a fresh salt while the database
  already holds data encrypted under the old one, instead of failing later
  (`CREDENTIAL_SALT_ALLOW_REGENERATE=true` overrides this if the original
  is permanently lost). **Upgrade step for Docker deployments:** while the
  *old* container is still running, copy both files onto the volume first,
  then upgrade as usual:

  ```sh
  docker compose exec perchtail sh -c 'cp -n /app/backend/data/credential_salt /data/ 2>/dev/null; cp -n /app/backend/data/ssh_known_hosts /data/ 2>/dev/null; ls -l /data/credential_salt'
  ```
- `OriginCheckMiddleware`'s CSRF defense-in-depth check was blocking every
  state-changing request — including login — when deployed behind a
  TLS-terminating reverse proxy (the standard, documented setup): uvicorn
  runs with no proxy-header trust configured, so it always saw the
  proxied connection's scheme as `http`, while a real browser's `Origin`
  header correctly said `https`, and the check compared both. It now
  compares only `host[:port]`, matching what the middleware's own
  docstring already claimed it did. Found and verified while writing the
  reverse-proxy deployment docs, before anyone hit it in production — see
  ROADMAP.md's CSRF-review notes for the full reproduction and rationale.
- CI: the `docker-image` job's Trivy scan was failing on every build (not
  just feature PRs) because `python:3.12-slim`'s baked-in OS packages had
  fallen behind Debian's own published security patches between upstream
  rebuilds. The `Dockerfile`'s backend stage now runs `apt-get update &&
  apt-get upgrade -y` before installing Python dependencies, pulling
  current packages from the live Debian security mirror at build time.

## [0.2.0] - 2026-09-08

Phase 2 (push-agent) and Phase 3 (full-text search, alerting, IdP
group-claim-to-role auto-mapping, external monitoring) complete, plus a
full admin audit log viewer, a redesigned connections home page, a set of
viewer toolbar/editor improvements, and an extensive pre-1.0
security-hardening pass — login lockout, security response headers,
CI-blocking dependency and container-image vulnerability scanning,
`CREDENTIAL_ENCRYPTION_KEY` rotation with a tooled migration path, salted
PBKDF2 credential-key derivation, persisted SSH host-key pinning, a
Sessions page for revoking a login remotely, a CSRF review, and two real
vulnerabilities found and fixed via a full security-review pass: a
DNS-rebind SSRF gap in outbound alert webhooks and a login-CSRF gap in
OIDC SSO. Covered end-to-end by a new Playwright e2e suite that drives
the real browser UI against real SSH/SMB test servers, not just mocks.
**Breaking for existing deployments**: the credential-encryption key
derivation change means previously-encrypted credentials need
`python -m app.rotate_credential_key` (or reconfiguring sources/SSO from
scratch) after upgrading — see
[docs/credential-key-rotation.md](docs/credential-key-rotation.md).

### Security
- Fixed a residual SSRF gap in the alert webhook safety check (found via a
  full security-review pass over everything merged since v0.1.1, follow-up
  to #49): the pre-send re-check re-validated the plain hostname and then
  handed it straight to `httpx.post`, which resolves DNS independently at
  connect time — an attacker's own authoritative DNS server could answer
  the validation query with a public address and the connection query,
  moments later, with an internal one (e.g. cloud metadata), since those
  were always two separate lookups no matter how close together. `send_webhook`
  now resolves and validates the hostname exactly once
  (`webhook_safety.resolve_pinned_webhook_target`) and connects directly to
  that specific already-validated IP, with the original hostname preserved
  as the `Host` header and TLS SNI/certificate-verification name — nothing
  re-resolves the hostname between validation and connection.
- Fixed a login-CSRF gap in OIDC SSO (issue #61): the OAuth `state` param
  was never bound to the browser that started the flow, so a code+state
  pair captured from an attacker's own successful login could be replayed
  by a victim's browser, silently logging the victim into the attacker's
  PerchTail account. `/auth/sso/login` now also sets a short-lived,
  `httponly`, `samesite=lax` cookie carrying the same `state` value;
  `/auth/sso/callback` rejects any request whose `state` doesn't match
  that cookie, closing the gap without needing a server-side flow table.
- New "Sessions" page under Settings (visible to every user, like Sources)
  lists every device/browser currently signed in as you and lets you
  revoke one remotely — a prerequisite for "someone else is logged in as
  me" incident response. Sessions now record the browser's `User-Agent` at
  login so rows in the list are actually distinguishable from each other.
  Self-service only — a user can see and revoke only their own sessions.
- `CREDENTIAL_ENCRYPTION_KEY` can now be rotated without losing access to
  already-encrypted credentials: `python -m app.rotate_credential_key`
  (with `--dry-run` to preview first) re-encrypts every `Source.credential_ref`
  and `SSOProviderConfig.config` row from the old key to a new one in a
  single all-or-nothing transaction. See
  [docs/credential-key-rotation.md](docs/credential-key-rotation.md).
- CSRF review: confirmed the existing `SameSite=strict` session cookie is
  sufficient protection on its own across all 44 state-changing endpoints
  (every one requires that cookie; no `CORSMiddleware` exists; the agent
  WebSocket endpoint uses a bearer token, not a cookie). Added
  `OriginCheckMiddleware` anyway as defense-in-depth: for every
  `POST`/`PUT`/`PATCH`/`DELETE`, `Origin` (or `Referer`) must match the
  request's own `Host` header when either is present, closing the residual
  gap of a browser bug or non-standard client not honoring `SameSite`. No
  frontend changes needed for this — `vite.config.ts`'s dev proxy was
  updated to strip these headers on proxied requests so local dev (which
  legitimately runs frontend and backend on different ports) isn't
  affected.
- CI now blocks on dependency and container vulnerabilities: `pip-audit`
  (backend), `npm audit --audit-level=high` (frontend), and `govulncheck`
  (the Go agent) run in their respective CI jobs; a new `docker-image` job
  builds the published `Dockerfile` and scans it with Trivy
  (HIGH/CRITICAL, fixable only), and generates a CycloneDX SBOM uploaded as
  a build artifact on every run. `.github/dependabot.yml` opens weekly
  update PRs against `dev` for all three ecosystems (pip, npm, gomod).
  Fixed one pre-existing high-severity transitive vulnerability (`nanoid`,
  via vite→postcss) as part of turning this on.
- Security response headers (`Content-Security-Policy`, `X-Frame-Options`,
  `X-Content-Type-Options`, `Referrer-Policy`) are now sent on every
  response, API and frontend alike. CSP allows only same-origin scripts and
  styles (`'unsafe-inline'` for `style-src` only, to accommodate CodeMirror
  6's runtime-injected editor styles); `frame-ancestors 'none'` backs up
  `X-Frame-Options: DENY` for clickjacking protection.
- Login now locks out after repeated failed attempts against one username
  (`login_max_attempts`, default 5) for a configurable duration
  (`login_lockout_seconds`, default 300s), returning `429` with a
  `Retry-After` header — previously nothing throttled `/auth/login` beyond
  argon2id's own hashing cost.
- SSH connector now persists known host keys across connections
  (`ssh_known_hosts_path`) instead of trusting whatever key each fresh
  connection presents with no memory of previous ones — a connection to a
  host whose key changed since it was last seen now fails loudly instead
  of silently succeeding, closing a man-in-the-middle window on every SSH
  source.
- Credential encryption now derives its key via PBKDF2-HMAC-SHA256 (600k
  iterations) with a persisted per-install salt, replacing a single
  unsalted SHA-256 round that had no work factor at all. The app also now
  refuses to start if `CREDENTIAL_ENCRYPTION_KEY` is still the shipped
  `"changeme"` default, rather than silently encrypting every stored
  credential under a key derivable from this project's own public source.
  **Breaking for existing deployments**: previously-encrypted credentials
  (source SSH/SMB/WinRM secrets, SSO client secrets) can't be decrypted
  under the new derivation — reconfigure them after upgrading.
- Fixed an SSRF vulnerability (#49) in `Alert.webhook_url`: any authenticated
  user — including a freshly auto-provisioned no-access account — could point
  a webhook at an internal address (loopback, link-local/cloud-metadata,
  RFC1918 private ranges) and use `POST /alerts/{id}/test` as a reachability
  oracle against the server's own network. `webhook_url` is now resolved and
  checked against disallowed address ranges both when an alert is created or
  updated and again immediately before the webhook is actually sent, closing
  the DNS-rebinding gap between those two points in time.

### Added
- Full audit log viewer: a new "Audit log" page under Settings, gated by a
  dedicated `view_audit_log` global capability (never implied by any other
  capability — a role needs it explicitly, same as everything else on this
  page being a global, not customer/folder/source-scoped, concern). Lists
  every `AuditLog` entry (login, and every source/rule/role/user/customer/
  folder/SSO/system-settings/severity-pattern change) paginated newest-first,
  filterable by target type and action (both built from the distinct values
  actually in the table right now via `GET /audit/filters`, so a new action
  namespace shows up automatically instead of the list going stale) and by
  date range. `GET /audit` also accepts `user_id` and a trailing `.*` on any
  `action` for a prefix match (e.g. `source.*`), for API consumers, even
  though the shipped UI only exposes exact-action and target-type controls.
  A new `audit_retention_days` system setting (default 365, `0` = keep
  forever), admin-configurable from Settings → System, drives a daily
  `app/audit_purge.py` sweep that permanently deletes anything older —
  resolving this roadmap's long-open "keep audit logs forever, or expire
  after N months?" question. The page itself has its own deployment-wide
  on/off toggle (`audit_view_enabled`, reusing the same `SystemSetting`
  mechanism as the Search toggle), separate from who can see it.
- A Playwright end-to-end test suite (`frontend/e2e/`, `npm run test:e2e`)
  covering login (success, failure, and the logged-out redirect); the
  built-in system log source's viewer and its non-editable sources-list
  row; the sessions page; source create/edit/delete against all three
  remote protocols (SSH, SMB, WinRM) including both rule-editor modes
  (row-based and raw-paste) and last-match-wins, browsing and opening a
  real file over each; roles (global capabilities, grant add/remove,
  duplicate) and users (create, reset password, role change,
  deactivate/reactivate); SSO provider settings, a test-connection
  failure, and group→role mappings; the deployment-wide search/alerts
  toggle and monitoring token generation; global severity-indicator
  patterns; and full-text search (indexing a real source and deep-linking
  a hit into the viewer) plus alerts (create, test webhook, toggle,
  delete). SSH and SMB run against real local test servers
  (`backend/scripts/setup_e2e_test_servers.sh`, plain OS packages, not
  Docker); WinRM is the one protocol mocked at the connector's session
  seam (`backend/app/testing/fake_winrm.py`), since a real target needs an
  actual Windows host. Runs against an isolated backend
  (`backend/scripts/run_e2e_server.sh`) seeded with a deterministic
  super-admin account (`backend/app/seed_e2e_admin.py`), on its own port
  and its own throwaway SQLite DB so it never touches a developer's real
  dev environment. Wired into CI as a new `e2e` job. See ROADMAP.md's
  "Frontend E2E testing (Playwright)" section for the design notes and
  what's deliberately still out of scope.
- SSO: IdP group-claim-to-role auto-mapping. Configure a "group claim" name
  on the OIDC provider and an ordered list of group → role mappings
  (evaluated last-match-wins, same rule as source Rules); a user's role is
  resolved from their IdP groups and applied on every SSO login, not just
  first provisioning, so it stays in sync as group membership changes.
  Applies immediately on first sign-in too — a new user whose groups match
  a mapping is provisioned straight into that role instead of always
  landing on the no-access default. Note: this means a role changed
  directly in PerchTail can be overwritten on that user's next login if
  their IdP groups still match a configured mapping — remove the mapping
  (or the user from that group) to stop the sync.
- Alerting: a new "Alerts" page for saving a search query and getting a
  webhook notification when new matching content shows up in a source's
  index. Rides entirely on the existing full-text search index/indexer —
  evaluated right after every indexing sweep, only re-checking files whose
  index entry has changed since the alert's last check, and only ever
  matching newly-appeared line content (never a filename match, which
  can't meaningfully be "new"). RBAC is re-checked on every evaluation, not
  just at creation, so losing access to a source silently stops that
  alert's scope without needing to edit or delete it. A per-alert **Test**
  button sends a synthetic webhook so the receiver can be verified without
  waiting for real content. Alerts are owned by the creating user (simple
  list/create/enable/delete, not part of the customer/folder/source grant
  tree), and the nav entry is gated by the same deployment-wide toggle as
  Search, since there's nothing for an alert to watch with it off.
- Detailed health endpoint for external monitoring: `GET /monitoring/health`,
  gated by its own deployment-wide bearer token (generated from Settings →
  System, hashed at rest, shown once) rather than user-session auth — a
  monitoring system like Zabbix or Prometheus can't do an interactive login.
  Reports overall status (ok/degraded/error), DB reachability + latency,
  scratch usage, source counts by protocol, agent connected-vs-configured
  counts, last search-indexing sweep time + overdue flag, and APScheduler job
  health, alongside app version and uptime. Stays separate from the existing
  fast, unauthenticated `GET /healthz` liveness check. See
  `docs/monitoring.md` for the response shape and a Zabbix HTTP-agent-item +
  JSONPath integration guide.
- Search now matches file paths and source names/hosts, not just line
  content. A query matching a file's path (but none of its lines) surfaces
  that file as a "filename match" hit; a query matching a source's name or
  host surfaces the source itself in a new "Sources matching" section,
  distinct from content hits — always current since it's a live filter over
  RBAC-visible source metadata, not part of the lagging background index.
  `search_index_fts`'s `file_path` column is now indexed (previously
  storage-only); existing deployments self-migrate their FTS schema and
  re-index on the next sweep, handled automatically at startup.
- Viewer: a "Find All" results panel (`lib/find-in-document.ts`,
  `FindInDocumentPanel.svelte`) alongside the existing Ctrl+F inline
  search — lists every match in the currently open file (line number +
  highlighted snippet), click a result to jump straight to it. Supports
  match-case and regex modes, and caps at 5000 results (reporting
  "truncated") as a safety valve for pathologically match-heavy queries
  rather than a design goal. Pure client-side scan of the tab's own
  content, no new dependency.
- Phase 2 (push-agent) backend infrastructure: a new `agent` protocol for
  sources that can't be reached inbound over SSH/SMB/WinRM. The agent
  (a future Go binary — see ROADMAP.md) dials out and holds a persistent
  WebSocket open at `/agent/connect`, authenticated with a bearer
  enrollment token (`POST /sources/{id}/agent-token`, hashed at rest,
  returned in plaintext once); the backend then relays live `list`/`fetch`
  commands down that connection on demand, exactly as it would call any
  other connector directly — nothing is proactively synced or mirrored, so
  the always-fresh, nothing-persisted rule holds for agent-mode sources too.
  `AgentRegistry` (`app/agent_registry.py`) tracks live connections
  in-memory and bridges FastAPI's synchronous connector calls to the async
  WebSocket via `asyncio.run_coroutine_threadsafe`. `SourcePublic` now
  reports `agent_connected`/`agent_last_seen_at` for agent-protocol sources.
- Phase 2 (push-agent): `agent/`, a standalone Go binary for sources that
  can't be reached inbound over SSH/SMB/WinRM. It dials out to the server
  and holds a persistent WebSocket connection open (`gorilla/websocket`),
  authenticated with the bearer enrollment token issued by the backend;
  the server then relays live `list`/`fetch` commands down that connection
  on demand — nothing is proactively synced. Cross-compiles for
  linux/amd64, linux/arm64, and windows/amd64; a dedicated CI job builds,
  vets, tests, and cross-compiles it on every push.
- Phase 2 (push-agent) frontend: `SourceEditor.svelte` gains an `agent`
  protocol option — no host/credential fields, instead a one-time
  enrollment-token generator ("Generate token" / "Regenerate token", with
  `has_agent_token` newly reported by `SourcePublic`) for pasting into the
  agent's `PERCHTAIL_AGENT_TOKEN` config. `Sources.svelte` shows agent
  sources' live connection state (connected / never connected / last seen)
  instead of the manual reachability check the other protocols use.
- Phase 3: full-text search. A new opt-in per-source flag
  (`Source.search_indexing_enabled`, off by default) enables a background
  indexer (`app/search_index.py`, an APScheduler sweep) that walks a
  source's rule-visible files and stores short per-line snippets in a
  SQLite FTS5 index — a genuinely separate, lagging, approximate structure,
  not a reuse of the ephemeral viewing scratch space or a departure from
  the always-fresh live-browsing model. `GET /search?q=...` searches across
  every source the current user can view, highlighting matches (safely
  HTML-escaped before its `<mark>` tags are added — log content is
  untrusted, arbitrary text). Frontend: a new Search page with a
  query box and a ranked, clickable results list; clicking a result opens
  the file in the Viewer and jumps straight to the matched line
  (`CodeMirrorPane` gained a `scrollToLine` method for this). The source
  editor gains an "Include in full-text search" toggle.
- Source editor: the Customer and Folder pickers gain an inline "+ Create
  new…" option, so a new customer or folder no longer needs a detour
  through an admin page that doesn't exist yet — pick it, type a name, and
  it's created (`POST /customers` / `POST /folders`) and selected in
  place. The Folder picker shows the existing nested tree indented by
  depth, and a new folder defaults its parent to whatever folder was
  already selected, so nesting one inside another is a single click.
  Folders remain purely organizational — name + optional parent, never a
  host or protocol — the backend already supported unlimited nesting via
  `Folder.parent_folder_id`; this was a frontend-only gap.
- Connections home redesign (Guacamole-inspired dashboard layout, see
  ROADMAP.md). The Viewer's landing page (`#/viewer` with no source
  selected) is now a two-column layout — recent connections on the left,
  all connections on the right — replacing the previous flat list.
  "Recent" is powered by new `source.open` `AuditLog` events (logged on
  the first browse into a source, not every sub-directory expand) read
  back via `GET /sources/recent`, RBAC-rechecked at read time so a
  revoked grant can't leak a source through history. `GET /sources/{id}/download`
  now also logs `file.download` — CLAUDE.md's own stated audit minimum
  that had never actually been wired up. "All connections" gains a search
  box matching folder, customer, or host, case-insensitive
  (`lib/connection-filter.ts`, not the source's own name); `SourcePublic`
  now reports `customer_name`/`folder_name` so cards can show this without
  a separate lookup.
- System settings: a new `SystemSetting` key-value table backs
  deployment-wide feature toggles, admin-configurable from a new
  "System" tab under Settings (`GET`/`PATCH /system-settings`, gated by a
  new `manage_system_settings` global capability). Ships with one toggle,
  "Search" — disabling it hides the Search nav entry for every user and
  redirects away from the `/search` route itself, not just the link. Built
  generically enough that the planned audit-log viewer's own toggle
  (ROADMAP.md) can reuse the same mechanism once that page exists.
- Viewer: admin-configurable severity indicators, both globally and
  per-source, replacing the old hardcoded `[error]`/`[warn]`-token and
  bare-word regexes. A new `SeverityPattern` model (level, pattern,
  glob/regex + `re:` prefix convention same as `Rule`, enabled,
  highlight-line, include-in-navigation, nullable `source_id`) backs
  `GET`/`POST`/`PATCH`/`DELETE /severity-patterns` (global set, gated by
  `manage_system_settings`) and the same under
  `/sources/{id}/severity-patterns` (per-source override, gated by
  `manage_rules`), plus `GET /sources/{id}/severity-patterns/effective`
  the Viewer reads — a source's own patterns win outright when it has any,
  else the global default set, same "most specific wins" shape as grant
  resolution. Seeded with sensible defaults on first startup (error/
  fatal/critical/panic/exception/traceback, warn, info, debug). New
  "Settings → Severity Indicators" page for the global set and a matching
  section on the source editor for per-source overrides, both row-based.
  The Viewer gained "‹ problem" / "problem ›" toolbar buttons that step
  through lines matching a navigation-eligible pattern (configurable per
  pattern, not a fixed severity floor), wrapping at either end. Matching
  logic lives client-side (`lib/severity-highlighting.ts`, pure and
  unit-tested); `codemirror-theme.ts` now builds its highlighting
  `ViewPlugin`s from whatever pattern set is effective instead of a fixed
  regex.
- Viewer: a set of small, per-tab toolbar toggles/actions, all display-only
  (never write anything back to the file or source) — line-wrap, "show all
  characters" (reveals whitespace as `·`/`→` glyphs and flags CRLF line
  endings, detected from the raw fetched content since CodeMirror's own
  line-separator matching consumes the `\r` before it can be inspected),
  go-to-line (Ctrl/Cmd+G, same page-level interception as the existing
  Ctrl/Cmd+F), copy-selected-lines-with-line-numbers, a manual reload
  button (re-fetches the open tab's content in place and releases the
  previous scratch reference), and bookmarks (pure client-side per-tab
  state, next/previous navigation reusing the same wrap-around stepper
  severity "next/previous problem" navigation already used — extracted to
  `lib/line-cycle.ts` so both share one implementation).
- Viewer: per-file-type syntax highlighting. Language is picked from the
  open file's own extension (`lib/file-language.ts`) — `.json`, `.xml`/
  `.html`/`.htm`/`.svg`, and `.js`/`.mjs`/`.cjs`/`.jsx`/`.ts`/`.tsx` now
  render with real syntax coloring via `@codemirror/lang-json`/`lang-xml`/
  `lang-javascript`, previously installed but unused dependencies.
  Anything unrecognized (most log files) is unaffected.

### Changed
- Frontend: Sources, Roles, Users, and SSO settings are now consolidated
  under a single "Settings" top-nav entry (previously four separate top-nav
  links), with a shared sub-nav across all four pages. Routes gained a
  `/settings` prefix (e.g. `/settings/sources`, `/settings/roles/:id`); a
  bare `/settings` redirects to `/settings/sources`. No capability gating
  changed — Sources stays visible to any authenticated user, Roles/Users/SSO
  stay gated behind their existing `manage_roles`/`manage_users`/`manage_sso`
  checks, same as before.
- README: status section now reflects Phase 2 (push-agent) and Phase 3
  (full-text search) as complete, not just Phase 1/1b; the three
  `docs/images/` screenshots (Sources, Viewer, Role editor) are
  regenerated against the new Settings nav; the Quick start walkthrough's
  "Sources → New source" now reads "Settings → Sources → + Add source".
- README: status section was stale in the other direction — it still
  listed alerting and IdP group-claim-to-role auto-mapping as not built,
  when both shipped earlier in this same Unreleased section. Corrected,
  and added a line on the pre-1.0 security-hardening work that's actually
  done (login lockout, security headers, CI vulnerability scanning,
  credential-key rotation, the Sessions page) versus what's still open,
  and a mention of the new Playwright e2e suite alongside the existing
  `pytest`/`vitest` coverage.

### Fixed
- Viewer: Ctrl/Cmd+F opened the browser's own find bar instead of
  CodeMirror's in-file search. Root cause was twofold — clicking into the
  read-only pane never actually moved DOM focus there (it isn't
  `contentEditable`, so a plain click doesn't focus it, and CodeMirror's
  `basicSetup` only wires the search *keymap*, not the `search()`
  extension the panel needs), so the keystroke had nothing to catch it
  and fell through to the browser. Fixed by intercepting Ctrl/Cmd+F at
  the window level in `Viewer.svelte` while a tab is open and opening
  the panel directly via `openSearchPanel`, regardless of focus state.
- `vite.config.ts`'s dev-proxy prefix list was missing `/sso`, so the SSO
  settings page silently failed to load under `npm run dev` (production is
  unaffected — the API and built SPA are served from the same FastAPI
  process there, with no path-based proxy split). Found while verifying the
  Settings reorganization in a real browser.
- SMB connector, two compounding bugs, both only reachable with a real
  `smbclient`/`smbprotocol` connection (found via the new Playwright e2e
  suite's real SMB server, not the mocked unit tests): `register_session`
  was left at `smbclient`'s default `auth_protocol` (`"negotiate"`, which
  tries Kerberos before falling back to NTLM) — `credential_ref` only ever
  decrypts to a bare username/password, with no realm/domain/KDC field
  anywhere in the `Source` model, so a client with no Kerberos
  configuration at all could fail the entire SPNEGO negotiation outright
  (`pyspnego.exceptions.BadMechanismError: Unable to negotiate common
  mechanism`) instead of ever reaching the NTLM this app actually
  authenticates with; and, once that was fixed, a source on a non-default
  SMB port still failed every `scandir`/`open_file` call — `smbclient`
  resolves its *own* session for those via `get_smb_tree(path, port=445,
  ...)`, defaulting to 445 independently of whatever port
  `register_session` was called with, so it silently attempted an
  unauthenticated connection on the wrong port instead of reusing the
  already-authenticated session. Both `auth_protocol="ntlm"` and the
  actual configured port are now passed to every `smbclient` call, not
  just the initial `register_session`; and a third, same-shape bug behind
  those two — `list_directory`'s per-entry `info.stat()` call (to read a
  file's size) triggers its own fresh `smbclient` connection lookup that
  forwards none of `_connect_kwargs`' `port`/`auth_protocol`, so it lands
  on a different, uncredentialed session and fails the same SPNEGO
  negotiation all over again. Fixed by reading the size straight off
  `info.smb_info.end_of_file` — data `scandir()` already returned in the
  original listing — instead of making a second network call at all.

## [0.1.1] - 2026-07-30

Phase 1b (SSO) complete: OIDC single sign-on alongside local auth, with
auto-provisioned no-access accounts for first-time SSO logins and an SSO
settings admin page.

### Added
- Phase 1b (SSO): `OIDCProvider` (`app/auth/providers/oidc.py`) — the second
  `AuthProvider` alongside `LocalPasswordProvider`, covering the OIDC
  authorization-code flow (discovery, code exchange, ID-token signature
  verification against the IdP's live JWKS, and `iss`/`aud`/`exp`/`nonce`
  claim checks). Admin CRUD for `SSOProviderConfig` at `/sso/...` (gated by
  `manage_sso`), with a test-connection action mirroring sources' `/check`
  endpoint; `GET /auth/sso/status`, `GET /auth/sso/login`, and
  `GET /auth/sso/callback` drive the actual sign-in flow, using a
  Fernet-encrypted, self-contained `state` param rather than a server-side
  state table. A first-time SSO sign-in auto-provisions a user with a new
  builtin "No Access" role (`app.bootstrap.seed_no_access_role`) — an admin
  assigns the real role afterward, per CLAUDE.md. Local login is unaffected
  and keeps working alongside an enabled provider. Frontend: an SSO
  settings admin page (gated by `manage_sso`, nav-linked) to configure the
  provider and test its connection, and a "Sign in with {name}" button on
  the Login page when one is enabled.
- More unit tests: backend `app/audit.py` (`record_audit_event`'s field
  defaults and its documented "doesn't commit" contract) and
  `app/timeutils.py` (`utcnow`'s naive-datetime guarantee) were the only two
  app modules without a dedicated test file — both now have one. Frontend:
  extracted `memberPath` (the archive-member-path slicing logic that had been
  duplicated inline in `FolderTree.svelte` twice, plus once more inside
  `download-href.ts`) into `lib/tab-key.ts`, with tests, so the three call
  sites can't silently drift apart.
- README: real screenshots (Sources, Viewer, Role editor) under `docs/images/`,
  a short table of contents, and a fixed `git clone` command (was still the
  `<your-org>` placeholder). CONTRIBUTING.md gets the same clone-URL fix plus
  frontend dev setup (`npm install`/`npm run dev`/`npm run check`/`npm test`),
  which had been missing entirely even though the frontend has been a core
  part of the stack since M7.

### Fixed
- `backend/requirements.txt` was missing `httpx`, which
  `app/auth/providers/oidc.py` imports unconditionally at module level
  (via `app/api/auth.py` → `app/main.py`) regardless of whether SSO is
  configured. It was only listed in `requirements-dev.txt`, so tests never
  caught it — a container built from `requirements.txt` alone (the
  production Docker image) crashed on every boot with
  `ModuleNotFoundError: No module named 'httpx'` right after migrations
  ran. Moved `httpx` into `requirements.txt` as a real runtime dependency.
- `RoleEditor.svelte`'s bare `form { flex-direction: column }` rule (meant for
  the role-details form) also matched the page's second `<form>` — the
  "add grant" row — flipping its flex main axis from row to column. Combined
  with `.add-grant select { flex: 0 0 160px }` (a width in the intended row
  layout), the flipped axis turned that into a *height*, rendering both scope
  dropdowns as ~160px-tall boxes instead of a normal single-line row. Found
  while capturing README screenshots. Scoped the rule to `form.card` (the
  role-details form's actual class) so it can't leak onto other forms in the
  same component.

## [0.1.0] - 2026-07-29

Phase 1 (MVP) complete: agentless live browsing over SSH/SFTP, SMB, and WinRM;
ephemeral fetch (nothing mirrored); rule-scoped RBAC (customers, folders,
roles, grants, local auth, audit log); the built-in log viewer; and the full
admin/viewer UI (Vite + Svelte), packaged as a single Docker image.

### Added
- Project scaffold (M0): FastAPI app under `backend/`, `structlog` JSON logging
  with request-id middleware and gzip-rotated daily log files, SQLite +
  SQLModel wired up with Alembic migrations, `ruff`/`black`/`pytest` tooling,
  CI workflow, and a `docker-compose`/`Dockerfile` skeleton.
- Data model (M1): `Customer`, `Source`, `Rule`, and the auth models `Role`,
  `RoleGrant`, `User`, `SSOProviderConfig`, `AuditLog`, plus an
  `app/crypto.py` helper (Fernet, keyed by `CREDENTIAL_ENCRYPTION_KEY`) so
  `Source.credential_ref` and `SSOProviderConfig.config` have a real path to
  encryption-at-rest from day one.
- RBAC & local auth (M2): grant-resolution logic and a FastAPI permission
  dependency (`auth/rbac.py`), `LocalPasswordProvider` (argon2id, forced
  password change on admin-created accounts), and `AuditLog` writes wired
  into login and role/grant creation, each paired with a structured INFO log
  line.
- `api/auth.py`: login/logout/me/change-password endpoints backed by
  server-side sessions (hashed opaque token in an httpOnly, SameSite=strict
  cookie, stored in a new `AuthSession` table) rather than a stateless JWT,
  so revoking access is immediate.
- Rule engine (M3): `rules.py` implements CLAUDE.md's matching semantics —
  glob (default, path-separator-aware `**`/`*`/`?`, not stdlib fnmatch) or
  regex (`re:` prefix) patterns, last-match-wins evaluated by rule order,
  and zero rules matching nothing.
- SSH/SFTP connector and ephemeral scratch (M4): `collectors/ssh.py`
  (paramiko-based live listing and fetch-on-open), `scratch.py`
  (refcounted purge, idle-sweep backstop, size-guard eviction, both wired
  as APScheduler jobs), `archives.py` (`.zip`/`.tar.gz` as virtual folders,
  transparent `.gz` decompression), and `api/archive.py`
  (browse/open/close/download, gated by the M2 permission dependency and
  independently by the rule engine on every specific path).
- Remaining connectors (M5): `collectors/smb.py` (`smbprotocol`),
  `collectors/winrm.py` (`pywinrm`, base64-through-PowerShell for file
  content since WinRM has no native bulk transfer), and `collectors/
  local.py` (reads directly off disk). `api/archive.py` now skips the
  scratch store entirely for local, plain files — served straight from
  their real path — while still using it for a local `.gz` or archive
  member, since decompressing/extracting produces new derived bytes that
  have to live somewhere.
- Nested folders for source organization (M4.5): a `Folder` model
  (self-referential, nested under a `Customer`) and `RoleGrant.scope_type`
  gains `folder`, generalizing the existing source-beats-customer grant
  resolution to N levels — source → folder chain (nearest first) →
  customer, most specific wins.
- Built-in log viewer (M6): `app/bootstrap.py` seeds a system source on
  first startup pointed at `LOG_DIR`, so the app's own rotated logs are
  browsable through the exact same live-browsing path as customer sources
  — gated purely by `is_super_admin` (already enforced since M2).
- Admin CRUD API (M7, part 1): `api/customers.py`, `api/folders.py`,
  `api/sources.py`, `api/rules.py`, `api/roles.py`, `api/users.py` — the
  full backend surface the admin/viewer UI needs. Sources gain a
  `POST /sources/{id}/check` on-demand connection check, replacing the
  pre-pivot "run-now"/"run history" wording in CLAUDE.md's original Web UI
  section (there's no `Run` model and nothing runs on a schedule anymore —
  see ROADMAP.md's M7 notes). Rules gain a gitignore-style raw-text
  paste-mode endpoint (`PUT .../rules/raw`, leading `!` negates a line) in
  addition to the row-based CRUD + reorder. Roles gain a duplicate-role
  action (clones grants too) and grant CRUD, with a privilege-escalation
  guard: only an existing super-admin can create or edit a super-admin
  role. Users gain admin-driven create/update/reset-password/deactivate
  (deactivate is a soft delete, per CLAUDE.md's Security notes). Customer/
  folder management is treated as an admin surface gated by the
  `create_source` global capability rather than per-scope grants, per
  CLAUDE.md's "its own small admin surface" wording. `api/archive.py` also
  gains `GET .../download-zip` (zips an entire folder, fetching each
  contained file fresh) so the Viewer can offer "download a zipped folder,"
  not just a single file. `auth/me` (and login/change-password) now also
  returns `is_super_admin`/`global_capabilities` so the frontend can gate
  nav without a second round-trip.
- Admin & viewer UI (M7, part 2): a Vite + Svelte + TypeScript SPA under
  `frontend/` — the frontend stack CLAUDE.md never named (see ROADMAP.md's
  M7 notes for why Svelte). Login/change-password, a sources list with
  inline connection-check and a create/edit form, a rule editor (row-based
  + gitignore-style raw-text paste mode, toggled per source), a lazy-loaded
  folder tree (children fetched only on expand, one level of `.zip`/
  `.tar.gz` expansion) feeding a CodeMirror 6 tabbed viewer pane (in-file
  search via CodeMirror's built-in search panel, single-file and
  zipped-folder download), and Roles/Users admin pages (grant CRUD,
  duplicate-role, reset-password, deactivate). CI gains a `frontend` job
  (`svelte-check` + `vite build`) alongside the existing backend job.
- Phase 1 exit (M8): multi-stage `Dockerfile` (Node stage builds the SPA,
  copied into the Python runtime image), `docker-entrypoint.sh` (runs
  `alembic upgrade head` before `uvicorn`), and `app.main` mounts the built
  frontend as static files at `/` — works unmodified with the frontend's
  hash-based routing, since the browser only ever requests `/`.
  `app.bootstrap.seed_initial_super_admin` creates a break-glass super-admin
  with a randomly generated, once-logged password on first startup (a fresh
  deployment previously had no way to create its first user at all).
  README's Quick start is now real, tested steps rather than a placeholder.
- Visual redesign and logo: a dark theme (CSS custom properties in
  `app.css` — background/border/text/accent tokens, per-protocol and
  per-status badge colors) applied across every page, plus a fish/perch mark
  (`frontend/public/favicon.svg`) per CLAUDE.md's mascot note — used as the
  favicon, the nav-bar brand, and the README header. The full brand asset
  pack (icon at multiple raster sizes, mark-only light/dark variants, and an
  icon+wordmark lockup) lives under `frontend/public/brand/` for later
  branding work. The Viewer's CodeMirror pane gains a matching dark theme
  and lightweight log-level highlighting (`[info]`/`[warn]`/`[error]` tokens
  colored, error lines tinted) so a log reads the same way `grep -i error`
  would highlight it.
- Frontend unit testing (`vitest`, added alongside this redesign): pulled
  the non-CSS logic that redesign touched or introduced out into small,
  pure, testable modules — `lib/codemirror-theme.ts`'s log-level token/line
  detection, `lib/tab-key.ts` (the tab/tree-highlight key format shared
  between Viewer and FolderTree, previously duplicated inline in both),
  `lib/download-href.ts` (FolderTree's per-entry download-URL branching),
  and `lib/rule-format.ts` (RuleEditor's raw-text line rendering/parsing) —
  plus tests for the existing `lib/api.ts` request wrapper, `lib/auth.ts`'s
  capability check and login/logout/session-refresh flows, and `lib/hash.ts`'s
  hashchange-driven route store. CI's `frontend` job runs `npm test` before
  the build step.

### Fixed
- Archive member listing/opening didn't check the rule chain against the
  specific member requested — browsing a `.zip`/`.tar.gz` listed every
  member unconditionally, and `/open`/`/download` didn't re-check a
  requested `member` against the rules at all, so a rule scoped to show
  only the archive itself leaked its full contents. Both paths now check
  `is_visible` on the member's combined virtual path.
- `is_safe_relative_path` only split on `/`, so a backslash (`..\\`) path
  traversal segment passed straight through — a real, exploitable gap for
  SMB/WinRM sources specifically, since those connectors join a relative
  path onto a Windows `base_path` with backslashes. Now splits on both
  `/` and `\\`, and rejects a bare `:` to rule out Windows drive-letter
  absolute paths.

See [ROADMAP.md](ROADMAP.md) for what's next (Phase 1b: SSO).
