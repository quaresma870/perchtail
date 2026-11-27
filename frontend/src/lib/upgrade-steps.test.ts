import { describe, expect, it } from 'vitest'
import { upgradeGuide, upgradeSteps } from './upgrade-steps'

describe('upgradeSteps', () => {
  it('points a Docker install at the new image tag', () => {
    const steps = upgradeSteps('docker', '0.6.0')
    expect(steps).toContain('ghcr.io/quaresma870/perchtail:0.6.0')
    expect(steps).toContain('docker compose pull && docker compose up -d')
  })

  it('upgrades a Kubernetes install with the chart from the registry and the values file', () => {
    const steps = upgradeSteps('kubernetes', '1.0.0')
    expect(steps).toContain(
      'helm upgrade perchtail oci://ghcr.io/quaresma870/charts/perchtail --version 1.0.0 -n <namespace> -f <your values file>',
    )
    expect(steps).not.toContain('--reuse-values')
    expect(steps).not.toContain('docker compose')
  })

  it('backs a Kubernetes install up before the helm upgrade', () => {
    const steps = upgradeSteps('kubernetes', '1.0.0')
    const backup = steps.indexOf(
      'kubectl -n <namespace> exec deploy/perchtail -- python -m app.backup create --reason pre-upgrade',
    )
    expect(backup).toBeGreaterThan(-1)
    expect(backup).toBeLessThan(steps.indexOf('helm upgrade'))
  })

  it('links a Kubernetes install to the guide of the new version, and no other', () => {
    expect(upgradeGuide('kubernetes', '1.0.0')).toBe(
      'https://github.com/quaresma870/perchtail/blob/v1.0.0/docs/kubernetes.md#upgrading',
    )
    expect(upgradeGuide('docker', '1.0.0')).toBeNull()
    expect(upgradeGuide('source', '1.0.0')).toBeNull()
  })

  it('starts both with an encrypted pre-upgrade backup, before the stop', () => {
    for (const method of ['docker', 'source'] as const) {
      const steps = upgradeSteps(method, '0.6.0')
      const backup = steps.indexOf('python -m app.backup create --reason pre-upgrade')
      expect(backup).toBeGreaterThan(-1)
      expect(backup).toBeLessThan(steps.indexOf('stop perchtail'))
    }
    expect(upgradeSteps('docker', '0.6.0')).toContain('exec -u perchtail perchtail')
    expect(upgradeSteps('source', '0.6.0')).toContain('--uid=perchtail')
    expect(upgradeSteps('source', '0.6.0')).not.toContain('db.backup-')
  })

  it('gives a source install the tag and the steps from the install guide', () => {
    const steps = upgradeSteps('source', '0.6.0')
    expect(steps).toContain('PERCHTAIL_TAG=v0.6.0')
    expect(steps).toContain('--chown=root:root')
    expect(steps).toContain('sudo systemctl start perchtail')
  })

  it('installs the pinned backend packages with their hashes checked, as the guides do', () => {
    expect(upgradeSteps('source', '0.6.0')).toContain(
      'sudo /opt/perchtail-venv/bin/pip install --require-hashes -r /opt/perchtail/backend/requirements.txt',
    )
  })
})
