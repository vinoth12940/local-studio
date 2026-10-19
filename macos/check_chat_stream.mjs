import assert from 'node:assert/strict';
import { savedMessages } from '../frontend/chat/record.mjs';
import { chatEvents } from '../frontend/chat/stream.mjs';
const original = globalThis.fetch;
const frame = (id, data) => `id: ${id}\ndata: ${JSON.stringify(data)}\n\n`;
const response = (text, id = 'test-stream') => new Response(text, { headers: { 'X-Chat-Stream-ID': id } });
try {
  const saved = JSON.parse(JSON.stringify(savedMessages({ messages: [{ role: 'assistant', content: '', stream_id: 'survives-reload', stream_pending: true }] })));
  assert.equal(saved[0].stream_id, 'survives-reload'); assert.equal(saved[0].stream_pending, true);
  assert.deepEqual(JSON.parse(JSON.stringify(savedMessages({messages:[{role:'user',content:'legacy'}]}))),[{role:'user',content:'legacy'}]);
  console.log('   ok chat_stream_handle_is_persisted_without_changing_legacy_messages');
  const calls = []; let n = 0;
  globalThis.fetch = async (url) => {
    calls.push(url);
    if (++n === 1) return new Response(new ReadableStream({ start(c) {
      c.enqueue(new TextEncoder().encode(frame(1, { choices: [{ delta: { content: 'one' } }] })));
      setTimeout(() => c.error(new TypeError('connection lost')), 10);
    } }), { headers: { 'X-Chat-Stream-ID': 'test-stream' } });
    return response(frame(1, { duplicate: true }) + frame(2, { choices: [{ delta: { content: 'two' } }] }) + 'id: 3\ndata: [DONE]\n\n');
  };
  const events = [];
  for await (const event of chatEvents({}, new AbortController().signal, async () => {})) events.push(event);
  assert.equal(calls.filter(x => x === '/api/chat').length, 1);
  assert.equal(calls[1], '/api/chat/streams/test-stream?after=1');
  assert.equal(events.length, 3);
  assert(!events.join('').includes('duplicate'));
  console.log('   ok chat_stream_reconnect_uses_get_without_duplicate_events');

  globalThis.fetch = async (url) => {
    assert.equal(url, '/api/chat/streams/saved?after=0');
    return response(frame(1, { x_approval: { id: 'approval', session_id: 'still-waiting' } }) + 'id: 2\ndata: [DONE]\n\n', 'saved');
  };
  const replay = [];
  for await (const event of chatEvents({}, new AbortController().signal, () => {}, 'saved')) replay.push(event);
  assert(replay[0].includes('still-waiting'));
  console.log('   ok chat_stream_reload_replays_saved_approval');

  const controller = new AbortController(); let deleted = false;
  globalThis.fetch = async (url, options) => {
    if (options.method === 'DELETE') { deleted = true; return new Response('{}'); }
    return new Response(new ReadableStream({ start(c) {
      options.signal.addEventListener('abort', () => c.error(new DOMException('Aborted', 'AbortError')));
      setTimeout(() => controller.abort(), 10);
    } }), { headers: { 'X-Chat-Stream-ID': 'stop' } });
  };
  await assert.rejects(async () => { for await (const event of chatEvents({}, controller.signal, () => {})) {} }, { name: 'AbortError' });
  assert(deleted);
  console.log('   ok chat_stream_stop_cancels_server_producer');

  let tries = 0;
  globalThis.fetch = async () => { tries++; return new Response('{"detail":"expired"}', { status: 410 }); };
  await assert.rejects(async () => { for await (const event of chatEvents({}, new AbortController().signal, () => {}, 'expired')) {} }, e => e.resumeExpired);
  assert.equal(tries, 1);
  globalThis.fetch = async (url) => { assert.equal(url, '/api/chat'); return new Response('data: {"legacy":true}\n\n'); };
  const legacy = [];
  for await (const event of chatEvents({}, new AbortController().signal, () => {})) legacy.push(event);
  assert.equal(legacy.length, 1);
  console.log('   ok chat_stream_expiry_is_terminal_and_legacy_eof_is_supported');
} finally { globalThis.fetch = original; }
