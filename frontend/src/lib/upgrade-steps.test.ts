import { describe, expect, it } from 'vitest'
import { upgradeSteps } from './upgrade-steps'

describe('upgradeSteps', () => {
  it('points a Docker install at the new image tag', () => {
    const steps = upgradeSteps('docker', '0.6.0')
    expect(steps).toContain('ghcr.io/quaresma870/perchtail:0.6.0')
    expect(steps).toContain('docker compose pull && docker compose up -d')
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
})
