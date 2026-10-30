import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import ts from 'typescript';

const require = createRequire(import.meta.url);
function load(path, dependencies = {}) {
  const source = readFileSync(new URL(path, import.meta.url), 'utf8');
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true },
  });
  const module = { exports: {} };
  new Function('require', 'module', 'exports', outputText)(name => {
    if (name.endsWith('.module.css')) return { __esModule: true, default: new Proxy({}, { get: (_target, key) => String(key) }) };
    return dependencies[name] || require(name);
  }, module, module.exports);
  return module.exports;
}
const diets = load('../lib/restaurant-diets.ts');
const { restaurantDietLabels: parse, restaurantMatchesDiet: matches } = diets;
const DietLabels = load('../components/RestaurantDietLabels.tsx', { '../lib/restaurant-diets': diets }).default;
const now = new Date('2026-10-30T04:00:00Z');
const source = (changes = {}) => ({ title: '本店菜单资料', source: 'https://example.com/restaurant/menu',
  reviewed_at: '2026-10-30', expires_at: '2026-11-29', ...changes });
const claim = (changes = {}) => ({ status: 'supported', basis: '菜单载明使用植物性食材。', sources: [source()], ...changes });
const metadata = (labels = {}) => ({ kind: 'menu_availability', version: diets.RESTAURANT_DIET_VERSION, labels });

test('three classes stay independent: Vegan is never automatically pure or lacto-ovo', () => {
  const data = parse(metadata({ vegan: claim() }), now);
  assert.deepEqual(data.labels.map(label => [label.key, label.status]), [['pure', 'unknown'], ['vegan', 'supported'], ['lacto_ovo', 'unknown']]);
  assert.equal(data.hasEvidence, true);
  assert.equal(matches(metadata({ vegan: claim() }), 'vegan', now), true);
  assert.equal(matches(metadata({ vegan: claim() }), 'pure', now), false);
});

test('on-request stays visibly distinct and can be filtered as a menu option', () => {
  const value = metadata({ vegan: claim({ status: 'on_request', basis: '需要在预订时提出调整。' }), lacto_ovo: claim() });
  assert.equal(parse(value, now).labels[1].statusText, '需提前说明');
  assert.equal(parse(value, now).labels[2].statusText, '有菜单依据');
  assert.equal(matches(value, 'vegan', now), true);
  assert.equal(matches(value, 'unknown', now), false);
});

test('unknown or legacy POI names/categories never imply certification or incompatibility', () => {
  for (const value of [undefined, null, {}, [], { name: '纯素餐厅', typecode: '050120' }, { ...metadata({ vegan: claim() }), kind: 'certification' }, { ...metadata({ vegan: claim() }), version: 'future-version' }]) {
    const data = parse(value, now);
    assert.equal(data.hasEvidence, false);
    assert.ok(data.labels.every(label => label.status === 'unknown' && label.statusText === '待确认'));
    assert.equal(matches(value, 'unknown', now), true);
    assert.equal(matches(value, 'all', now), true);
  }
});

test('source review and expiry use Shanghai calendar dates with exclusive expiry', () => {
  const value = metadata({ vegan: claim() });
  assert.equal(parse(value, new Date('2026-10-29T15:59:59Z')).hasEvidence, false);
  assert.equal(parse(value, new Date('2026-10-29T16:00:00Z')).hasEvidence, true);
  assert.equal(parse(value, new Date('2026-11-28T15:59:59Z')).hasEvidence, true);
  assert.equal(parse(value, new Date('2026-11-28T16:00:00Z')).hasEvidence, false);
  assert.equal(parse(value, new Date('invalid')).hasEvidence, false);
});

test('missing, invalid, unsafe or partially expired evidence downgrades the claim to unknown', () => {
  const invalidClaims = [
    claim({ sources: [] }), claim({ sources: null }), claim({ basis: '' }),
    claim({ sources: [source({ reviewed_at: '2026-02-31' })] }),
    claim({ sources: [source({ expires_at: '2026-10-30' })] }),
    claim({ sources: [source({ reviewed_at: '2026-10-31' })] }),
    claim({ sources: [source(), source({ expires_at: '2026-10-29', reviewed_at: '2026-10-01' })] }),
    claim({ sources: [source(), {}] }),
    ...['javascript:alert(1)', 'data:text/html,foo', 'http://example.com/', 'https://user:secret@example.com/', '//example.com/'].map(url => claim({ sources: [source({ source: url })] })),
  ];
  for (const bad of invalidClaims) {
    assert.equal(parse(metadata({ vegan: bad }), now).hasEvidence, false);
    assert.equal(matches(metadata({ vegan: bad }), 'vegan', now), false);
  }
  assert.equal(parse(metadata({ vegan: claim({ status: 'certified' }) }), now).hasEvidence, false);
});

test('compact rendering is inline and contains no sources or nested actions', () => {
  const html = renderToStaticMarkup(React.createElement(DietLabels, { value: metadata(), compact: true }));
  assert.match(html, /^<span /);
  assert.match(html, /饮食分类待确认/);
  assert.doesNotMatch(html, /<(a|button|details|div)\b/);
});

test('feature rendering keeps three statuses and sources collapsed with explicit limits', () => {
  const html = renderToStaticMarkup(React.createElement(DietLabels, { value: metadata({ vegan: claim() }) }));
  assert.match(html, /纯净素/); assert.match(html, /Vegan/); assert.match(html, /蛋奶素/);
  assert.match(html, /<details class="details">/); assert.doesNotMatch(html, /<details[^>]*\bopen/);
  assert.match(html, /查看分类依据/); assert.match(html, /待确认不代表不适合/); assert.match(html, /不是整店认证/);
  assert.match(html, /href="https:\/\/example.com\/restaurant\/menu"/);
  assert.match(html, /rel="noopener noreferrer"/);
  assert.match(html, /核对 2026-10-30/);
});
