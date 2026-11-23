# Load tests

Scripts that load a PerchTail instance the way its users do, measure what the server spends doing it, and turn each run into the tables of [docs/sizing.md](../docs/sizing.md). They are meant to be re-run for every release, and the same scenarios drive longer soak tests.

A run seeds the instance through its public API (customers, folders, sources of every protocol, push agents, users with a test role), writes log files for the sources to serve, then runs [k6](https://k6.io) scenarios in phases while a collector records CPU, memory, scratch space and the SQLite database every 5 seconds. Everything the kit creates is named `lt-…` (or another prefix, `LOADTEST_PREFIX`), recorded by id in a ledger, and can be removed again.

## Contents

| Path | What it is |
|---|---|
| `run.py` | Runs a profile end to end: seed, phases, collector, summary. |
| `profiles/*.json` | The profiles: `tiny` (validates the kit), `small`, `medium`, `large`, and `production` with its small version `production-mini`. |
| `seed/seed.py` | Seeds and cleans an instance through the API. |
| `seed/gen_logs.py` | Writes the served log files and keeps some of them growing. |
| `seed/growing.py` | The files of the growing-file scenarios, their line layout and their writer. |
| `seed/production.py` | The plan of the `production` profile: sources, write rates, kinds of people, the day's schedule and the incident. |
| `seed/agents.py` | Builds the push agent from `agent/` and runs one process per agent source. |
| `compose.sources.yml` | SSH and SMB test servers, and the instance settings a local run needs. |
| `sources/` | The images of those two servers. |
| `k6/*.js` | One file per scenario, and `profile.js`, which runs one phase of a profile. |
| `k6/lib/integrity.js`, `k6/lib/zip.js` | The checks of what the server sends of a growing file, and a ZIP reader (with inflate) for the folder ZIPs. |
| `collect/collect.py` | The server-side collector. |
| `collect/source_collect.py`, `collect/source_collect.ps1` | Light collectors for Linux and Windows test servers (see [Measuring the cost of each protocol and feature](#measuring-the-cost-of-each-protocol-and-feature)). |
| `matrix/` | The cost matrix: `matrix.py` runs the measurements, `measure.py` times and costs each window. |
| `report/report.py` | Builds the Markdown summary of a run; `report.py costs` the cost units (`report/costs.py`). |
| `tests/` | Unit tests of the kit's own logic: `python -m unittest discover -s loadtest/tests` (it also runs `node --test loadtest/tests/integrity.test.mjs`, the tests of the k6 checks, when `node` is on the PATH). |

Local files, ignored by git: `loadtest/.state/` (passwords, tokens, generated logs, the agent binary) and `loadtest/results/` (one folder per run).

## Tools and versions

| Tool | Version | Notes |
|---|---|---|
| k6 | 1.4.2 | Native (`k6` on the PATH, exactly this version) or the image `grafana/k6:1.4.2@sha256:3656673de3f30424e8ebcfa46acd9558d83b6a43612d0f668ffeac953950c6c7`, which `run.py` uses when the native one is missing or another version. |
| Docker Engine with Compose | Compose v2 or later | Runs the instance and the SSH/SMB test servers, and k6 and Go when they aren't installed. |
| Python | 3.12 or later | Standard library only: nothing to `pip install`. |
| Node.js | 22 or later, optional | Only for the unit tests of the k6 checks; they are skipped without it. |
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

## The minimal run (profile `tiny`, about 25 minutes)

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

`--phases background,mixed,mixed-index-off,large_files,ramp` leaves out the three growing-file phases (about 12 minutes); `--phases growing_files,growing_heavy,daily_log` runs only those (about 11 minutes). The summary is printed at the end and saved as `loadtest/results/<date>-tiny/summary.md`. Stop the instance with `docker compose -f docker-compose.yml -f loadtest/compose.sources.yml down` (add `-v` to delete its data volume too).

## Running a profile

`run.py --profile NAME` does everything in order:

1. `seed.py apply`, `gen_logs.py generate`, `agents.py build` (once) and `agents.py start`, and `seed.py index` (waits until the file-name index has listed every seeded source);
2. `gen_logs.py grow` in the background for the whole run;
3. each phase of the profile in turn: its System settings applied, the collector started, for a phase with `growing_files`, `growing_heavy` or `daily_log` the writer of their files (`gen_logs.py write`, which starts them afresh first), `k6 run k6/profile.js` with `LOADTEST_PHASE` set, the writer and the collector stopped; 15 seconds between phases;
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

Each phase's folder in `loadtest/results/<date>-<profile>/` holds `k6-<phase>.json` (the k6 summary), `k6.log`, `server.csv`, `health.jsonl` and `hardware.json`, and for a growing-file phase `writer.jsonl` (what the writer did and when: rates, rotations, file sizes every 5 s) and `writer.log`. A k6 exit code of 99 means a threshold failed; for `ramp` that is expected once a step passes the limits, and the summary says which step counts as the breaking point.

`tiny` takes about 25 minutes (about 750 s without the growing-file phases on a laptop), plus a minute or two the first time to build the images and the agent. `small`, `medium` and `large` run 70 minutes of fixed phases plus the ramp (24, 30 and 36 minutes at most), plus 81 minutes of growing-file phases (`growing_files` 15, `growing_heavy` 6, `daily_log` 60), so about 3 to 3.5 hours each.

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
| `production-plan.json` | `production`: the kinds of people and their users, the day's stages, the incident, the alerts. |
| `windows-agents/` | `production` with a Windows test server: what runs the Windows agents there (holds their tokens). |

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

- `lt-customer-NN` customers, each with `lt-cNN-folder-NN` folders (with an invented name after it when the profile gives `customer_names` and `folder_names`); sources are placed round-robin on the customers' roots and folders;
- `lt-ssh-NNN`, `lt-smb-NNN`, `lt-local-NNN`, `lt-winrm-NNN` and `lt-agent-NNN` sources, each with one rule, `include **`;
- the role `lt-role`, with `view` and `download` on every `lt-` customer, and the users `lt-user-NNN` holding it (`production` has one role per kind of person instead, `lt-role-support` and so on, and `lt-alert-NN` alerts). Each user is created with a random temporary password and then signs in and sets its own random password, as PerchTail's forced first-login change requires; the passwords are kept in `users.json`;
- an enrollment token for each agent source;
- a monitoring token, only when the instance has none;
- the profile's System settings. Before the kit first changes a key (in `apply`, or in a phase), it saves the instance's value of that key.

`clean` only acts on ledger objects of the current prefix that still have the kit's exact name (`lt-customer-NN[-name]`, `lt-cNN-folder-NN[-name]`, `lt-(ssh|smb|local|winrm|agent)-NNN`, `lt-role[-kind]`, `lt-user-NNN`, `lt-alert-NN`, with `lt` being the prefix): it deletes the alerts, sources, folders, customers and roles, and puts back only the System settings the kit changed. An object renamed since is left alone and forgotten; a folder or customer that still holds something else is kept (PerchTail refuses to delete it) and stays in the ledger. PerchTail's API can only deactivate users, never delete them (accounts are kept for the audit trail), so the kit's users are moved to the built-in no-access role and deactivated, and stay in the ledger: a later `apply` reactivates them. The audit log keeps its entries, and a monitoring token the kit generated stays configured (PerchTail has no way to remove one; it can be regenerated from Settings → Monitoring). From the instance's state it removes the agents' enrollment tokens (`agent-tokens.json`), the Windows agents' bundle (`windows-agents/`, whose start script holds their tokens), `seeded.json`, `production-plan.json`, and a restore copy a `protocol_matrix` run left in the PerchTail container (`restore-copy.json`). `--files` deletes `shared/` and `agents/` in the logs directory, and only when `gen_logs.py`'s marker file `.perchtail-loadtest` is there; nothing else in that directory is touched. Each object is handled on its own: when one can't be removed or deactivated (or the settings can't be put back), `clean` notes it and carries on with the rest, then lists what it couldn't clean and exits with a non-zero code. What wasn't removed stays in the ledger, so running `clean` again retries it.

The admin account comes from `LOADTEST_ADMIN_USERNAME` and `LOADTEST_ADMIN_PASSWORD` when they are set (an instance in a dedicated test environment), otherwise from `admin-init`. It must be a super admin without MFA.

### Source servers

`compose.sources.yml` adds two containers on the instance's network: `lt-ssh` (OpenSSH, SFTP only) and `lt-smb` (Samba, share `ltlogs`), and with `--profile fault` a third, `lt-ssh-fault`, for the cost matrix's reliability step, with one account, `lt-src`, whose random password `seed.py init` writes to `loadtest/.state/sources.env`. Both serve `loadtest/.state/logs/shared` read-only, which the instance also mounts at `/srv/lt-local` for its local-disk sources (`LOCAL_SOURCE_ROOTS`). The SSH server allows more parallel connections than OpenSSH's defaults, so the source isn't the bottleneck, and keeps its host keys in a volume of its own (`lt-ssh-keys`), so rebuilding or recreating the container doesn't change the key PerchTail has pinned (with a new key, PerchTail refuses to connect). `LOADTEST_LOGS_DIR` moves the logs elsewhere.

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
- `agents/agent-NNN/`: one folder per agent, with `files.agent_sizes_kb` and one growing file;
- `shared/growing/growing-N.log` (and in the first `files.growing_agents` running agents' folders): `files.growing_count` files of `files.growing_size_mb`, for `growing_files`;
- `shared/heavy/heavy-N.log`: `files.heavy_count` files of `files.heavy_start_mb`, for `growing_heavy`;
- `shared/daily/access.log`: an empty file that `daily_log` grows to `files.daily_size_gb`.

The last three are written by `seed/growing.py` with lines that can be checked (see [Growing files](#growing-files)), and the writer of a phase starts them afresh at its size before the phase begins. They need room: up to twice each growing file's size plus what it gains before it rotates (the `.1` copy), twice `files.heavy_rotate_mb` per heavy file, and the whole daily file: about 0.6 GiB for `tiny`; with the defaults, about 0.25 GiB per growing file and tree (50 MB plus 75 MB of growth, twice), 1.5 GiB for `heavy/` and 3 GiB for `daily/`, so about 7.4, 10.4 and 16.2 GiB for `small`, `medium` and `large`. `generate` prints how much and refuses to start when the disk of `LOADTEST_LOGS_DIR` has less free space; point `LOADTEST_LOGS_DIR` at a disk with room. `clean --files` deletes them with the rest.

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
| `growing_files`, `growing_heavy`, `daily_log` | See [Growing files](#growing-files). |
| `ramp` | `browse` visits with users added in steps of `step_vus` every `step_seconds`. Every step runs to the end. Each request is tagged with its step and its stage: `warmup` for the step's 10 s ramp-up and its first `warmup_seconds` (30 s by default, at most half the step), the new users' first sign-ins and connections; `steady` for the rest. The summary judges each step on its steady part only. The breaking point is the first step that ran to the end and either passed a limit, however few requests it completed, or completed fewer than `min_requests` steady requests and fewer than the step before: when a server collapses, requests hang until they time out, so a low count is a symptom of breaking, not a reason to skip the step. A step with too few requests, within the limits and not below the step before, is shown as too few to judge. |

### Growing files

`growing_files`, `growing_heavy` and `daily_log` work on files that are being written while users open, download and follow them, the usual state of a log and the case where a server can send a file that changes under it. Every line of these files is 128 bytes and says where it belongs: its generation (1, plus one at each rotation), its sequence number in that generation (from 1, without gaps), when it was written, the writer's rate, and a filler letter that depends on the sequence number:

```
G00001 S0000000001 T1763150000000 R000262144 INFO  growing-1            bbbb…b
```

So the kit can check every answer, in `k6/lib/integrity.js`:

- `open` and `download` must be the file from its first line, every line in sequence with none missing or repeated, all lines whole except possibly the last (the server read the file while a line was being written), never shorter than the `Content-Length` the server announced, and not cut off (a transfer aborted half-way, or a 2xx whose body can't be read to the end, counts as an integrity error; k6 counts it as a failed request too);
- each Follow read must carry on exactly where the last one stopped, including a line cut in two between reads, and the offset the server returns must move by exactly the bytes sent; after `X-Follow-Reset` (a rotation) the reloaded file must be a later generation, again from its first line. A tab is opened as the viewer opens it: a file the explorer lists above 200 MB (the viewer's `LARGE_FILE_BYTES`) isn't opened whole but from its end, as the viewer's "Tail the end" does (a Follow read of its last 1 MB, the part line before the first newline dropped, and after a reset the end of the new file again), and the lines from there on must be in sequence; a file below 200 MB that the server refuses cleanly as over `MAX_FETCH_MB` (413 `fetch_too_large`) can't be followed in the viewer either, so it counts in `refused_too_large`, not as an error, and the user waits a minute before trying again;
- each member of a folder ZIP (`download-zip`) that is one of these files, or its rotated `.1`, must pass the same checks as a whole file, its size and CRC-32 must match the ZIP's directory, and the ZIP must not have skipped any file (`SKIPPED.txt`). k6 has no inflate, so `k6/lib/zip.js` implements one; checking a ZIP costs k6 about one second of CPU per 15 MB of content.

Any failed check adds one to `integrity_errors` (tagged with the scenario, operation and protocol) and logs a line starting with `INTEGRITY` with the file, the protocol, the operation and the first deviation (the offset in the file, the line expected and what was found); the summary lists them. Requests are tagged `flow:growing`, `suite` (the scenario), `gop` (the operation) and `protocol`, so they never count in `browse`'s or `follow`'s latency.

- `growing_files`: `vus` users open a growing file whole, download one every `download_every` visits and the `growing/` folder as a ZIP every `zip_every` visits; `follow_vus` users follow a file for `follow_seconds`, long enough to see it rotate. Each takes the protocols in turn (SSH, SMB, local disk, WinRM when configured, and the first `files.growing_agents` agents). The writer appends to every file at `files.growing_rate_kb_s` (256 KB/s by default) and rotates it every `files.growing_rotate_s` (300 s): it renames the file to `.1` and starts a new one that grows again. On Windows a reader can hold the file open and block the rename; the writer then retries for 10 s and falls back to copying the file to `.1` and truncating it (`writer.jsonl` records which).
- `growing_heavy`: `files.heavy_count` files, each written at every rate of `files.heavy_rates_mb_s` in turn (1, 5 and 10 MB/s by default) for `files.heavy_step_seconds` (120 s) each, and rotated past `files.heavy_rotate_mb`; `followers_per_file` users follow each file for the whole phase. After each Follow tick the kit records how far behind the writer the viewer is, in seconds (now minus the time stamp of the last line received) and in bytes (the size the server reports minus what the viewer has), tagged with the step's rate and the half of the step. The summary gives, per rate, the median delay of each half, the server's CPU, memory and scratch in that step, and the first rate at which the delay grows within the step instead of staying level. The viewer reads at most 8 × 1 MB per tick (`RANGE_MAX_BYTES`), so with the default 2 s interval Follow can't take in more than about 4 MB/s per file whatever the server's capacity.
- `daily_log`: one file grows to `files.daily_size_gb` (3 GB) over a day compressed into `files.daily_duration_min` (60 minutes, so 2.5 minutes per hour), with a pattern per hour: low at night, rising in the morning, `files.daily_business_share` (70 %) of the volume between 09:00 and 18:00 with two peaks at `files.daily_peak_factor` (5) times the mean of those hours (several MB/s with the defaults), and falling in the evening. `follow_vus` users follow it all day; `vus` users list its folder and open or download it whole every `every_seconds`. Below `MAX_FETCH_MB` a whole read must pass the checks above; above it, the server must refuse it cleanly with 413 and `error_code: fetch_too_large` (anything else, a 5xx included, is an error), which `fetch_refusals` counts. Follow keeps going past both limits: once the file is listed above 200 MB the followers read it from its end, as the viewer does. The summary gives, per hour, the write rate, the file's size, Follow's delay, and the server's CPU, memory and scratch. With the defaults the file passes the default `MAX_FETCH_MB` (2048) in the afternoon; to see the refusals in `tiny`'s 200 MB day, start the test instance with `MAX_FETCH_MB=100`.

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
| `growing_files` | `open` 15 s, `download` 15 s, `download-zip` 60 s, `follow` 2 s; no integrity error | 1 % | A 50 MB file is a transfer the user waits for; 15 s is about 3.5 MB/s, well within a LAN. A ZIP of the folder holds several such files. A Follow read is at most 1 MB. Any integrity error fails the scenario: a viewer showing a file with lines missing or repeated is wrong, however fast. |
| `growing_heavy` | `open` 15 s, `follow` 5 s; no integrity error | 1 % | The reads are 1 MB each at high rates; the delay behind the writer, not the latency, is what this scenario measures, and it has no limit: the summary says from which rate it grows. |
| `daily_log` | `open` and `download` 60 s, `follow` 5 s; no integrity error | 1 % | Whole reads of a file of up to `MAX_FETCH_MB` (2 GB by default). A clean refusal (413 `fetch_too_large`) is a correct answer, not an error. |

These limits are per scenario under `scenarios.<name>.thresholds`: `open_p95_ms`, `download_p95_ms`, `zip_p95_ms`, `follow_p95_ms` and `error_rate`.

## Profiles

A profile is a JSON file in `profiles/`:

| Key | Meaning |
|---|---|
| `customers`, `folders_per_customer`, `users` | How many of each to seed. |
| `sources` | Sources per protocol: `ssh`, `smb`, `local`, `winrm`. |
| `hosts` | Optional: the servers each protocol's sources are spread over, round-robin (see [Source servers](#source-servers)). |
| `agents` | `running` (a process each) and `stopped` (configured, no process). |
| `files` | `app_folders`, `sizes_kb`, `agent_sizes_kb`, `per_size`, `gz`, `zip`, `large_mb` (below the default `MAX_FETCH_MB`, 2048), `large_count`, `growing`, `growing_bytes_per_second`, `growing_rotate_mb`; for the growing-file scenarios `growing_count`, `growing_size_mb`, `growing_rate_kb_s`, `growing_rotate_s`, `growing_agents`, `heavy_count`, `heavy_start_mb`, `heavy_rates_mb_s`, `heavy_step_seconds`, `heavy_rotate_mb`, `daily_size_gb`, `daily_duration_min`, `daily_business_share`, `daily_peak_factor` (defaults in `seed/growing.py`). |
| `settings` | System settings for the whole run (any key of `PATCH /system-settings`, e.g. `tail_interval_seconds`, `explorer_refresh_seconds`, `source_check_minutes`, `name_index_enabled`, `name_index_minutes`, `concurrency_global`). |
| `scenarios.<name>` | `vus`, the pause ranges (`think_seconds`, `read_seconds`, `hold_seconds`), `files_per_visit`, `follow_seconds`, `zip_every`, `follow_share`, `protocols`, `max_open_mb` (`browse`, `agents`, `ramp`), and `thresholds`; for `ramp`, `start_vus`, `step_vus`, `steps`, `step_seconds`, `warmup_seconds`, `min_requests`; for `growing_files` and `daily_log`, `follow_vus` (and `download_every`, or `every_seconds`); for `growing_heavy`, `followers_per_file`. |
| `customer_names`, `folder_names` | Optional: invented names put after the kit's customer and folder names. |
| `production` | The `production` profile's plan: `peak_users`, `start_users`, `end_users`, `ramp_up_minutes`, `peak_minutes`, `ramp_down_minutes`, `incident` (`minutes`, `users`, `zip_users`, `at_minutes`), `alerts`, `rate_classes`, `tree`, `personas` (per kind: `weight`, `session_minutes`, `think_seconds`, `read_seconds`, `files_per_visit`, `follow_files`), `day_minutes`. With it, `agents.windows` is how many of the running agents are Windows agents. |
| `phases` | In order: `name`, `duration` (k6 syntax), `scenarios` run together, and optional `settings` for that phase only (e.g. the file-name index off). A setting a phase changes goes back to the profile's value, or the instance's, for the phases that don't set it. |

Every profile runs the same eight phases: `background` (no users), `mixed` (browse, follow, download, name search and agents together, index on), `mixed-index-off` (the same without the file-name index), `large_files`, `ramp`, `growing_files`, `growing_heavy` and `daily_log`, with PerchTail's default concurrency limits (16, 4, 2). `tiny` makes the growing-file phases fit in a few minutes and little disk: 2 files of 5 MB rotating every minute, `growing_heavy` at 1 MB/s for one minute, and a day of 200 MB in 5 minutes. `small`, `medium` and `large` have 4, 8 and 16 growing files of 50 MB, and the two variants with their defaults. `large` adds `mixed-limits-2x` and `ramp-limits-2x`, the same with the three limits doubled (32, 8, 4), to show their effect. `tiny` only checks that the kit works; `small`, `medium` and `large` are the sizing profiles, with numbers to be adjusted in a dedicated test environment.

## The production profile

`production` runs PerchTail as an installation in use, rather than one kind of load at a time: many sources of every protocol, each with its own logs written at its own rate, and people of different kinds signing in, working and signing out through a working day, with the background work on and an incident in the middle. `production-mini` is the same at the scale of a development machine (10 sources, 5 users, 10 minutes), to check the profile works before a long run:

```bash
python loadtest/run.py --profile production-mini --clean
```

What it sets up (from `seed/production.py`, deterministic for a given profile):

- **Sources:** 170 by default, as an installation with 90 Linux and 80 Windows hosts: 60 SSH and 30 push agents (Linux), 40 SMB, 30 WinRM and 10 Windows push agents. Each source reads its own tree (`prod/<source>/` under the protocol's base path, or the agent's own folder): three `app-N/` folders with a growing `current.log` (in `app-1/`), rotated logs, a `.gz` and a `.zip`, and on every tenth source a big file (`tree.large_mb`, 200 MB). Every file is written with the checkable lines of [Growing files](#growing-files), so every open and download is checked. The sources are spread round-robin over the test servers of each protocol (see [Source servers](#source-servers)).
- **Write rates:** each source's `current.log` grows at the rate of its class, spread over the sources in a seeded order so every protocol gets its share: 70 % calm (2 to 8 KB/s), 25 % with movement (100 to 300 KB/s), both rotated past 64 MB, and 5 % intense, which follow `daily_log`'s pattern (3 GB over a day as long as the run, `production.day_minutes` to change it; rotated at the end of each day). A small profile still gets one source of each class. The writer (`gen_logs.py write`) runs for the whole phase.
- **Organisation:** 5 customers with invented names (`lt-customer-01-brightwater-retail` and so on) with 3 folders each (`production`, `staging`, `shared-services`), the sources placed round-robin.
- **People:** 40 users, each with the role of their kind; nobody but the investigators and the administrators sees everything:

  | Kind | Share | Role (grants) | A session |
  |---|---:|---|---|
  | support | 40 % | `view` on customers 1–3 | browses, opens files and archive members, reads them (Find and Find all work in the browser, without requests, so reading time stands for them) |
  | oncall | 25 % | `view` on each customer's first folder | follows 2 to 5 growing logs for the whole session (40 to 120 minutes) |
  | investigation | 20 % | `view` and `download` on every customer | searches file names, downloads files and folders as ZIPs, opens big files |
  | admin | 10 % | user, role, System settings, audit and `create_source` capabilities, `manage_source` and `manage_rules` | System settings, sources, rules, roles, users and the audit log; it saves settings with their current values, a source with its own name, adds and removes a rule that matches nothing, saves the kit's `occasional` role with its own name and sets a kit user's display name, so it changes nothing that matters |
  | occasional | 5 % | `view` on customers 4–5 | signs in, opens a file, signs out |

  Each session is one k6 iteration: a kind chosen by weight, a user of that kind, sign-in, work with the kind's pauses for a session length drawn from its range, sign-out. The number of people signed in follows the day: a few at the start, a rise through the morning in three steps (faster in the middle), the peak (`peak_users`, 30, for `peak_minutes`, 60), and a fall towards `end_users`. People keep arriving and leaving all along: a session that ends is followed by another person's.
- **Incident:** in the middle of the peak (or at `incident.at_minutes`), `incident.users` (20) people open and follow the same intense log for `incident.minutes` (10), and `incident.zip_users` (4) of them download its folder as a ZIP again and again.
- **Background:** the file-name index and the source checks at their usual intervals, a scheduled backup during the run (`backup_interval_hours: 1`, put back afterwards), the audit log, and `production.alerts` (10) alerts on different sources. The alerts watch each source's `current.log` for a word that never appears, so every pass reads what was appended and nothing is ever sent. PerchTail only accepts a webhook whose host resolves to public addresses, checked when the alert is created, so the kit has no default: set `LOADTEST_ALERT_WEBHOOK_URL` to a URL of your choice (it is never called; only the server's DNS lookup at creation goes outside). Without it, the run has no alerts and the summary says so.
- **Duration:** 2 hours by default (`ramp_up_minutes`, `peak_minutes`, `ramp_down_minutes`); for a soak test of several days, raise `peak_minutes`.

The summary's production section has: latency and errors per kind of person and operation (`gop`: login, page, browse, open, open-large, follow, search, download, download-zip, admin, close, logout, and background for the polling of an open page), the incident's own rows, sessions per kind, integrity errors, and over time (every minute, 5 or 10 minutes depending on the length, also written as `timeseries.csv` for charts) the planned number of people, CPU, memory, scratch, database and WAL, audit-log rows, connector slots in use and requests refused by limits; then memory's trend per hour (a least-squares slope after the first tenth of the run, and its share of the median) and a verdict: within the limits, or where not. The limits are p95 per operation (`page`, `browse`, `follow`, `admin` 1 s; `search` 500 ms; `open` 2 s; `open-large` 30 s; `download` 15 s; `download-zip` 60 s; `scenarios.production.thresholds.ops` changes them), errors under 1 % for the sessions and for the incident, no integrity error, memory growing by no more than 5 % of its median per hour over a run of at least an hour, and no request refused by PerchTail's limits.

### Setting up 100 or more sources with a few test servers

A source is a configuration on PerchTail, not a machine: many sources can point at one test server, each reading its own folder. With `LOADTEST_SSH_HOSTS=ssh-a,ssh-b,ssh-c`, the 60 SSH sources go 20 to each server, all with the same account and base path, each in `prod/<source>/`. So:

1. Give each protocol a few servers (three SSH, two SMB, two WinRM, say), with the account and base path of [Source servers](#source-servers). On the SSH servers, allow enough sessions for 20 to 60 sources and the incident (`MaxSessions` and `MaxStartups`; the kit's test server raises them).
2. Write the trees on each server: copy `loadtest/` there (Python 3.12, standard library) and run `python loadtest/seed/gen_logs.py generate --profile production --out <base path> --protocols <the protocols it serves>` (`--protocols ssh,smb` on a server that serves SSH and SMB, `--protocols winrm` on a WinRM server). Every server gets the tree of every source of those protocols, which is simpler than working out which sources land where and costs only disk. Without `--protocols`, it gets the trees of every SSH, SMB, local and WinRM source.
3. During the run, keep them growing on each server: `python loadtest/seed/gen_logs.py write --profile production --phase production --out <base path> --protocols <the same protocols> --stop-file <file>`, started before the run and stopped after it (create the stop file). Run the writer on the server, not on a share mounted from elsewhere: the source has to read a file being written, as with a real log.
4. Push agents run on the load machine (`agents.py start`, 30 processes of about 20 MB each). For the 10 Windows agents, set `LOADTEST_WINDOWS_AGENT_HOSTS` to the Windows test server: `run.py` then writes `windows-agents/` in the instance's state (`agents.py windows-bundle`: the Windows build of the agent, `start-agents.ps1` and `stop-agents.ps1`) and waits up to `LOADTEST_WINDOWS_AGENT_WAIT` seconds (900) for them to connect. On the Windows server, write their folders with `gen_logs.py generate --profile production --only-windows-agents --agents-dir C:\perchtail-loadtest\agents` and keep them growing with the same options on `write`, then run `start-agents.ps1`. The script holds the agents' tokens: copy the folder over a channel you trust and delete it afterwards (`seed.py clean` deletes it from the instance's state). Without a Windows test server the 10 agents run on the load machine like the others, and the summary says on which OS they ran.

What it needs:

- **Disk on each test server:** the trees of the protocols it serves. `generate` prints the upper bound for the protocols chosen with `--protocols` (every source's current.log at its largest, twice for the rotated copy) and refuses when there is less free space; a 2-hour run takes about half of it, without the rotated copies (the 3 GB intense logs take most of it). The upper bounds of the `production` profile on a test server, by protocol: SSH 26.9 GiB, SMB 17.9 GiB (so 44.8 GiB for a server with both), WinRM 16.4 GiB. `production-mini` needs about 1.6 GiB.
- **Writes on each test server:** about 13 MB/s on average with every source's tree on every server (less with `--protocols`), more at the intense logs' peaks; the servers' disks must take that and the reads, or the run measures them.
- **The load machine:** k6 with up to 34 virtual users holding answers of up to 200 MB while it checks them (plan 4 GB of memory and 2 cores for k6), plus the agents. The intense logs pass 200 MB in the morning of their day, and from then on Follow (on-call sessions and the incident) reads only their end, 1 MB at a time, as the viewer does, so they add little to that. The incident's ZIPs are the exception: its `zip_users` (4) download the intense log's folder every 30 seconds, which holds about 1.1 GB by midday, and k6 holds and checks each ZIP whole (about 75 seconds of CPU each), so plan about 6 GB of memory and 4 cores for k6 during the incident, or lower `incident.zip_users`; otherwise k6, not PerchTail, becomes the incident's bottleneck. Run it on a machine other than the instance's.
- **The instance:** the scheduled backup leaves its files in `BACKUP_DIR` (PerchTail keeps the last `BACKUP_KEEP`), and the run adds audit rows; `clean` removes neither.

## Measuring the cost of each protocol and feature

`protocol_matrix` measures what each protocol and each feature costs, one thing at a time, so they can be compared: PerchTail's CPU time, memory and network per operation, the source's, and from those, cost units per MB transferred, per source and per active user. A phase whose scenario is `protocol_matrix` runs `matrix/matrix.py` instead of k6, after the usual seeding; `protocol_matrix-mini` checks it on a development machine (one 1 MB file, one folder of 100 entries, 1 and 5 at once, no added delay; about 15 minutes):

```bash
export LOADTEST_SOURCE_CONTAINERS=perchtail-lt-ssh-1,perchtail-lt-smb-1
python loadtest/run.py --profile protocol_matrix-mini --clean
python loadtest/report/report.py costs loadtest/results/<date>-protocol_matrix-mini
```

The steps, each writing one row per measured cell to `measurements.csv` in the phase's folder (the same columns for every step: protocol, operation, size or entries, parallelism, network delay, then the metrics; see `matrix/measure.py`), and each request to `requests.csv`:

| Step | What it measures | Needs |
|---|---|---|
| `matrix` | On one source of each protocol the instance has (SSH, SMB, WinRM, WinRM through JEA, push agents by the OS they report, local disk): listing folders of 100, 1 000 and 10 000 entries (as the explorer's refresh does; PerchTail returns at most `listing_max_entries`), opening and downloading files of 1, 10, 50 and 200 MB, and following a file growing at `matrix.follow_rate_kb_s` for `matrix.follow_seconds` (5 minutes); one at a time, then 5 and 20 at once, each worker signed in as a user of its own (`users` in the profile is at least the largest number at once) and with PerchTail's three concurrency limits raised for the step and put back after, so the cells measure the protocols rather than those limits (`reliability` measures capacity). Per cell: latency, time to the first byte, MB/s, errors, PerchTail's CPU time, peak memory and bytes and packets on its interface, the source's CPU time, memory and bytes, and the audit rows written. First, 30 s with nothing to do, for the agents' own footprint. | Docker access to the PerchTail container, or `--pid` on Linux (without network counters); `LOADTEST_SOURCE_CONTAINERS` for the source servers' counters when they are containers on the same Docker. |
| network delay | The matrix again with 1, 20 and 80 ms added to everything the source servers send (`matrix.delays_ms`), only with `LOADTEST_NETEM=1`. | `tc` in the source containers (the kit's test server images have it) run through `docker exec --privileged`, and only in containers with the label `perchtail.loadtest=test-source`, which `compose.sources.yml` gives the kit's test servers: any other container in `LOADTEST_SOURCE_CONTAINERS` (PerchTail's own, say) is refused before anything runs; without `LOADTEST_NETEM=1` nothing runs in them at all. On other servers, add and remove the delay yourself with `tc qdisc add dev eth0 root netem delay 20ms` and run the matrix once per delay. |
| `fixed` | With nobody signed in, a source check and a file-name index pass on one source of each protocol, with the number of files indexed (cost per 10 000 files). | |
| `features` | The same small load with and without the audit page; sign-in with and without MFA (TOTP, enabled on one kit user for the step and disabled after; sign-ins 31 s apart, as a code is accepted once); and with each setting it looks for on and off: redaction and audited listings, when this version has them (the row says when it doesn't). | |
| `operation` | A backup (`python -m app.backup create` in the container) and its size; with `LOADTEST_RESTORE_TEST=1`, a restore of it into a `DATA_DIR` of its own that the kit makes in the container (`mktemp`, under `/tmp`) and records in the instance's state (`restore-copy.json`) before anything is written there, with the database, the credential salt, the SSH host keys and the restore's working folder all set inside it, so the live database is never touched whatever `DATABASE_URL`, `CREDENTIAL_SALT_PATH` or `SSH_KNOWN_HOSTS_PATH` the container has; the copy is removed afterwards, or by `seed.py clean` if a run stopped half-way. Without the option the restore is skipped and the summary's row says why. With `LOADTEST_MIGRATION_FROM=<revision>`, an Alembic upgrade to head from that revision, on a copy of the database. | Docker access to the PerchTail container. The backup stays in `BACKUP_DIR`. |
| `storms` | With `LOADTEST_ALLOW_RESTART=1`: PerchTail restarted with every push agent connected (100 in `protocol_matrix`), the time until it answers `/readyz` (its start-up time) and until every agent is back; then 30 sign-ins at once (argon2). | Leave nothing else on the instance: it restarts. |
| `reliability` | With `LOADTEST_RELIABILITY=1`: the other sources used together with a source on a server of its own, first slow (2 s added), then stopped, then started again: the effect on the others, the errors and the status PerchTail shows, and how long until a check passes again. Then how many parallel sessions one source of each protocol takes before more than 5 % fail, with PerchTail's concurrency limits raised for the step and put back after. | The fault server: `docker compose -f docker-compose.yml -f loadtest/compose.sources.yml --profile fault up -d` adds `lt-ssh-fault`; `protocol_matrix` spreads its SSH sources over `lt-ssh` and `lt-ssh-fault`. Set `LOADTEST_FAULT_CONTAINER` to its container's name; the step refuses a container without the label `perchtail.loadtest=test-source`, so it never slows down or stops anything but the kit's own fault server. |

On test servers that aren't containers here, run a source collector during the run and hand its file to `report.py costs --source-csv`: `collect/source_collect.py` on Linux (CPU, memory and disk I/O of `sshd`, `smbd` and the agent from `/proc`; reading another user's `/proc/<pid>/io` needs root) and `collect/source_collect.ps1` on Windows (`wsmprovhost`, the SMB server, which runs in the `System` process, and the agent; run it as an administrator to read `System`). Each cell has its start and end in `measurements.csv`, so every operation is charged with what it cost on the server over its window; keep the clocks in sync (NTP). For WinRM through JEA, set `LOADTEST_WINRM_JEA_CONFIGURATION` to the endpoint's name (`docs/examples/jea`): `protocol_matrix`'s second WinRM source uses it.

`report.py costs RUN [--production PRODUCTION_RUN] [--source-csv FILE ...]` writes `costs.csv` and `costs.md` in the run's folder, one row per unit (unit, protocol, kind of person, operation, metric, value, and how it was worked out):

- per operation: PerchTail's CPU time, the source's CPU time, bytes on PerchTail's interface and packets per request, for each protocol, operation, size and parallelism (no added delay);
- per MB transferred: PerchTail's and the source's CPU time and the bytes that crossed the wire, per MB of file, by protocol;
- per source: a check and an index pass, per 10 000 files, and per hour at the instance's intervals;
- per active user, from a `production` run: PerchTail's CPU time, network, database and audit-log growth per signed-in user per hour, memory per user at the peak, and per kind of person its requests per hour of session by operation, with the matrix's CPU time per request where an operation has a counterpart there.

It writes numbers and how each was worked out, nothing more.

How long it takes: `protocol_matrix-mini` about 15 minutes. `protocol_matrix` without delays about 20 minutes per protocol (the three 5-minute Follow cells take most of it), so about 2.5 hours for seven; each added delay adds as much again; `fixed` a few minutes, `features` about 5, `operation` depends on the database's size, `storms` a few minutes, `reliability` about 10 plus the capacity ramp.

## The collector

`run.py` starts it for each phase; it also runs on its own:

```bash
python loadtest/collect/collect.py --out DIR --container perchtail-perchtail-1 [--seconds 600]
python loadtest/collect/collect.py --out DIR --pid 1234 --scratch-dir /var/lib/perchtail/scratch --db /var/lib/perchtail/db/perchtail.db
```

Every 5 seconds it writes a row to `server.csv`: CPU, memory and bytes in and out of the server (`docker stats`, or `/proc/<pid>` for a process, without the network; CPU is in percent of one core, so 150 % is one and a half cores busy), bytes used under the scratch directory, the size of the database and its WAL and the number of audit-log rows (measured on disk and read-only, through `docker exec` for a container), and from `GET /monitoring/health` the database latency, scratch use as PerchTail sees it, connector slots and transfers in use, requests refused by limits, connected agents, the age of the oldest file-name index and failing source checks. Each health report is kept whole in `health.jsonl`, and `hardware.json` describes the machine (and Docker's CPUs and memory). It stops after `--seconds`, or when `DIR/collect.stop` exists. It reads the health report with the monitoring token and never follows a redirect, so the token can't reach another address: a redirect at startup stops it with an error naming where it pointed, and one during the run leaves that sample's health columns as `unreachable`.

## The summary

```bash
python loadtest/report/report.py loadtest/results/<date>-<profile>
```

Writes `summary.md` in the run folder: the profile, PerchTail's version (read from the instance), the date, the hardware and the commit; p50/p95/p99, requests and errors per phase and scenario; CPU, memory, scratch, database and WAL peaks per phase; the ramp's steps with the breaking point; for the growing-file scenarios a table per protocol and operation (requests, p50/p95, HTTP errors, integrity errors, MB transferred) with the first integrity errors, Follow's delay per write rate with the rate from which it stops keeping up, and per hour of `daily_log`'s day; and a short table per scenario (p95, errors, the phase's CPU and memory peaks), the one `docs/sizing.md` takes per profile.

## In a dedicated test environment

- Measure a release tag: `report.py` reads the instance's version from `/monitoring/health`.
- Run the instance on its own machine, and the load (k6, agents, collector if it can reach Docker there, or `--pid` on the instance's host) from another, so k6 doesn't compete with the server for CPU.
- Set `LOADTEST_BASE_URL` and `LOADTEST_ALLOWED_HOSTS` to the test instance, `LOADTEST_ADMIN_USERNAME`/`LOADTEST_ADMIN_PASSWORD` to a super admin, and `LOADTEST_MONITORING_TOKEN` if the instance already has a token.
- Point the sources at the environment's SSH, SMB and WinRM servers with the variables above (a list per protocol), and write the tree on each of them with `gen_logs.py generate --out`.
- Keep `MAX_FETCH_MB` and `SCRATCH_MAX_GB` at their defaults. With 5 GB of scratch, `large`'s `large_files` phase (8 users opening 1.8 GB files) meets the scratch limit: the refusals are part of what it measures.
- Raise PerchTail's limits (`concurrency_global`, `concurrency_per_source`, `concurrency_user_transfers`) only as a measured variant, like `large`'s `-limits-2x` phases: the defaults (16, 4, 2) are what most installs run, and the first thing `ramp` meets.
- Between phases, files that k6 interrupted at the end of a phase stay in scratch until the idle sweep (`SCRATCH_IDLE_SECONDS`, 300 s by default); for exact scratch numbers per phase, run the phases separately with `--phases`.
- Run the growing-file phases with the logs on the test servers' own disks (`gen_logs.py generate --out` and `write --out` there), not on a share mounted from the load machine: the writer and the source must see the same file, as with a real log.
- `growing_heavy`'s 10 MB/s per file over three files is 30 MB/s of writes on the source's disk, and as many reads: check the test servers' disks can take it, or the result measures them.
- For a soak test, make a profile with one long `mixed` phase (for example `"duration": "72h"`): the scenarios, collector and report work the same.
