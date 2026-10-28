// Synthetic API fixture; exercises Review and ClipPlayer without user media or AI.
const assert = require('node:assert/strict')
const path = require('node:path')
const { pathToFileURL } = require('node:url')
const { chromium } = require(process.env.AUTOCLIP_PLAYWRIGHT_MODULE || 'playwright')

async function eventually(check) {
  const deadline = Date.now() + 10000
  while (true) {
    try { check(); return } catch (error) {
      if (Date.now() > deadline) throw error
      await new Promise(resolve => setTimeout(resolve, 30))
    }
  }
}

const style = (key, sizeRatio, positionX = 0.5, positionY = 0.82) => ({
  key, label: key, description: 'Synthetic style', preview: {
    font: 'Arial', primary: '#ffffff', accent: null, outline: '#000000', outlineWidth: 2,
    allCaps: false, sizeRatio, marginRatio: 0.12, positionX, positionY, boxed: false,
    animation: 'none', maxWords: 5,
  },
})

async function main() {
  const { createServer } = await import(pathToFileURL(require.resolve('../frontend/node_modules/vite/dist/node/index.js')))
  const server = await createServer({ root: path.join(__dirname, '../frontend'), configFile: path.join(__dirname, '../frontend/vite.config.ts'), server: { host: '127.0.0.1', port: 0 } })
  let browser
  try {
    await server.listen()
    browser = await chromium.launch({ headless: true })
    const page = await browser.newPage({ viewport: { width: 375, height: 900 } })
    page.setDefaultTimeout(10000)
    const errors = [], edits = [], exports = []
    let failNextEdit = false, holdNextEdit = false, releaseHeldEdit, notifyEditHeld
    const editHeld = new Promise(resolve => { notifyEditHeld = resolve })
    page.on('pageerror', error => errors.push(error.message))
    const clips = ['a', 'b'].map((id, index) => ({
      id, job_id: 'job', rank: index + 1, start_s: 0, end_s: 5, duration_s: 5,
      start_word: 0, end_word: 1, title: `Clip ${id.toUpperCase()}`, hook: '', score: 90,
      reason: 'Fixture', status: 'kept', user_trimmed: false, caption_style: 'narrow', ratio: '9:16',
      burn_captions: true, framing: id === 'a' ? { mode: 'manual', center_x: 0.5, center_y: 0.5, zoom: 1 } : null,
      caption_overrides: null, exports: [],
    }))
    clips[1].caption_style = 'wide'
    const job = { id: 'job', source_id: 'source', status: 'done', current_stage: 'done', progress: 1,
      error: null, provider: 'fixture', created_at: '', updated_at: '', started_at: null, finished_at: null,
      source: { id: 'source', type: 'upload', title: 'Fixture', url: null, filename: 'fixture.mp4', channel: null,
        duration_s: 5, width: 1920, height: 1080, fps: 30, has_audio: false, has_video: true, created_at: '' } }
    const styles = [style('narrow', 0.062, 0.43, 0.78), style('wide', 0.034, 0.5, 0.84)]
    await page.route('**/api/**', async route => {
      const req = route.request(), url = new URL(req.url()), p = url.pathname
      if (p === '/api/jobs/job') return route.fulfill({ json: job })
      if (p === '/api/jobs/job/clips') return route.fulfill({ json: clips })
      if (p === '/api/caption-styles') return route.fulfill({ json: styles })
      if (p.endsWith('/words')) return route.fulfill({ json: [{ text: 'nieprawdopodobnie', start: 0, end: 4, speaker: null }] })
      if (p.endsWith('/crop-path')) return route.fulfill({ json: { source_width: 1920, source_height: 1080,
        segments: [{ start_s: 0, end_s: 5, width: 608, height: 1080, keyframes: [{ t: 0, x: 656, y: 0 }], strategy: 'general', zoom: 1, fit: false }] } })
      if (p.endsWith('/edit') && req.method() === 'PATCH') {
        const id = p.split('/')[3], patch = req.postDataJSON(); edits.push({ id, patch })
        if (failNextEdit) { failNextEdit = false; return route.fulfill({ status: 500, json: { detail: 'Fixture save failure' } }) }
        if (holdNextEdit) {
          holdNextEdit = false
          notifyEditHeld()
          await new Promise(resolve => { releaseHeldEdit = resolve })
        }
        Object.assign(clips.find(c => c.id === id), patch)
        return route.fulfill({ json: clips.find(c => c.id === id) })
      }
      if (p.endsWith('/export')) { exports.push(p); return route.fulfill({ json: { id: 'export', clip_id: 'a', ratio: '9:16', style: 'narrow', size_bytes: 1, created_at: '', download_url: '#' } }) }
      if (p.endsWith('/media')) return route.fulfill({ status: 404, body: '' })
      if (p === '/api/providers/status') return route.fulfill({ json: [] })
      if (p === '/api/system') return route.fulfill({ json: { accel: 'cpu' } })
      return route.fulfill({ status: 404, json: { detail: 'fixture route missing' } })
    })

    await page.goto(server.resolvedUrls.local[0] + 'jobs/job/clips')
    const slider = name => page.getByRole('slider', { name, exact: true })
    const button = name => page.getByRole('button', { name, exact: true })
    await page.locator('summary', { hasText: 'Advanced / legacy caption layout' }).click()
    await slider('Caption horizontal position').waitFor()
    await page.waitForFunction(() => !document.querySelector('[aria-label="Zoom"]')?.disabled)
    await slider('Zoom').focus()
    await slider('Zoom').press('ArrowRight')
    assert.equal(await slider('Caption size').isEnabled(), true, 'framing draft does not disable caption editor')
    await page.getByRole('button', { name: /^wide/ }).click()
    await eventually(() => assert.deepEqual(edits[0].patch, { caption_style: 'wide', caption_overrides: null }))
    assert.deepEqual(edits[0].patch, { caption_style: 'wide', caption_overrides: null }, 'style PATCH succeeds during framing draft')
    assert.equal(await page.getByRole('status').filter({ hasText: 'Unsaved framing' }).count(), 1, 'style change preserves framing draft')
    await page.getByLabel('Subtitles', { exact: true }).click()
    await eventually(() => assert.equal(edits.length, 2))
    assert.deepEqual(edits[1].patch, { burn_captions: false }, 'subtitle PATCH succeeds during framing draft')
    assert.equal(await page.getByRole('status').filter({ hasText: 'Unsaved framing' }).count(), 1)
    await button('Discard framing changes').click()
    await page.waitForFunction(() => !document.querySelector('[aria-label="Zoom"]')?.disabled)
    clips[0].caption_style = 'narrow'
    clips[0].burn_captions = true
    await page.reload()
    await page.locator('summary', { hasText: 'Advanced / legacy caption layout' }).click()
    await slider('Caption horizontal position').waitFor()
    await page.waitForFunction(() => !document.querySelector('[aria-label="Zoom"]')?.disabled)
    edits.length = 0
    assert.equal(await slider('Caption size').inputValue(), '6.2', 'preset fraction is preserved')
    const player = page.locator('.relative.w-full.overflow-hidden.bg-ink-850').first()
    const caption = page.locator('p.text-center.leading-\\[1\\.15\\]').first()
    await page.waitForTimeout(150)
    const playerHeight = await player.evaluate(el => el.getBoundingClientRect().height)
    const computedSize = await caption.evaluate(el => parseFloat(getComputedStyle(el).fontSize))
    assert.ok(Math.abs(computedSize - playerHeight * 0.062) < 1, `font size ${computedSize}px should be 6.2% of player height ${playerHeight}px`)
    assert.equal(await caption.evaluate(el => el.scrollWidth <= el.clientWidth), true, 'long Polish word must wrap inside caption width')

    await page.getByRole('button', { name: 'nieprawdopodobnie', exact: true }).click()
    await page.getByRole('textbox').fill('niezwykle')
    await page.getByRole('textbox').press('Enter')
    await page.getByText('Unsaved changes', { exact: true }).waitFor()
    await page.getByRole('button', { name: /^wide/ }).click()
    await eventually(() => assert.equal(edits.length, 1))
    assert.deepEqual(edits[0].patch, { caption_style: 'wide', caption_overrides: null }, 'style PATCH succeeds during word draft')
    assert.equal(await page.getByText('Unsaved changes', { exact: true }).count(), 1, 'style change preserves word draft')
    await page.getByLabel('Subtitles', { exact: true }).click()
    await eventually(() => assert.equal(edits.length, 2))
    assert.deepEqual(edits[1].patch, { burn_captions: false }, 'subtitle PATCH succeeds during word draft')
    assert.equal(await page.getByText('Unsaved changes', { exact: true }).count(), 1)
    clips[0].caption_style = 'narrow'
    clips[0].burn_captions = true
    await page.reload()
    await page.locator('summary', { hasText: 'Advanced / legacy caption layout' }).click()
    await slider('Caption horizontal position').waitFor()
    edits.length = 0

    await slider('Caption horizontal position').focus()
    for (let i = 0; i < 18; i++) await page.keyboard.press('ArrowRight')
    const draftOverlayX = await caption.evaluate(el => {
      const r = el.parentElement.getBoundingClientRect(), p = el.closest('.relative.w-full.overflow-hidden').getBoundingClientRect()
      return (r.left + r.width / 2 - p.left) / p.width
    })
    assert.ok(Math.abs(draftOverlayX - 0.43) < 0.02, `draft must not move preview before Apply: ${draftOverlayX}`)
    assert.equal(await button('Export this clip').isDisabled(), true)
    assert.equal(await page.getByLabel('Drag start of clip').isDisabled(), true, 'draft blocks trim')
    assert.equal(await slider('Zoom').isDisabled(), true, 'caption layout draft disables manual framing')
    assert.equal(exports.length, 0, 'draft blocks export')
    await button('1:1').click()
    await page.getByText('Whole clip framing', { exact: true }).click()
    await button('Auto').click()
    await page.getByRole('button', { name: /^wide/ }).click()
    assert.equal(edits.length, 0, 'ratio change is guarded while dirty')
    await page.getByText('Clip B', { exact: true }).click()
    assert.equal(await slider('Caption size').inputValue(), '6.2', 'draft guards clip switching')
    assert.equal(edits.length, 0)
    await button('Discard caption layout').click()
    await page.getByText('Clip B', { exact: true }).click()
    await page.waitForFunction(() => document.querySelector('[aria-label="Caption size"]')?.value === '3.4')
    assert.equal(await slider('Caption size').inputValue(), '3.4', 'clip B has its own style layout')
    await page.getByText('Clip A', { exact: true }).click()
    await page.waitForFunction(() => document.querySelector('[aria-label="Caption size"]')?.value === '6.2')
    assert.equal(await slider('Caption size').inputValue(), '6.2')
    assert.equal(edits.length, 0, 'discard sends no PATCH')

    await slider('Caption horizontal position').focus()
    for (let i = 0; i < 18; i++) await page.keyboard.press('ArrowRight')
    failNextEdit = true
    await button('Apply caption layout').click()
    await page.getByText('Fixture save failure', { exact: true }).waitFor()
    assert.equal(await page.getByRole('status').filter({ hasText: 'Unsaved caption layout' }).count(), 1, 'failed save retains draft')
    assert.equal(await button('Apply caption layout').isEnabled(), true, 'failed save can be retried')
    holdNextEdit = true
    await button('Apply caption layout').click()
    await editHeld
    await page.waitForFunction(() => document.querySelector('fieldset[aria-label="Caption layout"]')?.disabled)
    assert.equal(await slider('Caption size').isDisabled(), true, 'pending apply locks layout actions')
    assert.equal(await button('Export this clip').isDisabled(), true, 'pending apply locks export')
    releaseHeldEdit()
    await page.waitForFunction(() => !document.querySelector('fieldset[aria-label="Caption layout"]')?.disabled)
    assert.equal(edits.length, 2)
    assert.deepEqual(edits[0].patch.caption_overrides, { size_ratio: 0.062, position_x: 0.61, position_y: 0.78 })
    const overlayRect = await caption.evaluate(el => { const r = el.parentElement.getBoundingClientRect(); const p = el.closest('.relative.w-full.overflow-hidden').getBoundingClientRect(); return { x: (r.left + r.width / 2 - p.left) / p.width, y: (r.bottom - p.top) / p.height } })
    assert.ok(Math.abs(overlayRect.x - 0.61) < 0.02, `saved overlay x ${overlayRect.x}`)
    assert.ok(Math.abs(overlayRect.y - 0.78) < 0.02, `saved overlay y ${overlayRect.y}`)

    await page.waitForFunction(() => !document.querySelector('fieldset[aria-label="Caption layout"]')?.disabled)
    await button('Reset caption layout').click()
    assert.equal(edits.at(-1).patch.caption_overrides, null)
    await page.waitForFunction(() => !document.querySelector('fieldset[aria-label="Caption layout"]')?.disabled)
    await page.getByRole('button', { name: /^wide/ }).click()
    await page.waitForFunction(() => document.querySelector('[aria-label="Caption size"]')?.value === '3.4')
    assert.equal(await slider('Caption size').inputValue(), '3.4', 'clean layout follows changed preset')
    assert.equal(await page.getByRole('status').filter({ hasText: 'Unsaved caption layout' }).count(), 0)
    await page.reload()
    await page.locator('summary', { hasText: 'Advanced / legacy caption layout' }).click()
    await slider('Caption size').waitFor()
    assert.equal(await slider('Caption size').inputValue(), '3.4', 'reset and style persist after reload')

    const layoutEdits = edits.filter(e => !('caption_style' in e.patch))
    const overflow = await page.evaluate(() => ({ width: document.documentElement.scrollWidth, viewport: innerWidth }))
    assert.ok(overflow.width <= overflow.viewport, `mobile horizontal overflow: ${JSON.stringify(overflow)}`)
    assert.deepEqual(errors, [])
    console.log(JSON.stringify({ passed: true, editPatches: layoutEdits.length, exportsDuringDraft: exports.length, mobileWidth: overflow.viewport, pageErrors: errors.length }))
  } finally {
    if (browser) await browser.close()
    await server.close()
  }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
