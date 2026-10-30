import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from 'typescript';

async function moduleFrom(path, salt = '') {
  const source = readFileSync(new URL(path, import.meta.url), 'utf8');
  const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } });
  const apiSource = readFileSync(new URL('../lib/api.ts', import.meta.url), 'utf8');
  const apiModule = ts.transpileModule(apiSource, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText;
  const apiUrl = 'data:text/javascript;base64,' + Buffer.from(apiModule).toString('base64');
  const runnable = outputText.replace(/from ['"]\.\/api['"]/g, `from '${apiUrl}'`);
  return import('data:text/javascript;base64,' + Buffer.from(runnable + `\n// ${salt}`).toString('base64'));
}
const { restaurantData, parseCoordinate, straightLineDistance, restaurantNavigation, restaurantPageQuery, appendRestaurantPage, restaurantQueryOrigin, restaurantDistance, fillRestaurantPages, mapRestaurantQuery, mapViewIdentity, mapSearchArea, haversineMeters, createMapViewportQuery, restaurantWorkSignal } = await moduleFrom('../lib/restaurant-results.ts');
const configuration = { enabled: true, js_ready: true, verified: false, js_key: 'public-test-js-id', service_host: '/api/v1/map/proxy/_AMapService', coordinate_system: 'GCJ-02', notice: '' };

test('coordinates only come from usable result coordinates; missing and malformed values stay absent', () => {
  for (const value of [null, '', '116.4,', ',39.9', '181,40', '116,91', [116, 40], 'javascript:alert(1)']) assert.equal(parseCoordinate(value), null);
  assert.deepEqual(parseCoordinate('116.4,39.9'), [116.4, 39.9]);
  const data = restaurantData({ query: { mode: 'text', city: '北京', location: null }, items: [{ name: 'A', location: '116.4,39.9' }, { name: 'B', location: null }] });
  assert.equal(data.center, null);
  assert.deepEqual(data.items[0].location, [116.4, 39.9]);
  assert.equal(data.items[1].location, null);
  assert.notEqual(data.items[0].key, data.items[1].key);
  assert.equal(restaurantNavigation(data.items[1]), undefined);
  assert.match(restaurantNavigation(data.items[0]), /^https:\/\/uri.amap.com\/marker\?/);
});

test('distance appears only for around results with known straight-line distances', () => {
  assert.equal(straightLineDistance(0, 'around', 'straight_line'), '直线距离 0 米');
  assert.equal(straightLineDistance(1250, 'around', 'straight_line'), '直线距离 1.3 公里');
  for (const value of [null, '', false, -1, NaN]) assert.equal(straightLineDistance(value, 'around', 'straight_line'), null);
  assert.equal(straightLineDistance(200, 'text', 'straight_line'), null);
  assert.equal(straightLineDistance(200, 'around', null), null);
});

test('device distance survives map browsing, while missing coordinates never invent a user distance', () => {
  const item = { location: [116.4, 39.9], distance: '直线距离 400 米' };
  assert.equal(restaurantDistance(item, [116.4, 39.9], null), '距查询中心（直线） 400 米');
  assert.equal(restaurantDistance(item, [116.4, 39.9], [116.4, 39.9]), '距你（直线） 400 米');
  assert.equal(restaurantDistance(item, [117, 40], [116.4, 39.9]), '距你（直线） 0 米');
  assert.equal(restaurantDistance({ ...item, distance: null }, null, [116.4, 39.9]), '距你（直线） 0 米');
  assert.equal(restaurantDistance({ ...item, location: null }, [117, 40], [116.4, 39.9]), '距查询中心（直线） 400 米');
  assert.equal(restaurantDistance({ location: null, distance: null }, null, [116.4, 39.9]), null);
});

test('a long drag still queries when moveend precedes pointerup, but a stationary click does not', async () => {
  const clock = Date.now;
  let now = 10000, viewport = { key: 'start', query: 'start' };
  const calls = [];
  const gate = createMapViewportQuery({ read: () => viewport, search: query => { calls.push(query); return true; }, waitMs: 5 });
  try {
    Date.now = () => now;
    gate.gesture({ trusted: true, marker: false, type: 'pointerdown' });
    now += 4000; viewport = { key: 'destination', query: 'destination' }; gate.changed();
    gate.gesture({ trusted: true, marker: false, type: 'pointerup' });
    await new Promise(resolve => setTimeout(resolve, 12));
    assert.deepEqual(calls, ['destination']);
    viewport = { key: 'unqueried', query: 'unqueried' };
    gate.gesture({ trusted: true, marker: false, type: 'pointerdown' });
    gate.gesture({ trusted: true, marker: false, type: 'pointerup' });
    await new Promise(resolve => setTimeout(resolve, 12));
    assert.deepEqual(calls, ['destination']);
  } finally { Date.now = clock; gate.dispose(); }
});

test('unexpected coordinate systems never silently become GCJ-02 markers', () => {
  const data = restaurantData({ query: { coordinate_system: 'WGS-84', location: '116,40' }, items: [{ location: '116,40' }] });
  assert.equal(data.center, null); assert.equal(data.items[0].location, null);
});

test('SDK readiness uses enabled/js_ready and same-origin proxy, not the historical verified flag', async () => {
  const { mapConfigurationReady } = await moduleFrom('../lib/amap-sdk.ts', 'readiness');
  const origin = 'http://127.0.0.1:3000';
  assert.equal(mapConfigurationReady(configuration, origin), true);
  for (const patch of [{ enabled: false }, { js_ready: false }, { js_key: '' }, { service_host: 'https://untrusted.example/_AMapService' }, { service_host: '/wrong' }, { coordinate_system: 'WGS-84' }])
    assert.equal(mapConfigurationReady({ ...configuration, ...patch }, origin), false);
});

function browser() {
  const scripts = [];
  globalThis.window = { location: { origin: 'http://127.0.0.1:3000' }, setTimeout };
  globalThis.document = {
    createElement: () => ({ dataset: {}, remove() { this.removed = true; } }),
    head: { appendChild(script) { scripts.push(script); } },
  };
  return scripts;
}

test('SDK requests deduplicate across cards, configure proxy first, and reuse the loaded SDK', async () => {
  const scripts = browser();
  const { loadAMapSDK } = await moduleFrom('../lib/amap-sdk.ts', 'dedupe');
  const one = loadAMapSDK(configuration), two = loadAMapSDK(configuration);
  assert.equal(one, two); assert.equal(scripts.length, 1);
  assert.equal(globalThis.window._AMapSecurityConfig.serviceHost, 'http://127.0.0.1:3000/api/v1/map/proxy/_AMapService');
  assert.match(scripts[0].src, /^https:\/\/webapi.amap.com\/maps\?v=2.0&key=public-test-js-id$/);
  globalThis.window.AMap = { Map() {}, Marker() {} }; scripts[0].onload();
  assert.equal(await one, globalThis.window.AMap);
  assert.equal(await loadAMapSDK(configuration), globalThis.window.AMap); assert.equal(scripts.length, 1);
  delete globalThis.window; delete globalThis.document;
});

test('Fast Refresh keeps the successfully loaded exact SDK configuration usable', async () => {
  const scripts = browser();
  const firstModule = await moduleFrom('../lib/amap-sdk.ts', 'before-fast-refresh');
  const pending = firstModule.loadAMapSDK(configuration);
  globalThis.window.AMap = { Map() {}, Marker() {} };
  scripts[0].onload();
  await pending;
  const reloadedModule = await moduleFrom('../lib/amap-sdk.ts', 'after-fast-refresh');
  assert.equal(await reloadedModule.loadAMapSDK(configuration), globalThis.window.AMap);
  assert.equal(scripts.length, 1);
  await assert.rejects(reloadedModule.loadAMapSDK({ ...configuration, js_key: 'another-public-js-id' }), /配置已变化/);
  assert.equal(scripts.length, 1);
  delete globalThis.window; delete globalThis.document;
});

test('an unrelated window map SDK is not silently adopted as the app configuration', async () => {
  const scripts = browser();
  globalThis.window.AMap = { Map() {}, Marker() {} };
  const { loadAMapSDK } = await moduleFrom('../lib/amap-sdk.ts', 'unowned-window-sdk');
  await assert.rejects(loadAMapSDK(configuration), /配置已变化/);
  assert.equal(scripts.length, 0);
  delete globalThis.window; delete globalThis.document;
});

test('failed and timed-out SDK loads remove their script and allow a deliberate retry', async () => {
  const scripts = browser();
  const { loadAMapSDK } = await moduleFrom('../lib/amap-sdk.ts', 'retry');
  const first = loadAMapSDK(configuration); scripts[0].onerror();
  await assert.rejects(first, /加载失败/); assert.equal(scripts[0].removed, true);
  await assert.rejects(loadAMapSDK(configuration, 2), /加载失败/); assert.equal(scripts[1].removed, true);
  const last = loadAMapSDK(configuration);
  assert.equal(scripts.length, 3); globalThis.window.AMap = { Map() {}, Marker() {} }; scripts[2].onload();
  assert.equal(await last, globalThis.window.AMap);
  delete globalThis.window; delete globalThis.document;
});

test('unconfigured SDK does not create any script or contact the map provider', async () => {
  const scripts = browser();
  const { loadAMapSDK } = await moduleFrom('../lib/amap-sdk.ts', 'unconfigured');
  await assert.rejects(loadAMapSDK({ ...configuration, js_ready: false }), /尚未就绪/);
  assert.equal(scripts.length, 0);
  delete globalThis.window; delete globalThis.document;
});

test('one-off location uses the official converted coordinate plugin and does not watch the user', async () => {
  const scripts = browser(); const previousFetch = globalThis.fetch;
  globalThis.fetch = async () => ({ ok: true, json: async () => configuration });
  const { requestCurrentMapLocation } = await moduleFrom('../lib/amap-sdk.ts', 'location');
  const one = requestCurrentMapLocation(), two = requestCurrentMapLocation();
  assert.equal(one, two);
  await new Promise(resolve => setImmediate(resolve));
  let options, calls = 0;
  globalThis.window.AMap = {
    Map() {}, Marker() {}, plugin(name, done) { assert.equal(name, 'AMap.Geolocation'); done(); },
    Geolocation: class {
      constructor(value) { options = value; }
      getCurrentPosition(done) { calls++; done('complete', { position: { getLng: () => 116.4, getLat: () => 39.9 } }); }
    },
  };
  scripts[0].onload();
  assert.equal(await one, '116.4,39.9'); assert.equal(calls, 1);
  assert.equal(options.convert, true); assert.equal(options.timeout, 20000);
  assert.equal(options.GeoLocationFirst, true);
  assert.equal(options.getCityWhenFail, false);
  assert.equal(options.showMarker, false); assert.equal(options.showCircle, false);
  globalThis.fetch = previousFetch; delete globalThis.window; delete globalThis.document;
});

test('denied one-off location returns a user message and permits a fresh request', async () => {
  const scripts = browser(); const previousFetch = globalThis.fetch;
  globalThis.fetch = async () => ({ ok: true, json: async () => configuration });
  const { requestCurrentMapLocation } = await moduleFrom('../lib/amap-sdk.ts', 'denied-location');
  const first = requestCurrentMapLocation(); await new Promise(resolve => setImmediate(resolve));
  let allowed = false;
  globalThis.window.AMap = {
    Map() {}, Marker() {}, plugin(_name, done) { done(); },
    Geolocation: class {
      getCurrentPosition(done) { done(allowed ? 'complete' : 'error', allowed ? { position: { getLng: () => 116, getLat: () => 40 } } : { info: 'PERMISSION_DENIED' }); }
    },
  };
  scripts[0].onload(); await assert.rejects(first, /定位权限/);
  allowed = true; assert.equal(await requestCurrentMapLocation(), '116,40');
  globalThis.fetch = previousFetch; delete globalThis.window; delete globalThis.document;
});

test('unconfigured one-off location does not prompt the browser or load SDK', async () => {
  const scripts = browser(); const previousFetch = globalThis.fetch;
  globalThis.fetch = async () => ({ ok: true, json: async () => ({ ...configuration, js_ready: false }) });
  const { requestCurrentMapLocation } = await moduleFrom('../lib/amap-sdk.ts', 'unconfigured-location');
  await assert.rejects(requestCurrentMapLocation(), /尚未配置/); assert.equal(scripts.length, 0);
  globalThis.fetch = previousFetch; delete globalThis.window; delete globalThis.document;
});

test('location failures distinguish device permission, timeout and coordinate conversion', async () => {
  const { locationFailure } = await moduleFrom('../lib/amap-sdk.ts', 'failure-reasons');
  assert.match(locationFailure('PERMISSION_DENIED').text, /定位权限/);
  assert.match(locationFailure('TIME_OUT').text, /超时/);
  assert.match(locationFailure('CONVERT_FAILED').text, /坐标转换/);
  assert.match(locationFailure('GEOLOCATION_FAILED', 'Get geolocation failed.').text, /浏览器没有取得设备位置/);
  assert.equal(locationFailure('TIME_OUT').diagnostics.permissionDenied, false);
  assert.equal(locationFailure('CONVERT_FAILED').diagnostics.conversionFailed, true);
});

test('location failure diagnostics never expose raw provider messages or sensitive values', async () => {
  const { locationFailure } = await moduleFrom('../lib/amap-sdk.ts', 'failure-privacy');
  const raw = 'https://example.invalid/?key=secret-test-key&locations=116.4,39.9';
  const failure = locationFailure('TIME_OUT', raw);
  const serialized = JSON.stringify(failure);
  for (const value of [raw, 'secret-test-key', '116.4', 'example.invalid']) assert.equal(serialized.includes(value), false);
  assert.equal(Object.values(failure.diagnostics).every(value => typeof value === 'boolean'), true);
});


const restaurantPage = (page = 1, items = [], extra = {}) => ({
  query: { mode: 'text', city: '北京', location: null, coordinate_system: 'GCJ-02', radius_m: null, sortrule: null, page, page_size: 20 },
  scope: { rule: 'veg_category_evidence', version: 'veg-admission-v3' },
  items: items.map(item => ({ ...item, scope: { admitted: true, version: 'veg-admission-v3' } })),
  pagination: { page, page_size: 20, has_more: true, next_page: page + 1, count_source: 'unknown', matched_count: null }, ...extra,
});

test('only current v3 scope and matching item evidence qualify as a current restaurant result', () => {
  assert.equal(restaurantData(restaurantPage()).scopeCurrent, true);
  assert.equal(restaurantData(restaurantPage(1, [{ id: 'a' }])).scopeCurrent, true);
  for (const payload of [
    { items: [{ id: 'a', scope: { admitted: true } }] },
    { items: [{ id: 'a' }] }, { items: [] },
    restaurantPage(1, [], { scope: { rule: 'veg_category_evidence' } }),
    restaurantPage(1, [], { scope: { rule: 'veg_category_evidence', version: 'veg-admission-v1' } }),
    restaurantPage(1, [], { scope: { rule: 'veg_category_evidence', version: 'unexpected' } }),
    restaurantPage(1, [], { items: [{ id: 'a', scope: { admitted: true, version: 'veg-admission-v1' } }] }),
    restaurantPage(1, [], { items: [{ id: 'a', scope: { admitted: false, version: 'veg-admission-v3' } }] }),
  ]) assert.equal(restaurantData(payload).scopeCurrent, false);
});

test('next-page query keeps the area and page size; around uses only the original GCJ-02 center', () => {
  let query = new URLSearchParams(restaurantPageQuery(restaurantPage()));
  assert.equal(query.get('city'), '北京'); assert.equal(query.get('page'), '2'); assert.equal(query.get('page_size'), '20');
  const around = restaurantPage(1, [], { query: { mode: 'around', location: '116.4,39.9', coordinate_system: 'GCJ-02' } });
  query = new URLSearchParams(restaurantPageQuery(around));
  assert.equal(query.get('location'), '116.4,39.9'); assert.equal(query.has('city'), false);
  assert.equal(restaurantPageQuery({ ...around, query: { ...around.query, coordinate_system: 'WGS-84' } }), null);
  assert.equal(new URLSearchParams(restaurantPageQuery(around, true)).get('page'), '1');
});

test('district and keyword constraints survive pagination and first-page refresh in both query modes', () => {
  for (const query of [
    { mode: 'text', city: '北京', keywords: '朝阳区 素食', coordinate_system: 'GCJ-02' },
    { mode: 'around', location: '116.4,39.9', keywords: '素食火锅', coordinate_system: 'GCJ-02' },
  ]) {
    const payload = restaurantPage(1, [], { query });
    for (const firstPage of [true, false]) {
      const params = new URLSearchParams(restaurantPageQuery(payload, firstPage));
      assert.equal(params.get('keywords'), query.keywords);
      assert.equal(params.get('page'), firstPage ? '1' : '2');
      assert.equal(params.get(query.mode === 'text' ? 'city' : 'location'), query.mode === 'text' ? '北京' : '116.4,39.9');
    }
  }
  assert.equal(restaurantData(restaurantPage(1, [], { query: { mode: 'text', city: '北京', keywords: '朝阳区' } })).area, '北京 · 朝阳区');
});

test('pagination obeys explicit terminal state and refuses invalid or over-window next pages', () => {
  const last = restaurantPage(10, [], { pagination: { page: 10, page_size: 20, has_more: false, next_page: null, reached_window_end: true } });
  assert.equal(restaurantPageQuery(last), null);
  assert.equal(restaurantData(last).pagination.reachedWindowEnd, true);
  assert.equal(restaurantPageQuery(restaurantPage(10)), null);
  assert.equal(restaurantPageQuery({ query: { mode: 'text', city: '北京' }, items: [] }), null);
});

test('appending pages deduplicates valid ids but retains every missing-id row without mutating earlier results', () => {
  const first = restaurantPage(1, [{ id: 'a', name: 'One' }, { id: null, name: 'No id' }]);
  const second = restaurantPage(2, [{ id: 'a', name: 'Same id' }, { id: 'b', name: 'Two' }, { id: null, name: 'No id' }]);
  const merged = appendRestaurantPage(first, second);
  assert.deepEqual(merged.items.map(item => item.id), ['a', null, 'b', null]);
  assert.equal(first.items.length, 2); assert.equal(merged.pagination.page, 2);
  assert.equal(restaurantData(merged).items[0].key, restaurantData(first).items[0].key);
  assert.equal(new Set(restaurantData(merged).items.map(item => item.key)).size, 4);
});

test('filtered-empty pages preserve explicit has_more and an existing list', () => {
  const first = restaurantPage(1, [{ id: 'a' }]);
  const merged = appendRestaurantPage(first, restaurantPage(2));
  assert.equal(merged.items.length, 1);
  assert.equal(new URLSearchParams(restaurantPageQuery(merged)).get('page'), '3');
});

test('mismatched areas, pagination, or unchecked results cannot be merged into a current result', () => {
  const first = restaurantPage(1, [{ id: 'a' }]);
  const second = restaurantPage(2, [{ id: 'b' }]);
  for (const invalid of [
    { ...second, query: { ...second.query, city: '杭州' } },
    { ...second, query: { ...second.query, keywords: '朝阳' } },
    { ...second, pagination: { ...second.pagination, page: 3 } },
    { ...second, pagination: { ...second.pagination, page_size: 10 } },
    { ...second, scope: undefined },
    { ...second, items: null },
  ]) assert.throws(() => appendRestaurantPage(first, invalid), /已保留原有结果/);
  assert.deepEqual(first.items, [{ id: 'a', scope: { admitted: true, version: 'veg-admission-v3' } }]);
});

test('old or unversioned pages can only restart at page one, never append current results', () => {
  const current = restaurantPage(1, [{ id: 'a' }]);
  const next = restaurantPage(2, [{ id: 'b' }]);
  for (const version of [undefined, 'veg-admission-v1', 'veg-admission-v2', 'future-version']) {
    const old = { ...current, scope: { rule: 'veg_category_evidence', version } };
    assert.equal(restaurantPageQuery(old), null);
    assert.equal(new URLSearchParams(restaurantPageQuery(old, true)).get('page'), '1');
    assert.throws(() => appendRestaurantPage(old, next), /已保留原有结果/);
    assert.throws(() => appendRestaurantPage(current, { ...next, scope: old.scope }), /已保留原有结果/);
  }
  assert.equal(current.items.length, 1);
});

test('origin and distance share the original nearby query; a typed region is never the user position', () => {
  const nearby = restaurantPage(1, [{ id: 'a', distance_m: 400, distance_type: 'straight_line' }], {
    query: { mode: 'around', location: '116.4,39.9', coordinate_system: 'GCJ-02' },
  });
  assert.deepEqual(restaurantQueryOrigin(nearby, '116.4,39.9'), {
    position: [116.4, 39.9], isUserLocation: true, label: '你的位置（本次定位）',
  });
  assert.equal(restaurantQueryOrigin(nearby)?.isUserLocation, false);
  assert.equal(restaurantQueryOrigin(nearby, '121.4,31.2')?.isUserLocation, false);
  assert.equal(restaurantData(nearby).items[0].distance, '直线距离 400 米');
  for (const query of [
    { mode: 'text', city: '北京', location: '116.4,39.9' },
    { mode: 'around', location: null },
    { mode: 'around', location: '116.4,39.9', coordinate_system: 'WGS-84' },
  ]) {
    const payload = { ...nearby, query };
    assert.equal(restaurantQueryOrigin(payload, '116.4,39.9'), null);
    assert.equal(restaurantData(payload).items[0].distance, null);
  }
  assert.equal(restaurantQueryOrigin({ ...nearby, scope: { version: 'veg-admission-v1' } }, '116.4,39.9'), null);
});

test('first screen fills toward ten without exceeding three total pages or repeating a page', async () => {
  const calls = [], seen = [];
  const first = restaurantPage(1, [{ id: 'a' }, { id: 'b' }, { id: 'c' }]);
  const result = await fillRestaurantPages(first, { signal: new AbortController().signal,
    fetch: async query => {
      const params = new URLSearchParams(query), page = Number(params.get('page'));
      calls.push(page); assert.equal(params.get('page_size'), '20');
      return restaurantPage(page, Array.from({ length: 4 }, (_, i) => ({ id: `${page}-${i}` })));
    }, onPage: payload => seen.push(payload.items.length) });
  assert.deepEqual(calls, [2, 3]); assert.deepEqual(seen, [7, 11]);
  assert.equal(result.pages, 3); assert.equal(result.payload.items.length, 11);
  assert.equal(first.items.length, 3);
});

test('fill stops at the target and does not contact a next page for already sufficient or old results', async () => {
  const first = restaurantPage(1, Array.from({ length: 8 }, (_, i) => ({ id: String(i) })));
  let calls = 0;
  const result = await fillRestaurantPages(first, { signal: new AbortController().signal,
    fetch: async () => { calls++; return restaurantPage(2, [{ id: '8' }, { id: '9' }]); } });
  assert.equal(calls, 1); assert.equal(result.payload.items.length, 10);
  for (const payload of [result.payload, { ...first, scope: undefined }]) {
    await fillRestaurantPages(payload, { signal: new AbortController().signal, fetch: () => assert.fail('no automatic request') });
  }
});

test('filtered-empty pages remain pageable but auto fill has an absolute three-page batch budget', async () => {
  const calls = [];
  const result = await fillRestaurantPages(restaurantPage(), { signal: new AbortController().signal,
    fetch: async query => { const page = Number(new URLSearchParams(query).get('page')); calls.push(page); return restaurantPage(page); } });
  assert.deepEqual(calls, [2, 3]); assert.equal(result.payload.items.length, 0);
  assert.equal(new URLSearchParams(restaurantPageQuery(result.payload)).get('page'), '4');
});

test('partial failure and explicit stop retain every successful page without automatic retry', async () => {
  let calls = 0;
  const initial = restaurantPage(1, [{ id: 'one' }]);
  const failed = await fillRestaurantPages(initial, { signal: new AbortController().signal,
    fetch: async query => { calls++; if (calls === 2) throw new Error('额度已用完'); return restaurantPage(Number(new URLSearchParams(query).get('page')), [{ id: 'two' }]); } });
  assert.equal(calls, 2); assert.equal(failed.payload.items.length, 2); assert.equal(failed.error, '额度已用完');
  const controller = new AbortController(); calls = 0;
  const stopped = await fillRestaurantPages(initial, { signal: controller.signal,
    fetch: async query => { calls++; return restaurantPage(Number(new URLSearchParams(query).get('page')), [{ id: 'two' }]); },
    onPage: () => controller.abort() });
  assert.equal(calls, 1); assert.equal(stopped.stopped, true); assert.equal(stopped.payload.items.length, 2);
});

test('stopping an in-flight response prevents its late results from replacing the visible list', async () => {
  const controller = new AbortController(); const initial = restaurantPage(1, [{ id: 'one' }]);
  const result = await fillRestaurantPages(initial, { signal: controller.signal,
    fetch: async () => { controller.abort(); return restaurantPage(2, [{ id: 'late' }]); },
    onPage: () => assert.fail('abandoned page must not be published') });
  assert.deepEqual(result.payload.items.map(item => item.id), ['one']); assert.equal(result.stopped, true);
});

test('a new parent search invalidates the retained result immediately, blocking late pages and automatic resumption after failure or stop', async () => {
  for (const outcome of ['new search fails', 'new search stopped']) {
    const parentIntent = new AbortController(), child = new AbortController();
    const signal = restaurantWorkSignal(child.signal, parentIntent.signal);
    const initial = restaurantPage(1, [{ id: 'already-loaded' }]);
    let release, calls = 0, published = 0;
    const pending = fillRestaurantPages(initial, { signal,
      fetch: () => { calls++; return new Promise(resolve => { release = resolve; }); },
      onPage: () => published++ });
    assert.equal(calls, 1);
    parentIntent.abort(); // B starts before A's second page responds.
    release(restaurantPage(2, [{ id: 'stale-second-page' }]));
    const old = await pending;
    assert.equal(signal.aborted, true, outcome);
    assert.equal(old.stopped, true); assert.equal(published, 0); assert.equal(calls, 1);
    assert.deepEqual(old.payload.items.map(item => item.id), ['already-loaded']);
    await fillRestaurantPages(old.payload, { signal, fetch: () => assert.fail('failed/stopped B cannot restart A') });
    assert.equal(calls, 1);
  }
});

test('map viewport radius covers its farthest corner, clamps to approved bounds, and does not become device origin', () => {
  assert.ok(Math.abs(haversineMeters([0, 0], [0, 1]) - 111195) < 2);
  const small = mapSearchArea('116.4,39.9', '116.3999,39.8999', '116.4001,39.9001');
  assert.equal(small.radiusM, 250); assert.equal(small.capped, false);
  const huge = mapSearchArea('116.4,39.9', '115,39', '118,41');
  assert.equal(huge.radiusM, 50000); assert.equal(huge.capped, true);
  const area = mapSearchArea('116.4,39.9', '116.38,39.88', '116.42,39.92');
  assert.ok(area.radiusM >= haversineMeters([116.4,39.9], [116.42,39.92]));
  assert.equal(mapSearchArea('invalid', '116,39', '117,40'), null);
  const query = new URLSearchParams(mapRestaurantQuery('116.4,39.9', area.radiusM));
  assert.equal(query.get('radius_m'), String(area.radiusM)); assert.equal(query.get('page_size'), '20');
  assert.equal(query.has('city'), false); assert.equal(query.has('keywords'), false);
  assert.notEqual(mapViewIdentity('116.4,39.9', 14, 3000), mapViewIdentity('116.4,39.9', 14, 4000));
  assert.equal(mapViewIdentity('116.4,39.9', 10, 50000), mapViewIdentity('116.4,39.9', 7, 50000));
  assert.equal(mapRestaurantQuery('116.4,39.9', 50001), null);
});

test('zooming into street detail retains the selected search floor, while wider views can expand it', () => {
  for (const floor of [3000, 5000, 10000, 20000, 50000]) {
    assert.equal(mapSearchArea('116.4,39.9', '116.39,39.89', '116.41,39.91', floor).radiusM, floor);
  }
  const wide = mapSearchArea('116.4,39.9', '116.2,39.7', '116.6,40.1', 10000);
  assert.ok(wide.radiusM > 10000 && wide.radiusM < 50000);
  assert.equal(mapSearchArea('116.4,39.9', '115,39', '118,41', 10000).radiusM, 50000);
  for (const floor of [0, 50001, NaN]) assert.equal(mapSearchArea('116.4,39.9', '116.39,39.89', '116.41,39.91', floor), null);
});

test('zooming within the existing nearby search does not repeat it; dragging to a new center does', async () => {
  const calls = [];
  let center = '116.4,39.9', zoom = 14;
  const read = () => {
    const area = mapSearchArea(center, '116.39,39.89', '116.41,39.91', 10000);
    return { query: mapRestaurantQuery(center, area.radiusM), key: mapViewIdentity(center, zoom, area.radiusM) };
  };
  const gate = createMapViewportQuery({ initialKey: read().key, read, search: query => { calls.push(query); return true; }, waitMs: 5 });
  const change = async () => {
    gate.gesture({ trusted: true, marker: false, type: 'wheel' }); gate.changed();
    await new Promise(resolve => setTimeout(resolve, 12));
  };
  zoom = 16; await change();
  assert.equal(calls.length, 0);
  center = '116.41,39.91'; await change();
  assert.equal(calls.length, 1);
  assert.equal(new URLSearchParams(calls[0]).get('radius_m'), '10000');
  zoom = 17; await change();
  assert.equal(calls.length, 1);
  gate.dispose();
});

test('programmatic fit/selection, untrusted events and marker clicks cannot query the viewport', async () => {
  const calls = [];
  const gate = createMapViewportQuery({ read: () => ({ query: 'location=x', key: 'one' }), search: (...values) => { calls.push(values); return true; }, waitMs: 5 });
  gate.changed(); // initial fitView has no user gesture
  gate.gesture({ trusted: false, marker: false, type: 'pointerdown' }); gate.changed();
  gate.gesture({ trusted: true, marker: true, type: 'pointerdown' }); gate.changed();
  gate.gesture({ trusted: true, marker: false, type: 'keydown', key: 'Tab' }); gate.changed();
  gate.gesture({ trusted: true, marker: false, type: 'wheel' });
  gate.suppress(); gate.changed(); // choosing a row or clicking "view all"
  await new Promise(resolve => setTimeout(resolve, 12));
  assert.equal(calls.length, 0); gate.dispose();
});

test('user moveend plus zoomend coalesce, duplicate query keys do not fetch twice, and disposal cancels pending work', async () => {
  const calls = [];
  let viewport = { query: 'location=one&radius_m=3000', key: 'one' };
  const gate = createMapViewportQuery({ read: () => viewport, search: (...values) => { calls.push(values); return true; }, waitMs: 5 });
  const gesture = () => gate.gesture({ trusted: true, marker: false, type: 'touchend' });
  gesture(); gate.changed(); gate.changed();
  await new Promise(resolve => setTimeout(resolve, 12));
  assert.equal(calls.length, 1);
  gesture(); gate.changed();
  await new Promise(resolve => setTimeout(resolve, 12));
  assert.equal(calls.length, 1);
  viewport = { query: 'location=one&radius_m=4000', key: 'two' };
  gesture(); gate.changed(); gate.dispose();
  await new Promise(resolve => setTimeout(resolve, 12));
  assert.equal(calls.length, 1);
});

test('returning to the already loaded viewport before debounce cancels the abandoned pending search', async () => {
  const calls = []; let viewport = { query: 'one', key: 'one' };
  const gate = createMapViewportQuery({ read: () => viewport, search: query => { calls.push(query); return true; }, waitMs: 5 });
  const gesture = () => gate.gesture({ trusted: true, marker: false, type: 'pointerup' });
  gesture(); gate.changed(); await new Promise(resolve => setTimeout(resolve, 12));
  viewport = { query: 'two', key: 'two' }; gesture(); gate.changed();
  viewport = { query: 'one', key: 'one' }; gesture(); gate.changed();
  await new Promise(resolve => setTimeout(resolve, 12));
  assert.deepEqual(calls, ['one']); gate.dispose();
});

test('failed viewport requests release their key for a new user gesture and do not retry themselves', async () => {
  let attempts = 0;
  const viewport = { query: 'same-center&radius_m=3000', key: 'same' };
  const gate = createMapViewportQuery({ read: () => viewport, search: async () => { attempts++; return false; }, waitMs: 5 });
  const gesture = () => { gate.gesture({ trusted: true, marker: false, type: 'wheel' }); gate.changed(); };
  gesture(); await new Promise(resolve => setTimeout(resolve, 12));
  assert.equal(attempts, 1);
  await new Promise(resolve => setTimeout(resolve, 12)); assert.equal(attempts, 1);
  gesture(); await new Promise(resolve => setTimeout(resolve, 12));
  assert.equal(attempts, 2); gate.dispose();
});

test('stopped viewport requests can retry the same key even if the old request ignores abort; stale success cannot lock it again', async () => {
  const replies = []; let attempts = 0;
  const viewport = { query: 'same-center', key: 'same' };
  const gate = createMapViewportQuery({ read: () => viewport, search: () => { attempts++; return new Promise(resolve => replies.push(resolve)); }, waitMs: 5 });
  const gesture = () => { gate.gesture({ trusted: true, marker: false, type: 'touchend' }); gate.changed(); };
  gesture(); await new Promise(resolve => setTimeout(resolve, 12));
  gesture(); await new Promise(resolve => setTimeout(resolve, 12)); assert.equal(attempts, 1); // In-flight dedupe.
  gate.cancel();
  gesture(); await new Promise(resolve => setTimeout(resolve, 12)); assert.equal(attempts, 2);
  replies[0](true); replies[1](false); await new Promise(resolve => setImmediate(resolve));
  gesture(); await new Promise(resolve => setTimeout(resolve, 12)); assert.equal(attempts, 3);
  replies[2](true); await new Promise(resolve => setImmediate(resolve));
  gesture(); await new Promise(resolve => setTimeout(resolve, 12)); assert.equal(attempts, 3); // Successful result dedupes.
  gate.dispose();
});

test('map radius survives pagination and mismatched radius cannot append to the same query', () => {
  const query = { mode: 'around', location: '116.4,39.9', coordinate_system: 'GCJ-02', radius_m: 7200 };
  const first = restaurantPage(1, [{ id: 'a' }], { query });
  const next = restaurantPage(2, [{ id: 'b' }], { query });
  assert.equal(new URLSearchParams(restaurantPageQuery(first)).get('radius_m'), '7200');
  assert.equal(new URLSearchParams(restaurantPageQuery(first, true)).get('radius_m'), '7200');
  assert.equal(appendRestaurantPage(first, next).items.length, 2);
  assert.throws(() => appendRestaurantPage(first, { ...next, query: { ...query, radius_m: 3000 } }), /不一致/);
  assert.equal(restaurantQueryOrigin(first, '121.4,31.2').isUserLocation, false);
});
