# Troubleshooting PerchTail

This guide is for administrators: it lists the problems you are most likely to meet with sources, sign-in, server limits and startup, the exact messages PerchTail shows or logs for them, and what to do.

Related guides: [configuration.md](configuration.md) (every setting named here), [upgrade.md](upgrade.md), [admin-guide.md](admin-guide.md), [user-guide.md](user-guide.md), [agent-service.md](agent-service.md), [source-setup.md](source-setup.md), [sso-setup.md](sso-setup.md), [monitoring.md](monitoring.md), [install-from-source.md](install-from-source.md), [backup-and-restore.md](backup-and-restore.md) and the [JEA endpoint](examples/jea/README.md).

The messages below are quoted as PerchTail writes them, so you can search this page for the text on your screen.

## Where to look first

### The server log

PerchTail logs one JSON object per line, to standard output and to `perchtail.log` in `LOG_DIR`.

- Docker: `docker compose logs perchtail`. The file is in `/data/logs` in the container.
- Install from source with systemd: `journalctl -u perchtail`, and the file in `LOG_DIR` (`/var/log/perchtail` in [install-from-source.md](install-from-source.md)).
- Super admins can also read the log in PerchTail itself: the built-in source **PerchTail application logs** in the Viewer reads `LOG_DIR`. No grant gives other users access to it.

