# Rotating CREDENTIAL_ENCRYPTION_KEY

`CREDENTIAL_ENCRYPTION_KEY` (see [`.env.example`](../.env.example)) is used to
derive the key that encrypts every stored credential at rest: source
SSH/SMB/WinRM secrets (`Source.credential_ref`) and SSO client secrets
(`SSOProviderConfig.config`). This guide covers changing that key — after a
suspected exposure, as routine hygiene, or whenever you have a reason to —
without losing access to what's already encrypted under the old one.

Simply changing the value in `.env` and restarting does **not** work: every
already-encrypted row still needs the *old* key to decrypt, and the app now
only derives the *new* one (see the KDF's own startup check in
`app/crypto.py`). You have to re-encrypt everything first.

## Before you start

- **Stop the app**, or at least make sure nothing else is writing to a source's
  credential or an SSO provider's config while this runs. A live app process
  keeps its own cached Fernet key (derived from whatever `CREDENTIAL_ENCRYPTION_KEY`
  it started with) for the duration of the rotation window — if it re-encrypts
  something mid-rotation, that row could end up back under the old key after
  this script already moved past it.
- Have a **backup of the database file** before running this, same as before
  any operation that rewrites every credential row. `docker compose stop` and
  copy the volume, or your usual backup process.

## Running it

From inside the container (or a `backend/` checkout with the same `.env`):

```bash
# Preview only -- decrypts everything under the current key and reports what
# would be rotated, writes nothing:
docker compose exec perchtail python -m app.rotate_credential_key --dry-run

# The real rotation. CREDENTIAL_ENCRYPTION_KEY in the environment must still
# be the *current* value (same as any normal startup) -- that's what it
# decrypts everything under. NEW_CREDENTIAL_ENCRYPTION_KEY is the value
# you're rotating to.
docker compose exec -e NEW_CREDENTIAL_ENCRYPTION_KEY='<new-value>' \
  perchtail python -m app.rotate_credential_key
```

If you'd rather not pass the new key as an environment variable in your shell
history, omit `NEW_CREDENTIAL_ENCRYPTION_KEY` and the script prompts for it
interactively (never accepted as a `--flag`, for the same reason).

Every row is decrypted under the old key and re-encrypted under the new one
inside a single transaction — if any row fails to decrypt (wrong old key, or
the rotation already ran), nothing is written and the script exits with an
error explaining which row failed.

## After it succeeds

Update `CREDENTIAL_ENCRYPTION_KEY` in `.env` to the new value and restart the
app. The database now only decrypts correctly under the new key — the old one
no longer works, by design.

## Generating a new key

Same as the initial setup in the README's Quick start:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```
