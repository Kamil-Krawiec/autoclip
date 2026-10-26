// No server or paid API: exercise the real TypeScript client with controlled XHR events.
// Run: node tests/upload_client.cjs
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const ts = require('../frontend/node_modules/typescript')

class MockXHR extends EventTarget {
  static requests = []
  constructor() {
    super()
    this.upload = new EventTarget()
    this.headers = {}
    MockXHR.requests.push(this)
  }
  open(method, url) { Object.assign(this, { method, url }) }
  setRequestHeader(key, value) { this.headers[key.toLowerCase()] = value }
  send(body) {
    if (MockXHR.failSend) throw new Error('Synthetic send failure')
    this.body = body
    this.sent = true
  }
  abort() { this.aborted = true; this.emit('abort') }
  emit(type, values = {}, target = this) {
    const event = Object.assign(new Event(type), values)
    target.dispatchEvent(event)
    target[`on${type}`]?.(event)
  }
  complete(status, body, statusText = '') {
    this.status = status
    this.statusText = statusText
    this.responseText = typeof body === 'string' ? body : JSON.stringify(body)
    this.response = this.responseType === 'json' ? JSON.parse(this.responseText) : this.responseText
    this.emit('load')
    this.emit('loadend')
  }
}

const moduleObject = { exports: {} }
vm.runInNewContext(ts.transpileModule(
  fs.readFileSync(path.join(__dirname, '../frontend/src/api.ts'), 'utf8'),
  { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } },
).outputText, {
  module: moduleObject, exports: moduleObject.exports,
  XMLHttpRequest: MockXHR, FormData, DOMException, Error,
  fetch: () => { throw new Error('upload must use XHR') },
})
const { api, ApiError } = moduleObject.exports
const file = new Blob(['fixture video bytes'], { type: 'video/mp4' })
const source = { id: 'uploaded-source', filename: 'fixture.mp4' }

function trackedAbort() {
  const controller = new AbortController()
  const listeners = new Set()
  const signal = controller.signal
  const add = signal.addEventListener.bind(signal)
  const remove = signal.removeEventListener.bind(signal)
  signal.addEventListener = (type, listener, options) => {
    if (type === 'abort') listeners.add(listener)
    return add(type, listener, options)
  }
  signal.removeEventListener = (type, listener, options) => {
    if (type === 'abort') listeners.delete(listener)
    return remove(type, listener, options)
  }
  return { controller, signal, listeners }
}

async function main() {
  const progress = []
  const tracked = trackedAbort()
  const promise = api.uploadSource(file, {
    signal: tracked.signal,
    onProgress: (event) => progress.push(event),
  })
  const xhr = MockXHR.requests.at(-1)
  assert.equal(xhr.method, 'POST')
  assert.equal(xhr.url, '/api/sources/upload')
  assert.ok(xhr.body instanceof FormData)
  assert.equal(await xhr.body.get('file').text(), await file.text())
  assert.equal(xhr.headers['content-type'], undefined, 'browser owns the multipart boundary')
  xhr.emit('progress', { loaded: 25, total: 100, lengthComputable: true }, xhr.upload)
  xhr.emit('progress', { loaded: 50, total: 0, lengthComputable: false }, xhr.upload)
  assert.equal(progress.length, 2)
  assert.equal(progress[0].loaded, 25)
  assert.equal(progress[0].total, 100)
  assert.equal(progress[0].lengthComputable, true)
  assert.equal(progress[1].loaded, 50)
  assert.equal(progress[1].lengthComputable, false)
  xhr.emit('load', {}, xhr.upload)
  assert.equal(progress.at(-1).uploadComplete, true)
  let resolved = false
  promise.then(() => { resolved = true })
  await Promise.resolve()
  assert.equal(resolved, false, 'upload completion still waits for server validation')
  xhr.complete(201, source)
  assert.equal((await promise).id, source.id)
  assert.equal(tracked.listeners.size, 0, 'release abort listener after success')
  xhr.emit('progress', { loaded: 100, total: 100, lengthComputable: true }, xhr.upload)
  assert.equal(progress.length, 3, 'release upload progress listener after success')

  const before = trackedAbort()
  before.controller.abort()
  const initialCount = MockXHR.requests.length
  await assert.rejects(api.uploadSource(file, { signal: before.signal }), { name: 'AbortError' })
  assert.ok(MockXHR.requests.slice(initialCount).every((request) => !request.sent))
  assert.equal(before.listeners.size, 0)

  const during = trackedAbort()
  const cancelled = api.uploadSource(file, { signal: during.signal })
  const abortedXHR = MockXHR.requests.at(-1)
  const rejected = assert.rejects(cancelled, { name: 'AbortError' })
  during.controller.abort()
  await rejected
  assert.equal(abortedXHR.aborted, true)
  assert.equal(during.listeners.size, 0, 'release abort listener after cancellation')
  abortedXHR.complete(201, source)
  await assert.rejects(cancelled, { name: 'AbortError' })

  for (const [status, body, statusText, message, hint] of [
    [422, { detail: { message: 'Invalid video', hint: 'Choose a playable file' } }, '', 'Invalid video', 'Choose a playable file'],
    [413, { detail: 'Too large' }, '', 'Too large', ''],
    [502, '<html>proxy error</html>', 'Bad Gateway', '502 Bad Gateway', ''],
  ]) {
    const failure = trackedAbort()
    const result = api.uploadSource(file, { signal: failure.signal })
    MockXHR.requests.at(-1).complete(status, body, statusText)
    await assert.rejects(result, (error) => {
      assert.ok(error instanceof ApiError)
      assert.equal(error.status, status)
      assert.equal(error.message, message)
      assert.equal(error.hint, hint)
      return true
    })
    assert.equal(failure.listeners.size, 0, 'release abort listener after HTTP failure')
  }

  const network = trackedAbort()
  const offline = api.uploadSource(file, { signal: network.signal })
  MockXHR.requests.at(-1).emit('error')
  await assert.rejects(offline, (error) => /network/i.test(error.message) && error.name !== 'AbortError')
  assert.equal(network.listeners.size, 0, 'release abort listener after network failure')

  const malformed = api.uploadSource(file)
  MockXHR.requests.at(-1).complete(200, 'not JSON')
  await assert.rejects(malformed)
  const synchronous = trackedAbort()
  MockXHR.failSend = true
  await assert.rejects(api.uploadSource(file, { signal: synchronous.signal }), /Synthetic send failure/)
  MockXHR.failSend = false
  assert.equal(synchronous.listeners.size, 0, 'release abort listener when send throws')
  console.log('PASS upload client: multipart, byte progress, cancellation, late completion, cleanup, HTTP hints, network, invalid JSON')
}

main().catch((error) => { console.error(error); process.exitCode = 1 })
