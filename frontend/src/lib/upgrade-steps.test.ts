import { describe, expect, it } from 'vitest'
import { upgradeSteps } from './upgrade-steps'

describe('upgradeSteps', () => {
  it('points a Docker install at the new image tag', () => {
    const steps = upgradeSteps('docker', '0.6.0')
    expect(steps).toContain('ghcr.io/quaresma870/perchtail:0.6.0')
    expect(steps).toContain('docker compose pull && docker compose up -d')
  })

  it('gives a source install the tag and the steps from the install guide', () => {
    const steps = upgradeSteps('source', '0.6.0')
    expect(steps).toContain('PERCHTAIL_TAG=v0.6.0')
    expect(steps).toContain('--chown=root:root')
    expect(steps).toContain('sudo systemctl start perchtail')
  })
})
