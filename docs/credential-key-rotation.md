# Rotating CREDENTIAL_ENCRYPTION_KEY

`CREDENTIAL_ENCRYPTION_KEY` (see [`.env.example`](../.env.example)) is the root secret for everything PerchTail protects at rest. The rotation covers all of it in one transaction:

- source SSH/SMB/WinRM credentials (`Source.credential_ref`);
- SSO provider settings, including client secrets (`SSOProviderConfig.config`);
- users' MFA secrets (`User.mfa_secret_encrypted`), so MFA keeps working after the rotation;
- the audit log's hash chain, whose HMAC key is derived from the same secret: every row is re-signed under the new key.

The audit chain is verified under the old key first. If it doesn't verify (a row was changed or removed), the rotation stops and nothing is written, because re-signing a broken chain would vouch for whatever broke it: check the audit log's integrity banner and resolve that first. After a successful rotation the audit log has a `credential_key.rotate` entry with the counts.

This guide covers changing that key — after a suspected exposure, as routine hygiene, or whenever you have a reason to — without losing access to what's already encrypted under the old one.

Simply changing the value in `.env` and restarting does **not** work: every already-encrypted row still needs the *old* key to decrypt, and the app now only derives the *new* one (see the KDF's own startup check in `app/crypto.py`). You have to re-encrypt everything first.

## Before you start

- **Make a backup** while PerchTail still runs, and `verify` it ([backup-and-restore.md](backup-and-restore.md)): `docker compose exec -u perchtail perchtail python -m app.backup create`.
- **Stop PerchTail.** A running PerchTail keeps the key it started with, so anything it encrypts during the rotation would end up under the old key after the rotation has moved past it. The rotation command doesn't check that PerchTail is stopped: that's up to you.

## Running it

Run the command as PerchTail's own user, with the same environment as PerchTail. `CREDENTIAL_ENCRYPTION_KEY` must still be the *current* value: that's what everything is decrypted with. The new key comes from `NEW_CREDENTIAL_ENCRYPTION_KEY`, or, when that isn't set, from a prompt (it's never accepted as a command-line option, which would land in shell history and `ps` output).

Docker, in the folder with `docker-compose.yml`:

```bash
docker compose stop perchtail

# Preview only: decrypts everything under the current key and reports what
# would be rotated, writes nothing.
docker compose run --rm --no-deps -u perchtail --entrypoint python perchtail \
  -m app.rotate_credential_key --dry-run

# The real rotation. Both commands prompt for the new key.
docker compose run --rm --no-deps -u perchtail --entrypoint python perchtail \
  -m app.rotate_credential_key
```

`--entrypoint python` keeps the container from starting PerchTail itself; `docker compose run` reads the same `.env`.

Install from source ([install-from-source.md](install-from-source.md)):

```bash
sudo systemctl stop perchtail
sudo systemd-run --pty --wait --uid=perchtail --gid=perchtail \
  -p EnvironmentFile=/etc/perchtail/perchtail.env -p WorkingDirectory=/opt/perchtail/backend \
  /opt/perchtail-venv/bin/python -m app.rotate_credential_key --dry-run
# then the same command without --dry-run; both prompt for the new key
```

Every row is decrypted under the old key and re-encrypted under the new one inside a single transaction — if any row fails to decrypt (wrong old key, or the rotation already ran), nothing is written and the script exits with an error explaining which row failed.

## After it succeeds

Set `CREDENTIAL_ENCRYPTION_KEY` to the new value (in `.env`, or in `/etc/perchtail/perchtail.env` for an install from source) and start PerchTail again. The database now only decrypts correctly under the new key — the old one no longer works, by design.

**Backups made before the rotation still need the old key.** The credentials inside them are encrypted under it, and, unless `BACKUP_ENCRYPTION_KEY` is set, so are the backup files themselves. Keep the old key, labelled with the date it was retired, for as long as you keep those backups, and make a new backup right away so the latest one uses the new key. See [backup-and-restore.md](backup-and-restore.md#the-key).

## Generating a new key

Same as the initial setup in the README's Quick start:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```
