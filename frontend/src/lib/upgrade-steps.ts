// The upgrade steps the Updates card shows (#215), matching how this
// instance was installed. Kept in step with the README (Docker) and the
// "Upgrading" section of docs/install-from-source.md.

const IMAGE = 'ghcr.io/quaresma870/perchtail'

export function upgradeSteps(installMethod: 'docker' | 'source', version: string): string {
  if (installMethod === 'docker') {
    return [
      '# In the folder with docker-compose.yml:',
      'docker compose stop perchtail',
      '# Back up the perchtail-data volume, then set the image to:',
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
    'sudo systemctl stop perchtail',
    'sudo cp -a /var/lib/perchtail/db "/var/lib/perchtail/db.backup-$(date +%F)"',
    'sudo rsync -a --chown=root:root --delete --exclude .git --exclude frontend/node_modules ~/perchtail-build/ /opt/perchtail/',
    'sudo /opt/perchtail-venv/bin/pip install -r /opt/perchtail/backend/requirements.txt',
    'sudo systemctl start perchtail          # migrates on start',
  ].join('\n')
}
