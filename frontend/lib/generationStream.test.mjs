import test from 'node:test'
import assert from 'node:assert/strict'
import { readGenerationResult } from './generationStream.mjs'

function response(text, chunkSize = 1) {
  const bytes = new TextEncoder().encode(text)
  return new Response(new ReadableStream({
    start(controller) {
      for (let i = 0; i < bytes.length; i += chunkSize) controller.enqueue(bytes.slice(i, i + chunkSize))
      controller.close()
    },
  }))
}

test('waits for completion across split CRLF and UTF-8 chunks', async () => {
  const result = await readGenerationResult(response(
    'event: step\r\ndata: {"message":"Génération"}\r\n\r\n' +
    'event: complete\r\ndata: {"generated":1,"incomplete":0,"results":[{"result":"generated"}]}\r\n\r\n'))
  assert.equal(result.generated, 1)
})

test('reports an incomplete procedure rather than treating HTTP 200 as success', async () => {
  await assert.rejects(readGenerationResult(response(
    'event: complete\ndata: {"generated":0,"incomplete":1,"results":[{"result":"incomplete","notes":"Missing captured field"}]}\n\n')),
  /Missing captured field/)
})

test('preserves blocked SSO error', async () => {
  await assert.rejects(readGenerationResult(response(
    'event: error\ndata: {"message":"SSO precheck failed"}\n\nevent: complete\ndata: {"blocked":1}\n\n')), /SSO precheck failed/)
})

test('interrupted stream is not a success', async () => {
  await assert.rejects(readGenerationResult(response('event: step\ndata: {"message":"Started"}\n\n')), /before completion/)
})

test('HTTP error body is exposed', async () => {
  await assert.rejects(readGenerationResult(new Response(JSON.stringify({ detail: { message: 'Invalid proposal' } }), { status: 400 })), /Invalid proposal/)
})

test('supports a final event without a trailing blank line', async () => {
  assert.equal((await readGenerationResult(response('event: complete\ndata: {"generated":1,"results":[{"result":"generated"}]}'))).generated, 1)
})

test('reads the actual api.sse data-only envelope', async () => {
  const result = await readGenerationResult(response(
    'data: {"type":"step","data":{"message":"Generating"}}\n\n' +
    'data: {"type":"complete","data":{"generated":1,"incomplete":0,"results":[{"result":"generated"}]}}\n\n'))
  assert.equal(result.generated, 1)
})

test('backend terminal error closes without a complete event', async () => {
  await assert.rejects(readGenerationResult(response(
    'data: {"type":"error","data":{"message":"Login unavailable"}}\n\n')), /Login unavailable/)
})

test('backend incomplete envelope is rejected', async () => {
  await assert.rejects(readGenerationResult(response(
    'data: {"type":"complete","data":{"incomplete":1,"results":[{"result":"incomplete","notes":"Review saved approval"}]}}\n\n')),
  /Review saved approval/)
})