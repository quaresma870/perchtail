import { writable } from 'svelte/store'

function currentPath(): string {
  const hash = window.location.hash
  return hash.startsWith('#') ? hash.slice(1) || '/' : '/'
}

export const currentHash = writable(currentPath())

window.addEventListener('hashchange', () => {
  currentHash.set(currentPath())
})
