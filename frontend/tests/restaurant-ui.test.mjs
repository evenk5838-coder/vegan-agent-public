import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, existsSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import ts from 'typescript';

// Render the actual client components without effects, browser permissions,
// a business session, HTTP requests or a simulated provider success.
const require = createRequire(import.meta.url);
const root = fileURLToPath(new URL('../', import.meta.url));
const cache = new Map();
function load(file) {
  const path = resolve(root, file);
  if (cache.has(path)) return cache.get(path).exports;
  const module = { exports: {} }; cache.set(path, module);
  const { outputText } = ts.transpileModule(readFileSync(path, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true },
  });
  const localRequire = name => {
    if (name.endsWith('.module.css')) return { __esModule: true, default: new Proxy({}, { get: (_target, key) => String(key) }) };
    if (!name.startsWith('.')) return require(name);
    const base = resolve(dirname(path), name);
    const target = [base, base + '.ts', base + '.tsx'].find(candidate => existsSync(candidate));
    assert.ok(target, `Missing test import ${name}`);
    return load(target);
  };
  new Function('require', 'module', 'exports', outputText)(localRequire, module, module.exports);
  return module.exports;
}
const RestaurantResults = load('components/RestaurantResults.tsx').default;
const Restaurants = load('components/Restaurants.tsx').default;
const scope = { rule: 'veg_category_evidence', version: 'veg-admission-v3' };
const page = (changes = {}) => ({
  query: { mode: 'text', city: '北京朝阳区', keywords: '素食', coordinate_system: 'GCJ-02' }, scope,
  pagination: { page: 1, page_size: 20, has_more: true, next_page: 2 },
  items: Array.from({ length: 5 }, (_, index) => ({ id: String(index), name: `测试蔬食餐厅${index + 1}`,
    address: '测试路1号', location: '116.4,39.9', scope: { admitted: true, version: scope.version } })),
  ...changes,
});
const render = (payload, props = {}) => renderToStaticMarkup(React.createElement(RestaurantResults, { payload, ...props }));

test('dedicated results show the first ten while map and pagination stay discoverable before the list', () => {
  const payload = page({ items: Array.from({ length: 12 }, (_, index) => ({ id: String(index), name: `测试蔬食餐厅${index + 1}`,
    scope: { admitted: true, version: scope.version } })) });
  const html = render(payload);
  assert.ok(html.indexOf('加载页内地图') < html.indexOf('测试蔬食餐厅1'));
  assert.ok(html.indexOf('加载更多') < html.indexOf('测试蔬食餐厅1'));
  assert.ok(html.includes('测试蔬食餐厅10'));
  assert.equal(html.includes('测试蔬食餐厅11'), false);
  assert.ok(html.includes('展开已加载结果（还有 2 家）'));
  assert.ok(html.includes('北京朝阳区区域'));
  assert.equal(html.includes('素食区域'), false);
});

test('compact chat cards retain their initial three-item summary without enabling automatic fill', () => {
  const html = render(page(), { compact: true });
  assert.ok(html.includes('测试蔬食餐厅3'));
  assert.equal(html.includes('测试蔬食餐厅4'), false);
  assert.ok(html.includes('展开已加载结果（还有 2 家）'));
});

test('a current empty or coordinate-free page explains its map state and can keep paging', () => {
  for (const items of [[], [{ id: 'no-point', name: '无坐标蔬食餐厅', scope: { admitted: true, version: scope.version } }]]) {
    const html = render(page({ items }));
    assert.match(html, /<button[^>]*disabled=""[^>]*>[\s\S]*?暂无可用地图坐标<\/button>/);
    assert.ok(html.includes('可继续加载列表'));
    assert.ok(html.includes('加载更多'));
  }
});

test('regional searches never imply a user distance and explain how to request one', () => {
  const html = render(page());
  assert.ok(html.includes('区域查询未使用设备位置'));
  assert.ok(html.includes('地图中心不代表你的位置'));
  assert.equal(html.includes('你的位置（本次定位）'), false);
  assert.equal(html.includes('直线距离 400 米'), false);
});

