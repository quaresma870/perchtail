// The upgrade steps the Updates card shows (#215), matching how this
// instance was installed. Kept in step with the README (Docker), the
// "Upgrading" sections of docs/kubernetes.md and
// docs/install-from-source.md. All start with an encrypted pre-upgrade
// backup (#334, docs/backup-and-restore.md), the one to restore to roll the
// upgrade back.

import type { InstallMethod } from './types'

const IMAGE = 'ghcr.io/quaresma870/perchtail'
const CHART = 'oci://ghcr.io/quaresma870/charts/perchtail'
const DOCS = 'https://github.com/quaresma870/perchtail/blob'

// The guide the card links to under the steps, at the new version's tag;
// null when the steps are all there is.
export function upgradeGuide(installMethod: InstallMethod, version: string): string | null {
  return installMethod === 'kubernetes' ? `${DOCS}/v${version}/docs/kubernetes.md#upgrading` : null
}

export function upgradeSteps(installMethod: InstallMethod, version: string): string {
  if (installMethod === 'kubernetes') {
    return [
      '# On SQLite (on PostgreSQL, back the database up with its own tools):',
      'kubectl -n <namespace> exec deploy/perchtail -- python -m app.backup create --reason pre-upgrade',
      '# With the values file you installed with:',
      `helm upgrade perchtail ${CHART} --version ${version} -n <namespace> -f <your values file>`,
    ].join('\n')
  }
  if (installMethod === 'docker') {
    return [
      '# In the folder with docker-compose.yml:',
      'docker compose exec -u perchtail perchtail python -m app.backup create --reason pre-upgrade',
      'docker compose stop perchtail',
      '# Set the image to:',
      `#   ${IMAGE}:${version}`,
      'docker compose pull && docker compose up -d',
    ].join('\n')
  }
  return [
    `PERCHTAIL_TAG=v${version}`,
    'cd ~/perchtail-build',
    'git fetch --tags && git checkout "$PERCHTAIL_TAG"',
    '(cd frontend && npm ci && npm run build)',
    '',
    'sudo systemd-run --pipe --wait --uid=perchtail --gid=perchtail \\',
    '  -p EnvironmentFile=/etc/perchtail/perchtail.env -p WorkingDirectory=/opt/perchtail/backend \\',
    '  /opt/perchtail-venv/bin/python -m app.backup create --reason pre-upgrade',
    'sudo systemctl stop perchtail',
    'sudo rsync -a --chown=root:root --delete --exclude .git --exclude frontend/node_modules ~/perchtail-build/ /opt/perchtail/',
    'sudo /opt/perchtail-venv/bin/pip install --require-hashes -r /opt/perchtail/backend/requirements.txt',
    'sudo systemctl start perchtail          # migrates on start',
  ].join('\n')
}
