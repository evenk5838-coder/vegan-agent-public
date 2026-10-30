"""Reproduce a 20-recipe Chinese source pre-review packet; never writes the DB.

Ingredients below were checked against the pinned original ingredient, quantity,
procedure and note sections. This is AI source checking, not human approval,
food-safety review, nutritional verification, or a cooking trial.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from app.rules import VERSION, evaluate  # noqa: E402

BASE = ROOT / 'docs/知识库/大批量建库-2026-10-26/raw/howtocook'
DEST = ROOT / 'docs/知识库/候选/HowToCook-内测来源核对20条-2026-10-26'

# Each list retains source quantities. Additions from procedure-only ingredients
# (such as blanching water) are explicit in review notes, not invented amounts.
REVIEWS = {
    '糖拌西红柿': dict(ingredients=['西红柿 2 个（每个约100g，共200g）', '白砂糖 20g'],
        servings=2, flags=['source_open_flame_peeling_requires_review'],
        notes=['原文有明火旋转烤皮或开水冲皮两种路线；本批未做安全审定，保留原文待确认。', '冰箱是工具，已从食材清单移除；原文营养/功效与热量估计未作为产品结论。']),
    '清蒸南瓜': dict(ingredients=['南瓜 300g', '饮用水 1000ml（蒸锅用水）'], servings=None, flags=[],
        notes=['原文每份够1–2人，未擅自换算为固定1人份。蒸锅是工具。']),
    '松仁玉米': dict(ingredients=['玉米粒 200g（自备煮熟或罐头甜玉米）', '熟松子仁 30g', '胡萝卜 50g（可选）', '食用油 15ml', '白砂糖 10g', '盐 1g', '淀粉 5g', '水 20ml'],
        servings=None, flags=['compound_labels_pending', 'serving_count_unspecified'],
        notes=['原文“每份”未给人数；未编造份数。罐头玉米与食用油需核对实际配料，不按名字放行。']),
    '清炒花菜': dict(ingredients=['花菜 300g', '大蒜 2-3 瓣', '盐 3g', '食用油 15ml', '饮用水 50ml（炒制）', '饮用水 1000ml（焯水）'],
        servings=2, flags=['compound_labels_pending'],
        notes=['原文明确2人配菜。操作第3步另用1000ml焯水，已补入食材；蒜为五辛。食用油来源未明确，仍待核。']),
    '炒青菜': dict(ingredients=['青菜 100g', '蒜瓣 2 粒', '食用油 10-15ml', '食盐 2g', '饮用水 70ml', '白糖 5g'],
        servings=1, flags=['compound_labels_pending'], notes=['蒜在计算和操作中出现，已完整补入；食用油仍待核。']),
    '凉拌莴笋': dict(ingredients=['莴笋 1 根', '萝卜 0.25 根（可选）', '小米辣 2 个', '姜 1 片', '蒜头 2 粒', '盐 5g', '食用油 25ml', '生抽（可选，用量原文未给）', '白砂糖（可选，用量原文未给）', '香醋（可选，用量原文未给）', '芝麻（可选，用量原文未给）', '水（清洗和焯水用，原文未给用量）'],
        servings=None, flags=['optional_quantities_unspecified', 'compound_labels_pending', 'serving_count_unspecified'],
        notes=['附加内容的生抽/白糖/香醋/芝麻完整保留为可选项，没有为增加适配数量偷偷删掉。']),
    '凉拌豆腐': dict(ingredients=['豆腐 250g', '小葱 10g', '大蒜 2-3 瓣', '生抽 15ml', '香油 5ml', '醋 5ml（可选）', '白糖 2g（可选）', '辣椒油 5ml（可选）', '饮用水 500ml（焯水用）'],
        servings=1, flags=['compound_labels_pending'], notes=['操作第2步的500ml焯水补入；生抽等复合调味按实际标签待核，小葱和大蒜不适配纯净素。']),
    '凉拌金针菇': dict(ingredients=['金针菇 150g', '小葱 5g', '大蒜 2 瓣', '生抽 15ml', '醋 10ml', '白糖 3g（可选）', '香油 5ml（可选）', '辣椒油 5ml（可选）', '饮用水 1000ml（焯水用）'],
        servings=1, flags=['compound_labels_pending', 'cooking_conditions_not_professionally_reviewed'],
        notes=['操作中1000ml用水补入；原文焯煮条件仅作为来源记录，本批未做食品安全专业审核。']),
    '虎皮青椒': dict(ingredients=['青椒 5 个（每个长10-15cm）', '大蒜 2-3 瓣', '油 20ml', '白糖 15g', '生抽 15ml', '香醋 15ml', '盐 4g'],
        servings=None, flags=['compound_labels_pending', 'serving_count_unspecified'], notes=['“砵/碗”是工具已移除；油的品种及生抽实际成分仍待核，不替来源指定植物油。']),
    '烤茄子': dict(ingredients=['茄子 1 个（约200g）', '食用油 20-30ml', '酱油 4-6g', '小米椒 1 个（约20g）', '蒜蓉 3-4 瓣', '孜然 1-3g', '食用盐 0.5-2g'],
        servings=2, flags=['compound_labels_pending'], notes=['原文200℃两段烤制、表面擦干/涂油/扎孔及爆裂注意事项完整保留，未删成一步简写。']),
    '酸辣土豆丝': dict(ingredients=['土豆 240g', '大蒜 4 瓣', '青椒 0.5 个', '红椒 0.5 个', '干辣椒 3 个', '葱 1 根', '生抽 5ml', '陈醋 10ml', '盐 2g', '食用油 10-15ml', '水（清洗和焯水用，原文未给用量）'],
        servings=None, flags=['compound_labels_pending', 'serving_count_unspecified'], notes=['原文每份未给人数；葱蒜不适配纯净素，调味品待核。']),
    '印度土豆花菜': dict(ingredients=['土豆 2 个（约250g）', '花菜 1 个（约300g）', '洋葱 1 个（约100g）', '番茄 1 个（约100g）', '生姜 8g', '大蒜 3 瓣（约9g）', '青辣椒 1 根', '食用油 30ml', '孜然籽 3g', '姜黄粉 3g', '红辣椒粉 3g', '香菜粉 5g', '印度综合香料粉 3g', '盐 5g', '香菜叶 15g', '水 15-30ml（太干时可选）'],
        servings=None, cuisine='other', flags=['compound_labels_pending'],
        notes=['原文明示北印度Aloo Gobi，归入外国菜而不是按中文语言推断中国菜。每份2–3人不固定；综合香料和食用油需核标签。附加内容配饼/饭建议不计作本菜原料。']),
    '桂圆红枣粥': dict(ingredients=['糯米 100g（原文可用大米）', '红枣 15 颗', '桂圆 15 颗', '水 2000ml'],
        servings=None, flags=['serving_count_unspecified'], notes=['操作第3步2000ml清水补入；原文“每份”未给人数；原文补血安神等功效未作为本产品结论。']),
    '水煮玉米': dict(ingredients=['新鲜带皮玉米 1 个', '水（淹过玉米约半指节）', '盐 约2g/50ml水（按来源文字，需复核）', '糖（可选，用量原文未给）'],
        servings=None, flags=['source_salt_ratio_ambiguous', 'optional_quantities_unspecified'],
        notes=['原文盐水表达存在歧义，不自行纠正为猜测比例，保留待复核；15–20分钟是来源做法，不是通用安全保证。']),
    '电饭煲蒸米饭': dict(ingredients=['江南米或北方大米 100-200ml/人', '水（江南米约高出米面2.6cm；北方米约2cm，按来源指尖法）'],
        servings=None, flags=['water_measure_depends_on_equipment'], notes=['原文水量是指尖法，依米种与设备存在差异，未伪造精确毫升比例；电饭煲说明书提醒保留。']),
    '煮锅蒸米饭': dict(ingredients=['北方大米 100-200ml/人', '水（体积为米的2倍）'],
        servings=None, flags=[], notes=['原文米水体积2倍、小火10–15分钟和关火5分钟原样保留；末尾Enjoy仅转为“享用”，没有改变烹饪条件。']),
    '小米粥': dict(ingredients=['小米 100g', '水 2000g'], servings=None, flags=['serving_count_unspecified'],
        notes=['普通锅路线完整保留；附加内容高压锅/电饭锅建议1800g水单独留作来源分支，没有混进主路线。原文养胃等功效未作为结论。']),
    '银耳莲子粥': dict(ingredients=['银耳 60g', '去心莲子 20g', '红枣 6g', '枸杞 5-6g（可选）', '冰糖 10-20g（分两次各5-10g）', '水 600ml（另需浸泡水，用量原文未给）'],
        servings=1, flags=['dried_or_fresh_ingredient_ambiguous', 'cooking_conditions_not_professionally_reviewed'],
        notes=['银耳60g未写明干/鲜，未据常识改数；600ml操作用水补入；浸泡/熬制条件未做食品安全专业审核。']),
    '奇异果菠菜特调': dict(ingredients=['饮用水 700ml', '奇异果 2 个', '苹果 1/2 个', '菠菜叶 4 叶', '白砂糖 12g（可按原文0-12g调整）'],
        servings=None, flags=['raw_leaf_handling_unspecified', 'serving_count_unspecified'],
        notes=['原文未写菠菜清洗/生食处理条件，待补充后再考虑发布；工具榨汁机移出食材。']),
    '醪糟小汤圆': dict(ingredients=['水 300ml', '小汤圆 250g', '醪糟 50g', '枸杞 5 颗（可选）', '白糖（按口味，原文未给用量）'],
        servings=None, flags=['compound_labels_pending', 'optional_quantities_unspecified'],
        notes=['每份1–2人未换算；小汤圆/醪糟需看包装完整配料，不能因为菜名没有肉就标三类都适配。']),
}


def strip_markdown(value: str) -> str:
    text = re.sub(r'!\[[^]]*\]\([^)]*\)', '', value)
    text = re.sub(r'\[([^]]+)\]\([^)]*\)', r'\1', text)
    text = re.sub(r'^\s*\d+[.)、]\s*', '', text)
    text = re.sub(r'[*`]', '', text).strip()
    return text.replace('Enjoy :)', '享用。')


def build():
    source_rows = [json.loads(line) for line in (BASE / 'parsed.jsonl').read_text().splitlines() if line]
    by_name = {Path(row['source_path']).stem: row for row in source_rows}
    entries, summaries = [], []
    for index, (name, checked) in enumerate(REVIEWS.items(), 1):
        row = by_name[name]
        raw = (BASE / row['source_path']).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == row['source_sha256'], f'source changed: {name}'
        parsed = row['parsed']
        steps = [strip_markdown(line) for line in parsed['steps'] if strip_markdown(line)]
        source_notes = []
        for section in parsed.get('sections') or []:
            if section.get('kind') == 'other':
                text = re.sub(r'如果您遵循本指南.*', '', section.get('body') or '', flags=re.S)
                text = strip_markdown(text)
                if text:
                    source_notes.append(text)
        result = evaluate('、'.join(checked['ingredients']), True, [], False)
        labels = result['results']
        entry = {'type': 'recipe', 'title': name, 'source': row['source_url'], 'status': 'draft',
                 'ingredients': checked['ingredients'], 'steps': steps,
                 'notes': source_notes,
                 'servings_text': str(parsed.get('servings') or '来源未说明固定人数'),
                 'servings': checked['servings'], 'meal': None,
                 'cuisine': checked.get('cuisine', 'chinese'), 'nutrition_notes': '',
                 'body': '来源原料/用量/操作/附加内容已经AI逐条对照，尚未获发布批准、未试做、未经营养或食品安全专业审核。',
                 'conditions': {'source_id': row['source_id'], 'source_revision': row['source_commit_sha'],
                     'license': {'id': 'Unlicense', 'attribution': 'Anduin2017/HowToCook contributors',
                                 'url': 'https://github.com/Anduin2017/HowToCook/blob/' + row['source_commit_sha'] + '/LICENSE'},
                     'language': 'zh-CN', 'display_language': 'zh-CN',
                     'source_structural_complete': True, 'manual_reviewed': False,
                     'publication_approved': False, 'quality_flags': checked['flags'],
                     'source_servings_text': parsed.get('servings'), 'content_sha256': row['source_sha256'],
                     'reviewed_source_sha256': row['source_sha256'],
                     'ai_source_review': True, 'review_scope': 'AI source text comparison; not professional or trial review',
                     'source_author': 'Anduin2017/HowToCook contributors',
                     'source_checked_at': '2026-10-26',
                     'machine_rules_version': VERSION, 'machine_diet_labels': labels,
                     'machine_rules_sha256': hashlib.sha256((ROOT / 'backend/app/rules.py').read_bytes()).hexdigest(),
                     'original_content': {'markdown': raw.decode(), 'title': parsed['title'],
                                          'ingredients': checked['ingredients'],
                                          'steps': steps, 'notes': source_notes,
                                          'language': 'zh-CN', 'source_sha256': row['source_sha256'],
                                          'ingredients_text': parsed['ingredients_text'],
                                          'steps_text': parsed['steps_text'], 'source_parser_issues': parsed['issues']},
                     'editorial_review_notes': checked['notes'],
                     'source_additional_sections': [section for section in parsed.get('sections') or []
                                                   if section.get('kind') not in ('ingredients', 'calculation', 'steps')]},
                 'reviewer_note': 'AI来源核对。' + '；'.join(checked['notes']) + ' 未获用户发布确认。'}
        entries.append(entry)
        summaries.append({'id': f'HTC-{index:02}', 'title': name, 'source_id': row['source_id'],
                          'quality_flags': checked['flags'],
                          'diet_labels': {diet: value['status'] for diet, value in labels.items()},
                          'review_notes': checked['notes']})
    packet = {'batch_id': 'howtocook-chinese-pre-review-20-2026-10-26',
              'source_commit': source_rows[0]['source_commit_sha'], 'rules_version': VERSION,
              'rules_source_sha256': hashlib.sha256((ROOT / 'backend/app/rules.py').read_bytes()).hexdigest(),
              'count': len(entries), 'published': 0, 'manual_reviewed': 0,
              'ai_source_checked': len(entries), 'approval_status': 'not_approved',
              'boundary': '20 specific Chinese source pre-reviews; not 20 published or certified recipes',
              'entries': entries, 'review_summary': summaries}
    DEST.parent.mkdir(parents=True, exist_ok=True)
    DEST.with_suffix('.json').write_text(json.dumps(packet, ensure_ascii=False, indent=2) + '\n')
    chunks = ['# HowToCook 20条中文内测候选｜AI来源核对稿\n',
              '本批从固定修订原文逐条对照原料、计算、操作和附加内容，保留原始用量与分支。**20条候选，0条已发布；AI核对不等于人工批准、试做、营养或食品安全专业审核。**\n',
              '已排除发现蚝油/耗油、五花肉、鸡汤骨汤、熟肉分支的来源版本，没有按“素菜”名称自动收录。所有仍有复合成分的配方保留待核实；未按发布数量目标强行通过。\n',
              f'固定来源修订 `{packet["source_commit"]}`；文本许可 Unlicense；规则 `{VERSION}`。原文全文和来源hash保存在同名JSON，照片未引入。\n',
              '| 编号 | 菜名 | 纯净素 | Vegan | 蛋奶素 | 待核项 |\n|---|---|---|---|---|---|']
    state = {'suitable': '可见配料适配', 'not_suitable': '不适配', 'uncertain': '待核实'}
    for summary in summaries:
        labels = summary['diet_labels']
        chunks.append('| ' + ' | '.join([summary['id'], summary['title'], *(state[labels[d]] for d in ('pure', 'vegan', 'lacto_ovo')), ', '.join(summary['quality_flags']) or '仍需发布确认']) + ' |')
    for entry, summary in zip(entries, summaries):
        chunks += ['\n## ' + summary['id'] + ' ' + entry['title'],
                   '\n[固定修订原文](' + entry['source'] + ')；SHA256 `' + entry['conditions']['content_sha256'] + '`。',
                   '\n原始分量说明：' + str(entry['conditions']['source_servings_text'] or '来源未给固定人数，不补写。'),
                   '\n### 原料与用量\n' + '\n'.join('- ' + value for value in entry['ingredients']),
                   '\n### 原文操作\n' + '\n'.join(str(i) + '. ' + value for i, value in enumerate(entry['steps'], 1)),
                   '\n### 原文附加内容与注意事项\n' + ('\n\n'.join(entry['notes']) or '来源未另列。'),
                   '\n### 核对与待决事项\n' + '\n'.join('- ' + value for value in summary['review_notes']),
                   '\n三类依据：\n' + '\n'.join('- ' + d + '：' + json.dumps(entry['conditions']['machine_diet_labels'][d], ensure_ascii=False) for d in ('pure', 'vegan', 'lacto_ovo'))]
    chunks += ['\n## 发布前仍需处理\n',
               '本文件仅用于具体批次审阅，不操作真实业务数据库。需先解决各条内容中的歧义、复合配料和质量告警，再按用户明确批准的source_id及内容hash发布；旧3道菜和3条储存规则不在本批授权范围。']
    DEST.with_suffix('.md').write_text('\n'.join(chunks) + '\n')
    return {'count': len(entries), 'published': 0, 'manual_reviewed': 0,
            'quality_without_flags': sum(not e['conditions']['quality_flags'] for e in entries),
            'diet_counts': {diet: dict(Counter(e['conditions']['machine_diet_labels'][diet]['status'] for e in entries))
                            for diet in ('pure', 'vegan', 'lacto_ovo')},
            'json': str(DEST.with_suffix('.json').relative_to(ROOT)),
            'markdown': str(DEST.with_suffix('.md').relative_to(ROOT))}


if __name__ == '__main__':
    print(json.dumps(build(), ensure_ascii=False))
