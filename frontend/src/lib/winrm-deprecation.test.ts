import { describe, expect, it } from 'vitest'
import {
  isDeprecatedProtocol,
  PROTOCOL_CHOICE_GUIDE,
  WINRM_ALTERNATIVES_TEXT,
  WINRM_DEPRECATED_TEXT,
  WINRM_MIGRATION_GUIDE,
} from './winrm-deprecation'

describe('WinRM deprecation', () => {
  it('marks only WinRM as deprecated', () => {
    expect(isDeprecatedProtocol('winrm')).toBe(true)
    for (const protocol of ['ssh', 'smb', 'local', 'agent']) {
      expect(isDeprecatedProtocol(protocol)).toBe(false)
    }
  })

  it('says WinRM goes in a future major release and names the alternatives', () => {
    expect(WINRM_DEPRECATED_TEXT).toBe(
      'WinRM is deprecated and will be removed in a future major release.',
    )
    expect(WINRM_ALTERNATIVES_TEXT).toContain('push agent')
    expect(WINRM_ALTERNATIVES_TEXT).toContain('SMB')
    expect(WINRM_ALTERNATIVES_TEXT).toContain('OpenSSH')
  })

  it('links to the sections of the source setup guide', () => {
    expect(WINRM_MIGRATION_GUIDE).toMatch(/docs\/source-setup\.md#moving-a-winrm-source$/)
    expect(PROTOCOL_CHOICE_GUIDE).toMatch(/docs\/source-setup\.md#choosing-how-to-reach-a-host$/)
  })
})
