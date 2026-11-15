# High availability & horizontal scaling

**PerchTail runs as one process per `DATA_DIR`.** Its database is SQLite (with an FTS5 trigram table for the file-name index), its twelve background jobs run inside the process, and part of its state lives only in that process's memory (see [What lives in the process](#what-lives-in-the-process)). None of that is safe to run as several replicas at once. This page describes what works today, and why the usual ways of adding replicas don't, organized by how much traffic-splitting you want.

**One instance per `DATA_DIR`, enforced.** On startup, and before `alembic upgrade` migrates, PerchTail takes an exclusive lock from the operating system on `DATA_DIR/.perchtail.lock`. A second instance on the same `DATA_DIR` refuses to start with `startup.instance_lock_held` (naming the host, PID, version and start time of the one running) before it opens the database; there is no setting to turn this off. The lock dies with the process that holds it, however it ends, so a crash or `kill -9` never leaves a stale lock behind. The same lock covers every Alembic command run in online mode (`upgrade`, `downgrade`, `stamp`, even `current`), so while the app is running any of them is refused on purpose: stop the app first. Keep `DATA_DIR` on a local disk: locks on a network share may not reach other machines.

## What lives in the process

**Background jobs.** One scheduler in the process runs these jobs (`id`, as in `/monitoring/health`; see [monitoring.md](monitoring.md)):

| Job | Runs |
|---|---|
| `scratch_idle_sweep`, `scratch_size_guard` | every `SCRATCH_SWEEP_INTERVAL_SECONDS` (60) |
| `alert_pass` | every 15 seconds; a pass runs once the interval in Settings → System has gone by |
| `name_index_sweep` | every minute; lists the sources that are due |
| `source_checks` | every minute; checks the sources that are due |
| `agent_upgrades` | every minute; ends timed-out upgrades and runs automatic ones inside the window |
| `backup` | every 5 minutes; backs up once the interval in Settings → System has gone by (off by default) |
| `wal_checkpoint` | every hour |
| `session_purge`, `update_check` | every hour |
| `audit_purge` | every `AUDIT_PURGE_INTERVAL_SECONDS` (a day) |
| `audit_integrity_check` | every `AUDIT_INTEGRITY_CHECK_INTERVAL_DAYS` (a day) |

`audit_purge`, `session_purge`, `update_check` and `audit_integrity_check` catch up after a restart: when their interval has gone by since their last success, they run 30 seconds after startup. Each job's last run is stored in the database.

**In memory only**, lost on restart and not shared with any other process:

- the push agents' WebSocket connections: a command for an agent can only go through the process it's connected to;
- the connector limits (Settings → System → Limits): the slots in use per source and per user, and the count of refused requests in `/monitoring/health`;
- login throttling: failed sign-ins and lockouts per username and per client address, so a restart clears every lockout;
- the counting of repeated failed sign-ins before they're written to the audit log;
- which files in the scratch cache are open (the files themselves are on local disk under `SCRATCH_DIR`, and are cleared at startup);
- the file-name index passes and source checks queued or running;
- the result of the last live alert pass (`alerts` in `/monitoring/health`), the time of the last failed scheduled backup, and whether the log size limit deleted logs early (`logs.size_limit_deleted_early`).

Everything else (sources, rules, users, roles, sessions, settings, the file-name index, alert positions and pending webhook calls, the audit log, agent upgrade history and job records) is in the database.

