# Kubernetes

PerchTail installs on Kubernetes with the Helm chart in [`charts/perchtail/`](../charts/perchtail). This page covers what the cluster needs, installing, the main values, an ingress with TLS, backups and restoring, upgrading, uninstalling and PostgreSQL.

## What the chart runs

- **One pod, always.** PerchTail runs one process per data directory: it takes an instance lock, runs its scheduler and keeps some state in memory ([high-availability.md](high-availability.md)). The Deployment has one replica, which can't be changed, and uses the `Recreate` strategy, so the old pod lets go of the volume and the lock before the new one starts.
- **A data volume**, a PersistentVolumeClaim (`ReadWriteOnce`) mounted at `/data`: the SQLite database, the credential salt, the pinned SSH host keys, the application logs and the backups PerchTail makes. It must be block storage attached to the node, never NFS or another `ReadWriteMany` share: SQLite runs in WAL mode, which needs a local disk.
- **Scratch** in an `emptyDir` of its own with a size limit, mounted at `/scratch`; `SCRATCH_DIR` is `/scratch/files`, a folder PerchTail creates itself, readable by its user only (mode 0700), since the kubelet makes the mount point itself open to everyone. Fetched files only live there while they are opened or downloaded, and it is emptied at startup. `SCRATCH_MAX_GB` is set below the limit, so PerchTail refuses a fetch before the kubelet would evict the pod.
- **A ClusterIP Service** on port 80, and optionally an Ingress and a NetworkPolicy.
- **The same restrictions as the Docker Compose file**: it runs as PerchTail's user (uid and gid 10001, `runAsNonRoot`, `fsGroup` 10001 with `fsGroupChangePolicy: OnRootMismatch`), with a read-only root filesystem, no privilege escalation, every capability dropped, the `RuntimeDefault` seccomp profile, `/tmp` in memory and no service account token. Started as that user, the image's entrypoint skips its root step.
- **Probes that need no token.** `/healthz` and `/readyz` answer a caller on the pod's loopback address ([monitoring.md](monitoring.md#who-may-call-them)), and the kubelet isn't one, so the probes run the image's own health-check command inside the container: `/healthz` for liveness, `/readyz` for readiness, and a startup probe that leaves the first start up to five minutes for the database migrations.
- **No Helm hooks.** The entrypoint runs the migrations (`alembic upgrade head`) at every start, and there is one replica.

## Requirements

- Kubernetes 1.29 or later, and Helm 3.
- A storage class for block volumes (`ReadWriteOnce`), the cluster's default or one you name.
- An ingress controller to reach PerchTail from outside the cluster, with a TLS certificate: session cookies are only sent over HTTPS (`SESSION_COOKIE_SECURE` stays `true`).

## The encryption key

`CREDENTIAL_ENCRYPTION_KEY` encrypts the credentials PerchTail stores and keys its audit chain, together with the salt in the data volume ([credential-key-rotation.md](credential-key-rotation.md)). The chart never makes one up: a key generated at install time would be generated again by the next `helm template` or GitOps sync, and nothing encrypted could be read any more. Create a Secret with a key of your own, and keep a copy of the key somewhere outside the cluster:

```bash
kubectl create namespace perchtail
kubectl -n perchtail create secret generic perchtail-key \
  --from-literal=credential-encryption-key="$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")"
```

Without a key, `helm install` stops and says how to create that Secret. The key can also be passed as `credentialEncryptionKey.value`, which the chart stores in a Secret of its own; it is then in your values, so prefer an existing Secret.

## Installing

Keep your settings in a values file of your own, and give it to Helm at the install and at every upgrade, so an upgrade never depends on remembering what was set before. For example, `perchtail-values.yaml`:

```yaml
credentialEncryptionKey:
  existingSecret: perchtail-key
env:
  PUBLIC_BASE_URL: https://perchtail.example.com
```

From a checkout of the repository, with the Secret above:

```bash
helm install perchtail charts/perchtail -n perchtail -f perchtail-values.yaml
```

The notes Helm prints say how to read the first super admin's temporary password, which stays in the data volume until it is changed:

```bash
kubectl -n perchtail exec deploy/perchtail -- cat /data/db/initial-admin-password
```

