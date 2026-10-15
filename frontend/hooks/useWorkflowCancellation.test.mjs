import test from 'node:test'
import assert from 'node:assert/strict'
import { createWorkflowCancellationController, WorkflowCancelledError } from './useWorkflowCancellation.mjs'

const deferred = () => {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
const json = (body, status = 200) => new Response(JSON.stringify(body), { status })
const created = (id = 'flow-a') => json({ flow_id: id, control_key: `key-${id}`, state: 'active', active_operations: 0 })
const stopping = count => json({ state: 'cancelling', active_operations: count })
const stopped = () => json({ state: 'cancelled', active_operations: 0 })
const settle = async () => { for (let i = 0; i < 40; i++) await Promise.resolve() }

function harness(fetcher) {
  let nextId = 0
  const timers = new Map()
  const calls = []
  const controller = createWorkflowCancellationController({
    apiBase: '',
    fetchImpl: (url, options) => {
      calls.push({ url, options })
      return fetcher(url, options)
    },
    schedule: (callback, ms) => {
      const id = ++nextId
      timers.set(id, { callback, ms })
      return id
    },
    unschedule: id => timers.delete(id),
    requestTimeoutMs: 50,
    pollIntervalMs: 10,
  })
  const tick = async ms => {
    for (const [id, timer] of [...timers]) {
      if (timer.ms === ms) {
        timers.delete(id)
        timer.callback()
      }
    }
    await settle()
  }
  return { controller, calls, timers, tick }
}

test('idle configuration exits locally without creating a flow', async () => {
  const { controller, calls, timers } = harness(() => { throw new Error('Unexpected request') })
  const result = controller.cancel()
  assert.equal(controller.getSnapshot().state, 'cancelling')
  assert.equal(controller.isCancelled(), true)
  await result
  assert.equal(controller.getSnapshot().state, 'cancelled')
  assert.equal(calls.length, 0)
  assert.equal(timers.size, 0)
  await assert.rejects(controller.request('/action'), WorkflowCancelledError)
})

test('concurrent first requests create exactly one flow and share its capability', async () => {
  const initialization = deferred()
  const { controller, calls } = harness(url => url.endsWith('/flows') ? initialization.promise : json({ ok: true }))
  const first = controller.request('/action-a', { headers: { 'X-Triggered-By': 'test' } })
  const second = controller.request('/action-b')
  assert.equal(calls.length, 1)
  initialization.resolve(created())
  await Promise.all([first, second])
  assert.equal(calls.length, 3)
  for (const call of calls.slice(1)) {
    assert.equal(call.options.headers.get('X-Workflow-Id'), 'flow-a')
    assert.equal(call.options.headers.get('X-Workflow-Key'), 'key-flow-a')
    assert.ok(!call.url.includes('key-flow-a'))
  }
  assert.equal(calls[1].options.headers.get('X-Triggered-By'), 'test')
})

test('cancellation during initialization waits for identity and never launches either action', async () => {
  const initialization = deferred()
  const { controller, calls } = harness(url => url.endsWith('/flows') ? initialization.promise : stopped())
  const first = assert.rejects(controller.request('/action-a'), WorkflowCancelledError)
  const second = assert.rejects(controller.request('/action-b'), WorkflowCancelledError)
  const cancellation = controller.cancel()
  assert.equal(controller.getSnapshot().state, 'cancelling')
  initialization.resolve(created())
  await Promise.all([first, second, cancellation])
  assert.deepEqual(calls.map(call => call.url), ['/v1/test-suites/flows', '/v1/test-suites/flows/flow-a/cancel'])
  assert.equal(calls[1].options.headers['X-Workflow-Key'], 'key-flow-a')
  assert.equal(controller.getSnapshot().state, 'cancelled')
})

test('repeated cancellation shares one attempt and cannot start new work', async () => {
  const acknowledgement = deferred()
  const { controller, calls } = harness(url =>
    url.endsWith('/flows') ? created() : url.endsWith('/cancel') ? acknowledgement.promise : json({}))
  await controller.request('/action')
  const first = controller.cancel()
  const second = controller.cancel()
  assert.equal(first, second)
  await assert.rejects(controller.request('/next-action'), WorkflowCancelledError)
  acknowledgement.resolve(stopped())
  await first
  await controller.cancel()
  assert.equal(calls.filter(call => call.url.endsWith('/cancel')).length, 1)
})

test('all response streams remain open until cancelled AND zero active operations', async () => {
  let statusCount = 0
  const { controller, calls, tick, timers } = harness(url => {
    if (url.endsWith('/flows')) return created()
    if (url.endsWith('/cancel')) return stopping(2)
    if (url.includes('/flows/')) return ++statusCount === 1 ? stopping(0) : stopped()
    return new Response('data: {}\n\n')
  })
  await Promise.all([controller.request('/action-a'), controller.request('/action-b')])
  let closed = 0
  controller.trackReader({ cancel: () => { closed++ } })
  controller.trackReader({ cancel: () => { closed++ } })
  const signals = calls.slice(1).map(call => call.options.signal)
  await controller.cancel()
  assert.equal(closed, 0)
  assert.ok(signals.every(signal => !signal.aborted))
  await tick(10)
  assert.equal(controller.getSnapshot().state, 'cancelling')
  assert.equal(closed, 0)
  assert.ok(signals.every(signal => !signal.aborted))
  await tick(10)
  assert.equal(controller.getSnapshot().state, 'cancelled')
  assert.equal(closed, 2)
  assert.ok(signals.every(signal => signal.aborted))
  assert.equal(timers.size, 0)
})

test('cancel is sent before pending action transport abort; late fetch cannot continue work', async () => {
  const action = deferred()
  let actionSignal
  const { controller, calls } = harness((url, options) => {
    if (url.endsWith('/flows')) return created()
    if (url.endsWith('/cancel')) {
      assert.equal(actionSignal.aborted, false)
      return stopped()
    }
    actionSignal = options.signal
    return action.promise
  })
  const pending = assert.rejects(controller.request('/action'), WorkflowCancelledError)
  await settle()
  await controller.cancel()
  assert.equal(actionSignal.aborted, true)
  action.resolve(json({}))
  await pending
  await assert.rejects(controller.request('/next-action'), WorkflowCancelledError)
  assert.equal(calls.length, 3)
})

test('late JSON acceptance cannot launch generation, and guarded callbacks are suppressed', async () => {
  const body = deferred()
  const { controller, calls } = harness(url => {
    if (url.endsWith('/flows')) return created()
    if (url.endsWith('/cancel')) return stopped()
    return { ok: true, json: () => body.promise }
  })
  let callbackCount = 0
  const callback = controller.guard(() => callbackCount++)
  callback()
  const workflow = (async () => {
    const response = await controller.request('/first-action')
    await response.json()
    return controller.request('/next-action')
  })()
  const rejected = assert.rejects(workflow, WorkflowCancelledError)
  await settle()
  await controller.cancel()
  callback()
  body.resolve({ ok: true })
  await rejected
  assert.equal(callbackCount, 1)
  assert.ok(!calls.some(call => call.url === '/next-action'))
})

for (const [label, response] of [
  ['unknown flow', () => json({}, 404)],
  ['expired flow', () => json({}, 410)],
  ['server error', () => json({}, 500)],
  ['lost connection', () => Promise.reject(new Error('disconnected'))],
  ['cleanup failed', () => json({ state: 'cleanup_failed', active_operations: 0 })],
  ['cancelled with active work', () => json({ state: 'cancelled', active_operations: 1 })],
  ['missing active count', () => json({ state: 'cancelled' })],
  ['string zero', () => json({ state: 'cancelled', active_operations: '0' })],
  ['unknown state', () => json({ state: 'missing', active_operations: 0 })],
]) {
  test(`${label} never acknowledges cancellation or aborts active work`, async () => {
    const { controller, calls, timers } = harness(url =>
      url.endsWith('/flows') ? created() : url.endsWith('/cancel') ? response() : json({}))
    await controller.request('/action')
    await controller.cancel()
    assert.equal(controller.getSnapshot().state, 'cancelling')
    assert.ok(controller.getSnapshot().error)
    assert.equal(calls[1].options.signal.aborted, false)
    assert.equal(timers.size, 0)
    await assert.rejects(controller.request('/next-action'), WorkflowCancelledError)
  })
}

test('retry reuses the original flow and succeeds only on explicit acknowledgement', async () => {
  let tries = 0
  const { controller, calls } = harness(url => {
    if (url.endsWith('/flows')) return created()
    if (url.endsWith('/cancel')) return ++tries === 1 ? json({}, 503) : stopped()
    return json({})
  })
  await controller.request('/action')
  await controller.cancel()
  assert.ok(controller.getSnapshot().error)
  await controller.retry()
  assert.equal(controller.getSnapshot().state, 'cancelled')
  assert.equal(controller.getSnapshot().error, null)
  assert.equal(calls.filter(call => call.url.endsWith('/flows')).length, 1)
  assert.equal(tries, 2)
})

test('status polling error remains locked and can be retried', async () => {
  let cancels = 0
  const { controller, tick } = harness(url => {
    if (url.endsWith('/flows')) return created()
    if (url.endsWith('/cancel')) return ++cancels === 1 ? stopping(1) : stopped()
    if (url.includes('/flows/')) return json({}, 404)
    return json({})
  })
  await controller.request('/action')
  await controller.cancel()
  await tick(10)
  assert.equal(controller.getSnapshot().state, 'cancelling')
  assert.ok(controller.getSnapshot().error)
  await controller.retry()
  assert.equal(controller.getSnapshot().state, 'cancelled')
})

test('creation failure is retained, including across cancellation retries', async () => {
  const { controller, calls } = harness(() => json({}, 503))
  await assert.rejects(controller.request('/action'))
  await assert.rejects(controller.request('/another-action'))
  await controller.cancel()
  await controller.retry()
  assert.equal(controller.getSnapshot().state, 'cancelling')
  assert.ok(controller.getSnapshot().error)
  assert.equal(calls.length, 1)
})

test('control body timeout stays cancelling without aborting business work', async () => {
  const never = deferred()
  const { controller, calls, tick, timers } = harness(url => {
    if (url.endsWith('/flows')) return created()
    if (url.endsWith('/cancel')) return { ok: true, json: () => never.promise }
    return json({})
  })
  await controller.request('/action')
  const pending = controller.cancel()
  await settle()
  await tick(50)
  await pending
  assert.equal(controller.getSnapshot().state, 'cancelling')
  assert.ok(controller.getSnapshot().error)
  assert.equal(calls[1].options.signal.aborted, false)
  assert.equal(calls[2].options.signal.aborted, true)
  assert.equal(timers.size, 0)
  never.resolve({ state: 'cancelled', active_operations: 0 })
  await settle()
  assert.equal(controller.getSnapshot().state, 'cancelling')
})

test('creation timeout never creates a replacement flow or starts an action', async () => {
  const never = deferred()
  const { controller, calls, tick } = harness(() => never.promise)
  const request = assert.rejects(controller.request('/action'))
  const cancellation = controller.cancel()
  await tick(50)
  await Promise.all([request, cancellation])
  await controller.retry()
  assert.equal(controller.getSnapshot().state, 'cancelling')
  assert.equal(calls.length, 1)
})

test('flows are isolated: cancelling one does not block another', async () => {
  const a = harness(url => url.endsWith('/flows') ? created('a') : stopped())
  const b = harness(url => url.endsWith('/flows') ? created('b') : json({}))
  await a.controller.request('/action')
  await b.controller.request('/action')
  await a.controller.cancel()
  await b.controller.request('/next-action')
  assert.equal(b.controller.isCancelled(), false)
  assert.equal(b.calls[2].options.headers.get('X-Workflow-Key'), 'key-b')
  assert.ok(!b.calls.some(call => call.url.endsWith('/cancel')))
})

test('unmount clears polling without auto-cancelling or aborting action streams', async () => {
  const { controller, calls, timers } = harness(url => url.endsWith('/flows') ? created() : stopping(1))
  await controller.request('/action')
  controller.deactivate()
  assert.equal(calls.length, 2)
  assert.equal(calls[1].options.signal.aborted, false)
  controller.activate()
  await controller.cancel()
  assert.equal(timers.size, 1)
  controller.deactivate()
  assert.equal(timers.size, 0)
  assert.equal(calls[1].options.signal.aborted, false)
})

test('unmount clears an in-flight control timeout and suppresses late notifications', async () => {
  const pending = deferred()
  const { controller, timers } = harness(url =>
    url.endsWith('/flows') ? created() : url.endsWith('/cancel') ? pending.promise : json({}))
  await controller.request('/action')
  let notifications = 0
  controller.subscribe(() => notifications++)
  const cancellation = controller.cancel()
  await settle()
  controller.deactivate()
  await cancellation
  assert.equal(timers.size, 0)
  assert.equal(notifications, 1)
  pending.resolve(stopped())
  await settle()
  assert.equal(controller.getSnapshot().state, 'cancelling')
})