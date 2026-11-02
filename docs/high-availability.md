# High availability & horizontal scaling

**Where PerchTail stands today: single-instance by design, not by oversight.**
SQLite (with a raw FTS5 virtual table for full-text search), an in-process
APScheduler running five background jobs, and in-memory state for
login-throttle lockouts and live push-agent WebSocket connections are all
deliberate choices for a self-hosted, single-deployment tool (see CLAUDE.md's
tech-stack rationale). None of that is safe to run as multiple simultaneous
replicas without changing it first — this doc lays out exactly what would
need to change, organized by how much traffic-splitting you actually want,
so a decision to invest in any of it is made deliberately rather than by
someone assuming "just add replicas" works.

Every solution below is compatible with nginx in front (see the main
[README](../README.md#deployment-reverse-proxy-nginx--tls) for the base TLS
config) and with running under k3s or any other Kubernetes distribution —
what changes between them is *what kind of load balancing is actually safe*
to put in front of PerchTail, not whether Kubernetes itself is involved.

## At a glance

| Solution | Load balancing type | What changes vs. today | Effort |
| --- | --- | --- | --- |
| 0 — Single replica, self-healing | None (one pod; a proxy just routes to it) | Nothing in the app | None — works today |
| 1 — Active-passive (warm standby) | Failover only, never simultaneous | Continuous DB replication (e.g. Litestream) + a promotion mechanism | Small–medium |
| 2 — Sticky-session active-active | Client/session affinity | Nothing in the app, but doesn't fix the real problem (see below) | Low, but don't stop here |
| 3 — True active-active | Standard round-robin / least-connections | Postgres instead of SQLite, full-text search reimplemented, throttle/agent state centralized, scheduler split into its own tier | Large |

## Solution 0 (works today): single replica, orchestrator-level self-healing

One pod, restarted automatically by Kubernetes if it crashes or the
`/healthz` liveness probe fails. This is not high availability in the "no
downtime" sense — a crash or a rollout still causes a gap while the pod
restarts — but it removes the "someone has to notice and manually restart
it" failure mode, which is most of the practical benefit smaller deployments
actually need.

Load balancing here is trivial: a Kubernetes `Service` + `Ingress` route to
whichever single pod is `Ready`. No stickiness, no distribution logic —
there's only ever one backend.

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
      containers:
        - name: perchtail
          image: ghcr.io/quaresma870/perchtail:latest # pin a real tag in production
          ports:
            - containerPort: 8000
          envFrom:
            - secretRef:
                name: perchtail-env # CREDENTIAL_ENCRYPTION_KEY, PUBLIC_BASE_URL, etc.
          volumeMounts:
            - name: data
              mountPath: /data
          livenessProbe:
            httpGet:
              path: /healthz
              port: 8000
            initialDelaySeconds: 10
            periodSeconds: 15
          readinessProbe:
            httpGet:
              path: /healthz
              port: 8000
            initialDelaySeconds: 5
            periodSeconds: 5
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

Nothing here requires an application change. This is the recommended
default for most self-hosted deployments, including a product organization
running PerchTail as an internal tool.

## Solution 1: active-passive (warm standby)

**Load balancing type: failover only — the standby pod never receives live
traffic simultaneously with the primary.**

Two flavors, in increasing order of how much downtime a failure actually
costs:

- **1a — faster failover, no new infrastructure.** Tighten Solution 0's
  probe intervals and rely on Kubernetes rescheduling the pod quickly. This
  is really Solution 0 tuned, not a new architecture, and is worth doing
  regardless of whether 1b is ever built.
- **1b — real warm standby with data continuity.** Continuously replicate
  the SQLite file to object storage (e.g. [Litestream](https://litestream.io/)),
  and keep a second pod provisioned but not `Ready` (its readiness probe
  deliberately fails, or its replica count is held at 0 until promotion) —
  on primary failure, restore the latest replicated snapshot into the
  standby and flip it to serving. This meaningfully shortens recovery time
  compared to "wait for a fresh pod to reschedule against an empty PVC," but
  promotion isn't instant or fully automatic without extra tooling (a
  controller watching primary health and re-pointing the `Service`
  selector, or a manual runbook step) — it trades some downtime for real
  data continuity, not zero downtime.

Neither flavor needs Postgres, a scheduler redesign, or shared in-memory
state — the standby is never live at the same time as the primary, so none
of Solution 3's problems apply.

## Solution 2: sticky-session active-active (partial — don't stop here)

**Load balancing type: client/session affinity** (IP-hash or cookie-based
sticky routing at the nginx/Ingress layer), the thing people usually reach
for first when told "add a load balancer." Worth naming explicitly so it's
clear why it isn't actually a solution on its own:

- Sticky routing can keep one browser session's requests landing on the
  same pod, which would fix the agent-connection and scratch-cache
  per-pod-state problems described in the architecture notes below.
- It does **nothing** for SQLite's single-writer constraint. Two different
  users' browsers, pinned to two different pods, can both attempt writes
  (a login, a rule edit, a source change, an audit-log entry) against two
  independent SQLite file handles at the same time — that's exactly the
  failure mode multi-writer SQLite doesn't handle safely, sticky sessions
  or not.

Sticky sessions are a real, useful piece of Solution 3's design (for the
agent/scratch pieces specifically) — they're just not a standalone answer,
and shouldn't be adopted as one.

## Solution 3: true active-active horizontal scaling

**Load balancing type: standard round-robin / least-connections — no
stickiness required for anything backed by shared state.** This is the
actual fix, and a real engineering investment, not a configuration change.
Four independent pieces of work:

1. **Replace SQLite with Postgres.** Also means reimplementing full-text
   search: the existing engine is built directly on SQLite's FTS5 virtual
   table (`app/search_index.py`, schema created by `app/db.py`'s
   `ensure_search_schema`) — raw FTS5 SQL that doesn't run against Postgres
   at all. Postgres's own `tsvector`/`tsquery` full-text search is the
   natural replacement, but it's a real reimplementation, not a
   connection-string swap.
2. **Move login-throttle state out of process memory.** `app/login_throttle.py`
   is explicitly in-memory today ("an accepted trade-off for a
   single-container SQLite deployment," per its own docstring) — move
   lockout counters into Postgres or Redis so every replica sees the same
   state.
3. **Split into a stateless web tier and a dedicated single-replica worker
   tier.** The five APScheduler jobs wired up in `app/main.py`'s lifespan
   (scratch idle-sweep, scratch size-guard, search indexing, audit purge,
   audit integrity check) currently run in-process, once per pod — with
   `replicas: N`, that's N independent, uncoordinated copies of each job
   racing against the same database. The standard fix: `replicas: N` web
   pods that serve HTTP only, plus one dedicated `replicas: 1` worker pod
   that owns the scheduler.
4. **Centralize (or narrowly pin) agent connections and the scratch
   cache.** `app/agent_registry.py` keeps live push-agent WebSocket
   connections in an in-memory dict per pod — a command for a source
   connected to pod A can't be served by pod B. The ephemeral fetch
   scratch cache is local disk per pod, with the same problem for an "open"
   and its matching "download" landing on different pods. Either
   centralize both (a shared connection broker; shared/network storage for
   scratch), or scope Solution 2's sticky routing narrowly to just these
   two concerns while everything else rides on Postgres's shared state.

Build this once a real, measured need justifies it — a customer's uptime
SLA, or an actual concurrent-load ceiling being hit — not preemptively.
Building it speculatively risks a large, disruptive migration (SQLite →
Postgres touches the credential-encryption code path, the audit hash-chain,
and every existing Alembic migration's assumptions) for a scaling need that
may never materialize.

## Recommendation

Start with **Solution 0** — it costs nothing, is already fully described
above and in the README's reverse-proxy section, and covers the actual
majority of self-hosted and internal-tool deployments this project targets.
Treat Solutions 1–3 as scoped, gated engineering investments: pick up 1
(warm standby) if a specific customer needs faster recovery than a pod
reschedule provides; pick up 3 (the real fix) only once concurrent load or
a contractual uptime commitment genuinely requires simultaneous multi-replica
capacity. Skip 2 as a standalone target — treat it only as a component of 3.
