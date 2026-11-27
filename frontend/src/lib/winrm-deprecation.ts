import type { Protocol } from './types'

// WinRM is deprecated in PerchTail 1.0: it keeps working through 1.x and is
// removed in a later major release. The source editor warns when it is
// chosen and the sources list labels WinRM sources; Settings shows a banner
// while any exist (/system-settings/warnings, `winrm_deprecated`).

/** The migration guide in the source setup docs. */
export const WINRM_MIGRATION_GUIDE =
  'https://github.com/quaresma870/perchtail/blob/main/docs/source-setup.md#moving-a-winrm-source'

/** The protocol guide, which compares every way of reaching a host. */
export const PROTOCOL_CHOICE_GUIDE =
  'https://github.com/quaresma870/perchtail/blob/main/docs/source-setup.md#choosing-how-to-reach-a-host'

export const WINRM_DEPRECATED_TEXT =
  'WinRM is deprecated and will be removed in a future major release.'

export const WINRM_ALTERNATIVES_TEXT =
  'Prefer the push agent, which needs no inbound port or stored password; an SMB share of the log folder; or the OpenSSH server included with Windows, as an SSH / SFTP source.'

/** Whether sources of this protocol are deprecated. */
export const isDeprecatedProtocol = (protocol: Protocol | string): boolean => protocol === 'winrm'
