"""Validate the review batch against real schema pure functions, without app IO."""
from __future__ import annotations

import ast
import hashlib
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.search_recipe_corpus import search  # noqa: E402

BUILT = ROOT / 'docs/知识库/大批量建库-2026-10-26/built'


class EntryError(Exception):
    def __init__(self, code, message, status=400):
        self.code, self.message, self.status = code, message, status


def main():
    tree = ast.parse((ROOT / 'backend/app/knowledge.py').read_text())
    nodes = [node for node in tree.body if
             (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in {'TYPES', 'STATUSES'} for t in node.targets))
             or (isinstance(node, ast.FunctionDef) and node.name in {'validate_entry', '_recipe_fields'})]
    scope = {'AppError': EntryError}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), 'knowledge-schema-pure', 'exec'), scope)
    entries = json.loads((BUILT / 'knowledge-import-draft.json').read_text())['entries']
    report = json.loads((BUILT / 'build-report.json').read_text())
    errors, truncation = [], []
    seen = set()
    for entry in entries:
        key = entry['conditions']['source_id']
        assert key not in seen, key
        seen.add(key)
        assert entry['status'] == 'draft'
        try:
            clean = scope['validate_entry'](entry)
            if clean['source'] != entry['source']:
                truncation.append(key)
        except EntryError as error:
            errors.append({'source_id': key, 'code': error.code, 'message': error.message})
    assert len(entries) == report['review_candidates_after_exclusions']
    assert report['structurally_complete_review_candidates'] >= 1000
    assert not errors, errors[:5]
    assert not truncation, truncation[:5]
    samples = []
    for query, diet in [('土豆', None), ('tofu', None), ('soup', None), ('garlic', 'pure')]:
        begin = time.perf_counter()
        rows = search(query, diet, 10)
        assert all(row['review_candidate_eligible'] for row in rows)
        if query == '土豆':
            assert any('土豆' in row['title'] for row in rows)
            assert all(not re.search(r'瘦肉|肥牛|辣子鸡', row['title']) for row in rows)
        if diet == 'pure':
            assert all(row['diet_assessment']['labels']['pure']['status'] == 'suitable' for row in rows)
        samples.append({'query': query, 'diet': diet, 'returned': len(rows),
                        'milliseconds': round((time.perf_counter() - begin) * 1000, 2),
                        'source_ids': [row['source_id'] for row in rows]})
    source = {}
    unknown = Counter()
    providers = Counter()
    for line in (BUILT / 'corpus.jsonl').read_text().splitlines():
        row = json.loads(line)
        providers[row['provider']] += 1
        if row['review_candidate_eligible']:
            terms = set()
            for label in row['diet_assessment']['labels'].values():
                terms.update(label.get('unknown', []))
            unknown.update(terms)
        if row['source_id'] == 'wikibooks:447219':
            source = row
    if source:
        assert not source['review_candidate_eligible']
        assert source['diet_assessment']['labels']['vegan']['status'] == 'not_suitable'
    result = {'schema_entries': len(entries), 'schema_valid': len(entries), 'schema_errors': [],
              'source_truncations': [], 'unique_source_ids': len(seen),
              'minimum_1000_structural_review_candidates_passed': True,
              'source_vegan_label_is_not_used_to_override_real_ingredients_passed': bool(source),
              'search_samples': samples, 'business_database_written': False, 'published': 0,
              'corpus_sha256': hashlib.sha256((BUILT / 'corpus.jsonl').read_bytes()).hexdigest()}
    (BUILT / 'checks.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    (BUILT / 'schema-validation.json').write_text(json.dumps({k: v for k, v in result.items() if k != 'search_samples'}, ensure_ascii=False, indent=2) + '\n')
    (BUILT / 'unknown-ingredients.json').write_text(json.dumps({
        'rules_version': report['rules_version'], 'corpus_sha256': result['corpus_sha256'],
        'counting': 'review candidates only; one occurrence per source recipe across three diets',
        'unique_unknown_terms': len(unknown), 'unknown_terms': unknown.most_common(),
        'source_counts': dict(providers),
    }, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