Each line has `event` (the event's name, such as `source.status_changed`), `level`, `timestamp` and, for a request, `request_id`. Every response carries the same ID in its `X-Request-ID` header: find it in the browser's developer tools (Network tab) and search the log for it. A request that already has an `X-Request-ID` header of at most 64 letters, digits, dots, underscores or hyphens keeps that value; any other value is replaced with a new ID.

What PerchTail shows in the browser is deliberately short. The library's own error (paths, host keys, internals) goes only to the log, next to the request ID, also for **Test connection** and the folder picker, which give the same message for a closed port, a refused connection and a timeout.

`LOG_LEVEL` sets how much is logged (`INFO` by default; `DEBUG` for more). At `DEBUG`, the protocol libraries (SMB, SSH, WinRM, HTTP) stay at `INFO` while PerchTail itself logs at `DEBUG`. To see their own `DEBUG` messages while debugging a connection, set `LOG_QUIET_THIRD_PARTY=false` for a short time: those messages can include what is read from sources, before redaction, and they make reads slow and the logs large, so PerchTail logs a `logging.third_party_debug` warning at startup while it is set. The file rotates at `LOG_MAX_FILE_MB` or at midnight, and old files are deleted after `LOG_RETENTION_DAYS` or past `LOG_MAX_TOTAL_MB` in all.

[monitoring.md](monitoring.md#log-events-worth-alerting-on) lists the events worth an alert.

### "Can't reach PerchTail"

When the browser shows "Can't reach PerchTail — retrying…", PerchTail itself doesn't answer, or the reverse proxy in front of it doesn't. Source statuses can't be trusted until it answers again. Check that the service runs, and the proxy's own log.

## Source status and background checks

The dot before each source in the Viewer shows its last known status, the same for every user:

- **Reachable**: the last operation or check on the source worked.
- **Failing**: the last one couldn't reach the source. The tooltip gives the reason, such as "Can't reach the source" or "Authentication to the source failed".
- **Not checked yet**: no operation or check has run.
- For a push-agent source: "Agent connected", or "Agent not connected (last seen ...)".

Only errors about reaching the source (unreachable host, credentials, SSH host key, TLS) make it failing. A missing or locked file, or a permission problem on one path, doesn't change the status.

Every operation updates the status: a browse, an open, Tail, the file-name index. Any one that works sets it back to reachable.

PerchTail also checks sources in the background: **Settings → System → Sources → Check sources in the background**, in minutes (10 by default, 0 turns it off). Each check lists the source's base path. Four sources are checked at a time, stalest first. Push-agent sources aren't checked: their status is the live connection.

- A check that takes longer than twice `REMOTE_READ_TIMEOUT_SECONDS` plus 10 seconds is given up, and the source shows "The source didn't answer in time".
- After 3 failed background checks in a row, the source backs off. Its background checks and file-name index passes are skipped for twice the check interval, then twice as long after each further failure, at most 30 minutes, minus a random part of up to a quarter. The log has `source.check.backing_off` with the number of failures.
- The source stays failing until an operation on it works. Browsing it is never blocked by the backoff. **Test connection** on the source page, or the check button next to the source in the Viewer ("Check the connection now"), tries it at once; both are shown to users with **Manage sources** on the source.
- The failure count is kept in the database, so a restart doesn't reset it.

A disabled source isn't contacted at all. Opening it answers "This source is disabled. An admin can turn it back on in its settings." (409, with the code `source_disabled`): the viewer offers no **Try again**, and a tab following a file from it stops Tail.

## Problems by connector

### Messages common to every connector

| Message | HTTP status | What it means |
|---|---|---|
| "Can't reach the source" | 502 | The host doesn't answer, the port is closed, the name doesn't resolve, or the connection stalled. |
| "Authentication to the source failed" | 502 | The username, password or key was refused. |
| "This source has no credential yet: set one in its settings" | 502 | An SSH, SMB or WinRM source with no saved credential, often after a bulk import. |
| "The source account has no read permission for this path" | 403 | The account can't read that file or folder on the host. |
| "Not found on the source" | 404 | The path doesn't exist (any more). |
| "not found" | 404 | The rules hide the path, or a link (the path itself or a folder above it) leads out of the base path or to a file or folder the rules hide. PerchTail doesn't say which; the viewer shows "<file> wasn't found, or is hidden by this source's rules." |
| "The file is locked by another process on the source" | 409 | Windows refused to open the file. |
| "The source's TLS certificate isn't trusted: its issuer is unknown, so add the issuing CA certificate to the source" | 502 | WinRM over HTTPS with a certificate from an unknown CA. |
| "The source returned an error" | 502 | Any other error from the source. The log has the details. |

**Test connection** on the source page lists each base path and answers with one of these messages, followed by the path, for example "Authentication to the source failed (base path: /var/log/myapp)". A source with several folders names the one that fails.

### SSH / SFTP

- **"The server's SSH host key doesn't match the pinned one -- refusing to connect (possible man-in-the-middle). An administrator must verify and re-pin it."** The host presented a different key than the one in `SSH_KNOWN_HOSTS_PATH` (`/data/db/ssh_known_hosts` in Docker). First find out why: a rebuilt server, or something in the middle. Once you trust the new key, delete the host's line from the file (it starts with the host name, or with `[host]:port` for a port other than 22) and keep the file owned by the PerchTail user. PerchTail reads the file on every connection, so no restart is needed. With the default policy, the next connection pins the new key.
- **"The server's SSH host key isn't pinned and the policy is strict"**. `SSH_HOST_KEY_POLICY=strict` refuses hosts that aren't in the file. Add the host's key first: see [source-setup.md](source-setup.md#6-host-key-pinning).
- With the default policy, `tofu`, each new host's key is pinned on first connect and logged as `ssh.host_key_pinned` with its `SHA256:` fingerprint. Compare it with the server's key.
- **"Authentication to the source failed"**: wrong username, password or key, or the key isn't in the account's `authorized_keys`. PerchTail takes Ed25519, ECDSA and RSA keys in OpenSSH or PEM format, without a passphrase. A key with a passphrase is refused when you save the source, update credentials in bulk or import YAML: "Encrypted SSH private keys aren't supported; provide the key without a passphrase". A key PerchTail can't load gives "Authentication to the source failed".
- **"Not found on the source" for the base path, on an account with a `ChrootDirectory`**: the base path is relative to the chroot, usually `/`. **Test connection** says so: "If this account has a ChrootDirectory, Base path is relative to the chroot (usually /)."
- **Timeouts**: the TCP connection must open within 10 seconds. After that, the SSH banner, the login and every SFTP read must answer within `REMOTE_READ_TIMEOUT_SECONDS` (60 by default). A server that stalls fails with "Can't reach the source". Raise the value for a slow link; lower it to fail faster.
- **Rules and letter case**: on SSH sources, include rules are case-sensitive: `**/*.log` doesn't show `APP.LOG`. Exclude rules ignore case everywhere, so `secret/**` also hides `SECRET/`.
- **A link is missing from the listing, or answers "not found"**: a symlink (the file itself or a folder above it) is shown and read only when it leads inside the base path to a file or folder the rules show. Check the rules against where the link leads, not only its name: a link such as `current.log` to a file in a folder an exclude rule hides is hidden too. The audit log records each refusal (`file.open_denied`, `file.download_denied` or `access.denied`) with the reason `link_target_hidden`. The same applies to local-disk and push-agent sources.

### SMB

- **Base path is the share name, not a drive path.** Saving the source refuses the wrong kinds of path:
  - "SMB Base path is a share name, not a drive path like C:\Logs. Share the log folder on the server (see docs/source-setup.md), or use WinRM to read a drive path"
  - "SMB Base path is the share name only -- the server goes in Host, not in the path" (for a path that starts with `\\`)
  - "SMB Base path is the share name, optionally with a folder inside it (e.g. Logs or Logs\App) -- use Browse to pick one" (for an empty path)
- **Administrative shares are refused**: "C$ is an administrative share. Share exactly the log folder instead (see docs/source-setup.md) so the account can't read the whole disk". The same applies to any drive share, `ADMIN$` and `IPC$`. Create a share for the log folder: see [source-setup.md](source-setup.md#2-scope-the-share).
- **"That share doesn't exist on the server. For SMB, Base path is a share name (e.g. AppLogs), not a drive path."** The share name is wrong. **Browse…** next to **Base path** lists the shares the account can see.
- **"Authentication to the source failed"**: PerchTail always authenticates with NTLM. It doesn't use Kerberos, so a missing Kerberos configuration on the PerchTail host doesn't matter, and the `BadMechanismError` ("unable to negotiate common mechanism") that the SMB library gives when it tries Kerberos first doesn't happen. Check the password, that the account isn't locked or expired, and that the server or the domain doesn't restrict NTLM. For a domain account, the SMB library also accepts the username as `DOMAIN\user`.
- **"The source account has no read permission for this path"**: share permissions and NTFS permissions both apply. Give the account read on both: see [source-setup.md](source-setup.md#3-match-the-ntfs-acl-to-the-share).
- **Signing and encryption**: each SMB source has **Require signing** (on by default) and **Encrypt (SMB 3)** (off by default) under **SMB security** in the source editor. A server that can't sign fails to connect while **Require signing** is on; turn it off only for such a server, and the source's page then warns about it. A server that can't encrypt refuses the connection while **Encrypt (SMB 3)** is on.
- **Junctions and symbolic links** inside the share are never listed or followed.
- **Timeouts**: connecting and every SMB request use `REMOTE_READ_TIMEOUT_SECONDS`. A server that stops answering fails after up to about twice that time (smbprotocol first sends an echo and waits as long again). An operator's own `SMB_EXPERIMENTAL_TRANSPORT_RECEIVE_TIMEOUT` takes precedence.

### WinRM and JEA

- **HTTP or HTTPS**: the source's **Scheme** decides, and the port defaults to 5986 for HTTPS and 5985 for HTTP. Over HTTP, NTLM still encrypts the messages but the server isn't authenticated: the source page warns about it. Use HTTPS.
- **Certificate errors** (HTTPS):
  - "The source's TLS certificate isn't trusted: its issuer is unknown, so add the issuing CA certificate to the source": paste the CA certificate (PEM) into **CA certificate (PEM, optional)** in the source editor. It is trusted for that source only.
  - "The source's TLS certificate is for a different name: enter the host as it appears on the certificate, or reissue the certificate for this host": the **Host** must match a name on the certificate.
  - "The source's TLS certificate has expired"
- **"Authentication to the source failed"**: PerchTail authenticates with NTLM. Check the username and password.
- **With a JEA endpoint** (the **JEA endpoint (optional)** field set):
  - "The JEA endpoint wasn't found on the server": the name doesn't match the registered endpoint (`PerchTail` by default). Run `Register-PerchTailJea.ps1` on the host: see the [JEA endpoint](examples/jea/README.md).
  - "The WinRM account isn't allowed to use the JEA endpoint (see docs/examples/jea)": the account isn't in the group given to `-Group` when the endpoint was registered.
  - A base path outside the endpoint's allowed folders answers "not found". The base path must be one of the folders given to `-Roots`, or inside one. **Browse…** offers them.
- **Without a JEA endpoint** (a WinRS shell): "The WinRM account isn't allowed to run commands on the server (see docs/source-setup.md, WinRM)". The account needs remote-shell rights: see [source-setup.md](source-setup.md#2-create-a-dedicated-account). Use the JEA endpoint instead where you can.
- **"WinRM Base path must be an absolute Windows path on a drive, e.g. D:\Logs\App"** when saving: WinRM takes a drive path only. UNC paths (`\\server\share`) and device paths (`\\?\`, `\\.\`) are refused, since the WinRM host would reach out to another machine with its own credentials.
- **"This source's Base path isn't a path on a drive (e.g. D:\Logs): edit the source to correct it"** when browsing or opening a WinRM source: it was saved, before 0.7.0, with a UNC or device path, which PerchTail no longer reads (the log has `winrm.base_path_not_on_drive`). Edit the source and change its Base path or folders to a path on a drive of the WinRM host, or read the share through an SMB source.
- **Opening or downloading a large file is slow**: WinRM sends a file as base64 text in WS-Management responses of up to 500 KB, so a file of a few hundred MB takes minutes, where SMB or the agent take seconds. Use an SMB source or the agent for hosts with large logs, or **Tail the end** in the viewer. See [Large files](source-setup.md#4-large-files).
- **`winrm.envelope_fallback`** in the log (WARNING, with the `source_id`): the server refused envelopes of 512000 bytes, because its `MaxEnvelopeSizekb` is below 500, and PerchTail asked again with 153600. Reads still work, more slowly. Run `winrm get winrm/config` on the server as an administrator to check the value, and `winrm set winrm/config '@{MaxEnvelopeSizekb="500"}'` to restore the default. At `LOG_LEVEL=DEBUG`, each read through a WinRS shell logs `winrm.fetch_done` with the bytes read, the number of responses, the time taken and the envelope size.
- Use **Test connection** after each change.

### Local disk

- Local-disk sources are off until the operator sets `LOCAL_SOURCE_ROOTS` (comma-separated absolute folders) in the server's environment. Saving a source outside those folders fails with "Local-disk sources must point under a directory the server operator has allowed via LOCAL_SOURCE_ROOTS".
- The paths are on the PerchTail server. With Docker, they are paths inside the container: mount the host folder into the container first (read-only), then list the mount point in `LOCAL_SOURCE_ROOTS`. See [source-setup.md](source-setup.md#local-disk-directories-on-the-perchtail-host-itself).
- The check runs again on every access. If `LOCAL_SOURCE_ROOTS` was narrowed after a source was created, opening it answers "This local-disk source is outside the server's allowed directories" (403).
- Links that lead outside the base path, or to a file or folder the rules hide, are left out, as on SSH sources.

### Push agent

See [agent-service.md](agent-service.md) for installing and running the agent.

- **"This source's agent must be updated before its files can be read. Ask an administrator to update it from Settings → Agents."** (409): the agent is older than 0.7 and doesn't tell the server where links lead (it doesn't announce `links/1`). It stays connected and its folders can still be browsed, but opening, Tail, downloads, folder ZIPs and archives are refused, audited with the reason `agent_update_required`; alerts on its files wait where they are (the log has `alert.agent_update_required`). **Settings → Agents** shows "Update required: its files can't be read until it's updated" under the agent. Agents older than 0.7 can't be upgraded from the portal: upgrade each one by hand once (see [agent-service.md](agent-service.md)).
- **`agent.redaction_fallback` in the log**: redaction is on and the agent announces `links/1` but not `redaction/1`, so the server redacts what it sends. An agent older than 0.7 doesn't get this line: it announces neither, and its requests are refused (the 409 above), so nothing is read from it. Upgrade the agent (see [redaction.md](redaction.md#where-content-is-redacted)).
- **"The agent for this source isn't connected"**: the agent isn't running, can't reach the server, or the server refused it. Check the agent's log (`journalctl -u perchtail-agent`, or the wrapper's log file on Windows):
  - `dial: websocket: bad handshake`: the server or the reverse proxy didn't accept the WebSocket. The server refuses a token it doesn't know (wrong or regenerated) and a disabled or deleted source. A reverse proxy without the WebSocket headers on `/agent/connect`, or a wrong path in `PERCHTAIL_SERVER_URL`, gives the same error. See [agent-service.md](agent-service.md#reverse-proxy).
  - `x509: certificate signed by unknown authority`: the host doesn't trust the server's certificate. Add the CA to the host's trust store.
  - `dial: ... connection refused` or a timeout: a firewall or outbound proxy. The agent honours `HTTPS_PROXY`.
- **The token was regenerated**: PerchTail closes the agent's connection, with the reason "agent token regenerated", and refuses the old token from then on. Put the new token in the agent's configuration and restart it.
- **Two agents with the same token**: each new connection replaces the previous one ("replaced by a newer connection for this source"), so the two agents keep pushing each other off. Each replacement is audited as `agent.connection_replaced` with both addresses, and when the addresses differ the source's page warns that two agents may be using its token, until an agent next connects without replacing another connection. Give each host its own source, and regenerate the token if one of the agents isn't yours.
- **The agent doesn't start**:
  - `PERCHTAIL_SERVER_URL uses unencrypted ws://, ...`: use `wss://`, or set `PERCHTAIL_ALLOW_INSECURE=true` on a network you trust.
  - `PERCHTAIL_SERVER_URL must start with wss:// (got "https")`: the URL is `wss://<host>/agent/connect`, not the web address.
  - `PERCHTAIL_REMOTE_UPGRADE must be true or false (got "...")`, or a required variable is missing: fix the environment. See [agent-service.md](agent-service.md#2-configuration).
  - `PERCHTAIL_BASE_PATH "..." doesn't exist`, `... is not a folder`, `... can't be read` or `... is not readable by the agent's account`: the agent checks its base path at startup and stops with a non-zero exit code. Fix the path or the account's permissions.
- **"The agent didn't answer in time"**: the agent didn't answer a command within 30 seconds, or stopped sending a file for 30 seconds. Check the host's load and disk. An agent older than 0.7.0 also gets this when it must read a long way into a large archive before it has anything to send (a file near the end of a big `.tar.gz`, redacted on the agent), or must read a big `.tar.gz` through to list it (expanding it, or downloading it whole, on an agent that redacts): upgrade it, since from 0.7.0 the agent tells the server it is still working (`progress/1`, [agent-service.md](agent-service.md#what-the-agent-tells-the-server)).
- **"File is too large once decompressed (MAX_DECOMPRESSED_MB)" for an archive or `.gz` file that is over `MAX_FETCH_MB` as stored**, on a source whose agent redacts: an agent older than 0.7.0 doesn't say which limit it passed, and with both limits at the same value the server can't tell them apart. The refusal is right; upgrade the agent for the right message.
- **"Not found on the source" for every path**: `PERCHTAIL_BASE_PATH` was removed or replaced after the agent started. Restart the agent: it checks the path at startup.
- **"The source account has no read permission for this path"**: the agent's account can't read that path.
- **Rules ignore letter case on a Windows agent** (as NTFS does), but include rules are case-sensitive while **Agent OS** shows "not reported yet": agents older than 0.5.1 don't report their OS.
- **A folder shows fewer entries than it holds**: an agent lists at most 50,000 entries of a folder, or fewer once the list nears 8 MiB, and the rules are applied afterwards on the server. The explorer then says "The source listed only part of this folder; showing the N entries it listed."
- **Upgrades from the portal fail**: **Settings → Agents** shows the reason in **Last result**. Common ones: the host set `PERCHTAIL_REMOTE_UPGRADE=false`; the agent can't write to its binary's folder; the agent is a local build ("this agent's version is not a release"); the host can't download the binaries; the service doesn't restart the agent after it exits with code 3 on Windows (**Didn't come back** after 10 minutes). An agent older than 0.7 shows **Too old to upgrade remotely**: upgrade it by hand once. See [agent-service.md](agent-service.md#7-upgrading).

## Sign-in problems

### "Too many failed login attempts. Try again later."

PerchTail counts failed sign-ins two ways, and either one can lock. The answer is HTTP 429 with a `Retry-After` header. A wrong MFA code counts as a failed attempt too.

- **Per username**: after `LOGIN_MAX_ATTEMPTS` failed attempts in a row (5 by default) for one username, that username is locked for `LOGIN_LOCKOUT_SECONDS` (300 by default), from every address. Someone guessing a user's password also locks the real user out for that time.
- **Per address**: after `LOGIN_MAX_ATTEMPTS` failed attempts (5 by default, the same limit as per username) from one client address, whatever the usernames, that address is locked for `LOGIN_LOCKOUT_SECONDS`. Other addresses keep signing in. IPv6 addresses are counted by their /64. Everyone behind one NAT or one shared proxy has the same address, so five failures by anyone there lock sign-in for all of them for `LOGIN_LOCKOUT_SECONDS`. Behind a reverse proxy, set `TRUSTED_PROXIES` so the address is the client's and not the proxy's ([audit-events.md](audit-events.md#client-address)). When a proxy is missing from it, the log has a `client_address.untrusted_forwarded_header` warning with the proxy's address, and `/monitoring/health` reports `trusted_proxies_suspect: true` ([monitoring.md](monitoring.md)).
- **Emergency accounts**: for an active local super admin with MFA on, the per-username count is kept per address too, so failed attempts from one address never lock it out of another. Its MFA is what protects its password from guessing spread over many addresses. A super admin that hasn't set MFA up yet (for example right after upgrading) is locked per username, from every address, like any other account: set up MFA on each super admin soon after upgrading.
- Lockouts are kept in memory. They end on their own, and a restart of PerchTail clears them all.
- A successful sign-in clears the username's count, not the address's.
- The audit log records failed sign-ins and each lockout (`user.locked_out`, with its `scope`).

Changing your own password, your email or your MFA settings asks for your password again. Those attempts share the sign-in count above and lock the same way, answered with HTTP 429 and "Too many failed attempts. Try again later."

### Forced password change and expired passwords

- A new local account, and an account whose password an administrator reset, must set a new password at the first sign-in. Until then, everything except signing in and changing the password answers "Password change required" (403), and PerchTail shows **Set a new password**.
- When the password policy has **Expire after N days** set (Settings → System), an expired password signs in normally and then asks for a new one, the same way.

### MFA

- After the password, the sign-in page asks for "the 6-digit code from your authenticator app, or a backup code".
- **Set up two-factor authentication** right after signing in, and nothing else works: the account must use MFA (a super admin, a role with any global capability, or any local account while **Require MFA for local accounts** is on in **Settings → System → Two-factor authentication**) and hasn't set it up. Until it does, the API answers every other request with 403 "Set up two-factor authentication to continue" (`mfa_setup_required`), and each such sign-in is audited as `user.mfa_required`. The user sets it up on that page with an authenticator app; SSO accounts aren't affected.
- "Invalid authentication code": the code was wrong or used already. The server tolerates one 30-second step of clock difference either way: if codes keep failing, check the clock of the phone and of the PerchTail server.
- A user who lost the authenticator can sign in with one of their backup codes, then turn MFA off and on again under **My profile** (this asks for their password and another backup code). Each backup code works once. An account that must use MFA is taken back to **Set up two-factor authentication** as soon as it's off.
- Administrators can't turn off another user's MFA in this version. Resetting the password doesn't remove MFA.

### Sign-in seems to work, then you're back at the sign-in page

With `SESSION_COOKIE_SECURE=true` (the default), the session cookie has the `Secure` flag, and browsers drop it over plain `http://`. The sign-in succeeds, but the next request has no cookie, so PerchTail asks you to sign in again, with no error.

- Serve PerchTail over HTTPS, behind a reverse proxy that terminates TLS: see the [README](../README.md#deployment-reverse-proxy-nginx--tls).
- For a local test over `http://localhost` only, set `SESSION_COOKIE_SECURE=false`.

The same flag applies to the cookie that SSO sign-in uses, so SSO fails over plain HTTP too.

### "Cross-site request blocked" (403)

For every change (POST, PUT, PATCH, DELETE), PerchTail compares the host and port in the browser's `Origin` header (or `Referer`) with the request's `Host` header, and refuses the request with 403 "Cross-site request blocked" when they differ. Signing in is a change, so this can block sign-in too.

Behind a reverse proxy, this happens when the proxy doesn't pass the original `Host`:

- In nginx, set `proxy_set_header Host $host;`. Without it, nginx sends the address of the upstream (for example `127.0.0.1:8080`), which never matches.
- If users reach PerchTail on a port other than 443 (for example `https://perchtail.example.com:8443`), `$host` drops the port while the browser's `Origin` keeps it. Use `proxy_set_header Host $http_host;` instead.

The scheme isn't compared, so a proxy that terminates TLS and talks plain HTTP to PerchTail is fine.

### SSO

When SSO sign-in fails, whether PerchTail can't start it (the first four rows below) or it fails after the identity provider, the browser comes back to the sign-in page, which shows "SSO sign-in failed — please try again or use your local account." More than 20 attempts a minute from one address get 429 "Too many sign-in attempts. Try again later." The reason is only in the server log. Local accounts keep working alongside SSO.

| Log event | Cause | What to do |
|---|---|---|
| `sso.login.insecure_idp_url` | The issuer, or an endpoint its discovery document names, isn't `https://` (for example a provider saved before 0.7.0 with an `http://` issuer). | Change the **Issuer URL** in **Settings → SSO** to the provider's `https://` address. `SSO_ALLOW_HTTP_LOCALHOST=true` allows `http://` on a loopback address, for local development only. |
| `sso.login.issuer_mismatch` | The discovery document's `issuer` isn't the configured issuer (a trailing slash aside). Audited as `user.sso_login_failed` with reason `issuer_mismatch`. | Set the **Issuer URL** to the exact `issuer` value the provider publishes; **Test connection** checks it. |
| `sso.login.discovery_failed` | PerchTail can't get the identity provider's discovery document: the host doesn't answer, or it resolves to a private, loopback or link-local address, which PerchTail refuses (the same address policy as alert webhooks, with no redirects followed). | Check the **Issuer URL** in **Settings → SSO**, DNS and outbound access from the PerchTail host. The identity provider must be on a public address; `SSO_ALLOW_HTTP_LOCALHOST=true` also lets a loopback host through, for local development only. |
| `sso.login.config_unreadable`, `sso.callback.config_unreadable` | The saved client secret can't be decrypted with the current key (`CREDENTIAL_ENCRYPTION_KEY` or the salt changed outside the rotation tool). Audited with reason `secret_unreadable`. | Enter the client secret again in **Settings → SSO**. |
| `sso.callback.idp_error` | The identity provider sent back an error, or no code. | Read the `error` field. A common one is a redirect URI the provider doesn't know: see below. |
| `sso.callback.state_cookie_mismatch` | The browser came back without the cookie PerchTail set when the sign-in started. | Users must open PerchTail at the same address as `PUBLIC_BASE_URL`: the cookie set on one host name isn't sent to another. Over plain HTTP with `SESSION_COOKIE_SECURE=true`, the cookie is dropped. |
| `sso.callback.invalid_state` | The sign-in took longer than 10 minutes, or the state couldn't be read. | Start the sign-in again. |
| `sso.callback.issuer_mismatch` | When the callback reads the discovery document, its `issuer` isn't the configured issuer. Audited as `user.sso_login_failed` with reason `issuer_mismatch`. | As for `sso.login.issuer_mismatch`. |
| `sso.callback.login_failed` | The code exchange or the ID token check failed. | Read the `error` field: client ID or secret, audience, issuer, clock, signature algorithm. See [sso-setup.md](sso-setup.md#troubleshooting). |
| `sso.callback.no_access_role_missing` | The built-in **No Access** role is missing. | It is created at startup: check the startup log. |

The redirect URI is always `PUBLIC_BASE_URL` followed by `/auth/sso/callback`. Register exactly that value at the identity provider, and set `PUBLIC_BASE_URL` to the external `https://` address users type, without a trailing slash. After the callback, PerchTail sends the browser to `PUBLIC_BASE_URL` as well.

An SSO user whom no group mapping matches gets the **No Access** role at every sign-in (audited as `user.sso_role_synced` with reason `no_matching_mapping`), and sees no sources until a mapping matches their groups. A role given in **Settings → Users** lasts only until their next sign-in. The last active super admin is never demoted this way: the log then has `sso.role_sync_kept_last_super_admin`.

## Server limits and disk space

### 507: scratch space or disk full

Every file that's opened, downloaded or zipped is fetched into scratch space (`SCRATCH_DIR`) first. Before a new fetch, PerchTail checks the space and refuses it with HTTP 507 when there's not enough:

- "The server's scratch space is full (SCRATCH_MAX_GB) with files other users have open. Try again once some are closed." Scratch is at `SCRATCH_MAX_GB` (5 by default) even after removing files nobody has open. The log has `scratch.full`.
- "The server's disk is almost full (less free space than SCRATCH_MIN_FREE_GB / SCRATCH_MIN_FREE_PERCENT), so nothing more can be fetched right now. An administrator needs to free some space." The filesystem under `SCRATCH_DIR` is down to the larger of `SCRATCH_MIN_FREE_GB` (1) and `SCRATCH_MIN_FREE_PERCENT` (5). The log has `scratch.disk_low`.

Free space on that disk, raise `SCRATCH_MAX_GB`, or move `SCRATCH_DIR` to a disk of its own, apart from the database and the logs. Files left in scratch are deleted when they're closed, after `SCRATCH_IDLE_SECONDS` without use (300 by default), and when PerchTail starts.

### 413: file too large

- "This file is N MB, over this server's M MB limit (MAX_FETCH_MB)." (or "This folder is ..." for a folder ZIP): the file, or the folder's total, is over `MAX_FETCH_MB` (2048 by default). Expanding a `.zip` or `.tar.gz` in the explorer reads it to list it, so an archive over the limit gets this answer when it is expanded as well as when it is opened or downloaded (a single file on a local source is read in place and has no such limit).
- "File is too large once decompressed (MAX_DECOMPRESSED_MB)": a `.gz` file, or a member of a `.zip` or `.tar.gz`, decompresses to more than `MAX_DECOMPRESSED_MB` (2048 by default).

Raise the limit only if scratch space (and memory, for WinRM and agent sources) can take it. For a big log, **Tail the end** in the viewer opens only its last part.

### 503: busy, try again

PerchTail limits how much connector work runs at once: listings, opens, Tail checks, downloads, folder ZIPs and archive listings. A request that finds no free slot is answered right away with HTTP 503 and `Retry-After: 5`, and the explorer and the viewer offer **Try again**:

- "PerchTail is busy with other listings and downloads right now. Try again in a few seconds."
- "This source is busy with other requests right now. Try again in a few seconds."
- "You already have as many downloads and folder ZIPs in progress as this server allows. Wait for one to finish, then try again."

The limits are in **Settings → System → Limits**: **Listings and downloads at once** (16 by default), **At once on one source** (4) and **Downloads per user** (2). The log has `limits.busy` with the `scope`, and `/monitoring/health` shows what's in use (see [monitoring.md](monitoring.md)). Raise a limit if users see these messages often and the server and the sources have room.

### Listing truncated

A folder or an archive shows at most **Entries per listing** entries (**Settings → System → Limits**, 5000 by default, 100 to 20,000): a folder keeps its folders first, an archive its first members by path. The explorer then says "Showing the first N entries; narrow with the search." The search finds files beyond that limit through the file-name index.

A `.tar.gz` or `.tgz` is read in one pass, up to 50,000 entries (files, folders and links together) and 8 MiB of names. A bigger one says "This archive has more entries than were read; showing the N read." under it (or the message above, when what was read still fills **Entries per listing**), and its members past that point answer "not found". Its whole download is refused when the source has an exclude rule, and its redacted download is always refused. Split such archives into smaller ones. An archive with more than 1 MiB of headers for one entry, or of global pax headers in all, or with headers chained deeper than PerchTail follows, can't be read.

### Archive members and folder ZIPs refused

- "This archive member is encrypted or compressed with a method PerchTail can't read, so it can't be opened." (409): a `.zip` member that is encrypted, or compressed with a method PerchTail can't read, to open or to download. Audited as `file.open_denied` or `file.download_denied` with the reason `member_encrypted` or `member_unsupported_compression`. On a push agent that reads archives on its host (redaction on), an encrypted member gets the same answer; a member compressed with a method the agent can't read answers "The source returned an error".
- "This archive can't be read: it is damaged, or it isn't really a .zip or .tar.gz file." (409): a `.zip`, `.tar.gz` or `.tgz` that can't be read as one, when it is expanded in the explorer or a file inside it is opened or downloaded; a `.tar.gz` whose headers are too large, or chained deeper than PerchTail follows, too. A file inside it is audited as `file.open_denied` or `file.download_denied` with the reason `archive_unreadable`, and the log has `archive.unreadable` with the error type. On a push agent that lists and reads archives on its host (redaction on), the agent says it can't read the archive, with the same answer, audit and log. A `.tar.gz` that ends before its first whole file once decompressed (a few bytes, nothing at all, or only headers such as a long file name or a global header) can't be read either, and one that holds only its end-of-archive blocks is an empty archive; a `.zip` whose index is intact but one of its files is damaged still expands, and only that file is refused. Downloaded whole, it is sent as it is, unless the source has an exclude rule or downloads are redacted (below).
- "This compressed file can't be read: it is damaged, or it isn't really a .gz file." (409): a `.gz` file opened or downloaded on its own that can't be decompressed. Audited as `file.open_denied` or `file.download_denied` with the reason `archive_unreadable`.
- "This archive can't be downloaded whole: open or download its files one by one." (403): the archive holds a file an exclude rule hides (files no rule matches don't count), or, when the source has an exclude rule, it can't be read as an archive, its listing was cut short or it holds another archive. With redacted downloads, a whole archive is rebuilt instead, and the same message means it couldn't be read or rebuilt (a `.tar.gz` whose listing was cut short, for example).
- "This folder is nested too deeply for a ZIP; narrow it to a subfolder" and "This folder has more entries than a ZIP can list; narrow it to a subfolder" (409, or 403 for a redacted folder ZIP): the folder has folders 32 or more levels down, or a push agent cut a listing inside it short. Audited as `file.download_denied` with the reason `too_deep` or `listing_truncated`.

### Redaction

With redaction on ([redaction.md](redaction.md)), content redaction can't handle is refused rather than sent in clear:

- "This file is in an encoding redaction doesn't support (UTF-32), so it can't be shown." (409, `redaction_unsupported_encoding`). Content that turns UTF-32 part way shows `[redacted: unsupported encoding]` from there instead.
- "This file is in a compressed format redaction doesn't support, so it can't be shown." (409, `redaction_unsupported_format`): bzip2, xz, zstd, 7z or rar content, or gzip or zip content in a file whose name doesn't say so (such as a rotated `app.log.1`).
- "This compressed file can't be read, so it can't be redacted." (409, `redaction_unreadable`): a `.gz` file inside an archive that can't be read as gzip, opened or downloaded on its own. Audited with the reason `redaction_unreadable`.
- "The redaction rules can't be applied. An administrator needs to check them." (500): a stored rule can't be compiled. The log has `redaction.rules_invalid` with the source and the error; fix or delete the rule in **Settings → Redaction** or in the source's **Redaction** section.

## Startup failures

PerchTail refuses to start in these cases. The reason is in the service's output (`docker compose logs perchtail` or `journalctl -u perchtail`). The Docker image and the systemd unit in [install-from-source.md](install-from-source.md) run `alembic upgrade head` before the server, so the lock and database checks can also stop that step, with the same messages.

- **Insecure key**: "CREDENTIAL_ENCRYPTION_KEY is still the insecure default ('changeme') -- refusing to start." Event `startup.insecure_credential_encryption_key`. Set a real `CREDENTIAL_ENCRYPTION_KEY`. On an existing installation, use the key it was set up with: every stored credential is encrypted with a key derived from it. To change the key, see [credential-key-rotation.md](credential-key-rotation.md).
- **Weak key, when refused**: "CREDENTIAL_ENCRYPTION_KEY is too weak (...) and CREDENTIAL_KEY_REFUSE_WEAK is true -- refusing to start." Event `startup.weak_credential_encryption_key` at `CRITICAL`. Without `CREDENTIAL_KEY_REFUSE_WEAK=true`, a weak key (shorter than 32 bytes once decoded, or less than 128 bits of estimated entropy) only logs the same event as a warning, with the reasons, and **Settings → System** shows "CREDENTIAL_ENCRYPTION_KEY is weaker than recommended." at the top. Generate a new key and change to it with `python -m app.rotate_credential_key` ([credential-key-rotation.md](credential-key-rotation.md)): replacing the value alone makes the stored credentials unreadable.
- **Missing salt**: "CREDENTIAL_SALT_PATH (...) is missing, but the database already holds data encrypted with a key derived from it. ..." Event `startup.credential_salt_missing`. Restore the original `credential_salt` file from a backup (see [backup-and-restore.md](backup-and-restore.md)). Only if it's lost for good, set `CREDENTIAL_SALT_ALLOW_REGENERATE=true`: every stored credential, SSO secret and MFA secret then has to be entered again.
- **Instance lock held**: "another PerchTail instance is using DATA_DIR (...): host=..., pid=..., version=..., started_at=.... Only one instance may run per DATA_DIR; stop the other one first." Event `startup.instance_lock_held`. Another PerchTail process, or a migration, is using the same `DATA_DIR`. Stop it. The lock ends with its process, so a crash never leaves a stale lock behind.
- **Database migrated by a newer version**: "the database is at revision ..., which PerchTail ... doesn't know: it was migrated by a newer version. Run that version, or restore a backup taken before the upgrade." Event `startup.database_newer_than_app`. This happens when you start an older version after an upgrade. See [upgrade.md](upgrade.md) and [backup-and-restore.md](backup-and-restore.md#rolling-back-an-upgrade).
- **Bad network list**: `PROBE_ALLOWED_NETWORKS: 'x' is not an IP address or CIDR block` (or `MONITORING_ALLOWED_NETWORKS: ...`). The settings are read before logging starts, so this appears as a Python traceback, not as a JSON line. Fix the entry: a comma-separated list of addresses or CIDR blocks. Any other invalid setting, such as an `SSH_HOST_KEY_POLICY` other than `tofu` or `strict`, stops PerchTail the same way.
- **Test hooks set**: "PERCHTAIL_TEST_PATCH_MODULE is set without PERCHTAIL_TEST_MODE=true; ...", or, in the Docker image, "PERCHTAIL_TEST_PATCH_MODULE and PERCHTAIL_TEST_MODE are for the tests only; remove them from the environment." Remove both variables: they are for the end-to-end tests only.

Not fatal, but worth fixing: `scratch.leftovers_not_cleared` means `SCRATCH_DIR` is a filesystem root or holds the database or the logs, so PerchTail didn't empty it at startup. Give scratch a folder of its own.

## FAQ

### Why isn't anything cached?

By design. Every open fetches the file fresh from the source into scratch space, and the copy is deleted when it's closed or after `SCRATCH_IDLE_SECONDS` without use. Nothing is mirrored or synced ahead of time, so what you see is always the current file. The file-name index keeps only names, sizes and times, never content.

### Why does a file reload whole when I Tail it?

Tail normally reads only what was added since the last poll. It reloads the whole file instead:

- for a `.gz` file or a file inside an archive, which the server can't read from an offset (it answers a Tail poll with 409 `follow_unsupported`): the tab switches to reloading the whole file without a message, and the **Tail** button's tooltip then says it "reloads the whole file (compressed files and archive members)";
- once, when the file was rotated or truncated: "<file> was rotated or truncated: reloaded from the start."

Any other refusal of a Tail check is shown as the server sends it, such as "This source's agent must be updated before its files can be read…". When it won't change at the next check (a disabled source, an agent to update, content redaction can't read), Tail stops, the tab keeps what it had, and the message ends with "The tab keeps what it had; Tail stopped."; a file locked on the source is tried again at the next check.

### Why does a user see no sources?

- The user's role has no grant on any customer, folder or source. The Viewer then says "No sources visible to your account." Check the role in **Settings → Users** and its grants in **Settings → Roles**.
- New SSO users that no group mapping matches get the **No Access** role.
- Disabled sources don't appear in the Viewer.

A user who sees the source but no files: the source has no rules. A source with no rules shows nothing, by design; its page says "No rules yet — nothing is visible until you add an include rule." Add an include rule such as `**/*.log`.

### Why is a file or folder missing from the listing?

The rules hide it, it is a link (or is reached through one) that leads out of the base path or to something the rules hide, its name contains a control character, or the folder has more entries than **Entries per listing**.

### Why did the viewer turn a severity indicator off?

The viewer says "A severity indicator took too long to match and is turned off until you reload the page:" followed by the pattern. Indicators are matched in the browser, apart from the page, on the first 2000 characters of each line; one that takes more than a second in all over the lines in view, or over the whole file for **Previous problem** and **Next problem**, is turned off in that browser tab until the page is reloaded, and the others keep working. Make the pattern cheaper in **Settings → Severity indicators**: anchor it, avoid nested repetition such as `(a+)+`, or match a fixed word instead. New patterns are saved only in the rules' RE2 syntax (no backreferences or lookaround) and up to 1000 characters; one saved before with either, or longer, keeps working and can still be switched off, given another level or switches, or deleted; only a change to its pattern must follow them.

### How do I reset a password if someone is locked out?

There's no command-line tool for this in this version. What exists:

- A lockout after failed attempts ends by itself after `LOGIN_LOCKOUT_SECONDS`, or at once when PerchTail restarts.
- A user of a local account who isn't an administrator can reset it with **Forgot password?** on the sign-in page, when a super admin has turned on password recovery (**Settings → System → Password recovery**, which needs **Settings → Email**). See [user-guide.md](user-guide.md#forgotten-password).
- A user with **Manage users** can reset another user's password in **Settings → Users**: PerchTail shows a temporary password once, the user must change it at the next sign-in, and the user is signed out everywhere. A delegated administrator can only reset accounts whose role is within their authority, and only a super admin resets an account whose role has **Manage users** or **Manage roles**. An SSO account's password can't be reset (409).
- A user who lost their authenticator signs in with a backup code.
- The break-glass super admin with a random password is created only on the very first start, when the database has no users. Keep a second super admin account, so one can reset the other.
- SSO accounts have no password in PerchTail: they sign in through the identity provider, and their password and MFA are reset there.

### Where are the agent's logs?

On the agent's host, not in PerchTail: `journalctl -u perchtail-agent` with systemd, or the file the service wrapper writes on Windows (`AppStderr` with NSSM). See [agent-service.md](agent-service.md).
