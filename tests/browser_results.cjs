// Synthetic API fixture for results, exports, direct editor links, and dirty-draft guards.
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
    const errors = [], patchStatuses = [], exports = [], thumbnailRequests = []
    let failedJobLoads = 2, holdExport = false, releaseExport, notifyExportHeld
    const exportHeld = new Promise(resolve => { notifyExportHeld = resolve })
    const clips = ['a', 'b'].map((id, index) => ({
      id, job_id: 'job', rank: index + 1, start_s: 0, end_s: 5, duration_s: 5,
      start_word: 0, end_word: 1, title: `Clip ${id.toUpperCase()}`, hook: '', score: 90 - index,
      reason: `Reason ${id}`, status: index ? 'kept' : 'candidate', user_trimmed: false,
      caption_style: 'clean', ratio: '9:16', burn_captions: true, framing: null,
      caption_overrides: null, exports: [],
    }))
    const job = { id: 'job', source_id: 'source', status: 'done', current_stage: 'done', progress: 1, error: null,
      provider: 'fixture', created_at: '', updated_at: '', started_at: null, finished_at: null,
      source: { id: 'source', type: 'upload', title: 'Synthetic source', url: null, filename: 'fixture.mp4', channel: null,
        duration_s: 5, width: 1920, height: 1080, fps: 30, has_audio: false, has_video: true, created_at: '' } }
    page.on('pageerror', error => errors.push(error.message))
    await page.route('**/api/**', async route => {
      const req = route.request(), url = new URL(req.url()), p = url.pathname
      if (p === '/api/system') return route.fulfill({ json: { accel: 'cpu', ready: true } })
      if (p === '/api/providers/status') return route.fulfill({ json: [] })
      if (p === '/api/jobs/job' && failedJobLoads > 0) {
        failedJobLoads -= 1
        return route.fulfill({ status: 503, json: { detail: 'Synthetic initial load failure' } })
      }
      if (p === '/api/jobs/job') return route.fulfill({ json: job })
      if (p === '/api/jobs/empty') return route.fulfill({ json: { ...job, id: 'empty', source: { ...job.source, title: 'Empty source' } } })
      if (p === '/api/jobs/job/clips') return route.fulfill({ json: clips })
      if (p === '/api/jobs/empty/clips') return route.fulfill({ json: [] })
      if (/^\/api\/clips\/[ab]\/thumbnail$/.test(p)) {
        thumbnailRequests.push({ path: p, start: url.searchParams.get('start'), end: url.searchParams.get('end') })
        return route.fulfill({ status: 200, contentType: 'image/png', body: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j0eQAAAAASUVORK5CYII=', 'base64') })
      }
      if (p === '/api/caption-styles') return route.fulfill({ json: [] })
      if (p === '/api/clips/a/words' || p === '/api/clips/b/words') return route.fulfill({ json: [{ text: 'hello', start: 0, end: 1, speaker: null }] })
      if (p.endsWith('/crop-path')) return route.fulfill({ status: 404, json: { detail: 'No crop path' } })
      if (p.endsWith('/media')) return route.fulfill({ status: 404, body: '' })
      if (p === '/api/clips/a' && req.method() === 'PATCH') {
        const update = req.postDataJSON(); patchStatuses.push(update.status)
        Object.assign(clips[0], update)
        return route.fulfill({ json: clips[0] })
      }
      if ((p === '/api/clips/a' || p === '/api/clips/b') && req.method() === 'GET') return route.fulfill({ json: clips.find(c => p.endsWith(c.id)) })
      if (p.endsWith('/export')) {
        const id = p.split('/')[3]; exports.push(id)
        if (holdExport) await new Promise(resolve => { releaseExport = resolve; notifyExportHeld() })
        const clip = clips.find(c => c.id === id)
        clip.status = 'exported'
        clip.exports.push({ id: `export-${id}`, clip_id: id, ratio: '9:16', style: 'clean', size_bytes: 12, created_at: '2026-01-01T00:00:00Z', download_url: `/api/exports/export-${id}/download` })
        return route.fulfill({ json: clip.exports.at(-1) })
      }
      if (p.endsWith('/edit') && req.method() === 'PATCH') return route.fulfill({ json: clips.find(c => p.includes(c.id)) })
      return route.fulfill({ status: 404, json: { detail: `Fixture route missing: ${p}` } })
    })

    const base = server.resolvedUrls.local[0]
    await page.goto(`${base}jobs/job/clips`)
    await page.getByRole('button', { name: 'Retry loading results' }).waitFor()
    assert.equal(failedJobLoads, 0, 'both initial job loads failed before retry')
    await page.getByRole('button', { name: 'Retry loading results' }).click()
    await page.getByRole('heading', { name: 'Synthetic source' }).waitFor()
    const thumbnails = page.getByRole('img', { name: /^Source preview for Clip [AB]$/ })
    assert.equal(await thumbnails.count(), 2)
    for (const thumbnail of await thumbnails.all()) {
      await thumbnail.scrollIntoViewIfNeeded()
      await page.waitForFunction(image => image.complete && image.naturalWidth === 1, await thumbnail.elementHandle())
    }
    assert.deepEqual(thumbnailRequests.map(request => request.path), ['/api/clips/a/thumbnail', '/api/clips/b/thumbnail'])
    assert.ok(thumbnailRequests.every(request => request.start === '0' && request.end === '5'))
    assert.equal(await page.getByText('Reason a').isVisible(), false, 'reason starts collapsed')
    await page.getByText('Why this clip').first().focus()
    await page.keyboard.press('Enter')
    await page.getByText('Reason a').waitFor()
    await page.getByRole('button', { name: 'Keep', exact: true }).click()
    await page.getByRole('button', { name: 'Undo keep', exact: true }).first().waitFor()
    assert.deepEqual(patchStatuses, ['kept'])
    await page.getByRole('button', { name: 'Drop', exact: true }).first().click()
    await page.getByRole('button', { name: 'Undo drop' }).waitFor()
    await page.getByRole('button', { name: 'Undo drop' }).click()
    await page.getByRole('button', { name: 'Keep', exact: true }).click()
    await page.getByRole('button', { name: 'Export kept (2)' }).click()
    await page.getByRole('link', { name: /Download export/ }).nth(1).waitFor()
    assert.deepEqual(exports, ['a', 'b'], 'kept export is sequential and refreshes cards')
    assert.equal(await page.getByRole('link', { name: /Download export/ }).count(), 2)
    await page.getByRole('button', { name: 'Export again' }).last().click()
    await page.getByRole('link', { name: /Download export/ }).nth(2).waitFor()
    assert.deepEqual(exports, ['a', 'b', 'b'], 'individual export reloads export history')
    holdExport = true
    await page.getByRole('button', { name: 'Export again' }).first().click()
    await exportHeld
    assert.equal(await page.evaluate(() => {
      const event = new Event('beforeunload', { cancelable: true })
      window.dispatchEvent(event)
      return event.defaultPrevented
    }), true, 'active export prevents browser unload')
    releaseExport()
    await page.getByRole('link', { name: /Download export/ }).nth(3).waitFor()
    holdExport = false

    await page.getByRole('button', { name: 'Edit' }).first().click()
    await page.waitForURL('**/jobs/job/clips/a')
    await page.getByRole('heading', { name: 'Synthetic source' }).waitFor()
    await page.getByRole('button', { name: 'Transcript', exact: true }).click()
    await page.getByRole('button', { name: 'hello' }).click()
    await page.getByRole('textbox').fill('edited')
    await page.getByRole('textbox').press('Enter')
    await page.getByRole('link', { name: '← Results' }).click()
    await page.getByRole('alert').getByText('Unsaved edits will be lost').waitFor()
    await page.getByRole('button', { name: 'Stay' }).click()
    await page.waitForURL('**/jobs/job/clips/a')
    await page.getByRole('button', { name: 'Save edits' }).click()
    await page.getByRole('link', { name: '← Results' }).click()
    await page.waitForURL('**/jobs/job/clips')
    const dimensions = await page.evaluate(() => ({ viewport: innerWidth, content: document.documentElement.scrollWidth }))
    assert.ok(dimensions.content <= dimensions.viewport, `mobile overflow: ${JSON.stringify(dimensions)}`)
    await page.goto(`${base}jobs/empty/clips/missing`)
    await page.getByRole('heading', { name: 'Clip not found' }).waitFor()
    await page.getByRole('link', { name: 'Back to results' }).click()
    await page.waitForURL('**/jobs/empty/clips')
    await page.getByText('No clips are available for this job.').waitFor()
    assert.deepEqual(errors, [])
    console.log(JSON.stringify({ passed: true, statusPatches: patchStatuses.length, sequentialExports: exports, mobile: dimensions, pageErrors: errors.length }))
  } finally {
    if (browser) await browser.close()
    await server.close()
  }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
