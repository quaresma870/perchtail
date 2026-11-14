# Sizing guide

How much hardware a PerchTail instance needs for a number of users and sources, from load tests rather than estimates. Every number here was measured with the load-test kit in [`loadtest/`](../loadtest/README.md), on the release named next to it; re-run the kit to check a figure for another release or other hardware.

PerchTail runs as a single process with a SQLite database (see [high-availability.md](high-availability.md)), so sizing means one machine: CPU, memory, and two disks worth thinking about, the database's and scratch. Every measurement here is of one instance with its SQLite database, with PerchTail's default limits (`MAX_FETCH_MB`, `SCRATCH_MAX_GB` and the three concurrency limits) unless a row says otherwise.

## Profiles

| Profile | Users (concurrent) | Sources | Push agents | CPU | RAM | Scratch disk | Database disk | Measured on |
|---|---|---|---|---|---|---|---|---|
| Small | up to 10 | about 50 (SSH, SMB, WinRM, local) | about 12 | to be measured | to be measured | to be measured | to be measured | to be measured |
| Medium | up to 50 | about 250 | about 45 | to be measured | to be measured | to be measured | to be measured | to be measured |
| Large | up to 200 | about 1000 | about 160 | to be measured | to be measured | to be measured | to be measured | to be measured |

The exact mix of each profile (sources per protocol, file sizes, how long people read, follow and download) is in `loadtest/profiles/small.json`, `medium.json` and `large.json`. A deployment that differs a lot from them (much bigger files, many more people following at once, sources far away) should read the next sections and adjust.

## Measured results

Each profile's section names the release and hardware it was measured on, and has one short table per scenario, from the "For docs/sizing.md" table of the kit's summary: p95 latency, error rate, and the CPU and memory peaks of the phase the scenario ran in. Then the ramp's breaking point: the number of concurrent browsing users where p95 latency passes 1 s or errors pass 1 %, judged on each step once its new users have settled. The full tables (p50 and p99, scratch, database and WAL, connector slots, each ramp step) stay in each run's `summary.md`.

### Small

To be measured.

| Phase | Scenario | p95 (ms) | Errors | CPU peak | Memory peak (MiB) |
|---|---|---:|---:|---:|---:|
| mixed | browse | – | – | – | – |

Breaking point: to be measured.

### Medium

To be measured, in the same form as Small.

### Large

To be measured, in the same form as Small, plus the `mixed-limits-2x` and `ramp-limits-2x` phases: the same load with the three concurrency limits doubled (32, 8, 4), next to the default ones, to show what raising them buys.

## What drives each resource

### CPU

Most CPU goes to talking to sources: SSH encryption and SFTP framing, SMB signing, WinRM's XML and HTTP, decompressing `.gz` files and archive members, and building folder ZIPs. Serving the files to browsers and the database come next. The background jobs add a cost per source that exists with nobody signed in: the source check lists each source every `source_check_minutes`, and the file-name index lists each source's whole tree every `name_index_minutes`. The `background` phase of each profile measures that base cost.

### Memory

To be measured: the process's resident memory with no users, and its growth with concurrent opens, follows and transfers. Fetched files are streamed to scratch, not held in memory; each connected push agent holds a WebSocket and its buffers.

### Scratch disk

Every file opened or downloaded from a remote source (SSH, SMB, WinRM, push agent) is fetched in full into scratch, and stays there while any tab has it open; a download or folder ZIP holds it only for the transfer. Local-disk sources are read in place and use no scratch. So scratch needs about the number of files open at the same time across all users, times their size: ten people with a 500 MB log open each need 5 GB. Decompressed `.gz` files and archive members count at their decompressed size.

Scratch is bounded by `SCRATCH_MAX_GB` (the oldest unused copies are evicted first), by `SCRATCH_MIN_FREE_GB`/`SCRATCH_MIN_FREE_PERCENT` (new fetches are refused with HTTP 507 rather than filling the disk), and per file by `MAX_FETCH_MB` and `MAX_DECOMPRESSED_MB`. Copies a closed browser never released are deleted after `SCRATCH_IDLE_SECONDS`. Put scratch on its own volume (see `docker-compose.yml`) so a burst of large opens can't fill the database's disk.

### Database disk and writes

The database itself stays small; the file-name index (one row per file on every indexed source) and the audit log are what grow it. Writes matter more than size: SQLite takes one writer at a time, and every sign-in, every file open, download and Follow start (each is audited), each session's last-seen time (at most once a minute per session), each source check result and each file-name index pass is a write. The WAL grows during bursts of writes and is checkpointed back.

