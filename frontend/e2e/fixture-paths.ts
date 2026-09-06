import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

// frontend/package.json declares "type": "module", so Playwright runs this
// file as real ESM -- no __dirname/__filename (CommonJS-only globals);
// derive the equivalent from import.meta.url instead.
const __dirname = path.dirname(fileURLToPath(import.meta.url))

// Written by backend/scripts/setup_e2e_test_servers.sh before the e2e
// backend ever answers /healthz -- SSH_ROOT/SMB_ROOT are absolute paths
// under this checkout, so there's nothing fixed a spec file could
// hardcode instead.
interface ProtocolFixture {
  host: string
  port: number
  base_path: string
  username: string
  password: string
}

interface FixturePaths {
  ssh: ProtocolFixture
  smb: ProtocolFixture
}

export function loadFixturePaths(): FixturePaths {
  const file = path.join(__dirname, '..', '..', 'backend', 'data', 'e2e', 'fixture-paths.json')
  return JSON.parse(readFileSync(file, 'utf-8'))
}
