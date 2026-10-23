// One transport for legacy POST streams and opt-in server replay. Reconnection never sends another POST.
export async function* chatEvents(body, signal, onStream, resumeId = null, onWorkflowRun = null) {
  let streamId = resumeId, after = 0, retries = 0, complete = false;
  const stop = () => { if (streamId) fetch(`/api/chat/streams/${encodeURIComponent(streamId)}`, { method: 'DELETE', keepalive: true }).catch(() => {}); };
  signal.addEventListener('abort', stop);
  try {
    for (;;) {
      let reader;
      try {
        const res = await fetch(streamId ? `/api/chat/streams/${encodeURIComponent(streamId)}?after=${after}` : '/api/chat', streamId
          ? { signal } : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal });
        if (!res.ok) {
          const detail = await res.json().catch(() => ({}));
          const error = new Error(detail.detail || `HTTP ${res.status}`);
          error.terminal = true; error.resumeExpired = [404, 410].includes(res.status);
          throw error;
        }
        const id = res.headers.get('X-Chat-Stream-ID');
        const runId = res.headers.get('X-Workflow-Run-ID');
        if (runId && /^[a-f0-9]{32}$/.test(runId) && onWorkflowRun) await onWorkflowRun(runId);
        if (id && id !== streamId) { streamId = id; await onStream(id); }
        reader = res.body.getReader();
        const decoder = new TextDecoder(); let buffer = '';
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          let index;
          while ((index = buffer.indexOf('\n\n')) >= 0) {
            const event = buffer.slice(0, index); buffer = buffer.slice(index + 2);
            const idLine = event.match(/^id:\s*(\d+)$/m);
            const eventId = idLine ? +idLine[1] : null;
            if (eventId !== null && eventId <= after) continue;
            if (eventId !== null) after = eventId;
            if (/^data:\s*\[DONE\]\s*$/m.test(event)) complete = true;
            yield event;
            if (complete) return;
          }
        }
        if (!streamId || complete) return; // legacy EOF is unchanged
        throw new Error('The reply stream disconnected.');
      } catch (error) {
        if (signal.aborted || error.name === 'AbortError' || error.terminal || !streamId || retries >= 3) throw error;
        retries++;
        await new Promise((resolve) => setTimeout(resolve, 250 * retries));
        if (signal.aborted) throw new DOMException('Stopped', 'AbortError');
      } finally {
        if (reader) { await reader.cancel().catch(() => {}); reader.releaseLock(); }
      }
    }
  } finally {
    signal.removeEventListener('abort', stop);
  }
}
