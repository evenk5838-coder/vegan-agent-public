"""Create a local, offline review UI. No API, publication or write actions."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILT = ROOT / 'docs/知识库/大批量建库-2026-10-26/built'


def build():
    report = json.loads((BUILT / 'build-report.json').read_text())
    rows = []
    for line in (BUILT / 'corpus.jsonl').read_text().splitlines():
        row = json.loads(line)
        rows.append({k: row.get(k) for k in ('source_id', 'title', 'source', 'provider', 'source_language',
                                             'ingredients', 'steps', 'body', 'servings', 'structural_complete',
                                             'all_diets_rejected', 'quality_flags', 'source_license', 'review_candidate_eligible')} |
                    {'labels': {k: v['status'] for k, v in row['diet_assessment']['labels'].items()},
                     'canonical': ' '.join(row['diet_assessment']['canonical_ingredients'])})
    data = json.dumps({'report': report, 'rows': rows}, ensure_ascii=False).replace('<', '\\u003c')
    html = r'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>蔬食 · 菜谱来源库审阅</title><style>
:root{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;color:#243d32;background:#f6f6ef}*{box-sizing:border-box}body{margin:0}main{max-width:1120px;margin:auto;padding:32px 24px 80px}header{display:flex;justify-content:space-between;align-items:center;gap:20px}h1{font-size:30px;letter-spacing:-1px;margin:15px 0}p{line-height:1.75}small,.muted{color:#69796d}small{font-size:13px}.badge{background:#e9eedc;border-radius:99px;padding:8px 13px;white-space:nowrap}.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:24px 0}.stat{background:#fff;border:1px solid #dce2d4;border-radius:16px;padding:20px}.stat strong{display:block;font-size:30px;margin:7px 0}.toolbar{display:flex;gap:10px;flex-wrap:wrap;margin:24px 0 12px}input,select,button{font:inherit;border:1px solid #d8dfd2;border-radius:11px;background:#fff;padding:11px;color:inherit}input[type=search]{flex:1;min-width:220px}button{cursor:pointer}button:disabled{opacity:.4;cursor:default}details{background:#fff;border:1px solid #dce2d4;border-radius:16px;margin:12px 0;padding:18px 20px}summary{cursor:pointer;line-height:1.6;list-style:none;display:flex;justify-content:space-between;gap:18px}summary strong{font-size:18px}.tags{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}.tag{font-size:12px;background:#f1f3eb;padding:4px 9px;border-radius:6px}.uncertain{background:#fff3da;color:#7c5c20}.not_suitable{background:#f5e6e1;color:#884f3a}.suitable{background:#e5efdb;color:#365e3d}.text{white-space:pre-wrap;line-height:1.8;color:#3c4e42}.notice{background:#ecefe3;padding:15px 18px;border-radius:12px}a{color:#376c50;word-break:break-all}nav{display:flex;align-items:center;justify-content:space-between;margin-top:22px}.content{margin-top:18px;border-top:1px solid #e6eadf;padding-top:14px}.content h3{font-size:15px}.content li{line-height:1.8;margin:6px 0}@media(max-width:650px){main{padding:18px 15px 50px}h1{font-size:25px}.stats{grid-template-columns:repeat(2,1fr)}.stat{padding:14px}.stat strong{font-size:25px}header{align-items:start}.badge{font-size:12px}summary{display:block}}</style>
<main><header><div><small>蔬食 SHUSHI · 内容工作台</small><h1>菜谱来源库，已经开始成形。</h1></div><span class="badge">本地审阅 · 全部草稿</span></header>
<p class="muted">这是实际采集、抽取、去重后的来源资料审阅页。可以搜索、展开原料与步骤、查看三类规则初判。<br>它与产品已发布菜谱库分开计数；英文和历史原文保留原语言，未逐条人工核对。</p><section class="stats" id="stats"></section>
<div class="notice">来源分类只作线索。三类结果可重叠，“待核实”不代表符合；本页没有发布或保存按钮，不会调用模型或修改业务数据。</div>
<div class="toolbar"><input type="search" id="query" placeholder="搜索菜名、原料、步骤，例如：土豆 / tofu / soup" aria-label="搜索菜谱资料"><select id="diet" aria-label="饮食类型"><option value="">三类全部</option><option value="pure">纯净素</option><option value="vegan">Vegan</option><option value="lacto_ovo">蛋奶素</option></select><select id="status" aria-label="规则结果"><option value="">结果不限</option><option value="suitable">初判适配</option><option value="uncertain">待核实</option><option value="not_suitable">不适配</option></select><select id="provider" aria-label="来源"><option value="">全部来源</option><option value="howtocook">HowToCook</option><option value="wikibooks">Wikibooks</option><option value="gutenberg">Gutenberg古籍</option></select></div>
<label><input type="checkbox" id="complete"> 仅显示结构化食材与步骤齐全</label>　<label><input type="checkbox" id="rejected"> 审核时也显示排除或隔离的来源资料</label>
<p id="count" aria-live="polite" class="muted"></p><section id="results"></section><nav><button id="prev">上一页</button><small id="page"></small><button id="next">下一页</button></nav></main>
<script type="application/json" id="data">__DATA__</script><script>
const data=JSON.parse(document.getElementById('data').textContent),rows=data.rows,report=data.report;let page=0;const size=20;
const names={pure:'纯净素',vegan:'Vegan',lacto_ovo:'蛋奶素'}, statuses={suitable:'初判适配',uncertain:'待核实',not_suitable:'不适配'};
const $=id=>document.getElementById(id);const el=(tag,text,cls)=>{const node=document.createElement(tag);if(text!==undefined)node.textContent=text;if(cls)node.className=cls;return node};
for(const [name,value]of [['独立来源记录',report.source_recipes_after_exact_dedup],['待审核候选',report.review_candidates_after_exclusions],['结构字段齐全',report.structurally_complete],['已发布',report.published]]){const box=el('div',undefined,'stat');box.append(el('small',name),el('strong',String(value)));$('stats').append(box)}
for(const row of rows)row.search=[row.title,row.canonical,row.body,...row.ingredients,...row.steps].join('\n').toLowerCase();
function detail(row){const box=el('details'),summary=el('summary'),left=el('div');left.append(el('strong',row.title));const tags=el('div',undefined,'tags');for(const[key,value]of Object.entries(row.labels))tags.append(el('span',names[key]+' · '+statuses[value],'tag '+value));left.append(tags);summary.append(left,el('small',row.provider+' · '+(row.source_language==='zh'?'中文原文':'英文原文')));box.append(summary);box.addEventListener('toggle',()=>{if(!box.open||box.dataset.loaded)return;box.dataset.loaded='1';const content=el('div',undefined,'content');content.append(el('p','原文分量：'+(row.servings||'未明确提取')+'；'+(row.structural_complete?'结构化字段齐全，尚未人工复核。':'存在缺项或解析问题，不能当作已核对完整教程。'),'muted'));for(const[name,list]of [['原料',row.ingredients],['做法原文',row.steps]]){content.append(el('h3',name));if(!list.length)content.append(el('p','尚未独立提取；原文仍保留。','muted'));else{const ul=el(name==='原料'?'ul':'ol');for(const text of list)ul.append(el('li',text));content.append(ul)}}if(!row.ingredients.length){content.append(el('h3','原文段落'),el('p',row.body||'','text'))}content.append(el('h3','来源与解析状态'));try{const url=new URL(row.source);if(url.protocol==='https:'&&['github.com','en.wikibooks.org','www.gutenberg.org'].includes(url.hostname)){const link=el('a',row.source);link.href=url.href;link.target='_blank';link.rel='noopener noreferrer';content.append(link)}}catch{}content.append(el('p',row.quality_flags.join(' · ')||'未记录额外解析警告；仍需人工复核。','muted'));box.append(content)});return box}
function render(reset=false){if(reset)page=0;const terms=$('query').value.trim().toLowerCase().split(/\s+/).filter(Boolean),diet=$('diet').value,status=$('status').value,provider=$('provider').value;const found=rows.filter(row=>(!provider||row.provider===provider)&&($('rejected').checked||row.review_candidate_eligible)&&(!$('complete').checked||row.structural_complete)&&(!status||(diet?row.labels[diet]===status:Object.values(row.labels).includes(status)))&&terms.every(term=>row.search.includes(term)));const pages=Math.max(1,Math.ceil(found.length/size));page=Math.min(page,pages-1);$('count').textContent='当前条件找到 '+found.length+' 条来源资料；每页 '+size+' 条。';$('results').replaceChildren(...found.slice(page*size,(page+1)*size).map(detail));$('page').textContent=(page+1)+' / '+pages;$('prev').disabled=!page;$('next').disabled=page>=pages-1}
for(const id of ['query','diet','status','provider','complete','rejected'])$(id).addEventListener(id==='query'?'input':'change',()=>render(true));$('prev').onclick=()=>{page--;render()};$('next').onclick=()=>{page++;render()};render();
</script></html>'''
    target = BUILT / 'review.html'
    target.write_text(html.replace('__DATA__', data))
    print(json.dumps({'file': str(target), 'records': len(rows), 'bytes': target.stat().st_size}, ensure_ascii=False))


if __name__ == '__main__':
    build()
