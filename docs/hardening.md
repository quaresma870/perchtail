# Hardening guide

A checklist for running PerchTail in production. Each item says what to do and why; the reasons behind the design are in [security-model.md](security-model.md), and every setting is in [configuration.md](configuration.md). The defaults are already the safer choice where one exists; this guide covers what only the operator can do.

## Before the first start

- [ ] **Generate `CREDENTIAL_ENCRYPTION_KEY` at random**, for example `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`. It encrypts every stored credential and keys the audit chain. PerchTail refuses to start with the shipped `changeme` or with a weak key; leave `CREDENTIAL_KEY_REFUSE_WEAK` at its default `true`.
- [ ] **Keep the key outside the host**, in your organisation's secret store, where you can reach it if the host is lost. The database, the salt in `DATA_DIR` and the key are useless without each other.
- [ ] **Set `BACKUP_ENCRYPTION_KEY`** to a second random key, kept apart from the first, so a stolen backup and a leaked `CREDENTIAL_ENCRYPTION_KEY` aren't enough together ([backup-and-restore.md](backup-and-restore.md#the-key)).
- [ ] **Set `PUBLIC_BASE_URL`** to the `https://` address users will use. The SSO redirect address and password-reset links are built from it, and password recovery by email can only be turned on while it is `https://`. Push agents don't use it: each has its own `PERCHTAIL_SERVER_URL`.
- [ ] **Leave `SESSION_COOKIE_SECURE` at `true`.** Set it to `false` only for a local test over plain HTTP.
- [ ] **Change `INITIAL_ADMIN_USERNAME`** if you don't want the emergency account to be called `admin`. It is used only on the first start.

## Network