test('a superseded result keeps its successful list but disables further old-intent requests', () => {
  const controller = new AbortController(); controller.abort();
  const html = render(page(), { suspendSignal: controller.signal });
  assert.ok(html.includes('测试蔬食餐厅1'));
  assert.ok(html.includes('测试蔬食餐厅5'));
  assert.match(html, /<button[^>]*disabled=""[^>]*>[\s\S]*?加载页内地图<\/button>/);
  assert.match(html, /<button[^>]*disabled=""[^>]*>[\s\S]*?加载更多<\/button>/);
});

test('nearby results show the actual one-off origin and returned straight-line distance', () => {
  const payload = page({ query: { mode: 'around', location: '116.4,39.9', coordinate_system: 'GCJ-02' },
    items: [{ id: 'nearby', name: '附近蔬食餐厅', location: '116.41,39.91', distance_m: 400,
      distance_type: 'straight_line', scope: { admitted: true, version: scope.version } }] });
  const html = render(payload, { currentLocation: '116.4,39.9' });
  assert.ok(html.includes('你的位置（本次定位）'));
  assert.ok(html.includes('距你（直线） 400 米'));
  assert.ok(html.includes('不是步行里程'));
});

test('regional results offer device distance without replacing the region search', () => {
  const html = render(page());
  assert.ok(html.includes('显示距我距离'));
  assert.ok(html.includes('不代表该地区全部餐厅'));
  assert.ok(html.includes('<details><summary>查询范围与来源</summary>'));
});

test('legacy results remain visibly historical and cannot load an unsafe old map', () => {
  const html = render(page({ scope: { ...scope, version: 'veg-admission-v2' } }));
  assert.ok(html.includes('旧规则餐厅记录'));
  assert.ok(html.includes('按当前规则重新查询'));
  assert.equal(html.includes('加载页内地图'), false);
});

test('the restaurant page exposes separate regional and nearby controls without auto-requests', () => {
  const previousFetch = globalThis.fetch;
  globalThis.fetch = () => assert.fail('rendering the restaurant page cannot request a supplier or session');
  try {
    const html = renderToStaticMarkup(React.createElement(Restaurants));
    assert.ok(html.includes('按城市或区县查询'));
    assert.ok(html.includes('查询该区域'));
    assert.ok(html.includes('获取位置，查附近'));
    assert.ok(html.includes('附近查询范围'));
    assert.match(html, /<option value="10000" selected="">10 公里（默认）<\/option>/);
    for (const radius of [3000, 5000, 20000, 50000]) assert.ok(html.includes(`<option value="${radius}">`));
  } finally { globalThis.fetch = previousFetch; }
});

test('menu categories appear in feature results with separate statuses and collapsed evidence', () => {
  const payload = page({ items: [{ id: 'menu', name: '菜单测试素食馆', scope: { admitted: true, version: scope.version }, diet_labels: {
    version: 'restaurant-diets-2026-10-30.1', kind: 'menu_availability', labels: {
      vegan: { status: 'on_request', basis: '需要提前安排菜单', sources: [{ title: '本店菜单资料', source: 'https://example.org/menu', reviewed_at: '2000-01-01', expires_at: '2099-01-01' }] },
      lacto_ovo: { status: 'supported', basis: '菜单含可用选项', sources: [{ title: '本店菜单资料', source: 'https://example.org/menu', reviewed_at: '2000-01-01', expires_at: '2099-01-01' }] },
    },
  } }] });
  const html = render(payload);
  assert.ok(html.includes('需提前说明'));
  assert.ok(html.includes('有菜单依据'));
  assert.ok(html.includes('纯净素'));
  assert.ok(html.includes('待确认'));
  assert.ok(html.includes('查看分类依据'));
  assert.equal(html.includes('<details open'), false);
  assert.ok(html.includes('换个关键词，补充找餐厅'));
  assert.ok(html.includes('搜索关键词'));
  assert.ok(html.includes('每次补充只查一页'));
  const compact = render(payload, { compact: true });
  assert.equal(compact.includes('查看分类依据'), false);
  assert.equal(compact.includes('换个关键词，补充找餐厅'), false);
});