### Network and source latency

Nothing is cached: every folder expanded is a live listing on the source, and every open is a full fetch. The time a source takes to answer (network latency, the source's disk) adds directly to what users wait for, and holds a connector slot for that long. Polling multiplies the requests a single open page makes:

- Follow: one request every `tail_interval_seconds` per followed tab, about `followed tabs ÷ tail interval` requests a second, each a ranged read on the source.
- Auto-refresh: each expanded folder is listed again every `explorer_refresh_seconds`, about `users × expanded folders ÷ refresh interval` listings a second, plus one source list per user per interval.
- Background checks: `sources ÷ (source_check_minutes × 60)` listings a second, and `sources ÷ (name_index_minutes × 60)` full recursive listings a second for the file-name index.

### Push agents

Each connected agent holds one WebSocket to the server; the agent pings every 25 seconds and the server every 30. Listings and fetches are relayed down that connection, so a request to an agent source costs the round trip to the agent and the agent host's disk. Agents that are configured but down cost a failing status and nothing else.

## Settings that trade load for freshness

All of these are under **Settings → System**.

| Setting | Default | What it drives | Raising it |
|---|---|---|---|
| Follow interval (`tail_interval_seconds`) | 2 s | Follow polls, per followed tab | Fewer requests to the server and sources; new lines appear later. |
| Explorer auto-refresh (`explorer_refresh_seconds`) | 30 s | Re-listing every expanded folder, per open page | Fewer listings on every source; new files show up later. 0 turns it off. |
| Background source check (`source_check_minutes`) | 10 min | One listing per source per interval | Less base load per source; a broken source is noticed later. 0 turns it off. |
| File-name index (`name_index_enabled`, `name_index_minutes`) | on, 15 min | A full recursive listing of each indexed source per interval, and its rows in the database | Less load on sources and fewer database writes; search results are staler. Can be turned off per source. |
| Live alerts (`alerts_interval_seconds`) | 60 s | Reading the files that alerts watch | Less load; alerts fire later. |
| Connector limits (`concurrency_global`, `concurrency_per_source`, `concurrency_user_transfers`) | 16, 4, 2 | How many listings and fetches run at once, in all, per source and per user | More parallel work, more CPU and source load; past the limit requests get HTTP 503 with `Retry-After`. |
| Listing cap (`listing_max_entries`) | 5000 | The largest folder listing returned | Bigger answers and more time per listing in huge folders. |

The measured effect of the file-name index is the difference between the `mixed` and `mixed-index-off` phases of each profile.

## Limits of a single SQLite process

PerchTail is one process with one SQLite database, and that sets ceilings that more hardware doesn't lift:

- **One writer at a time.** SQLite in WAL mode lets reads run during a write, but writes queue; a write waits up to 30 seconds for the lock before failing. Sign-ins, audited opens and downloads, and file-name index passes all compete for it. To be measured: the write rate where waiting for the lock starts to show in p95 latency.
- **A bounded pool of request threads.** The API's endpoints are synchronous and run in the server's thread pool, and database access goes through a bounded connection pool; slow source calls hold a thread and a connection for as long as they take. To be measured: where the ramp's breaking point comes from (thread pool, connection pool, connector limits or CPU).
- **The connector limits** above are deliberate back-pressure: with the defaults, at most 16 source operations run at once, so the 17th concurrent open waits for a slot or gets a 503.
- **No horizontal scaling.** One instance per data directory (a second one refuses to start), and the scheduler and agent connections live in that process. Past the large profile, split users and sources over separate instances.

## Repeating the measurements

The load-test kit's [README](../loadtest/README.md) has the details; in short:

1. Install the tools at the versions the README pins (k6, Docker, Python, Go).
2. Start the instance to measure, and give the kit its URL and allowed host (`LOADTEST_BASE_URL`, `LOADTEST_ALLOWED_HOSTS`), an admin account, and the source servers (SSH, SMB, WinRM) it should use.
3. Run `python loadtest/run.py --profile small` (then `medium`, `large`), from a machine other than the instance's.
4. Measure a release tag, with PerchTail's default limits. Copy the "For docs/sizing.md" table and the breaking point from `loadtest/results/<date>-<profile>/summary.md` into this page, with the release and hardware they came from, and update the profiles table.
