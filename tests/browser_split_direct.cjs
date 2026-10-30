const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const { execFileSync } = require('node:child_process')
const os = require('node:os')
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

async function main() {
  const { createServer } = await import(pathToFileURL(require.resolve('../frontend/node_modules/vite/dist/node/index.js')))
  const server = await createServer({
    root: path.join(__dirname, '../frontend'),
    configFile: path.join(__dirname, '../frontend/vite.config.ts'),
    server: { host: '127.0.0.1', port: 0 },
  })
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'autoclip-split-ui-'))
  const mediaPath = path.join(temp, 'source.mp4')
  execFileSync('ffmpeg', ['-v','error','-y','-f','lavfi','-i','color=c=red:s=640x360:r=5:d=20,drawbox=x=320:y=0:w=320:h=360:color=blue:t=fill,drawbox=x=320:y=180:w=320:h=180:color=lime:t=fill','-c:v','libx264','-pix_fmt','yuv420p',mediaPath])
  process.env.AUTOCLIP_TEST_VIDEO = mediaPath
  let browser
  try {
    await server.listen()
    browser = await chromium.launch({ headless: true })
    const page = await browser.newPage({ viewport: process.env.AUTOCLIP_DESKTOP ? { width: 1600, height: 1000 } : { width: 375, height: 900 } })
    page.setDefaultTimeout(10000)
    const errors = [], edits = []
    let failEdit = false

    page.on('pageerror', error => errors.push(error.message))

    const clips = [
      {
        id: 'clip-1', job_id: 'job-1', rank: 1, start_s: 0, end_s: 10, duration_s: 10,
        start_word: 0, end_word: 2, title: 'Clip 1', hook: '', score: 95,
        reason: 'Main', status: 'kept', user_trimmed: false, caption_style: 'bold_pop',
        ratio: '9:16', burn_captions: false, framing: {mode:'split'}, caption_overrides: null, exports: [],
      },
      {
        id: 'clip-2', job_id: 'job-1', rank: 2, start_s: 10, end_s: 20, duration_s: 10,
        start_word: 3, end_word: 5, title: 'Clip 2', hook: '', score: 85,
        reason: 'Second', status: 'candidate', user_trimmed: false, caption_style: 'bold_pop',
        ratio: '9:16', burn_captions: false, framing: {mode:'split'}, caption_overrides: null, exports: [],
      },
    ]

    const job = {
      id: 'job-1', source_id: 'src-1', status: 'done', current_stage: 'done', progress: 1,
      error: null, provider: 'fixture', created_at: '', updated_at: '', started_at: null, finished_at: null,
      source: { id: 'src-1', type: 'upload', title: 'Fixture', url: null, filename: 'fixture.mp4',
        duration_s: 30, width: 640, height: 360, fps: 30, has_audio: false, has_video: true, created_at: '' },
    }

    await page.route('**/api/**', async route => {
      const req = route.request(), url = new URL(req.url()), p = url.pathname
      if (p === '/api/jobs/job-1') return route.fulfill({ json: job })
      if (p === '/api/jobs/job-1/clips') return route.fulfill({ json: clips })
      if (p === '/api/caption-styles') return route.fulfill({ json: [] })
      if (p.endsWith('/words')) return route.fulfill({ json: [{ text: 'słowo', start: 0, end: 10, speaker: null }] })
      if (p.endsWith('/crop-path')) {
        const clip = clips.find(c => c.id === p.split('/')[3])
        const leaves = clip.framing?.mode === 'slices' ? clip.framing.segments : [{start_s:clip.start_s,end_s:clip.end_s,framing:clip.framing}]
        return route.fulfill({json:{source_width:640,source_height:360,segments:leaves.map(s=>({start_s:s.start_s-clip.start_s,end_s:s.end_s-clip.start_s,width:640,height:360,keyframes:[{t:0,x:0,y:0}],strategy:'general',zoom:0,fit:s.framing?.mode==='fit',split:s.framing?.mode==='split',split_panels:s.framing?.panels ?? null}))}})
      }
      if (p.endsWith('/edit') && req.method() === 'PATCH') {
        const id = p.split('/')[3], patch = req.postDataJSON()
        if (failEdit) { failEdit = false; return route.fulfill({status:500,json:{detail:"Synthetic save failure"}}) }
        edits.push({ id, patch })
        const clip = clips.find(c => c.id === id)
        Object.assign(clip, patch)
        return route.fulfill({ json: clip })
      }
      if (p.endsWith('/media')) {
        if (process.env.AUTOCLIP_TEST_VIDEO) {
          const bytes = fs.readFileSync(process.env.AUTOCLIP_TEST_VIDEO)
          const match = /^bytes=(\d+)-(\d*)$/.exec(req.headers().range || '')
          if (match) {
            const start = Number(match[1]), end = match[2] ? Math.min(Number(match[2]), bytes.length - 1) : bytes.length - 1
            return route.fulfill({ status: 206, contentType: 'video/mp4', headers: { 'Content-Range': `bytes ${start}-${end}/${bytes.length}`, 'Accept-Ranges': 'bytes' }, body: bytes.subarray(start, end + 1) })
          }
          return route.fulfill({ body: bytes, contentType: 'video/mp4', headers: { 'Accept-Ranges': 'bytes' } })
        }
        return route.fulfill({ status: 404, body: '' })
      }
      if (p === '/api/providers/status') return route.fulfill({ json: [] })
      if (p === '/api/system') return route.fulfill({ json: { accel: 'cpu' } })
      return route.fulfill({ status: 404, json: { detail: 'missing route' } })
    })


    await page.goto(server.resolvedUrls.local[0] + 'jobs/job-1/clips/clip-1')
    const button = name => page.getByRole('button', { name, exact: true })
    const stage = page.getByTestId('clip-preview')
    await button('Edit split frames').waitFor()
    assert.equal(edits.length,0)
    await page.waitForFunction(()=>document.querySelectorAll('[data-testid="clip-preview"] video').length===2)
    await button('Edit split frames').click()
    await page.getByTestId('split-frame-0').waitFor()
    await page.waitForFunction(()=>document.querySelectorAll('[data-testid="clip-preview"] video').length===1)
    assert.equal(await stage.evaluate(el=>el.clientWidth>el.clientHeight),true)
    const top = page.getByTestId('split-frame-0'), bottom = page.getByTestId('split-frame-1')
    const resizeTop=button('Resize top split frame')
    await resizeTop.focus()
    for(let i=0;i<10;i++) await resizeTop.press('ArrowLeft')
    for(let i=0;i<30;i++) await resizeTop.press('Shift+ArrowUp')
    await top.focus()
    for(let i=0;i<25;i++) await top.press('Shift+ArrowDown')
    for(let i=0;i<28;i++) await top.press('Shift+ArrowRight')
    await button('Select bottom frame').click()
    await bottom.focus()
    for(let i=0;i<26;i++) await bottom.press('Shift+ArrowLeft')
    assert.equal(edits.length,0)
    assert.equal(await button('Slice Fit + blur').isDisabled(),true)
    await button('Apply split framing').click()
    await eventually(()=>assert.equal(edits.length,1))
    const saved = structuredClone(clips[0].framing)
    assert.equal(saved.mode,'split')
    assert.ok(saved.panels[0].x>0.5)
    assert.ok(saved.panels[1].x<0.01)
    await page.waitForFunction(()=>document.querySelectorAll('[data-testid="clip-preview"] video').length===2)
    // Actual CSS output has the selected source panel on the correct output half.
    await page.waitForFunction(()=>[...document.querySelectorAll('[data-testid="clip-preview"] video')].every(v=>v.readyState>=2))
    const shot = await stage.screenshot()
    const rgb=execFileSync('ffmpeg',['-v','error','-i','pipe:0','-vf','scale=108:192','-f','rawvideo','-pix_fmt','rgb24','-'],{input:shot})
    const pixel=(x,y)=>rgb.subarray((y*108+x)*3,(y*108+x)*3+3)
    assert.ok(pixel(54,48)[1]>180 && pixel(54,48)[2]<30, `top should be green from lower right: ${pixel(54,48)}`)
    assert.ok(pixel(54,144)[0]>180, `bottom should be red: ${pixel(54,144)}`)
    await page.reload(); await button('Edit split frames').click()
    await eventually(()=>assert.equal(clips[0].framing.panels[0].x,saved.panels[0].x))
    // Pointer drag remains local and Discard restores the acknowledged geometry.
    await button('Select top frame').click()
    const box=await top.boundingBox()
    await page.mouse.move(box.x+box.width/2,box.y+box.height/2); await page.mouse.down(); await page.mouse.move(box.x+box.width/2-20,box.y+box.height/2,{steps:4}); await page.mouse.up()
    assert.equal(edits.length,1)
    await button('Discard split changes').click()
    assert.deepEqual(clips[0].framing,saved)
    // Pointer resize also remains a draft; cancellation releases the active gesture.
    await button('Edit split frames').click()
    // Hover waits for the source overlay to settle and scrolls the actual handle into view.
    await resizeTop.hover()
    const handleBox=await resizeTop.boundingBox(), widthBefore=await top.evaluate(el=>parseFloat(el.style.width))
    const handleCenter={x:handleBox.x+handleBox.width/2,y:handleBox.y+handleBox.height/2}
    assert.equal(await resizeTop.evaluate((el,point)=>el===document.elementFromPoint(point.x,point.y),handleCenter),true)
    await page.mouse.down()
    await page.mouse.move(handleCenter.x-20,handleCenter.y-10,{steps:3})
    await page.mouse.up()
    // Pointer transforms reach the controlled draft through a React effect.
    await page.waitForFunction(before=>parseFloat(document.querySelector('[data-testid="split-frame-0"]').style.width)<before,widthBefore)
    assert.equal(edits.length,1)
    await button('Discard split changes').click()
    // Save failure keeps the editable draft and retry available.
    await button('Edit split frames').click(); await top.focus(); await top.press('ArrowLeft')
    failEdit=true
    await button('Apply split framing').click()
    await page.getByText('Synthetic save failure',{exact:true}).waitFor()
    assert.equal(await top.count(),1)
    assert.equal(await button('Apply split framing').isEnabled(),true)
    await button('Discard split changes').click()
    // Existing custom panels survive global -> slices and splitting a slice.
    await button('Framing').click()
    await page.getByText('Whole clip framing',{exact:true}).click()
    await button('Per-slice framing').click()
    await eventually(()=>assert.equal(clips[0].framing.mode,'slices'))
    assert.deepEqual(clips[0].framing.segments[0].framing,saved)
    const video=stage.locator('video').last()
    await video.evaluate(el=>{el.currentTime=4;el.dispatchEvent(new Event('timeupdate'))})
    await page.getByRole('button',{name:/^Split slice at playhead/}).click()
    await eventually(()=>assert.equal(clips[0].framing.segments.length,2))
    assert.deepEqual(clips[0].framing.segments.map(s=>s.framing),[saved,saved])
    // Touch drag in source editor, followed by a per-slice save.
    await button('Edit split frames').click(); await button('Select top frame').click()
    const touchBox=await top.boundingBox(), cdp=await page.context().newCDPSession(page)
    const point={x:touchBox.x+touchBox.width/2,y:touchBox.y+touchBox.height/2}
    const beforeTouch=await top.evaluate(el=>parseFloat(el.style.left))
    await cdp.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[point]})
    await cdp.send('Input.dispatchTouchEvent',{type:'touchMove',touchPoints:[{x:point.x-20,y:point.y}]})
    await cdp.send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]})
    await page.waitForTimeout(50)
    assert.ok(await top.evaluate(el=>parseFloat(el.style.left))<beforeTouch)
    await button('Apply split framing').click()
    await eventually(()=>assert.notDeepEqual(clips[0].framing.segments[1].framing,saved))
    assert.deepEqual(clips[0].framing.segments[0].framing,saved)
    assert.deepEqual(clips[1].framing,{mode:'split'})
    await page.setViewportSize({width:375,height:900})
    await button('Edit split frames').click()
    await page.waitForFunction(()=>document.querySelectorAll('[data-testid="clip-preview"] video').length===1)
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false)
    await button('Discard split changes').click()
    assert.deepEqual(errors,[])
    console.log(JSON.stringify({passed:true,edits:edits.length,legacy:true,keyboard:true,pointer:true,touch:true,cssColors:true,persistence:true,perSlice:true,mobile:true}))
  } finally { await browser?.close(); await server.close(); fs.rmSync(temp,{recursive:true,force:true}) }
}
main().catch(error=>{console.error(error);process.exitCode=1})
