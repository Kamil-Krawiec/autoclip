// Synthetic browser contract: actual Ingest/API, controlled XHR; no backend, media or AI.
// Run: AUTOCLIP_PLAYWRIGHT_MODULE=/path/to/playwright node tests/browser_upload.cjs
const assert = require('node:assert/strict')
const path = require('node:path')
const { pathToFileURL } = require('node:url')
const { chromium } = require(process.env.AUTOCLIP_PLAYWRIGHT_MODULE || 'playwright')

async function main() {
  const { createServer } = await import(pathToFileURL(require.resolve('../frontend/node_modules/vite/dist/node/index.js')))
  const server = await createServer({
    root: path.join(__dirname, '../frontend'),
    configFile: path.join(__dirname, '../frontend/vite.config.ts'),
    server: { host: '127.0.0.1', port: 0, watch: { ignored: ['**/*.tsbuildinfo'] } },
  })
  let browser
  try {
    await server.listen()
    browser = await chromium.launch({ headless: true })
    const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } })
    page.setDefaultTimeout(10000)
    const errors = []
    page.on('pageerror', (error) => errors.push(error.message))
    await page.addInitScript(() => {
      window.testUploads = []
      class UploadXHR extends EventTarget {
        constructor() {
          super()
          this.upload = new EventTarget()
          window.testUploads.push(this)
        }
        open(method, url) { this.method = method; this.url = url }
        send(form) { this.filename = form.get('file').name }
        abort() { this.aborted = true; this.dispatchEvent(new Event('abort')) }
        progress(loaded, total, lengthComputable = true) {
          this.upload.dispatchEvent(new ProgressEvent('progress', { loaded, total, lengthComputable }))
        }
        uploaded() { this.upload.dispatchEvent(new Event('load')) }
        complete(status = 201, body = { id: 'uploaded-source' }) {
          this.status = status
          this.statusText = 'Fixture response'
          this.responseText = JSON.stringify(body)
          this.dispatchEvent(new Event('load'))
        }
      }
      window.XMLHttpRequest = UploadXHR
    })
    const source = { id: 'saved-source', title: 'Saved source', duration_s: 60 }
    let jobPosts = []
    let youtubePosts = 0
    let releaseJob
    const jobGate = new Promise((resolve) => { releaseJob = resolve })
    await page.route('**/api/**', async (route) => {
      const request = route.request()
      const url = new URL(request.url())
      if (url.pathname === '/api/jobs' && request.method() === 'POST') {
        jobPosts.push(request.postDataJSON())
        await jobGate
        return route.fulfill({ json: { id: 'fixture-job' } })
      }
      if (url.pathname === '/api/sources/youtube') youtubePosts += 1
      if (url.pathname === '/api/sources') return route.fulfill({ json: [source] })
      if (url.pathname === '/api/jobs') return route.fulfill({ json: [] })
      if (url.pathname === '/api/providers/status') return route.fulfill({ json: [
        { name: 'fixture', available: true, models: [], detail: '', has_key: false, requires_key: false },
      ] })
      if (url.pathname === '/api/system') return route.fulfill({ json: { accel: 'cpu', ready: true } })
      if (url.pathname === '/api/jobs/fixture-job') return route.fulfill({ json: {
        id: 'fixture-job', source, source_id: source.id, status: 'queued', current_stage: 'ingest', progress: 0,
      } })
      return route.fulfill({ status: 503, json: { detail: 'Fixture: unused API' } })
    })
    await page.goto(server.resolvedUrls.local[0])
    await page.getByLabel('Existing source', { exact: true }).waitFor()
    await page.getByLabel('Paste a link', { exact: true }).fill('https://youtube.com/watch?v=fixture')
    await page.getByRole('button', { name: 'Options', exact: true }).click()
    await page.getByLabel('Max clips', { exact: true }).fill('3')
    const file = { name: 'same-file.mp4', mimeType: 'video/mp4', buffer: Buffer.alloc(100) }
    const input = page.locator('input[type=file]')
    const progress = page.getByRole('progressbar', { name: 'File upload progress' })
    await input.setInputFiles(file)
    await page.getByRole('button', { name: 'Cancel', exact: true }).waitFor()
    await page.evaluate(() => window.testUploads[0].progress(25, 100))
    await page.waitForFunction(() => document.querySelector('[role=progressbar]')?.getAttribute('aria-valuenow') === '25')
    assert.equal(await progress.getAttribute('aria-valuenow'), '25')
    assert.equal(jobPosts.length, 0, 'byte progress must not create a job')
    assert.equal(await input.isDisabled(), true)
    assert.equal(await page.getByLabel('Paste a link', { exact: true }).isDisabled(), true)
    assert.equal(await page.getByLabel('Existing source', { exact: true }).isDisabled(), true)
    await page.evaluate(() => {
      for (const form of document.querySelectorAll('form')) {
        form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
      }
      const dataTransfer = new DataTransfer()
      dataTransfer.items.add(new File(['duplicate'], 'duplicate.mp4', { type: 'video/mp4' }))
      document.querySelector('[role=button]').dispatchEvent(new DragEvent('drop', {
        bubbles: true, cancelable: true, dataTransfer,
      }))
    })
    assert.equal(await page.evaluate(() => window.testUploads.length), 1)
    assert.equal(jobPosts.length, 0)
    assert.equal(youtubePosts, 0)
    await page.getByRole('button', { name: 'Cancel', exact: true }).click()
    await page.getByRole('status').filter({ hasText: 'Upload cancelled' }).waitFor()
    assert.equal(await page.evaluate(() => window.testUploads[0].aborted), true)
    await page.evaluate(() => window.testUploads[0].complete())
    assert.equal(jobPosts.length, 0, 'late upload completion must not create a job')

    // The same file can be selected after cancel; validation is already server processing.
    await input.setInputFiles(file)
    await page.evaluate(() => window.testUploads[1].progress(40, 0, false))
    await page.waitForFunction(() => document.querySelector('[role=progressbar]')?.getAttribute('aria-valuenow') === null)
    assert.equal(await progress.getAttribute('aria-valuenow'), null)
    assert.match(await progress.getAttribute('aria-valuetext'), /40 B uploaded/)
    await page.evaluate(() => window.testUploads[1].uploaded())
    await page.getByText('Validating media…', { exact: true }).first().waitFor()
    assert.equal(jobPosts.length, 0)
    assert.equal(await page.getByRole('button', { name: 'Cancel', exact: true }).count(), 0)
    await page.evaluate(() => window.testUploads[1].complete(422, { detail: 'Validation fixture failure' }))
    await page.getByText('Validation fixture failure', { exact: true }).waitFor()

    await input.setInputFiles(file)
    await page.evaluate(() => window.testUploads[2].complete(422, {
      detail: { message: 'Invalid fixture video', hint: 'Choose a playable video' },
    }))
    await page.getByText('Invalid fixture video', { exact: true }).waitFor()
    await page.getByText('Choose a playable video', { exact: true }).waitFor()
    assert.equal(await input.isEnabled(), true)
    assert.equal(await page.getByLabel('Max clips', { exact: true }).inputValue(), '3')

    // Leaving Ingest aborts its transfer and late completion cannot start analysis.
    await input.setInputFiles(file)
    await page.getByRole('link', { name: 'Settings', exact: true }).click()
    await page.waitForURL('**/settings')
    assert.equal(await page.evaluate(() => window.testUploads[3].aborted), true)
    await page.evaluate(() => window.testUploads[3].complete())
    assert.equal(jobPosts.length, 0)
    await page.getByRole('link', { name: 'New', exact: true }).click()
    await page.getByLabel('Existing source', { exact: true }).waitFor()
    await page.getByRole('button', { name: 'Options', exact: true }).click()
    await page.getByLabel('Max clips', { exact: true }).fill('3')
    await input.setInputFiles(file)
    await page.evaluate(() => {
      window.testUploads[4].complete()
      // Response delivery and cancellation can occur before the async continuation runs.
      Array.from(document.querySelectorAll('button')).find((button) => button.textContent.trim() === 'Cancel').click()
    })
    await page.getByRole('status').filter({ hasText: 'Upload cancelled' }).waitFor()
    assert.equal(jobPosts.length, 0, 'cancel in the source-response microtask gap prevents a job')
    await input.setInputFiles(file)
    await page.evaluate(() => {
      window.testUploads[5].progress(100, 100)
      window.testUploads[5].uploaded()
    })
    await page.getByText('Validating media…', { exact: true }).first().waitFor()
    assert.equal(jobPosts.length, 0)
    // Options may remain editable: the job must retain the settings captured at submission.
    if (await page.getByLabel('Max clips', { exact: true }).isEnabled()) {
      await page.getByLabel('Max clips', { exact: true }).fill('7')
    }
    const jobRequest = page.waitForRequest((request) => request.url().endsWith('/api/jobs') && request.method() === 'POST')
    await page.evaluate(() => window.testUploads[5].complete())
    await page.getByText('Starting job…', { exact: true }).first().waitFor()
    assert.equal(await page.getByRole('button', { name: 'Cancel', exact: true }).count(), 0)
    assert.equal(await input.isDisabled(), true)
    await jobRequest
    assert.deepEqual(jobPosts, [{ source_id: 'uploaded-source', settings: { max_clips: 3 } }])
    releaseJob()
    await page.waitForURL('**/jobs/fixture-job')
    assert.equal(youtubePosts, 0)
    assert.deepEqual(errors, [])
    console.log('PASS browser upload: byte/unknown progress, validation/job phases, transfer cancel/retry, late completion, guards, hints, settings snapshot, unmount')
  } finally {
    if (browser) await browser.close()
    await server.close()
  }
}

main().catch((error) => { console.error(error); process.exitCode = 1 })
