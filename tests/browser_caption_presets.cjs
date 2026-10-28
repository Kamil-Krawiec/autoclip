// Synthetic API fixture exercising caption presets drawer, atomic PATCH and draft guards.
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

const style = (key, label, sizeRatio, marginRatio = 0.12, positionX = 0.5, positionY = 0.88) => ({
  key, label, description: 'Synthetic preset style', preview: {
    font: 'Arial', primary: '#ffffff', accent: null, outline: '#000000', outlineWidth: 2,
    allCaps: false, sizeRatio, marginRatio, positionX, positionY, boxed: false,
    animation: 'none', maxWords: 5,
  },
})

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
    const page = await browser.newPage({ viewport: { width: 375, height: 900 } })
    page.setDefaultTimeout(10000)
    const errors = [], edits = []
    let failNextEdit = false, holdNextEdit = false, releaseHeldEdit, notifyEditHeld
    const editHeld = new Promise(resolve => { notifyEditHeld = resolve })
    page.on('pageerror', error => errors.push(error.message))

    const clips = [{
      id: 'clip-a', job_id: 'job-1', rank: 1, start_s: 0, end_s: 5, duration_s: 5,
      start_word: 0, end_word: 1, title: 'Clip A', hook: '', score: 90,
      reason: 'Fixture', status: 'kept', user_trimmed: false, caption_style: 'karaoke_lower',
      ratio: '9:16', burn_captions: true, framing: null,
      caption_overrides: { size_ratio: 0.08, position_x: 0.3, position_y: 0.4 }, exports: [],
    }]
    const job = {
      id: 'job-1', source_id: 'src-1', status: 'done', current_stage: 'done', progress: 1,
      error: null, provider: 'fixture', created_at: '', updated_at: '', started_at: null, finished_at: null,
      source: { id: 'src-1', type: 'upload', title: 'Fixture', url: null, filename: 'fixture.mp4',
        duration_s: 5, width: 1920, height: 1080, fps: 30, has_audio: false, has_video: true, created_at: '' },
    }
    const styles = [
      style('karaoke_lower', 'Karaoke Lower', 0.048, 0.12, 0.5, 0.88),
      style('bold_pop_lower', 'Bold Pop Lower', 0.052, 0.14, 0.5, 0.86),
      style('bold_pop', 'Bold Pop', 0.062, 0.18, 0.5, 0.82),
      style('karaoke_fill', 'Karaoke Fill', 0.056, 0.22, 0.5, 0.78),
    ]

    await page.route('**/api/**', async route => {
      const req = route.request(), url = new URL(req.url()), p = url.pathname
      if (p === '/api/jobs/job-1') return route.fulfill({ json: job })
      if (p === '/api/jobs/job-1/clips') return route.fulfill({ json: clips })
      if (p === '/api/caption-styles') return route.fulfill({ json: styles })
      if (p.endsWith('/words')) return route.fulfill({ json: [{ text: 'przetestowane', start: 0, end: 4, speaker: null }] })
      if (p.endsWith('/crop-path')) return route.fulfill({ json: { source_width: 1920, source_height: 1080,
        segments: [{ start_s: 0, end_s: 5, width: 608, height: 1080, keyframes: [{ t: 0, x: 656, y: 0 }], strategy: 'general', zoom: 1, fit: false }] } })
      if (p.endsWith('/edit') && req.method() === 'PATCH') {
        const id = p.split('/')[3], patch = req.postDataJSON()
        edits.push({ id, patch })
        if (failNextEdit) { failNextEdit = false; return route.fulfill({ status: 500, json: { detail: 'Preset patch failure' } }) }
        if (holdNextEdit) {
          holdNextEdit = false
          notifyEditHeld()
          await new Promise(resolve => { releaseHeldEdit = resolve })
        }
        Object.assign(clips.find(c => c.id === id), patch)
        return route.fulfill({ json: clips.find(c => c.id === id) })
      }
      if (p.endsWith('/export')) return route.fulfill({ json: { id: 'exp', clip_id: 'clip-a', ratio: '9:16', style: 'karaoke_lower', size_bytes: 1, created_at: '', download_url: '#' } })
      if (p.endsWith('/media')) return route.fulfill({ status: 404, body: '' })
      if (p === '/api/providers/status') return route.fulfill({ json: [] })
      if (p === '/api/system') return route.fulfill({ json: { accel: 'cpu' } })
      return route.fulfill({ status: 404, json: { detail: 'missing route' } })
    })

    await page.goto(server.resolvedUrls.local[0] + 'jobs/job-1/clips/clip-a')
    const summary = page.locator('summary', { hasText: 'Advanced / legacy caption layout' })
    const details = page.locator('details').filter({ has: summary })
    const slider = name => page.getByRole('slider', { name, exact: true })
    const button = name => page.getByRole('button', { name, exact: true })

    await page.getByRole('button', { name: /^Karaoke Lower/ }).waitFor()
    assert.equal(edits.length, 0, 'no PATCH on initial load')
    await page.getByText('Custom layout active; choose a preset to restore its layout.').waitFor()
    assert.equal(await details.evaluate(el => el.open), false, 'advanced layout details is default closed')

    await summary.focus()
    await page.keyboard.press('Enter')
    assert.equal(await details.evaluate(el => el.open), true, 'keyboard Enter opens details drawer')
    assert.equal(await slider('Caption size').inputValue(), '8', 'reflects custom size override')
    assert.equal(await slider('Caption horizontal position').inputValue(), '30', 'reflects custom position X override')
    assert.equal(await slider('Caption vertical position').inputValue(), '40', 'reflects custom position Y override')

    await page.getByRole('button', { name: /^Karaoke Lower/ }).click()
    await eventually(() => assert.equal(edits.length, 1))
    assert.deepEqual(edits[0].patch, { caption_style: 'karaoke_lower', caption_overrides: null })
    await page.waitForFunction(() => document.querySelector('[aria-label="Caption size"]')?.value === '4.8')
    assert.equal(await slider('Caption size').inputValue(), '4.8')
    assert.equal(await slider('Caption horizontal position').inputValue(), '50')
    assert.equal(await slider('Caption vertical position').inputValue(), '88')
    assert.equal(await page.getByRole('status').filter({ hasText: 'Unsaved caption layout' }).count(), 0, 'no false dirty after reset')

    await page.reload()
    await summary.click()
    assert.equal(await slider('Caption size').inputValue(), '4.8', 'persisted defaults after reload')
    edits.length = 0

    await page.getByRole('button', { name: /^Bold Pop Lower/ }).click()
    await eventually(() => assert.equal(edits.length, 1))
    assert.deepEqual(edits[0].patch, { caption_style: 'bold_pop_lower', caption_overrides: null })
    await page.waitForFunction(() => document.querySelector('[aria-label="Caption size"]')?.value === '5.2')
    assert.equal(await slider('Caption vertical position').inputValue(), '86')

    failNextEdit = true
    await page.getByRole('button', { name: /^Karaoke Lower/ }).click()
    await page.getByText('Preset patch failure', { exact: true }).waitFor()
    assert.equal(clips[0].caption_style, 'bold_pop_lower', 'failed preset patch preserves saved style')

    holdNextEdit = true
    await page.getByRole('button', { name: /^Karaoke Lower/ }).click()
    await editHeld
    assert.equal(await page.getByRole('button', { name: /^Karaoke Lower/ }).isDisabled(), true, 'pending lock disables presets')
    releaseHeldEdit()
    await page.getByRole('button', { name: /^Karaoke Lower/ }).waitFor({ state: 'visible' })
    await page.waitForFunction(() => !Array.from(document.querySelectorAll('button')).find(el => /^Karaoke Lower/.test(el.textContent || ''))?.disabled)

    await slider('Caption horizontal position').focus()
    await page.keyboard.press('ArrowRight')
    assert.equal(await page.getByRole('status').filter({ hasText: 'Unsaved caption layout' }).count(), 1)
    await summary.click()
    assert.equal(await details.evaluate(el => el.open), false, 'drawer can close while dirty')
    edits.length = 0
    await page.getByRole('button', { name: /^Bold Pop Lower/ }).click()
    assert.equal(edits.length, 0, 'dirty layout blocks preset change')
    assert.equal(await button('Export this clip').isDisabled(), true, 'dirty layout blocks export')
    await summary.click()
    assert.equal(await slider('Caption horizontal position').inputValue(), '51', 'dirty draft preserved on reopen')
    await button('Discard caption layout').click()
    await page.waitForFunction(() => !Array.from(document.querySelectorAll('button')).find(el => el.textContent?.trim() === 'Export this clip')?.disabled)
    assert.equal(await button('Export this clip').isEnabled(), true, 'discard unblocks export')

    assert.deepEqual(errors, [])
  } finally {
    if (browser) await browser.close()
    await server.close()
  }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
