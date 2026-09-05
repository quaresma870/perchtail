import { defineConfig, devices } from '@playwright/test'

// The e2e backend serves the built SPA itself (see app/main.py's static
// mount), same as production behind nginx -- no vite dev server or proxy
// involved, so there's no origin mismatch for OriginCheckMiddleware to
// trip on. Port 8001, not the documented dev port 8000 (CONTRIBUTING.md),
// so this can run alongside a developer's own `npm run dev` backend.
const baseURL = 'http://127.0.0.1:8001'

export default defineConfig({
  testDir: './e2e',
  fullyParallel: true,
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
  // `npx playwright install --with-deps chromium` first.
  webServer: {
    command: 'npm run e2e:server',
    url: `${baseURL}/healthz`,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
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
