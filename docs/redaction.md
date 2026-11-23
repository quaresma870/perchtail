# Redaction

Redaction replaces secrets, payment card numbers and personal data in file content with a marker such as `[redacted: email]` before anyone sees it: in the viewer, in Tail, in downloads and in what alerts send to their webhooks. It never changes the files on the source.

**Redaction is best effort.** Each rule finds only what it was written to find, in the forms it was written for. A secret written in a way no rule expects (split across lines, encoded, in a format nobody anticipated) stays in clear, and a rule can also replace text that isn't secret. Turn on the rules that fit your logs, check them on real files before you rely on them, and keep access to sources limited to the people who need it: redaction is a second line of defence, not a replacement for access control.

## Contents

- [Who configures it](#who-configures-it)
- [Turning it on](#turning-it-on)
- [Built-in rules](#built-in-rules)
- [Custom rules](#custom-rules)
- [A source's rules](#a-sources-rules)
- [Where content is redacted](#where-content-is-redacted)
- [What users see](#what-users-see)
- [Downloads](#downloads)
- [Alerts](#alerts)
- [What redaction doesn't cover](#what-redaction-doesnt-cover)
- [Audit](#audit)

## Who configures it

Only super admins. They see **Settings → Redaction** and the **Redaction** section of each source's editor; nobody else sees either, and the API refuses everyone else, `manage_system_settings` and `manage_rules` included. Every change is recorded in the audit log.

Redaction applies to every user, super admins included. There is no role or capability that sees the original content while redaction is on.

## Turning it on

**Settings → Redaction** holds three switches ([configuration.md](configuration.md#redaction) lists their keys):

- **Redact file content** turns redaction on or off. It asks for confirmation either way. Off, which is the default, nothing is redacted and the rules below are kept as they are.
- **Redact downloads too** (on by default): while redaction is on, downloads are redacted as well. Off, downloads carry the original content, while the viewer, Tail and alerts stay redacted. See [Downloads](#downloads).
- **Say so in the file name** (on by default): a redacted download is named for it, such as `app.redacted.log`. It only counts while downloads are redacted.

Redaction applies to a source when it is on and at least one rule runs on that source. With no system rule on, only sources with rules of their own are redacted, and the page says so.

## Built-in rules

PerchTail ships eleven built-in rules. All of them are off on a new install; each one turned on in **Settings → Redaction** applies to every source unless a source turns it off. Built-in rules can be turned on or off and turned off per source, but not changed or deleted.

| Rule | What it replaces | Marker | False positives |
|---|---|---|---|
| `card` | Numbers of 12 to 19 digits, written together or grouped with spaces or hyphens, that pass the Luhn check and start and are as long as a card brand's numbers (Visa, Mastercard, American Express, Discover, JCB, Diners, UnionPay, Maestro). | `[redacted: card ****1234]`, with the last four digits | Long numeric ids that pass the Luhn check and look like a card brand's (about one in ten 16-digit ids starting with 4). IMEIs and millisecond timestamps are left alone. |
| `bearer` | The token after `Bearer` (8 characters or more), as in `Authorization` headers and token logs. | `[redacted: token]` | Prose where "bearer" is followed directly by a word of 8 or more characters, such as "the bearer certificate". |
| `authorization` | The value of an `Authorization` or `Proxy-Authorization` header or key, whatever its scheme (Basic, Digest, a token). | `[redacted: authorization]` | A key named `authorization` whose value isn't a secret, such as `authorization: granted`. |
| `secret_kv` | Values of keys whose name holds `password`, `passwd`, `pwd`, `secret`, `token`, `api key`, `access key`, `client secret` or `private key`, written `key=value` or `key: value`. `null` and `none` are left alone. | `[redacted: password]`, `[redacted: api key]` and so on, from the key's name | `PWD=/home/user` in shell traces, and counters such as `token_count=5`. |
| `url_password` | The password in URLs such as `postgres://user:password@host`. The user name stays. | `[redacted: password]` | Rare. |
| `private_key` | PEM private keys, from the `BEGIN ... PRIVATE KEY` line to its `END` line, or up to 200 lines when the `END` line is missing. The line breaks stay, so line numbers don't move. Certificates aren't redacted. | `[redacted: private key]` | Almost none. |
| `jwt` | JSON Web Tokens (`eyJ....eyJ....signature`). | `[redacted: jwt]` | Very rare. |
| `email` | Email addresses. Nothing of the address is kept. | `[redacted: email]` | `user@host.domain` in SSH logs, and other ids written like an address with a domain. |
| `ipv4` | IPv4 addresses. | `[redacted: ip]` | Version numbers with four parts (`1.2.3.4`), and parts of OIDs. |
| `ipv6` | IPv6 addresses, checked to be valid: times such as `12:30:45` and MAC addresses are left alone. | `[redacted: ip]` | Hexadecimal ids in colon-separated groups that happen to form a valid IPv6 address. |
| `iban` | Bank account numbers (IBAN), checked against the country's length and the check digits. Nothing of the number is kept. | `[redacted: iban]` | Very rare once the check digits are checked. |

## Custom rules

A custom rule is a regular expression of your own. **Settings → Redaction → Custom rules for every source** holds the ones that apply to every source; a source's editor holds the ones of that source only. Each one has:

- **Name**: at most 48 characters, without control characters, `[` or `]`, unique where the rule lives. It goes into each replacement: `[redacted: order id]`.
- **Pattern (RE2)**: at most 1000 characters, in [RE2 syntax](https://github.com/google/re2/wiki/Syntax), so it runs in linear time: no backreferences or lookaround, and no `\C`. It runs one line at a time, so it never matches across a line break, and it can't match empty text.
- **Group to replace**: the capture group to replace, keeping the rest of the match (for example the key before a value); empty replaces the whole match.
- **Longest match (bytes)**: the most bytes the pattern can match, from 1 to 4096. A pattern whose longest match can be worked out (no unbounded repeat) must fit in it. PerchTail uses it to make sure a value cut by the [line length limit](#what-redaction-doesnt-cover) leaves nothing behind.
- **Notes**, for yourself.

A pattern made only of ASCII characters matches byte by byte, so invalid UTF-8 in a file doesn't stop it. A pattern with other characters matches UTF-8 text; on bytes that aren't valid UTF-8 the server and the push agent can then find slightly different matches.

**Try it on sample text** runs the pattern on text you type and shows what it matches and the redacted result. It never runs on a file and saves nothing: use made-up values, never real secrets.

Each place holds at most 50 rules. Rules can be turned off without deleting them, and moved up or down: when two rules match overlapping text, the matches are merged into one replacement and the first rule gives the marker.

## A source's rules

The **Redaction** section of a source's editor, under **Settings → Sources**, shows:

- **System rules**: every rule from **Settings → Redaction**, with whether it applies here. A rule on system-wide can be turned off for this source only (and back on); a rule off system-wide applies nowhere and can't be turned on for one source.
- **This source's own rules**: custom rules that apply to this source only, after the system rules.
- For a push-agent source whose agent can't redact on its host, a warning; for one older than 0.7, a warning that it must be updated before its files can be read (see [Where content is redacted](#where-content-is-redacted)).

Changes in this section are saved at once, apart from the rest of the form. The rules that run on a source are the system rules that are on and that it doesn't turn off, in their order, then its own rules that are on, in theirs.

A source's own rules and the system rules it turns off are also in the configuration export and import (`redaction_rules`, `redaction_disable`; see [bulk-import.md](bulk-import.md)). The system rules themselves aren't.

## Where content is redacted

- **SSH, SMB, WinRM and local sources**: on the PerchTail server, as the content arrives, before it is written to scratch, sent to a browser or queued for a webhook. Only the redacted copy is kept in scratch.
- **Push-agent sources**: on the agent's host, by agents of version 0.7.0 and later, which announce it when they connect (`redaction/1`). The rules travel with each request, the agent redacts with the same engine (both are tested against the same test vectors), and the original content never leaves the host. Archives are listed and their files read on the host; the server only rebuilds an archive or a ZIP from what arrived redacted. The server uses an answer only when it says it was redacted with the rules it sent. If the agent can't run a custom rule, the request fails with the rule's name rather than falling back to the server.
- **Push agents older than 0.7.0** can't redact on their host, and they don't say where links lead either (`links/1`), so PerchTail reads none of their files, with or without redaction: their folders can still be browsed, alerts skip their files, and opening, Tail, downloads, folder ZIPs and archives are refused with 409 and "This source's agent must be updated before its files can be read. Ask an administrator to update it from Settings → Agents.", audited with the reason `agent_update_required`. **Settings → Agents** marks them **Update required**, and the source editor's **Redaction** section says the agent must be updated before its files can be read. Upgrade them by hand once ([agent-service.md](agent-service.md#upgrading-by-hand)).

## What users see

- In the viewer and Tail, each replaced value shows as `[redacted: <marker>]`, highlighted, and the viewer says how many values were redacted in the open file. In Tail, the count follows what the tab still holds.
- A line still being written, with no line break yet, shows as `[redacted: incomplete line]` until it is complete, since no rule can see the whole value yet. A file opened at its end starts at the next whole line.
- A line longer than 1 MiB keeps its start, redacted, and the rest becomes `[redacted: line over 1 MiB]`.
- UTF-16 files are redacted and shown as UTF-8.
- The encoding is checked again on every line, as content can change encoding part way. From a line of plain text that looks UTF-16, or a line of a UTF-16 file with plain ASCII text in it, to the end of what is read, the content is redacted as bytes and as both UTF-16 readings, and shown as bytes. From a line that looks UTF-32, the rest of what is read isn't shown: the viewer and Tail show what came before it and `[redacted: unsupported encoding]` in its place, which the count of redacted values leaves out. Binary content can look UTF-16 or UTF-32 to these checks: it is then redacted more, or cut short, never shown less redacted.
- A file in a format redaction can't read isn't shown: UTF-32 files (409, `redaction_unsupported_encoding`), and content compressed in a format PerchTail doesn't decompress (bzip2, xz, zstd, 7z, rar, and gzip or zip content in a file not named as one, such as a rotated `app.log.1` that is really gzip), told by its first bytes (409, `redaction_unsupported_format`). The refusal is audited.
- `.zip` and `.tar.gz` archives still open as folders, and their files open one by one, redacted; a `.gz` file opens decompressed and redacted.
- The viewer's download button says **Download (redacted)** when the download will be redacted.

## Downloads

While **Redact downloads too** is on:

- a single file, a `.gz` file (downloaded decompressed) or an archive member is redacted as it is sent, its last line included; a UTF-16 file stays UTF-16; one whose content turns UTF-32 part way ends with `[redacted: unsupported encoding]` in place of the rest; a UTF-32 file, content in a compressed format PerchTail doesn't decompress, and a `.gz` archive member that can't be redacted whole are refused (409);
- a whole `.zip` or `.tar.gz` is rebuilt in the same format, without the files the visibility rules hide and with every other file redacted; a `.gz` file inside is decompressed, redacted and compressed again under the same name; an archive that can't be read is refused (403);
- a folder ZIP holds every file redacted, its `.gz` files and archives treated the same way;
- what can't be redacted is left out and listed, with the reason, in `REDACTION-SKIPPED.txt` inside the archive or ZIP, including a file whose content turns UTF-32 part way;
- every redacted download carries the `X-PerchTail-Redacted: 1` header and, with **Say so in the file name**, `.redacted` in its name;
- each archive rebuilt redacted, downloaded whole or inside a folder ZIP, on the server or from what a push agent redacted, logs `archive.rebuild_done` (INFO) with the source (`source_id`), the archive's `path`, the size of the rebuilt archive (`result_bytes`), how many files went in redacted (`redacted_members`) and how many were left out (`skipped_members`), and how long it took (`seconds`).

[configuration.md](configuration.md#redaction) has the details of each case.

While it's off, downloads carry the original content, from push-agent sources too.

## Alerts

An alert's query runs on the redacted lines, for every alert, so a value that was replaced can't be matched, and only redacted lines are queued and sent to webhooks. The lines are redacted as Tail's are, on the agent's host for an agent that redacts there; each hit's `offset` is still where its line starts in the original file, and turning redaction on or off neither restarts a file nor repeats a match. A webhook call still waiting to be sent or retried is redacted again as it goes out, with the rules that apply then, so one queued before redaction was turned on doesn't send its lines in clear; while the rules can't run, it isn't sent. A file redaction can't read (UTF-32, also from a line part way through it, or a compressed format PerchTail doesn't decompress) waits where it is, logged once as `alert.file_read_failed` until it changes. See [Alerts](admin-guide.md#how-alerts-read-files) in the administrator guide.

## What redaction doesn't cover

- **File and folder names**, including the names of archive members, aren't redacted anywhere: in the explorer, the file-name search, downloads or the audit log.
- **Encrypted archive members**, **symbolic links in a `.zip`** and **zip members compressed with a method PerchTail can't read** (such as Deflate64) can't be redacted: they are left out of redacted downloads and listed in `REDACTION-SKIPPED.txt`.
- **Archives inside an archive** (second level) aren't redacted as a whole: the explorer doesn't expand them, while redaction is on they can't be opened in the viewer (403), and a redacted download of the outer archive leaves the inner archive out and lists it in `REDACTION-SKIPPED.txt`, and from a push agent it can't be downloaded while downloads are redacted.
- **UTF-32 files** and **content in compressed formats PerchTail doesn't decompress** are refused rather than shown, as above, and left out of redacted archives and ZIPs. Content that turns UTF-32 part way is cut short at that line, as above.
- **Content compressed part way through a plain text file** (a gzip or xz stream after text lines) is redacted as bytes, so values inside the compressed stream aren't found.
- **Files over `MAX_DECOMPRESSED_MB`** once decompressed are left out of redacted archives and ZIPs.
- **A `.tar.gz` with more than 50,000 entries** lists only its first 50,000 and can't be downloaded whole while downloads are redacted.
- **The last, unfinished line** of a file being written isn't shown in the viewer or Tail until it ends, as above. Downloads count the end of the file as the end of a line.
- **Values split across lines**: every rule matches within one line, except private keys.
- **Severity indicators** and the viewer's find run in the browser on the redacted content, so they can't find a replaced value.

## Audit

- `redaction_rule.create` (the new rule's values), `redaction_rule.update` (each changed field, old and new) and `redaction_rule.delete` (the rule's name), and `redaction_override.set` when a source turns a system rule off or back on.
- Changes to the three switches, as `system_settings.update`.
- `file.open`, `file.follow`, `file.download` and `folder.download` carry `"redaction": "applied"` when what was sent was redacted, with `"redaction_at": "server"` or `"agent"`; a whole archive and a folder ZIP also carry `skipped_count`, the files listed in `REDACTION-SKIPPED.txt`.
- When content turned UTF-32 part way, `file.open` and `file.follow` say so with `redaction_refused` and `redaction_refused_offset` (where the refused rest starts in the file). For a download redacted on the server as it is sent, that is only known once it is sent: a `file.download_redaction_skipped` entry follows, with `"reason": "redaction_unsupported_encoding"` and `offset`; redacted on an agent, `file.download` carries `redaction_refused` and `redaction_refused_offset` itself. A folder ZIP that only found while it was sent that it had to leave files out gets one `file.download_redaction_skipped` entry per reason (`redaction_unsupported_encoding` or `redaction_unsupported_format`), with `skipped_count` and their paths in `skipped`.
- Refusals of content redaction can't read are recorded as `file.open_denied` (with `"follow": true` for Tail) or `file.download_denied`, with the reason, such as `redaction_unsupported_encoding`, `redaction_unsupported_format` or `redaction_whole_archive`.

The audit log never holds file content, redacted or not. [audit-events.md](audit-events.md) has every event.
