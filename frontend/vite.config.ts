import { defineConfig } from 'vitest/config'
import { svelte } from '@sveltejs/vite-plugin-svelte'

const backendTarget = 'http://127.0.0.1:8000'

// The production deployment (docker-compose + nginx, see CLAUDE.md's
// Packaging section) proxies these same top-level paths straight to the
// backend and serves everything else as the SPA — mirrored here so `npm run
// dev` behaves the same way without a `/api` prefix rewrite.
const apiPrefixes = [
  '/auth',
  '/customers',
  '/folders',
  '/sources',
  '/roles',
  '/users',
  '/search',
  '/alerts',
  '/sso',
  '/system-settings',
  '/severity-patterns',
  '/monitoring',
  '/healthz',
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
  },
})
