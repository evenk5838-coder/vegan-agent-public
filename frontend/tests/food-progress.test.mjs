import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from 'typescript';
const source=readFileSync(new URL('../lib/food-progress.ts',import.meta.url),'utf8');
const {outputText}=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.ESNext,target:ts.ScriptTarget.ES2022}});
const {foodProgress}=await import('data:text/javascript;base64,'+Buffer.from(outputText).toString('base64'));
const now=Date.parse('2026-10-23T12:00:00+08:00');
const item={status:'active',assessment_status:'estimated',display_status:'stored',valid_started_at:'2026-10-22T12:00:00+08:00',valid_deadline_at:'2026-10-24T12:00:00+08:00',deadline_basis:{type:'package'}};

test('condition changes suppress the old deadline and progress',()=>{
  const result=foodProgress({...item,assessment_status:'needs_review'},now);
  assert.equal(result.deadline,null);assert.equal(result.percent,null);assert.match(result.label,/待核对/);
});
test('unreviewed rule entries cannot look like formal expiry estimates',()=>{
  const result=foodProgress({...item,deadline_basis:{type:'rule',rules_status:'draft'}},now);
  assert.equal(result.deadline,null);assert.equal(result.percent,null);assert.equal(result.draftRule,true);
});
test('user dates show remaining time without inventing a total lifetime',()=>{
  const result=foodProgress({...item,assessment_status:'user_set',deadline_basis:{type:'user'}},now);
  assert.equal(result.percent,null);assert.match(result.label,/距自设日期/);
});
test('deadline without a valid start still shows remaining time',()=>{
  const result=foodProgress({...item,valid_started_at:null},now);
  assert.equal(result.percent,null);assert.match(result.label,/24 小时/);
});
test('progress clamps to its actual interval',()=>{
  assert.equal(foodProgress(item,now).percent,50);
  assert.equal(foodProgress(item,now+10*86400000).percent,100);
  assert.equal(foodProgress(item,now-10*86400000).percent,0);
});
test('short deadlines retain an hourly warning instead of rounding to days',()=>{
  const result=foodProgress({...item,valid_deadline_at:new Date(now+20*60000).toISOString()},now);
  assert.match(result.label,/不足 1 小时/);
});
test('an unknown assessment is never treated as an established deadline',()=>{
  const result=foodProgress({...item,assessment_status:'unknown'},now);
  assert.equal(result.deadline,null);assert.equal(result.percent,null);
});
test('qualitative use priority cannot create an expiry or progress percentage',()=>{
  const result=foodProgress({...item,assessment_status:'unknown',valid_started_at:null,valid_deadline_at:null,
    suggestion:{usage:{priority:'use_soon',title:'优先安排使用',text:'尽快使用不表示食用安全。'}}},now);
  assert.equal(result.deadline,null);assert.equal(result.percent,null);
});
test('registration time is not a purchase date or an estimated lifetime start',()=>{
  const result=foodProgress({...item,storage:'unknown',started_at:null,created:'2026-10-23T10:00:00+08:00',valid_started_at:null,valid_deadline_at:null,assessment_status:'needs_review'},now);
  assert.equal(result.deadline,null);assert.equal(result.percent,null);assert.match(result.label,/信息待补充/);
});
