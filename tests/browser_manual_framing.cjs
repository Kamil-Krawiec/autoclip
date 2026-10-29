// Disposable worker-disabled backend: manual-job, manual-a/manual-b and synthetic video.
const assert = require('node:assert/strict')
const { chromium } = require(process.env.AUTOCLIP_PLAYWRIGHT_MODULE || 'playwright')
const baseURL = process.env.AUTOCLIP_TEST_URL || 'http://127.0.0.1:19879'
async function eventually(check) {
  const end = Date.now() + 10000
  for (;;) {
    try { await check(); return } catch (error) {
      if (Date.now() > end) throw error
      await new Promise(resolve => setTimeout(resolve, 40))
    }
  }
}
async function main() {
  const browser = await chromium.launch({ headless: true })
  try {
    const context = await browser.newContext({ baseURL, hasTouch: true, viewport: { width: 1600, height: 1200 } })
    for (const [id, text, start, end] of [['manual-a','Pierwszy',0,2.9],['manual-b','Drugi',3,5.9]]) {
      assert.equal((await context.request.patch(`/api/clips/${id}/edit`, { data: {
        framing: null, words: [{text,start,end}], caption_style: 'clean_lower', burn_captions: true, ratio: '9:16',
      } })).status(), 200)
    }
    const page = await context.newPage()
    const errors = [], patches = [], trims = [], exports = []
    let mode = 'real', releaseEdit, releaseExport
    page.on('pageerror', e => errors.push(e.message))
    await page.route('**/api/providers/status', r => r.fulfill({json: []}))
    await page.route('**/api/system', r => r.fulfill({json: {accel:'cpu'}}))
    await page.route('**/api/clips/*/edit', async r => {
      if (r.request().method() !== 'PATCH') return r.continue()
      patches.push(r.request().postDataJSON())
      if (mode === 'fail') return r.fulfill({status:500,json:{detail:'Manual save rejected'}})
      if (mode === 'hold') await new Promise(resolve => {releaseEdit=resolve})
      return r.continue()
    })
    await page.route('**/api/clips/*', r => {
      if (r.request().method() === 'PATCH') trims.push(r.request().postDataJSON())
      return r.continue()
    })
    await page.route('**/api/clips/*/export', async r => {
      exports.push(r.request().url())
      await new Promise(resolve => {releaseExport=resolve})
      return r.fulfill({status:500,json:{detail:'Mock export complete'}})
    })
    const button = name => page.getByRole('button', {name, exact:true})
    const range = name => page.getByRole('slider', {name, exact:true})
    const getClip = async id => (await context.request.get(`/api/clips/${id}`)).json()
    await page.goto('/jobs/manual-job/clips/manual-a')
    await button('Transcript').click()
    await button('Pierwszy').waitFor()
    await page.locator('summary', { hasText: 'Clips · 2' }).click()
    await button('Slice Manual').click()
    await button('Framing').click()
    await page.getByText('Fine position', {exact:true}).click()
    await range('Zoom').waitFor()
    await eventually(async()=>assert.equal(await range('Zoom').isEnabled(),true))
    assert.deepEqual((await getClip('manual-a')).framing,{mode:'manual',center_x:0.5,center_y:0.5,zoom:1})
    const stage = page.getByTestId('clip-preview')
    await stage.scrollIntoViewIfNeeded()
    let box = await stage.boundingBox()
    let count = patches.length
    await page.mouse.move(box.x+box.width*.6,box.y+box.height*.5)
    await page.mouse.down()
    await page.mouse.move(box.x+box.width*.3,box.y+box.height*.5,{steps:5})
    assert.equal(patches.length,count)
    await page.mouse.up()
    await button('Apply framing').click()
    await eventually(()=>assert.equal(patches.length,count+1))
    await eventually(async()=>assert.equal(await range('Zoom').isEnabled(),true))
    assert.ok((await getClip('manual-a')).framing.center_x>.5)

    await range('Zoom').focus(); await range('Zoom').press('ArrowRight')
    await button('Discard framing changes').waitFor()
    await eventually(async()=>assert.equal(await button('Apply framing').isEnabled(),true))
    count=patches.length
    await page.getByRole('button',{name:/Clip B/}).first().click()
    await button('Transcript').click()
    await button('Pierwszy').waitFor()
    await button('Framing').click()
    await page.getByText('Whole clip framing', {exact:true}).click()
    for(const name of ['1:1','16:9','Auto','Fit + blurred background']) await button(name).click()
    for(const name of ['Export kept','Export this clip','Drag start of clip']) assert.equal(await button(name).isDisabled(),true)
    assert.equal(patches.length,count); assert.equal(exports.length,0); assert.equal(trims.length,0)
    assert.ok((await page.getByRole('alert').innerText()).includes('Apply or discard'))
    // Caption edits remain independently available while framing is a draft.
    assert.equal(await page.getByLabel('Subtitles',{exact:true}).isEnabled(),true)
    await button('Discard framing changes').click()
    await eventually(async()=>assert.equal(await button('Export this clip').isEnabled(),true))
    await range('Zoom').focus(); await range('Zoom').press('ArrowRight')
    await range('Center X').focus(); await range('Center X').press('ArrowLeft')
    await range('Center Y').focus(); await range('Center Y').press('ArrowDown')
    await button('Apply framing').click()
    await eventually(async()=>assert.equal(await range('Zoom').isEnabled(),true))
    const saved=await getClip('manual-a')
    await page.reload()
    await eventually(async()=>assert.equal(await range('Zoom').isEnabled(),true))
    await page.getByText('Fine position', {exact:true}).click()
    assert.equal(Number(await range('Zoom').inputValue()),saved.framing.zoom)
    const path=await (await context.request.get('/api/clips/manual-a/crop-path')).json()
    const segment=path.segments[0]
    const fg=page.locator('video').first()
    await eventually(async()=>assert.ok(Math.abs(parseFloat(await fg.evaluate(v=>v.style.left))-(-segment.keyframes[0].x/segment.width*100))<.01))
    mode='fail'
    await range('Zoom').focus(); await range('Zoom').press('ArrowRight')
    await button('Apply framing').click()
    await page.getByText('Manual save rejected',{exact:true}).waitFor()
    await eventually(async()=>assert.equal(Number(await range('Zoom').inputValue()),saved.framing.zoom))
    assert.equal(await button('Apply framing').isDisabled(),true)
    mode='hold'
    await range('Zoom').focus(); await range('Zoom').press('ArrowRight')
    await button('Apply framing').click()
    await eventually(()=>assert.equal(typeof releaseEdit,'function'))
    for(const name of ['Zoom','Center X','Center Y']) assert.equal(await range(name).isDisabled(),true)
    releaseEdit(); mode='real'
    await eventually(async()=>assert.equal(await range('Zoom').isEnabled(),true))
    // Native touch events exercise touch-action and pointer capture, including vertical movement.
    await stage.evaluate(el=>el.scrollIntoView({block:'center',behavior:'instant'}))
    await page.evaluate(()=>new Promise((resolve,reject)=>{
      let previous=scrollY, stable=0, frames=0
      const sample=()=>{
        const current=scrollY
        stable=current===previous?stable+1:0
        previous=current
        if(stable>=4) return resolve()
        if(++frames>120) return reject(new Error('Page scroll did not settle before touch drag'))
        requestAnimationFrame(sample)
      }
      requestAnimationFrame(sample)
    }))
    box=await stage.boundingBox()
    assert.equal(await stage.evaluate(el=>getComputedStyle(el).touchAction),'none')
    const cdp=await context.newCDPSession(page)
    count=patches.length
    const scroll=await page.evaluate(()=>scrollY)
    const assertNoTouchScroll=async()=>assert.equal(await page.evaluate(()=>scrollY),scroll)
    await cdp.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x:box.x+box.width*.5,y:box.y+box.height*.5}]})
    await assertNoTouchScroll()
    for(const frac of [.45,.35,.25]) {
      await cdp.send('Input.dispatchTouchEvent',{type:'touchMove',touchPoints:[{x:box.x+box.width*frac,y:box.y+box.height*.7}]})
      await assertNoTouchScroll()
    }
    assert.equal(patches.length,count)
    await cdp.send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]})
    await assertNoTouchScroll()
    await button('Apply framing').click()
    await eventually(()=>assert.equal(patches.length,count+1))
    await eventually(async()=>assert.equal(await range('Zoom').isEnabled(),true))
    assert.ok((await getClip('manual-a')).framing.center_x>saved.framing.center_x)
    await button('Export this clip').click()
    await eventually(()=>assert.equal(typeof releaseExport,'function'))
    for(const name of ['Zoom','Center X','Center Y']) assert.equal(await range(name).isDisabled(),true)
    releaseExport()
    await eventually(async()=>assert.equal(await range('Zoom').isEnabled(),true))
    const a=await getClip('manual-a'), b=await getClip('manual-b')
    assert.equal(a.caption_style,'clean_lower'); assert.equal(a.burn_captions,true)
    assert.deepEqual(await (await context.request.get('/api/clips/manual-a/words')).json(),[{text:'Pierwszy',start:0,end:2.9,speaker:null}])
    assert.equal(b.framing,null)
    await page.locator('summary', { hasText: 'Clips · 2' }).click()
    await page.getByRole('button',{name:/Clip B/}).first().click(); await button('Transcript').click(); await button('Drugi').waitFor()
    assert.equal(await range('Zoom').count(),0)
    await page.getByRole('button',{name:/Clip A/}).first().click()
    await button('Framing').click()
    await eventually(async()=>assert.equal(await range('Zoom').isEnabled(),true))
    assert.deepEqual(errors,[])
    await page.screenshot({path:'/tmp/autoclip-manual-browser.png',fullPage:true})
    console.log(JSON.stringify({passed:true,editPatches:patches.length,touchDrag:true,pageErrors:errors.length,screenshot:'/tmp/autoclip-manual-browser.png'}))
  } finally { await browser.close() }
}
main().catch(error=>{console.error(error);process.exitCode=1})
