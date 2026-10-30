import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from 'typescript';

function compiled(path) {
  return ts.transpileModule(readFileSync(new URL(path, import.meta.url), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  }).outputText;
}
const resultsURL = 'data:text/javascript;base64,' + Buffer.from(compiled('../lib/restaurant-results.ts')).toString('base64');
const source = compiled('../lib/restaurant-discovery.ts').replace("from './restaurant-results'", `from '${resultsURL}'`);
const { RESTAURANT_DISCOVERY_KEYWORDS, restaurantDiscoveryKeyword, restaurantDiscoveryQuery,
  assertRestaurantDiscoveryResponse, mergeRestaurantDiscoveryChannels } = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
const version = 'veg-admission-v3';
const item = (id, name = id) => ({ id, name, scope: { admitted: true, version } });
function response(keyword = '素食', rows = [item('A')], query = {}, paging = {}) {
  return {
    items: rows,
    scope: { rule: 'veg_category_evidence', version },
    query: { mode: 'text', city: '北京市朝阳区', keywords: keyword, coordinate_system: 'GCJ-02', page: 1, page_size: 20, ...query },
    pagination: { page: 1, page_size: 20, has_more: true, next_page: 2, ...paging },
  };
}

test('one selected discovery keyword preserves area and page size; unsupported keywords do not query', () => {
  const original = response(undefined, [], { keywords: undefined, page_size: 10 }, { page_size: 10 });
  assert.equal(restaurantDiscoveryKeyword(original), '素食');
  for (const keyword of RESTAURANT_DISCOVERY_KEYWORDS) {
    assert.deepEqual(Object.fromEntries(new URLSearchParams(restaurantDiscoveryQuery(original, keyword))), {
      city: '北京市朝阳区', keywords: keyword, page: '1', page_size: '10',
    });
  }
  assert.equal(restaurantDiscoveryQuery(original, '烤鸭'), null);
  assert.equal(restaurantDiscoveryQuery(original, 'Vegan'), null);
  assert.equal(restaurantDiscoveryQuery(original, '素食|vegan'), null);
});

test('legacy category cards do not masquerade as an already queried 素食 keyword channel', () => {
  const legacyByStrategy = response(null, [item('A')], { search_strategy: 'vegetarian_category' });
  const legacyByType = { ...response(null, [item('A')]), typecode: '050120' };
  const incoming = { ...response('素食', [item('A'), item('B')]), typecode: '050000' };
  for (const legacy of [legacyByStrategy, legacyByType]) {
    assert.equal(restaurantDiscoveryKeyword(legacy), null);
    assert.equal(new URLSearchParams(restaurantDiscoveryQuery(legacy, '素食')).get('keywords'), '素食');
    assert.doesNotThrow(() => assertRestaurantDiscoveryResponse(legacy, incoming, '素食'));
    const merged = mergeRestaurantDiscoveryChannels([legacy], incoming);
    assert.deepEqual(merged.items.map(row => row.id), ['A', 'B']);
  }
  assert.equal(restaurantDiscoveryKeyword(response(null)), '素食');
  assert.equal(restaurantDiscoveryKeyword({ ...incoming, typecode: '050120' }), '素食');
});

test('nearby discovery preserves the exact center and chosen radius instead of reacquiring GPS', () => {
  const around = response(null, [], { mode: 'around', city: null, location: '116.460200,39.921900', radius_m: 10000, sortrule: 'distance' });
  assert.equal(restaurantDiscoveryKeyword(around), null);
  assert.deepEqual(Object.fromEntries(new URLSearchParams(restaurantDiscoveryQuery(around, '蔬食'))), {
    keywords: '蔬食', page: '1', page_size: '20', location: '116.4602,39.9219', radius_m: '10000',
  });
  const incoming = response('蔬食', [], { mode: 'around', city: null, location: '116.4602,39.9219', radius_m: 10000, sortrule: 'distance' });
  assert.doesNotThrow(() => assertRestaurantDiscoveryResponse(around, incoming, '蔬食'));
  for (const patch of [{ location: '116.46,39.9219' }, { radius_m: 3000 }, { radius_m: undefined }, { location: undefined }, { coordinate_system: 'WGS-84' }, { sortrule: undefined }]) {
    assert.throws(() => assertRestaurantDiscoveryResponse(around, { ...incoming, query: { ...incoming.query, ...patch } }, '蔬食'), /保留原有餐厅/);
  }
});

