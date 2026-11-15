import { defineConfig } from 'vitest/config'
import { svelte } from '@sveltejs/vite-plugin-svelte'

const backendTarget = 'http://127.0.0.1:8000'

// In production the backend serves the built app and the API from one
// origin. In `npm run dev`, Vite serves the app and these top-level API
// paths are proxied to the backend, so the app calls them without a `/api`
// prefix rewrite. backend/tests/test_dev_proxy_prefixes.py checks that this
// list names every top-level path the backend's routes use.
const apiPrefixes = [
  '/admin',
  '/agent',
  '/agent-upgrades',
  '/alerts',
  '/audit',
  '/auth',
  '/customers',
  '/files',
  '/folders',
  '/healthz',
  '/metrics',
  '/monitoring',
  '/readyz',
  '/redaction-rules',
  '/roles',
  '/severity-patterns',
  '/smtp-settings',
  '/sources',
  '/sso',
  '/system',
  '/system-settings',
  '/users',
]

export default defineConfig({
  plugins: [svelte()],
  server: {
    proxy: Object.fromEntries(
      apiPrefixes.map((prefix) => [
        prefix,
        {
          target: backendTarget,
          changeOrigin: true,
          // The backend's OriginCheckMiddleware (see backend/app/main.py)
          // compares Origin/Referer against the request's own Host header
          // as CSRF defense-in-depth. In dev, this proxy legitimately sits
          // between two different ports (Vite's own, and backendTarget
          // above) -- forwarding the browser's real Origin (Vite's port)
          // unchanged would make every proxied POST/PATCH/PUT/DELETE look
          // cross-origin to the backend and get blocked. Stripping these
          // headers here mirrors what a same-origin request looks like
          // (the middleware treats an absent Origin/Referer as "rely on
          // SameSite instead"), same as production's reverse-proxy setup
          // where frontend and backend share one origin, so there's
          // nothing to strip in the first place.
          configure(proxy) {
            proxy.on('proxyReq', (proxyReq) => {
              proxyReq.removeHeader('origin')
              proxyReq.removeHeader('referer')
            })
          },
        },
      ]),
    ),
  },
  test: {
    environment: 'jsdom',
    // e2e/ holds Playwright specs (see playwright.config.ts) -- a separate
    // test runner with its own test()/expect(), not vitest's.
    exclude: ['e2e/**', 'node_modules/**'],
  },
})
