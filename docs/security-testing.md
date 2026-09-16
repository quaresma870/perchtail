# Local security testing with Strix

This covers running [Strix](https://github.com/usestrix/strix), an open-source
autonomous AI pentesting agent, against a local PerchTail instance. It's a
supplement to the CI-blocking checks described in the README (dependency
scanning, Trivy container-image scanning) and the manual review scope in
[SECURITY.md](../SECURITY.md) — not a replacement for either. Run it locally,
on a throwaway dev instance, never against a deployment holding real
credentials or customer data.

Strix needs a local Docker daemon to run its scan containers, which isn't
available in every environment (e.g. this project's CI sandbox) — that's why
this is a "run it yourself" doc rather than something wired into CI.

## Prerequisites

- Docker running locally.
- An LLM provider Strix can call. Set both:
  ```bash
  export STRIX_LLM="anthropic/claude-sonnet-5"   # or another provider Strix supports
  export LLM_API_KEY="<your-api-key>"
  ```
  (Strix also supports a Docker-free `strix cloud login` mode instead of the
  two env vars above, if you'd rather not manage a local key.)
- Install the CLI:
  ```bash
  curl -sSL https://strix.ai/install | bash
  ```

Config persists to `~/.strix/cli-config.json` after the first run.

## Running PerchTail locally as a scan target

Bring up a normal dev instance — Strix needs something running to attack.
Either the docker-compose stack:

```bash
docker compose up -d
```

which serves on `http://localhost:8080` (see `docker-compose.yml`), or the
backend + frontend dev servers directly per the README's dev setup.

**Use a disposable dev/test database and dummy source credentials.** Strix
will exercise the app aggressively, including anything it can reach through
auth flows and forms — don't point it at an instance with real SSH/SMB/WinRM
credentials configured, and don't run it against anything but `localhost`.

## Running a scan

```bash
strix --target http://localhost:8080 \
  --scan-mode standard \
  --instruction "Focus on the RBAC grant-resolution logic (customer/folder/source scoping), auth/session handling (local + OIDC), and the archive browse/download endpoints for path traversal. Do not attempt destructive requests against the source-credential encryption-at-rest mechanism beyond confirming it's not returned in API responses." \
  --output ./strix-report
```

Flags worth knowing:

- `-t/--target` — the URL to scan (required).
- `-m/--scan-mode` — `quick`, `standard`, or `deep`. Start with `quick` to
  sanity-check the setup before committing to a longer `deep` run.
- `--instruction` — free-text steering. Point it at the areas called out in
  SECURITY.md's scope section (RBAC, auth providers, path traversal/injection
  via rule patterns or archive endpoints, anything that could turn the
  read-only viewer into a write path back to a source) rather than letting it
  wander generically — this app's actual attack surface is narrower than a
  typical web app's.
- `-o/--output` — where to write the report.
- `--keep-container` — leave the scan container running after completion, if
  you want to inspect its state.
- `-v/--verbose` — more detail while it runs.
- `-n` — non-interactive, for scripting a scan without prompts.

## Triaging findings

Strix's report is a starting point, not a verdict — like any automated
scanner, expect some false positives, especially around this app's read-only
invariant (it may flag things a normal CRUD app would consider a write path
that PerchTail's design already treats as append-only/audit-only, e.g.
`AuditLog` writes).

For anything that looks real:

1. Reproduce it manually first — confirm the request/response, don't just
   trust the agent's narrative.
2. If confirmed, follow [SECURITY.md](../SECURITY.md)'s reporting process
   (private security advisory or direct email) rather than opening a public
   issue, same as any other vulnerability report — even though you found it
   yourself.
3. If it's a hardening idea rather than an exploitable bug (e.g. a missing
   defense-in-depth header), a regular public issue is fine.
