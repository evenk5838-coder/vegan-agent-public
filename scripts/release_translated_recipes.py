"""Explicit, local inner-beta release of a fixed, structurally checked Chinese batch.

Keeps machine-translation/review flags and original attribution. This is a local
content release authorized by the user, not professional review. Normal imports
and knowledge.set_status publication gates remain unchanged.
"""
from __future__ import annotations

import argparse
from collections import Counter
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))

FORMAT = 'local-chinese-recipe-release-v1'
ALLOWED_FLAGS = {'machine_translation_unreviewed', 'missing_servings'}
AMOUNT = re.compile(r'\d|[½¼¾⅛⅜⅝⅞⅓⅔]|适量|少许|少量|按需|按口味|一撮|一把|若干|几滴')
# Conservative release quarantine, including animal instructions outside the list.
# Never classify a recipe as suitable from this screen; three diet rules stay separate.
ANIMAL_WORDS = re.compile(
    r'小牛肉|牛尾|猪肉|牛肉|羊肉|鸡肉|鸭肉|火鸡|鱼肉|鳕鱼|鱼片|鱼柳|三文鱼|金枪鱼|'
    r'凤尾鱼|鲑鱼|鲈鱼|鲱鱼|沙丁鱼|鲭鱼|鲨鱼|虾米|虾仁|虾酱|蚝油|鱼露|猪油|牛油(?!果)|'
    r'牛脂|羊油|鸡油|明胶|骨胶|培根|火腿|香肠|肉汤|骨汤|鸡汤|海鲜|海米|干贝|'
    r'蟹肉|蟹黄|牡蛎|贻贝|蛤蜊|龙虾|扇贝|鱿鱼|章鱼|鹿肉|排骨|猪肝|猪肚|牛肚|'
    r'羊排|牛排|猪排|鸡腿|鸡胸|鸡翅|鸭腿|鸭胸|五花肉|肥牛|牛腩|'
    r'\b(?:beef|veal|pork|chicken|duck|lamb|mutton|oxtail|cod|salmon|tuna|'
    r'anchov(?:y|ies)|shrimp|prawns?|lobster|crab|gelatine?|lard|bacon|ham|fish|meat|'
    r'turkey|venison|rabbit|quail|goose|geese|steak|ribs?|mussels?|clams?|'
    r'oysters?(?!\s+mushrooms?)|scallops?|squid|octopus|mackerel|halibut|haddock|trout|herring|sardines?)\b', re.I)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def prepare(entries):
    from app import knowledge, rules
    accepted, rejected, seen = [], [], set()
    for entry in entries:
        reasons = []
        clean = knowledge.validate_entry(entry)
        meta = clean['source_meta']
        key = knowledge._source_key(clean)
        original = meta.get('original_content') or {}
        flags = set(meta.get('quality_flags') or []) | set(meta.get('translation_quality_flags') or [])
        translation = meta.get('translation') or {}
        validation = translation.get('validation') or {}
        if clean['type'] != 'recipe' or not key or key in seen:
            reasons.append('missing_or_duplicate_identity')
        seen.add(key)
        if not knowledge.chinese_recipe_display_ready(clean):
            reasons.append('chinese_tutorial_incomplete')
        if not clean['ingredients'] or not clean['steps'] or any(not AMOUNT.search(s) for s in clean['ingredients']):
            reasons.append('missing_core_information_or_quantity')
        if not original.get('ingredients') or not original.get('steps'):
            reasons.append('original_missing')
        if translation.get('status') != 'complete' or not all(validation.get(k) is True for k in (
                'strict_schema', 'identities_match', 'numeric_values_match',
                'quantity_unit_pairs_match', 'known_dangerous_terms_preserved',
                'ingredients_steps_lengths_match')):
            reasons.append('translation_proof_incomplete')
        if flags - ALLOWED_FLAGS or meta.get('review_eligibility') == 'quarantined':
            reasons.append('blocking_quality_flags')
        license_data = meta.get('license') or {}
        if (not isinstance(license_data, dict) or license_data.get('id') != 'CC-BY-SA-4.0'
                or not license_data.get('attribution') or not license_data.get('url')):
            reasons.append('unsupported_attribution')
        # Scan all retained tutorial sections, not only the structured list.
        texts = [clean['title'], clean['body'], *clean['ingredients'], *clean['steps'], *clean['notes'],
                 json.dumps(original, ensure_ascii=False),
                 json.dumps(clean['conditions'].get('source_additional_sections') or [], ensure_ascii=False)]
        matches = sorted(set(ANIMAL_WORDS.findall('\n'.join(texts))))
        if matches:
            reasons.append('animal_wording_in_tutorial')
        evaluation = rules.evaluate('\n'.join([*clean['ingredients'], *original.get('ingredients', [])]), True, [], False)
        if all(v['status'] == 'not_suitable' for v in evaluation['results'].values()):
            reasons.append('all_diets_denied')
        if reasons:
            rejected.append({'title': clean['title'], 'source_key': key,
                             'reasons': reasons, 'animal_wording': matches})
            continue
        meta.update(machine_diet_labels=evaluation['results'], machine_rules_version=rules.VERSION,
                    machine_rules_sha256=hashlib.sha256((ROOT / 'backend/app/rules.py').read_bytes()).hexdigest(),
                    manual_reviewed=False, translation_reviewed=False)
        clean['source_meta'] = meta
        accepted.append(clean)
    return {'format': FORMAT, 'accepted': accepted, 'held': rejected,
            'rules_version': rules.VERSION, 'input_entries': len(entries)}


