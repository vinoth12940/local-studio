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
globalThis.document = new Proxy({}, { get: (_, key) => key === 'documentElement' ? { dataset: {} } : key === 'hidden' ? false : element() });
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

// A pending frame can remain suspended when the native window is covered.
// Prompts must still update the tool state and request an immediate paint.
const { chHarnessCardHTML } = await import('../frontend/chat/thread.mjs');
for (const scenario of ['approval-stop', 'question-stop', 'tool-eof']) {
  listState.current = { id: 'prompt-test', title: 'Prompt test', messages: [{ role: 'user', content: '/init' }] };
  let saved, inspected = false, cancelledFrame = false;
  globalThis.requestAnimationFrame = () => 123;
  globalThis.cancelAnimationFrame = () => { cancelledFrame = true; };
  globalThis.fetch = async (url, options = {}) => {
    if (url === '/api/chat') {
      const step = { id: '0-0', name: 'write_file', summary: 'Writing file: AGENTS.md', status: 'running' };
      if (scenario === 'tool-eof') return new Response(frame(1, { x_tool: step }));
      const isQuestion = scenario === 'question-stop';
      const prompt = { id: '0-0', session_id: 'prompt-session', title: 'Create AGENTS.md', question: 'Create guidance?', kind: 'diff', preview: '+# Guide' };
      return new Response(new ReadableStream({ start(c) {
        c.enqueue(new TextEncoder().encode(frame(1, { x_tool: step }) + frame(2, { [isQuestion ? 'x_question' : 'x_approval']: prompt })));
        options.signal.addEventListener('abort', () => c.error(new DOMException('Aborted', 'AbortError')));
        setTimeout(() => {
          const bot = listState.current.messages.at(-1);
          assert.equal(bot.tool_steps[0].status, 'waiting');
          assert(bot[isQuestion ? 'pendingQuestion' : 'pendingApproval']);
          assert(chHarnessCardHTML(bot).includes(isQuestion ? 'Create guidance?' : 'Create AGENTS.md'));
          assert(cancelledFrame, 'prompt must not wait for a suspended animation frame');
          inspected = true;
          listState.streaming.abort();
        }, 10);
      } }));
    }
    if (url === '/api/chats/prompt-test' && options.method === 'PUT') { saved = JSON.parse(options.body).messages[1]; return new Response('{}'); }
    if (url === '/api/chats') return new Response('[]');
    return new Promise(() => {});
  };
  await generate();
  if (scenario !== 'tool-eof') assert(inspected);
  assert.equal(saved.tool_steps[0].status, 'error');
  assert.equal(saved.rounds[0].tools[0].status, 'error');
  assert.match(saved.tool_steps[0].error, scenario === 'tool-eof' ? /ended before/ : /Stopped/);
  assert.equal(listState.streaming, null);
  console.log(`   ok chat_${scenario}_does_not_leave_running_tools`);
}
