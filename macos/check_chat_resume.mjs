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
  verification: { status: 'unverified', revision: 1, missing: ['tests'], failed: [], syntax_failures: [], background_started: false },
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
    for (const key of ['content', 'reasoning', 'tool_steps', 'checkpoint', 'rounds', 'verification']) assert.deepEqual(saved[key], original[key], `${scenario}: ${key}`);
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

// An early backend error is a finished reply, not a disconnected stream to retry.
listState.current = { id: 'early-error-test', title: 'Early error', messages: [{ role: 'user', content: 'Hello' }] };
const earlyError = "The model isn't downloaded yet. Get it from the Model library.";
let errorReply, streamRequests = 0;
globalThis.fetch = async (url, options = {}) => {
  if (url === '/api/chat') {
    streamRequests++;
    return new Response(frame(1, { error: earlyError }) + 'id: 2\ndata: [DONE]\n\n',
      { headers: { 'X-Chat-Stream-ID': 'early-error-stream' } });
  }
  assert(!url.startsWith('/api/chat/streams/'), 'a terminal error must not reconnect');
  if (url === '/api/chats/early-error-test' && options.method === 'PUT') {
    errorReply = JSON.parse(options.body).messages[1];
    return new Response('{}');
  }
  if (url === '/api/chats') return new Response('[]');
  return new Promise(() => {});
};
await generate();
assert.equal(streamRequests, 1);
assert.equal(errorReply.error, earlyError);
assert.equal(errorReply.stream_pending, false);
assert.equal(listState.streaming, null);
console.log('   ok chat_early_error_preserves_diagnostic_without_retry_or_resume');

// Verification status is persisted from the actual SSE event and renders on redraw.
// It never turns a not-required or malformed event into a success claim.
const { chVerificationHTML, turnHTML, verificationResult } = await import('../frontend/chat/thread.mjs');
for (const scenario of ['verified', 'unverified', 'not_required', 'malformed']) {
  listState.current = { id: 'verification-test', title: 'Verification test', messages: [{ role: 'user', content: 'Verify change' }] };
  const verification = { status: scenario, revision: 2, missing: scenario === 'unverified' ? ['<test-id>'] : [],
    failed: [], syntax_failures: [], background_started: false };
  let saved;
  globalThis.fetch = async (url, options = {}) => {
    if (url === '/api/chat') return new Response(frame(1, { x_verification: verification })
      + frame(2, { choices: [{ delta: { content: scenario === 'unverified' ? 'Unverified: checks missing.' : 'Final response' }, finish_reason: 'stop' }] })
      + 'id: 3\ndata: [DONE]\n\n');
    if (url === '/api/chats/verification-test' && options.method === 'PUT') { saved = JSON.parse(options.body).messages[1]; return new Response('{}'); }
    if (url === '/api/chats') return new Response('[]');
    return new Promise(() => {});
  };
  await generate();
  assert(saved);
  const html = turnHTML(saved, 1);
  if (scenario === 'malformed') {
    assert.equal(saved.verification, undefined);
    assert.equal(chVerificationHTML(saved), '');
  } else {
    assert.deepEqual(saved.verification, verification);
    if (scenario === 'verified') assert(html.includes('Configured checks passed for this turn'));
    if (scenario === 'unverified') {
      assert(html.includes('Verification incomplete') && html.includes('&lt;test-id&gt;'));
      assert(!html.includes('<test-id>') && !html.includes('Configured checks passed'));
    }
    if (scenario === 'not_required') assert.equal(chVerificationHTML(saved), '');
  }
  assert.equal(listState.streaming, null);
  console.log(`   ok chat_verification_${scenario}_persists_and_renders_accurately`);
}
assert.equal(verificationResult({ status: 'verified', revision: 1, missing: ['tests'], failed: [], syntax_failures: [], background_started: false }), null);
assert.equal(verificationResult({ status: 'verified' }), null);
assert.equal(verificationResult({ status: 'verified', revision: -1, missing: [], failed: [], syntax_failures: [], background_started: false }), null);
console.log('   ok chat_verification_rejects_inconsistent_success_events');
{
  const step = { id: '1-1', name: 'edit_file', status: 'done', summary: 'Editing src/a.py', rules_complete: false,
    rules: [{ target: 'src/<b>a.py', files: [{ name: 'AGENTS.md', scope: '.', sha256: 'abc', truncated: false },
                                              { name: 'src/<img src=x onerror=alert(1)>.md', scope: 'src', sha256: 'def', truncated: true }] }] };
  const html = turnHTML({ role: 'assistant', content: 'Done', tool_steps: [step] }, 1);
  assert(html.includes('Project rules applied') && html.includes('AGENTS.md') && html.includes('truncated') && html.includes('incomplete'));
  assert(html.includes('&lt;img src=x onerror=alert(1)&gt;') && html.includes('src/&lt;b&gt;a.py'));
  assert(!html.includes('<img src=x') && !html.includes('<b>a.py'), 'rule names from a repository must be escaped');
  const plain = turnHTML({ role: 'assistant', content: 'Done', tool_steps: [{ id: '1-2', name: 'edit_file', status: 'done', summary: 'Editing b.py' }] }, 1);
  assert(!plain.includes('Project rules applied'), 'no rules, no block');
  const empty = turnHTML({ role: 'assistant', content: 'Done', tool_steps: [{ ...step, rules: [{ target: 'x', files: [] }], rules_complete: true }] }, 1);
  assert(!empty.includes('Project rules applied'));
  console.log('   ok tool_step_shows_applied_rules_and_escapes_repository_text');
}
