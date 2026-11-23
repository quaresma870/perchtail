// Redaction settings (#382, #249): what the screens for super admins show
// about the rules. The server decides and enforces everything; this only
// describes it.

import type { RedactionRule, Source } from './types'

export interface DetectorInfo {
  title: string
  description: string
  falsePositives: string
}

/** The built-in detectors (backend/app/redaction/detectors.json), in the
 * words the settings screen uses. */
export const DETECTOR_INFO: Record<string, DetectorInfo> = {
  card: {
    title: 'Payment card numbers',
    description:
      'Numbers of 12 to 19 digits, written together or grouped with spaces or hyphens, that pass the Luhn check and start and are as long as a card brand\'s numbers. Shown as [redacted: card ****1234], with the last four digits.',
    falsePositives:
      'Long numeric ids that happen to pass the Luhn check and look like a card brand\'s (about one in ten 16-digit ids starting with 4). IMEIs and millisecond timestamps are left alone.',
  },
  bearer: {
    title: 'Bearer tokens',
    description: 'The token after "Bearer" (8 characters or more), as in Authorization headers and token logs.',
    falsePositives: 'Prose such as "bearer of" followed by a long word.',
  },
  authorization: {
    title: 'Authorization headers',
    description:
      'The value of an Authorization or Proxy-Authorization header or key, whatever its scheme (Basic, Digest, a token...).',
    falsePositives: 'A key named authorization whose value isn\'t a secret, such as "authorization: granted".',
  },
  secret_kv: {
    title: 'Passwords, secrets and keys',
    description:
      'Values of keys whose name holds password, passwd, pwd, secret, token, api key, access key, client secret or private key, written key=value or key: value. The replacement says which kind ([redacted: password]). null and none are left alone.',
    falsePositives: 'PWD=/home/user in shell traces, and counters such as token_count=5 or tokens: 12.',
  },
  url_password: {
    title: 'Passwords in URLs',
    description: 'The password in URLs such as postgres://user:password@host. The user name stays.',
    falsePositives: 'Rare.',
  },
  private_key: {
    title: 'Private keys',
    description:
      'PEM private keys, from the BEGIN ... PRIVATE KEY line to its END line, or up to 200 lines when the END line is missing. Line numbers stay as they were. Certificates aren\'t redacted.',
    falsePositives: 'Almost none.',
  },
  jwt: {
    title: 'JSON Web Tokens',
    description: 'Tokens of the form eyJ....eyJ....signature.',
    falsePositives: 'Very rare.',
  },
  email: {
    title: 'Email addresses',
    description: 'Email addresses. Nothing of the address is kept.',
    falsePositives: 'user@host.domain in SSH logs, and other ids written like an address with a domain.',
  },
  ipv4: {
    title: 'IPv4 addresses',
    description: 'IPv4 addresses.',
    falsePositives: 'Version numbers with four parts (1.2.3.4), and parts of OIDs.',
  },
  ipv6: {
    title: 'IPv6 addresses',
    description: 'IPv6 addresses, checked to be valid: times such as 12:30:45 and MAC addresses are left alone.',
    falsePositives: 'Hexadecimal ids in colon-separated groups that happen to form a valid IPv6 address.',
  },
  iban: {
    title: 'IBANs',
    description:
      'Bank account numbers (IBAN), checked against the country\'s length and the check digits. Nothing of the number is kept.',
    falsePositives: 'Very rare once the check digits are checked.',
  },
}

export function detectorInfo(detector: string): DetectorInfo {
  return DETECTOR_INFO[detector] ?? { title: detector, description: '', falsePositives: '' }
}

export const builtinRules = (rules: RedactionRule[]): RedactionRule[] =>
  rules.filter((rule) => rule.detector !== null)

export const customRules = (rules: RedactionRule[]): RedactionRule[] =>
  rules.filter((rule) => rule.detector === null)

export interface InheritedRule {
  rule: RedactionRule
  /** Whether the engine runs it on this source. */
  appliesHere: boolean
  /** On system-wide, but this source turns it off. */
  turnedOffHere: boolean
}

/** A source's view of the system rules: each one, whether it runs on the
 * source (it's in the source's effective rules) and whether the source
 * turns it off. A rule off system-wide runs nowhere, whatever the source
 * says, so only rules on system-wide can be turned off here. */
export function inheritedRules(system: RedactionRule[], effective: RedactionRule[]): InheritedRule[] {
  const running = new Set(effective.map((rule) => rule.id))
  return system.map((rule) => ({
    rule,
    appliesHere: running.has(rule.id),
    turnedOffHere: rule.enabled && !running.has(rule.id),
  }))
}

/** The warning for a push-agent source whose agent can't redact on its
 * host, or null. An agent that doesn't say where links lead (links/1) gets
 * none of its files read until it is updated, so there is nothing for
 * anyone to redact; one that does but can't redact has the server redact
 * what it sends, so the original leaves the host. An agent that never
 * connected isn't known yet. */
