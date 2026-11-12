import assert from 'node:assert/strict'
import test from 'node:test'
import { readSSEEvents } from '../src/lib/sse.ts'

const encoder = new TextEncoder()

async function decode(chunks: Uint8Array[]) {
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      chunks.forEach(chunk => controller.enqueue(chunk))
      controller.close()
    },
  })
  const events = []
  for await (const event of readSSEEvents(body)) events.push(event)
  assert.equal(body.locked, false)
  return events
}

test('error event and Chinese data survive every possible byte split', async () => {
  const bytes = encoder.encode('event: error\ndata: {"error":"模型依赖缺失"}\n\n')
  for (let split = 0; split <= bytes.length; split++) {
    assert.deepEqual(await decode([bytes.slice(0, split), bytes.slice(split)]), [
      { event: 'error', data: '{"error":"模型依赖缺失"}' },
    ])
  }
})

test('CRLF, bare CR, comments, multiline data and default event names', async () => {
  const text = ': ping\r\nevent:error\r\ndata: {\r\ndata: "error":"failed"}\r\n\r\nevent: ignored\r\rdata: final\r\r'
  const events = await decode([...encoder.encode(text)].map(byte => Uint8Array.of(byte)))
  assert.deepEqual(events, [
    { event: 'error', data: '{\n"error":"failed"}' },
    { event: 'message', data: 'final' },
  ])
})

test('multiple events keep distinct immutable names and data', async () => {
  assert.deepEqual(await decode([encoder.encode('event: chunk\ndata: first\n\nevent: done\ndata: {}\n\n')]), [
    { event: 'chunk', data: 'first' },
    { event: 'done', data: '{}' },
  ])
})

test('EOF never dispatches a partial terminal event', async () => {
  for (const text of ['event: done\ndata: {}', 'event: done\ndata: {}\n']) {
    assert.deepEqual(await decode([encoder.encode(text)]), [])
  }
})

test('stopping after a terminal event cancels the body and releases its lock', async () => {
  let cancelled = false
  const body = new ReadableStream<Uint8Array>({
    start(controller) { controller.enqueue(encoder.encode('event: error\ndata: {}\n\n')) },
    cancel() { cancelled = true },
  })
  for await (const event of readSSEEvents(body)) {
    assert.equal(event.event, 'error')
    break
  }
  assert.equal(cancelled, true)
  assert.equal(body.locked, false)
})

test('a failed reader releases its lock and preserves the network error', async () => {
  const body = new ReadableStream<Uint8Array>({
    start(controller) { controller.error(new Error('synthetic network failure')) },
  })
  await assert.rejects(async () => {
    for await (const _event of readSSEEvents(body)) assert.fail('No event expected')
  }, /synthetic network failure/)
  assert.equal(body.locked, false)
})
