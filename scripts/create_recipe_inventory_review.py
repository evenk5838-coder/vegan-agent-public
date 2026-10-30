"""Build a public-source-only, offline review snapshot. No DB writes or APIs.

The optional database snapshot is a separately exported read-only selection of
system knowledge; it must never contain personal records, chats or credentials.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
sys.path.insert(0, str(ROOT))
from app import rules
from scripts.release_translated_recipes import ANIMAL_WORDS

BASE = ROOT / 'docs/知识库/大批量建库-2026-10-26'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def lines(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def key(row):
    meta = row.get('source_meta') or {}
    sid = row.get('source_id') or meta.get('source_id')
    rev = row.get('source_revision') or meta.get('source_revision')
    return (sid or row.get('source') or row.get('entry_id'), str(rev or ''))


def text_list(value):
    if isinstance(value, list):
        return [str(v) for v in value if v is not None]
    return [str(value)] if value else []


def build(snapshot, output):
    corpus_path = BASE / 'built/corpus.jsonl'
    translated_path = BASE / 'translation-zh-v3/translated.jsonl'
    corpus, translated = lines(corpus_path), lines(translated_path)
    public = json.loads(snapshot.read_text())
    assert all(set(r) == {'record_id', 'payload'} and r['payload'].get('type') == 'recipe' for r in public)
    base = {key(r): r for r in corpus}
    trans = {key(r): r for r in translated}
    db = {key(r['payload']): r for r in public}
    assert len(base) == len(corpus) and len(trans) == len(translated) and len(db) == len(public)
    rows = []
    cached = {}
    for ident in dict.fromkeys([*base, *trans, *db]):
        raw = base.get(ident) or {}
        translated_row = trans.get(ident)
        stored = db.get(ident)
        payload = stored['payload'] if stored else {}
        display = translated_row or payload or raw
        # Prefer the current published text over the earlier translation asset.
        if payload.get('status') == 'published':
            display = payload
        meta = display.get('source_meta') or {}
        original = meta.get('original_content') or (translated_row or {}).get('source_meta', {}).get('original_content') or raw
        ingredients, steps = text_list(display.get('ingredients')), text_list(display.get('steps'))
        original_ingredients = text_list(original.get('ingredients'))
        evidence = '\n'.join([*ingredients, *original_ingredients])
        cache_key = (evidence, bool(ingredients))
        if cache_key not in cached:
            cached[cache_key] = rules.evaluate(evidence, bool(ingredients), [], False)
        assessed = cached[cache_key]
        flags = sorted(set(text_list(raw.get('quality_flags')) + text_list(display.get('quality_flags'))
                           + text_list(meta.get('quality_flags')) + text_list(meta.get('translation_quality_flags'))
                           + text_list((translated_row or {}).get('translation_quality_flags'))))
        extra = display.get('review_sections') or (display.get('conditions') or {}).get('source_additional_sections') or []
        original_sections = original.get('sections') or raw.get('sections') or []
        tutorial = '\n'.join([display.get('title') or '', display.get('body') or '', *ingredients, *steps,
                              *text_list(display.get('notes')), json.dumps(extra, ensure_ascii=False),
                              json.dumps(original, ensure_ascii=False)])
        animal_terms = sorted(set(ANIMAL_WORDS.findall(tutorial)))
        egg_milk_honey = sorted({i['name'] for i in assessed['items'] if i['group'] in ('egg_milk', 'dairy_compound', 'honey')})
        license_data = meta.get('license') or raw.get('source_license') or {}
        rows.append({
            'id': len(rows) + 1, 'source_id': ident[0], 'revision': ident[1],
            'entry_id': payload.get('entry_id'), 'record_id': stored['record_id'] if stored else None,
            'title': display.get('title') or raw.get('title') or '未命名',
            'original_title': original.get('title') or raw.get('title') or '',
            'provider': raw.get('provider') or ('wikibooks' if str(ident[0]).startswith('wikibooks:') else '原有项目内容'),
            'source': display.get('source') or raw.get('source') or '',
            'license': license_data if isinstance(license_data, dict) else {'id': str(license_data)},
            'translated': bool(translated_row), 'in_corpus': bool(raw), 'in_database': bool(stored),
            'published': payload.get('status') == 'published',
            'new_release': bool((payload.get('source_meta') or {}).get('local_beta_release')),
            'chinese': bool(translated_row) or str(raw.get('source_language')).startswith('zh') or bool(stored and not raw),
            'cuisine': payload.get('cuisine') or '',
            'servings': display.get('servings_text') or display.get('servings'),
            'ingredients': ingredients, 'steps': steps, 'notes': text_list(display.get('notes')),
            'body': display.get('body') or '', 'extra': extra,
            'original': {k: original.get(k) for k in ('title', 'ingredients', 'steps', 'notes', 'body')},
            'original_sections': original_sections,
            'labels': assessed['results'], 'flags': flags,
            'animal_terms': animal_terms, 'egg_milk_honey': egg_milk_honey,
            'core_missing': not ingredients or not steps,
        })
    counts = {
        'review_records': len(rows), 'source_records': len(corpus),
        'business_records': len(public), 'translated': len(translated),
        'published': sum(r['published'] for r in rows),
        'new_release': sum(r['new_release'] for r in rows),
        'source_providers': dict(Counter(r['provider'] for r in corpus)),
        'translated_diet_counts': {diet: dict(Counter(r['labels'][diet]['status'] for r in rows if r['translated']))
                                   for diet in ('pure', 'vegan', 'lacto_ovo')},
    }
    manifest = {
        'at': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(), 'counts': counts,
        'rules_version': rules.VERSION,
        'inputs': {str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else p.name: sha(p)
                   for p in (corpus_path, translated_path, snapshot, ROOT / 'backend/app/rules.py')},
        'database_writes': 0, 'external_calls': 0, 'deleted': 0,
        'scope': 'All public recipe sources and current system recipe visibility; no personal data. Review marks are browser-local only.',
        'historical_import_manifest': {'count': 4383, 'meaning': 'earlier imported source batch; not the latest offline source count'},
    }
    data = {'manifest': manifest, 'rows': rows}
    output.mkdir(parents=True, exist_ok=True)
    (output / 'inventory.json').write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')))
    (output / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    encoded = json.dumps(data, ensure_ascii=False, separators=(',', ':')).replace('<', '\\u003c')
    (output / 'index.html').write_text(HTML.replace('__DATA__', encoded))
    (output / 'README.md').write_text(f'''# 菜谱全量审阅 · 2026-10-28

当前离线来源 {counts['source_records']} 条；中文译文 {counts['translated']} 份；业务库 {counts['business_records']} 条；页面可见 {counts['published']} 条，其中新批次 {counts['new_release']} 条。

旧4383条是历史导入批次，当前来源文件含后续采集；862份译文是来源记录的子集，不能相加。页面合并匹配source_id和revision，保留原料、步骤、原文、质量标记、当前三类规则的依据和数据库可见状态。原文未翻译的内容保留原语言，不调用模型翻译。

打开index.html可离线浏览。默认看862份译文，也能切到已展示内容或全部资料。搜索、筛选、逐条标记并导出审阅决定；标记仅保存在此浏览器，不会删除、下架或改动业务库。导出的决定包含来源/版本及快照指纹，后续按用户明确决定处理相应批次。

本次没有删除任何内容、没有修改发布状态、没有外部调用。原资产和业务库快照指纹见manifest.json。inventory.json仅含公共来源菜谱，不包含账号、聊天、私人菜谱或密钥。
''')
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


HTML = r'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>蔬食菜谱 · 全量内容审阅</title>
<style>
:root{font:15px/1.65 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;color:#233c30;background:#f5f5ef}*{box-sizing:border-box}body{margin:0}main{max-width:1360px;margin:auto;padding:28px 24px}h1{font-size:28px;margin:5px 0}h2{font-size:23px;line-height:1.4}h3{font-size:17px}p{margin:8px 0}.muted{color:#677368;font-size:13px}.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:20px 0}.stat{border:1px solid #dbe0d3;border-radius:14px;background:#fff;padding:12px 18px}.stat strong{display:block;font-size:28px}.notice{padding:12px 16px;background:#eceee0;border-radius:12px}.toolbar{display:flex;gap:9px;flex-wrap:wrap;margin:17px 0}input,select,button{font:inherit;color:inherit;background:#fff;border:1px solid #d3d9cd;border-radius:9px;padding:9px}input[type=search]{flex:1;min-width:200px}button{cursor:pointer}button:disabled{opacity:.4;cursor:default}button.active{background:#294d38;color:#fff}.layout{display:grid;grid-template-columns:340px 1fr;gap:18px;align-items:start}.list,.detail{background:#fff;border:1px solid #dbe0d3;border-radius:14px;padding:15px}.list{position:sticky;top:12px;max-height:85vh;overflow:auto}.entry{display:block;text-align:left;width:100%;border:0;border-radius:8px;margin:3px 0;padding:10px;background:#f7f8f3;overflow-wrap:anywhere}.entry.selected{background:#e2e9d9;outline:1px solid #a2b695}.entry small{display:block;color:#687468}.tags{display:flex;flex-wrap:wrap;gap:7px;margin:12px 0}.tag{font-size:12px;padding:4px 8px;border-radius:6px;background:#eff1e9}.suitable{background:#e3eddb}.uncertain{background:#fff1d4;color:#725620}.not_suitable,.risk{background:#f8e8e1;color:#8b4934}li{margin:7px 0;white-space:pre-wrap;overflow-wrap:anywhere}.text{white-space:pre-wrap;overflow-wrap:anywhere}a{color:#326747;overflow-wrap:anywhere}details{border-top:1px solid #e0e5d8;margin-top:18px;padding-top:14px}summary{cursor:pointer}.actions{display:flex;gap:8px;flex-wrap:wrap;margin:18px 0}.pager{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-top:15px}.pager input{width:70px}.reason{font-size:13px;margin:5px 0}.empty{padding:30px;text-align:center}@media(max-width:760px){main{padding:18px 12px}.stats{grid-template-columns:repeat(2,1fr)}.layout{grid-template-columns:1fr}.list{position:static;max-height:300px}.toolbar{gap:7px}h1{font-size:24px}.detail{padding:15px}}
</style></head><body><main><span class="muted">蔬食 · 全量内容审阅 · 仅公开来源</span><h1>先看内容，再决定留不留。</h1><p class="muted">4,383是历史导入来源批次；862是已完成中文翻译的子集。当前离线来源另含后续采集，下面按实际文件和业务库快照计数，不重复累加。</p><div class="stats" id="stats"></div>
<div class="notice">本页可看全部资料，包括已排除、未翻译和存在问题的内容。规则“待核实”与“结构齐备”都不等于合格。下方“保留 / 不合格”仅作审阅标记，不会删库或发布；决定后可导出标记。</div>
<div class="toolbar"><input id="query" type="search" aria-label="搜索菜名原料做法" placeholder="搜索菜名、原料或做法"><select id="scope" aria-label="审阅范围"><option value="translated">862份中文译文</option><option value="published">当前页面可见内容</option><option value="new">本次新增可见内容</option><option value="all">全部来源与项目记录</option><option value="untranslated">尚无中文译文的外文来源</option></select><select id="diet" aria-label="三类素食"><option value="vegan">Vegan</option><option value="pure">纯净素</option><option value="lacto_ovo">蛋奶素</option></select><select id="suitability" aria-label="适配结果"><option value="">适配结果不限</option><option value="suitable">规则显示符合</option><option value="uncertain">待核实</option><option value="not_suitable">规则显示不符合</option></select></div>
<div class="toolbar"><select id="issue" aria-label="问题筛选"><option value="">问题不限</option><option value="animal">教程出现肉鱼等用语</option><option value="egg">规则检出蛋乳蜂蜜</option><option value="missing">原料或步骤缺项</option><option value="flags">有质量标记</option></select><select id="mark" aria-label="我的审阅标记"><option value="">审阅标记不限</option><option value="keep">已标保留</option><option value="reject">已标不合格</option><option value="unreviewed">尚未标记</option></select><button id="export">导出我的审阅标记</button><button id="catalog">下载完整目录 CSV</button></div>
<p class="muted" id="count" aria-live="polite"></p><div class="layout"><aside class="list"><div id="list"></div><nav class="pager"><button id="prev">上一页</button><span id="page"></span><button id="next">下一页</button></nav><div class="pager"><label>跳到第 <input id="jump" type="number" min="1" aria-label="跳转页码"> 页</label><button id="go">跳转</button></div></aside><article class="detail" id="detail"></article></div><p class="muted" id="footer"></p></main>
<script type="application/json" id="data">__DATA__</script><script>
const data=JSON.parse(document.getElementById('data').textContent),rows=data.rows,m=data.manifest,$=id=>document.getElementById(id),size=25;let page=0,selected=null,found=[];const storageKey='shushi-recipe-review-20261028-'+m.inputs['docs/知识库/大批量建库-2026-10-26/built/corpus.jsonl'];let marks={};try{marks=JSON.parse(localStorage.getItem(storageKey)||'{}')}catch{}
const names={pure:'纯净素',vegan:'Vegan',lacto_ovo:'蛋奶素'},states={suitable:'按规则符合',uncertain:'待核实',not_suitable:'按规则不符合'};
const flagNames={missing_servings:'未明确份量',machine_translation_unreviewed:'机器译文未人工复核',unparsed_table:'原文表格未完整解析',missing_ingredients:'未提取原料',missing_steps:'未提取步骤',truncated_steps:'步骤可能截断',source_incomplete:'来源不完整'};
function el(tag,text,cls){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n}
function tag(text,cls=''){return el('span',text,'tag '+cls)}
function urlLink(url,label){try{const u=new URL(url);if(!['https:','http:'].includes(u.protocol))return el('span','来源地址不可打开');const a=el('a',label||url);a.href=u.href;a.target='_blank';a.rel='noopener noreferrer';return a}catch{return el('span','未提供来源链接')}}
function markKey(r){return JSON.stringify([r.source_id,r.revision,r.record_id])}
function persist(){try{localStorage.setItem(storageKey,JSON.stringify(marks))}catch{alert('本机浏览器未能保存标记，请及时导出。')}}
function setMark(r,value){const k=markKey(r);if(value)marks[k]=value;else delete marks[k];persist();render()}
for(const [name,value]of [['当前离线来源',m.counts.source_records],['当前业务库记录',m.counts.business_records],['完成中文译文',m.counts.translated],['当前页面可见',m.counts.published]]){const s=el('div',undefined,'stat');s.append(el('span',name),el('strong',String(value)));$('stats').append(s)}
$('footer').textContent='快照：'+m.at+'；规则版本：'+m.rules_version+'。本页无外部调用、无业务库写入、未删除任何内容。';
for(const r of rows)r.search=[r.title,r.original_title,r.source_id,...r.ingredients,...r.steps,r.body,JSON.stringify(r.original)].join('\n').toLowerCase();
function detail(r){const d=$('detail');d.replaceChildren();if(!r){d.append(el('p','当前筛选没有记录。','empty'));return}d.append(el('span','全量编号 '+r.id+' · '+r.provider,'muted'),el('h2',r.title));if(r.original_title!==r.title)d.append(el('p',r.original_title,'muted'));const status=r.published?(r.new_release?'本次新增，页面可见':'原已发布，页面可见'):(r.in_database?'业务库草稿，页面不可见':'离线来源，未进入业务库');d.append(el('p',status+'；'+(r.translated?'有完整中文机器译文':r.chinese?'中文原文':'尚无中文译文，以下为原文')+'。','muted'));
const tags=el('div',undefined,'tags');for(const diet of Object.keys(names))tags.append(tag(names[diet]+' · '+states[r.labels[diet].status],r.labels[diet].status));d.append(tags);
const actions=el('div',undefined,'actions');for(const [label,value]of [['保留','keep'],['不合格','reject'],['取消标记','']]){const b=el('button',label,marks[markKey(r)]===value?'active':'');b.onclick=()=>setMark(r,value);actions.append(b)}d.append(actions,el('p','仅标记本条，保存于当前浏览器；尚未改动或删除实际菜谱。','muted'));
if(r.egg_milk_honey.length)d.append(el('p','配料规则检出蛋乳/蜂蜜：'+r.egg_milk_honey.join('、'),'reason risk'));if(r.animal_terms.length)d.append(el('p','完整教程出现肉鱼等用语（也可能在替代或搭配说明中）：'+r.animal_terms.join('、'),'reason risk'));if(r.core_missing)d.append(el('p','原料或步骤缺项，请核对原文。','reason risk'));
for(const diet of Object.keys(names)){const a=r.labels[diet];if(a.denied.length)d.append(el('p',names[diet]+'不适配依据：'+a.denied.join('、'),'reason'));if(a.unknown.length)d.append(el('p',names[diet]+'待核实配料：'+a.unknown.join('、'),'reason muted'))}
if(r.servings)d.append(el('p','来源份量：'+String(r.servings)));
for(const [heading,items]of [['原材料与用量',r.ingredients],['制作步骤',r.steps],['备注',r.notes]]){if(heading==='备注'&&!items.length)continue;d.append(el('h3',heading));if(!items.length)d.append(el('p','未提取到此项，不能按完整教程使用。','muted'));else{const list=el(heading==='制作步骤'?'ol':'ul');for(const v of items)list.append(el('li',v));d.append(list)}}
if(r.body){const b=el('details');b.append(el('summary','查看保留的正文'),el('p',r.body,'text'));d.append(b)}
if(r.extra.length){const b=el('details');b.append(el('summary','补充段落'));for(const s of r.extra)b.append(el('h3',s.heading||s.kind||'补充'),el('p',s.text||JSON.stringify(s),'text'));d.append(b)}
const origin=el('details');origin.append(el('summary','核对原文和原始提取段落'));for(const[k,label]of [['ingredients','原文原料'],['steps','原文做法'],['notes','原文备注'],['body','原文正文']]){const v=r.original[k];if(v&&(!Array.isArray(v)||v.length))origin.append(el('h3',label),el('p',Array.isArray(v)?v.join('\n'):v,'text'))}for(const s of r.original_sections)origin.append(el('h3',s.heading||'原始段落'),el('p',s.text||JSON.stringify(s),'text'));d.append(origin);
d.append(el('h3','来源与质量记录'),urlLink(r.source,'打开来源原文'));if(r.license.attribution)d.append(el('p',r.license.attribution,'muted'));if(r.license.url)d.append(urlLink(r.license.url,r.license.id||'来源许可'));d.append(el('p','来源ID：'+r.source_id+'；版本：'+(r.revision||'未提供'),'muted'));if(r.entry_id)d.append(el('p','业务条目ID：'+r.entry_id,'muted'));d.append(el('p',r.flags.length?r.flags.map(f=>flagNames[f]||f).join('；'):'没有额外质量标记，仍不代表已人工审核。','muted'))}
function render(reset=false){if(reset)page=0;const q=$('query').value.trim().toLowerCase().split(/\s+/).filter(Boolean),scope=$('scope').value,diet=$('diet').value,status=$('suitability').value,issue=$('issue').value,mark=$('mark').value;found=rows.filter(r=>(scope==='all'||scope==='translated'&&r.translated||scope==='published'&&r.published||scope==='new'&&r.new_release||scope==='untranslated'&&!r.chinese)&&(!status||r.labels[diet].status===status)&&(!issue||issue==='animal'&&r.animal_terms.length||issue==='egg'&&r.egg_milk_honey.length||issue==='missing'&&r.core_missing||issue==='flags'&&r.flags.length)&&(!mark||mark==='unreviewed'&&!marks[markKey(r)]||marks[markKey(r)]===mark)&&q.every(t=>r.search.includes(t)));const pages=Math.max(1,Math.ceil(found.length/size));page=Math.min(Math.max(page,0),pages-1);if(!found.some(r=>r.id===selected))selected=found[page*size]?.id||null;const shown=found.slice(page*size,(page+1)*size);if(!shown.some(r=>r.id===selected))selected=shown[0]?.id||null;$('list').replaceChildren(...shown.map(r=>{const b=el('button',r.title,'entry'+(r.id===selected?' selected':''));const mk=marks[markKey(r)];b.append(el('small','#'+r.id+' · '+(r.translated?'中文译文':r.chinese?'中文原文':'外文原文')+(r.published?' · 页面可见':'')+(mk?' · '+(mk==='keep'?'已标保留':'已标不合格'):'')));b.onclick=()=>{selected=r.id;render()};return b}));$('count').textContent='当前条件 '+found.length+' 条；已标保留 '+Object.values(marks).filter(x=>x==='keep').length+' 条，不合格 '+Object.values(marks).filter(x=>x==='reject').length+' 条。每页 '+size+' 条，展开右侧即可阅读全文。';$('page').textContent=(page+1)+' / '+pages;$('prev').disabled=page===0;$('next').disabled=page>=pages-1;$('jump').max=pages;$('jump').value=page+1;detail(rows.find(r=>r.id===selected))}
for(const id of ['query','scope','diet','suitability','issue','mark'])$(id).addEventListener(id==='query'?'input':'change',()=>render(true));$('prev').onclick=()=>{page--;selected=null;render()};$('next').onclick=()=>{page++;selected=null;render()};$('go').onclick=()=>{page=Number($('jump').value)-1;selected=null;render()};
function download(name,text,type){const u=URL.createObjectURL(new Blob([text],{type})),a=el('a');a.href=u;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(u),1000)}
$('export').onclick=()=>download('菜谱审阅决定-2026-10-28.json',JSON.stringify({manifest:m,decisions:rows.filter(r=>marks[markKey(r)]).map(r=>({source_id:r.source_id,revision:r.revision,record_id:r.record_id,entry_id:r.entry_id,title:r.title,decision:marks[markKey(r)]})),actual_deletions:0},null,2),'application/json;charset=utf-8');
function csv(v){let s=String(v??'');if(/^[=+@\-\t\r]/.test(s))s="'"+s;return '"'+s.replaceAll('"','""')+'"'}
$('catalog').onclick=()=>{const out=[['编号','菜名','原文菜名','来源','来源ID','版本','有中文译文','页面可见','Vegan规则结果','Vegan不适配依据','Vegan待核实项','蛋乳蜂蜜线索','肉鱼用语线索','质量标记']];for(const r of rows)out.push([r.id,r.title,r.original_title,r.source,r.source_id,r.revision,r.translated?'是':'否',r.published?'是':'否',states[r.labels.vegan.status],r.labels.vegan.denied.join('、'),r.labels.vegan.unknown.join('、'),r.egg_milk_honey.join('、'),r.animal_terms.join('、'),r.flags.join('；')]);download('菜谱完整审阅目录-2026-10-28.csv','\ufeff'+out.map(v=>v.map(csv).join(',')).join('\r\n'),'text/csv;charset=utf-8')};render();
</script></body></html>'''


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--public-snapshot', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=ROOT / 'docs/知识库/全量审阅-2026-10-28')
    args = parser.parse_args()
    build(args.public_snapshot, args.output)
