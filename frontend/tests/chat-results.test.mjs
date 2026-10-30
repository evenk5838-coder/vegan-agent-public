import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, existsSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import ts from 'typescript';

// Load the real TS helpers and render read-only cards without a browser or business service.
const require = createRequire(import.meta.url);
const root = fileURLToPath(new URL('../', import.meta.url));
const cache = new Map();
function load(file) {
  const path = resolve(root, file);
  if (cache.has(path)) return cache.get(path).exports;
  const module = { exports: {} };
  cache.set(path, module);
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
const actions = load('lib/chat-actions.ts');
const read = load('lib/chat-read.ts');
const { ApiError } = load('lib/api.ts');
const ChatReadResult = load('components/ChatReadResult.tsx').default;
const ChatAction = load('components/ChatAction.tsx').default;
const Citations = load('components/Citations.tsx').default;
const RecipeDetail = load('components/RecipeDetail.tsx').default;
const { recipeView } = load('lib/recipe-display.ts');
const { RestaurantGuidance, historicalRestaurantReply } = load('components/Chat.tsx');
const ChatResults = load('components/ChatResults.tsx').default;
const ChatFeatureDetail = load('components/ChatFeatureDetail.tsx').default;
const { featureResults, requestedRestaurantMap } = load('lib/chat-presentation.ts');
const now = Date.parse('2026-10-26T00:00:00Z');
const action = (changes = {}) => actions.pendingAction({ record_id: 'record-1', action_id: 'display-id', kind: 'food_action', status: 'pending',
  needs_confirmation: true, expires_at: '2026-10-26T00:30:00Z', target: { food_id: 'food-1', name: '豆腐' }, proposed: { action: 'food_status', status: 'used' }, ...changes });

test('chat groups food results into one destination and keeps evidence out of the message', () => {
  const cards = [
    { id: 'inventory', type: 'food_inventory', payload: { items: [{ id: 'f', name: '豆腐', status: 'active', package_note: '仅在详情出现的包装依据' }] } },
    { id: 'advice', type: 'food_storage_advice', payload: { food_id: 'f', guidance: { summary: '仅在详情出现的长建议' } } },
  ];
  const citations = [{ entry_id: 'source', title: '仅在详情出现的来源', source: 'https://example.org/source' }];
  const before = JSON.stringify(cards), groups = featureResults(cards, citations);
  assert.equal(groups.length, 1); assert.equal(groups[0].page, 'foods');
  assert.deepEqual(groups[0].cards.map(card => card.type), ['food_inventory', 'food_storage_advice', 'citations']);
  assert.equal(JSON.stringify(cards), before);
  const html = renderToStaticMarkup(React.createElement(ChatResults, { cards, citations, onOpen() {} }));
  assert.equal((html.match(/查看食材详情/g) || []).length, 1);
  assert.doesNotMatch(html, /<details|<form|<progress|记录与依据|仅在详情出现|参考资料|联网查建议/);
  const detail = renderToStaticMarkup(React.createElement(ChatFeatureDetail, { result: groups[0], onNavigate() {}, onClose() {} }));
  assert.match(detail, /记录与依据/); assert.match(detail, /仅在详情出现的来源/);
});

test('opening details retains full results, location and failed state without dispatching an operation', () => {
  const cards = [{ id: 'r', type: 'restaurant_list', payload: { items: [{ id: 'p', name: '实际店名' }] } }];
  let opened;
  const tree = ChatResults({ cards, failed: true, currentLocation: '116.4,39.9', onOpen: result => { opened = result; } });
  const links = tree.props.children.at(-1);
  const button = links.props.children[0];
  assert.equal(button.type, 'button'); assert.equal(button.props.type, 'button');
  assert.equal(opened, undefined); button.props.onClick();
  assert.equal(opened.page, 'restaurants'); assert.equal(opened.failed, true); assert.equal(opened.currentLocation, '116.4,39.9');
  assert.equal(opened.cards[0], cards[0]);
  const html = renderToStaticMarkup(tree);
  assert.match(html, /查看餐厅与地图/); assert.doesNotMatch(html, /实际店名|加载页内地图/);
});

test('all four feature paths retain their detail destination and chat confirmation stays lightweight', () => {
  const cards = ['ingredient_result', 'restaurant_list', 'recipe_list', 'food_draft'].map((type, i) => ({ id: String(i), type, payload: type==='food_draft'?{draft_id:'draft'}:{} }));
  assert.deepEqual(featureResults(cards).map(group => group.page), ['ingredients', 'restaurants', 'recipes', 'foods']);
  const html = renderToStaticMarkup(React.createElement(ChatResults, { cards, onOpen() {} }));
  assert.match(html, /食材保存确认/); assert.match(html, /确认存入冰箱/);
  assert.doesNotMatch(html, /补充或修改信息|储存建议与依据|<form|<fieldset/);
  assert.doesNotMatch(html, /查看食材详情/, 'the confirmation already has its own detail entry');
  const failed = renderToStaticMarkup(React.createElement(ChatResults, { cards, failed:true, onOpen() {} }));
  assert.doesNotMatch(failed, /食材保存确认|确认保存/);
});

test('persistent confirmation uses record_id, never the display action_id', () => {
  assert.equal(actions.actionFromPayload({ pending_action: { action_id: 'only-display' } }), null);
  const current = action();
  assert.equal(actions.actionFromPayload({ pending_action: current }).record_id, 'record-1');
  assert.equal(actions.actionPath('record/a b'), '/actions/record%2Fa%20b');
  assert.throws(() => actions.requireAction({ ...current, record_id: 'other' }, current.record_id), /不一致/);
  assert.deepEqual(actions.actionConfirmationBody(current, { recordId: 'record-1', key: 'same-attempt' }, now), { confirm_key: 'user_confirmed', idempotency_key: 'same-attempt' });
  assert.throws(() => actions.actionConfirmationBody(current, { recordId: 'display-id', key: 'same-attempt' }, now));
});

test('closed, expired, malformed, and unconfirmed actions cannot generate a confirmation body', () => {
  for (const current of [action({ status: 'confirmed' }), action({ status: 'cancelled' }), action({ needs_confirmation: false }),
    action({ expires_at: '2026-10-25T00:00:00Z' }), action({ expires_at: 'bad-date' }),
    action({ proposed: { action: 'food_status', status: 'delete' } }), action({ proposed: { action: 'food_condition' } }), action({ target: {} })]) {
    assert.equal(actions.canConfirmAction(current, now), false);
    assert.throws(() => actions.actionConfirmationBody(current, { recordId: current.record_id, key: 'key' }, now));
  }
  assert.equal(actions.canConfirmAction(action(), now), true);
});

test('the full effective reminder patch is visible, including zero temperature and false opened state', () => {
  const current = action({ proposed: { action: 'food_reminder', opened: false, temperature_c: 0, storage: 'refrigerated', remind_at: null } });
  assert.deepEqual(actions.actionProposalLines(current), [
    { label: '提醒时间', value: '取消提醒' }, { label: '存放方式', value: '冷藏' }, { label: '开封状态', value: '未开封' }, { label: '温度', value: '0 ℃' },
  ]);
  assert.equal(actions.canConfirmAction(current, now), true);
});

test('stale and expired conflicts close the original confirmation; an uncertain result requires status recovery', () => {
  assert.equal(actions.actionClosure(new ApiError('ACTION_STALE', 'changed', 409)), 'stale');
  assert.equal(actions.actionClosure(new ApiError('ACTION_EXPIRED', 'expired', 409)), 'expired');
  assert.equal(actions.actionClosure(new ApiError('ACTION_CLOSED', 'closed', 409)), 'closed');
  assert.equal(actions.actionClosure(new ApiError('NOT_FOUND', 'missing', 404)), 'missing');
  assert.equal(actions.actionClosure(new ApiError('INVALID_ARGUMENTS', 'invalid', 422)), null);
  assert.equal(actions.actionResultUncertain(new ApiError('CONNECTION_LOST', 'lost', 0)), true);
  assert.equal(actions.actionResultUncertain(new ApiError('INVALID_RESPONSE', 'unreadable', 200)), true);
  assert.equal(actions.actionResultUncertain(new ApiError('ACTION_STALE', 'changed', 409)), false);
});

test('context confirmation shows the actual date and notes before acknowledging their fields', () => {
  const current = action({ required_ack_fields: ['started_at', 'date_type', 'storage_note', 'package_note'], proposed: {
    action: 'food_condition', started_at: '2026-10-25T16:00:00Z', date_type: 'purchase', storage_note: '买后密封冷藏', package_note: '没有包装日期',
  } });
  const lines = actions.actionProposalLines(current);
  assert.deepEqual(lines, [{ label: '购买日期', value: '2026/10/26' }, { label: '存放情况', value: '买后密封冷藏' }, { label: '包装说明', value: '没有包装日期' }]);
  assert.deepEqual(actions.actionConfirmationBody(current, { recordId: current.record_id, key: 'context' }, now).acknowledged_fields,
    ['started_at', 'date_type', 'storage_note', 'package_note']);
  assert.match(actions.actionContextDate('2026-10-26T08:30:00+08:00'), /08:30/);
  assert.equal(actions.canConfirmAction(action({ required_ack_fields: ['date_type'], proposed: { action: 'food_condition', date_type: 'prepared' } }), now), true);
});

test('hidden unknown context or malformed dates never receive an acknowledgement', () => {
  for (const patch of [
    { required_ack_fields: ['future_context'] }, { required_ack_fields: ['started_at'] },
    { required_ack_fields: 'started_at' }, { required_ack_fields: [null] },
    ...['bad', '2026-10-26', '2026-10-26T08:00:00', '2026-02-30T08:00:00+08:00'].map(started_at => ({ proposed: { action: 'food_condition', storage: 'refrigerated', started_at } })),
    { proposed: { action: 'food_status', status: 'used', date_type: 'purchase' } },
    { proposed: { action: 'food_condition', date_type: 'constructor' } },
    { proposed: { action: 'food_condition', package_note: '' } },
  ]) {
    const current = action(patch);
    assert.equal(actions.canConfirmAction(current, now), false, JSON.stringify(patch));
    assert.throws(() => actions.actionConfirmationBody(current, { recordId: current.record_id, key: 'unsafe' }, now));
  }
});

test('citations preserve source prose and deduplicate entry IDs, allowing only absolute HTTP(S) links', () => {
  const rows = [{ entry_id: 'a', title: '公开资料', source: 'https://example.org/source' },
    { entry_id: 'a', title: '重复', source: 'https://example.org/other' },
    { entry_id: 'b', title: '不安全来源', source: 'javascript:alert(1)' }, { title: '缺失 ID', source: 'https://example.org' }];
  const views = read.citationViews(rows);
  assert.equal(views.length, 2);
  assert.equal(views[0].href, 'https://example.org/source');
  assert.equal(views[1].href, undefined);
  const html = renderToStaticMarkup(React.createElement(Citations, { items: rows }));
  assert.match(html, /href="https:\/\/example.org\/source"/);
  assert.doesNotMatch(html, /href="javascript:/);
  assert.match(html, /noopener noreferrer/);
  assert.match(html, /javascript:alert\(1\)/);
});

test('knowledge cards show actual conditions and retain unsafe source text without creating a link', () => {
  const html = renderToStaticMarkup(React.createElement(ChatReadResult, { type: 'knowledge_result', payload: { items: [{ entry_id: 'k', title: '冷藏参考', type: 'storage', source: 'data:text/html,unsafe', excerpt: '适用条件下的参考资料', conditions: { storage: 'refrigerated', opened: false, temperature_max_c: 4 } }] } }));
  assert.match(html, /冷藏/);
  assert.match(html, /开封状态：否/);
  assert.match(html, /最高温度：4 ℃/);
  assert.match(html, /data:text\/html,unsafe/);
  assert.doesNotMatch(html, /href="data:/);
});

test('inventory never presents an unpublished rule as a formal deadline or progress', () => {
  const html = renderToStaticMarkup(React.createElement(ChatReadResult, { type: 'food_inventory', payload: { items: [{ id: 'f', name: '豆腐', status: 'active', storage: 'refrigerated', condition: 'raw', assessment_status: 'estimated', valid_started_at: '2026-10-25T00:00:00Z', valid_deadline_at: '2026-10-30T00:00:00Z', deadline_basis: { type: 'rule', rules_status: 'draft' } }] } }));
  assert.match(html, /尚未发布/);
  assert.doesNotMatch(html, /<progress|适用截止日期|预计剩余/);
});

test('reminder status does not equate service acceptance with delivery to the device', () => {
  const html = renderToStaticMarkup(React.createElement(ChatReadResult, { type: 'reminder_status', payload: { items: [{ task_id: 't', status: 'accepted', food_name: '豆腐', due: '2026-10-27T10:00:00+08:00' }] } }));
  assert.match(html, /推送服务已接受/);
  assert.match(html, /不代表设备已送达/);
});

test('restoring an action card never renders a usable confirm button before its GET recovery', () => {
  const html = renderToStaticMarkup(React.createElement(ChatAction, { payload: { pending_action: action() } }));
  assert.match(html, /读取最新状态/);
  assert.doesNotMatch(html, /确认修改<\/button>/);
  assert.doesNotMatch(html, /豆腐/, 'the stale chat payload is not presented as the recovered action');
});

test('all six restaurant states guide without dispatching a query or reproducing place names', () => {
  for (const status of ['ok', 'missing_area', 'not_queried', 'empty', 'failed', 'not_restaurant']) {
    let continued = 0;
    const onContinue = () => { continued++; };
    const view = RestaurantGuidance({ guard: { status, allowed_place_names: ['不可从守卫名单拼接的店名'] }, onContinue });
    const html = renderToStaticMarkup(view);
    assert.doesNotMatch(html, /不可从守卫名单拼接的店名/);
    assert.equal(continued, 0, 'rendering cannot dispatch a continuation');
    if (status === 'not_restaurant') { assert.equal(view, null); continue; }
    if (status === 'ok') { assert.doesNotMatch(html, /<button/); continue; }
    const button = view.props.children.find(child => child?.type === 'button');
    assert.ok(button);
    assert.equal(button.props.type, 'button', 'guidance cannot submit the composer form');
    assert.equal(button.props.onClick, onContinue, 'the button only invokes the supplied focus callback');
    button.props.onClick();
    assert.equal(continued, 1);
    if (status === 'missing_area') assert.match(html, /补充城市或区县/);
  }
});

test('old restaurant text stays historical unless server guidance and current scope are both present', () => {
  const current = { type: 'restaurant_list', payload: { items: [], scope: { rule: 'veg_category_evidence', version: 'veg-admission-v3' } } };
  assert.equal(historicalRestaurantReply({ cards: [current], restaurant_guard: null }), true);
  assert.equal(historicalRestaurantReply({ cards: [current], restaurant_guard: { restaurant_turn: true, recommendation_source: 'model' } }), true);
  assert.equal(historicalRestaurantReply({ cards: [current], restaurant_guard: { restaurant_turn: true, recommendation_source: 'server' } }), false);
  assert.equal(historicalRestaurantReply({ cards: [{ ...current, payload: {} }], restaurant_guard: { restaurant_turn: true, recommendation_source: 'server' } }), true);
  assert.equal(historicalRestaurantReply({ cards: [{ ...current, payload: { ...current.payload, scope: { rule: 'veg_category_evidence', version: 'veg-admission-v2' } } }], restaurant_guard: { restaurant_turn: true, recommendation_source: 'server' } }), true);
  assert.equal(historicalRestaurantReply({ cards: [], restaurant_guard: { status: 'not_restaurant', recommendation_source: 'model' } }), false);
});

test('a declared personal original shows real recorded quantity without claiming review or dietary suitability', () => {
  const recipe = recipeView({ id: 'own', title: '自己的汤', origin: 'user_original', ingredients: '豆腐200克', steps: '煮熟', yield_amount: 2, yield_unit: '人份' });
  const html = renderToStaticMarkup(React.createElement(RecipeDetail, { recipe, diet: 'vegan' }));
  assert.match(html, /2 人份/);
  assert.match(html, /个人原创（用户声明）/);
  assert.match(html, /不代表内容已审核或饮食适配已经确认/);
  assert.match(html, /Vegan：待核实/);
  assert.doesNotMatch(html, /href=/);
});

test('public recipe attribution remains a real source link even when the input claims original ownership', () => {
  const recipe = recipeView({ entry_id: 'public', scope: 'public', origin: 'user_original', ingredients: ['豆腐'], steps: ['煮熟'], source: 'https://example.org/public', servings: 2 });
  const html = renderToStaticMarkup(React.createElement(RecipeDetail, { recipe }));
  assert.match(html, /公共来源 · 只读/);
  assert.match(html, /href="https:\/\/example.org\/public"/);
  assert.doesNotMatch(html, /个人原创（用户声明）/);
});

test('translated public tutorials retain contributor credit and a safe license link without claiming review', () => {
  for (const url of ['https://creativecommons.org/licenses/by-sa/4.0/', 'javascript:alert(1)']) {
    const recipe = recipeView({ entry_id: 'translated', scope: 'public', title: '中文沙拉',
      ingredients: ['胡萝卜 200克'], steps: ['混合食材。'],
      source: 'https://en.wikibooks.org/wiki/Cookbook:Salad',
      translation: { status: 'complete' }, source_meta: { manual_reviewed: false,
        quality_flags: ['machine_translation_unreviewed'],
        license: { attribution: 'Wikibooks contributors', id: 'CC-BY-SA-4.0', url } } });
    const html = renderToStaticMarkup(React.createElement(RecipeDetail, { recipe }));
    assert.match(html, /Wikibooks contributors/);
    assert.match(html, /CC-BY-SA-4.0/);
    assert.match(html, /中文机器翻译/);
    assert.match(html, /尚未经过人工复核/);
    if (url.startsWith('https')) assert.ok(html.includes(`href="${url}"`));
    else assert.doesNotMatch(html, /href="javascript:/);
  }
});

const restaurantPayload = (changes = {}) => ({
  scope: { rule: 'veg_category_evidence', version: 'veg-admission-v3' },
  query: { mode: 'text', city: '北京', page_size: 20 },
  pagination: { has_more: true, next_page: 2, page: 1, page_size: 20 },
  items: Array.from({ length: 7 }, (_, i) => ({ id: String(i), name: `素食店${i}`, address: `地址${i}`, location: i === 2 ? null : '116.4,39.9', scope: { admitted: true, version: 'veg-admission-v3' } })),
  ...changes,
});
const restaurantCard = (payload = restaurantPayload()) => ({ id: 'restaurant-card', type: 'restaurant_list', status: 'ready', payload });
function buttonsIn(tree) {
  if (!tree || typeof tree !== 'object') return [];
  if (Array.isArray(tree)) return tree.flatMap(buttonsIn);
  return [...(tree.type === 'button' ? [tree] : []), ...buttonsIn(tree.props?.children)];
}
test('chat shows three selectable restaurants, loaded count and further pages without long details', () => {
  const card = restaurantCard(); let opened;
  const tree = ChatResults({ cards: [card], currentLocation: '116.4,39.9', onOpen: result => { opened = result; } });
  const html = renderToStaticMarkup(tree);
  assert.match(html, /素食店0/); assert.match(html, /素食店2/); assert.doesNotMatch(html, /素食店3/);
  assert.match(html, /已找到 7 家/); assert.match(html, /还有更多可查/); assert.match(html, /查看全部 7 家/);
  assert.doesNotMatch(html, /查询范围与来源|加载页内地图|<form|<details/);
  const buttons = buttonsIn(tree);
  buttons.find(b => b.props['aria-label'] === '查看素食店0的地图位置').props.onClick();
  assert.equal(opened.cards[0], card);
  assert.deepEqual(opened.restaurantView, { cardId: card.id, view: 'map', selectedKey: 'poi-0' });
  assert.equal(opened.currentLocation, '116.4,39.9');
  buttons.find(b => b.props['aria-label'] === '查看素食店2的详情').props.onClick();
  assert.equal(opened.restaurantView.view, 'list', 'missing coordinates still opens details');
});
test('only new successful map replies navigate; history, failed replies and stale scopes stay inert', () => {
  const card = restaurantCard(restaurantPayload({ presentation: { preferred_view: 'map' } }));
  const message = { role: 'assistant', status: 'succeeded', turn_id: 'turn-1', cards: [card] };
  const submitted = new Set(['turn-1']);
  assert.deepEqual(requestedRestaurantMap(message, submitted)?.restaurantView, { cardId: card.id, view: 'map' });
  assert.equal(requestedRestaurantMap(message, new Set()), null);
  for (const status of ['running', 'failed', 'cancelled']) assert.equal(requestedRestaurantMap({ ...message, status }, submitted), null);
  assert.equal(requestedRestaurantMap({ ...message, cards: [restaurantCard(restaurantPayload())] }, submitted), null);
  assert.equal(requestedRestaurantMap({ ...message, cards: [restaurantCard({ ...card.payload, scope: { version: 'old' } })] }, submitted), null);
  assert.equal(requestedRestaurantMap({ ...message, cards: [restaurantCard({ ...card.payload, items: [] })] }, submitted), null);
  submitted.delete('turn-1');
  assert.equal(requestedRestaurantMap(message, submitted), null, 'repeated polling must not reopen map');
});
test('empty current restaurant results keep a detail path but do not render phantom choices', () => {
  const html = renderToStaticMarkup(React.createElement(ChatResults, { cards: [restaurantCard(restaurantPayload({ items: [] }))], onOpen() {} }));
  assert.match(html, /查看餐厅与地图/); assert.doesNotMatch(html, /对话推荐餐厅|看地图|已找到 0/);
});
