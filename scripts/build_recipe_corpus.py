"""Build a traceable source-review corpus and local full-text search index.

No app imports, environment credentials, network or business-database writes.
Every output remains draft; counts distinguish raw sources, structure and diet.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import runpy
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.recipe_sources.normalize import explicit_animal_evidence  # noqa: E402
from scripts.recipe_sources.wikibooks import parse as parse_wiki  # noqa: E402

DEST = ROOT / 'docs/知识库/大批量建库-2026-10-26'
DIETS = ('pure', 'vegan', 'lacto_ovo')


def read_jsonl(path: Path):
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip():
                yield json.loads(line)


def plain_markdown(value: str) -> str:
    value = re.sub(r'!\[[^]]*\]\([^)]*\)', '', value)
    value = re.sub(r'\[([^]]+)\]\([^)]*\)', r'\1', value)
    return re.sub(r'[*`]', '', value).strip()


def markdown_lines(blocks):
    for block in blocks:
        for line in str(block).splitlines():
            if line.strip() and not line.lstrip().startswith('#'):
                line = re.sub(r'^\s*(?:[-*+]\s+|\d+[.、)]\s*)', '', line)
                line = plain_markdown(line)
                if line:
                    yield line


def sources():
    for path in sorted((ROOT / 'data/recipe-sources/wikibooks').glob('[0-9]*.json')):
        page = json.loads(path.read_text())
        row = parse_wiki(page)
        row.update(source_id=f"wikibooks:{page['pageid']}", provider='wikibooks',
                   source_language='en', source_license=page.get('license'),
                   source=row.get('source_url'), source_raw_file=str(path.relative_to(ROOT)))
        yield row
    for row in read_jsonl(DEST / 'raw/howtocook/parsed.jsonl'):
        p = row['parsed']
        ingredients = list(markdown_lines(p.get('ingredients') or []))
        steps = list(markdown_lines(p.get('steps') or []))
        yield {**row, 'title': p['title'], 'ingredients': ingredients, 'steps': steps,
               'body': p.get('ingredients_text', '') + '\n' + p.get('steps_text', ''),
               'servings': p.get('servings'), 'source': row['source_url'],
               'source_revision': row['source_commit_sha'], 'provider': 'howtocook',
               'structural_complete': bool(ingredients and steps),
               'quality_flags': list(p.get('issues') or [])}
    for row in read_jsonl(DEST / 'raw/gutenberg/parsed.jsonl'):
        yield {**row, 'provider': 'gutenberg', 'structural_complete': bool(row.get('ingredients') and row.get('steps'))}


def label(row, rules):
    ingredients = row.get('ingredients') or []
    details = []
    terms = []
    for ingredient in ingredients:
        # Classification receives original source lines through the same
        # normalizer as the app; a source-only alias table is not a second
        # policy and must not discard residual or mixed ingredient evidence.
        item = {'terms': rules['normalize_ingredients'](ingredient),
                'method': 'shared_application_ingredient_normalizer'}
        details.append({'source_text': ingredient, **item})
        terms.extend(item['terms'])
    # Overlapping policy sets are a known pending backend fix; never let a
    # composite name inherit plant clearance in this review batch.
    overlap = set(terms) & (rules['PLANTS'] & rules['COMPOUND'])
    complete = bool(ingredients) and bool(row.get('structural_complete')) and not overlap
    evaluation = rules['evaluate']('\n'.join(ingredients), complete, [], False)
    # Ingredient list alone is not a verified finished recipe. Keep source and
    # parser quality separate from diet-rule results; release remains manual.
    return {'rules_version': rules['VERSION'], 'normalizer_version': 'shared-application-rules-v2',
            'rules_source_sha256': rules['_source_sha256'],
            'labels': {diet: evaluation['results'][diet] for diet in DIETS},
            'canonical_ingredients': list(dict.fromkeys(terms)),
            'ingredient_normalization': details, 'policy_overlap_requires_review': sorted(overlap),
            'pungent': evaluation['pungent'], 'pungent_items': evaluation['pungent_items']}


def recipe_field_limit_errors(row):
    """Oversized source content stays auditable; it must never be silently cut."""
    limits = {'title': 200, 'body': 8000, 'ingredients': 60, 'steps': 100, 'notes': 40}
    return [f'{field}:{len(row.get(field) or [])}>{maximum}'
            for field, maximum in limits.items() if len(row.get(field) or []) > maximum]


def make_entry(row):
    limit_errors = recipe_field_limit_errors(row)
    if limit_errors:
        raise ValueError(f"recipe_field_limit_exceeded:{row.get('source_id')}:{','.join(limit_errors)}")
    labels = row['diet_assessment']['labels']
    match = re.fullmatch(r'\s*(\d{1,2})\s*(?:人份|人|份|servings?)?\s*', str(row.get('servings') or ''), re.I)
    servings = int(match[1]) if match and 1 <= int(match[1]) <= 50 else None
    categories = ' '.join(row.get('source_categories') or []).casefold()
    foreign = ('italian recipes', 'french recipes', 'indian recipes', 'nigerian recipes',
               'american recipes', 'british recipes', 'japanese recipes', 'korean recipes',
               'mexican recipes', 'thai recipes', 'greek recipes', 'german recipes',
               'spanish recipes', 'turkish recipes', 'lebanese recipes', 'russian recipes')
    chinese_evidence = 'chinese recipes' in categories
    foreign_evidence = any(value in categories for value in foreign)
    cuisine = 'chinese' if chinese_evidence and not foreign_evidence else 'western' if foreign_evidence and not chinese_evidence else None
    # Source language is not evidence of cuisine. Leave unsupported labels open.
    return {'type': 'recipe', 'title': row['title'], 'source': row['source'], 'status': 'draft',
            'body': row.get('body', ''), 'ingredients': row['ingredients'], 'steps': row['steps'],
            'notes': row.get('notes') or [],
            'servings': servings, 'meal': None, 'cuisine': cuisine,
            'conditions': {'source_id': row['source_id'], 'source_revision': row.get('source_revision'),
                           'license': row.get('source_license'), 'language': row.get('source_language'),
                           'source_categories_are_hints': True, 'manual_reviewed': False,
                           'source_structural_complete': row['structural_complete'],
                           'quality_flags': row['quality_flags'], 'source_servings_text': row.get('servings'),
                           'machine_diet_labels': labels, 'machine_rules_version': row['diet_assessment']['rules_version'],
                           'machine_rules_sha256': row['diet_assessment']['rules_source_sha256'],
                           'canonical_ingredients': row['diet_assessment']['canonical_ingredients']},
            'reviewer_note': '开放来源机器抽取草稿；未逐条人工复核、未试做、未获发布批准。原文/许可/修订与规则结果可追溯。'}


def build(output: Path):
    started = time.perf_counter()
    output.mkdir(parents=True, exist_ok=True)
    rules = runpy.run_path(str(ROOT / 'backend/app/rules.py'))  # pure re-only module
    rules['_source_sha256'] = hashlib.sha256((ROOT / 'backend/app/rules.py').read_bytes()).hexdigest()
    seen_id, seen_content = set(), set()
    rows, duplicates, skipped = [], [], []
    for raw in sources():
        row = {**raw}
        row['ingredients'] = [str(x).strip() for x in row.get('ingredients') or [] if str(x).strip()]
        row['steps'] = [str(x).strip() for x in row.get('steps') or [] if str(x).strip()]
        row['quality_flags'] = list(dict.fromkeys(row.get('quality_flags') or []))
        if not row.get('source') or not row.get('title') or 'redirect' in ' '.join(row['quality_flags']):
            skipped.append({'source_id': row.get('source_id'), 'reason': 'missing_identity_or_redirect'})
            continue
        content = re.sub(r'\s+', ' ', '\n'.join([*row['ingredients'], *row['steps']])).casefold().strip()
        fingerprint = hashlib.sha256(content.encode()).hexdigest() if content else None
        if row['source_id'] in seen_id or fingerprint in seen_content:
            duplicates.append({'source_id': row['source_id'], 'reason': 'same_source_id_or_exact_normalized_content'})
            continue
        seen_id.add(row['source_id'])
        if fingerprint:
            seen_content.add(fingerprint)
        limit_errors = recipe_field_limit_errors(row)
        if limit_errors:
            row['quality_flags'].append('exceeds_current_app_structured_field_limits')
            row['structured_field_limit_errors'] = limit_errors
            row['structural_complete'] = False
        row['content_sha256'] = fingerprint
        row['status'] = 'draft'
        row['publication_approved'] = False
        row['manual_reviewed'] = False
        row['diet_assessment'] = label(row, rules)
        row['all_diets_rejected'] = all(v['status'] == 'not_suitable' for v in row['diet_assessment']['labels'].values())
        row['explicit_animal_evidence'] = explicit_animal_evidence(row['ingredients'] or [row.get('body') or ''])
        row['source_category_exclusion_evidence'] = []
        source_path = row.get('source_path') or ''
        if source_path.startswith(('dishes/meat_dish/', 'dishes/aquatic/')):
            row['source_category_exclusion_evidence'].append('source_meat_or_aquatic_section:' + source_path)
        for category in row.get('source_categories') or []:
            if re.search(r'Category:(?:Beef|Pork|Chicken|Turkey|Duck|Lamb|Mutton|Meat|Fish|Seafood|Shrimp|Prawn|Bacon|Ham|Sausage|Offal|Lard) recipes\b', category, re.I):
                row['source_category_exclusion_evidence'].append(category)
        # A suspicious source category can withhold a draft, never approve it.
        row['review_candidate_eligible'] = (not row['all_diets_rejected'] and not row['explicit_animal_evidence']
                                          and not row['source_category_exclusion_evidence'] and not limit_errors)
        rows.append(row)
    corpus = output / 'corpus.jsonl'
    corpus.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows))
    reviewable = [row for row in rows if row['review_candidate_eligible']]
    import_rows = [make_entry(row) for row in reviewable]
    (output / 'knowledge-import-draft.json').write_text(json.dumps({'entries': import_rows}, ensure_ascii=False, indent=2) + '\n')
    index = output / 'recipe-search.sqlite3'
    temporary = index.with_suffix('.tmp')
    temporary.unlink(missing_ok=True)
    with sqlite3.connect(temporary) as db:
        db.execute('CREATE TABLE recipes(id TEXT PRIMARY KEY,title TEXT,source TEXT,provider TEXT,language TEXT,structural_complete INTEGER,review_excluded INTEGER,labels TEXT,record TEXT)')
        db.execute('CREATE VIRTUAL TABLE recipe_fts USING fts5(id UNINDEXED,title,ingredients,steps,canonical,tokenize="unicode61")')
        for row in rows:
            assessment = row['diet_assessment']
            db.execute('INSERT INTO recipes VALUES(?,?,?,?,?,?,?,?,?)',
                       (row['source_id'], row['title'], row['source'], row['provider'], row.get('source_language'),
                        int(row.get('structural_complete', False)), int(not row['review_candidate_eligible']),
                        json.dumps(assessment['labels'], ensure_ascii=False), json.dumps(row, ensure_ascii=False)))
            db.execute('INSERT INTO recipe_fts VALUES(?,?,?,?,?)',
                       (row['source_id'], row['title'], '\n'.join(row['ingredients']), '\n'.join(row['steps']),
                        ' '.join(assessment['canonical_ingredients'])))
        db.commit()
    temporary.replace(index)
    report = {'source_recipes_after_exact_dedup': len(rows), 'distinct_normalized_titles': len({row['title'].casefold().strip() for row in rows}),
              'source_counts': dict(Counter(row['provider'] for row in rows)),
              'structurally_complete': sum(bool(row.get('structural_complete')) for row in rows),
              'with_ingredients_and_steps': sum(bool(row['ingredients'] and row['steps']) for row in rows),
              'review_candidates_after_exclusions': len(reviewable),
              'structurally_complete_review_candidates': sum(bool(row.get('structural_complete')) for row in reviewable),
              'all_diets_rejected': sum(row['all_diets_rejected'] for row in rows),
              'additional_source_evidence_quarantined': sum(not row['review_candidate_eligible'] and not row['all_diets_rejected'] for row in rows),
              'diet_labels': {diet: dict(Counter(row['diet_assessment']['labels'][diet]['status'] for row in rows)) for diet in DIETS},
              'review_candidates_diet_labels': {diet: dict(Counter(row['diet_assessment']['labels'][diet]['status'] for row in reviewable)) for diet in DIETS},
              'duplicates': duplicates, 'skipped': skipped, 'rules_version': rules['VERSION'],
              'business_database_written': False, 'published': 0, 'manual_reviewed': 0,
              'corpus_sha256': hashlib.sha256(corpus.read_bytes()).hexdigest(),
              'policy_source_sha256': hashlib.sha256((ROOT / 'backend/app/rules.py').read_bytes()).hexdigest(),
              'normalizer_source_sha256': hashlib.sha256((ROOT / 'scripts/recipe_sources/normalize.py').read_bytes()).hexdigest(),
              'build_seconds': round(time.perf_counter() - started, 3),
              'boundary': 'source corpus and local review search only; structural completeness is not manual review or dietary certification'}
    (output / 'build-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=DEST / 'built')
    args = parser.parse_args()
    report = build(args.output)
    print(json.dumps({k: v for k, v in report.items() if k not in ('duplicates', 'skipped')}, ensure_ascii=False, indent=2))