Sign in with it, set your own password and two-factor authentication. Until an ingress is set up, `kubectl -n perchtail port-forward svc/perchtail 8080:80` reaches PerchTail at `http://127.0.0.1:8080`; signing in over that plain HTTP needs `env.SESSION_COOKIE_SECURE=false`, for a test only.

## Main values

The full list, with comments, is in [`values.yaml`](../charts/perchtail/values.yaml), and `values.schema.json` refuses unknown or wrongly typed values.

| Value | Default | What it does |
|---|---|---|
| `credentialEncryptionKey.existingSecret`, `.existingSecretKey` | none, `credential-encryption-key` | The Secret holding `CREDENTIAL_ENCRYPTION_KEY`, and the key inside it. |
| `database.existingSecret`, `.existingSecretKey` | none, `database-url` | A Secret holding `DATABASE_URL` ([PostgreSQL](#postgresql)). Without it, SQLite in the data volume. |
| `extraVolumes`, `extraVolumeMounts` | none | Volumes and their mounts added to the pod, for files it needs from a Secret or ConfigMap, such as the PostgreSQL CA certificate ([PostgreSQL](#postgresql)). |
| `image.registry`, `.repository`, `.tag`, `.digest`, `.pullSecrets` | `ghcr.io`, `quaresma870/perchtail`, the chart's `appVersion`, none, none | Where the image comes from: a mirror in a closed network, a pinned digest (which wins over the tag), pull credentials. |
| `persistence.size`, `.storageClass`, `.existingClaim` | `10Gi`, the cluster's default, none | The data volume, or an existing claim to use. |
| `scratch.sizeLimitGi`, `scratch.maxGb` | `6`, `5` | Scratch's `emptyDir` size limit and `SCRATCH_MAX_GB`, which must be below it. Raise both for large files ([sizing.md](sizing.md)). |
| `env` | none | Plain settings as environment variables ([configuration.md](configuration.md)), for example `PUBLIC_BASE_URL`, `TRUSTED_PROXIES`, `LOG_LEVEL`. |
| `extraEnv`, `extraEnvFrom` | none | Full environment entries and `envFrom` sources, for anything else (another Secret, a ConfigMap). |
| `resources` | requests 250m CPU and 512Mi, memory limit 1Gi | Sized for a small installation; see [sizing.md](sizing.md). |
| `ingress.*` | off | An Ingress: `className`, `annotations`, `hosts`, `tls`. |
| `networkPolicy.*` | off | A NetworkPolicy: traffic in only from the ingress controller's namespace and the `extraIngress` rules; traffic out open unless `egress` rules are given. |

## Ingress, TLS and the client's address

```yaml
env:
  PUBLIC_BASE_URL: https://perchtail.example.com
  # The pods of the ingress controller, so PerchTail believes their X-Forwarded-For.
  TRUSTED_PROXIES: 10.244.0.0/16
ingress:
  enabled: true
  className: nginx
  annotations:
    # The push agents keep a WebSocket open on /agent/connect.
    nginx.ingress.kubernetes.io/proxy-read-timeout: "3600"
    nginx.ingress.kubernetes.io/proxy-send-timeout: "3600"
  hosts:
    - host: perchtail.example.com
      paths:
        - path: /
          pathType: Prefix
  tls:
    - secretName: perchtail-tls
      hosts:
        - perchtail.example.com
```

- **Push agents** connect to `wss://<host>/agent/connect` and keep that WebSocket open. ingress-nginx closes a connection that has been quiet for 60 seconds by default; the two timeouts above keep it for an hour (the agent sends keepalives, and reconnects if it is closed anyway). Other controllers have their own setting for WebSocket timeouts.
- **The client's address.** The audit log records each request's client address, and PerchTail only believes `X-Forwarded-For` from an address in `TRUSTED_PROXIES` (loopback only by default). Set it to the network the ingress controller's pods get their addresses from (the cluster's pod network), or every entry records the controller's address instead of the client's ([audit-events.md](audit-events.md#client-address)).
- **NetworkPolicy.** With `networkPolicy.enabled`, only pods in `networkPolicy.ingressControllerNamespace` reach PerchTail, plus what the rules in `networkPolicy.extraIngress` allow (Kubernetes NetworkPolicy ingress rules, none by default). Traffic out stays open: sources, the SSO provider, webhooks and email servers vary. `networkPolicy.egress` restricts it to the rules given; `values.yaml` has a commented example (DNS plus the source networks).
- **Monitoring through the NetworkPolicy.** A monitoring system in another namespace can't reach `/metrics` or `/monitoring/health` ([monitoring.md](monitoring.md)) until a rule in `networkPolicy.extraIngress` lets it in. For a Prometheus in the `monitoring` namespace:

  ```yaml
  networkPolicy:
    enabled: true
    extraIngress:
      - from:
          - namespaceSelector:
              matchLabels:
                kubernetes.io/metadata.name: monitoring
            podSelector:
              matchLabels:
                app.kubernetes.io/name: prometheus
        ports:
          - port: http
            protocol: TCP
  ```

  It still needs the monitoring token, and an address in `MONITORING_ALLOWED_NETWORKS` when that is set.

## Backups

Two things, kept apart, and both needed to restore:

- **The data volume**, with your storage's volume snapshots, or the encrypted backups PerchTail makes itself into `/data/backups` (scheduled under Settings → Backups and updates, or `kubectl -n perchtail exec deploy/perchtail -- python -m app.backup create`), copied out of the cluster (`kubectl -n perchtail cp <pod>:/data/backups/<file> ./<file>`, with the pod's name from `kubectl -n perchtail get pods`). See [backup-and-restore.md](backup-and-restore.md).
- **The key** in the Secret: without it, nothing encrypted in the volume can be read; without the volume, the key is useless. Keep a copy outside the cluster, away from the volume's backups.

## Restoring a backup

A restore needs PerchTail stopped: it takes the same instance lock as PerchTail, so `kubectl exec` into the running pod is refused. Stop the Deployment, run the restore in a pod of its own on the same data volume, and start the Deployment again.

1. If the backup isn't in the volume yet, copy it in while PerchTail still runs:

   ```bash
   POD=$(kubectl -n perchtail get pods -l app.kubernetes.io/instance=perchtail -o jsonpath='{.items[0].metadata.name}')
   kubectl -n perchtail exec "$POD" -- mkdir -p -m 0700 /data/backups
   kubectl -n perchtail cp ./<file> "$POD:/data/backups/<file>"
   ```

2. Stop PerchTail, and wait until its pod is gone:

   ```bash
   kubectl -n perchtail scale deploy/perchtail --replicas=0
   kubectl -n perchtail wait --for=delete pod -l app.kubernetes.io/instance=perchtail --timeout=120s
   ```

3. Run the restore in a pod with the same image, data volume, key and restrictions as PerchTail's, `perchtail-restore.yaml`:

   ```yaml
   apiVersion: v1
   kind: Pod
   metadata:
     name: perchtail-restore
   spec:
     restartPolicy: Never
     automountServiceAccountToken: false
     securityContext:
       runAsUser: 10001
       runAsGroup: 10001
       runAsNonRoot: true
       fsGroup: 10001
       fsGroupChangePolicy: OnRootMismatch
       seccompProfile:
         type: RuntimeDefault
     containers:
       - name: restore
         # The image the Deployment runs:
         # kubectl -n perchtail get deploy perchtail -o jsonpath='{.spec.template.spec.containers[0].image}'
         image: ghcr.io/quaresma870/perchtail:<version>
         command: ["python", "-m", "app.backup", "restore", "/data/backups/<file>"]
         env:
           - name: CREDENTIAL_ENCRYPTION_KEY
             valueFrom:
               secretKeyRef:
                 name: perchtail-key
                 key: credential-encryption-key
           # Add BACKUP_ENCRYPTION_KEY, and any env or extraEnv entry of your
           # values that changes a path, as the Deployment has them.
         securityContext:
           readOnlyRootFilesystem: true
           allowPrivilegeEscalation: false
           capabilities:
             drop:
               - ALL
         volumeMounts:
           - name: data
             mountPath: /data
           - name: tmp
             mountPath: /tmp
     volumes:
       - name: data
         persistentVolumeClaim:
           claimName: perchtail-data
       - name: tmp
         emptyDir:
           medium: Memory
   ```

   ```bash
   kubectl -n perchtail apply -f perchtail-restore.yaml
   kubectl -n perchtail logs -f pod/perchtail-restore
   kubectl -n perchtail get pod perchtail-restore   # STATUS Completed: the restore worked
   kubectl -n perchtail delete pod perchtail-restore
   ```

   `command` replaces the image's entrypoint, so the pod runs the restore and nothing else. What the restore checks, refuses and leaves behind (the `before-restore` copy) is in [backup-and-restore.md](backup-and-restore.md#restoring).

4. Start PerchTail again; it migrates the restored database as it starts, as after an upgrade:

   ```bash
   kubectl -n perchtail scale deploy/perchtail --replicas=1
   ```

### Rolling back an upgrade

The previous version refuses a database the new one migrated, so a rollback restores the `pre-upgrade` backup ([upgrade.md](upgrade.md#rolling-back)): stop PerchTail (step 2 above), run the restore (step 3) with the previous version's image, put the previous release back (`helm rollback perchtail -n perchtail`, or `helm upgrade` from the previous checkout with your values file), then start it (step 4) if it isn't running yet.

## Upgrading

Make a backup first:

```bash
kubectl -n perchtail exec deploy/perchtail -- python -m app.backup create --reason pre-upgrade
```

Then, from a checkout of the new release, with the same values file as at the install:

```bash
helm upgrade perchtail charts/perchtail -n perchtail -f perchtail-values.yaml
```

Prefer your values file to `--reuse-values`: that option takes only the values of the previous release and ignores the defaults of the new chart, so a value a newer chart adds is missing from the upgrade.

The pod is replaced (the old one stops before the new one starts) and the new one runs the database migrations as it starts. The chart's version is the app's: a chart from a newer checkout installs that release's image unless `image.tag` or `image.digest` says otherwise.

## Uninstalling

```bash
helm uninstall perchtail -n perchtail
```

The data volume is kept (`helm.sh/resource-policy: keep`), and so is a key Secret the chart made from `credentialEncryptionKey.value`; a Secret you created is yours. Delete the claim (`kubectl -n perchtail delete pvc perchtail-data`) only when the data is no longer needed.

## PostgreSQL

SQLite in the data volume is the default. (Outside Kubernetes, `docker-compose.postgres.yml` runs PerchTail with a PostgreSQL of its own in Docker Compose: see the [README](../README.md#running-on-postgresql-with-docker-compose).) To use PostgreSQL, prepare the database as [configuration.md](configuration.md#postgresql) says (PostgreSQL 18 or later, UTF8, verified TLS over the network), put the connection URL in a Secret, and name it in `database.existingSecret`:

```bash
kubectl -n perchtail create secret generic perchtail-database \
  --from-literal=database-url='postgresql://perchtail:<password>@db.example.com:5432/perchtail?sslmode=verify-full&sslrootcert=<the CA certificate file>'
```

```yaml
database:
  existingSecret: perchtail-database
```

The chart sets `DATABASE_URL` from that Secret, so the URL and its password are never in your values. The CA certificate that `sslrootcert` names reaches the pod through `extraVolumes` and `extraVolumeMounts`, from a Secret (or ConfigMap) holding it:

```sh
kubectl -n perchtail create secret generic perchtail-pg-ca --from-file=ca.crt=<the CA certificate file>
```

```yaml
extraVolumes:
  - name: pg-ca
    secret:
      secretName: perchtail-pg-ca
extraVolumeMounts:
  - name: pg-ca
    mountPath: /etc/perchtail/pg-ca
    readOnly: true
```

and the URL ends with `?sslmode=verify-full&sslrootcert=/etc/perchtail/pg-ca/ca.crt`. A PostgreSQL in the same cluster is reached over the pod network, which isn't local to PerchTail: without a verified certificate PerchTail refuses to start, unless `DATABASE_REQUIRE_VERIFIED_TLS=false` (in `env`) turns the refusal into a warning. The same two values mount a file for `DATABASE_URL_FILE`, if you prefer it to `database.existingSecret`.

The data volume stays needed with PostgreSQL, for the credential salt, the pinned SSH host keys, the audit chain marker, the instance lock and the logs. `python -m app.backup` works on SQLite only for now, so on PostgreSQL back the database up with PostgreSQL's own tools, and the data volume and the key as above ([backup-and-restore.md](backup-and-restore.md)).
