// Requires a disposable, worker-disabled backend seeded with the M2 synthetic media fixtures.
const assert = require('node:assert/strict')
const { firefox } = require(process.env.AUTOCLIP_PLAYWRIGHT_MODULE || 'playwright')
const baseURL = process.env.AUTOCLIP_TEST_URL || 'http://127.0.0.1:19879'
const screenshot = process.env.AUTOCLIP_SCREENSHOT || '/tmp/autoclip-m2-browser.png'

async function eventually(check) {
  const end = Date.now() + 10000
  while (true) {
    try { await check(); return } catch (error) {
      if (Date.now() >= end) throw error
      await new Promise((resolve) => setTimeout(resolve, 50))
    }
  }
}

async function main() {
  const browser = await firefox.launch({ headless: true })
  try {
    const context = await browser.newContext({ baseURL, viewport: { width: 1600, height: 1200 } })
    const page = await context.newPage()
    page.setDefaultTimeout(10000)
    const errors = []
    const unexpected = []
    page.on('pageerror', (error) => errors.push(error.message))
    await page.route('**/api/providers/status', (route) => route.fulfill({ json: [
      { name: 'ollama', available: true, requires_key: false, has_key: false },
    ] }))
    await page.route('**/api/system', (route) => route.fulfill({ json: { accel: 'cpu' } }))
    for (const [id, text, start, end] of [
      ['m2-clip-a', 'Sprawdzenie', 0, 2.9], ['m2-clip-b', 'Drugi', 3, 5.9],
    ]) {
      const response = await context.request.patch(`/api/clips/${id}/edit`, { data: {
        words: [{ text, start, end }], burn_captions: true, framing: null,
        caption_style: 'clean_lower', ratio: '9:16',
      } })
      assert.equal(response.status(), 200)
    }
    let editMode = 'real'
    let releaseEdit
    await page.route('**/api/clips/*/edit', async (route) => {
      if (route.request().method() !== 'PATCH') return route.continue()
      if (editMode === 'fail') {
        return route.fulfill({ status: 500, json: { detail: 'Edit rejected for test' } })
      }
      if (editMode === 'hold') await new Promise((resolve) => { releaseEdit = resolve })
      return route.continue()
    })
    const exports = []
    const pendingExports = []
    await page.route('**/api/clips/*/export', async (route) => {
      exports.push(route.request().url().split('/').at(-2))
      await new Promise((resolve) => pendingExports.push({ route, resolve }))
    })
    await page.route('**/api/clips/*', (route) => {
      if (route.request().method() === 'PATCH') {
        unexpected.push(route.request().url())
        return route.fulfill({ status: 500, json: { detail: 'Unexpected trim/status request' } })
      }
      return route.continue()
    })
    const finishExport = async (status = 200) => {
      await eventually(() => assert.ok(pendingExports.length))
      const pending = pendingExports.shift()
      await pending.route.fulfill({ status, json: status === 200 ? {} : { detail: 'Export rejected for test' } })
      pending.resolve()
    }
    await page.goto('/jobs/m2-job/clips')
    const subtitles = page.getByLabel('Subtitles', { exact: true })
    const trim = page.getByRole('button', { name: 'Drag start of clip', exact: true })
    const word = (text) => page.getByRole('button', { name: text, exact: true })
    const clipB = page.getByRole('button', { name: /Clip B/ }).first()
    const fg = page.locator('video:not([aria-hidden])')
    const bg = page.locator('video[aria-hidden="true"]')
    const overlay = page.locator('div[style*="bottom:"]')
    await word('Sprawdzenie').waitFor()

    await subtitles.click()
    await eventually(async () => assert.equal(await subtitles.isChecked(), false))
    await page.getByRole('button', { name: 'Fit + blurred background', exact: true }).click()
    await bg.waitFor()
    await page.reload()
    await bg.waitFor()
    assert.equal(await subtitles.isChecked(), false)
    assert.equal(await overlay.count(), 0)
    assert.equal(await fg.evaluate((video) => getComputedStyle(video).objectFit), 'contain')
    assert.equal(await bg.evaluate((video) => video.muted), true)
    await eventually(async () => assert.ok(await fg.evaluate((video) => video.readyState >= 2)))
    await fg.evaluate((video) => { video.currentTime = 0.6; video.playbackRate = 1.25 })
    await eventually(async () => assert.ok(await page.evaluate(() => {
      const [background, foreground] = document.querySelectorAll('video')
      return Math.abs(background.currentTime - foreground.currentTime) < 0.12 && background.playbackRate === 1.25
    })))
    await page.getByRole('button', { name: 'Play', exact: true }).last().click()
    await eventually(async () => assert.equal(await bg.evaluate((video) => video.paused), false))
    await page.getByRole('button', { name: 'Pause', exact: true }).last().click()
    await eventually(async () => assert.equal(await bg.evaluate((video) => video.paused), true))
    await subtitles.click()
    await overlay.waitFor()
    assert.ok(await overlay.evaluate((element) => Number(getComputedStyle(element).zIndex)) >
      await fg.evaluate((element) => Number(getComputedStyle(element).zIndex)))

    await clipB.click()
    await word('Drugi').waitFor()
    assert.equal(await subtitles.isChecked(), true)
    assert.equal(await bg.count(), 0)
    await page.getByRole('button', { name: /Clip A/ }).first().click()
    await word('Sprawdzenie').waitFor()
    await bg.waitFor()
    editMode = 'fail'
    await subtitles.click()
    await page.getByText('Edit rejected for test', { exact: true }).waitFor()
    assert.equal(await subtitles.isChecked(), true)
    await eventually(async () => assert.equal(await subtitles.isEnabled(), true))
    editMode = 'real'

    await word('Sprawdzenie').click()
    // React autofocus sets focus without retaining an autofocus attribute.
    const editorInput = page.locator('input[type="text"], input:not([type])')
    await editorInput.fill('Zapisane')
    await editorInput.press('Enter')
    assert.equal(await trim.isDisabled(), true)
    assert.equal(await page.getByRole('button', { name: 'Export kept', exact: true }).isDisabled(), true)
    await page.getByText('Save caption changes before trimming or exporting.', { exact: true }).waitFor()
    const trimPosition = await trim.getAttribute('style')
    for (const name of ['Export kept', 'Export this clip']) {
      await page.getByRole('button', { name, exact: true }).evaluate((button) => button.click())
    }
    await trim.evaluate((button) => {
      button.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, clientX: 500 }))
      window.dispatchEvent(new PointerEvent('pointermove', { clientX: 600 }))
      window.dispatchEvent(new PointerEvent('pointerup', { clientX: 600 }))
    })
    assert.equal(await trim.getAttribute('style'), trimPosition)
    assert.equal((await page.request.get('/api/health')).ok(), true)
    assert.equal(exports.length, 0)
    assert.deepEqual(unexpected, [])
    editMode = 'hold'
    await page.getByRole('button', { name: 'Save edits', exact: true }).click()
    await eventually(() => assert.equal(typeof releaseEdit, 'function'))
    assert.equal(await word('Zapisane').isDisabled(), true)
    assert.equal(await trim.isDisabled(), true)
    assert.equal(await subtitles.isDisabled(), true)
    releaseEdit()
    await eventually(async () => assert.equal(await word('Zapisane').isEnabled(), true))
    await page.reload()
    await word('Zapisane').waitFor()
    editMode = 'real'
    assert.equal(await trim.isEnabled(), true)
    assert.equal(exports.length, 0)

    await page.getByRole('button', { name: 'Export kept', exact: true }).click()
    await eventually(() => assert.deepEqual(exports, ['m2-clip-a']))
    const assertLocked = async () => {
      assert.equal(await word('Zapisane').isDisabled(), true)
      assert.equal(await trim.isDisabled(), true)
      assert.equal(await clipB.getAttribute('aria-disabled'), 'true')
      assert.equal(await subtitles.isDisabled(), true)
      await clipB.evaluate((row) => row.click())
      assert.equal(await word('Zapisane').count(), 1)
    }
    await assertLocked()
    await finishExport()
    await eventually(() => assert.deepEqual(exports, ['m2-clip-a', 'm2-clip-b']))
    await assertLocked()
    await finishExport(500)
    await page.getByText('Export rejected for test', { exact: true }).waitFor()
    await eventually(async () => assert.equal(await word('Zapisane').isEnabled(), true))
    await page.getByRole('button', { name: 'Export this clip', exact: true }).click()
    await eventually(() => assert.equal(exports.length, 3))
    await assertLocked()
    await finishExport(500)
    await eventually(async () => assert.equal(await trim.isEnabled(), true))
    assert.deepEqual(exports, ['m2-clip-a', 'm2-clip-b', 'm2-clip-a'])
    assert.deepEqual(unexpected, [])
    assert.deepEqual(errors, [])
    await page.screenshot({ path: screenshot, fullPage: true })
    console.log(JSON.stringify({ passed: true, mockedExports: exports, pageErrors: errors.length, screenshot }))
  } finally {
    await browser.close()
  }
}

main().catch((error) => { console.error(error); process.exitCode = 1 })
