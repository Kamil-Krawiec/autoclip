// Run only against a disposable backend with the worker disabled and M1 fixtures seeded.
// AUTOCLIP_PLAYWRIGHT_MODULE may point to an external Playwright installation.
const assert = require('node:assert/strict')
const { firefox } = require(process.env.AUTOCLIP_PLAYWRIGHT_MODULE || 'playwright')

const baseURL = process.env.AUTOCLIP_TEST_URL || 'http://127.0.0.1:19879'
const screenshot = process.env.AUTOCLIP_SCREENSHOT || '/tmp/autoclip-m1-browser.png'

async function main() {
  const browser = await firefox.launch({ headless: true })
  try {
    const context = await browser.newContext({ baseURL, viewport: { width: 1440, height: 1100 } })
    const page = await context.newPage()
    page.setDefaultTimeout(10000)
    const pageErrors = []
    page.on('pageerror', (error) => pageErrors.push(error.message))

    await page.route('**/api/providers/status', (route) => route.fulfill({ json: [
      { name: 'ollama', available: true, requires_key: false, has_key: false, detail: 'Browser fixture' },
    ] }))
    await page.route('**/api/system', (route) => route.fulfill({ json: {
      accel: 'cpu', nvenc_works: false, python_version: '3.11',
    } }))

    let sourceMode = 'loading'
    let releaseSources
    const sourcesGate = new Promise((resolve) => { releaseSources = resolve })
    await page.route('**/api/sources', async (route) => {
      if (sourceMode === 'loading') await sourcesGate
      if (sourceMode === 'empty') return route.fulfill({ json: [] })
      if (sourceMode === 'error') {
        return route.fulfill({ status: 500, json: { detail: 'Source list unavailable' } })
      }
      return route.continue()
    })

    let ingestRequests = 0
    await page.route('**/api/sources/**', (route) => {
      if (route.request().method() === 'POST') {
        ingestRequests += 1
        return route.abort()
      }
      return route.continue()
    })

    let jobPosts = 0
    let jobMode = 'error'
    let releaseJob
    const jobGate = new Promise((resolve) => { releaseJob = resolve })
    let newJob
    await page.route('**/api/jobs', async (route) => {
      if (route.request().method() !== 'POST') return route.continue()
      jobPosts += 1
      if (jobMode === 'error') {
        return route.fulfill({ status: 500, json: { detail: 'Create job unavailable' } })
      }
      assert.deepEqual(route.request().postDataJSON(), {
        source_id: 'm1-source', settings: { max_clips: 3 },
      })
      await jobGate
      const response = await route.fetch()
      assert.equal(response.status(), 201)
      newJob = await response.json()
      await route.fulfill({ response })
    })

    await page.goto('/')
    await page.getByText('Loading saved sources…', { exact: true }).waitFor()
    sourceMode = 'empty'
    releaseSources()
    await page.getByText('No saved sources yet.', { exact: true }).waitFor()
    assert.equal(await page.getByRole('button', { name: 'Process again', exact: true }).count(), 0)

    sourceMode = 'error'
    await page.reload()
    await page.getByText('Could not load saved sources', { exact: false }).waitFor()
    sourceMode = 'real'
    await page.getByRole('button', { name: 'Retry', exact: true }).click()
    const sourceSelect = page.getByLabel('Existing source', { exact: true })
    await sourceSelect.waitFor()
    assert.equal(await sourceSelect.inputValue(), 'm1-source')
    assert.match(await sourceSelect.textContent(), /Kod Zero test source/)
    const openClips = page.getByRole('link', { name: 'Open clips', exact: true })
    assert.equal(await openClips.getAttribute('href'), '/jobs/m1-old-job/clips')
    await openClips.click()
    await page.waitForURL('**/jobs/m1-old-job/clips')
    await page.goto('/')
    await sourceSelect.waitFor()

    await page.getByRole('button', { name: 'Process again', exact: true }).click()
    await page.getByText('Create job unavailable', { exact: true }).waitFor()
    assert.equal(await sourceSelect.isEnabled(), true)
    assert.equal(await page.locator('input[type="file"]').isEnabled(), true)
    assert.equal(await page.getByRole('button', { name: 'Process again', exact: true }).isEnabled(), true)
    assert.equal(jobPosts, 1)

    jobMode = 'real'
    jobPosts = 0
    await page.getByRole('button', { name: 'Options', exact: true }).click()
    await page.getByLabel('Max clips', { exact: true }).fill('3')
    await page.getByRole('button', { name: 'Process again', exact: true }).evaluate((button) => {
      // Dispatch both submits before React can render the disabled state.
      button.form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
      button.form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
      const transfer = new DataTransfer()
      transfer.items.add(new File(['synthetic'], 'duplicate.mp4', { type: 'video/mp4' }))
      document.querySelector('[role="button"]').dispatchEvent(new DragEvent('drop', {
        bubbles: true, cancelable: true, dataTransfer: transfer,
      }))
    })
    await page.getByRole('button', { name: 'Starting…', exact: true }).waitFor()
    assert.equal(await sourceSelect.isDisabled(), true)
    assert.equal(await page.locator('input[type="file"]').isDisabled(), true)
    assert.equal(await page.getByRole('button', { name: 'Drop video or audio' }).getAttribute('aria-disabled'), 'true')
    // A same-origin round trip lets queued duplicate requests reach the route handler.
    assert.equal((await page.request.get('/api/health')).ok(), true)
    assert.equal(jobPosts, 1)
    assert.equal(ingestRequests, 0)
    await page.screenshot({ path: screenshot, fullPage: true })
    releaseJob()
    await page.waitForURL(/\/jobs\/[^/]+$/)
    assert.ok(newJob)
    assert.notEqual(newJob.id, 'm1-old-job')
    assert.equal(newJob.source_id, 'm1-source')
    assert.equal(newJob.status, 'queued')
    assert.equal(new URL(page.url()).pathname, `/jobs/${newJob.id}`)
    assert.equal(jobPosts, 1)
    assert.equal(ingestRequests, 0)
    const oldWords = await page.request.get('/api/clips/m1-old-clip/words')
    assert.equal(oldWords.status(), 200)
    assert.ok((await oldWords.json()).some((word) => word.text === 'Poprawione'))
    const oldJob = await page.request.get('/api/jobs/m1-old-job')
    assert.equal(oldJob.status(), 200)
    assert.equal((await oldJob.json()).status, 'done')
    assert.deepEqual(pageErrors, [])
    console.log(JSON.stringify({ passed: true, newJob: newJob.id, jobPosts, ingestRequests, screenshot }))
  } finally {
    await browser.close()
  }
}

main().catch((error) => {
  console.error(error)
  process.exitCode = 1
})