Every solution below is compatible with nginx in front (see the main [README](../README.md#deployment-reverse-proxy-nginx--tls) for the base TLS config) and with running under k3s or any other Kubernetes distribution — what changes between them is *what kind of load balancing is actually safe* to put in front of PerchTail, not whether Kubernetes itself is involved.

## At a glance

| Solution | Load balancing type | What changes vs. today | Effort |
| --- | --- | --- | --- |
| 0 — Single replica, self-healing | None (one pod; a proxy just routes to it) | Nothing in the app | None — works today |
| 1 — Active-passive (warm standby) | Failover only, never simultaneous | Continuous DB replication (e.g. Litestream) + a promotion mechanism | Small–medium |
| 2 — Sticky-session active-active | Client/session affinity | Nothing in the app, but doesn't fix the real problem (see below) | Low, but don't stop here |
| 3 — True active-active | Standard round-robin / least-connections | Not possible today: needs a shared database instead of SQLite, the in-memory state shared, and the scheduler in a process of its own | Large changes in PerchTail |

## Solution 0 (works today): single replica, orchestrator-level self-healing

One pod, restarted automatically by Kubernetes if it crashes or the `/healthz` liveness probe fails. This is not high availability in the "no downtime" sense — a crash or a rollout still causes a gap while the pod restarts — but it removes the "someone has to notice and manually restart it" failure mode, which is most of the practical benefit smaller deployments actually need.

Load balancing here is trivial: a Kubernetes `Service` + `Ingress` route to whichever single pod is `Ready`. No stickiness, no distribution logic — there's only ever one backend.

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: perchtail-data
spec:
  accessModes: ["ReadWriteOnce"] # sufficient, and a deliberate safety rail:
  # this alone stops a second pod from ever mounting the same SQLite file
  resources:
    requests:
      storage: 10Gi
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: perchtail
spec:
  replicas: 1
  # Recreate, not the default RollingUpdate -- RollingUpdate would try to
  # bring up a second pod before tearing down the first, which a
  # ReadWriteOnce PVC blocks anyway (the new pod just hangs in
  # ContainerCreating), turning a clean rollout into a stuck one. Recreate
  # tears the old pod down first, trading a few seconds of downtime per
  # deploy for a rollout that actually completes.
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app: perchtail
  template:
    metadata:
      labels:
        app: perchtail
    spec:
      terminationGracePeriodSeconds: 30 # above SHUTDOWN_TIMEOUT_SECONDS (20)
      containers:
        - name: perchtail
          image: ghcr.io/quaresma870/perchtail:latest # pin a real tag in production
          ports:
            - containerPort: 8000
          envFrom:
            - secretRef:
                name: perchtail-env # CREDENTIAL_ENCRYPTION_KEY, PUBLIC_BASE_URL, etc.
          env:
            # The kubelet probes from the node's address, not loopback: let
            # the nodes' addresses call /healthz and /readyz without the
            # monitoring token (docs/monitoring.md, "Who may call them").
            # Only the nodes: a range that also holds the ingress controller
            # or another proxy opens the probes to everyone behind it. Or
            # send the token with httpGet.httpHeaders instead.
            - name: PROBE_ALLOWED_NETWORKS
              value: "10.0.1.0/24" # your nodes' addresses, nothing wider
          volumeMounts:
            - name: data
              mountPath: /data
          livenessProbe:
            httpGet:
              path: /healthz
              port: 8000
            initialDelaySeconds: 10
            periodSeconds: 15
          # /readyz: a database write works, DATA_DIR is writable and the
          # scheduler runs. /healthz stays the liveness probe, so a locked
          # database takes the pod out of the Service without restarting it.
          readinessProbe:
            httpGet:
              path: /readyz
              port: 8000
            initialDelaySeconds: 5
            periodSeconds: 10
            timeoutSeconds: 5
      volumes:
        - name: data
          persistentVolumeClaim:
            claimName: perchtail-data
---
apiVersion: v1
kind: Service
metadata:
  name: perchtail
spec:
  selector:
    app: perchtail
  ports:
    - port: 8000
      targetPort: 8000
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: perchtail
  annotations:
    # Agent-protocol WebSocket connections (wss://.../agent/connect) are
    # long-lived and often idle -- raise this well past ingress-nginx's
    # default, same reasoning as the plain-nginx proxy_read_timeout in the
    # README's reverse-proxy section. Omit this annotation entirely if you
    # don't use the Agent protocol.
    nginx.ingress.kubernetes.io/proxy-read-timeout: "3600"
    cert-manager.io/cluster-issuer: letsencrypt # if using cert-manager for TLS
spec:
  ingressClassName: nginx # k3s ships Traefik by default -- either install
  # k3s with `--disable=traefik` and add ingress-nginx separately, or run
  # plain nginx as its own Deployment/Service in front instead of using an
  # Ingress at all; both work identically from PerchTail's side.
  tls:
    - hosts: ["perchtail.example.com"]
      secretName: perchtail-tls
  rules:
    - host: perchtail.example.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: perchtail
                port:
                  number: 8000
```

Nothing here requires an application change. This is the recommended default for most self-hosted deployments, including a product organization running PerchTail as an internal tool.

**Stop grace period.** On shutdown PerchTail stops starting background jobs and gives the running ones (an audit purge, an alert pass, a file-name index pass, source checks, a scheduled backup) up to `SHUTDOWN_TIMEOUT_SECONDS` (20 by default) to finish before it exits. The orchestrator must wait longer than that before it kills the container, or a rollout still cuts that work off mid-run: Docker's default is 10 seconds, so `docker-compose.yml` sets `stop_grace_period: 30s`; Kubernetes waits `terminationGracePeriodSeconds` (30 by default, set explicitly above). Raise both together if you raise `SHUTDOWN_TIMEOUT_SECONDS`.

## Solution 1: active-passive (warm standby)

**Load balancing type: failover only — the standby pod never receives live traffic simultaneously with the primary.**

Two flavors, in increasing order of how much downtime a failure actually costs:

- **1a — faster failover, no new infrastructure.** Tighten Solution 0's probe intervals and rely on Kubernetes rescheduling the pod quickly. This is really Solution 0 tuned, not a new architecture.
- **1b — real warm standby with data continuity.** Continuously replicate the SQLite file to object storage (e.g. [Litestream](https://litestream.io/)), and keep a second pod provisioned but not `Ready` (its readiness probe deliberately fails, or its replica count is held at 0 until promotion) — on primary failure, restore the latest replicated snapshot into the standby and flip it to serving. This meaningfully shortens recovery time compared to "wait for a fresh pod to reschedule against an empty PVC," but promotion isn't instant or fully automatic without extra tooling (a controller watching primary health and re-pointing the `Service` selector, or a manual runbook step) — it trades some downtime for real data continuity, not zero downtime. PerchTail has no built-in replication or standby: this is a setup you build and test yourself. PerchTail checkpoints and truncates SQLite's write-ahead log every hour (the `wal_checkpoint` job), and a replication tool must cope with that. The standby needs its own `DATA_DIR`, the same `CREDENTIAL_ENCRYPTION_KEY` and a copy of the credential salt, which lives in `DATA_DIR` next to the database.

Neither flavor needs another database, a scheduler redesign, or shared in-memory state — the standby is never live at the same time as the primary, so none of Solution 3's problems apply.

## Solution 2: sticky-session active-active (partial — don't stop here)

**Load balancing type: client/session affinity** (IP-hash or cookie-based sticky routing at the nginx/Ingress layer), the thing people usually reach for first when told "add a load balancer." Worth naming explicitly so it's clear why it isn't actually a solution on its own:

- Sticky routing can keep one browser session's requests landing on the same pod, which would avoid some of the per-process state problems (agent connections, the scratch cache) described in Solution 3 below.
- It does **nothing** for SQLite's single-writer constraint. Two different users' browsers, pinned to two different pods, can both attempt writes (a login, a rule edit, a source change, an audit-log entry) against two independent SQLite file handles at the same time — that's exactly the failure mode multi-writer SQLite doesn't handle safely, sticky sessions or not.

Sticky sessions alone don't make several instances safe, so don't adopt them as an answer.

## Solution 3: true active-active horizontal scaling

**Load balancing type: standard round-robin / least-connections — no stickiness required for anything backed by shared state.** PerchTail can't run this way: it would take changes in PerchTail itself, not configuration. What stands in the way:

1. **The database is SQLite**, which allows one writer, on a local disk. The file-name index's search is built on an SQLite FTS5 trigram table (`app/name_index_store.py`).
2. **Login throttling is in process memory** (`app/login_throttle.py`): each replica would count failed sign-ins on its own, so an attacker spreading attempts across replicas would get more tries.
3. **Every replica would run every background job.** The twelve jobs in [What lives in the process](#what-lives-in-the-process) run in each process, so `replicas: N` means N uncoordinated copies of each job working on the same data.
4. **Agent connections and the scratch cache are per process.** `app/agent_registry.py` keeps the push agents' WebSocket connections in memory — a command for a source whose agent is connected to pod A can't be served by pod B. The scratch cache is local disk per pod, with the same problem for an open and its matching download landing on different pods. The connector limits are counted per process too.

## Recommendation

Use **Solution 0** — it needs no change, and is fully described above and in the README's reverse-proxy section. Consider Solution 1 only if you need faster recovery than a pod reschedule gives, and test the failover yourself. Don't use Solutions 2 or 3: PerchTail doesn't support more than one active instance.
