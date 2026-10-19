// Exercise generate() and the real serializer with a stub DOM and synthetic HTTP only.
import assert from 'node:assert/strict';
const text = new Set(['value', 'textContent', 'innerHTML', 'placeholder', 'title', 'className']);
const element = () => new Proxy(function () {}, {
  get: (target, key) => key === Symbol.toPrimitive ? () => '' : text.has(key) ? ''
    : key === 'querySelectorAll' ? () => []
    : key === 'isConnected' ? false
    : key === 'classList' ? { toggle() {}, add() {}, remove() {}, contains() { return false; } }
    : key === 'style' ? { setProperty() {} } : key === 'dataset' ? {} : element(),
  set: () => true, apply: () => element(),
});
globalThis.document = new Proxy({}, { get: (_, key) => key === 'documentElement' ? { dataset: {} } : element() });
globalThis.window = globalThis;
globalThis.localStorage = { getItem: () => null, setItem() {} };
globalThis.matchMedia = () => ({ matches: false });
globalThis.innerWidth = 1400;
globalThis.fetch = () => new Promise(() => {});
globalThis.setInterval = () => 0;
globalThis.requestAnimationFrame = () => 0;
globalThis.cancelAnimationFrame = () => {};
await import('../frontend/main.mjs');
const { generate } = await import('../frontend/chat/live.mjs');
const { listState } = await import('../frontend/chat/list.mjs');
const frame = (id, data) => `id: ${id}\ndata: ${JSON.stringify(data)}\n\n`;
const original = {
  role: 'assistant', content: 'Saved partial answer', reasoning: 'Saved reasoning',
  tool_steps: [{ id: '1-1', name: 'edit_file', status: 'done', backup_id: 'backup' }],
  checkpoint: { turn_id: 'turn-with-backups' },
  rounds: [{ reasoning: 'Earlier reasoning', note: 'Edited file', tools: [] }],
  stream_id: 'saved-stream', stream_pending: true,
};
for (const scenario of ['404', '410', 'network', 'partial-then-expired', 'abort', 'success']) {
  const bot = structuredClone(original);
  listState.current = { id: 'resume-test', title: 'Resume test', messages: [{ role: 'user', content: 'Test' }, bot] };
  let saved, gets = 0;
  globalThis.fetch = async (url, options = {}) => {
    if (url.startsWith('/api/chat/streams/')) {
      if (options.method === 'DELETE') return new Response('{}');
      gets++;
      if (scenario === 'network') throw new TypeError('Network unavailable');
      if (scenario === 'abort') { listState.streaming.abort(); throw new DOMException('Aborted', 'AbortError'); }
      if (scenario === 'success') return new Response(frame(1, { choices: [{ delta: { content: 'Completed replay' } }] }) + 'id: 2\ndata: [DONE]\n\n');
      if (scenario === 'partial-then-expired' && gets === 1) return new Response(frame(1, { choices: [{ delta: { content: 'Only first fragment' } }] }));
      return new Response('{"detail":"Stream expired"}', { status: scenario === '404' ? 404 : 410 });
    }
    assert.notEqual(url, '/api/chat', 'resuming must never execute a fresh turn');
    if (url === '/api/chats/resume-test' && options.method === 'PUT') { saved = JSON.parse(options.body).messages[1]; return new Response('{}'); }
    if (url === '/api/chats') return new Response('[]');
    return new Promise(() => {}); // Background status polls do not contact a live backend.
  };
  await generate(bot);
  assert(saved, 'generate must save the final reply');
  if (scenario === 'success') {
    assert.equal(saved.content, 'Completed replay');
    assert.equal(saved.checkpoint, undefined);
    assert.equal(saved.stream_pending, false);
  } else {
    for (const key of ['content', 'reasoning', 'tool_steps', 'checkpoint', 'rounds']) assert.deepEqual(saved[key], original[key], `${scenario}: ${key}`);
    assert.equal(saved.stream_pending, scenario === 'network');
    if (scenario !== 'abort') assert(saved.error);
  }
  assert.equal(listState.streaming, null);
  console.log(`   ok chat_resume_${scenario}_preserves_saved_reply_or_commits_success`);
}
