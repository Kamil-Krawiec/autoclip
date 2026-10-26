// Synthetic browser contract for Ingest's server-library flow. No backend, media or AI.
// Run: AUTOCLIP_PLAYWRIGHT_MODULE=/path/to/playwright node tests/browser_library.cjs
const assert = require('node:assert/strict')
const path = require('node:path')
const { pathToFileURL } = require('node:url')
const { chromium } = require(process.env.AUTOCLIP_PLAYWRIGHT_MODULE || 'playwright')

const longName = `${'very-long-library-name-'.repeat(6)}.mp4`
const longFolderName = 'very-long-folder-name-'.repeat(6)
const rootListing = {
  available: true,
  directory: '',
  entries: [
    { path: 'folder-a', name: 'Folder A', is_dir: true, size_bytes: null },
    { path: 'folder-b', name: 'Folder B', is_dir: true, size_bytes: null },
    { path: 'empty', name: 'Empty folder', is_dir: true, size_bytes: null },
    { path: 'missing-mount', name: 'Unavailable mount', is_dir: true, size_bytes: null },
    { path: 'folder-c', name: 'Folder C', is_dir: true, size_bytes: null },
    { path: 'long-folder', name: longFolderName, is_dir: true, size_bytes: null },
    { path: longName, name: longName, is_dir: false, size_bytes: 2048 },
  ],
  truncated: true,
}

