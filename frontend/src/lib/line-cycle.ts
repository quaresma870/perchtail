/** Generic wrap-around stepping through a sorted list of line numbers,
 * relative to a current position. Shared by severity-indicator "next/
 * previous problem" navigation (severity-highlighting.ts) and bookmark
 * navigation (Viewer.svelte) — same shape either way: "step to the next/
 * previous marked line, wrapping at either end." */

export function nextLine(lines: number[], current: number): number | null {
  if (lines.length === 0) return null
  return lines.find((line) => line > current) ?? lines[0]
}

export function previousLine(lines: number[], current: number): number | null {
  if (lines.length === 0) return null
  for (let i = lines.length - 1; i >= 0; i -= 1) {
    if (lines[i] < current) return lines[i]
  }
  return lines[lines.length - 1]
}