test('new-channel acceptance rejects changed city, administrative scope, rule, keyword, page and size', () => {
  const area = { adcode: '110105', level: 'district' };
  const original = response('素食', [], { area_resolution: area });
  const incoming = response('vegan', [], { area_resolution: area });
  assert.doesNotThrow(() => assertRestaurantDiscoveryResponse(original, incoming, 'vegan'));
  const invalid = [
    { ...incoming, query: { ...incoming.query, city: '北京海淀' } },
    { ...incoming, query: { ...incoming.query, city: null } },
    { ...incoming, query: { ...incoming.query, area_resolution: undefined } },
    { ...incoming, query: { ...incoming.query, area_resolution: { adcode: '110108', level: 'district' } } },
    { ...incoming, scope: { rule: 'veg_category_evidence', version: 'veg-admission-v2' } },
    { ...incoming, items: [item('Z', 'wrong')].map(row => ({ ...row, scope: { admitted: false, version } })) },
    { ...incoming, query: { ...incoming.query, keywords: 'vegetarian' } },
    response('vegan', [], { area_resolution: area, page: 2 }, { page: 2, next_page: 3 }),
    response('vegan', [], { area_resolution: area, page_size: 10 }, { page_size: 10 }),
    { ...incoming, query: { ...incoming.query, page_size: 10 } },
    { ...incoming, items: null },
  ];
  for (const value of invalid) assert.throws(() => assertRestaurantDiscoveryResponse(original, value, 'vegan'), /保留原有餐厅/);
  assert.throws(() => assertRestaurantDiscoveryResponse(original, response('素食', [], { keywords: null, area_resolution: area }), '素食'), /保留原有餐厅/);
});

test('missing and malformed query scope cannot create a supplemental request', () => {
  for (const query of [{ mode: null }, { city: '' }, { city: 'x'.repeat(41) }, { coordinate_system: 'WGS-84' }, { location: '116,39' }, { radius_m: 3000 },
    { mode: 'around', city: null, location: '116,39' }, { mode: 'around', city: null, location: '181,39', radius_m: 10000 },
    { mode: 'around', city: null, location: '116,39', radius_m: 50001 }]) {
    assert.equal(restaurantDiscoveryQuery(response('素食', [], query), 'vegan'), null);
  }
  assert.equal(restaurantDiscoveryQuery(response('素食', [], { page_size: 26 }, { page_size: 26 }), 'vegan'), null);
});

test('cross-channel merge deduplicates trimmed IDs but retains unidentified rows and latest active fields', () => {
  const first = response('素食', [item('A', '旧名称'), item(null, '无 id 一'), item('  B  ', 'B')]);
  const second = response('蔬食', [item(' A ', '新名称'), item(null, '无 id 二'), item('', '空白 id'), item(123, '数字 id'), item('123', '字符 id')]);
  const merged = mergeRestaurantDiscoveryChannels([first, second], second);
  assert.deepEqual(merged.items.map(row => [row.id, row.name]), [
    ['A', '新名称'], [null, '无 id 一'], ['B', 'B'], [null, '无 id 二'], [null, '空白 id'], [null, '数字 id'], ['123', '字符 id'],
  ]);
  assert.equal(merged.query, second.query);
  assert.equal(merged.pagination, second.pagination);
  assert.equal(first.items[0].name, '旧名称');
  assert.equal(second.items[0].id, ' A ');
});

test('replacing a cumulative channel after paging does not repeat its existing unknown-ID rows', () => {
  const first = response('素食', [item('A'), item(null, '未知一')]);
  const second = response('蔬食', [item('A'), item(null, '未知二')]);
  const cumulative = response('蔬食', [...second.items, item('C'), item(null, '未知三')], { page: 2 }, { page: 2, has_more: false, next_page: null });
  const merged = mergeRestaurantDiscoveryChannels([first, second], cumulative);
  assert.deepEqual(merged.items.map(row => row.name), ['A', '未知一', '未知二', 'C', '未知三']);
  assert.equal(merged.pagination.has_more, false);
  assert.equal(merged.pagination.page, 2);
  const again = mergeRestaurantDiscoveryChannels([first, cumulative], cumulative);
  assert.deepEqual(again, merged);
});

test('a channel revisit keeps ordering stable and fresh active data wins', () => {
  const first = response('素食', [item('A', 'first'), item('B')]);
  const second = response('蔬食', [item('A', 'second'), item('C')]);
  const refreshed = response('素食', [item('A', 'refreshed'), item('B')]);
  const merged = mergeRestaurantDiscoveryChannels([first, second], refreshed);
  assert.deepEqual(merged.items.map(row => row.id), ['A', 'B', 'C']);
  assert.equal(merged.items[0].name, 'refreshed');
});

test('drifted channels throw before altering the original result or combining provider totals', () => {
  const first = response('素食', [item('A')]);
  first.pagination.matched_count = 300;
  const second = response('蔬食', [item('B')]);
  second.pagination.matched_count = 500;
  const merged = mergeRestaurantDiscoveryChannels([first], second);
  assert.equal(merged.items.length, 2);
  assert.equal(merged.pagination.matched_count, 500);
  assert.equal(first.items.length, 1);
  assert.throws(() => mergeRestaurantDiscoveryChannels([first], response('蔬食', [item('B')], { city: '上海' })), /保留原有餐厅/);
});
