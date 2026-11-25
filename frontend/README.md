# PerchTail frontend

The web interface of PerchTail: a Svelte 5 single-page app in TypeScript, built with Vite. Routing is hash-based (`svelte-spa-router`, for example `#/viewer` and `#/settings/sources`), so the backend serves the built app as plain static files. The viewer's editor is CodeMirror 6.

- `src/routes/` — one component per page (Viewer, Alerts, the Settings pages, My profile, Login).
- `src/lib/` — shared code, with its unit tests next to it (`*.test.ts`).
- `src/lib/components/` — shared components.
- `e2e/` — the Playwright end-to-end tests.

The frontend talks to the backend's HTTP API, which is internal and may change before 1.0.

## Requirements

Node.js 22 (the version the Docker image and CI build with) and npm. Install the dependencies once, from this folder:

```bash
npm ci
```

## Running it in development

The development server needs a backend running on `127.0.0.1:8000`. Set the backend up as described in [CONTRIBUTING.md](../CONTRIBUTING.md), then start it from `backend/` with its virtual environment active:

```bash
cd backend
python -c "import secrets; print(f'CREDENTIAL_ENCRYPTION_KEY={secrets.token_urlsafe(32)}')" >> .env   # once only: the data is encrypted with this key, and a weak key is refused
export SESSION_COOKIE_SECURE=false                       # the dev server is plain HTTP
python -m alembic upgrade head
uvicorn app.main:app --reload --port 8000
```

The backend also reads these settings from a `.env` file in the folder it starts in. By default it keeps its data under `backend/data/`. On the first start it creates the `admin` account and writes its temporary password to `initial-admin-password` in `DATA_DIR` (`backend/data/db/` by default); the `initial_super_admin.seeded` log line names the file, which is deleted once the password is changed. At the first sign-in, `admin` sets a new password and then two-factor authentication, which super admins must use, so keep an authenticator app at hand.

Then, in another terminal, from `frontend/`:

```bash
npm run dev
```

Open the address Vite prints (`http://localhost:5173` by default). The dev server reloads on every change and forwards the backend's paths to `127.0.0.1:8000`; the list of forwarded paths is `apiPrefixes` in `vite.config.ts`. A page that calls a path missing from that list gets the app's HTML instead of the API's answer in development, so add new backend paths there. The proxy also drops the browser's `Origin` and `Referer` headers, because the backend's cross-site request check would otherwise refuse every change sent from the dev server's port.

## Checks and tests

```bash
npm run check      # svelte-check on the app, and tsc on the Node and e2e configs
npm test           # vitest unit tests (src/**/*.test.ts), in jsdom
```

CI runs both, then `npm run build` and `npm audit` at the high level through `.github/scripts/npm_audit.py`, which accepts an advisory only when it is listed, with a reason and an expiry date, in `.github/npm-audit-exceptions.json` (see [CONTRIBUTING.md](../CONTRIBUTING.md)).

### End-to-end tests (Playwright)

```bash
npx playwright install --with-deps chromium   # once per machine
npm run test:e2e
```

`npm run test:e2e` starts its own server through `npm run e2e:server`: it builds the frontend and runs `backend/scripts/run_e2e_server.sh`, which starts a backend on `127.0.0.1:8001` (so it doesn't touch a development backend on 8000) with a throwaway database under `backend/data/e2e/`, deleted on every run, and a seeded admin account. That backend serves the built app itself, without the Vite proxy.

The script also starts real SSH and SMB test servers on ports 2222 and 1445 (`backend/scripts/setup_e2e_test_servers.sh`); the first run installs `openssh-server` and `samba` with `apt-get` and may ask for `sudo`. Set `SKIP_E2E_TEST_SERVERS=1` to skip them: then `sources-ssh.spec.ts`, `sources-smb.spec.ts` and `import-export.spec.ts` (which reads the fixture paths those servers write) fail and the rest runs. WinRM is faked in the e2e backend.

The tests run one at a time, since they change shared settings on one backend. Outside CI, a server already listening on port 8001 is reused. The HTML report is written to `playwright-report/`. `e2e/accessibility.spec.ts` runs axe-core on the main pages and fails on serious or critical WCAG 2.0/2.1 A and AA violations.

## Building

```bash
npm run build
```

This writes the app to `frontend/dist/`. The backend serves that folder at `/` when it exists (`backend/app/main.py`), as the Docker image and an install from source ([docs/install-from-source.md](../docs/install-from-source.md)) do.
