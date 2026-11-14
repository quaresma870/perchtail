# Load tests

Scripts that load a PerchTail instance the way its users do, measure what the server spends doing it, and turn each run into the tables of [docs/sizing.md](../docs/sizing.md). They are meant to be re-run for every release, and the same scenarios drive longer soak tests.

A run seeds the instance through its public API (customers, folders, sources of every protocol, push agents, users with a test role), writes log files for the sources to serve, then runs [k6](https://k6.io) scenarios in phases while a collector records CPU, memory, scratch space and the SQLite database every 5 seconds. Everything the kit creates is named `lt-…` (or another prefix, `LOADTEST_PREFIX`), recorded by id in a ledger, and can be removed again.

## Contents

| Path | What it is |
|---|---|
| `run.py` | Runs a profile end to end: seed, phases, collector, summary. |
| `profiles/*.json` | The profiles: `tiny` (validates the kit), `small`, `medium`, `large`. |
| `seed/seed.py` | Seeds and cleans an instance through the API. |
| `seed/gen_logs.py` | Writes the served log files and keeps some of them growing. |
| `seed/agents.py` | Builds the push agent from `agent/` and runs one process per agent source. |
| `compose.sources.yml` | SSH and SMB test servers, and the instance settings a local run needs. |
| `sources/` | The images of those two servers. |
| `k6/*.js` | One file per scenario, and `profile.js`, which runs one phase of a profile. |
| `collect/collect.py` | The server-side collector. |
| `report/report.py` | Builds the Markdown summary of a run. |
| `tests/` | Unit tests of the kit's own logic: `python -m unittest discover -s loadtest/tests`. |

Local files, ignored by git: `loadtest/.state/` (passwords, tokens, generated logs, the agent binary) and `loadtest/results/` (one folder per run).

## Tools and versions

| Tool | Version | Notes |
|---|---|---|
| k6 | 1.4.2 | Native (`k6` on the PATH, exactly this version) or the image `grafana/k6:1.4.2@sha256:3656673de3f30424e8ebcfa46acd9558d83b6a43612d0f668ffeac953950c6c7`, which `run.py` uses when the native one is missing or another version. |
| Docker Engine with Compose | Compose v2 or later | Runs the instance and the SSH/SMB test servers, and k6 and Go when they aren't installed. |
| Python | 3.12 or later | Standard library only: nothing to `pip install`. |
| Go | the version in `agent/go.mod` (1.27) | Builds the push agent. Without it, `agents.py build` uses `golang:1.27@sha256:ec47ea3725fea0cc88f6de94919aef19f40f12fc1ec0e2d1b3426dbf15ecca04` in Docker. |
| Test servers | `debian:trixie-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2` | OpenSSH and Samba from Debian, in `sources/`. |

Installing k6 natively: see [the k6 installation guide](https://grafana.com/docs/k6/latest/set-up/install-k6/) and pick v1.4.2, or skip it and let `run.py` use the image.

## Safety

- `LOADTEST_BASE_URL` (the instance) is required and has no default. Every script, and every k6 scenario, refuses to run unless its host is listed in `LOADTEST_ALLOWED_HOSTS` (comma-separated host names), so load is never sent to someone else's instance by mistake.
- No credential is in the repository, in logs or on stdout. Passwords and tokens come from environment variables or from `loadtest/.state/`, written with owner-only permissions where the OS supports them.
- The kit's state is kept per instance (see [State](#state)): a command refuses state written for another URL, so cleaning instance B never restores instance A's settings and a stale seed never points the index at another instance's sources.
- The kit only touches what it created. `apply` reuses objects from its ledger and refuses when one of its names is already taken by something else; `clean` only removes or deactivates ledger objects whose names still match the kit's exact patterns. Settings are restored key by key, only the keys the kit changed.
- The admin account seeds and cleans; the collector reads `/monitoring/health` with the monitoring token only. On an instance that already has a monitoring token, pass it in `LOADTEST_MONITORING_TOKEN`: the kit never regenerates an existing one, which would break the monitoring that uses it.
- Nothing contacts outside services except what installing the tools needs: pulling the Docker images above, and Go downloading the agent's one module dependency when it builds. The `compose.sources.yml` override also turns off PerchTail's daily release check (`UPDATE_CHECK=false`).

## The minimal run (profile `tiny`, under 15 minutes)

On a development machine with Docker, from the repository root. On Windows, use Git Bash; check out the repository with `git clone -c core.autocrlf=false …`, or the instance's image gets a shell entrypoint with CRLF line endings and the container doesn't start.

1. Create `.env` for the instance (ignored by git), with a random credential key and plain-HTTP cookies:

   ```bash
   python -c "import secrets; print(f'CREDENTIAL_ENCRYPTION_KEY={secrets.token_urlsafe(32)}')" > .env
   printf 'SESSION_COOKIE_SECURE=false\nPUBLIC_BASE_URL=http://localhost:8080\n' >> .env
   ```

2. Create the kit's state (a random password for the test servers' account) and start the instance, built from this checkout, with the test servers. `MAX_FETCH_MB` and `SCRATCH_MAX_GB` keep their defaults (2048 MB and 5 GB), the values the sizing guide is measured with:

   ```bash
   python loadtest/seed/seed.py init
   docker compose -f docker-compose.yml -f loadtest/compose.sources.yml up -d --build
   ```

3. Point the kit at it, and set up the admin account. On first start PerchTail logs a one-time password for `admin`; `admin-init` reads it from the logs on stdin, signs in and replaces it with a random password kept in the instance's state (`loadtest/.state/instances/localhost-8080/admin.json`). Nothing is printed:

   ```bash
   export LOADTEST_BASE_URL=http://localhost:8080
   export LOADTEST_ALLOWED_HOSTS=localhost
   docker compose logs perchtail | python loadtest/seed/seed.py admin-init
   ```

4. Run the profile, and clean up afterwards:

   ```bash
   python loadtest/run.py --profile tiny --clean
   ```

The summary is printed at the end and saved as `loadtest/results/<date>-tiny/summary.md`. Stop the instance with `docker compose -f docker-compose.yml -f loadtest/compose.sources.yml down` (add `-v` to delete its data volume too).

## Running a profile

`run.py --profile NAME` does everything in order:

1. `seed.py apply`, `gen_logs.py generate`, `agents.py build` (once) and `agents.py start`, and `seed.py index` (waits until the file-name index has listed every seeded source);
2. `gen_logs.py grow` in the background for the whole run;
3. each phase of the profile in turn: its System settings applied, the collector started, `k6 run k6/profile.js` with `LOADTEST_PHASE` set, the collector stopped; 15 seconds between phases;
4. the profile's settings back, agents and growing stopped, `report.py` on the run folder.

| Option | Meaning |
|---|---|
| `--phases a,b` | Only these phases of the profile. |
| `--container NAME` | The PerchTail container the collector measures (default `perchtail-perchtail-1`, or `LOADTEST_CONTAINER`). |
| `--pid PID --scratch-dir DIR --db FILE` | Measure a PerchTail process on this Linux machine instead (an install from source). |
| `--k6 native\|docker` | Force where k6 runs (default: native when it is v1.4.2, otherwise Docker). |
| `--skip-seed` | The instance is already seeded for this profile (checked against its own state). |
| `--clean` | `seed.py clean` at the end. |
| `--interval S` | Collector interval (default 5 seconds). |

Each phase's folder in `loadtest/results/<date>-<profile>/` holds `k6-<phase>.json` (the k6 summary), `k6.log`, `server.csv`, `health.jsonl` and `hardware.json`. A k6 exit code of 99 means a threshold failed; for `ramp` that is expected once a step passes the limits, and the summary says which step counts as the breaking point.

`tiny` takes about 13 minutes (about 750 s per run on a laptop), plus a minute or two the first time to build the images and the agent. `small`, `medium` and `large` run 70 minutes of fixed phases plus the ramp (24, 30 and 36 minutes at most), so about 1.5 to 2 hours each.

## State

`loadtest/.state/` (or `LOADTEST_STATE_DIR`) holds, at its root, what doesn't depend on an instance: `sources.env` (the test servers' account), `bin/` (the agent binary) and `logs/` (the generated files). Everything about one instance lives in `instances/<host>-<port>/`, named after `LOADTEST_BASE_URL`'s host and port:

