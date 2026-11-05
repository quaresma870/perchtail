import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import { IMPORT_TEMPLATE } from './import-template'

describe('import template', () => {
  it('matches the documented example', () => {
    const doc = readFileSync(
      resolve(__dirname, '../../../docs/examples/perchtail-import.example.yaml'),
      'utf-8',
    )
    expect(IMPORT_TEMPLATE).toBe(doc)
  })
})
