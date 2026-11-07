// Real gallery UI, synthetic HTTP media: bounded busy retry and unavailable fallback.
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
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } })
    const errors = [], requests = { a: 0, b: 0, c: 0 }
    page.on('pageerror', error => errors.push(error.message))
    const clips = ['a', 'b', 'c'].map((id, index) => ({ id, job_id: 'job', rank: index + 1,
      start_s: 10, end_s: 14, duration_s: 4, start_word: 0, end_word: 0, title: `Clip ${id}`,
      score: 90, reason: '', status: 'candidate', exports: [], caption_style: 'clean', ratio: '9:16' }))
    const job = { id: 'job', source_id: 'source', status: 'done', progress: 1,
      source: { id: 'source', title: 'Synthetic gallery', duration_s: 20, has_video: true } }
    const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=', 'base64')
    await page.route('**/api/**', async route => {
      const url = new URL(route.request().url()), p = url.pathname
      if (p === '/api/system') return route.fulfill({ json: { accel: 'cpu', ready: true } })
      if (p === '/api/providers/status') return route.fulfill({ json: [] })
      if (p === '/api/jobs/job') return route.fulfill({ json: job })
      if (p === '/api/jobs/job/clips') return route.fulfill({ json: clips })
      if (p.endsWith('/thumbnail')) {
        const id = p.split('/')[3]
        requests[id]++
        assert.equal(url.searchParams.get('start'), '10')
        assert.equal(url.searchParams.get('end'), '14')
        if (id === 'b') return route.fulfill({ status: 404, body: '' })
        if (requests[id] < 3) return route.fulfill({ status: 503, headers: { 'Retry-After': '2' }, body: '' })
        return route.fulfill({ contentType: 'image/png', body: png })
      }
      return route.fulfill({ status: 404, body: '' })
    })
    await page.goto(`${server.resolvedUrls.local[0]}jobs/job/clips`)
    await page.getByRole('heading', { name: 'Synthetic gallery' }).waitFor()
    await page.waitForFunction(() => [...document.querySelectorAll('article img')].filter(img => img.complete && img.naturalWidth > 0).length === 2, { timeout: 20000 })
    assert.equal(await page.locator('article img[loading="lazy"]').count(), 2)
    await page.getByText('Preview unavailable · media missing, audio only, or renderer busy').waitFor({ timeout: 20000 })
    assert.deepEqual(requests, { a: 3, b: 5, c: 3 }, 'busy retries recover and permanent failure stops')
    await page.waitForTimeout(1500)
    assert.equal(requests.b, 5)
    await page.getByRole('button', { name: 'Retry preview' }).click()
    await page.waitForFunction(() => document.querySelector('article [role="status"] p')?.textContent === 'Loading preview…')
    assert.ok(requests.b >= 6)
    await page.setViewportSize({ width: 375, height: 900 })
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false)
    assert.deepEqual(errors, [])
    console.log(JSON.stringify({ passed: true, requests, errors }))
  } finally {
    if (browser) await browser.close()
    await server.close()
  }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