| File | What it holds |
|---|---|
| `admin.json` | The admin account `admin-init` set up. |
| `ledger.json` | The id of every object the kit created, by prefix, kind and name. |
| `users.json` | The test users' passwords. |
| `seeded.json`, `catalog.json` | The seeded sources and the file paths, for the scenarios. |
| `agent-tokens.json`, `agent-pids.json`, `agents/` | The agents' tokens, processes and output. |
| `monitoring.json` | The monitoring token, when the kit generated it. |
| `settings-original.json` | The instance's value of each System setting the kit changed, from before its first change. |

Every file records the URL it was written for (normalized: scheme, host, explicit port, path), and every command refuses one written for another URL. State left at the root of `loadtest/.state/` by an earlier version of the kit isn't tied to an instance: the kit refuses to start until it is removed.

## Seeding

```bash
python loadtest/seed/seed.py init                    # the state directory and the test servers' account
docker compose logs perchtail | python loadtest/seed/seed.py admin-init   # fresh instance only
python loadtest/seed/seed.py apply --profile small   # customers, folders, sources, role, users, agent tokens
python loadtest/seed/seed.py index                   # run the file-name index on the seeded sources, and wait
python loadtest/seed/seed.py status                  # count the kit's objects, and any other objects with its prefix
python loadtest/seed/seed.py clean [--files]         # remove the kit's objects (and the generated logs)
```

