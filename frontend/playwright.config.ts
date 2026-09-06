import { defineConfig, devices } from '@playwright/test'

// The e2e backend serves the built SPA itself (see app/main.py's static
// mount), same as production behind nginx -- no vite dev server or proxy
// involved, so there's no origin mismatch for OriginCheckMiddleware to
// trip on. Port 8001, not the documented dev port 8000 (CONTRIBUTING.md),
// so this can run alongside a developer's own `npm run dev` backend.
const baseURL = 'http://127.0.0.1:8001'

export default defineConfig({
  testDir: './e2e',
  // Deliberately sequential, not parallel: most specs beyond the original
  // login/viewer/sessions trio exercise shared, mutating admin state
  // (sources, roles, users, alerts, deployment-wide system settings)
  // against one backend process -- two specs racing on that would be
  // genuinely flaky, not just slow. Each spec still names its own unique
  // fixtures (timestamped names) and cleans them up so re-runs stay clean.
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  reporter: 'html',
  use: {
    baseURL,
    trace: 'on-first-retry',
  },
  // Sandboxed dev containers used to build this suite pre-install Chromium
  // under PLAYWRIGHT_BROWSERS_PATH -- deliberately not hardcoded here.
  // Everywhere else (CI, another contributor's machine) needs its own
  // `npx playwright install --with-deps chromium` first. Timeout is
  // generous (not just the frontend build) -- webServer also provisions
  // real sshd/smbd test servers (backend/scripts/setup_e2e_test_servers.sh),
  // including an apt-get install on a first run with nothing cached.
  webServer: {
    command: 'npm run e2e:server',
    url: `${baseURL}/healthz`,
    reuseExistingServer: !process.env.CI,
    timeout: 300_000,
  },
  projects: [
    { name: 'setup', testMatch: /.*\.setup\.ts/ },
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'], storageState: 'e2e/.auth/admin.json' },
      dependencies: ['setup'],
    },
  ],
})
