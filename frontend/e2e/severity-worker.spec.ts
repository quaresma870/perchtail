import { expect, test } from '@playwright/test'

type SeverityPattern = Record<string, unknown>

// A severity pattern that is valid RE2, so the server takes it, but that
// the browser's backtracking engine takes exponential time on: one call to
// the regex engine never returns on a line of `a` ending in a `b`. It runs
// in a Web Worker, so the viewer keeps answering, the pattern is turned off
// with a notice, and the other patterns still apply.
const SYSTEM_SOURCE_NAME = 'PerchTail application logs'
const FINE_MARKER = `e2e-fine-marker-${Date.now()}`

function extraPattern(overrides: SeverityPattern): SeverityPattern {
  return {
    id: 0,
    source_id: null,
    level: 'error',
    pattern: '',
    pattern_kind: 'regex',
    enabled: true,
    highlight_line: false,
    include_in_navigation: true,
    ...overrides,
  }
}

test('an exponential severity pattern is turned off without stalling the viewer', async ({
  page,
}) => {
  await page.route('**/severity-patterns/effective', async (route) => {
    const response = await route.fetch()
    const patterns = (await response.json()) as SeverityPattern[]
    patterns.push(
      extraPattern({ id: 990_001, pattern: '(a+)+$' }),
      extraPattern({ id: 990_002, pattern: FINE_MARKER, pattern_kind: 'glob', level: 'warning' }),
    )
    await route.fulfill({ response, json: patterns })
  })
  // The log opens with the line the pattern backtracks on, in view: 1999
  // `a` and a `b`, all within the 2000 characters of a line that are
  // matched (with 2000 `a` first, the `b` would be cut off and the pattern
  // would match at once).
  await page.route('**/open?**', async (route) => {
    const response = await route.fetch()
    const body = `${'a'.repeat(1999)}b\n${FINE_MARKER} here\n${await response.text()}`
    await route.fulfill({ response, body })
  })

  await page.goto('/#/viewer')
  await page.locator('.explorer').getByRole('button', { name: SYSTEM_SOURCE_NAME }).click()
  await page.locator('.tree-body').getByRole('button', { name: 'perchtail.log' }).click()

  await expect(page.getByRole('alert').filter({ hasText: '(a+)+$' })).toBeVisible({
    timeout: 15_000,
  })
  await expect(page.locator('.cm-level-warn', { hasText: FINE_MARKER })).toBeVisible()
  // The page still answers: the find bar opens and takes text.
  await page.getByRole('button', { name: 'Find', exact: true }).click()
  const find = page.getByRole('textbox', { name: 'Find', exact: true })
  await find.fill(FINE_MARKER)
  await expect(find).toHaveValue(FINE_MARKER)
})
