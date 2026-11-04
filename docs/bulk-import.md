# Bulk import and export

**Settings → Import / export** (super admins only) creates and updates
customers, folders, sources and their rules from one YAML file, and exports
the current configuration in the same format. Use it to set up many sources
at once, to keep the configuration in version control, or to copy it
between PerchTail instances.

Start from the [template](examples/perchtail-import.example.yaml), which
the import screen also offers as a download.

## How an import works

1. Paste or load the file and click **Preview**. Nothing is written yet.
   The preview lists every customer, folder and source the file would
   create or change, and what changes, or the problems that stop it, each
   with its place in the file (`customers[0].sources[2].base_path`).
2. Type credentials for any new sources the file has none for, or leave
   them blank and set them later in each source's settings.
3. Click **Apply**. The whole file is applied in one transaction: either
   every change lands or none does.

An import is **idempotent**: importing the same file again changes nothing.
It is also **additive**: anything the file doesn't mention is left as it is,
and nothing is ever deleted.

Things are matched by name:

- a customer by its name;
- a folder by its name under the same parent folder of the same customer;
- a source by its name under the same customer, wherever it sits in that
  customer's folders. Putting it under another folder in the file moves it.
  A source with no customer is matched among the other sources with no
  customer. The protocol of an existing source can't be changed by an
  import.

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
| `search_indexing_enabled` | no | default `false` |
| `credential` | no | `username` plus `password` or `private_key`; see below |
| `rules` | no | replaces the source's rules when given; leave it out to keep the rules it has |

Rules are evaluated in order and the **last match wins**, as everywhere in
PerchTail. Write each as `include <pattern>` or `exclude <pattern>`; patterns
are globs, or regexes when they start with `re:`. To add a note, use the
long form:

```yaml
rules:
  - include **/*.log
  - exclude **/debug/**
  - type: include
    pattern: re:^access.*\.log$
    notes: access logs
```

A new source with no rules shows nothing until rules are added.

Unknown keys are reported as problems rather than ignored, so a typo like
`base_pth` doesn't silently drop a setting.

## Credentials

Credentials are **never exported**. Re-importing an exported file keeps
the credential each existing source already has.

On import, a source's credential comes from, in this order:

1. what you type for it in the import screen;
2. its `credential` block in the file. A value can be inline, or a
   reference to an environment variable of the PerchTail server written
   `${env:PERCHTAIL_IMPORT_NAME}`. Only variables whose names start with
   `PERCHTAIL_IMPORT_` can be referenced, so an import can't read the
   server's other settings, such as the credential encryption key;
3. the credential the source already has.

A file with inline passwords is as sensitive as the passwords themselves.
Prefer environment references or typing them in, and don't commit such a
file to version control.

## What isn't covered

- Push-agent enrollment tokens: generate them in the source's settings
  after importing.
- Roles, grants, users, SSO and system settings.
- Severity indicators and alerts.

## Audit

Every customer, folder and source an import creates or changes gets its
own audit entry, marked `"via": "import"`, plus one `config.import` entry
with the totals. Exports are audited as `config.export`.
