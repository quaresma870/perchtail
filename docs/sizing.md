# Sizing guide

How much hardware a PerchTail instance needs for a number of users and sources, from load tests rather than estimates. Every number here was measured with the load-test kit in [`loadtest/`](../loadtest/README.md), on the release named next to it; re-run the kit to check a figure for another release or other hardware. No figure has been measured yet for 0.7.0.

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

### Files that grow while they are read

Logs are usually being written while people open, download and follow them. Each profile has three phases about that, and their results go here per profile, from the summary's growing-file tables:

- `growing_files`: files of 50 MB that grow at 256 KB/s and rotate every 5 minutes, opened whole, downloaded, downloaded as a folder ZIP and followed, on every protocol. Besides latency per protocol and operation, every answer is checked line by line: the file from its first line, nothing missing, repeated or cut off, never shorter than the server announced, and Follow carrying on exactly where it stopped, across rotations. The number that matters is the integrity errors, which must be zero: a viewer that shows a log with lines missing is wrong, however fast it is.
- `growing_heavy`: files written at 1, 5 and 10 MB/s each, followed at the viewer's pace. The result is the write rate from which Follow stops keeping up (its delay behind the writer keeps growing instead of staying level), with the server's CPU, memory and scratch at each rate. The viewer reads at most 8 × 1 MB per tick, so with a 2 s Follow interval about 4 MB/s per file is the most it can take in, whatever the hardware; a shorter interval raises that, at the cost of more requests.
- `daily_log`: one log that grows to 3 GB over a day compressed into an hour, quiet at night and busy in office hours with peaks of several MB/s, like a busy web server's access log. Follow runs all day, and the file is opened and downloaded whole now and then: below `MAX_FETCH_MB` that must work with every line intact, and above it the server must refuse cleanly (413) while Follow carries on. The result is Follow's delay per hour, above all at the peaks, and the server's CPU, memory and scratch through the day.

To be measured.

### A typical installation: the `production` profile

The profiles above load one thing at a time. `production` is the reference for a typical installation in use: 170 sources (90 Linux hosts, 60 over SSH and 30 with a push agent; 80 Windows hosts, 40 over SMB, 30 over WinRM and 10 with a push agent), each with its own logs, most of them quiet (70 % write a few KB/s), a quarter busier (hundreds of KB/s) and a few as busy as a web server's access log; and up to 30 people signed in at once at the peak of a working day, of five kinds: support staff browsing and opening files (40 %), people on call following several logs for hours (25 %), investigators searching names and downloading files and folders (20 %), administrators (10 %) and occasional visitors (5 %), each with a role that shows them only their part. In the middle of the peak, 20 people open and follow the same busy log for 10 minutes, as during an incident. The file-name index, the source checks, 10 alerts and a scheduled backup run as usual.

Its result goes here: latency and errors per kind of person, the server's CPU, memory, scratch and database over the day (and memory's trend per hour, to show a leak), what the incident costs, and whether the day stayed within the limits. It also runs for several days as a soak test.

To be measured.

### What each protocol and feature costs

The `protocol_matrix` profile measures one thing at a time, so protocols and features can be compared: on one source of each protocol (SSH, SMB, WinRM, WinRM through JEA, push agents on Linux and Windows, local disk), listing folders of 100 to 10,000 entries, opening and downloading files of 1 to 200 MB and following a growing file, one at a time and with 5 and 20 at once, optionally with added network delay towards the source. For each cell it records latency, time to the first byte and throughput, PerchTail's CPU time, memory and network traffic, and the source server's CPU, memory and disk reads. It also measures the fixed cost of a source (a reachability check and a file-name index pass), features with and without their cost (the audit page, sign-in with MFA), a backup (and, when asked for, its restore into a separate copy), and how a slow or failing source affects the others. From these, `report.py costs` works out cost units per operation, per MB transferred, per source and, with a `production` run, per active user.

To be measured.

## What drives each resource

### CPU

Most CPU goes to talking to sources: SSH encryption and SFTP framing, SMB signing, WinRM's XML and HTTP, decompressing `.gz` files and archive members, and building folder ZIPs. Serving the files to browsers and the database come next. The background jobs add a cost per source that exists with nobody signed in: the source check lists each source every `source_check_minutes`, and the file-name index lists each source's whole tree every `name_index_minutes`. The `background` phase of each profile measures that base cost.

### Memory

To be measured: the process's resident memory with no users, and its growth with concurrent opens, follows and transfers. Fetched files are streamed to scratch, not held in memory; each connected push agent holds a WebSocket and its buffers.

### Scratch disk

Every file opened or downloaded from a remote source (SSH, SMB, WinRM, push agent) is fetched in full into scratch. An open's copy is its own, for the session that opened it, and stays there until the viewer closes the tab or `SCRATCH_IDLE_SECONDS` (5 minutes) after it was sent, whichever comes first, since the browser already holds the content; a download holds its copy only for the transfer. A folder ZIP fetches its plain files one at a time, but it fetches and checks the folder's `.zip` and `.tar.gz` files (and, when downloads are redacted, its `.gz` files too) before the ZIP starts, and they wait in scratch for their turn, so size scratch for all the archives in the largest folder you zip. Local-disk sources serve plain files in place, and use scratch only for `.gz` files, archive members and redacted copies. So scratch needs about the number of files opened within a few minutes of each other across all users, times their size: ten people opening a 500 MB log each at about the same time need 5 GB. A file that grows is fetched at the size it has when it is opened; reopening it fetches it again at its new size, and Follow reads only what was appended, without scratch. Decompressed `.gz` files and archive members count at their decompressed size.

