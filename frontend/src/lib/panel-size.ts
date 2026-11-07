// Height of the Find all panel (#285), dragged by the user and remembered
// in this browser only.

const KEY = 'perchtail.findAllHeight'

/** The panel and the editor above it each keep at least this much room. */
export const MIN_PANEL_PX = 160 // the panel's CSS min-height
export const MIN_EDITOR_PX = 120
/** How far one arrow key press moves the handle. */
export const KEY_STEP_PX = 24

/** `wanted` kept within the room there is: the panel at least
 * MIN_PANEL_PX, the editor at least MIN_EDITOR_PX of `available` (the
 * height the two share). */
export function clampPanelHeight(wanted: number, available: number): number {
  const max = Math.max(MIN_PANEL_PX, available - MIN_EDITOR_PX)
  return Math.round(Math.min(Math.max(wanted, MIN_PANEL_PX), max))
}

/** The height remembered in this browser, or null for the default. */
export function loadPanelHeight(): number | null {
  try {
    const value = Number(localStorage.getItem(KEY))
    return Number.isFinite(value) && value >= MIN_PANEL_PX ? value : null
  } catch {
    return null
  }
}

export function savePanelHeight(px: number): void {
  try {
    localStorage.setItem(KEY, String(Math.round(px)))
  } catch {
    // Storage blocked: the height just isn't remembered.
  }
}
