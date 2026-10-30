"""Prepare review buckets without changing publication status or trusting old labels.

Accepts --input FILE for an offline draft batch, or reads system knowledge rows.
Each recipe keeps three independent labels and a separate source-quality gate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'backend'))

from app import core, knowledge  # noqa: E402
from app.recipes import diet_labels  # noqa: E402
from app.rules import VERSION, evaluate  # noqa: E402

BUCKETS = ('reference_ready', 'needs_ingredients', 'excluded', 'special_review')
DIETS = ('pure', 'vegan', 'lacto_ovo')


def source_meta(row: dict) -> dict:
    return {**(row.get('conditions') or {}), **(row.get('source_meta') or {})}


def assess(row: dict) -> dict:
    meta = source_meta(row)
    ingredients = row.get('ingredients') or []
    if isinstance(ingredients, str):
        ingredients = ingredients.splitlines()
    steps = row.get('steps') or []
    complete = bool(ingredients and steps and row.get('source'))
    # Always recompute from source ingredient text using the current shared rule
    # engine. Never reuse canonical text produced by an older lossy normalizer.
    evidence = [*ingredients, *knowledge.original_recipe_ingredients(row)]
    result = evaluate('、'.join(evidence), bool(ingredients), [], False)
    labels = diet_labels(result)
    flags = list(meta.get('quality_flags') or [])
    language = str(meta.get('display_language') or meta.get('language') or '').casefold()
    translation_flags = list(meta.get('translation_quality_flags') or [])
    translation = meta.get('translation') or {}
    chinese_ready = (language.startswith('zh') and not translation_flags
                     and (not translation or translation.get('status') == 'complete')
                     and knowledge.chinese_recipe_display_ready(row))
    quality = {'tutorial_complete': complete, 'quality_flags': flags,
               'source_structural_complete': meta.get('source_structural_complete'),
               'manual_reviewed': bool(meta.get('manual_reviewed')),
               'publication_approved': False,
               'chinese_display_ready': chinese_ready,
               'translation_quality_flags': translation_flags}
    status = {diet: labels[diet]['status'] for diet in DIETS}
    if all(value == 'not_suitable' for value in status.values()):
        bucket = 'excluded'
    elif meta.get('review_eligibility') == 'quarantined':
        bucket = 'special_review'
    elif not complete:
        bucket = 'needs_ingredients'
    elif flags:
        bucket = 'special_review'
    elif language and not chinese_ready:
        bucket = 'special_review'
    elif any(value == 'suitable' for value in status.values()):
        bucket = 'reference_ready'
    else:
        bucket = 'needs_ingredients'
    stored_version = meta.get('machine_rules_version')
    rules_hash = hashlib.sha256((PROJECT / 'backend/app/rules.py').read_bytes()).hexdigest()
    return {'bucket': bucket, 'diet_labels': labels, 'quality': quality,
            'rules_version': result.get('rules_version', VERSION),
            'stored_machine_rules_version': stored_version,
            'stored_machine_rules_sha256': meta.get('machine_rules_sha256'),
            'stored_machine_labels_current': bool(stored_version == VERSION
                and meta.get('machine_rules_sha256') == rules_hash),
            'labels_recomputed_from_source': True,
            'eligible_diets': [diet for diet in DIETS if status[diet] == 'suitable']}


def classify(row: dict) -> str:
    return assess(row)['bucket']


def build_manifest(rows: list[dict], *, input_sha256: str, generated_from: str,
                   corpus_sha256: str | None = None) -> dict:
    buckets = {key: [] for key in BUCKETS}
    for row in rows:
        if row.get('type') != 'recipe' or row.get('status', 'draft') != 'draft':
            continue
        meta = source_meta(row)
        assessment = assess(row)
        entry = {'entry_id': row.get('entry_id'), 'title': row.get('title'),
                 'source': row.get('source'), 'language': meta.get('language'),
                 'source_id': meta.get('source_id'),
                 'source_revision': meta.get('source_revision'),
                 'license': meta.get('license'), **assessment}
        buckets[assessment['bucket']].append(entry)
    return {'rules_version': VERSION,
            'rules_source_sha256': hashlib.sha256((PROJECT / 'backend/app/rules.py').read_bytes()).hexdigest(),
            'input_sha256': input_sha256, 'corpus_sha256': corpus_sha256,
            'generated_from': generated_from, 'database_written': False,
            'publication_changed': False,
            'note': '仅AI来源预审清单，适配标签按当前规则重算；结构/标签不等于人工、专业或试做审核，未批准不得发布。',
            'counts': {key: len(value) for key, value in buckets.items()},
            'diet_counts': {diet: dict(Counter(entry['diet_labels'][diet]['status']
                            for entries in buckets.values() for entry in entries)) for diet in DIETS},
            'buckets': buckets}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', nargs='?', type=Path, default=PROJECT / 'docs/知识库/大批量建库-2026-10-26/built/release-manifest.json')
    parser.add_argument('--input', type=Path, help='Read draft JSON, without connecting to business DB')
    parser.add_argument('--corpus', type=Path, help='Attach exact corpus SHA256')
    args = parser.parse_args(argv)
    if args.input:
        raw = args.input.read_bytes()
        data = json.loads(raw)
        rows = data.get('entries') if isinstance(data, dict) else data
        if not isinstance(rows, list):
            parser.error('input must contain an entries array')
        input_hash = hashlib.sha256(raw).hexdigest()
        generated_from = 'draft-file'
    else:
        from sqlalchemy import select
        core.init_db()
        with core.DB() as db:
            rows = [r.payload for r in db.scalars(select(core.Record).where(
                core.Record.owner == 'system', core.Record.kind == 'knowledge'))]
        serialized = json.dumps(sorted(rows, key=lambda r: str(r.get('entry_id'))), ensure_ascii=False, sort_keys=True)
        input_hash = hashlib.sha256(serialized.encode()).hexdigest()
        generated_from = 'business-db-read-only-snapshot'
    report = build_manifest(rows, input_sha256=input_hash, generated_from=generated_from,
                            corpus_sha256=hashlib.sha256(args.corpus.read_bytes()).hexdigest() if args.corpus else None)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    print(json.dumps({'output': str(args.output), 'counts': report['counts'],
                      'input_sha256': input_hash, 'rules_version': VERSION}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
