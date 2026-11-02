import test from 'node:test';
import assert from 'node:assert/strict';
import { setTimeout as delay } from 'node:timers/promises';
import { loadPair } from './node-owned-pair.mjs';
import { renderAccount } from './node-request-local-account.mjs';
import { withTrace, currentRequestId } from './node-request-trace-scope.mjs';

const parent = () => new AbortController().signal;
function untilAbort(signal, cleanup) {
  signal.throwIfAborted();
  return new Promise((_, reject) => {
    signal.addEventListener('abort', () => {
      cleanup();
      reject(signal.reason);
    }, { once: true });
  });
}

test('pair returns both results in argument order', async () => {
  const results = await loadPair(async () => { await delay(2); return 'left'; }, async () => 'right', parent(), 1000);
  assert.deepEqual(results, ['left', 'right']);
});

test('pair failure cancels sibling and waits for cleanup, preserving error', { timeout: 2000 }, async () => {
  let releaseCleanup;
  const cleanupBarrier = new Promise(resolve => { releaseCleanup = resolve; });
  let signalCleanupStarted;
  const cleanupStarted = new Promise(resolve => { signalCleanupStarted = resolve; });
  let cleaned = false;
  let settled = false;
  const failure = new Error('right failed');
  const pending = loadPair(
    async signal => {
      try {
        await untilAbort(signal, () => {});
      } finally {
        signalCleanupStarted();
        await cleanupBarrier;
        cleaned = true;
      }
    },
    async () => { throw failure; }, parent(), 1000);
  // Observe rejection immediately, including in the deliberately faulty variant.
  const outcome = pending.then(
    value => { settled = true; return { value }; },
    error => { settled = true; return { error }; },
  );
  let result;
  try {
    await cleanupStarted;
    // One event-loop turn lets every ready Promise reaction run; this is not
    // a throughput/timing assertion. Cleanup remains blocked by our barrier.
    await delay(0);
    assert.equal(settled, false, 'loadPair returned before asynchronous sibling cleanup');
  } finally {
    releaseCleanup();
    result = await outcome;
  }
  assert.equal(result.error, failure);
  assert.equal(cleaned, true);
});

test('synchronous port failure also owns the other task', async () => {
  let cleaned = false;
  const failure = new Error('sync failure');
  await assert.rejects(() => loadPair(
    () => { throw failure; },
    signal => untilAbort(signal, () => { cleaned = true; }), parent(), 1000),
    error => error === failure);
  assert.equal(cleaned, true);
});

test('caller cancellation reaches both port tasks', async () => {
  const caller = new AbortController();
  let cleaned = 0;
  const failure = new Error('caller stopped');
  const pending = loadPair(
    signal => untilAbort(signal, () => { cleaned++; }),
    signal => untilAbort(signal, () => { cleaned++; }), caller.signal, 1000);
  await Promise.resolve();
  caller.abort(failure);
  await assert.rejects(() => pending, error => error === failure);
  assert.equal(cleaned, 2);
});

test('interleaved SSR requests keep tenant and user in request locals', async () => {
  let release;
  const barrier = new Promise(resolve => { release = resolve; });
  const principals = {
    a: { tenantId: 'ta', userId: 'a' },
    b: { tenantId: 'tb', userId: 'b' },
  };
  const auth = async credential => principals[credential];
  const load = async principal => { await barrier; return { ...principal }; };
  const render = value => value;
  const a = renderAccount('a', auth, load, render);
  const b = renderAccount('b', auth, load, render);
  await Promise.resolve();
  release();
  const results = await Promise.all([a, b]);
  assert.deepEqual(results.map(r => r.principal), [principals.a, principals.b]);
  assert.deepEqual(results.map(r => r.account), [principals.a, principals.b]);
});

test('AsyncLocalStorage preserves separate traces and restores outer scope', async () => {
  const run = id => withTrace(id, async () => {
    await delay(id === 'a' ? 2 : 1);
    return currentRequestId();
  });
  assert.deepEqual(await Promise.all([run('a'), run('b')]), ['a', 'b']);
  assert.equal(currentRequestId(), undefined);
});
