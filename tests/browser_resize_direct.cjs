// Disposable worker-disabled backend: manual-job and synthetic video fixture.
const assert = require('node:assert/strict')
const { chromium } = require(process.env.AUTOCLIP_PLAYWRIGHT_MODULE || 'playwright')
const baseURL = process.env.AUTOCLIP_TEST_URL || 'http://127.0.0.1:19879'
async function eventually(check) {
  const end = Date.now() + 10000
  for (;;) { try { await check(); return } catch (error) { if (Date.now() > end) throw error; await new Promise(r => setTimeout(r, 40)) } }
}
async function main() {
  const browser = await chromium.launch({headless:true})
  try {
    const context = await browser.newContext({baseURL, viewport:{width:1500,height:1100}})
    await context.request.patch('/api/clips/manual-a/edit', {data:{framing:null,words:[{text:'Pierwszy',start:0,end:2.9}],caption_style:'clean_lower',burn_captions:true,ratio:'9:16'}})
    const page=await context.newPage(), patches=[]
    await page.route('**/api/providers/status',r=>r.fulfill({json:[]}))
    await page.route('**/api/system',r=>r.fulfill({json:{accel:'cpu'}}))
    await page.route('**/api/clips/*/edit',async r=>{if(r.request().method()==='PATCH') patches.push(r.request().postDataJSON()); await r.continue()})
    const button=name=>page.getByRole('button',{name,exact:true})
    await page.goto('/jobs/manual-job/clips/manual-a')
    await button('Transcript').click(); await button('Pierwszy').waitFor()
    await page.locator('summary',{hasText:'Clips · 2'}).click()
    await button('Slice Manual').click(); await button('Framing').click()
    await eventually(async()=>assert.ok(await page.getByTestId('crop-frame').count()))
    const stage=page.getByTestId('clip-preview'), frame=page.getByTestId('crop-frame'), handle=page.getByRole('button',{name:'Resize crop frame'})
    assert.equal(await stage.locator('video').count(),1)
    assert.equal(await stage.evaluate(el=>{const [w,h]=el.style.aspectRatio.split('/').map(Number);return w>h}),true)
    for(const name of ['Center X','Center Y','Zoom']) assert.equal(await page.getByRole('slider',{name,exact:true}).count(),0)
    const frameBox=await frame.boundingBox(), stageBox=await stage.boundingBox()
    const initialPatchCount=patches.length
    const initialFrame=await frame.evaluate(el=>({left:parseFloat(el.style.left),width:parseFloat(el.style.width)}))
    const panDirection=frameBox.x+frameBox.width < stageBox.x+stageBox.width-8 ? 1 : -1
    await page.mouse.move(frameBox.x+frameBox.width/2,frameBox.y+frameBox.height/2)
    await page.mouse.down(); await page.mouse.move(frameBox.x+frameBox.width/2+20*panDirection,frameBox.y+frameBox.height/2,{steps:3}); await page.mouse.up()
    const movedFrame=await frame.evaluate(el=>({left:parseFloat(el.style.left),width:parseFloat(el.style.width)}))
    assert.ok((movedFrame.left-initialFrame.left)*panDirection>0)
    assert.equal(patches.length,initialPatchCount,JSON.stringify(patches))
    await button('Apply framing').click()
    await eventually(()=>assert.equal(patches.length,initialPatchCount+1))
    let saved=patches.at(-1).framing
    assert.equal(saved.mode,'manual')
    assert.ok(Math.abs(saved.center_x-(movedFrame.left+movedFrame.width/2)/100)<0.002)
    await eventually(async()=>assert.ok(await handle.isEnabled()))
    const resized=await handle.boundingBox()
    const resizeDirection=saved.zoom<3.99 ? -1 : 1
    await page.mouse.move(resized.x+resized.width/2,resized.y+resized.height/2); await page.mouse.down(); await page.mouse.move(resized.x+resized.width/2+24*resizeDirection,resized.y+resized.height/2+16*resizeDirection,{steps:3}); await page.mouse.up()
    await button('Apply framing').click(); await eventually(()=>assert.equal(patches.length,initialPatchCount+2))
    const resizedSaved=patches.at(-1).framing
    assert.ok(resizeDirection<0 ? resizedSaved.zoom>saved.zoom : resizedSaved.zoom<saved.zoom)
    await eventually(async()=>assert.ok(await handle.isEnabled()))
    const keyboardDirection=saved.center_x<0.9 ? 'ArrowRight' : 'ArrowLeft'
    await frame.focus(); await frame.press(keyboardDirection); await button('Apply framing').click(); await eventually(()=>assert.equal(patches.length,initialPatchCount+3))
    const keyboardSaved=patches.at(-1).framing
    assert.equal(keyboardSaved.mode,'manual')
    assert.ok(keyboardDirection==='ArrowRight' ? keyboardSaved.center_x>resizedSaved.center_x : keyboardSaved.center_x<resizedSaved.center_x)
    assert.ok(keyboardSaved.center_x<1 && keyboardSaved.zoom>=1 && keyboardSaved.zoom<=4)
    const resizeKey = keyboardSaved.zoom < 3.99 ? 'ArrowLeft' : 'ArrowRight'
    await handle.focus(); await handle.press(resizeKey); await button('Apply framing').click()
    await eventually(()=>assert.equal(patches.length,initialPatchCount+4))
    const handleKeyboardSaved=patches.at(-1).framing
    assert.notEqual(handleKeyboardSaved.zoom,keyboardSaved.zoom)
    assert.ok(Math.abs(handleKeyboardSaved.center_x-keyboardSaved.center_x)<0.002)
    assert.ok(Math.abs(handleKeyboardSaved.center_y-keyboardSaved.center_y)<0.002)
    console.log(JSON.stringify({passed:true,patches:patches.length,singlePlayer:true,sourcePreview:true,keyboard:true}))
  } finally { await browser.close() }
}
main().catch(e=>{console.error(e);process.exitCode=1})
