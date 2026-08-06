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
  '/sso',
  '/system-settings',
  '/healthz',
]

export default defineConfig({
  plugins: [svelte()],
  server: {
    proxy: Object.fromEntries(
      apiPrefixes.map((prefix) => [prefix, { target: backendTarget, changeOrigin: true }]),
    ),
  },
  test: {
    environment: 'jsdom',
  },
})