export function agentRedactionWarning(source: Pick<Source, 'protocol' | 'agent_version' | 'agent_features'>): string | null {
  if (source.protocol !== 'agent' || source.agent_version === null) return null
  if (!source.agent_features.includes('links/1')) {
    return `This agent (${source.agent_version}) must be updated before its files can be read: until then nothing is opened, followed or downloaded from it. Update it from Settings → Agents.`
  }
  if (source.agent_features.includes('redaction/1')) return null
  return `This agent (${source.agent_version}) can't redact on its host: PerchTail redacts what it sends, so the original content leaves the host. Upgrade it (Settings → Agents) to redact on the host.`
}

// Number fields hold what a bound number input gives: a number, null when
// empty, or the text they started with.
export interface RuleDraft {
  name: string
  pattern: string
  maxLength: string | number | null
  secretGroup: string | number | null
  notes: string
}

const fieldText = (value: string | number | null | undefined): string =>
  value === null || value === undefined ? '' : String(value).trim()

/** The group a draft or test names: null for the whole match, NaN when
 * it isn't a whole number of zero or more. */
export function draftGroup(value: string | number | null): number | null {
  const text = fieldText(value)
  if (text === '') return null
  const group = Number(text)
  return Number.isInteger(group) && group >= 0 ? group : NaN
}

export const MAX_NAME_CHARS = 48
export const MAX_PATTERN_CHARS = 1000
export const MAX_RULE_LENGTH = 4096
export const DEFAULT_MAX_LENGTH = 256

export const emptyDraft = (): RuleDraft => ({
  name: '',
  pattern: '',
  maxLength: String(DEFAULT_MAX_LENGTH),
  secretGroup: '',
  notes: '',
})

export function draftFromRule(rule: RedactionRule): RuleDraft {
  return {
    name: rule.name,
    pattern: rule.pattern ?? '',
    maxLength: String(rule.max_length ?? DEFAULT_MAX_LENGTH),
    secretGroup: rule.secret_group === null ? '' : String(rule.secret_group),
    notes: rule.notes ?? '',
  }
}

/** What's wrong with a draft before it's sent, or null. The server checks
 * the pattern itself (RE2, empty matches, the longest match). */
export function draftError(draft: RuleDraft): string | null {
  const name = draft.name.trim()
  if (!name) return 'The rule needs a name.'
  if (name.length > MAX_NAME_CHARS) return `The name is longer than ${MAX_NAME_CHARS} characters.`
  if (/[\x00-\x1f\x7f[\]]/.test(name)) return "The name can't contain control characters, [ or ]."
  if (!draft.pattern) return 'The rule needs a pattern.'
  if (draft.pattern.length > MAX_PATTERN_CHARS) return `The pattern is longer than ${MAX_PATTERN_CHARS} characters.`
  const maxText = fieldText(draft.maxLength)
  const maxLength = Number(maxText)
  if (maxText === '' || !Number.isInteger(maxLength) || maxLength < 1 || maxLength > MAX_RULE_LENGTH) {
    return `Longest match: a whole number of bytes from 1 to ${MAX_RULE_LENGTH}.`
  }
  if (Number.isNaN(draftGroup(draft.secretGroup))) return GROUP_ERROR
  return null
}

export const GROUP_ERROR = 'Group: a whole number, or empty for the whole match.'

/** The body of a create or update request for a valid draft. */
export function draftPayload(draft: RuleDraft) {
  const notes = (draft.notes ?? '').trim()
  return {
    name: draft.name.trim(),
    pattern: draft.pattern,
    max_length: Number(fieldText(draft.maxLength)),
    secret_group: draftGroup(draft.secretGroup),
    notes: notes === '' ? null : notes,
  }
}

export interface Segment {
  text: string
  match: boolean
}

/** The sample cut into the parts a test matched and the rest, to show
 * which text a rule would replace. Overlapping or out-of-range matches are
 * clipped. */
export function matchSegments(sample: string, matches: { start: number; end: number }[]): Segment[] {
  const out: Segment[] = []
  let at = 0
  for (const { start, end } of [...matches].sort((a, b) => a.start - b.start)) {
    const from = Math.max(start, at)
    const to = Math.min(end, sample.length)
    if (to <= from) continue
    if (from > at) out.push({ text: sample.slice(at, from), match: false })
    out.push({ text: sample.slice(from, to), match: true })
    at = to
  }
  if (at < sample.length) out.push({ text: sample.slice(at), match: false })
  return out
}

/** The order values that move `rules[index]` one place up (-1) or down
 * (+1) among `rules` (sorted by order): the two rules swap places. Returns
 * the rules to update and their new order, or [] at an end. */
export function moveRule(rules: RedactionRule[], index: number, step: -1 | 1): { id: number; order: number }[] {
  const other = index + step
  if (index < 0 || index >= rules.length || other < 0 || other >= rules.length) return []
  const a = rules[index]
  const b = rules[other]
  if (a.order !== b.order) {
    return [
      { id: a.id, order: b.order },
      { id: b.id, order: a.order },
    ]
  }
  // The same order (ties are broken by id): put the moved rule just past the other.
  return [{ id: a.id, order: b.order + step }]
}
