# Administrator guide

This guide is for the people who set PerchTail up and run it day to day: it covers customers, folders, sources and rules, roles and users, single sign-on, the audit log, alerts, the file-name index, source checks and redaction.

Related guides:

- [User guide](user-guide.md): signing in, the explorer, the viewer, Tail, downloads and My profile.
- [Configuration](configuration.md): environment variables and the system settings on the Settings pages.
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
- [Access review](#access-review)
- [Single sign-on](#single-sign-on)
- [Email](#email)
- [Audit log](#audit-log)
- [Alerts](#alerts)
- [File-name index](#file-name-index)
- [Background source checks and statuses](#background-source-checks-and-statuses)
- [Agents](#agents)
- [Severity indicators](#severity-indicators)
- [Redaction](#redaction)

## The Settings pages

**Settings** in the top bar opens a sidebar of pages grouped in five sections; on a screen narrower than 768 pixels the sidebar is a list at the top of the page. Each page appears only to people whose role allows it, and a section with no page for them isn't shown:

| Section | Page | Shown to |
|---|---|---|
| Sources | **Sources** | everyone |
| Sources | **Folders**, **Agents** | roles with **Create new sources** |
| Sources | **Source defaults** | roles with **Manage system settings (feature toggles)** |
| Access | **Users** | roles with **Manage users** |
| Access | **Roles** | roles with **Manage roles** |
| Access | **Single sign-on** | roles with **Manage SSO settings** |
| Access | **Sign-in security** | roles with **Manage system settings (feature toggles)** |
| Content | **Redaction** | super admins |
| Content | **Severity indicators**, **Explorer and limits** | roles with **Manage system settings (feature toggles)** |
| Operations | **Alerts**, **Backups and updates**, **Monitoring** | roles with **Manage system settings (feature toggles)** |
| Operations | **Email**, **Import / export** | super admins |
| Audit | **Audit log** | roles with **View audit log**, while the **Audit log** switch is on; and roles with **Manage system settings (feature toggles)**, which see the audit log's settings below the log |
| Audit | **Access review** | roles with **View access report** |

A super admin sees every page; while the **Audit log** switch is off, the **Audit log** page shows only its settings. [Configuration](configuration.md#system-settings) describes each setting of Source defaults, Sign-in security, Redaction, Explorer and limits, Alerts, Backups and updates, Monitoring and Audit log. The address of the former System page (`#/settings/system`) opens **Settings → Explorer and limits**.

## Customers and folders

A customer is the top level of the organisation, for example one client or one environment. Folders group sources inside a customer and can be nested as deep as you need. Folders are only for organising and for granting access: they are not hosts.

Only a super admin creates customers, from the source editor: in **Settings → Sources → + Add source**, pick **+ Create new customer…** in the **Customer** list. There is no page to rename or delete a customer.

Customer and folder names are information of each customer, so lists show a role only the customers and folders its grants reach: a customer with a grant on it, on one of its folders or on one of its sources; every folder of a customer with a customer grant; a folder with a grant and every folder below it; and the folders on the path to a source with a grant. Any grant holding at least one capability counts, whatever those capabilities are. A customer or folder outside that reach answers as if it didn't exist, and the refusal is recorded in the audit log. Super admins see everything.

**Settings → Folders** shows one customer at a time (pick it in **Customer**), with its folders and the sources inside them:

- **+ New folder** adds a folder at the top level or inside another folder (**Inside**).
- Each folder has **Rename**, **Move**, **+ Subfolder** and **Delete**.
- Drag a folder or a source onto another folder to move it there, or onto the **Top level of …** bar to take it out of its folder. A folder can't be moved into itself or below itself.
- A folder can only be deleted when it holds no subfolders and no sources.

You can also create a folder from the source editor with **+ Create new folder…** in the **Folder** list.

A source in a folder always belongs to that folder's customer, so it can't sit in a folder while escaping its customer's grants: saving a source whose folder belongs to another customer, or with no customer, is refused ("folder_id must belong to customer_id"). Upgrading to 0.7.0 gave the sources saved that way before the folder's customer ([Upgrading to 0.7.0](upgrade.md#upgrading-to-070)).

Adding, renaming, moving and deleting folders needs **Create new sources** and the `manage_source` capability, see [Moving never widens access](#moving-never-widens-access). Deleting a folder or a customer also deletes the role grants and the alerts scoped to it.

## Sources

A source is one host and how to reach it: SSH/SFTP, SMB, WinRM, a push agent, or a local directory on the PerchTail host. [Preparing a source server](source-setup.md) explains what each protocol needs on the server, the **Base path** field, several folders in one source, and the **Browse…** folder picker.

**Settings → Sources** lists the sources you can view, with their customer and folder, protocol, status, credential age and number of rules. Use the filter box and the **Protocol**, **Status** and credential-age lists to narrow it, and click a column header to sort.

- **+ Add source** opens the source editor. A new source opens on its own page when it's saved.
- Click a source's name to open its page: its details, **Browse**, **Test connection** (with `manage_source` on the source), **Edit**, the **File names** line of the file-name index, the agent enrollment token for push-agent sources, and its rules.
- **Edit** and **Delete** appear for roles with **Create new sources**. The change itself also needs `manage_source` on that source.
- Tick sources (at most 200 at a time) and click **Update credentials** to set one new username and password, or SSH key, on all of them. Each source keeps its own encrypted copy. With **Test the new credential on each source first and update only where it works** (ticked by default), a source the new credential can't reach keeps its old one. The popup then says how many sources were updated, failed and skipped, with the reason for each one not updated: a source without a credential (push agent, local) is skipped, and so is one on which your role lacks `manage_source`, shown only by its number as "Not found" and recorded in the audit log as `access.denied`. Each updated source is audited as `source.credential_update`, without the secret.

The **Enabled** checkbox in the source editor turns a source off without deleting it. A disabled source keeps its configuration, but it leaves the Viewer, open tabs from it close, and nothing can be listed, opened, followed, downloaded or checked on it, whatever the user's grants. Those requests answer 409 with the code `source_disabled`, so the viewer offers no **Try again** and a tab following a file from it stops Tail.

Changing a source's host, port, base path or folders means its rules now apply to whatever is at the new location: check that they still match. The change also clears the source's file-name index, the alerts' positions in its files and its status.

### Archives and large folders

A few limits decide what the explorer lists and what can be downloaded. The [User guide](user-guide.md#archives) describes what users see.

- A folder or an archive lists at most **Entries per listing** entries (**Settings → Explorer and limits**, **Limits** card, 5000 by default), folders first. The explorer then says "Showing the first N entries; narrow with the search." When the source cut the folder's listing before that (a push agent lists at most 50,000 entries of a folder), it says "The source listed only part of this folder; showing the N entries it listed." instead.
- A `.tar.gz` or `.tgz` is read in one pass, and only its first 50,000 entries (files, folders and links together) and 8 MiB of names are read. A bigger one lists its first entries and says the listing was cut short ("This archive has more entries than were read; showing the N read.", unless they fill **Entries per listing**); its members past them can't be opened, a download of the whole archive is refused when the source has an exclude rule, and a redacted download of it is refused. Split such archives into smaller ones. An archive with more than 1 MiB of headers for one entry, or of global headers in all, can't be read.
- A `.zip` member that is encrypted, or compressed with a method PerchTail can't read, can't be opened or downloaded: the answer is "This archive member is encrypted or compressed with a method PerchTail can't read, so it can't be opened.", audited as `file.open_denied` or `file.download_denied` with the reason `member_encrypted` or `member_unsupported_compression`. On a push agent that reads archives on its host (every agent from 0.7, with or without redaction), an encrypted member gets the same answer; a member compressed with a method the agent can't read gets the source's generic error.
- A `.zip` or `.tar.gz` that can't be read as one (damaged, or another kind of file) can't be expanded, and its files can't be opened or downloaded: the answer is "This archive can't be read: it is damaged, or it isn't really a .zip or .tar.gz file.", audited for a file inside it as `file.open_denied` or `file.download_denied` with the reason `archive_unreadable`. A `.gz` file that can't be decompressed answers "This compressed file can't be read: it is damaged, or it isn't really a .gz file." with the same code and audit reason. On a push agent that lists and reads archives on its host (every agent from 0.7, with or without redaction), the agent says it can't read them, and the answer is the same. A `.tar.gz` that ends before its first whole file once decompressed (a few bytes, nothing at all, or only headers such as a long file name or a global header) can't be read either; one that holds only its end-of-archive blocks is an empty archive. A `.zip` whose index is intact but one of its files is damaged still expands, and only that file is refused.
- A folder ZIP is refused before it starts when it couldn't hold every file: "This folder has more entries than a ZIP can list; narrow it to a subfolder" when the source cut a folder's listing short (a push agent lists at most 50,000 entries of a folder), and "This folder is nested too deeply for a ZIP; narrow it to a subfolder" when it has a visible folder 32 or more levels below the folder being zipped. Both are audited as `file.download_denied`, with the reason `listing_truncated` or `too_deep`. A folder whose files add up to more than `MAX_FETCH_MB` is refused with a message naming its size and the limit.

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
- Include rules follow the source's filesystem: they ignore letter case on SMB and WinRM sources and on push agents running on Windows, and respect it everywhere else. Exclude rules, glob and `re:` alike, ignore letter case on every source, so `secret/**` also hides `SECRET/` and `Secret/` on SSH, local and Linux-agent sources.
- Links are judged by where they lead. On SSH, local and push-agent sources, a symlink (and, on a Windows agent, a junction) is listed and read only when both its own path and its target are visible and the target is inside the base path; this holds for a link anywhere on the path, a folder above the file included. A link to a file or folder the rules hide, or out of the base path, is left out of listings, and opening, Tail or downloading it answers as if it didn't exist, audited with the reason `link_target_hidden`. A link to a visible file, such as `current.log` to today's log, keeps working. SMB and WinRM sources never follow a link or a junction.
- A file or folder whose name contains a control character (a newline, a tab) is never listed.
- A source has at most 500 rules, and a pattern (with its `re:` prefix) at most 1000 characters, in the rows, the raw text and a YAML import alike. Rules saved before these limits keep working: a source that already has more than 500 rules can't get another until some are deleted, any change in the rows to a rule whose pattern is longer is refused until its pattern is shortened (it can still be moved or deleted), and raw text or an import that keeps them is refused.

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

A role is a set of global capabilities plus access grants. Each user has exactly one role. Manage roles under **Settings → Roles**: **+ New role**, and **Edit**, **Duplicate** and **Delete** on each row (built-in roles have no **Delete**).

The role editor has:

- **Name**.
- **Super-admin (bypasses all grants, reaches system sources)**. Only a super admin can turn this on or off.
- **Global capabilities**, one switch each.
- **Access grants**, once the role is saved: pick a scope type (**Customer**, **Folder** or **Source**), a target and capabilities, then **Add grant**. Grants are saved at once. To change a grant, remove it and add it again. A role holds at most one grant per customer, folder or source, and a grant needs at least one capability; one saved without any before 0.7.0 gives nothing, not even a view of the customer or folder names.

A role with no grants can't see any source.

### Global capabilities

| On screen | Key | What it allows |
|---|---|---|
| **Create new sources** | `create_source` | Shows **Settings → Folders** and **Settings → Agents**. Together with `manage_source` where it applies: add, edit, move and delete sources, add, rename, move and delete folders, rename and delete customers, change credentials in bulk, regenerate agent tokens, use **Browse…** with a saved credential, and upgrade push agents. Lists only the customers and folders the role's grants reach. |
| **Manage users** | `manage_users` | **Settings → Users**: create users, change their role, name and email, reset passwords, deactivate and reactivate. |
| **Manage roles** | `manage_roles` | **Settings → Roles**: create, edit, duplicate and delete roles and their grants. Short of super admin, only the roles within your own access are listed (see [Delegated administrators](#delegated-administrators)). |
| **Manage SSO settings** | `manage_sso` | **Settings → Single sign-on**: the OIDC provider and the group-to-role mappings. |
| **Manage system settings (feature toggles)** | `manage_system_settings` | **Settings → Source defaults**, **Sign-in security**, **Severity indicators**, **Explorer and limits**, **Alerts**, **Backups and updates**, **Monitoring** (the monitoring token) and the settings on **Audit log**, and the system-wide agent upgrade policy and window. |
| **View audit log** | `view_audit_log` | **Settings → Audit log**, including **Verify now**. |
| **Create and edit alerts (webhooks export log content)** | `manage_alerts` | Create, edit, test, turn on and off your own alerts. Alerts send log lines to an outside URL, so viewing a source doesn't imply this. |
| **View access report** | `view_access_report` | Generate the access report, see [Access review](#access-review). It only reads, so a reviewer can review access without being able to change it; **Manage users** and **Manage roles** don't include it. |

Some actions are for super admins only, whatever the capabilities: creating customers, **Settings → Redaction**, **Settings → Email** and **Settings → Import / export**, **Mark as known** on the audit log, the **Updates** card under **Settings → Backups and updates**, shortening the audit log retention, turning **Audit folder listings** off, the **Two-factor authentication** and **Password recovery** cards, removing or turning off the SSO provider, the super-admin flag, and the built-in application-log source.

### Grant capabilities

A grant gives capabilities in one customer, folder or source. The role editor shows each one by its name; hover over it to read what it does. The import/export YAML, the API and the audit log use the key.

| Name (key) | What it allows |
|---|---|
| **View** (`view`) | See the source in the Viewer and in **Settings → Sources**, browse it, open files, use Tail and read its rules. Opening a file sends its whole content to the browser, so `view` can read whole files. Every open is audited. |
| **Download** (`download`) | Download single files and folders as a ZIP. Every download is audited. |
| **Manage rules** (`manage_rules`) | Edit the source's rules and its own severity indicators. |
| **Manage sources** (`manage_source`) | **Test connection** and checking a source's status from **Settings → Sources** or the explorer. With **Create new sources**: edit, move and delete sources and folders here, rename folders here, rename and delete the customer when the grant is on the customer, their agent token, saved credential and agent upgrades, and add sources and folders here. It gives no access to the rules. |
| **Run now** (`run_now`) | **Index now** on the source's page, which lists the source's files for the file-name index ahead of schedule. |

Give `manage_rules` and `manage_source` together with `view`: the source's page and editor need `view` to load. Without **Create new sources**, `manage_source` only allows **Test connection** and checking the source's status; editing, moving, deleting and adding sources and folders also need **Create new sources**.

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

Only a super admin creates customers. A user with **Create new sources** adds sources and folders only in customers where someone gave them `manage_source`.

### Delegated administrators

A delegated administrator is someone with **Manage users**, **Manage roles** or **Manage SSO settings** who is not a super admin. They can only hand out what they hold themselves:

- They can't change their own role, and can't edit or delete the role they hold.
- They can only give a role a global capability they hold.
- They can only add a grant whose capabilities they hold at that scope, and in any folder or source below it where their own grant gives less.
- They can't create or edit a super-admin role.
- They can only assign a role, create a user with it, duplicate it or map an SSO group to it when the whole role is within their own access.
- They can't reset the password of, deactivate or edit a user whose role reaches beyond their own access.
- They can't reset the password of, deactivate or change the role of a user whose role has **Manage users** or **Manage roles**, even within their own access: only a super admin can. The refusal is recorded in the audit log as `access.denied` with the reason `peer_admin`.
- They only see the roles within their own access: the others are left out of the roles list and the role pickers, and opening one, or its grants, answers as if the role didn't exist. In **Users**, a user holding such a role shows it by id and can't be changed.

The **Target** list of the role editor lists the customers and folders within the delegated administrator's own reach, and the sources they can view.

### Built-in roles

PerchTail has two built-in roles, marked **Built-in**. Nobody can change their name, capabilities or grants, or delete them:

- **Super Admin**: the role of the first account, created on first start.
- **No Access**: no capabilities and no grants. An SSO account gets it at each sign-in when no group mapping gives it another role (see [Group-to-role mapping](#group-to-role-mapping)).

Built-in roles can be duplicated, and the copy can be edited.

A role can't be deleted while users hold it or while an SSO group mapping points at it. The super-admin flag can't be taken off the last role that active super admins hold.

## Users

**Settings → Users** lists every account with its role, status, sign-in type (**Local** or **SSO**) and last login. Filter by text, **Role**, **Status** and **Sign-in**, and click a column header to sort.

- **+ New user** creates a local account with a **Username**, optional **Display name** and **Email**, a **Temporary password** that follows the password policy, and a **Role**. The user must choose a new password at their first sign-in, and the list shows **must change password** until they do.
- **Edit**, or a click on the username, changes the display name and email of a local account, the role, and **Active**. An SSO account's name and email come from the identity provider and can't be edited here. The username can't be changed.
- The **Role** list in each row changes the role at once.
- **Reset password** (local accounts only) creates a random temporary password, shows it once, signs the user out everywhere and makes them choose a new one at their next sign-in. The server refuses it for SSO accounts, which sign in only through the identity provider.
- **Deactivate** ends all of the user's sessions at once and stops them signing in. Their audit history is kept, and their alerts stop. **Reactivate** restores the account; the old sessions don't come back.

Users are never deleted, only deactivated. The last active super admin can't be deactivated or moved to a role without super admin.

Users manage their own MFA in **My profile**. There is no way to reset another user's MFA from the Settings pages. Local accounts of super admins and of roles with any global capability must use MFA; **Require MFA for local accounts** under **Settings → Sign-in security** extends that to every local account ([configuration.md](configuration.md#two-factor-authentication-card)). An account that must use MFA and hasn't set it up can only set it up after signing in.

## Access review

The access report answers who holds what, where, for periodic reviews of access rights. It is computed by the same grant resolution that decides every request (see [How a grant is chosen](#how-a-grant-is-chosen)), at the moment it is generated, so it shows what PerchTail actually enforces. Everything it reads comes from one read transaction, so a change saved while it is being generated is either wholly in the report or not at all. Generating it needs **View access report**; nothing is stored, and each report is generated anew.

### The Access review page

**Settings → Access review** shows the report; the tab appears only to roles with **View access report**. The top of the page says when the report on screen was generated and by whom, in your time zone, and a delegated reviewer also sees a note that the report is limited to their scope (see [Delegated reviewers](#delegated-reviewers)). Opening the page, switching views and changing the number of days each generate a new report.

- **By user** lists each user with their role, sign-in method (with the identity-provider group that gave the role, for an SSO account), last sign-in, MFA, global capabilities and flags. The filter box matches usernames, display names and roles; the flag checkboxes keep the users with any of the flags picked. **Access** on a user's row opens their access, grouped by customer and folder, each group collapsed until opened, with a search box that matches customers, folders and sources and opens what matches. Each source shows the user's capabilities on it and the grant that gave them.
- **By source** lists each source with its customer and folders, and for each capability the users holding it, their role and the grant that gave it. The search box matches sources, customers and folders.
- **Days without sign-in** (90 by default, 1 to 3650) sets when an account counts as inactive; changing it generates the report again.
- **Export CSV** and **Export JSON** download the report from the server for the view and number of days on screen, so every export is generated, stamped and audited like any other report; the filters and searches on the page don't apply to the exports.

The exports are named `perchtail-access-report-<view>-<UTC date>.csv` or `.json`, where `<view>` is `users` or `sources`. Every export says when (UTC) and by whom it was generated: `generated_at` and `generated_by` at the top of the JSON file, and as the first two columns of every CSV row. The JSON file also says the `scope` (`full` or `delegated`, see below), the `view` and the `parameters` (`inactive_days`, the days without sign-in).

### What it lists

Only active accounts are listed. For each user:

| Field | What it is |
|---|---|
| `username`, `display_name` | The account. Emails are not included. |
| `role` | The user's role (id and name). |
| `auth_provider` | `local` or `oidc`. |
| `sso_role_group` | For an SSO account, the identity-provider group whose mapping gave the role at the user's last sign-in. Empty when no mapping matched, when an administrator changed the role by hand afterwards, and until the user's first sign-in after upgrading to the release that added it. Only this one group name is kept, never the account's other groups. |
| `last_login_at` | The last sign-in, UTC; empty if the account never signed in. |
| `mfa` | `enabled` or `disabled` for a local account; `idp` for an SSO account, whose second factor is the identity provider's. |
| `global_capabilities` | The role's global capabilities; all of them for a super admin. |
| `flags` | See below. |
| `reaches_all_sources` | `capabilities`: the capabilities the user holds on every source in the report (the system source aside). `customer_grant_on_every_customer`: whether the role also has a grant on every customer, so that sources added later are reached too. |
| `access` (`users` view) | One entry per source where the user holds at least one capability: the source, its customer and folders, the `capabilities`, and `via`. |

The `sources` view lists every source in the report, with its customer and folders, and for each user who holds something on it the `capabilities` and `via`. A source nobody holds anything on is listed with no users.

`via` says what decided an access, as a list, so that other ways of holding access can be added later without changing the format. Each entry has `principal_type` (today always `role`), `principal_id` and `principal_name` (the role), and the scope of the grant that decided: `scope_type` (`customer`, `folder` or `source`, or `super_admin` for a super admin, whom no grant decides), `scope_id` and `scope_name`. When the grant sits above what the person generating the report may see, the entry keeps its `scope_type`, has no `scope_id` or `scope_name`, and `above_your_scope` is `true`.

### Flags

| Flag | Meaning |
|---|---|
| `super_admin` | The role is a super-admin role: it passes every check. |
| `global_capabilities` | The role has at least one global capability: a privileged account. |
| `local_without_mfa` | A local account without MFA. |
| `must_change_password` | An account created with a temporary password that hasn't chosen its own yet. |
| `inactive` | Never signed in, or not for more than `inactive_days` days. |
| `reaches_all_sources` | The user holds at least one capability on every source in the report; `reaches_all_sources.capabilities` says which. |

### Delegated reviewers

A super admin's report covers every user and every source, the built-in application-log source included (`scope` is `full`). Anyone else with **View access report** gets a report limited to their own scope (`scope` is `delegated`):

- only the sources where they hold at least one capability themselves;
- every active user who holds something on those sources, whatever their own privileges (super admins included), and only the access they hold there;
- users without any access there only when the reviewer also has **Manage users**;
- folder names above the reviewer's own grant are left out of a source's path (`folder_path_truncated` is `true`, and the CSV shows `…` in their place), and so are the id and name of a grant scope above it in `via`; `reaches_all_sources` and its customer detail only consider the sources and customers within that scope.
- a source's customer is always shown in its place (`customer_id` and `customer_name`, and the start of `path`), even when the reviewer's grant is on a folder below it, because the reviewer already sees it in the Sources list; a grant on that customer still shows as `above_your_scope` in `via`, without its id or name.

### CSV columns

The CSV has one row per user and source (**By user**) or per source and user (**By source**). A user without access, or a source without users, gets one row with the other side empty. Lists inside a cell are separated by `; `. A cell starting with a tab, a carriage return or a line feed, or with `=`, `+`, `-`, `@` or their full-width forms (`＝`, `＋`, `－`, `＠`) after any leading spaces, gets a leading `'`, so a spreadsheet doesn't run it as a formula.

- **By user** (`users`): `generated_at`, `generated_by`, `username`, `display_name`, `role`, `auth_provider`, `sso_role_group`, `last_login_at`, `mfa`, `global_capabilities`, `flags`, `source_id`, `source`, `path`, `capabilities`, `via`.
- **By source** (`sources`): `generated_at`, `generated_by`, `source_id`, `source`, `path`, `username`, `display_name`, `role`, `capabilities`, `via`.

`path` is the customer and folders, separated by ` / `. `via` reads like `role Operators: folder Web servers`, `role Operators: customer above your scope` or `role Super Admin: super admin`.

Every report generated, shown or downloaded, is recorded in the audit log as `access_report.generate`, with the view, format, scope, `inactive_days` and how many users and rows it held, never its content. A request without **View access report** is refused and recorded as `access.denied`.

## Single sign-on

PerchTail supports one OIDC provider at a time. [SSO setup](sso-setup.md) covers registering PerchTail with Entra ID, Okta, Google Workspace, Keycloak or Authentik, and the fields under **Settings → Single sign-on**. When a provider is enabled, the sign-in page shows **Sign in with** and the provider's name.

### Auto-provisioning

The first SSO sign-in creates the account, with the email claim as username when the identity provider says it is verified (otherwise `preferred_username`, or the subject). The account gets the **No Access** role unless a group mapping matches, so it signs in but sees no sources. An SSO account whose username is already taken by another account can't sign in until that clash is resolved.

The display name and email of an SSO account are updated from the identity provider at every sign-in. SSO accounts have no PerchTail password or MFA: the identity provider handles both, and the local sign-in form refuses them.

### Group-to-role mapping

**Group → role mapping** on **Settings → Single sign-on** assigns a role from the user's IdP groups. It needs **Group claim** set on the provider.

- **+ Add mapping** takes an **IdP group name** and a **Role**. A new mapping is added after the existing ones. To change the order, remove mappings and add them again.
- Mappings are evaluated in order and the last matching mapping wins, like rules.
- At every SSO sign-in, if a mapping matches, the user gets that role, even if an administrator gave them another role in PerchTail. The audit log records the change as `user.sso_role_synced`.
- If no mapping matches, the user gets the **No Access** role, recorded as `user.sso_role_synced` with reason `no_matching_mapping`. Removing a user from a mapped group, or removing the mapping, takes the role away at their next sign-in. The last active super admin is the exception: they keep the role, and the log has `sso.role_sync_kept_last_super_admin`.
- A role change from a mapping takes effect at the user's next SSO sign-in. Sessions already open keep the current role until then; to remove access at once, deactivate the user in **Settings → Users**.

A delegated administrator can only create, change or delete a mapping whose role is within their own access, and only at a position before every mapping whose role is beyond it (the last matching mapping wins). They also can't enable a provider or change its issuer, client ID or group claim while any mapping points at a role beyond their access. Only a super admin removes the provider (**Remove** on **Settings → Single sign-on**) or turns it off. **Settings → Single sign-on** shows a delegated administrator every mapping, but a role beyond their access as "A role outside your access", without its name, and offers **Remove** only on the mappings they may remove.

## Email

**Settings → Email**, for super admins only, sets the SMTP server PerchTail sends email through. Nothing is configured by default, and nothing is sent until a server is saved with **Enabled** on. PerchTail sends three kinds of message: the test message from this page, password-reset links when [password recovery](#password-recovery) is on, and a notice to a user's previous address when they change their own email.

| Field | What it is |
|---|---|
| **SMTP server**, **Port** | The server's host name or IP address and its port. |
| **Security** | **STARTTLS** (usually port 587): PerchTail connects and upgrades to TLS before anything else. A server that doesn't offer STARTTLS is refused; PerchTail never sends in plain text. **Implicit TLS** (usually port 465): TLS from the first byte. There is no mode without TLS. |
| **Username**, **Password** | The account PerchTail signs in with, after TLS is up. Leave the username blank for a relay that accepts mail without signing in; the password is then not kept. The password is encrypted with `CREDENTIAL_ENCRYPTION_KEY`, never shown again, and left as it is when the field is blank on saving. **Remove the saved password** deletes it. |
| **Sender address**, **Sender name** | The `From` of every message. The name defaults to `PerchTail`. |
| **CA certificate** | Optional PEM certificates trusted in addition to the system's, for a server whose certificate comes from an internal CA. The certificate is always checked, and must name the host in **SMTP server**. |
| **Enabled** | Turns sending on or off without losing the settings. |

The server can be on a private network (an internal relay is the usual case) or on the internet. It can't be PerchTail's own host (loopback), a link-local address (which includes cloud metadata services), a multicast, unspecified or reserved address. The host name is resolved and checked when the settings are saved and again before every message, and PerchTail connects to the address it checked, so a name that later resolves somewhere else is checked again rather than followed.

**Remove** deletes the saved settings, password included, and stops all sending.

The **Send a test message** card appears once settings are saved: **Send test message** sends `Test message from PerchTail` to the **Recipient**, or to your own email when the field is blank, with the saved settings, and shows the result. Errors are short and never include the password or the server's reply. At most 5 test messages can be sent per minute for the whole installation.

Every message is plain text and carries `Auto-Submitted: auto-generated`, so autoresponders don't answer it. It holds only what the feature sending it needs; no log content is ever sent by email.

Saving, removing and testing are recorded in the audit log (`smtp_settings.*`), without the password and without the message. See [Audit log events](audit-events.md).

### Password recovery

**Password recovery by email**, in the **Password recovery** card of **Settings → Sign-in security**, lets users of local accounts choose a new password when they've forgotten theirs, without an administrator. It's off by default, and only a super admin can turn it on or change how long links work (**Links work for**, 5 to 60 minutes, 30 by default). It can be turned on only while email is set up and on here and `PUBLIC_BASE_URL` starts with `https://`, since the link carries a secret; if email is later turned off or removed, recovery stops working until it's back, and the card says why.

While it's on, the sign-in page shows **Forgot password?**. A user enters their username or email address and PerchTail answers the same way whether or not an account matches; the lookup and the email happen after the answer, so neither the answer nor its timing says whether the account exists.

- Only an active local account with an email address can use it. Super admins, roles with any global capability, SSO accounts and accounts without an email never get a link. This is checked again when the link is used, so an account that changes role, is deactivated or moves to SSO in the meantime can't use a link it already has.
- The email holds only the link and how long it works, and says to ignore it if you didn't ask: no username, name or address. The link is `<PUBLIC_BASE_URL>/#/reset-password/<token>`: the token, 32 random bytes, sits after `#`, so browsers never send it to PerchTail or a reverse proxy when the page opens, and the page removes it from the address bar at once. PerchTail keeps only its SHA-256.
- A link works once, for the configured minutes. It also stops working when the user's password changes (their own change or an administrator's **Reset password**), when their email changes, and when they ask for another link.
- The new password must follow the password policy, can't be the current one or, with history on, a recent one. Setting it ends every session of the account and signs nobody in: the user signs in again, with their second factor if they use MFA. It doesn't lift a sign-in lockout.
- Limits, fixed: 10 requests per 15 minutes from one address (more get `429` and wait); 3 links per hour for one account, or for one entered username or email that matches no account (more are answered as usual, but nothing is sent); 20 link checks and uses per 15 minutes from one address.

Every request is recorded as `user.password_reset_requested` with its result, including requests that match no account, which keep the username or email that was entered, as failed sign-ins keep the username. Repeats within a minute are folded into one entry. Using a link is recorded as `user.password_reset_completed`, and a link refused when it is opened or used as `user.password_reset_failed` with the reason. No token, link or password is ever logged or audited. See [Audit log events](audit-events.md).

Because the email address is where a reset link goes, users must enter their current password to change their own email in **My profile**, and when email is on, the previous address is told of the change (`user.email_change_notice`). An administrator can still change a user's email under **Settings → Users**; any reset link already sent stops working.

## Audit log

**Settings → Audit log** lists every sign-in and every change to sources, rules, roles, users, customers, folders, SSO and system settings, plus every file open, Tail start, download and folder ZIP, and every alert change.

- The table has **Time**, **User**, **Action**, **Target**, **From** (the client's address and User-Agent), **Session** (a short reference to the sign-in session, never its token) and **Details**. Times are shown in your browser's time zone, named in the **Time** header and next to each time. Click **Time**, **User**, **Action** or **Target** to sort; the sort covers every matching entry, not only the page on screen.
- Filter by **Action**, by target (one list per target type, to pick single targets by name or every target of that type, including deleted ones) and by **Date range**. **Clear filters** resets them.
- Targets link to the object while it exists. A deleted object shows its id.
- Pages hold 50 entries, with **Previous** and **Next**.

The **Audit log** switch, on the **Settings → Audit log** page below the log, hides the log for everyone; whoever manages system settings still sees the page, with its settings, so the switch can be turned on again. Events are still recorded while it is off. The **Audit folder listings** switch records every folder listed below a source's root; only a super admin can turn it off. Every recorded event is listed in [audit-events.md](audit-events.md).

### Integrity

Each audit entry is chained to the one before it with a keyed hash. The key is derived from `CREDENTIAL_ENCRYPTION_KEY`, so changing or deleting a row breaks the chain from that row on. Rotating the key with the documented procedure re-signs the chain ([credential key rotation](credential-key-rotation.md)).

The banner at the top of the page shows the result of the last check:

- **Audit log integrity verified** with the time of the check.
- **Tampering detected**, with the row where the chain breaks. Everything from that row on can no longer be trusted as unaltered.
- A warning that breaks match **the #213 race**: concurrent writes before 0.5.2 could fork the chain. These rows were written by PerchTail itself. A super admin clicks **Mark as known** once, then **Record as known** in the confirmation; verification then accepts exactly those rows. Any other break is still reported as tampering.
- **Audit log integrity has not been checked yet**.

The check runs on its own every `AUDIT_INTEGRITY_CHECK_INTERVAL_DAYS` days (every day by default). **Verify now** runs it at once; a second click within 30 seconds returns the result already stored. A broken chain also turns `/monitoring/health` to `degraded` ([Monitoring](monitoring.md)).

### Retention

**Audit log retention** under **Settings → Audit log** sets how many days entries are kept (365 by default; at least 90, or 0 to keep them forever). Only a super admin can shorten it. A purge runs once a day (`AUDIT_PURGE_INTERVAL_SECONDS`), removes older entries from the oldest up and is itself recorded as `audit.purge`. The chain keeps the hash of the last removed entry, so a purge doesn't count as a break.

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

The webhook URL is stored encrypted and never shown again: the list shows only its scheme, host and port, and when editing, leaving **Webhook URL** empty keeps the saved one.

Each alert in the list shows its scope, query, files, the webhook's scheme, host and port, when it was last checked and last fired, and any failed deliveries. **Test** sends one sample call (at most 5 a minute per user), **Edit** changes the alert, the switch turns it on or off, and **Delete** removes it with its file positions and pending calls.

### How alerts read files

- A pass runs at the interval set under **Settings → Alerts** (**Check alerts every**, 60 seconds by default, from 15 seconds to an hour).
- An alert only sees files the [file-name index](#file-name-index) lists. A new file is watched once the index has listed it, and a source left out of the index gives the alert no files.
- A file seen for the first time is read from its end. After that, each pass reads only what was appended, up to 1 MiB per file per pass, like Tail. Only whole lines are matched.
- A file that shrank or whose first bytes changed (rotated or rewritten) is read again from the beginning.
- Each source gets at most 20 files and 15 seconds per alert per pass. Files that don't fit wait for the next passes, the least recently checked first, and the backlog shows in `/monitoring/health`.
- Nothing of the files' content is kept, apart from the matching lines of a call waiting to be retried.
- When [redaction](configuration.md#redaction) is on for a source, the query runs on its lines as redacted, for every alert, so a value that was replaced can't be matched, and only redacted lines are kept and sent. A line with no end within one read is replaced by a `[redacted: ...]` marker, and a file in an encoding or compressed format redaction can't read isn't matched at all: it waits where it is, logged once as `alert.file_read_failed` until its size, modification time or first bytes change. A call still waiting to be sent or retried is redacted again as it goes out, with the rules that apply then, so turning redaction on also covers calls queued before; while the rules can't run, it isn't sent.

### Webhook calls

Each pass sends at most one call per alert, a JSON `POST` with `alert_id`, `alert_name`, `query`, `regex`, `matched_count` and up to 50 `hits`. Each hit has `source_id`, `source_name`, `file_path`, `offset` (the byte position in the file) and `line` (the matching line, cut at 2000 characters). A test call also has `"test": true`.

A call that fails is retried with a growing delay for up to an hour, then dropped and counted on the alert, with its error shown in the list. Pending calls are dropped as soon as the alert is turned off or its owner may no longer alert. Errors never include the webhook URL, and the audit log records only the webhook's host.

The webhook's host name must resolve only to public addresses. URLs that point at loopback, private, link-local, carrier-grade NAT, reserved or multicast addresses, or at IPv6 addresses that carry an IPv4 one for a translator or tunnel (NAT64, 6to4, Teredo, IPv4-translated and IPv4-compatible addresses), are refused when the alert is saved and again at each call, and the call goes to the address that was checked. A receiver on an internal network can't be used directly. When the receiver can't be reached, whatever the reason (refused, timed out, reset, TLS), the alert and the test show only "connection failed"; the reason is in the server log.

Deleting a source, folder or customer deletes the alerts scoped to it.

## File-name index

The file-name index lists each source's rule-visible files in the background and keeps their names, sizes and modification times, never their content. It powers the explorer's search across sources ([User guide](user-guide.md#searching-for-sources-and-files)) and tells alerts which files exist.

- **Index file names** under **Settings → Source defaults** turns it on or off for everyone. It is on by default. Turning it off stops the listings; names already indexed stay until a source changes.
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

- **Check sources in the background** under **Settings → Source defaults** sets the interval: 10 minutes by default, 0 turns it off.
- Every minute, the sources whose status is older than the interval are checked, the oldest first, four at a time. Each check lists the base path (each folder, for a source with several). A check that doesn't answer in time counts as failing.
- Push-agent sources are not checked: their status is their live connection.
- Every real operation also updates the status: a browse, an open, a Tail poll. Only errors about reaching the source (unreachable, credentials, host key, TLS) count as failing. An error about one file changes nothing.
- After three failed background checks in a row, a source backs off: its background checks and index passes are skipped for a growing time, up to 30 minutes. It stays failing until any operation on it works. Browsing it is never blocked.
- Changing a source's host, port, base path or folders, credential, CA certificate, WinRM configuration or SMB signing and encryption options clears its status.

**Settings → Sources** shows the status in the **Status** column:

| Status | Meaning |
|---|---|
| ✓ reachable | the last check or operation worked |
| ✕ failing | the last check or operation couldn't reach the source; hover for the reason |
| ⏱ check | not checked yet; click to check now (shown as **not checked yet** without `manage_source`) |
| ✕ failed | the check you just clicked couldn't reach the source; hover for the reason |
| ✓ connected | a push agent is connected |
| ✕ last seen … | a push agent is not connected |
| never connected | a push agent has never connected |
| disabled | the source is turned off |

With `manage_source` on the source, clicking a status checks it again. In the explorer, a coloured dot before each source shows the same status, with the reason in its tooltip.

## Agents

**Settings → Agents** lists every push agent with its version, and upgrades agents to the server's version from the portal: off by default, then manually or automatically inside a weekly window. The page is shown to roles with **Create new sources**. Upgrading an agent, setting its policy or resuming it needs `manage_source` on its source, and changing the system-wide policy and window needs **Manage system settings (feature toggles)**.

An agent older than 0.7 doesn't tell the server where links lead, so the server reads no file through it: its folders can still be browsed, but opening, Tail, downloads, folder ZIPs and archives are refused with "This source's agent must be updated…" (audited with the reason `agent_update_required`), alerts skip its files until it is updated, and the page marks it **Update required**. Such agents must be upgraded by hand once ([Upgrading by hand](agent-service.md#upgrading-by-hand)).

See [Push agent as a service](agent-service.md) for running the agent, and [Upgrades from the portal](../agent/README.md#upgrades-from-the-portal) for what the agent's host needs.

## Severity indicators

Severity indicators colour matching text in the viewer, by level (error, warning, info, debug). They only change how lines look, never what can be opened.

- **Settings → Severity indicators** holds the global set, used by every source without its own set. It needs **Manage system settings (feature toggles)**.
- A source can have its own set, edited in its source editor. Its own set replaces the global set entirely; leave it empty to keep the global set. It needs `manage_rules` on the source.

A pattern is plain text matched anywhere in a line, ignoring case, or a regular expression with the `re:` prefix. Each pattern has a level and switches: **line** tints the whole line instead of only the matched text, **nav** includes it in the viewer's previous and next problem buttons, and **on** turns it on or off.

Patterns run in every viewer's browser, so they have the same limits as rule patterns: at most 1000 characters, and regular expressions in the rules' syntax (RE2: no backreferences or lookaround). The viewer matches them in a background worker, apart from the page, and only on the first 2000 characters of each line. A pattern that takes more than a second in all over the lines in view, or over the whole file for the previous and next problem buttons, or that gets stuck on a line, is turned off until the page is reloaded, with a warning that names it ("A severity indicator took too long to match and is turned off until you reload the page: …"), and the other patterns keep working: make such a pattern simpler, for example without a repeated group inside another (`(a+)+`).

A pattern saved before these limits, with lookaround, a backreference or more than 1000 characters, keeps working in the viewer, and can still be switched off, given another level or switches, or deleted; only a change to its pattern must be in the rules' syntax.

## Redaction

Redaction replaces secrets, payment card numbers and personal data in file content with `[redacted: ...]` for every user, super admins included: in the viewer, Tail, alerts and, while **Redact downloads too** is on (the default), downloads. Only super admins configure it: **Settings → Redaction** holds the switches, the eleven built-in rules and the custom rules for every source, and the **Redaction** section of each source's editor turns system rules off for that source and adds rules of its own. Nobody else sees either. Redaction is best effort: see [Redaction](redaction.md) for the rules, their false positives, where content is redacted and what it doesn't cover.
