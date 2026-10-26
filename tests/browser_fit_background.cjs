// Run against the frontend Vite dev server; uses synthetic media and the real ClipPlayer.
const assert = require('node:assert/strict')
const { execFileSync } = require('node:child_process')
const { mkdtempSync, readFileSync, rmSync } = require('node:fs')
const { tmpdir } = require('node:os')
const { join } = require('node:path')
const { chromium } = require(process.env.AUTOCLIP_PLAYWRIGHT_MODULE || 'playwright')

async function main() {
  const temporary = mkdtempSync(join(tmpdir(), 'autoclip-fit-'))
  let browser
  try {
    const media = join(temporary, 'colors.webm')
    execFileSync('ffmpeg', ['-hide_banner', '-loglevel', 'error',
      ...['red', 'green', 'blue'].flatMap((color) => [
        '-f', 'lavfi', '-i', `color=${color}:s=640x360:r=15:d=2`,
      ]), '-filter_complex', '[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]',
      '-map', '[v]', '-c:v', 'libvpx', '-threads', '1', media])
    const data = readFileSync(media)
    browser = await chromium.launch({ headless: true,
      ...(process.env.AUTOCLIP_CHROMIUM_EXECUTABLE ? {
        executablePath: process.env.AUTOCLIP_CHROMIUM_EXECUTABLE,
      } : {}),
    })
    const page = await browser.newPage({ viewport: { width: 900, height: 1000 } })
    const errors = []
    page.on('pageerror', (error) => { errors.push(error.message); console.error(error.message) })
    await page.route('**/src/main.tsx*', (route) => route.fulfill({
      contentType: 'application/javascript',
      body: `import React from '/node_modules/.vite/deps/react.js';
        import ReactDOM from '/node_modules/.vite/deps/react-dom_client.js';
        import {ClipPlayer} from '/src/components/ClipPlayer.tsx';
        import '/src/index.css';
        ReactDOM.createRoot(document.getElementById('root')).render(
          React.createElement(ClipPlayer, {src:'/fit-colors.webm', startS:2.2,
            endS:5.9, words:[], ratio:'9:16', burnCaptions:false,
            cropPath:{source_width:640,source_height:360,segments:[{
              start_s:0,end_s:3.7,fit:true,width:640,height:360,keyframes:[]}]}}));`,
    }))
    await page.route('**/fit-colors.webm', (route) => {
      const range = route.request().headers().range
      const start = range ? Number(range.match(/bytes=(\d+)-/)[1]) : 0
      return route.fulfill({ status: range ? 206 : 200, headers: {
        'Content-Type': 'video/webm', 'Accept-Ranges': 'bytes',
        ...(range ? { 'Content-Range': `bytes ${start}-${data.length - 1}/${data.length}` } : {}),
      }, body: data.subarray(start) })
    })
    await page.goto(process.env.AUTOCLIP_TEST_URL || 'http://127.0.0.1:19881')
    const foreground = page.locator('video:not([aria-hidden])')
    await foreground.waitFor()
    await page.waitForFunction(() => document.querySelector('video:not([aria-hidden])')?.readyState >= 2)

    async function checkPixels(channel) {
      const rect = await foreground.boundingBox()
      const shot = await page.screenshot()
      const samples = await page.evaluate(async ({ base64, rect }) => {
        const image = new Image()
        image.src = 'data:image/png;base64,' + base64
        await image.decode()
        const canvas = document.createElement('canvas')
        canvas.width = image.width
        canvas.height = image.height
        const ctx = canvas.getContext('2d')
        ctx.drawImage(image, 0, 0)
        return [0.1, 0.4, 0.9].map((fraction) => Array.from(ctx.getImageData(
          Math.round(rect.x + rect.width * 0.25),
          Math.round(rect.y + rect.height * fraction), 1, 1).data).slice(0, 3))
      }, { base64: shot.toString('base64'), rect })
      for (const sample of samples) {
        assert.ok(sample[channel] > 110, `Expected visible current-frame color: ${samples}`)
        assert.ok(sample.every((value, index) => index === channel || value < 10), `${samples}`)
        assert.ok(Math.abs(sample[channel] - samples[1][channel]) < 8,
          `Background artificially darkened or stale: ${samples}`)
      }
      console.log(JSON.stringify({ time: await foreground.evaluate((video) => video.currentTime), samples }))
    }
    await page.waitForTimeout(250)
    assert.ok(Math.abs(await foreground.evaluate((video) => video.currentTime) - 2.2) < 0.05)
    await checkPixels(1)
    for (const [time, channel] of [[2.2, 1], [4.4, 2], [0.7, 0], [3.1, 1]]) {
      await foreground.evaluate((video, time) => { video.currentTime = time }, time)
      await page.waitForTimeout(250)
      await checkPixels(channel)
    }
    await foreground.evaluate((video) => { video.currentTime = 3.8 })
    await page.waitForTimeout(200)
    await page.getByRole('button', { name: 'Play', exact: true }).last().click()
    await page.waitForFunction(() => document.querySelector('video:not([aria-hidden])').currentTime > 4.3)
    await page.getByRole('button', { name: 'Pause', exact: true }).last().click()
    await page.waitForTimeout(150)
    await checkPixels(2)
    assert.deepEqual(errors, [])
  } finally {
    await browser?.close()
    rmSync(temporary, { recursive: true, force: true })
  }
}
main().catch((error) => { console.error(error); process.exitCode = 1 })
