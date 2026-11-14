# Administrator guide

This guide is for the people who set PerchTail up and run it day to day: it covers customers, folders, sources and rules, roles and users, single sign-on, the audit log, alerts, the file-name index and source checks.

Related guides:

- [User guide](user-guide.md): signing in, the explorer, the viewer, Tail, downloads and My profile.
- [Configuration](configuration.md): environment variables and the settings under **Settings → System**.
- [Upgrading](upgrade.md) and [Backup and restore](backup-and-restore.md).
- [Push agent as a service](agent-service.md) and the [agent README](../agent/README.md).
- [Troubleshooting](troubleshooting.md).
- [Preparing a source server](source-setup.md), [SSO setup](sso-setup.md), [Bulk import and export](bulk-import.md) and [Monitoring](monitoring.md).

## Contents

- [The Settings pages](#the-settings-pages)
- [Customers and folders](#customers-and-folders)
- [Sources](#sources)
- [Rules](#rules)
- [Roles and capabilities](#roles-and-capabilities)
- [Users](#users)
- [Single sign-on](#single-sign-on)
- [Audit log](#audit-log)
- [Alerts](#alerts)
- [File-name index](#file-name-index)
- [Background source checks and statuses](#background-source-checks-and-statuses)
- [Agents](#agents)
- [Severity indicators](#severity-indicators)

## The Settings pages

**Settings** in the top bar opens a row of pages. Each page appears only to people whose role allows it:

| Page | Shown to |
|---|---|
| **Sources** | everyone |
| **Folders**, **Agents** | roles with **Create new sources** |
| **Roles** | roles with **Manage roles** |
| **Users** | roles with **Manage users** |
| **SSO** | roles with **Manage SSO settings** |
| **System**, **Severity indicators** | roles with **Manage system settings (feature toggles)** |
| **Import / export** | super admins |
| **Audit log** | roles with **View audit log**, while the **Audit log** switch under **Settings → System** is on |

A super admin sees every page (the Audit log only while its switch in **Settings → System** is on). [Configuration](configuration.md) describes each setting under **Settings → System**.

## Customers and folders

A customer is the top level of the organisation, for example one client or one environment. Folders group sources inside a customer and can be nested as deep as you need. Folders are only for organising and for granting access: they are not hosts.

Create a customer from the source editor: in **Settings → Sources → + Add source**, pick **+ Create new customer…** in the **Customer** list. There is no page to rename or delete a customer.

**Settings → Folders** shows one customer at a time (pick it in **Customer**), with its folders and the sources inside them:

- **+ New folder** adds a folder at the top level or inside another folder (**Inside**).
- Each folder has **Rename**, **Move**, **+ Subfolder** and **Delete**.
- Drag a folder or a source onto another folder to move it there, or onto the **Top level of …** bar to take it out of its folder. A folder can't be moved into itself or below itself.
- A folder can only be deleted when it holds no subfolders and no sources.

You can also create a folder from the source editor with **+ Create new folder…** in the **Folder** list.

Adding, moving and deleting folders needs **Create new sources** and the `manage_source` capability, see [Moving never widens access](#moving-never-widens-access). Deleting a folder or a customer also deletes the role grants and the alerts scoped to it.

## Sources

A source is one host and how to reach it: SSH/SFTP, SMB, WinRM, a push agent, or a local directory on the PerchTail host. [Preparing a source server](source-setup.md) explains what each protocol needs on the server, the **Base path** field, several folders in one source, and the **Browse…** folder picker.

**Settings → Sources** lists the sources you can view, with their customer and folder, protocol, status, credential age and number of rules. Use the filter box and the **Protocol**, **Status** and credential-age lists to narrow it, and click a column header to sort.

- **+ Add source** opens the source editor. A new source opens on its own page when it's saved.
- Click a source's name to open its page: its details, **Browse**, **Test connection**, **Edit**, the **File names** line of the file-name index, the agent enrollment token for push-agent sources, and its rules.
- **Edit** and **Delete** appear for roles with **Create new sources**. The change itself also needs `manage_source` on that source.
- Tick sources and click **Update credentials** to set one new username and password, or SSH key, on all of them.

The **Enabled** checkbox in the source editor turns a source off without deleting it. A disabled source keeps its configuration, but it leaves the Viewer, open tabs from it close, and nothing can be listed, opened, followed, downloaded or checked on it, whatever the user's grants.

Changing a source's host, port, base path or folders means its rules now apply to whatever is at the new location: check that they still match. The change also clears the source's file-name index, the alerts' positions in its files and its status.

### The built-in application-log source

PerchTail creates one system source on first start, **PerchTail application logs**. It reads PerchTail's own log directory (`LOG_DIR`) on the PerchTail host and starts with one include rule, `**/*`. It shows a **system** badge.

Only super admins can reach it: no grant, however broad, gives access to it. Its rules can't be edited, it can't be edited, moved or deleted, and alerts can't watch it.

## Rules

Rules decide which files and folders of a source are visible. They belong to the source and are edited on the source's page (**Settings → Sources**, then the source's name).

- Each rule is an **include** or an **exclude** and has a pattern.
- A pattern is a glob by default. Prefix it with `re:` to write a regular expression, for example `re:^access.*\.log$`.
- Patterns are matched against the path relative to the base path, for example `iis/W3SVC1/u_ex2611.log`. For a source with several folders, the path starts with the folder's label.
- In a glob, `*` matches within one folder, `?` matches one character, `**/` matches any number of folders (so `**/*.log` matches `app.log` and `var/log/app.log`), and a `**` on its own matches anything. A glob must match the whole path.
- A regular expression matches anywhere in the path unless you anchor it with `^` and `$`. Rules use RE2: backreferences and lookaround are refused when you save.
- Rules are evaluated in order and the **last matching rule wins**. A path that no rule matches is hidden.
- A source with **zero rules shows nothing**. The source's page warns about it and **Browse** stays disabled until there is a rule.
- Rules ignore letter case on SMB and WinRM sources and on push agents running on Windows, and respect it everywhere else.

Folders follow the rules too: a folder is shown only if some file below it could be visible. To hide a whole folder reliably, use a glob exclude such as `secret/**`. [Preparing a source server](source-setup.md#principles-that-apply-to-every-protocol) explains how folders, archives and links are handled.

The **Rules** card has two modes:

- **Rows**: one row per rule, with ▲ and ▼ to change the order, the type, the pattern, optional notes and **Delete**. **Add rule** appends a rule at the end.
- **Raw text**: the whole chain as text, one rule per line, top to bottom. A line starting with `!` is an exclude, `re:` switches the line to a regular expression, and blank lines and lines starting with `#` are ignored. **Apply** replaces every rule of the source with the text. A bad line refuses the whole text and leaves the rules as they were. Rule notes are not kept by raw text: applying it replaces the rules without notes.

For example, this shows every `.log` file except those under `secret/`, and still shows `secret/public.log`:

```text
**/*.log
!secret/**
secret/public.log
```

Every rule change is recorded in the audit log. A change that hides files also removes their names from the file-name index at once.

Editing rules needs the `manage_rules` capability on the source. Users with only `view` can read the rules on the source's page.

## Roles and capabilities

A role is a set of global capabilities plus access grants. Each user has exactly one role. Manage roles under **Settings → Roles**: **+ New role**, and **Edit**, **Duplicate** and **Delete** on each row.

The role editor has:

- **Name**.
- **Super-admin (bypasses all grants, reaches system sources)**. Only a super admin can turn this on or off.
- **Global capabilities**, one switch each.
- **Access grants**, once the role is saved: pick a scope type (**Customer**, **Folder** or **Source**), a target and capabilities, then **Add grant**. Grants are saved at once. To change a grant, remove it and add it again.

A role with no grants can't see any source.

### Global capabilities

| On screen | Key | What it allows |
|---|---|---|
| **Create new sources** | `create_source` | Shows **Settings → Folders** and **Settings → Agents**. Together with `manage_source` where it applies: add, edit, move and delete sources, add, move and delete folders, change credentials in bulk, regenerate agent tokens, use **Browse…** with a saved credential, and upgrade push agents. Also creates customers, renames folders, and shows the full error when a connection test fails. |
| **Manage users** | `manage_users` | **Settings → Users**: create users, change their role, name and email, reset passwords, deactivate and reactivate. |
| **Manage roles** | `manage_roles` | **Settings → Roles**: create, edit, duplicate and delete roles and their grants. |
| **Manage SSO settings** | `manage_sso` | **Settings → SSO**: the OIDC provider and the group-to-role mappings. |
| **Manage system settings (feature toggles)** | `manage_system_settings` | **Settings → System** and **Settings → Severity indicators**, the monitoring token, and the system-wide agent upgrade policy and window. |
| **View audit log** | `view_audit_log` | **Settings → Audit log**, including **Verify now**. |
| **Create and edit alerts (webhooks export log content)** | `manage_alerts` | Create, edit, test, turn on and off your own alerts. Alerts send log lines to an outside URL, so viewing a source doesn't imply this. |

Some actions are for super admins only, whatever the capabilities: **Settings → Import / export**, **Mark as known** on the audit log, the **Updates** card under **Settings → System**, the super-admin flag, and the built-in application-log source.

### Grant capabilities

A grant gives capabilities in one customer, folder or source. The role editor and the roles list show each one by its name; hover over it to read what it does. The import/export YAML, the API and the audit log use the key.

| Name (key) | What it allows |
|---|---|
| **View** (`view`) | See the source in the Viewer and in **Settings → Sources**, browse it, open files, use Tail, read its rules, and run the connection check. Opening a file sends its whole content to the browser, so `view` can read whole files. Every open is audited. |
| **Download** (`download`) | Download single files and folders as a ZIP. Every download is audited. |
| **Manage rules** (`manage_rules`) | Edit the source's rules and its own severity indicators. |
| **Manage sources** (`manage_source`) | With **Create new sources**: edit, move and delete sources and folders here, their agent token, saved credential and agent upgrades, and add sources and folders here. It gives no access to the rules. |
| **Run now** (`run_now`) | **Index now** on the source's page, which lists the source's files for the file-name index ahead of schedule. (Its tooltip in the role editor says it isn't used by any feature yet; it is used for this.) |

Give `manage_rules` and `manage_source` together with `view`: the source's page and editor need `view` to load. `manage_source` does nothing without **Create new sources**.

### How a grant is chosen

For each source, PerchTail uses the most specific grant the role has and ignores the others:

1. a grant on the source itself;
2. otherwise a grant on the source's folder;
3. otherwise a grant on the nearest parent folder that has one;
4. otherwise a grant on the customer;
5. otherwise no access.

The most specific grant wins even when it gives less. For example, `view` and `download` on a customer with only `view` on one of its folders means no downloads anywhere in that folder.

Super admins pass every check. The built-in application-log source is reachable only by super admins.

### Moving never widens access

Moving a source or a folder needs `manage_source` on it and `manage_source` where it goes: the destination folder, or the customer for its top level.

A move is also refused when it would give the person moving it more than they already have on the moved item. If the mover's role has no grant on the item itself, the item takes the grants of wherever it lands, so every capability the mover holds at the destination must also be held where the item is now. If the mover's role has a grant on the item itself, that grant keeps applying and the move is allowed.

Adding a source or a folder needs **Create new sources** and `manage_source` where it goes. Only a super admin can add a source with neither a customer nor a folder.

A user with **Create new sources** who creates a new customer has no grant on it yet, so they can't add sources or folders to it until someone gives them `manage_source` there.

### Delegated administrators

A delegated administrator is someone with **Manage users**, **Manage roles** or **Manage SSO settings** who is not a super admin. They can only hand out what they hold themselves:

- They can't change their own role, and can't edit or delete the role they hold.
- They can only give a role a global capability they hold.
- They can only add a grant whose capabilities they hold at that scope, and in any folder or source below it where their own grant gives less.
- They can't create or edit a super-admin role.
- They can only assign a role, create a user with it, duplicate it or map an SSO group to it when the whole role is within their own access.
- They can't reset the password of, deactivate or edit a user whose role reaches beyond their own access.

The **Target** list of the role editor lists customers and folders only for people who also have **Create new sources**. Others can still add grants on sources.

### Built-in roles

PerchTail has two built-in roles, marked **built-in**. Nobody can change their name, capabilities or grants, or delete them:

- **Super Admin**: the role of the first account, created on first start.
- **No Access**: no capabilities and no grants. New SSO users get it unless a group mapping gives them another role.

Built-in roles can be duplicated, and the copy can be edited.

A role can't be deleted while users hold it or while an SSO group mapping points at it. The super-admin flag can't be taken off the last role that active super admins hold.

## Users

**Settings → Users** lists every account with its role, status, sign-in type (**Local** or **SSO**) and last login. Filter by text, **Role**, **Status** and **Sign-in**, and click a column header to sort.

- **+ New user** creates a local account with a **Username**, optional **Display name** and **Email**, a **Temporary password** that follows the password policy, and a **Role**. The user must choose a new password at their first sign-in, and the list shows **must change password** until they do.
- **Edit**, or a click on the username, changes the display name and email of a local account, the role, and **Active**. An SSO account's name and email come from the identity provider and can't be edited here. The username can't be changed.
- The **Role** list in each row changes the role at once.
- **Reset password** (local accounts only) creates a random temporary password, shows it once, signs the user out everywhere and makes them choose a new one at their next sign-in.
- **Deactivate** signs the user out and stops them signing in. Their audit history is kept, and their alerts stop. **Reactivate** restores the account.

Users are never deleted, only deactivated. The last active super admin can't be deactivated or moved to a role without super admin.

Users manage their own MFA in **My profile**. There is no way to reset another user's MFA from the Settings pages.

## Single sign-on

PerchTail supports one OIDC provider at a time. [SSO setup](sso-setup.md) covers registering PerchTail with Entra ID, Okta, Google Workspace, Keycloak or Authentik, and the fields under **Settings → SSO**. When a provider is enabled, the sign-in page shows **Sign in with** and the provider's name.

### Auto-provisioning

The first SSO sign-in creates the account, with the email claim as username (or `preferred_username`, or the subject). The new account gets the **No Access** role unless a group mapping matches, so it signs in but sees no sources until someone gives it a role. An SSO account whose username is already taken by another account can't sign in until that clash is resolved.

The display name and email of an SSO account are updated from the identity provider at every sign-in. SSO accounts have no PerchTail password or MFA: the identity provider handles both.

### Group-to-role mapping

**Group → role mapping** on **Settings → SSO** assigns a role from the user's IdP groups. It needs **Group claim** set on the provider.

- **+ Add mapping** takes an **IdP group name** and a **Role**. A new mapping is added after the existing ones. To change the order, remove mappings and add them again.
- Mappings are evaluated in order and the last matching mapping wins, like rules.
- At every SSO sign-in, if a mapping matches, the user gets that role, even if an administrator gave them another role in PerchTail. The audit log records the change as `user.sso_role_synced`.
- If no mapping matches, the user keeps the role they have. Removing a user from a mapped group, or removing the mapping, stops the sync but does not take the role away. To remove access, change the user's role or deactivate the user in **Settings → Users**.
- A role change from a mapping takes effect at the user's next SSO sign-in. Sessions already open keep the current role until then.

A delegated administrator can only map a group to a role within their own access. They also can't enable a provider or change its issuer, client ID or group claim while any mapping points at a role beyond their access.

## Audit log

**Settings → Audit log** lists every sign-in and every change to sources, rules, roles, users, customers, folders, SSO and system settings, plus every file open, Tail start, download and folder ZIP, and every alert change.

- The table has **Time**, **User**, **Action**, **Target**, **From** (the client's address and User-Agent), **Session** (a short reference to the sign-in session, never its token) and **Details**. Times are shown in your browser's time zone, named in the **Time** header and next to each time. Click **Time**, **User**, **Action** or **Target** to sort; the sort covers every matching entry, not only the page on screen.
- Filter by **Action**, by target (one list per target type, to pick single targets by name or every target of that type, including deleted ones) and by **Date range**. **Clear filters** resets them.
- Targets link to the object while it exists. A deleted object shows its id.
- Pages hold 50 entries, with **Previous** and **Next**.

The **Audit log** switch under **Settings → System** hides the page for everyone. Events are still recorded while it is off. The **Audit folder listings** switch records every folder listed below a source's root; only a super admin can turn it off. Every recorded event is listed in [audit-events.md](audit-events.md).

### Integrity

Each audit entry is chained to the one before it with a keyed hash. The key is derived from `CREDENTIAL_ENCRYPTION_KEY`, so changing or deleting a row breaks the chain from that row on. Rotating the key with the documented procedure re-signs the chain ([credential key rotation](credential-key-rotation.md)).

The banner at the top of the page shows the result of the last check:

- **Audit log integrity verified** with the time of the check.
- **Tampering detected**, with the row where the chain breaks. Everything from that row on can no longer be trusted as unaltered.
- **Known breaks from the #213 race**: concurrent writes before 0.5.2 could fork the chain. These rows were written by PerchTail itself. A super admin clicks **Mark as known** once; verification then accepts exactly those rows. Any other break is still reported as tampering.
- **Audit log integrity has not been checked yet**.

The check runs on its own every `AUDIT_INTEGRITY_CHECK_INTERVAL_DAYS` days (every day by default). **Verify now** runs it at once; a second click within 30 seconds returns the result already stored. A broken chain also turns `/monitoring/health` to `degraded` ([Monitoring](monitoring.md)).

### Retention

**Audit log retention** under **Settings → System** sets how many days entries are kept (365 by default; at least 90, or 0 to keep them forever). Only a super admin can shorten it. A purge runs once a day (`AUDIT_PURGE_INTERVAL_SECONDS`), removes older entries from the oldest up and is itself recorded as `audit.purge`. The chain keeps the hash of the last removed entry, so a purge doesn't count as a break.

## Alerts

An alert calls a webhook when a new line matching its query is written to the files it watches. **Alerts** in the top bar shows the alerts you own. Each user sees only their own alerts, super admins included.

### Who can create alerts

Creating, editing, testing and turning alerts on or off needs **Create and edit alerts (webhooks export log content)**. Without it you can still see and delete your own alerts.

An alert runs only while its owner is active and still holds that capability, checked at every pass. It reads only sources its owner can currently view.

### Creating an alert

**+ New alert** opens a form:

- **Name**.
- **Query**: plain text, matched anywhere in a line, ignoring case. With **Regular expression** ticked, an RE2 regular expression matched anywhere in a line, ignoring case.
- **Watch**: one source, one folder with its subfolders, one customer, or **Every source I can view**. The built-in application-log source can't be watched.
- **Files**: a glob over each source's visible files, `**/*.log` by default. Compressed files (`.gz`) and archives are never read.
- **Webhook URL**: an `http://` or `https://` URL.

Each alert in the list shows its scope, query, files, webhook URL, when it was last checked and last fired, and any failed deliveries. **Test** sends one sample call, **Edit** changes the alert, the switch turns it on or off, and **Delete** removes it with its file positions and pending calls.

### How alerts read files

- A pass runs at the interval set under **Settings → System** (**Alerts**, 60 seconds by default, from 15 seconds to an hour).
- An alert only sees files the [file-name index](#file-name-index) lists. A new file is watched once the index has listed it, and a source left out of the index gives the alert no files.
- A file seen for the first time is read from its end. After that, each pass reads only what was appended, up to 1 MiB per file per pass, like Tail. Only whole lines are matched.
- A file that shrank or whose first bytes changed (rotated or rewritten) is read again from the beginning.
- Each source gets at most 20 files and 15 seconds per alert per pass. Files that don't fit wait for the next passes, the least recently checked first, and the backlog shows in `/monitoring/health`.
- Nothing of the files' content is kept, apart from the matching lines of a call waiting to be retried.

### Webhook calls

Each pass sends at most one call per alert, a JSON `POST` with `alert_id`, `alert_name`, `query`, `regex`, `matched_count` and up to 50 `hits`. Each hit has `source_id`, `source_name`, `file_path`, `offset` (the byte position in the file) and `line` (the matching line, cut at 2000 characters). A test call also has `"test": true`.

A call that fails is retried with a growing delay for up to an hour, then dropped and counted on the alert, with its error shown in the list. Pending calls are dropped as soon as the alert is turned off or its owner may no longer alert. Errors never include the webhook URL, and the audit log records only the webhook's host.

The webhook's host name must resolve only to public addresses. URLs that point at loopback, private, link-local, carrier-grade NAT, reserved or multicast addresses are refused when the alert is saved and again at each call, and the call goes to the address that was checked. A receiver on an internal network can't be used directly.

Deleting a source, folder or customer deletes the alerts scoped to it.

## File-name index

The file-name index lists each source's rule-visible files in the background and keeps their names, sizes and modification times, never their content. It powers the explorer's search across sources ([User guide](user-guide.md#searching-for-sources-and-files)) and tells alerts which files exist.

- **Index file names** under **Settings → System** turns it on or off for everyone. It is on by default. Turning it off stops the listings; names already indexed stay until a source changes.
- **List each source's files every** sets how old a source's index may get before it's listed again: 15 minutes by default, from 5 minutes to a day.
- **Index file names** in the source editor leaves one source out. Turning it off drops that source's names at once.

Every minute, PerchTail queues the sources whose index is older than the interval, the oldest first, and lists four at a time. Each listing walks the source through its rules, like the explorer, down to 32 folder levels. Folders that can't be listed, or that are deeper, are skipped, and the names already indexed below them are kept. Sources that are disabled, left out, or backing off after failed checks are skipped.

The source's page shows the result on the **File names** line: how many files were found and when, or why the last pass stopped short. **Index now** lists the source again without waiting; it needs the `run_now` capability, and it is refused while the index is off for everyone or for that source.

What drops a source's names:

- turning the index off for the source, disabling the source, or deleting it;
- changing its host, port, base path or folders, since that is a different set of files.

A rule change removes the names it now hides at once. Search also checks each source's current rules, so a hidden file never shows in results.

`/monitoring/health` reports the index: the sources taking part, those never indexed, the oldest pass and the passes that failed or stopped short ([Monitoring](monitoring.md)).

## Background source checks and statuses

PerchTail checks in the background that each source answers, so the explorer shows which ones are down before anyone opens them.

- **Check sources in the background** under **Settings → System** sets the interval: 10 minutes by default, 0 turns it off.
- Every minute, the sources whose status is older than the interval are checked, the oldest first, four at a time. Each check lists the base path (each folder, for a source with several). A check that doesn't answer in time counts as failing.
- Push-agent sources are not checked: their status is their live connection.
- Every real operation also updates the status: a browse, an open, a Tail poll. Only errors about reaching the source (unreachable, credentials, host key, TLS) count as failing. An error about one file changes nothing.
- After three failed background checks in a row, a source backs off: its background checks and index passes are skipped for a growing time, up to 30 minutes. It stays failing until any operation on it works. Browsing it is never blocked.
- Changing a source's host, port, path, credential, CA certificate or WinRM configuration clears its status.

**Settings → Sources** shows the status in the **Status** column:

| Status | Meaning |
|---|---|
| ✓ reachable | the last check or operation worked |
| ✕ failing | the last check or operation couldn't reach the source; hover for the reason |
| ⏱ check | not checked yet; click to check now |
| ✓ connected | a push agent is connected |
| ✕ last seen … | a push agent is not connected |
| never connected | a push agent has never connected |
| disabled | the source is turned off |

Clicking a status checks the source again. In the explorer, a coloured dot before each source shows the same status, with the reason in its tooltip.

## Agents

**Settings → Agents** lists every push agent with its version, and upgrades agents to the server's version from the portal: off by default, then manually or automatically inside a weekly window. The page is shown to roles with **Create new sources**. Upgrading an agent, setting its policy or resuming it needs `manage_source` on its source, and changing the system-wide policy and window needs **Manage system settings (feature toggles)**.

See [Push agent as a service](agent-service.md) for running the agent, and [Upgrades from the portal](../agent/README.md#upgrades-from-the-portal) for what the agent's host needs.

## Severity indicators

Severity indicators colour matching text in the viewer, by level (error, warning, info, debug). They only change how lines look, never what can be opened.

- **Settings → Severity indicators** holds the global set, used by every source without its own set. It needs **Manage system settings (feature toggles)**.
- A source can have its own set, edited in its source editor. Its own set replaces the global set entirely; leave it empty to keep the global set. It needs `manage_rules` on the source.

A pattern is plain text matched anywhere in a line, ignoring case, or a regular expression with the `re:` prefix. Each pattern has a level and switches: **line** tints the whole line instead of only the matched text, **nav** includes it in the viewer's previous and next problem buttons, and **on** turns it on or off.