def system_rows(db):
    from app.core import Record
    from sqlalchemy import select
    return list(db.scalars(select(Record).where(Record.owner == 'system', Record.kind == 'knowledge')
                           .order_by(Record.id)))


def snapshot(rows):
    return [{'id': r.id, 'payload': r.payload} for r in rows]


def release(batch, *, authorization, expected_db_sha=None, backup_path=None):
    from app import core, knowledge
    if core.settings.app_environment != 'local-development':
        raise ValueError('This command only releases to the local inner-beta environment.')
    if not authorization.strip():
        raise ValueError('Explicit user authorization is required.')
    apply = expected_db_sha is not None
    counts = Counter()
    results = []
    batch_sha = digest(batch)
    with core.DB() as db:
        core.advisory_lock(db, 'knowledge-source-import')
        rows = system_rows(db)
        before = snapshot(rows)
        db_sha = digest(before)
        if apply:
            if expected_db_sha != db_sha:
                raise ValueError('Knowledge changed since preview; no writes made.')
            target = Path(backup_path).resolve()
            if ROOT == target or ROOT in target.parents:
                raise ValueError('Backup must be outside the repository.')
            with open(target, 'x', encoding='utf-8', opener=lambda p, f: os.open(p, f, 0o600)) as handle:
                json.dump({'format': FORMAT, 'db_sha256': db_sha, 'records': before}, handle, ensure_ascii=False)
        by_source = {}
        for row in rows:
            sid = (row.payload.get('source_meta') or {}).get('source_id')
            if sid:
                by_source.setdefault(sid, []).append(row)
        for clean in batch['accepted']:
            sid = clean['source_meta']['source_id']
            existing = by_source.get(sid, [])
            if any(knowledge.is_review_protected(r.payload) for r in existing):
                counts['already_visible_or_protected'] += 1
                continue
            if len(existing) > 1:
                counts['ambiguous_existing_identity'] += 1
                continue
            prior = existing[0] if existing else None
            if prior and knowledge._source_key(prior.payload) != knowledge._source_key(clean):
                counts['different_revision_held'] += 1
                continue
            payload = knowledge.draft_payload(copy.deepcopy(clean), prior.payload if prior else None)
            payload['status'] = 'published'
            payload['reviewer_note'] = '用户批准用于本地内测；中文机器译文，未逐条人工复核或试做。'
            payload['source_meta']['local_beta_release'] = {
                'scope': 'local_internal_test', 'authorization': authorization,
                'batch_sha256': batch_sha, 'at': core.now().isoformat(),
                'structural_validation': True, 'professional_review': False}
            payload['status_history'].append({'from': prior.payload.get('status') if prior else None,
                'to': 'published', 'at': core.now().isoformat(), 'reason': 'explicit_user_local_beta_release',
                'batch_sha256': batch_sha})
            counts['updated_existing' if prior else 'added'] += 1
            results.append({'entry_id': payload['entry_id'], 'title': payload['title'],
                            'source_id': sid, 'cuisine': payload['cuisine']})
            if apply:
                if prior:
                    prior.payload = payload
                else:
                    db.add(core.Record(owner='system', kind='knowledge', payload=payload))
        if apply:
            db.commit()
    return {'format': FORMAT, 'applied': apply, 'batch_sha256': batch_sha, 'db_sha256': db_sha,
            'counts': dict(counts), 'results': results, 'held': batch['held'],
            'external_calls': 0, 'manual_reviewed': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('--authorization', required=True)
    parser.add_argument('--expected-input-sha', required=True)
    parser.add_argument('--expected-db-sha')
    parser.add_argument('--backup', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if hashlib.sha256(args.input.read_bytes()).hexdigest() != args.expected_input_sha:
        raise SystemExit('Input SHA differs; no writes made.')
    if args.output.exists() or (args.expected_db_sha and not args.backup):
        raise SystemExit('Use a new output path and supply a backup for apply.')
    from app import core
    core.init_db()
    batch = prepare(json.loads(args.input.read_text())['entries'])
    result = release(batch, authorization=args.authorization, expected_db_sha=args.expected_db_sha,
                     backup_path=args.backup)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: v for k, v in result.items() if k not in ('results', 'held')}, ensure_ascii=False))
    print('held', len(result['held']))


if __name__ == '__main__':
    main()
