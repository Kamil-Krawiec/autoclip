const assert = require('node:assert/strict')
const fs = require('node:fs')
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

async function main() {
  const { createServer } = await import(pathToFileURL(require.resolve('../frontend/node_modules/vite/dist/node/index.js')))
  const server = await createServer({
    root: path.join(__dirname, '../frontend'),
    configFile: path.join(__dirname, '../frontend/vite.config.ts'),
    server: { host: '127.0.0.1', port: 0 },
  })
  let browser
  try {
    await server.listen()
    browser = await chromium.launch({ headless: true })
    const page = await browser.newPage({ viewport: process.env.AUTOCLIP_DESKTOP ? { width: 1600, height: 1000 } : { width: 375, height: 900 } })
    page.setDefaultTimeout(10000)
    const errors = [], edits = [], trims = [], previewRequests = []
    let failPreview = false, previewBusy = false, holdPreview = false, releaseHeldPreview

    page.on('pageerror', error => errors.push(error.message))

    const clips = [
      {
        id: 'clip-1', job_id: 'job-1', rank: 1, start_s: 0, end_s: 10, duration_s: 10,
        start_word: 0, end_word: 2, title: 'Clip 1', hook: '', score: 95,
        reason: 'Main', status: 'kept', user_trimmed: false, caption_style: 'bold_pop',
        ratio: '9:16', burn_captions: true, framing: null, caption_overrides: null, exports: [],
      },
      {
        id: 'clip-2', job_id: 'job-1', rank: 2, start_s: 10, end_s: 20, duration_s: 10,
        start_word: 3, end_word: 5, title: 'Clip 2', hook: '', score: 85,
        reason: 'Second', status: 'candidate', user_trimmed: false, caption_style: 'bold_pop',
        ratio: '9:16', burn_captions: true, framing: null, caption_overrides: null, exports: [],
      },
    ]

    const job = {
      id: 'job-1', source_id: 'src-1', status: 'done', current_stage: 'done', progress: 1,
      error: null, provider: 'fixture', created_at: '', updated_at: '', started_at: null, finished_at: null,
      source: { id: 'src-1', type: 'upload', title: 'Fixture', url: null, filename: 'fixture.mp4',
        duration_s: 30, width: 1920, height: 1080, fps: 30, has_audio: false, has_video: true, created_at: '' },
    }

    await page.route('**/api/**', async route => {
      const req = route.request(), url = new URL(req.url()), p = url.pathname
      if (p === '/api/jobs/job-1') return route.fulfill({ json: job })
      if (p === '/api/jobs/job-1/clips') return route.fulfill({ json: clips })
      if (p === '/api/caption-styles') return route.fulfill({ json: [] })
      if (p.endsWith('/words')) return route.fulfill({ json: [{ text: 'słowo', start: 0, end: 10, speaker: null }] })
      if (p.endsWith('/crop-path')) {
        return route.fulfill({
          json: {
            source_width: 1920, source_height: 1080,
            segments: [{ start_s: 0, end_s: 10, width: 608, height: 1080, keyframes: [{ t: 0, x: 656, y: 0 }], strategy: 'general', zoom: 1, fit: false }],
          },
        })
      }
      if (p.endsWith('/edit') && req.method() === 'PATCH') {
        const id = p.split('/')[3], patch = req.postDataJSON()
        edits.push({ id, patch })
        const clip = clips.find(c => c.id === id)
        Object.assign(clip, patch)
        return route.fulfill({ json: clip })
      }
      if (/^\/api\/clips\/[^/]+$/.test(p) && req.method() === 'PATCH') {
        const clip = clips.find(c => c.id === p.split('/')[3])
        const patch = req.postDataJSON()
        trims.push(patch)
        Object.assign(clip, patch)
        return route.fulfill({ json: clip })
      }
      if (p.endsWith('/preview') && req.method() === 'POST') {
        const id = p.split('/')[3], body = req.postDataJSON()
        previewRequests.push({ id, body })
        if (previewBusy) return route.fulfill({ status: 409, json: { detail: 'Preview renderer busy' } })
        if (failPreview) {
          failPreview = false
          return route.fulfill({ status: 500, json: { detail: 'Preview render failed' } })
        }
        if (holdPreview) {
          await new Promise(resolve => { releaseHeldPreview = resolve })
        }
        return route.fulfill({
          status: 200,
          contentType: 'video/mp4',
          body: Buffer.from([0x00, 0x00, 0x00, 0x18, 0x66, 0x74, 0x79, 0x70]),
        })
      }
      if (p.endsWith('/media')) {
        if (process.env.AUTOCLIP_TEST_VIDEO) {
          const bytes = fs.readFileSync(process.env.AUTOCLIP_TEST_VIDEO)
          const match = /^bytes=(\d+)-(\d*)$/.exec(req.headers().range || '')
          if (match) {
            const start = Number(match[1]), end = match[2] ? Math.min(Number(match[2]), bytes.length - 1) : bytes.length - 1
            return route.fulfill({ status: 206, contentType: 'video/mp4', headers: { 'Content-Range': `bytes ${start}-${end}/${bytes.length}`, 'Accept-Ranges': 'bytes' }, body: bytes.subarray(start, end + 1) })
          }
          return route.fulfill({ body: bytes, contentType: 'video/mp4', headers: { 'Accept-Ranges': 'bytes' } })
        }
        return route.fulfill({ status: 404, body: '' })
      }
      if (p === '/api/providers/status') return route.fulfill({ json: [] })
      if (p === '/api/system') return route.fulfill({ json: { accel: 'cpu' } })
      return route.fulfill({ status: 404, json: { detail: 'missing route' } })
    })

    // Monitor Object URL creations and revocations
    await page.addInitScript(() => {
      window.__createdBlobUrls = []
      window.__revokedBlobUrls = []
      const origCreate = URL.createObjectURL
      const origRevoke = URL.revokeObjectURL
      URL.createObjectURL = function(blob) {
        const u = origCreate.call(URL, blob)
        window.__createdBlobUrls.push(u)
        return u
      }
      URL.revokeObjectURL = function(u) {
        window.__revokedBlobUrls.push(u)
        return origRevoke.call(URL, u)
      }
    })

    await page.goto(server.resolvedUrls.local[0] + 'jobs/job-1/clips')
    const button = name => page.getByRole('button', { name, exact: true })

    // The timeline is available immediately without changing an existing edit.
    await page.getByRole('button', { name: /^Slice 1:/ }).waitFor()
    assert.equal(edits.length, 0)
    // 1. Enable per-slice framing
    await page.getByText('Whole clip framing', { exact: true }).click()
    await button('Per-slice framing').click()
    await eventually(() => assert.equal(edits.length, 1))
    assert.equal(edits[0].patch.framing.mode, 'slices')
    assert.equal(edits[0].patch.framing.segments.length, 1)
    await page.getByRole('button', { name: /^Slice 1:/ }).waitFor()

    // 2. Split slice at playhead
    const video = page.locator('video').first()
    if (process.env.AUTOCLIP_TEST_VIDEO) {
      await page.waitForFunction(() => document.querySelector('video')?.readyState >= 1)
    }
    await video.evaluate(el => { el.currentTime = 4.0; el.dispatchEvent(new Event('timeupdate')) })
    await page.getByRole('button', { name: /^Split slice at playhead/ }).click()
    await eventually(() => assert.equal(edits.length, 2))
    const twoSlices = edits[1].patch.framing.segments
    assert.equal(twoSlices.length, 2)
    assert.equal(twoSlices[0].end_s, 4.0)
    assert.equal(twoSlices[1].start_s, 4.0)

    // A timeline click selects the part and seeks the actual player without saving.
    const firstSlice = page.getByRole('button', { name: /^Slice 1:/ })
    await firstSlice.click()
    await page.waitForFunction(() => Math.abs(document.querySelector('video').currentTime - 2) < 0.2)
    assert.equal(await firstSlice.getAttribute('aria-pressed'), 'true')
    const secondSlice = page.getByRole('button', { name: /^Slice 2:/ })
    await secondSlice.click()
    await page.waitForFunction(() => Math.abs(document.querySelector('video').currentTime - 7) < 0.2)
    assert.equal(await secondSlice.getAttribute('aria-pressed'), 'true')
    assert.equal(edits.length, 2)
    if (process.env.AUTOCLIP_SCREENSHOT) await page.screenshot({ path: process.env.AUTOCLIP_SCREENSHOT, fullPage: true })

    // 3. Select slice and set Manual mode, adjust zoom and Apply framing
    await button('Slice Manual').click()
    await page.getByText('Fine position', { exact: true }).click()
    if (process.env.AUTOCLIP_TEST_VIDEO) {
      await page.locator('[data-editing-framing="true"]').waitFor()
      assert.equal(await page.locator('video').count(), 1)
      assert.equal(await page.getByRole('group', { name: 'Framing slices' }).count(), 1)
      if (process.env.AUTOCLIP_RESIZE_SCREENSHOT) await page.screenshot({ path: process.env.AUTOCLIP_RESIZE_SCREENSHOT, fullPage: true })
    }
    const originalWidth = await video.evaluate(el => parseFloat(el.style.width))
    await page.getByRole('slider', { name: 'Zoom' }).focus()
    await page.keyboard.press('ArrowRight')
    await page.getByRole('status').filter({ hasText: 'Unsaved framing' }).waitFor()
    if (process.env.AUTOCLIP_TEST_VIDEO) {
      await video.evaluate(el => { el.currentTime = 7.5; el.dispatchEvent(new Event('timeupdate')) })
      await page.waitForFunction(() => Math.abs(document.querySelector('video').currentTime - 7.5) < 0.2)
      assert.ok(Number(await page.getByRole('slider', { name: 'Zoom' }).inputValue()) > 1)
      await page.waitForFunction(width => parseFloat(document.querySelector('video').style.width) > width, originalWidth)
      const before = Number(await page.getByRole('slider', { name: 'Center X' }).inputValue())
      const box = await page.getByTestId('clip-preview').boundingBox()
      await page.mouse.move(box.x + box.width * 0.5, box.y + box.height * 0.5)
      await page.mouse.down()
      await page.mouse.move(box.x + box.width * 0.7, box.y + box.height * 0.5, { steps: 4 })
      await page.mouse.up()
      await page.waitForFunction(value => Number(document.querySelector('[aria-label="Center X"]').value) < value, before)
    }
    await button('Apply framing').click()
    await eventually(() => assert.equal(edits.length, 4))
    assert.equal(edits[3].patch.framing.segments[1].framing.mode, 'manual')
    assert.ok(edits[3].patch.framing.segments[1].framing.zoom > 1.0)

    // 4. Merge with next slice
    await page.getByRole('button', { name: /^Slice 1:/ }).click()
    await button('Merge with next slice').click()
    await eventually(() => assert.equal(edits.length, 5))
    assert.equal(edits[4].patch.framing.segments.length, 1)
    assert.equal(edits[4].patch.framing.segments[0].start_s, 0)
    assert.equal(edits[4].patch.framing.segments[0].end_s, 10)

    // 5. Reload persists slice state
    await page.reload()
    await page.getByRole('button', { name: /^Slice 1:/ }).waitFor()

    // 6. Clip independence: switch to clip-2, ensure framing is Auto
    await page.locator('li').filter({ hasText: 'Clip 2' }).click()
    await page.waitForFunction(() => Array.from(document.querySelectorAll('button')).some(el => el.textContent.trim() === 'Auto' && el.classList.contains('btn-primary')))
    assert.equal(clips[1].framing, null)
    await page.locator('li').filter({ hasText: 'Clip 1' }).click()
    await page.getByRole('button', { name: /^Slice 1:/ }).waitFor()

    // 7. Exact preview: render, error retry, busy 409
    failPreview = true
    await button('Render exact preview').click()
    await page.getByText('Preview render failed').waitFor()

    await button('Retry exact preview').click()
    await page.getByRole('link', { name: 'Open exact preview' }).waitFor()
    assert.equal(previewRequests.length, 2)

    // 409 busy test
    previewBusy = true
    await button('Re-render exact preview').click()
    await page.getByText('Preview renderer busy').waitFor()
    previewBusy = false

    // 8. Revoke blob URLs on clear / stale responses
    const createdCountBefore = await page.evaluate(() => window.__createdBlobUrls.length)
    await button('Clear preview').click()
    const revokedCountAfter = await page.evaluate(() => window.__revokedBlobUrls.length)
    assert.ok(revokedCountAfter >= createdCountBefore)

    // 9. Stale response while switching clip discarded
    holdPreview = true
    await button('Render exact preview').click()
    await page.locator('li').filter({ hasText: 'Clip 2' }).click()
    if (releaseHeldPreview) releaseHeldPreview()
    await page.waitForTimeout(100)
    assert.equal(await page.getByRole('link', { name: 'Open exact preview' }).count(), 0)

    // 10. Dirty framing / caption draft blocks preview button
    await page.locator('li').filter({ hasText: 'Clip 1' }).click()
    await button('Slice Manual').click()
    await page.getByRole('slider', { name: 'Zoom' }).focus()
    await page.keyboard.press('ArrowRight')
    await page.getByRole('status').filter({ hasText: 'Unsaved framing' }).waitFor()
    await page.waitForFunction(() => Array.from(document.querySelectorAll('button')).some(el => el.textContent.trim() === 'Render exact preview' && el.disabled))
    assert.equal(await button('Render exact preview').isDisabled(), true)
    await button('Discard framing changes').click()
    await page.waitForFunction(() => Array.from(document.querySelectorAll('button')).some(el => el.textContent.trim() === 'Render exact preview' && !el.disabled))
    assert.equal(await button('Render exact preview').isEnabled(), true)

    // Trim handles are on the same timeline as the slice buttons.
    const handle = page.getByRole('button', { name: 'Drag start of clip' })
    const track = page.getByRole('group', { name: 'Framing slices' })
    assert.equal(await track.count(), 1)
    const beforeTrim = await handle.boundingBox()
    const timelineBox = await track.boundingBox()
    await page.mouse.move(beforeTrim.x + beforeTrim.width / 2, beforeTrim.y + beforeTrim.height / 2)
    await page.mouse.down()
    await page.mouse.move(timelineBox.x + timelineBox.width * 0.2, beforeTrim.y + beforeTrim.height / 2, { steps: 5 })
    await page.mouse.up()
    await eventually(() => assert.ok(trims.length > 0))
    assert.ok(trims[0].start_s > 0)

    // 11. 375px mobile viewport has no horizontal overflow
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)
    assert.equal(overflow, false, 'No horizontal overflow on 375px viewport')

    assert.deepEqual(errors, [])
  } finally {
    if (browser) await browser.close()
    await server.close()
  }
}

main().catch(error => { console.error(error); process.exitCode = 1 })
