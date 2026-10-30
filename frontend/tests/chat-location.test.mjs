import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from 'typescript';

const source = readFileSync(new URL('../lib/chat-location.ts', import.meta.url), 'utf8');
const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } });
const { needsRestaurantLocation, withCurrentLocation, messageLocation, visibleChatText, regionalRestaurantRequest } = await import('data:text/javascript;base64,' + Buffer.from(outputText).toString('base64'));

test('explicit nearby dining requests obtain a position before their single submission', () => {
  for (const text of ['找一找我周围的餐厅', '想找附近的素食餐厅', '附近有什么可以吃的', '我附近有没有素食馆', '帮我查周边素食', '用地图看看附近餐厅', '离我最近的素食餐厅', '周围餐厅有哪些', '看看这里附近的素食馆', '这包能吃吗？也找我附近的餐厅']) {
    assert.equal(needsRestaurantLocation(text), true, text);
  }
  assert.equal(needsRestaurantLocation('那就查周围的', true), true);
  assert.equal(needsRestaurantLocation('那就查周围的', false), false);
  assert.equal(needsRestaurantLocation('这包怎么判断？再找我附近的素食餐厅'), true);
});

test('named regions, non-dining questions, denials and explanations do not acquire a position', () => {
  for (const text of ['查北京朝阳附近的素食餐厅', '三里屯周边的素食馆', '帮我找上海静安的餐厅', '附近天气怎么样', '查一下附近超市', '记录食材豆腐', '附近餐厅怎么查', '不要获取我的位置，查北京的餐厅', '不用定位，找附近餐厅', '别找我附近的餐厅', '取消查附近餐厅', '我家附近有哪些餐厅', '看刚才的餐厅地图']) {
    assert.equal(needsRestaurantLocation(text), false, text);
  }
});

test('the current coordinate is attached once with the exact existing backend marker and full request', () => {
  const original = '帮我查附近的素食餐厅，并在地图上显示。也判断一下这张配料表。';
  const full = withCurrentLocation(original, '116.4,39.9');
  assert.equal(messageLocation(full), '116.4,39.9');
  assert.equal(needsRestaurantLocation(full), false);
  assert.equal(visibleChatText(full), original);
  assert.equal(withCurrentLocation(full, '116.5,39.8'), `我的本次位置是 116.5,39.8（GCJ-02）。${original}`);
  assert.equal(messageLocation('随便一个坐标116.4,39.9'), null);
  assert.equal(visibleChatText('查北京的餐厅'), '查北京的餐厅');
});

test('invalid position and oversized combined content do not silently truncate or send', () => {
  for (const coordinate of ['NaN,39', '181,39', '116,91', '116,39 extra']) assert.throws(() => withCurrentLocation('查附近餐厅', coordinate), /位置无效/);
  assert.throws(() => withCurrentLocation('字'.repeat(3999), '116.4,39.9'), /太长/);
});

test('after location failure a short area answer preserves restaurant intent and prior preferences', () => {
  const original = '再找我周围的素食餐厅，想要便宜一点的';
  const content = regionalRestaurantRequest('那就北京朝阳吧', original);
  assert.match(content, /^请查找北京朝阳的素食餐厅/);
  assert.ok(content.includes(original));
  assert.equal(needsRestaurantLocation(content), false);
  assert.equal(messageLocation(content), null);
  for (const text of ['推荐一道豆腐菜谱', '帮我记录食材', '不查了', '算了', '找我附近的餐厅', '用地图显示北京的餐厅']) assert.equal(regionalRestaurantRequest(text, original), text);
});