- [ ] **Put a reverse proxy in front that terminates TLS** (TLS 1.2 or later), and send HTTP to HTTPS. The README has an nginx example ([Deployment: reverse proxy + TLS](../README.md#deployment-reverse-proxy-nginx--tls)). Use the protocols and cipher suites of Mozilla's "intermediate" profile ([TLS settings](#tls-settings)).
- [ ] **Set HSTS on the proxy**, with `includeSubDomains`: `Strict-Transport-Security: max-age=31536000; includeSubDomains`. PerchTail doesn't set it because it doesn't terminate TLS. `includeSubDomains` makes browsers that visited PerchTail use HTTPS for every subdomain of its host name as well (for `perchtail.example.com`, also `a.perchtail.example.com`), for a year: leave it out only if such a subdomain must still be reached over plain HTTP.
- [ ] **Limit request rates at the proxy.** PerchTail limits sign-ins, SSO sign-in starts, password-recovery requests and webhook tests itself, holds each account to 100 alerts, and bounds connector work in all, per source and per user, but it doesn't limit how many requests a signed-in user sends. A limit per client address at the proxy bounds that ([Rate limiting](#rate-limiting)).
- [ ] **Keep PerchTail itself unreachable except through the proxy.** `docker-compose.yml` publishes it on `127.0.0.1` only; with the proxy in another container, publish no port at all and share a Docker network. A source install listens where its `ExecStart` line says ([install-from-source.md](install-from-source.md)). On Kubernetes, the chart's Service is `ClusterIP`: reach it through the ingress, and turn on its NetworkPolicy so only the ingress controller's namespace can ([kubernetes.md](kubernetes.md#ingress-tls-and-the-clients-address)).
- [ ] **Set `TRUSTED_PROXIES`** to the proxy's address when it isn't on the same host. Otherwise every audit entry and every failed sign-in carries the proxy's address, and failed sign-ins from everyone count against one address. Never set it to a range that holds clients ([audit-events.md](audit-events.md#client-address)). On Kubernetes, that is the network the ingress controller's pods get their addresses from. When a request carries `X-Forwarded-For` or `Forwarded` from an address outside it, the log has `client_address.untrusted_forwarded_header` once and `/monitoring/health` reports `trusted_proxies_suspect: true` until the next restart.
- [ ] **Allow only the outbound connections you use** from the PerchTail host: the sources (SSH 22, SMB 445, WinRM 5986), the identity provider and the SMTP server over HTTPS and SMTP, the webhook receivers of your alerts, and `api.github.com` for the release check. Inbound, only the proxy needs to reach PerchTail, and push agents need to reach the proxy. The list of everything PerchTail connects to is in [data-and-retention.md](data-and-retention.md#what-leaves-the-installation).
- [ ] **Turn the release check off on networks without internet access**: `UPDATE_CHECK=false` makes sure the request is never made.
- [ ] **Limit who can call the monitoring endpoints**: keep the monitoring token in your monitoring system only, and set `MONITORING_ALLOWED_NETWORKS` (and `PROBE_ALLOWED_NETWORKS` only for a load balancer or orchestrator that can't send the token) to its addresses ([monitoring.md](monitoring.md)).
- [ ] **Leave `API_DOCS` off.** The API is internal to the web interface.

### TLS settings

Use Mozilla's "intermediate" profile: TLS 1.2 and 1.3 only, with forward-secret AEAD cipher suites. It serves every browser of the last several years. [Mozilla's SSL Configuration Generator](https://ssl-config.mozilla.org/) writes it for your proxy and its version. For the nginx example in the README, these lines go in the `server` block that listens on 443, with HSTS:

```nginx
ssl_protocols TLSv1.2 TLSv1.3;
ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305:DHE-RSA-AES128-GCM-SHA256:DHE-RSA-AES256-GCM-SHA384:DHE-RSA-CHACHA20-POLY1305;
ssl_prefer_server_ciphers off;
ssl_session_cache shared:MozSSL:10m;
ssl_session_timeout 1d;
ssl_session_tickets off;

add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
```

Caddy's defaults already match the profile; add HSTS with `header Strict-Transport-Security "max-age=31536000; includeSubDomains"`. In Traefik, set it as the default TLS option and add HSTS with a `headers` middleware:

```yaml
# Traefik dynamic configuration
tls:
  options:
    default:
      minVersion: VersionTLS12
      cipherSuites:
        - TLS_ECDHE_ECDSA_WITH_AES_128_GCM_SHA256
        - TLS_ECDHE_RSA_WITH_AES_128_GCM_SHA256
        - TLS_ECDHE_ECDSA_WITH_AES_256_GCM_SHA384
        - TLS_ECDHE_RSA_WITH_AES_256_GCM_SHA384
        - TLS_ECDHE_ECDSA_WITH_CHACHA20_POLY1305
        - TLS_ECDHE_RSA_WITH_CHACHA20_POLY1305
http:
  middlewares:
    perchtail-hsts:
      headers:
        stsSeconds: 31536000
        stsIncludeSubdomains: true
```

PerchTail's own outgoing connections (the identity provider, webhooks, the SMTP server, WinRM over HTTPS, PostgreSQL, the release check) use TLS 1.2 or later with the server's certificate checked, without any setting.

### Rate limiting

A limit per client address at the proxy bounds how many requests anyone, signed in or not, can send. The web interface sends a burst of requests when a page opens, and each open Tail polls on its own, so allow a generous burst; users behind one NAT or office proxy share an address. Each push agent holds one long-lived request (`/agent/connect`), which a request rate doesn't affect. A request over the limit gets 429 from the proxy.

nginx, with a stricter limit on the sign-in form on top of PerchTail's own lockout:

```nginx
# In the http block:
limit_req_zone $binary_remote_addr zone=perchtail:10m rate=10r/s;
limit_req_zone $binary_remote_addr zone=perchtail_login:10m rate=10r/m;

# In the server block that listens on 443:
limit_req_status 429;

location / {
    limit_req zone=perchtail burst=100 nodelay;
    # ... the proxy settings of the README's example
}

location = /auth/login {
    limit_req zone=perchtail_login burst=10 nodelay;
    # ... the same proxy settings
}
```

On Kubernetes with ingress-nginx, the Ingress annotations `nginx.ingress.kubernetes.io/limit-rps` and `nginx.ingress.kubernetes.io/limit-burst-multiplier` do the same ([kubernetes.md](kubernetes.md)).

Caddy, with the `caddy-ratelimit` module (Caddy built with it, for example with `xcaddy build --with github.com/mholt/caddy-ratelimit`):

```caddy
{
	order rate_limit before reverse_proxy
}

perchtail.example.com {
	rate_limit {
		zone per_client {
			key {remote_host}
			events 600
			window 1m
		}
	}
	reverse_proxy 127.0.0.1:8080
}
```

Traefik, with its `rateLimit` middleware, which counts per client address by default:

```yaml
# Traefik dynamic configuration
http:
  middlewares:
    perchtail-ratelimit:
      rateLimit:
        average: 10
        burst: 100
```

Attach it, and the `perchtail-hsts` middleware above, to PerchTail's router.

## The host and its files

- [ ] **Run the container as shipped**: read-only root filesystem, `no-new-privileges`, every capability dropped except the few the entrypoint's root step needs to hand `/data` to PerchTail's user before it drops them all. Better, run it as PerchTail's user from the start (`user: "10001:10001"` in `docker-compose.yml`, without `cap_add`), as the README's Quick start describes.
- [ ] **Never run PerchTail as root** on a source install. It runs as its own user, and `python -m app.backup`, `python -m app.rotate_credential_key` and `python -m app.migrate_database` refuse to run as root.
- [ ] **Keep `DATA_DIR`, `LOG_DIR`, `SCRATCH_DIR` and `BACKUP_DIR` readable by PerchTail's user only.** PerchTail sets them to mode 0700, and the database, the salt and `ssh_known_hosts` to 0600, at every start; don't loosen them.
- [ ] **Put each on its own volume**, so a burst of downloads or logging can't fill the database's disk, and keep `DATA_DIR` on a local disk (SQLite in WAL mode needs one). Scratch holds the content of open files while they are open: put it on a disk only the host can read, encrypted or in memory.
- [ ] **Encrypt the disks** that hold `DATA_DIR`, `BACKUP_DIR` and `SCRATCH_DIR`, if the host's platform allows it.
- [ ] **Keep the host's clock right with NTP.** Sessions, lockouts, password-reset links, TOTP codes, the audit log's timestamps and the scheduled jobs all depend on it. A clock more than about 30 seconds off makes TOTP codes fail.
- [ ] **Keep local sources off** unless you need them: `LOCAL_SOURCE_ROOTS` is empty by default, which disables them. When you set it, list only folders mounted read-only, never `DATA_DIR` or anything holding secrets.
- [ ] **Keep only one PerchTail per `DATA_DIR`.** PerchTail enforces it with a lock file; don't work around it.

## Sources

- [ ] **Use read-only service accounts on the sources**, with access to the log folders only ([source-setup.md](source-setup.md)).
- [ ] **SSH: use `SSH_HOST_KEY_POLICY=strict`** once the sources' host keys are known, and add new hosts' keys to `DATA_DIR/ssh_known_hosts` by hand. The default, `tofu`, pins a key on first connection and refuses a changed one afterwards.
- [ ] **SMB: leave Require signing on**, and turn **Encrypt** on where the servers support SMB 3.
- [ ] **WinRM: use HTTPS** (the default, port 5986) with the server's certificate checked, and a **JEA endpoint** with the read-only role in [examples/jea](examples/jea/README.md), so the account needs no remote shell or administrator rights.
- [ ] **Push agents: keep `wss://`** (never `PERCHTAIL_ALLOW_INSECURE=true` outside a test), set each agent's base path to the log folders only, and set `PERCHTAIL_REMOTE_UPGRADE=false` on hosts that must only be upgraded by hand ([agent/README.md](../agent/README.md)). Run agents of 0.7.0 or later: the server reads no file from an older one, and from 0.7.0 an agent redacts on its own host. Give each agent its own source and token, and regenerate a source's token when its page warns that two agents may be sharing it ([agent-service.md](agent-service.md)).
- [ ] **Rotate source credentials** regularly: Settings → Sources flags credentials older than the age set in Settings → Source defaults (90 days by default), and **Update credentials** changes them on many sources at once.

## Accounts and access

- [ ] **Keep two local super admins as emergency accounts**, each with MFA, their passwords and backup codes stored where your organisation keeps emergency credentials. Use them only when SSO or the other administrators are unavailable, and review their sign-ins (`user.login`) in the audit log.
- [ ] **Use SSO for everyone else**, with MFA required by the identity provider, and map identity-provider groups to roles (Settings → Single sign-on). An SSO user whose groups match no mapping gets "No Access". The provider's issuer must be `https://` and on a public address; leave `SSO_ALLOW_HTTP_LOCALHOST` off outside local development.
- [ ] **Turn on Require MFA for local accounts** (Settings → Sign-in security → Two-factor authentication) if you keep local accounts other than the emergency ones. Administrators must use MFA whatever this setting says.
- [ ] **Give the least access that does the job**: grants on the customers, folders or sources a role needs, `download` only where downloads are needed, `manage_alerts` only to those who should send log lines to a webhook, and global capabilities to as few roles as possible. Separate who reads the audit log (`view_audit_log`) and who reviews access (`view_access_report`) from who administers.
- [ ] **Review access periodically** with Settings → Access review: who holds what, accounts without MFA, and accounts that haven't signed in for a while. Deactivate accounts that are no longer needed: that ends their sessions at once.
- [ ] **Tighten the password policy** (Settings → Sign-in security) if your organisation asks for more than the defaults (12 characters, simple and personal passwords refused).
- [ ] **Leave password recovery by email off** unless users need it; when on, it never serves administrators or SSO accounts.
- [ ] **Give every alert an `https://` webhook URL.** New and edited alerts must use one; an alert saved with `http://` before keeps sending the matching lines unencrypted, with a warning in the alerts list and `alert.webhook_insecure` in the log.
- [ ] **Turn redaction on** (Settings → Redaction) when logs hold secrets or personal data that not everyone who reads them should see, with the rules that fit them, and check it on real files: it is best effort ([redaction.md](redaction.md)).

## Logging and monitoring

- [ ] **Keep the audit retention** at least as long as your organisation requires (365 days by default, at least 90). Only a super admin can shorten it.
- [ ] **Leave Audit folder listings on** (the default) so the audit log shows every folder browsed.
- [ ] **Watch the audit chain's integrity**: it is checked every day (`AUDIT_INTEGRITY_CHECK_INTERVAL_DAYS`), and a failure shows in `/monitoring/health` and `/metrics` and is logged at `ERROR`. Alert on it and on the other events in [monitoring.md](monitoring.md#log-events-worth-alerting-on).
- [ ] **Send the application logs to a separate system** (a syslog server, a SIEM or your log collector), so a copy of every audit line is kept where someone who controls the PerchTail host can't change it ([Sending logs off the host](#sending-logs-off-the-host)).
- [ ] **Leave `LOG_LEVEL` at `INFO`.** `DEBUG` is for troubleshooting only.

### Sending logs off the host

PerchTail writes the same JSON lines to standard output and to `LOG_DIR/perchtail.log`; send one of the two, not both. Every audit entry is also one of those lines, named after its action (`user.login`, `file.open` and so on) with the user, the target, the client address and the session, so the copy off the host records every audited action as it happened (the details of each entry stay in the audit log). What each log holds, and who can read it, is in [data-and-retention.md](data-and-retention.md#logs).

- **Docker**: give the service a log driver that forwards standard output, for example syslog over TLS. Docker keeps a local copy as well, so `docker compose logs` still works.

  ```yaml
  services:
    perchtail:
      logging:
        driver: syslog
        options:
          syslog-address: "tcp+tls://logs.example.com:6514"
          syslog-format: rfc5424
          tag: perchtail
  ```

  The `journald`, `gelf` and `fluentd` drivers work the same way, towards journald, Graylog or Fluentd.
- **Kubernetes**: the cluster's log collector (Fluent Bit, Vector or the platform's own) reads the pod's standard output; nothing to set in PerchTail.
- **Install from source**: systemd's journal keeps standard output; forward it with rsyslog or `systemd-journal-upload`, or have a collector read `LOG_DIR/perchtail.log` and its rotated `.gz` files.
- **SIEM**: each line is a JSON object with `timestamp` (ISO 8601, UTC), `level` and `event`, plus `request_id` on the lines of an HTTP request; audit lines add `user_id`, `target_type`, `target_id`, `client_ip` and `session_id`. A SIEM parses them as JSON, with no custom format.

Push agents log to standard error on their own hosts ([agent-service.md](agent-service.md)): forward those with the host's own tooling.

## Backups and upgrades

- [ ] **Keep the scheduled backup on** (every 24 hours on a new install; Settings → Backups and updates → Backups) and **copy backups off the host** ([backup-and-restore.md](backup-and-restore.md)).
- [ ] **Test a restore** now and then on another host.
- [ ] **Back up before every upgrade** (`python -m app.backup create --reason pre-upgrade`), and apply security releases promptly: [SECURITY.md](../SECURITY.md) says which releases get fixes and how fast.
- [ ] **Verify a release before running it**: the image's signature and provenance, and the agent binaries' signed checksums ([verifying-releases.md](verifying-releases.md)).
- [ ] **Rotate `CREDENTIAL_ENCRYPTION_KEY`** if it may have been exposed, with `python -m app.rotate_credential_key` ([credential-key-rotation.md](credential-key-rotation.md)).

## Security-related settings

| Setting | Default | Hardened value |
|---|---|---|
| `CREDENTIAL_ENCRYPTION_KEY` | `changeme` (refused) | A random key of at least 32 bytes, kept outside the host |
| `CREDENTIAL_KEY_REFUSE_WEAK` | `true` | `true` |
| `CREDENTIAL_SALT_ALLOW_REGENERATE` | `false` | `false`; only for an installation whose salt is lost for good |
| `BACKUP_ENCRYPTION_KEY` | empty (uses `CREDENTIAL_ENCRYPTION_KEY`) | A second random key |
| `PUBLIC_BASE_URL` | `http://localhost:8080` | The `https://` address users use |
| `SESSION_COOKIE_SECURE` | `true` | `true` |
| `SESSION_TTL_HOURS` | `12` | As short as your users' working day allows |
| `SSO_ALLOW_HTTP_LOCALHOST` | `false` | `false` |
| `LOGIN_MAX_ATTEMPTS`, `LOGIN_LOCKOUT_SECONDS` | `5`, `300` | Keep, or lower the attempts |
| `TRUSTED_PROXIES` | `127.0.0.0/8, ::1` | The reverse proxy's address only |
| `PROBE_ALLOWED_NETWORKS`, `MONITORING_ALLOWED_NETWORKS` | empty | The monitoring system's addresses |
| `SSH_HOST_KEY_POLICY` | `tofu` | `strict` |
| `LOCAL_SOURCE_ROOTS` | empty (local sources off) | Empty, or read-only log folders |
| `API_DOCS` | `false` | `false` |
| `UPDATE_CHECK` | `true` | `false` on networks without internet access |
| `AUDIT_INTEGRITY_CHECK_INTERVAL_DAYS` | `1` | `1` |
| `LOG_LEVEL` | `INFO` | `INFO` |
| `MAX_FETCH_MB`, `MAX_DECOMPRESSED_MB`, `REMOTE_READ_TIMEOUT_SECONDS` | `2048`, `2048`, `60` | Lower if your logs are smaller, to bound what one request can take |