Every object `apply` creates goes into the ledger, by id, as soon as it is created. `apply` is idempotent: it reuses the ledger's objects and only creates what is missing, re-enables what is disabled, and checks each user's saved password (resetting it through the API if it doesn't work). It never adopts an object it didn't create: when one of its names is already taken by an object outside the ledger (another kit's, or one made by hand), it stops before creating anything and says which. Running a bigger profile after a smaller one adds the difference.

Names start with `LOADTEST_PREFIX` and a dash; the prefix is `lt` by default, and can be any lower-case letter followed by up to 15 lower-case letters or digits. Another prefix is the way out when the default names are taken by objects outside the ledger: PerchTail can't rename or delete user accounts, so `lt-user-NNN` accounts left by an old run, or by a state directory that was lost, would otherwise block `apply` for good. With `LOADTEST_PREFIX=lt2`, the kit creates `lt2-customer-01`, `lt2-user-001` and so on. The ledger keeps each prefix's objects apart, and `seed.py` and `run.py` only work on the current prefix's: to clean objects created under another prefix, run `clean` with that prefix (`clean` and `status` say when the ledger holds any). Set the prefix for every command of a run.

What it creates (with the default prefix):

- `lt-customer-NN` customers, each with `lt-cNN-folder-NN` folders; sources are placed round-robin on the customers' roots and folders;
- `lt-ssh-NNN`, `lt-smb-NNN`, `lt-local-NNN`, `lt-winrm-NNN` and `lt-agent-NNN` sources, each with one rule, `include **`;
- the role `lt-role`, with `view` and `download` on every `lt-` customer, and the users `lt-user-NNN` holding it. Each user is created with a random temporary password and then signs in and sets its own random password, as PerchTail's forced first-login change requires; the passwords are kept in `users.json`;
- an enrollment token for each agent source;
- a monitoring token, only when the instance has none;
- the profile's System settings. Before the kit first changes a key (in `apply`, or in a phase), it saves the instance's value of that key.

`clean` only acts on ledger objects of the current prefix that still have the kit's exact name (`lt-customer-NN`, `lt-cNN-folder-NN`, `lt-(ssh|smb|local|winrm|agent)-NNN`, `lt-role`, `lt-user-NNN`, with `lt` being the prefix): it deletes the sources, folders, customers and role, and puts back only the System settings the kit changed. An object renamed since is left alone and forgotten; a folder or customer that still holds something else is kept (PerchTail refuses to delete it) and stays in the ledger. PerchTail's API can only deactivate users, never delete them (accounts are kept for the audit trail), so the kit's users are moved to the built-in no-access role and deactivated, and stay in the ledger: a later `apply` reactivates them. The audit log keeps its entries, and a monitoring token the kit generated stays configured (PerchTail has no way to remove one; it can be regenerated from Settings → System). `--files` deletes `shared/` and `agents/` in the logs directory, and only when `gen_logs.py`'s marker file `.perchtail-loadtest` is there; nothing else in that directory is touched. Each object is handled on its own: when one can't be removed or deactivated (or the settings can't be put back), `clean` notes it and carries on with the rest, then lists what it couldn't clean and exits with a non-zero code. What wasn't removed stays in the ledger, so running `clean` again retries it.

The admin account comes from `LOADTEST_ADMIN_USERNAME` and `LOADTEST_ADMIN_PASSWORD` when they are set (an instance in a dedicated test environment), otherwise from `admin-init`. It must be a super admin without MFA.

### Source servers

`compose.sources.yml` adds two containers on the instance's network: `lt-ssh` (OpenSSH, SFTP only) and `lt-smb` (Samba, share `ltlogs`), with one account, `lt-src`, whose random password `seed.py init` writes to `loadtest/.state/sources.env`. Both serve `loadtest/.state/logs/shared` read-only, which the instance also mounts at `/srv/lt-local` for its local-disk sources (`LOCAL_SOURCE_ROOTS`). The SSH server allows more parallel connections than OpenSSH's defaults, so the source isn't the bottleneck. `LOADTEST_LOGS_DIR` moves the logs elsewhere.

To use other servers, set per protocol:

| Variable | Default |
|---|---|
| `LOADTEST_SSH_HOSTS`, `LOADTEST_SSH_PORT`, `LOADTEST_SSH_BASE_PATH`, `LOADTEST_SSH_USERNAME`, `LOADTEST_SSH_PASSWORD` | `lt-ssh`, `22`, `/srv/logs`, the account in `sources.env` |
| `LOADTEST_SMB_HOSTS`, `LOADTEST_SMB_PORT`, `LOADTEST_SMB_BASE_PATH`, `LOADTEST_SMB_USERNAME`, `LOADTEST_SMB_PASSWORD` | `lt-smb`, `445`, `ltlogs`, the account in `sources.env` |
| `LOADTEST_LOCAL_BASE_PATH` | `/srv/lt-local` |
| `LOADTEST_WINRM_HOSTS`, `LOADTEST_WINRM_PORT`, `LOADTEST_WINRM_SCHEME`, `LOADTEST_WINRM_BASE_PATH`, `LOADTEST_WINRM_USERNAME`, `LOADTEST_WINRM_PASSWORD`, `LOADTEST_WINRM_CA_FILE` | no defaults except port `5986` and scheme `https`: required when the profile has WinRM sources |

`LOADTEST_<PROTOCOL>_HOSTS` is a comma-separated list of `host` or `host:port` (`[v6]:port` for IPv6), and a protocol's sources are spread over it round-robin: with `LOADTEST_SSH_HOSTS=ssh-a,ssh-b,ssh-c` and 500 SSH sources, each server gets about 167. The same list can go in the profile instead, as `"hosts": {"ssh": ["ssh-a", "ssh-b"], "winrm": ["win-a:5986"]}`; the variable wins when both are set. A host without a port takes `LOADTEST_<PROTOCOL>_PORT`. `LOADTEST_<PROTOCOL>_HOST`, a single host, still works when neither is set. The other settings (base path, account) are the same on every server of a protocol.

There is no WinRM test server here: WinRM sources need a dedicated test environment. The served tree must exist on those servers: `python loadtest/seed/gen_logs.py generate --profile NAME --out DIR` writes it to any folder (run it on the server, or on a share it serves; with `--out` it needs neither `LOADTEST_BASE_URL` nor the kit's state, and leaves the catalog to the kit's machine), and `grow --out DIR` keeps it growing there.

### Log files

`gen_logs.py generate` writes, for a profile, under `loadtest/.state/logs/` (with the marker file `.perchtail-loadtest`):

- `shared/app-NN/service-<size>k-N.log`: plain logs of every size in `files.sizes_kb`; `app-NN/rotated/*.gz` and `app-NN/bundle-N.zip`;
- `shared/large/large-N.log`: `files.large_mb` each, close to `MAX_FETCH_MB`;
- `shared/live/live-N.log`: files that `grow` appends to at `files.growing_bytes_per_second`, starting over (a rotation, which Follow detects) past `files.growing_rotate_mb`;
- `agents/agent-NNN/`: one folder per agent, with `files.agent_sizes_kb` and one growing file.

Files already at their size are kept, so it is cheap to run again. `catalog.json` in the state folder lists the paths the scenarios use.

### Push agents

`agents.py build` compiles `agent/` (local Go or the Go image) into `loadtest/.state/bin/`. `agents.py start` starts one process per agent source the profile marks running, each with its own token and its own `agents/agent-NNN` folder, writing to `agents/<name>.log` in the instance's state; the agents the profile marks stopped keep their source and token but get no process, like an agent that is installed but down. An agent's environment holds only its own `PERCHTAIL_*` settings and what the OS and Go need (`PATH`, temp and home folders, Windows' system folders, proxy and CA variables), never the kit's passwords. The agents dial `LOADTEST_AGENT_URL`, by default the `ws(s)://` form of `LOADTEST_BASE_URL` plus `/agent/connect`; plain `ws://` is accepted only for an allowed host. `agents.py stop` stops them, after checking that each saved PID still runs the kit's agent binary (a PID reused by another program is left alone).

## Scenarios

Each scenario is one file in `k6/` and reproduces what the web UI does (`frontend/src/lib/api.ts`, `routes/Viewer.svelte`, `lib/follow.ts`, `lib/explorer-refresh.ts`):

- Sign-in is `POST /auth/login`, which sets the session cookie. There is no anti-CSRF token: the cookie is `SameSite=Strict` and the server compares the `Origin` of state-changing requests with `Host`, so the scenarios send the page's `Origin` like a browser.
- A page load reads `/auth/me`, `/system-settings`, `/sources` and `/sources/recent`. The intervals come from `/system-settings`, as in the UI. A source's severity patterns are read once per page, the first time a file of that source is opened.
- Expanding an archive lists all its members at once (`browse?…&all=true`), and an archive is not re-listed on auto-refresh, as in `FolderTree.svelte`.
- While a virtual user reads a file, the background requests of an open page fall due at their real times: every `explorer_refresh_seconds` each expanded folder is listed again (`browse?…&refresh=true`) and the source list is re-read (every 60 s when auto-refresh is off); a followed file is polled every `tail_interval_seconds`, with up to 8 reads per tick while `X-Follow-More` says more is waiting, and reopened when `X-Follow-Reset` reports a rotation.
- Opened files are closed with `POST /close`, which releases their scratch copy. Pauses are random within the profile's ranges.
- When a request answers 401 after signing in (the session expired after 12 hours, or was revoked), the virtual user signs in again, as the login page would, and repeats the request once: the 401 counts as one error, and `session_relogins` in the k6 summary counts the sign-ins again.

| Scenario | What a virtual user does |
|---|---|
| `browse` | Picks a source (SSH, SMB, local, WinRM), expands folders down to a file, opens 1–N files (plain logs up to 10 MB, or `max_open_mb`; `.gz`; archive members), reads each for a while, closes them. Bigger files are `large_files`' job. |
| `follow` | Opens a growing file and follows it for `follow_seconds`, then closes it. |
| `download` | Downloads single files (`/download`), and every `zip_every`-th visit a folder as a ZIP (`/download-zip`). |
| `name_search` | Searches the file-name index (`GET /files`) with plain text, globs and regexes, and sometimes opens a hit. |
| `agents` | The `browse` and `follow` visits over the running push-agent sources only (`follow_share` of visits follow). |
| `background` | No users: the instance's own background work (source checks, file-name index, alerts, scratch sweeps) at the configured intervals, for the base cost per source. |
| `large_files` | Opens files close to `MAX_FETCH_MB` in parallel on remote sources (local-disk files are read in place, without scratch) and keeps them open for `hold_seconds`, for scratch and memory. |
| `ramp` | `browse` visits with users added in steps of `step_vus` every `step_seconds`. Every step runs to the end. Each request is tagged with its step and its stage: `warmup` for the step's 10 s ramp-up and its first `warmup_seconds` (30 s by default, at most half the step), the new users' first sign-ins and connections; `steady` for the rest. The summary judges each step on its steady part only. The breaking point is the first step that ran to the end and either passed a limit, however few requests it completed, or completed fewer than `min_requests` steady requests and fewer than the step before: when a server collapses, requests hang until they time out, so a low count is a symptom of breaking, not a reason to skip the step. A step with too few requests, within the limits and not below the step before, is shown as too few to judge. |

A single scenario can run on its own, with the same environment `run.py` sets: `LOADTEST_BASE_URL`, `LOADTEST_ALLOWED_HOSTS`, `LOADTEST_PROFILE` (a profile file), `LOADTEST_STATE_DIR` (the instance's state folder), and optionally `LOADTEST_DURATION` and `LOADTEST_RESULTS_DIR`:

```bash
LOADTEST_PROFILE=loadtest/profiles/tiny.json LOADTEST_STATE_DIR=loadtest/.state/instances/localhost-8080 \
  LOADTEST_DURATION=2m k6 run loadtest/k6/browse.js
```

### Thresholds

Each scenario has a p95 latency limit and an error-rate limit, set per profile under `scenarios.<name>.thresholds` (`p95_ms`, `error_rate`). Latency leaves out signing in (one argon2 verification per virtual user, reported on its own line), so it measures what the scenario is about. Every HTTP answer outside 2xx counts as an error, including `503` from PerchTail's connector and transfer limits: hitting a limit is degradation. That includes 3xx: k6 counts redirects as success by default and follows them, but PerchTail's API never redirects these requests, so the scenarios don't follow redirects and a 3xx (say, a proxy in front sending the request to a sign-in page) counts as an error instead of a fast answer. The defaults and why:

| Scenario | p95 | Errors | Why |
|---|---|---|---|
| `browse` | 1 s | 1 % | Listing a folder and opening a log are interactive: past about a second a person loses the flow of what they were doing. It covers files up to 10 MB, so p95 measures browsing rather than the transfer of a big file over SSH; bigger ones fall under `large_files`. |
| `follow` | 1 s | 1 % | A poll goes out every 2 s by default and the viewer skips a tick while one is still running, so an answer slower than half the interval delays new lines visibly. |
| `download` | 5 s (`tiny`), 30 s | 1 % | A transfer the user waits for, dominated by size: 5 s for `tiny`'s files of at most 2 MB, 30 s for the bigger profiles' files of up to 500 MB. |
| `name_search` | 500 ms | 1 % | The search box queries as you type (250 ms after the last key): a slower answer makes it lag behind typing. The limit applies to the searches (`GET /files`); opening a hit counts towards errors only. |
| `agents` | 1.5 s | 1 % | `browse` plus one extra hop through the agent's WebSocket and the agent host's disk. |
| `large_files` | 30 s (`tiny`), 120 s | 1 % | This scenario measures scratch and memory, not latency; the limit only flags a transfer slower than about 15 MB/s for an 1.8 GB file. |
| `ramp` | 1 s | 1 % | The same limits as `browse`, over each step's steady part: the breaking point is where browsing stops feeling interactive or starts failing. |

## Profiles

A profile is a JSON file in `profiles/`:

| Key | Meaning |
|---|---|
| `customers`, `folders_per_customer`, `users` | How many of each to seed. |
| `sources` | Sources per protocol: `ssh`, `smb`, `local`, `winrm`. |
| `hosts` | Optional: the servers each protocol's sources are spread over, round-robin (see [Source servers](#source-servers)). |
| `agents` | `running` (a process each) and `stopped` (configured, no process). |
| `files` | `app_folders`, `sizes_kb`, `agent_sizes_kb`, `per_size`, `gz`, `zip`, `large_mb` (below the default `MAX_FETCH_MB`, 2048), `large_count`, `growing`, `growing_bytes_per_second`, `growing_rotate_mb`. |
| `settings` | System settings for the whole run (any key of `PATCH /system-settings`, e.g. `tail_interval_seconds`, `explorer_refresh_seconds`, `source_check_minutes`, `name_index_enabled`, `name_index_minutes`, `concurrency_global`). |
| `scenarios.<name>` | `vus`, the pause ranges (`think_seconds`, `read_seconds`, `hold_seconds`), `files_per_visit`, `follow_seconds`, `zip_every`, `follow_share`, `protocols`, `max_open_mb` (`browse`, `agents`, `ramp`), and `thresholds`; for `ramp`, `start_vus`, `step_vus`, `steps`, `step_seconds`, `warmup_seconds`, `min_requests`. |
| `phases` | In order: `name`, `duration` (k6 syntax), `scenarios` run together, and optional `settings` for that phase only (e.g. the file-name index off). A setting a phase changes goes back to the profile's value, or the instance's, for the phases that don't set it. |

Every profile runs the same five phases: `background` (no users), `mixed` (browse, follow, download, name search and agents together, index on), `mixed-index-off` (the same without the file-name index), `large_files` and `ramp`, with PerchTail's default concurrency limits (16, 4, 2). `large` adds `mixed-limits-2x` and `ramp-limits-2x`, the same with the three limits doubled (32, 8, 4), to show their effect. `tiny` only checks that the kit works; `small`, `medium` and `large` are the sizing profiles, with numbers to be adjusted in a dedicated test environment.

## The collector

`run.py` starts it for each phase; it also runs on its own:

```bash
python loadtest/collect/collect.py --out DIR --container perchtail-perchtail-1 [--seconds 600]
python loadtest/collect/collect.py --out DIR --pid 1234 --scratch-dir /var/lib/perchtail/scratch --db /var/lib/perchtail/db/perchtail.db
```

Every 5 seconds it writes a row to `server.csv`: CPU and memory of the server (`docker stats`, or `/proc/<pid>` for a process; CPU is in percent of one core, so 150 % is one and a half cores busy), bytes used under the scratch directory and the size of the database and its WAL (measured on disk, through `docker exec` for a container), and from `GET /monitoring/health` the database latency, scratch use as PerchTail sees it, connector slots and transfers in use, requests refused by limits, connected agents, the age of the oldest file-name index and failing source checks. Each health report is kept whole in `health.jsonl`, and `hardware.json` describes the machine (and Docker's CPUs and memory). It stops after `--seconds`, or when `DIR/collect.stop` exists. It reads the health report with the monitoring token and never follows a redirect, so the token can't reach another address: a redirect at startup stops it with an error naming where it pointed, and one during the run leaves that sample's health columns as `unreachable`.

## The summary

```bash
python loadtest/report/report.py loadtest/results/<date>-<profile>
```

Writes `summary.md` in the run folder: the profile, PerchTail's version (read from the instance), the date, the hardware and the commit; p50/p95/p99, requests and errors per phase and scenario; CPU, memory, scratch, database and WAL peaks per phase; the ramp's steps with the breaking point; and a short table per scenario (p95, errors, the phase's CPU and memory peaks), the one `docs/sizing.md` takes per profile.

## In a dedicated test environment

- Measure a release tag: `report.py` reads the instance's version from `/monitoring/health`.
- Run the instance on its own machine, and the load (k6, agents, collector if it can reach Docker there, or `--pid` on the instance's host) from another, so k6 doesn't compete with the server for CPU.
- Set `LOADTEST_BASE_URL` and `LOADTEST_ALLOWED_HOSTS` to the test instance, `LOADTEST_ADMIN_USERNAME`/`LOADTEST_ADMIN_PASSWORD` to a super admin, and `LOADTEST_MONITORING_TOKEN` if the instance already has a token.
- Point the sources at the environment's SSH, SMB and WinRM servers with the variables above (a list per protocol), and write the tree on each of them with `gen_logs.py generate --out`.
- Keep `MAX_FETCH_MB` and `SCRATCH_MAX_GB` at their defaults. With 5 GB of scratch, `large`'s `large_files` phase (8 users opening 1.8 GB files) meets the scratch limit: the refusals are part of what it measures.
- Raise PerchTail's limits (`concurrency_global`, `concurrency_per_source`, `concurrency_user_transfers`) only as a measured variant, like `large`'s `-limits-2x` phases: the defaults (16, 4, 2) are what most installs run, and the first thing `ramp` meets.
- Between phases, files that k6 interrupted at the end of a phase stay in scratch until the idle sweep (`SCRATCH_IDLE_SECONDS`, 300 s by default); for exact scratch numbers per phase, run the phases separately with `--phases`.
- For a soak test, make a profile with one long `mixed` phase (for example `"duration": "72h"`): the scenarios, collector and report work the same.
