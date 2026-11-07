// Tabs keep editor controls mounted and preserve independent drafts at desktop and mobile widths.
const assert = require('node:assert/strict')
const path = require('node:path')
const { pathToFileURL } = require('node:url')
const { chromium } = require(process.env.AUTOCLIP_PLAYWRIGHT_MODULE || 'playwright')

async function main() {
  const { createServer } = await import(pathToFileURL(require.resolve('../frontend/node_modules/vite/dist/node/index.js')))
  const server = await createServer({ root: path.join(__dirname, '../frontend'), configFile: path.join(__dirname, '../frontend/vite.config.ts'), server: { host: '127.0.0.1', port: 0 } })
  let browser
  try {
    await server.listen()
    browser = await chromium.launch({ headless: true })
    const page = await browser.newPage({ viewport: { width: 375, height: 900 } })
    const errors = [], edits = []
    const clip = { id: 'a', job_id: 'job', rank: 1, start_s: 0, end_s: 5, duration_s: 5,
      start_word: 0, end_word: 1, title: 'Clip A', hook: 'A synthetic hook', score: 90,
      reason: 'A synthetic reason', status: 'kept', user_trimmed: false, caption_style: 'clean',
      ratio: '9:16', burn_captions: true, framing: null,
      caption_overrides: null, exports: [] }
    const job = { id: 'job', source_id: 'source', status: 'done', current_stage: 'done', progress: 1,
      error: null, provider: 'fixture', created_at: '', updated_at: '', started_at: null, finished_at: null,
      source: { id: 'source', type: 'upload', title: 'Synthetic source', url: null, filename: 'fixture.mp4',
        duration_s: 5, width: 1920, height: 1080, fps: 30, has_audio: false, has_video: true, created_at: '' } }
    const style = { key: 'clean', label: 'Clean', description: 'Synthetic preset', preview: {
      font: 'Arial', primary: '#ffffff', accent: null, outline: '#000000', outlineWidth: 2,
      allCaps: false, sizeRatio: 0.05, marginRatio: 0.1, boxed: false, animation: 'none', maxWords: 5,
    } }
    page.on('pageerror', error => errors.push(error.message))
    await page.route('**/api/**', async route => {
      const req = route.request(), p = new URL(req.url()).pathname
      if (p === '/api/system') return route.fulfill({ json: { accel: 'cpu', ready: true } })
      if (p === '/api/providers/status') return route.fulfill({ json: [] })
      if (p === '/api/jobs/job') return route.fulfill({ json: job })
      if (p === '/api/jobs/job/clips') return route.fulfill({ json: [clip] })
      if (p === '/api/caption-styles') return route.fulfill({ json: [style] })
      if (p === '/api/clips/a/words') return route.fulfill({ json: [{ text: 'hello', start: 0, end: 1, speaker: null }] })
      if (p === '/api/clips/a/crop-path') return route.fulfill({ json: { source_width: 1920, source_height: 1080,
        segments: [{ start_s: 0, end_s: 5, width: 608, height: 1080, keyframes: [{ t: 0, x: 656, y: 0 }], strategy: 'general', zoom: 1, fit: false }] } })
      if (p.endsWith('/edit') && req.method() === 'PATCH') {
        const patch = req.postDataJSON(); edits.push(patch); Object.assign(clip, patch)
        return route.fulfill({ json: clip })
      }
      if (p.endsWith('/media')) {
        if (!process.env.AUTOCLIP_TEST_VIDEO) return route.fulfill({ status: 404, body: '' })
        const fs = require('node:fs'), bytes = fs.readFileSync(process.env.AUTOCLIP_TEST_VIDEO)
        const match = /^bytes=(\d+)-(\d*)$/.exec(req.headers().range || '')
        if (match) {
          const start = Number(match[1]), end = match[2] ? Math.min(Number(match[2]), bytes.length - 1) : bytes.length - 1
          return route.fulfill({ status: 206, contentType: 'video/mp4', headers: { 'Content-Range': `bytes ${start}-${end}/${bytes.length}`, 'Accept-Ranges': 'bytes' }, body: bytes.subarray(start, end + 1) })
        }
        return route.fulfill({ body: bytes, contentType: 'video/mp4', headers: { 'Accept-Ranges': 'bytes' } })
      }
      return route.fulfill({ status: 404, json: { detail: `Missing fixture route ${p}` } })
    })

    await page.goto(`${server.resolvedUrls.local[0]}jobs/job/clips/a`)
    const panelButton = name => page.getByRole('button', { name, exact: true })
    const slider = name => page.getByRole('slider', { name, exact: true })
    await page.getByTestId('clip-preview').waitFor()
    await page.getByRole('button', { name: /^Captions$/ }).waitFor()
    assert.equal(await page.locator('video').count(), 1, 'editor uses one player')
    assert.equal(await page.getByText('A synthetic reason').isVisible(), false)
    await page.locator('summary', { hasText: 'Why this clip' }).click()
    assert.equal(await page.getByText('A synthetic reason').isVisible(), true)
    assert.equal(await page.getByText('A synthetic hook').isVisible(), true)
    assert.equal(await panelButton('Captions').getAttribute('aria-pressed'), 'true')

    await page.locator('summary', { hasText: 'Advanced / legacy caption layout' }).click()
    await slider('Caption horizontal position').focus()
    await slider('Caption horizontal position').press('ArrowRight')
    const captionDraft = await slider('Caption horizontal position').inputValue()
    assert.equal(await page.getByRole('status').filter({ hasText: 'Unsaved caption layout' }).count(), 1)
    await panelButton('Framing').click()
    assert.equal(await panelButton('Framing').getAttribute('aria-pressed'), 'true')
    assert.equal(await slider('Caption horizontal position').isVisible(), false)
    assert.equal(await page.locator('video').count(), 1)
    await panelButton('Captions').click()
    assert.equal(await slider('Caption horizontal position').inputValue(), captionDraft, 'caption layout draft survives switching away and back')
    await panelButton('Transcript').click()
    await page.getByRole('button', { name: 'hello', exact: true }).click()
    await page.getByRole('textbox').fill('edited')
    await panelButton('Captions').click()
    await panelButton('Transcript').click()
    await page.getByRole('button', { name: 'edited', exact: true }).waitFor()
    await page.getByText('Unsaved changes', { exact: true }).waitFor()
    assert.equal(await page.getByRole('textbox').count(), 0, 'blur commits text into the unsaved transcript draft')
    await panelButton('Captions').click()
    await page.getByRole('button', { name: 'Discard caption layout' }).click()
    await panelButton('Transcript').click()
    await page.getByRole('button', { name: 'Save edits', exact: true }).click()
    await page.getByText('Click any word to correct it').waitFor()
    await panelButton('Framing').click()
    await page.getByRole('button', { name: 'Slice Manual', exact: true }).click()
    const cropFrame = page.getByTestId('crop-frame')
    await cropFrame.waitFor()
    await cropFrame.focus()
    await page.keyboard.press('ArrowRight')
    const framingDraftLeft = await cropFrame.evaluate(el => parseFloat(el.style.left))
    assert.equal(await page.getByRole('status').filter({ hasText: 'Unsaved framing' }).count(), 1)
    await panelButton('Transcript').click()
    await panelButton('Framing').click()
    assert.equal(await cropFrame.evaluate(el => parseFloat(el.style.left)), framingDraftLeft, 'framing draft survives switching panels')
    assert.equal(edits.length, 2, 'only the transcript save and explicit framing mode change persisted')

    for (const width of [375, 768, 1440]) {
      await page.setViewportSize({ width, height: 1000 })
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)
      assert.equal(overflow, false, `no horizontal overflow at ${width}px`)
    }
    assert.deepEqual(errors, [])
    console.log(JSON.stringify({ passed: true, widths: [375, 768, 1440], playerCount: 1, pageErrors: errors.length }))
  } finally {
    if (browser) await browser.close()
    await server.close()
  }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
