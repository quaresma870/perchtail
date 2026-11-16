# Bulk import and export

**Settings → Import / export** (super admins only) creates and updates customers, folders, sources and their rules from one YAML file, and exports the current configuration in the same format. Use it to set up many sources at once, to keep the configuration in version control, or to copy it between PerchTail instances.

Start from the [template](examples/perchtail-import.example.yaml), which the import screen also offers (**Download template**, **Start from template**). **Export current configuration** downloads what PerchTail has now.

## How an import works

1. Paste the file, or pick it with **Load a file**, and click **Preview**. Nothing is written yet. The preview lists every customer, folder and source the file would create or change, and what changes, or the problems that stop it, each with its place in the file (`customers[0].sources[2].base_path`).
2. Type credentials for any new sources the file has none for, or leave them blank and set them later in each source's settings.
3. Click **Apply** (the button shows how many changes). The whole file is applied in one transaction: either every change lands or none does.

An import is **idempotent**: importing the same file again changes nothing. It is also **additive**: customers, folders and sources the file doesn't mention are left as they are, and none is ever deleted.

A file can be at most 1 MB and hold at most 5000 sources.

Things are matched by name:

- a customer by its name;
- a folder by its name under the same parent folder of the same customer;
- a source by its name under the same customer, wherever it sits in that customer's folders. Putting it under another folder in the file moves it. A source with no customer is matched among the other sources with no customer. The protocol of an existing source can't be changed by an import.

## Format

```yaml
version: 1
customers:
  - name: Example Customer
    folders:                  # optional, nested as deep as you like
      - name: production
        folders: [...]
        sources: [...]
    sources: [...]            # sources directly under the customer
sources: [...]                # sources with no customer
```

A source:

| Key | Required | Notes |
|---|---|---|
| `name` | yes | |
| `protocol` | yes | `ssh`, `smb`, `winrm`, `local` or `agent` |
| `host` | yes | |
| `port` | no | the protocol's default otherwise |
| `base_path` | yes, unless `roots` | checked as in the source editor, see [Base path](source-setup.md#base-path) |
| `roots` | no | two or more `{label, path}` to read several folders, see [Several folders in one source](source-setup.md#several-folders-in-one-source) |
| `enabled` | no | default `true` |
| `search_indexing_enabled` | no | removed in 0.7 with the content index: ignored with a warning, refused from 1.0 |
| `name_index_enabled` | no | default `true`: the source's file names (never content) go into the file-name index |
| `winrm_scheme` | no | WinRM sources only: `https` or `http`. Left out: `https` when `port` is left out or is 5986, `http` for any other port |
| `tls_ca_pem` | no | WinRM sources only: the CA certificate (PEM) of an HTTPS listener whose certificate comes from an internal CA or is self-signed, as **CA certificate** in the source editor |
| `smb_require_signing` | no | SMB sources only: `false` lets the source connect to a server that doesn't sign its messages, as **Require signing** in the source editor. Left out: a new source requires signing; an existing one keeps its setting |
| `smb_encrypt` | no | SMB sources only: `true` asks for SMB 3 encryption and refuses a server that can't encrypt, as **Encrypt** in the source editor. Left out: a new source leaves encryption to the server; an existing one keeps its setting |
| `agent_upgrade_policy` | no | push-agent sources only: `inherit` (default, follows Settings → Agents), `off`, `manual` or `automatic`; see [agent upgrades](../agent/README.md#upgrades-from-the-portal) |
| `credential` | no | `username` plus `password` or `private_key`; see below |
| `rules` | no | replaces the source's rules when given; leave it out to keep the rules it has |
| `redaction_rules` | no | the source's own redaction rules; replaces them when given, leave it out to keep them; see below |
| `redaction_disable` | no | names of the system redaction rules the source turns off; replaces that list when given, leave it out to keep it |

**Updating an existing source:** apart from `credential`, `rules`, `redaction_rules`, `redaction_disable`, `smb_require_signing` and `smb_encrypt`, every setting in the table takes the value in the file, or its default when the file leaves it out. For example, a source that's disabled in PerchTail is enabled again by a file without `enabled: false`, and a WinRM source loses its CA certificate when the file has no `tls_ca_pem`. Export first, and edit the exported file, to keep what's set. A source's **JEA endpoint** isn't in the file (see below) and is kept.

Rules are evaluated in order and the **last match wins**, as everywhere in PerchTail. Write each as `include <pattern>` or `exclude <pattern>`; patterns are globs, or regexes when they start with `re:`. To add a note, use the long form:

```yaml
rules:
  - include **/*.log
  - exclude **/debug/**
  - type: include
    pattern: re:^access.*\.log$
    notes: access logs
```

Don't quote just the pattern. In `rules: [include **/*, exclude "*/secret/**"]` each item is a single string, so the quotes would become part of the pattern and the rule would never match; the import refuses such a pattern. Quote the whole rule if you need to (`- "exclude */secret/**"`), or use the list form above.

A new source with no rules shows nothing until rules are added.

Redaction rules replace matching text in file content before it leaves the server. Each of a source's own rules has a `name` (at most 48 characters, used in the replacement `[redacted: <name>]`), an RE2 `pattern`, `max_length` (the longest match it can make, in bytes, from 1 to 4096), and optionally `secret_group` (the capture group to replace instead of the whole match), `enabled` (default `true`) and `notes`. They're checked as when they're saved one by one: a pattern that doesn't compile, matches empty text, or can match more than `max_length` bytes is a problem. `redaction_disable` names system rules, which must exist; the system rules themselves aren't in the file.

```yaml
redaction_rules:
  - name: msisdn
    pattern: \b(?:\+?255)(\d{9})\b
    secret_group: 1
    max_length: 13
redaction_disable:
  - ipv4
```

Unknown keys are reported as problems rather than ignored, so a typo like `base_pth` doesn't silently drop a setting.

## Credentials

Credentials are **never exported**. Re-importing an exported file keeps the credential each existing source already has.

On import, a source's credential comes from, in this order:

1. what you type for it in the import screen;
2. its `credential` block in the file. A value can be inline, or a reference to an environment variable of the PerchTail server written `${env:PERCHTAIL_IMPORT_NAME}`. Only variables whose names start with `PERCHTAIL_IMPORT_` can be referenced, so an import can't read the server's other settings, such as the credential encryption key;
3. the credential the source already has.

A file with inline passwords is as sensitive as the passwords themselves. Prefer environment references or typing them in, and don't commit such a file to version control.

## What isn't covered

- Push-agent enrollment tokens: generate them on the source's page after importing.
- A WinRM source's **JEA endpoint**: it isn't exported, and the import has no key for it. An import keeps the endpoint an existing source has; a new source has none, so it uses a WinRS shell until you set **JEA endpoint** in the source editor. When you copy a configuration to another instance, set it again on each WinRM source that uses the [read-only JEA endpoint](examples/jea/README.md).
- Roles, grants, users, SSO and system settings.
- Severity indicators and alerts.

## Audit

Every customer, folder and source an import creates or changes gets its own audit entry, marked `"via": "import"`, plus one `config.import` entry with the totals. Exports are audited as `config.export`.