async function main() {
  const { createServer } = await import(pathToFileURL(require.resolve('../frontend/node_modules/vite/dist/node/index.js')))
  const server = await createServer({
    root: path.join(__dirname, '../frontend'),
    configFile: path.join(__dirname, '../frontend/vite.config.ts'),
    server: { host: '127.0.0.1', port: 0, watch: { ignored: ['**/*.tsbuildinfo'] } },
  })
  let browser
  let releaseInitial
  let releaseFolderA
  let releaseFolderC
  let releaseImport
  let releaseJob
  try {
    await server.listen()
    browser = await chromium.launch({ headless: true })
    const page = await browser.newPage({ viewport: { width: 375, height: 900 } })
    page.setDefaultTimeout(10000)
    const errors = []
    page.on('pageerror', (error) => errors.push(error.message))
    const source = {
      id: 'library-source', type: 'library', title: 'Library source', url: null,
      filename: longName, library_path: `folder-a/${longName}`, duration_s: 60,
      width: 1920, height: 1080, fps: 30, has_audio: true, has_video: true, created_at: 'fixture',
    }
    const existingSource = { ...source, id: 'existing-source', title: 'Existing source', filename: 'existing.mp4' }
    const libraryRequests = []
    const importRequests = []
    const jobPosts = []
    const apiCalls = []
    let firstAFails = true
    const initialGate = new Promise((resolve) => { releaseInitial = resolve })
    const folderAGate = new Promise((resolve) => { releaseFolderA = resolve })
    const folderCGate = new Promise((resolve) => { releaseFolderC = resolve })
    const importGate = new Promise((resolve) => { releaseImport = resolve })
    const jobGate = new Promise((resolve) => { releaseJob = resolve })

    await page.route('**/api/**', async (route) => {
      const request = route.request()
      const url = new URL(request.url())
      apiCalls.push({ path: url.pathname, method: request.method() })
      if (url.pathname === '/api/library' && request.method() === 'GET') {
        const directory = url.searchParams.get('directory') || ''
        libraryRequests.push(directory)
        if (libraryRequests.length === 1) {
          await initialGate
          return route.fulfill({ json: rootListing })
        }
        if (directory === 'folder-a' && firstAFails) {
          firstAFails = false
          return route.fulfill({ status: 500, json: { detail: 'Fixture folder read failure' } })
        }
        if (directory === 'folder-a') {
          if (libraryRequests.filter((item) => item === 'folder-a').length === 3) await folderAGate
          return route.fulfill({ json: { ...rootListing, directory, entries: [
            { path: `folder-a/${longName}`, name: longName, is_dir: false, size_bytes: 4096 },
          ], truncated: false } })
        }
        if (directory === 'folder-b') {
          return route.fulfill({ json: { ...rootListing, directory, entries: [
            { path: 'folder-b/other.mp4', name: 'other.mp4', is_dir: false, size_bytes: 1024 },
          ], truncated: false } })
        }
        if (directory === 'folder-c') {
          await folderCGate
          return route.fulfill({ json: { ...rootListing, directory, entries: [], truncated: false } })
        }
        if (directory === 'empty') {
          return route.fulfill({ json: { available: true, directory, entries: [], truncated: false } })
        }
        if (directory === 'missing-mount') {
          return route.fulfill({ json: { available: false, directory: '', entries: [], truncated: false } })
        }
        return route.fulfill({ json: rootListing })
      }
      if (url.pathname === '/api/library/import' && request.method() === 'POST') {
        const body = request.postDataJSON()
        importRequests.push(body)
        if (importRequests.length === 1) {
          return route.fulfill({ status: 422, json: { detail: 'Fixture import failure' } })
        }
        await importGate
        return route.fulfill({ status: 201, json: source })
      }
      if (url.pathname === '/api/jobs' && request.method() === 'POST') {
        jobPosts.push(request.postDataJSON())
        await jobGate
        return route.fulfill({ json: { id: 'fixture-job' } })
      }
      if (url.pathname === '/api/sources') return route.fulfill({ json: [existingSource] })
      if (url.pathname === '/api/jobs') return route.fulfill({ json: [] })
      if (url.pathname === '/api/providers/status') return route.fulfill({ json: [] })
      if (url.pathname === '/api/system') return route.fulfill({ json: { accel: 'cpu', ready: true } })
      if (url.pathname === '/api/jobs/fixture-job') return route.fulfill({ json: {
        id: 'fixture-job', source, source_id: source.id, status: 'queued', current_stage: 'ingest', progress: 0,
      } })
      return route.fulfill({ status: 503, json: { detail: 'Fixture: unused API' } })
    })

    await page.goto(server.resolvedUrls.local[0])
    await page.getByText('Loading server media folder…', { exact: true }).waitFor()
    releaseInitial()
    await page.getByRole('button', { name: 'Folder A/', exact: true }).waitFor()
    await page.getByText('Showing first 7 items.', { exact: false }).waitFor()
    assert.equal(await page.getByLabel('Server media file').locator('option').count(), 1)
    assert.match(await page.getByLabel('Server media file').locator('option').first().textContent(), /2\.0 KB/)
    const overflow = await page.evaluate(() => ({
      width: document.documentElement.scrollWidth,
      offenders: Array.from(document.querySelectorAll('body *'))
        .filter((element) => element.getBoundingClientRect().right > window.innerWidth + 1)
        .slice(0, 5)
        .map((element) => ({ tag: element.tagName, text: element.textContent.trim().slice(0, 80), right: element.getBoundingClientRect().right })),
    }))
    assert.equal(overflow.width <= 375, true, `long library names must not cause horizontal overflow: ${JSON.stringify(overflow)}`)

    await page.getByRole('button', { name: 'Folder A/', exact: true }).click()
    await page.getByRole('alert').filter({ hasText: 'Fixture folder read failure' }).waitFor()
    await page.getByRole('button', { name: 'Retry', exact: true }).click()
    await page.getByText(`/${'folder-a'}`, { exact: true }).waitFor()
    assert.equal(libraryRequests.at(-1), 'folder-a', 'retry must target the failed directory')
    await page.getByRole('button', { name: '← Back', exact: true }).click()
    await page.getByText('Showing first 7 items.', { exact: false }).waitFor()

    // Force same-tick navigation so the latest response wins when the first arrives last.
    await page.evaluate(() => {
      const buttons = Array.from(document.querySelectorAll('button'))
      buttons.find((button) => button.textContent.trim() === 'Folder A/').click()
      buttons.find((button) => button.textContent.trim() === 'Folder B/').click()
    })
    await page.getByText('/folder-b', { exact: true }).waitFor()
    releaseFolderA()
    await page.waitForTimeout(50)
    assert.equal(await page.getByText('/folder-b', { exact: true }).count(), 1, 'stale folder response must not replace the latest listing')
    assert.equal(libraryRequests.includes('folder-a') && libraryRequests.includes('folder-b'), true)

    await page.getByRole('button', { name: '← Back', exact: true }).click()
    await page.getByRole('button', { name: 'Folder A/', exact: true }).click()
    await page.getByText(`/${'folder-a'}`, { exact: true }).waitFor()
    await page.getByRole('button', { name: '← Back', exact: true }).click()
    await page.getByRole('button', { name: 'Empty folder/', exact: true }).click()
    await page.getByText('This folder is empty.', { exact: true }).waitFor()
    await page.getByRole('button', { name: '← Back', exact: true }).click()
    await page.getByRole('button', { name: 'Unavailable mount/', exact: true }).click()
    await page.getByText('No server media folder available', { exact: true }).waitFor()

    // Unmount with an in-flight listing and release its response after navigation.
    await page.getByRole('link', { name: 'Settings', exact: true }).click()
    await page.getByRole('link', { name: 'New', exact: true }).click()
    await page.getByRole('button', { name: 'Folder C/', exact: true }).click()
    await page.getByText('Loading folder…', { exact: true }).waitFor()
    await page.getByRole('link', { name: 'Settings', exact: true }).click()
    releaseFolderC()
    await page.getByRole('link', { name: 'New', exact: true }).click()
    await page.getByRole('button', { name: 'Folder C/', exact: true }).waitFor()

    await page.getByLabel('Server media file').selectOption(longName)
    await page.getByRole('button', { name: 'Options', exact: true }).click()
    await page.getByLabel('Max clips', { exact: true }).fill('3')
    await page.getByRole('button', { name: 'Process file', exact: true }).click()
    await page.getByText('Fixture import failure', { exact: true }).waitFor()
    assert.equal(await page.getByLabel('Server media file').inputValue(), longName, 'import failure must preserve selection')
    assert.equal(await page.getByRole('button', { name: 'Process file', exact: true }).isEnabled(), true)

    const importRequest = page.waitForRequest((request) => request.url().endsWith('/api/library/import') && request.method() === 'POST')
    await page.getByRole('button', { name: 'Process file', exact: true }).click()
    await page.getByRole('button', { name: 'Starting…', exact: true }).waitFor()
    await importRequest
    assert.equal(await page.getByLabel('Paste a link', { exact: true }).isDisabled(), true)
    assert.equal(await page.getByLabel('Existing source', { exact: true }).isDisabled(), true)
    assert.equal(await page.locator('input[type=file]').isDisabled(), true)
    assert.equal(await page.getByLabel('Server media file').isDisabled(), true)
    await page.getByLabel('Max clips', { exact: true }).fill('7')
    const jobRequest = page.waitForRequest((request) => request.url().endsWith('/api/jobs') && request.method() === 'POST')
    releaseImport()
    await jobRequest
    await page.waitForTimeout(20)
    assert.deepEqual(importRequests, [{ path: longName }, { path: longName }])
    assert.equal(jobPosts.length, 1, 'successful library import must create exactly one job')
    assert.deepEqual(jobPosts, [{ source_id: 'library-source', settings: { max_clips: 3 } }])
    assert.equal(apiCalls.filter(({ path, method }) => method === 'POST' && path === '/api/jobs').length, 1)
    assert.equal(apiCalls.some(({ path, method }) => method === 'POST' && (path.startsWith('/api/sources') || path === '/api/sources/youtube')), false)
    releaseJob()
    await page.waitForURL('**/jobs/fixture-job')

    assert.deepEqual(errors, [])
    console.log('PASS browser library: loading, nested/back browsing, sizes/truncation, race/retry/empty, import retry, job snapshot/guards, 375px overflow')
  } finally {
    if (browser) await browser.close()
    await server.close()
  }
}

main().catch((error) => { console.error(error); process.exitCode = 1 })