Scratch is bounded by `SCRATCH_MAX_GB` (the oldest unused copies are evicted first), by `SCRATCH_MIN_FREE_GB`/`SCRATCH_MIN_FREE_PERCENT` (new fetches are refused with HTTP 507 rather than filling the disk), and per file by `MAX_FETCH_MB` and `MAX_DECOMPRESSED_MB`. Copies a closed browser never released are deleted after `SCRATCH_IDLE_SECONDS`. Put scratch on its own volume (see `docker-compose.yml`) so a burst of large opens can't fill the database's disk.

### Database disk and writes

The database itself stays small; the file-name index (one row per file on every indexed source) and the audit log are what grow it. Writes matter more than size: SQLite takes one writer at a time, and every sign-in, every file open, download and Follow start, every folder listed below a source's root (while **Audit folder listings** is on, the default) and every file-name search (each is audited), each session's last-seen time (at most once a minute per session), each source check result and each file-name index pass is a write. The WAL grows during bursts of writes and is checkpointed back.

### Network and source latency

Nothing is cached: every folder expanded is a live listing on the source, and every open is a full fetch. The time a source takes to answer (network latency, the source's disk) adds directly to what users wait for, and holds a connector slot for that long. Polling multiplies the requests a single open page makes:

- Follow: one request every `tail_interval_seconds` per followed tab, about `followed tabs ÷ tail interval` requests a second, each a ranged read on the source.
- Auto-refresh: each expanded folder is listed again every `explorer_refresh_seconds`, about `users × expanded folders ÷ refresh interval` listings a second, plus one source list per user per interval (every 60 seconds when the refresh is off).
- Background checks: `sources ÷ (source_check_minutes × 60)` listings a second, and `sources ÷ (name_index_minutes × 60)` full recursive listings a second for the file-name index.

### Push agents

Each connected agent holds one WebSocket to the server; the agent pings every 25 seconds and the server every 30. Listings and fetches are relayed down that connection, so a request to an agent source costs the round trip to the agent and the agent host's disk. Agents that are configured but down cost a failing status and nothing else.

## Settings that trade load for freshness

The follow interval, auto-refresh, connector limits and listing cap are under **Settings → Explorer and limits**, the background check and the file-name index under **Settings → Source defaults**, and live alerts under **Settings → Alerts**.

| Setting | Default | What it drives | Raising it |
|---|---|---|---|
| **Tail interval** (`tail_interval_seconds`) | 2 s | Follow polls, per followed tab | Fewer requests to the server and sources; new lines appear later. |
| **Refresh open folders** (`explorer_refresh_seconds`) | 30 s | Re-listing every expanded folder, per open page | Fewer listings on every source; new files show up later. 0 turns it off. |
| **Check sources in the background** (`source_check_minutes`) | 10 min | One listing per source per interval | Less base load per source; a broken source is noticed later. 0 turns it off. |
| **Index file names** and **List each source's files every** (`name_index_enabled`, `name_index_minutes`) | on, 15 min | A full recursive listing of each indexed source per interval, and its rows in the database | Less load on sources and fewer database writes; search results are staler. Can be turned off per source. |
| **Check alerts every** (`alerts_interval_seconds`) | 60 s | Reading the files that alerts watch | Less load; alerts fire later. |
| **Listings and downloads at once**, **At once on one source**, **Downloads per user** (`concurrency_global`, `concurrency_per_source`, `concurrency_user_transfers`) | 16, 4, 2 | How many listings, opens, Tail polls and fetches run at once, in all and per source, and how many downloads and folder ZIPs each user may have in progress | More parallel work, more CPU and source load; past the limit requests get HTTP 503 with `Retry-After`. |
| **Entries per listing** (`listing_max_entries`) | 5000 | The largest folder listing returned | Bigger answers and more time per listing in huge folders. |

The measured effect of the file-name index is the difference between the `mixed` and `mixed-index-off` phases of each profile.

## Limits of a single SQLite process

PerchTail is one process with one SQLite database, and that sets ceilings that more hardware doesn't lift:

- **One writer at a time.** SQLite in WAL mode lets reads run during a write, but writes queue; a write waits up to 30 seconds for the lock before failing. Sign-ins, audited opens and downloads, and file-name index passes all compete for it. To be measured: the write rate where waiting for the lock starts to show in p95 latency.
- **A bounded pool of request threads.** The API's endpoints are synchronous and run in the server's thread pool, and database access goes through a bounded connection pool; slow source calls hold a thread and a connection for as long as they take. To be measured: where the ramp's breaking point comes from (thread pool, connection pool, connector limits or CPU).
- **The connector limits** above are deliberate back-pressure: with the defaults, at most 16 source operations run at once, so the 17th concurrent open gets a 503 with `Retry-After` instead of waiting for a slot.
- **No horizontal scaling.** One instance per data directory (a second one refuses to start), and the scheduler and agent connections live in that process. Past the large profile, split users and sources over separate instances.

## Repeating the measurements

The load-test kit's [README](../loadtest/README.md) has the details; in short:

1. Install the tools at the versions the README pins (k6, Docker, Python, Go).
2. Start the instance to measure, and give the kit its URL and allowed host (`LOADTEST_BASE_URL`, `LOADTEST_ALLOWED_HOSTS`), an admin account, and the source servers (SSH, SMB, WinRM) it should use.
3. Run `python loadtest/run.py --profile small` (then `medium`, `large`), from a machine other than the instance's.
4. Measure a release tag, with PerchTail's default limits. Copy the "For docs/sizing.md" table and the breaking point from `loadtest/results/<date>-<profile>/summary.md` into this page, with the release and hardware they came from, and update the profiles table.
