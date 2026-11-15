# User guide

This guide is for everyone who uses PerchTail to browse, read and download log files: it covers signing in, the explorer, the viewer, Tail, downloads, archives, searching for files and My profile.

Related guides:

- [Administrator guide](admin-guide.md): sources, rules, roles, users, alerts and the audit log.
- [Configuration](configuration.md), [Upgrading](upgrade.md), [Push agent as a service](agent-service.md) and [Troubleshooting](troubleshooting.md), for whoever runs PerchTail.

What you can see and do depends on your role. If a source, a button or a page described here is missing, ask an administrator.

## Contents

- [Signing in](#signing-in)
- [The explorer](#the-explorer)
- [Searching for sources and files](#searching-for-sources-and-files)
- [Tabs](#tabs)
- [The viewer](#the-viewer)
- [Tail](#tail)
- [Downloads](#downloads)
- [Archives](#archives)
- [My profile](#my-profile)

## Signing in

**With a PerchTail account:** enter your **Username** and **Password** and click **Sign in**.

- If your account has two-factor authentication, PerchTail then asks for an **Authentication code**: the 6-digit code from your authenticator app, or one of your backup codes. Click **Verify**, or **Back** to start again.
- If an administrator created your account or reset your password, or your password expired, PerchTail asks you to **Set a new password** before anything else. Enter the temporary or current password, then the new one. The rules it must follow are shown under the field.
- After 5 failed attempts in a row, sign-in for that username is refused for 5 minutes (the defaults; your administrator may have changed them).

**With single sign-on:** click **Sign in with** followed by your organisation's provider. Your password and any second factor are handled by the provider, not by PerchTail. The first time you sign in, PerchTail creates your account. It may see nothing until an administrator gives it a role.

A session lasts 12 hours from sign-in by default, then you sign in again. To sign out, open the menu with your name at the top right and click **Log out**. Signing out closes your open tabs.

## The explorer

**Viewer** in the top bar opens the explorer on the left and the viewer on the right.

The explorer lists the sources you can view. Two views may be available, **List** and **Tree**:

- **List** shows every source in one list.
- **Tree** groups sources by customer and folder. Sources without a customer appear under **No customer**.

Your choice is kept on the server and follows you to other browsers. If your administrator has turned the switch off, everyone sees the same view and the buttons are hidden.

Click a source to expand it, then click folders to expand them and files to open them. Each file shows its size; hover over it for the exact size and when it was last modified. Folders always come first. The order of the files is set by your administrator for everyone.

Only files the source's rules allow are listed. A folder appears only if some file below it is visible. "Nothing visible here — check the source's rules." means the rules show nothing in that place.

A folder or archive lists at most a set number of entries (5000 by default). When there are more, the explorer says "Showing the first N entries; narrow with the search."

### Status

A coloured dot before each source shows whether it answers: reachable, failing, an agent that isn't connected, or not checked yet. Hover over the dot for details. **✓?** next to a source checks the connection now.

### Refresh

The folders you have open are listed again on their own every 30 seconds by default, so files that rotate in and out show up. Only the list of files is refreshed, never their content, and nothing is refreshed while the browser tab is hidden. New files are marked. **⟳** next to an expanded source or folder lists it again at once.

### Recent sources

When no file is open, the viewer shows **Recent**: up to 8 sources you opened most recently. Click one to go to it in the explorer.

## Searching for sources and files

The box at the top of the explorer, **Search sources and files**, finds both:

- **Sources** whose name, host, customer or folder matches. Click one to show it in the tree.
- **Files** in every source you can view, from the file-name index. Each result shows the file's name, size, source and folder. Click a file to open it, and **Show more files** to load the next results.

While there is text in the box, the results replace the tree. Clear the box to get the tree back.

What you type decides how it matches, always ignoring letter case:

| You type | It matches |
|---|---|
| text, such as `error` | file names that contain it |
| a glob with `*` or `?`, such as `app-*.log` | whole file names; `*` stays within one folder, `**` crosses folders |
| a regular expression between slashes, such as `/app\.log\.\d/` | file names in which the expression is found |
| any of these with a `/`, such as `iis/*.log` | the file's path in the source instead of its name |

The file-name index is refreshed in the background, so results can be a few minutes old. The **Files** heading says when the files were last listed. A file a rule now hides never shows, even if it was listed before.

If the index is turned off, or some sources are left out of it, the results say so. Click **Select** to search those sources live instead.

### Searching sources live

**Filter sources**, next to **List** and **Tree**, adds checkboxes to the explorer. Tick sources, folders inside them, or customers and folders of sources, then type in the search box: PerchTail lists every folder below what you ticked, live, and shows the files whose names match. This works for sources left out of the index too.

- **Add or change** goes back to the checkboxes, and **Show the files** back to the results.
- **Stop** ends one search, and **Stop all** every search.
- **Clear all** removes every ticked item.

A live search lists folders through the source each time, so it is slower than the index on large sources.

## Tabs

Each file opens in its own tab. Tabs from several sources can be open side by side; the tab then shows the source's name before the file name, and a coloured dot tells the sources apart. Hover over a tab for its source and path.

- Click **×** on a tab to close it.
- At most 30 tabs can be open. Opening a 31st shows "30 files are open, the most the viewer keeps at once. Close a tab to open another."
- Tabs stay open while you move around PerchTail. Reloading the page or signing out closes them.
- Tabs of a source that is disabled, deleted or no longer granted to you close on their own.

## The viewer

The viewer is a read-only editor: nothing you do in it changes the file on the source. Each open is a fresh read from the source.

### Opening files

Click a file in the explorer or in search results. A file over 200 MB opens only after a warning, which offers:

- **Tail the end**: loads the last 1 MiB and starts Tail. The toolbar shows **End of the file only**. Not offered for `.gz` files or files inside archives.
- **Download**, if your role can download.
- **Open anyway**: loads the whole file into the browser tab, which can be slow.

A `.gz` file opens decompressed. A `.zip`, `.tar.gz` or `.tgz` file opens as a folder, see [Archives](#archives).

When a file can't be opened, the viewer says why, for example that it no longer exists on the source, that it is hidden by the source's rules, or that it is over the server's size limit. **Try again** appears when retrying may help, for example when PerchTail is busy.

### Toolbar

| Control | What it does |
|---|---|
| **Find** (Ctrl/Cmd+F) | Opens the find bar, see below. |
| **Go to line** (Ctrl/Cmd+G) | Jumps to a line number. |
| **Compare** | Then pick a second file, in the explorer or another tab, from any source: the two are shown side by side with their differences. **✕ Exit compare** ends it. |
| **Wrap** | Wraps long lines. |
| **Show chars** | Shows spaces, tabs and other invisible characters. |
| **Beautify** and **Minify** | For JSON and XML files: reformat for reading, or collapse to one line. Display only. |
| **Highlight** | Colours every occurrence of a text, or of a regular expression written as `re:…`, ignoring case. Add several, each in its own colour; **×** removes one and **Clear highlights** removes all. |
| **Tail** | Shows new lines as they're written, see [Tail](#tail). |
| Copy lines | Copies the selected lines with their line numbers. |
| Bookmark | Toggles a bookmark on the current line; the arrows next to it jump between bookmarks. |
| Previous and next problem | Jump between lines the severity indicators mark. |
| Reload | Reads the file again from the source. |
| Download | Downloads the file, if your role can download. |

Wrap, Show chars, highlights and bookmarks belong to the tab and are lost when it closes.

### Severity highlighting

Words that mark a severity (error, warning, info, debug) are coloured by level, and some patterns tint the whole line. Which words count is set by your administrator, for all sources or for one source. **Previous problem** and **Next problem** in the toolbar step through the marked lines.

### Find and Find all

The find bar searches the open file:

- Type the text, then use **Match case** (Aa), **Whole word** (ab) and **Regex** (.*) as needed.
- The bar shows which match you're on and how many there are, for example "3 of 120". An invalid expression shows "Invalid regex".
- **Enter** goes to the next match, **Shift+Enter** to the previous one, and **Escape** closes the bar.
- **Find all** (Alt+Enter) lists every matching line in a panel below the file, with each match highlighted. Double-click a line to jump to it in the file. Drag the panel's top edge to resize it.

Right-click in the Find all panel for more:

- **Copy selected lines** and **Copy with line numbers**. Select lines with click, Ctrl/Cmd+click, Shift+click or Ctrl/Cmd+A, or **Select all**.
- **Search in these results…** keeps only the lines that also match another search, and **Exclude lines matching…** removes the lines that match it. Steps can be chained; click **All results** or an earlier step to go back.
- **Go to line** jumps to the selected line.

Find and Find all work on what the tab holds, in your browser; they don't read the source again.

## Tail

**Tail** shows new lines as they're written to the file, like `tail -f`. While it runs, a dot shows on the button and the toolbar says how often it checks, for example "every 2 s". The interval is set by your administrator: 1, 2, 5 or 10 seconds.

- Tail fetches only what was appended since the last check, not the whole file.
- Only the active tab is followed, and nothing is fetched while the browser tab is hidden.
- If you have scrolled up, Tail doesn't move you: a button shows how many new lines arrived; click it to jump to the end.
- If the file was rotated or truncated (it shrank, or its start changed), the tab reloads it from the start and says so.
- If the file disappears from the source, Tail stops and the tab keeps what it had.
- A tab keeps at most the last 200,000 lines while Tail adds to it. Older lines are dropped from the top, and the toolbar says so. **Reload** reads the file again.

Some files can't be read from where Tail left off: `.gz` files, files inside archives, and files on push agents older than 0.6. For those, Tail reloads the whole file at each check.

For files over 200 MB, use **Tail the end** in the warning shown when you open them, see [Opening files](#opening-files).

## Downloads

Downloads need the download capability on the source. Without it, the download buttons don't appear.

- **A file:** the download icon in the viewer's toolbar, or the one next to the file in the explorer. A `.gz` file downloads decompressed, without its `.gz` extension.
- **A folder:** the download icon next to the folder in the explorer downloads it as a ZIP of every visible file inside it, in its subfolders too.

Every download is a fresh read from the source and is recorded in the audit log.

Limits set by the server:

- A file, or a folder's total, larger than the server's limit is refused with a message naming the size and the limit (`MAX_FETCH_MB`, 2048 MB by default). A `.gz` file or a file inside an archive that would decompress to more than `MAX_DECOMPRESSED_MB` is refused too.
- Each user can have 2 downloads or folder ZIPs in progress at once by default. When PerchTail is busy, it answers "try again in a few seconds" instead of waiting.
- When the server is out of space for temporary files, the download is refused until space is freed.

A folder ZIP is streamed while it is built, so the browser doesn't know its total size in advance. If something fails once it has started (a source error, a file that grew past the limit), the download stops short and the ZIP is incomplete. A folder ZIP goes at most 32 folders deep, and `.gz` files go into it as they are.

## Archives

`.zip`, `.tar.gz` and `.tgz` files appear in the explorer as folders. Expand one to list the files inside; its list is kept while it stays expanded. Open or download each file inside like any other file. The source's rules apply to the files inside an archive too, by their path through it, for example `logs/old.zip/app.log`.

An archive inside an archive is listed as a file: it doesn't expand.

An archive can be downloaded whole only when none of its files is hidden on purpose by the rules. Otherwise the download is refused with "This archive can't be downloaded whole: open or download its files one by one." The files you can see in it still open and download one by one.

PerchTail also refuses the whole archive when it can't read it as an archive, or when it holds another archive, as soon as the source has any exclude rule.

A folder ZIP leaves out the archives that can't be downloaded whole and lists them in `SKIPPED.txt` inside the ZIP (or `SKIPPED (PerchTail).txt` if the folder already has a `SKIPPED.txt`).

## My profile

Open the menu with your name at the top right and click **My profile**.

### Name and email

For a PerchTail account, change your **Display name** and **Email** and click **Save**. The display name is shown in the top bar. Your **Username** can't be changed. For an SSO account, these come from your identity provider and are updated each time you sign in.

### Password

For a PerchTail account: enter your **Current password** and a **New password**, then click **Change password**. The rules the new password must follow are shown under the field. Changing your password signs you out everywhere else.

### Two-factor authentication

For a PerchTail account, you can add a code from an authenticator app (TOTP) to your sign-in. SSO accounts use their identity provider's second factor instead.

Some accounts must use it: super admins, roles with an administrative capability, and every PerchTail account when an administrator turns on **Require MFA for local accounts**. Such an account without it is taken to **Set up two-factor authentication** after signing in, and can't use anything else until the steps below are done. Turning it off again takes you back to that page.

To turn it on:

1. Click **Enable two-factor authentication**, enter your current password and click **Continue**.
2. Scan the QR code with your authenticator app, or enter the key shown under it.
3. Enter the 6-digit code the app shows and click **Confirm**.
4. Save the 10 backup codes shown: each one works once in place of a code from the app, and they are shown only this time. **Copy codes** copies them. Click **Done**.

While it's on, **Regenerate backup codes** replaces your backup codes with 10 new ones, and **Disable MFA** turns it off. Both ask for your current password, and **Disable MFA** also asks for an **Authentication code**: a code from your authenticator app or one of your backup codes.

If you lose your authenticator app and your backup codes, you can't sign in with that account. An administrator can't turn your two-factor authentication off from the Settings pages.

### Sessions

**Sessions** lists every browser or device signed in as you, with when it signed in and when it was last active. **This device** marks the one you're using. **Revoke** signs another session out; on your own session the button is **Log out**.
