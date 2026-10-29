// Synthetic API fixture for the Publish page.
//
// Regression for the review finding where the upload used the stored
// publication while the confirmation described the unsaved local select:
// the page must PATCH the form first, then confirm the saved privacy.
const assert = require('node:assert/strict')
const path = require('node:path')
const { pathToFileURL } = require('node:url')
const { chromium } = require(process.env.AUTOCLIP_PLAYWRIGHT_MODULE || 'playwright')

const PNG =
  'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=='

async function eventually(check, timeout = 10000) {
  const deadline = Date.now() + timeout
  while (true) {
    try {
      check()
      return
    } catch (error) {
      if (Date.now() > deadline) throw error
      await new Promise((resolve) => setTimeout(resolve, 30))
    }
  }
}

function fixture() {
  return {
    id: 'pub-a',
    export_id: 'exp-a',
    clip_id: 'clip-a',
    job_id: 'job-a',
    source_title: 'Fixture source',
    clip_title: 'Fixture clip',
    duration_s: 42,
    ratio: '9:16',
    export_created_at: '2026-10-01T10:00:00+00:00',
    status: 'ready',
    title: 'Zapisany tytuł',
    description: 'Zapisany opis',
    tags: ['tag'],
    best_day: null,
    thumbnail_prompt: 'Prompt',
    thumbnail_url: PNG,
    thumbnail_wide_url: PNG,
    privacy_status: 'public',
    youtube_video_id: null,
    youtube_url: null,
    error: null,
    ready_to_upload: true,
    created_at: '2026-10-01T10:00:00+00:00',
    updated_at: '2026-10-01T10:00:00+00:00',
    uploaded_at: null,
  }
}

async function main() {
  const { createServer } = await import(
    pathToFileURL(require.resolve('../frontend/node_modules/vite/dist/node/index.js'))
  )
  const server = await createServer({
    root: path.join(__dirname, '../frontend'),
    configFile: path.join(__dirname, '../frontend/vite.config.ts'),
    server: { host: '127.0.0.1', port: 0 },
  })
  let browser
  try {
    await server.listen()
    const launchOptions = { headless: true }
    if (process.env.AUTOCLIP_CHROMIUM_PATH) {
      launchOptions.executablePath = process.env.AUTOCLIP_CHROMIUM_PATH
    }
    browser = await chromium.launch(launchOptions)
    const page = await browser.newPage({ viewport: { width: 1280, height: 1000 } })
    page.setDefaultTimeout(10000)

    let publication = fixture()
    const events = []
    const dialogs = []
    const errors = []
    let nextDialogAction = 'dismiss'
    page.on('pageerror', (error) => errors.push(error.message))
    page.on('dialog', async (dialog) => {
      dialogs.push(dialog.message())
      if (nextDialogAction === 'accept') await dialog.accept()
      else await dialog.dismiss()
    })

    const status = {
      youtube_client_secret_present: true,
      youtube_token_present: true,
      youtube_ready: true,
      openai_image_key_present: true,
      metadata_provider: 'openai',
      metadata_model: 'gpt-6-luna',
      metadata_key_present: true,
      image_model: 'gpt-image-2.5-flare',
    }

    await page.route('**/api/**', async (route) => {
      const request = route.request()
      const url = new URL(request.url())
      const pathname = url.pathname
      if (pathname === '/api/system') {
        return route.fulfill({ json: { accel: 'cpu', gpu_name: null, ready: true } })
      }
      if (pathname === '/api/publish/status') return route.fulfill({ json: status })
      if (pathname === '/api/publish/pub-a' && request.method() === 'PATCH') {
        const patch = request.postDataJSON()
        events.push({ type: 'patch', patch })
        Object.assign(publication, patch, { updated_at: new Date().toISOString() })
        return route.fulfill({ json: publication })
      }
      if (pathname === '/api/publish/pub-a/upload' && request.method() === 'POST') {
        events.push({
          type: 'upload',
          title: publication.title,
          privacy: publication.privacy_status,
        })
        Object.assign(publication, {
          status: 'published',
          youtube_video_id: 'test-video',
          youtube_url: 'https://youtu.be/test-video',
          uploaded_at: new Date().toISOString(),
          ready_to_upload: false,
        })
        return route.fulfill({ status: 202, json: publication })
      }
      if (pathname === '/api/publish/pub-a') return route.fulfill({ json: publication })
      if (pathname === '/api/exports/exp-a/download') return route.fulfill({ status: 404 })
      return route.fulfill({ status: 404, json: { detail: 'missing fixture route' } })
    })

    const detailUrl = server.resolvedUrls.local[0] + 'publish/pub-a'
    const publishButton = () => page.getByRole('button', { name: 'Publish to YouTube' })
    const titleInput = () => page.locator('input.field').first()

    // Scenario A: the saved publication is public. Publish must confirm the
    // saved value; dismissing the dialog must not upload, accepting must.
    await page.goto(detailUrl)
    await titleInput().waitFor()
    assert.equal(await page.locator('select').inputValue(), 'public')

    nextDialogAction = 'dismiss'
    await publishButton().click()
    await eventually(() => assert.equal(dialogs.length, 1))
    assert.match(dialogs[0], /public/i)
    assert.equal(events.filter((event) => event.type === 'upload').length, 0)

    nextDialogAction = 'accept'
    await publishButton().click()
    await page.getByText('Published to YouTube.').waitFor()
    await eventually(() => assert.equal(events.filter((event) => event.type === 'upload').length, 1))
    assert.equal(events.at(-1).privacy, 'public')

    // Scenario B: saved public, local select private plus an edited title.
    // The page must PATCH first, then upload with the saved private value,
    // without a confirmation dialog.
    publication = fixture()
    events.length = 0
    dialogs.length = 0
    nextDialogAction = 'dismiss'
    await page.reload()
    await titleInput().waitFor()
    await page.locator('select').selectOption('private')
    await titleInput().fill('Niezapisany tytuł')
    assert.equal(await page.locator('select').inputValue(), 'private')

    await publishButton().click()
    await page.getByText('Published to YouTube.').waitFor()

    await eventually(() => assert.equal(events.length, 2))
    assert.equal(dialogs.length, 0, 'private upload must not require a confirmation')
    assert.deepEqual(events[0].type, 'patch')
    assert.equal(events[0].patch.privacy_status, 'private')
    assert.equal(events[0].patch.title, 'Niezapisany tytuł')
    assert.equal(events[1].type, 'upload')
    assert.equal(events[1].privacy, 'private')
    assert.equal(events[1].title, 'Niezapisany tytuł')
    assert.deepEqual(errors, [])

    console.log(JSON.stringify({ passed: true, dialogs, events, errors }, null, 2))
  } finally {
    if (browser) await browser.close()
    await server.close()
  }
}

main().catch((error) => {
  console.error(error)
  process.exit(1)
})
