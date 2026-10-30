import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from 'typescript';

const source = readFileSync(new URL('../lib/restaurant-search.ts', import.meta.url), 'utf8');
const output = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText;
const { runRestaurantSearch, RestaurantSearchError } = await import('data:text/javascript;base64,' + Buffer.from(output).toString('base64'));
function deferred() {
  let resolve, reject;
  const promise = new Promise((accept, fail) => { resolve = accept; reject = fail; });
  return { promise, resolve, reject };
}

test('city search uses the submitted region without requesting location permission', async () => {
  const phases = [];
  const calls = [];
  const result = await runRestaurantSearch({ mode: 'city', city: ' 深圳南山 ' }, new AbortController().signal, {
    locate: async () => assert.fail('a city query must not locate'),
    query: async (query, signal) => { calls.push(query); assert.equal(signal.aborted, false); return { items: [] }; },
    onPhase: phase => phases.push(phase),
  });
  assert.deepEqual(calls, [{ city: '深圳南山' }]);
  assert.deepEqual(phases, [{ kind: 'searching', mode: 'city', city: '深圳南山' }]);
  assert.equal(result.currentLocation, null);
});

test('missing city never defaults to Beijing or invokes a query', async () => {
  await assert.rejects(runRestaurantSearch({ mode: 'city', city: ' ' }, new AbortController().signal, {
    locate: async () => assert.fail('no location fallback'),
    query: async () => assert.fail('no default-city fallback'),
    onPhase: () => assert.fail('no query should begin'),
  }), error => error instanceof RestaurantSearchError && /填写/.test(error.message));
});

test('nearby search exposes location and query as separate phases in order', async () => {
  const location = deferred();
  const phases = [];
  let queried = false;
  const result = runRestaurantSearch({ mode: 'nearby' }, new AbortController().signal, {
    locate: () => location.promise,
    query: async query => { queried = true; assert.deepEqual(query, { location: '116.4,39.9', radius_m: '10000' }); return 'results'; },
    onPhase: phase => phases.push(phase),
  });
  assert.equal(queried, false);
  assert.deepEqual(phases, [{ kind: 'locating' }]);
  location.resolve('116.4,39.9');
  assert.deepEqual(await result, { payload: 'results', currentLocation: '116.4,39.9' });
  assert.deepEqual(phases, [{ kind: 'locating' }, { kind: 'searching', mode: 'nearby' }]);
});

test('nearby search sends the selected radius in its first request without progressive expansion calls', async () => {
  for (const radiusM of [3000, 5000, 10000, 20000, 50000]) {
    const calls = [];
    await runRestaurantSearch({ mode: 'nearby', radiusM }, new AbortController().signal, {
      locate: async () => '116.4,39.9',
      query: async query => { calls.push(query); return { items: [] }; },
      onPhase() {},
    });
    assert.deepEqual(calls, [{ location: '116.4,39.9', radius_m: String(radiusM) }]);
  }
});

test('invalid nearby radius cannot trigger location permission or restaurant requests', async () => {
  for (const radiusM of [0, 249, 50001, 3000.5, NaN, Infinity]) {
    await assert.rejects(runRestaurantSearch({ mode: 'nearby', radiusM }, new AbortController().signal, {
      locate: async () => assert.fail('invalid radius must not request location'),
      query: async () => assert.fail('invalid radius must not query'),
      onPhase: () => assert.fail('invalid radius must not begin a search'),
    }), error => error instanceof RestaurantSearchError && error.phase === 'searching');
  }
});

test('location refusal and unusable coordinates never start a restaurant query', async () => {
  for (const failure of [new Error('定位超时'), '116,', '181,40', ',40']) {
    await assert.rejects(runRestaurantSearch({ mode: 'nearby' }, new AbortController().signal, {
      locate: async () => { if (failure instanceof Error) throw failure; return failure; },
      query: async () => assert.fail('location failed: restaurant query is forbidden'),
      onPhase: phase => assert.equal(phase.kind, 'locating'),
    }), error => error instanceof RestaurantSearchError && error.phase === 'locating');
  }
});

test('cancelling a pending location prevents late coordinates from starting a query', async () => {
  const location = deferred();
  const controller = new AbortController();
  const phases = [];
  const result = runRestaurantSearch({ mode: 'nearby' }, controller.signal, {
    locate: () => location.promise,
    query: async () => assert.fail('cancelled location must never start a query'),
    onPhase: phase => phases.push(phase),
  });
  controller.abort();
  location.resolve('116.4,39.9');
  await assert.rejects(result, { name: 'AbortError' });
  assert.deepEqual(phases, [{ kind: 'locating' }]);
});

test('an abandoned query that ignores abort cannot return data after a newer query', async () => {
  const oldResponse = deferred();
  const oldController = new AbortController();
  let displayed = { items: ['previous successful result'] };
  const oldSearch = runRestaurantSearch({ mode: 'city', city: '上海' }, oldController.signal, {
    locate: async () => assert.fail(),
    query: () => oldResponse.promise,
    onPhase() {},
  }).then(result => { displayed = result.payload; });
  assert.deepEqual(displayed, { items: ['previous successful result'] });
  oldController.abort();
  const fresh = await runRestaurantSearch({ mode: 'city', city: '杭州' }, new AbortController().signal, {
    locate: async () => assert.fail(),
    query: async () => ({ items: ['new Hangzhou result'] }),
    onPhase() {},
  });
  displayed = fresh.payload;
  oldResponse.resolve({ items: ['late Shanghai result'] });
  await assert.rejects(oldSearch, { name: 'AbortError' });
  assert.deepEqual(displayed, { items: ['new Hangzhou result'] });
});

test('restaurant failure stays a query failure and does not become a location error', async () => {
  await assert.rejects(runRestaurantSearch({ mode: 'nearby' }, new AbortController().signal, {
    locate: async () => '116.4,39.9',
    query: async () => { throw new Error('查询服务限流'); },
    onPhase() {},
  }), error => error instanceof RestaurantSearchError && error.phase === 'searching' && error.message === '查询服务限流');
});
