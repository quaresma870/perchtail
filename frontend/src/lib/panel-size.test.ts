import { afterEach, describe, expect, it } from 'vitest'
import { clampPanelHeight, loadPanelHeight, MIN_EDITOR_PX, MIN_PANEL_PX, savePanelHeight } from './panel-size'

describe('clampPanelHeight', () => {
  it('keeps a height that fits', () => {
    expect(clampPanelHeight(300, 800)).toBe(300)
  })

  it('never goes below the panel minimum', () => {
    expect(clampPanelHeight(10, 800)).toBe(MIN_PANEL_PX)
  })

  it('leaves the editor its minimum', () => {
    expect(clampPanelHeight(790, 800)).toBe(800 - MIN_EDITOR_PX)
  })

  it('keeps the panel minimum even when there is too little room for both', () => {
    expect(clampPanelHeight(500, 150)).toBe(MIN_PANEL_PX)
  })
})

describe('remembered height', () => {
  afterEach(() => localStorage.clear())

  it('round-trips a saved height', () => {
    savePanelHeight(321.6)
    expect(loadPanelHeight()).toBe(322)
  })

  it('is null when nothing (or nonsense) was saved', () => {
    expect(loadPanelHeight()).toBeNull()
    localStorage.setItem('perchtail.findAllHeight', 'abc')
    expect(loadPanelHeight()).toBeNull()
    localStorage.setItem('perchtail.findAllHeight', '5')
    expect(loadPanelHeight()).toBeNull()
  })
})
