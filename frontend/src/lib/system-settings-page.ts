import { api, ApiError } from './api'
import { systemSettings } from './settings'
import type { SystemSettings, SystemWarning } from './types'

// What the system settings pages share (Settings -> Source defaults,
// Sign-in security, Explorer and limits, Alerts, Backups and updates,
// Monitoring, and the settings on the Audit log page): loading and saving
// /system-settings, the setup warnings, and the drafts saved with one button.

export const LOAD_FAILED = 'Failed to load system settings'
export const SAVE_FAILED = 'Failed to update system settings'

export function loadSystemSettings(): Promise<SystemSettings> {
  return api.get<SystemSettings>('/system-settings')
}

/** Setup problems only the operator can fix (a weak encryption key...).
 * Non-fatal: a page works without them. */
export async function loadSystemWarnings(): Promise<SystemWarning[]> {
  try {
    return await api.get<SystemWarning[]>('/system-settings/warnings')
  } catch {
    return []
  }
}

/** Saves a change and shares the settings the server answers with the app. */
export async function saveSystemSettings(patch: Partial<SystemSettings>): Promise<SystemSettings> {
  const settings = await api.patch<SystemSettings>('/system-settings', patch)
  systemSettings.set(settings)
  return settings
}

/** The server's reason for a refusal, or `fallback`. */
export function failureText(err: unknown, fallback: string): string {
  return err instanceof ApiError ? err.detail : fallback
}

// The shortest audit retention the server accepts, besides 0 (forever).
export const AUDIT_RETENTION_MIN_DAYS = 90

// Password policy (#245), saved together with one button.
export const POLICY_KEYS = [
  'password_min_length',
  'password_max_length',
  'password_reject_simple',
  'password_reject_personal',
  'password_history',
  'password_required_classes',
  'password_max_age_days',
] as const
export type PolicyFields = Pick<SystemSettings, (typeof POLICY_KEYS)[number]>

export const pickPolicy = (settings: SystemSettings): PolicyFields =>
  Object.fromEntries(POLICY_KEYS.map((key) => [key, settings[key]])) as PolicyFields

export const policyChanged = (saved: PolicyFields | null, draft: PolicyFields | null): boolean =>
  saved !== null && draft !== null && POLICY_KEYS.some((key) => saved[key] !== draft[key])

// Server-side limits (#344, #347), saved together with one button.
export const LIMIT_FIELDS = [
  {
    key: 'concurrency_global',
    label: 'Listings and downloads at once',
    unit: 'in all',
    min: 1,
    max: 256,
    hint: 'Folder listings, file opens, downloads, folder ZIPs and archive listings running at the same time across the server.',
  },
  {
    key: 'concurrency_per_source',
    label: 'At once on one source',
    unit: 'per source',
    min: 1,
    max: 64,
    hint: "The same, on any one source, so one busy server can't take every slot. At most the number above.",
  },
  {
    key: 'concurrency_user_transfers',
    label: 'Downloads per user',
    unit: 'per user',
    min: 1,
    max: 32,
    hint: 'Downloads and folder ZIPs one person may have in progress.',
  },
  {
    key: 'listing_max_entries',
    label: 'Entries per listing',
    unit: 'entries',
    min: 100,
    max: 20000,
    hint: 'The most entries a folder or archive shows in the explorer. A bigger folder shows the first ones and says so; the search finds the rest.',
  },
] as const
export type LimitFields = Pick<SystemSettings, (typeof LIMIT_FIELDS)[number]['key']>

export const pickLimits = (settings: SystemSettings): LimitFields =>
  Object.fromEntries(LIMIT_FIELDS.map((field) => [field.key, settings[field.key]])) as LimitFields

export const limitsChanged = (saved: LimitFields | null, draft: LimitFields | null): boolean =>
  saved !== null &&
  draft !== null &&
  LIMIT_FIELDS.some((field) => Number(saved[field.key]) !== Number(draft[field.key]))

/** Why the limits can't be saved as they are, or '' when they can. */
export function limitsProblem(draft: LimitFields): string {
  for (const field of LIMIT_FIELDS) {
    const value = Number(draft[field.key])
    if (!Number.isInteger(value) || value < field.min || value > field.max) {
      return `${field.label}: enter a whole number from ${field.min} to ${field.max}.`
    }
  }
  return ''
}

/** The limits as numbers, as the server takes them. */
export const limitsPatch = (draft: LimitFields): Partial<SystemSettings> =>
  Object.fromEntries(LIMIT_FIELDS.map((field) => [field.key, Number(draft[field.key])]))
