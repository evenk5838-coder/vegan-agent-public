"""Preview verified Chinese recipe translations; explicitly update existing system drafts.

Default is offline, with no application/settings/DB import. --database-plan is
read-only. --apply requires both reviewed input and database snapshot hashes.
No new records, publication, upstream requests or source/audit replacement.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import translate_recipe_corpus as tr

FORMAT = 'recipe-translation-draft-import-v1'
MAX_FILE_BYTES = 128 * 1024 * 1024
MAX_PROOF_BYTES = 512 * 1024 * 1024
MAX_ROWS = 10_000
DISPLAY_FIELDS = ('title', 'ingredients', 'steps', 'notes', 'body', 'servings_text')
MANDATORY_FLAGS = ('machine_translation_unreviewed',)
IMPORTED_CODE = {str(path.resolve()): hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in (Path(tr.__file__), ROOT / 'backend/app/rules.py', Path(__file__))}


class ImportError(ValueError):
    pass


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_read(path: Path, fingerprints: dict) -> bytes:
    target = path.resolve()
    if (target.suffix not in ('.json', '.jsonl', '.py') or any(part.startswith('.env') for part in target.parts)
            or target == ROOT / 'data' or ROOT / 'data' in target.parents):
        raise ImportError('Only public JSON proof/source files are allowed; no environment or business data files')
    if not target.is_file() or target.stat().st_size > MAX_FILE_BYTES:
        raise ImportError('Input/proof file is missing or exceeds the size limit')
    raw = target.read_bytes()
    if len(raw) > MAX_FILE_BYTES:
        raise ImportError('Input/proof file exceeds the size limit')
    fingerprints[str(target)] = sha(raw)
    return raw


def unchanged(fingerprints: dict):
    for name, expected in fingerprints.items():
        path = Path(name)
        if not path.is_file() or sha(path.read_bytes()) != expected:
            raise ImportError('Input, proof or validator changed; take a fixed snapshot and preview again')


def chinese_display(value, approved_texts=()) -> bool:
    """Accept Chinese prose with names/brands/units, not foreign prose sentences.

    Fidelity and quantities have already passed the source-bound translator.
    This is only a conservative display check, not a second translation judge.
    Audited source-link annotations are allowed only at their exact proven text.
    """
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return True
        for approved in sorted(approved_texts, key=len, reverse=True):
            if approved:
                text = text.replace(approved, '已核对的来源说明')
        if re.search(r'来源附带的[^\n。]*互链', text):
            return False
        if re.search(r'(?:^|\n)\s*[a-z]{2,3}:[^\s\u3400-\u9fff]+', text) or '{{' in text:
            return False
        without_links = re.sub(r'https?://[^\s<>]+', '', text)
        # Preserve explicit original-name/brand explanations and common unit
        # symbols. Arbitrary English sentences do not gain this exception.
        without_links = re.sub(r'[（(][^()（）\n]{0,120}(?:原名|音译|英文名|品牌)[^()（）\n]{0,120}[）)]', '', without_links)
        without_links = re.sub(r'(?<![A-Za-z])(?:kg|mg|g|ml|cl|dl|l|oz|lb|lbs|cm|mm|m|kcal|kj|°?[cf])(?![A-Za-z])',
                               '', without_links, flags=re.I)
        if not re.search(r'[\u3400-\u9fff]', text):
            return not any(character.isalpha() for character in without_links)
        # Chinese explanations may faithfully retain multiword dish names,
        # brands, pronunciations and botanical names. Do not classify all
        # Latin runs as untranslated prose. Only unmistakable prose patterns
        # add a display skip beyond the source-bound fidelity checks.
        runs = re.findall(r"[A-Za-z][A-Za-z'’\-]*(?:[\s\d.,:/–—\-]+[A-Za-z][A-Za-z'’\-]*)+", without_links)
        imperative = r'(?:cook|serve|mix|add|heat|place|stir|bake|boil|cover|reduce|remove|wash|cut|bring|turn|preheat|pour|sprinkle|combine|drain|dice|peel|chop|allow|use|keep|store|put|leave)'
        prose_words = {'the', 'this', 'these', 'that', 'those', 'it', 'is', 'are', 'was', 'were',
                       'with', 'for', 'from', 'into', 'until', 'then', 'but', 'still', 'should', 'must',
                       'will', 'would', 'could', 'can', 'you', 'your'}
        for run in runs:
            words = re.findall(r'[A-Za-z]+', run.lower())
            if re.match(imperative + r'\b', run, re.I) or len(prose_words.intersection(words)) >= 2:
                return False
        return True
    if isinstance(value, list):
        return all(chinese_display(item, approved_texts) for item in value)
    if isinstance(value, dict):
        return all(key == 'kind' or chinese_display(item, approved_texts) for key, item in value.items())
    return value is None or (type(value) in (int, float))


def check_display(row: dict, approved_texts=()):
    for field in DISPLAY_FIELDS:
        if not chinese_display(row.get(field), approved_texts):
            raise ImportError('Translated display field still contains untranslated prose: ' + field)
    if not chinese_display(row.get('review_sections'), approved_texts):
        raise ImportError('Translated additional sections still contain untranslated prose')
    if not isinstance(row.get('title'), str) or not row['title'].strip():
        raise ImportError('Translated title is missing')
    for field, maximum in (('ingredients', 60), ('steps', 100), ('notes', 40)):
        values = row.get(field) or []
        if (not isinstance(values, list) or len(values) > maximum
                or any(not isinstance(item, str) or not item.strip() or len(item) > 8000 for item in values)):
            raise ImportError('Translated list exceeds application limits or has invalid lines: ' + field)
    # Full translation is independent of tutorial completeness. The strict
    # source/checkpoint comparison preserves empty/missing source lists; never
    # invent ingredients/steps merely to make a historical source complete.
    for field, maximum in (('title', 200), ('body', 8000), ('servings_text', 800)):
        value = row.get(field) or ''
        if not isinstance(value, str) or len(value) > maximum:
            raise ImportError('Translated field exceeds application limits: ' + field)


def _materialized(source: dict, translated: dict, plan: dict, editorial_flags: dict, editorial_refs: dict) -> dict:
    original = source['original']
    validation = {'strict_schema': True, 'identities_match': True, 'numeric_values_match': True,
                  'quantity_unit_pairs_match': True, 'known_dangerous_terms_preserved': True,
                  'ingredients_steps_lengths_match': True, 'semantic_quality_manually_reviewed': False}
    original_content = {**source['fields'], 'servings_text': source['fields'].get('servings'),
                        'language': original.get('source_language'), 'source_sha256': source['source_sha256']}
    flags = sorted({'machine_translation_unreviewed',
                    *(original.get('source_meta') or {}).get('translation_quality_flags', []),
                    *editorial_flags.get(source['source_key'], set())})
    return {**original, **translated, 'servings_text': translated.get('servings'),
            **{key: source[key] for key in ('source_id', 'source_revision', 'source_sha256', 'source_key')},
            'source_meta': {**(original.get('source_meta') or {}), 'original_content': original_content,
                            'translation_quality_flags': flags,
                            'translation_corrections': editorial_refs.get(source['source_key'], [])},
            'display_language': 'zh-CN',
            'translation': {'status': 'complete', 'model': tr.MODEL, 'source_sha256': source['source_sha256'],
                            'output_sha256': tr.digest(translated), 'validation': validation},
            'translation_quality_flags': flags, 'status': 'draft',
            'manual_reviewed': False, 'publication_approved': False, 'plan_sha256': plan['plan_sha256']}


def load_inputs(plan_path: Path, translated_path: Path, proof_dir: Path | None = None) -> dict:
    fingerprints = {}
    plan_raw = safe_read(plan_path, fingerprints)
    translated_raw = safe_read(translated_path, fingerprints)
    safe_read(Path(tr.__file__), fingerprints)
    safe_read(ROOT / 'backend/app/rules.py', fingerprints)
    safe_read(Path(__file__), fingerprints)
    if any(fingerprints[path] != expected for path, expected in IMPORTED_CODE.items()):
        raise ImportError('Validator changed after this process imported it; restart the offline preview')
    annotation_path = tr.SOURCE_ANNOTATION_MANIFEST
    if annotation_path.exists():
        safe_read(annotation_path, fingerprints)
    plan = tr.load_plan(plan_path, annotation_manifest_path=annotation_path)
    frozen_plan = copy.deepcopy(plan)
    for job in frozen_plan['jobs']:
        job.pop('_approved_source_annotations', None)
    if json.loads(plan_raw) != frozen_plan or len(plan.get('sources', [])) > MAX_ROWS:
        raise ImportError('Plan changed or exceeds the source limit')
    proof_dir = (proof_dir or plan_path.parent).resolve()
    ledger_path = proof_dir / 'ledger.json'
    safe_read(ledger_path, fingerprints)
    recovery_path = proof_dir / 'offline-recovery.json'
    if recovery_path.exists():
        recovery = json.loads(safe_read(recovery_path, fingerprints))
        files = recovery.get('legacy_files') or {}
        if set(files) != {'plan', 'response', 'ledger'}:
            raise ImportError('Offline recovery has incomplete legacy proof')
        for record in files.values():
            safe_read(Path(record['path']), fingerprints)
    status_path = proof_dir / 'translation-status.json'
    if status_path.exists():
        safe_read(status_path, fingerprints)
    for job in plan['jobs']:
        for directory in ('validated', 'responses', 'receipts', 'adoptions', 'content-quarantines', 'discarded'):
            path = proof_dir / directory / (job['job_id'] + '.json')
            if path.exists():
                safe_read(path, fingerprints)
    if sum(Path(path).stat().st_size for path in fingerprints) > MAX_PROOF_BYTES:
        raise ImportError('Combined input/proof exceeds the size limit')
    # Constructor is read-only and revalidates receipts, hashes and recovery.
    ledger = tr.Ledger(ledger_path, plan, 0, 0)
    texts = {item['segment_id']: item['text'] for item in ledger.recovery['segments']} if ledger.recovery else {}
    by_source = defaultdict(list)
    editorial_flags, editorial_refs, approved_display = defaultdict(set), defaultdict(list), defaultdict(set)
    successful = {'succeeded', 'offline_recovered', 'offline_verified_from_quarantine', 'content_quarantined'}
    for job in plan['jobs']:
        for segment in job['segments']:
            by_source[segment['source_key']].append(segment)
            annotation = job.get('_approved_source_annotations', {}).get(segment['segment_id'])
            if annotation is not None:
                editorial_flags[segment['source_key']].add('source_parser_artifact_unreviewed')
                approved_display[segment['source_key']].add(annotation['text'])
        entry = ledger.state['jobs'].get(job['job_id'], {})
        if entry.get('status') == 'content_quarantined':
            _, accepted, _ = tr.read_content_quarantine(tr.effective_job(job, ledger.recovery), proof_dir)
            texts.update({item['segment_id']: item['text'] for item in accepted['translations']})
            continue
        if entry.get('status') not in successful:
            continue
        effective = tr.effective_job(job, ledger.recovery)
        checkpoint = tr.validate_internal_response(effective if effective['segments'] else job,
            (proof_dir / 'validated' / (job['job_id'] + '.json')).read_text())
        texts.update({item['segment_id']: item['text'] for item in checkpoint['translations']})
        adoption_path = proof_dir / 'adoptions' / (job['job_id'] + '.json')
        if adoption_path.exists():
            adoption = json.loads(adoption_path.read_text())
            overlay = adoption.get('correction_overlay')
            if overlay is not None:
                for correction in overlay['corrections']:
                    key = correction['source_key']
                    editorial_flags[key].update(overlay['quality_flags'])
                    approved_display[key].add(correction['corrected_text'])
                    editorial_refs[key].append({'segment_id': correction['segment_id'],
                        'operator': overlay['operator'], 'overlay_sha256': tr.digest(overlay), 'reason': correction['reason']})
    sources, expected, identities = {}, {}, set()
    for source in plan['sources']:
        identity = tr.source_identity(source['original'])
        if (any(source.get(key) != value for key, value in identity.items())
                or source['fields'] != tr.human_fields(source['original'])
                or source['source_id'] in identities or not str(source['original'].get('source_language', '')).startswith('en')
                or type(source['source_revision']) not in (str, int) or source['source_revision'] == ''):
            raise ImportError('Plan source identity, version or original fields are invalid')
        identities.add(source['source_id'])
        sources[source['source_key']] = source
        segments = by_source.get(source['source_key'], [])
        paths = defaultdict(list)
        for segment in segments:
            paths[tuple(segment['path'])].append(segment)
        original_paths = {tuple(path): value for path, value in tr.walk_strings(source['fields'])}
        if set(paths) != set(original_paths):
            raise ImportError('Plan source paths do not cover the full human-readable content')
        for path, pieces in paths.items():
            pieces.sort(key=lambda value: value['piece'])
            if ([piece['piece'] for piece in pieces] != list(range(len(pieces)))
                    or ''.join(piece['text'] for piece in pieces) != original_paths[path]):
                raise ImportError('Plan segments do not reproduce their original source field')
        if any(segment['segment_id'] not in texts for segment in segments):
            continue
        translated = copy.deepcopy(source['fields'])
        for path, pieces in paths.items():
            target = translated
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = ''.join(texts[piece['segment_id']] for piece in pieces)
        expected[source['source_key']] = _materialized(source, translated, plan, editorial_flags, editorial_refs)
    if set(by_source) - set(sources):
        raise ImportError('Unknown source key in translation plan')
    rows, seen, skipped = [], set(), []
    for line in translated_raw.splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict) or row.get('source_key') in seen:
            raise ImportError('Translated snapshot differs from its fully validated checkpoint/source proof')
        proven = expected.get(row.get('source_key'))
        if row != proven:
            changed = sorted(key for key in set(row) | set(proven or {}) if row.get(key) != (proven or {}).get(key))
            raise ImportError('Translated snapshot differs from validated proof fields: ' + ','.join(changed))
        seen.add(row['source_key'])
        if len(seen) > MAX_ROWS:
            raise ImportError('Translated snapshot exceeds the row limit')
        try:
            check_display(row, approved_display[row['source_key']])
        except ImportError as error:
            skipped.append({'source_id': row['source_id'], 'source_revision': row['source_revision'],
                            'reason': 'display_not_importable', 'detail': str(error)})
            continue
        rows.append(row)
    if not seen:
        raise ImportError('No complete verified translated rows in the supplied snapshot')
    unchanged(fingerprints)
    meta = {'format': FORMAT, 'plan_sha256': plan['plan_sha256'],
            'plan_file_sha256': sha(plan_raw), 'translated_file_sha256': sha(translated_raw),
            'fidelity_version': tr.FIDELITY_VERSION, 'proof_files': fingerprints,
            'complete_input_rows': len(seen), 'importable_input_rows': len(rows),
            'input_skipped_rows': len(skipped), 'input_skips': skipped, 'plan_sources': len(sources),
            'receipt_origins': dict(Counter(entry.get('settlement_origin') or entry.get('status')
                                           for entry in ledger.state['jobs'].values()
                                           if entry.get('status') in successful))}
    meta['input_sha256'] = tr.digest(meta)
    return {'rows': rows, 'meta': meta}


def _metadata(payload: dict) -> dict:
    return {**(payload.get('conditions') or {}), **(payload.get('source_meta') or {})}


def _protected(payload: dict) -> bool:
    meta = _metadata(payload)
    return bool(payload.get('status') != 'draft' or payload.get('manual_reviewed')
                or (payload.get('conditions') or {}).get('manual_reviewed') or meta.get('manual_reviewed')
                or payload.get('translation_reviewed') or (payload.get('conditions') or {}).get('translation_reviewed')
                or meta.get('translation_reviewed'))


def _existing_translation(payload: dict, row: dict) -> bool:
    translation = _metadata(payload).get('translation') or {}
    return bool(translation and translation.get('status') == 'complete'
                and translation.get('source_sha256') == row['source_sha256']
                and translation.get('output_sha256') == row['translation']['output_sha256'])


def update_payload(payload: dict, row: dict, meta: dict) -> tuple[dict | None, str]:
    old = _metadata(payload)
    if _protected(payload):
        return None, 'protected_review_or_status'
    if old.get('translation'):
        if (_existing_translation(payload, row)
                and all(payload.get(field) == (row.get(field) or ('' if field in ('body', 'servings_text') else []))
                        for field in DISPLAY_FIELDS if field != 'title') and payload.get('title') == row['title']):
            return None, 'already_same_translation'
        return None, 'protected_translation_version'
    if any(str(location.get(key) or '').startswith('zh')
           for location in (payload, payload.get('conditions') or {}, payload.get('source_meta') or {})
           for key in ('language', 'display_language')):
        return None, 'protected_untranslated_chinese_record'
    original = row['source_meta']['original_content']
    for field in ('title', 'ingredients', 'steps', 'notes', 'body'):
        default = [] if field in ('ingredients', 'steps', 'notes') else ''
        if (payload.get(field) or default) != (original.get(field) or default):
            return None, 'draft_source_content_changed'
    if payload.get('source') != row.get('source'):
        return None, 'source_reference_changed'
    if payload.get('nutrition_notes') and not chinese_display(payload['nutrition_notes']):
        return None, 'untranslated_existing_display_field'
    prior_original = old.get('original_content') or {}
    if not isinstance(prior_original, dict) or any(key in prior_original and prior_original[key] != value
                                                for key, value in original.items()):
        return None, 'original_evidence_conflict'
    candidate = copy.deepcopy(payload)
    for field in DISPLAY_FIELDS:
        candidate[field] = copy.deepcopy(row.get(field) or ('' if field in ('body', 'servings_text') else []))
    metadata = copy.deepcopy(payload.get('source_meta') or {})
    locations = (payload, payload.get('conditions') or {}, payload.get('source_meta') or {},
                 row, row.get('conditions') or {}, row.get('source_meta') or {})
    translation_flags = list(dict.fromkeys([flag for location in locations
                                           for flag in (location.get('translation_quality_flags') or [])] + list(MANDATORY_FLAGS)))
    quality = list(dict.fromkeys([flag for location in locations for flag in (location.get('quality_flags') or [])]
                                + translation_flags))
    audit = {'format': FORMAT, 'plan_sha256': meta['plan_sha256'], 'input_sha256': meta['input_sha256'],
             'source_key': row['source_key'], 'source_sha256': row['source_sha256'],
             'output_sha256': row['translation']['output_sha256'], 'receipt_origins': meta['receipt_origins'],
             'at': datetime.now(timezone.utc).isoformat(), 'publication_changed': False}
    metadata.update(display_language='zh-CN', translation=copy.deepcopy(row['translation']),
                    original_content={**copy.deepcopy(original), **copy.deepcopy(prior_original)},
                    translation_quality_flags=translation_flags, quality_flags=quality,
                    source_additional_sections=copy.deepcopy(row.get('review_sections') or []),
                    translation_import=audit)
    if type(row.get('structural_complete')) is bool:
        metadata['source_structural_complete'] = row['structural_complete']
    corrections = [*(metadata.get('translation_corrections') or []),
                   *((row.get('source_meta') or {}).get('translation_corrections') or [])]
    metadata['translation_corrections'] = list({tr.canonical(value): copy.deepcopy(value) for value in corrections}.values())
    candidate['source_meta'] = metadata
    candidate['translation_import_history'] = list(candidate.get('translation_import_history') or []) + [audit]
    return candidate, 'update_existing_draft'


def plan_updates(records: list[dict], bundle: dict, target_fingerprint: str = '') -> tuple[dict, list[dict]]:
    source_ids = {row['source_id'] for row in bundle['rows']}
    relevant = [record for record in records if record.get('owner') == 'system' and record.get('kind') == 'knowledge'
                and _metadata(record['payload']).get('source_id') in source_ids]
    snapshot = {'target_fingerprint': target_fingerprint, 'records': sorted(relevant, key=lambda record: record['id'])}
    results, changes = copy.deepcopy(bundle['meta'].get('input_skips') or []), []
    for translated in bundle['rows']:
        related = [record for record in relevant if _metadata(record['payload']).get('source_id') == translated['source_id']]
        matches = [record for record in related
                   if tr.canonical(_metadata(record['payload']).get('source_revision')) == tr.canonical(translated['source_revision'])]
        conflict = any(any(key in (record['payload'].get('conditions') or {})
                           and key in (record['payload'].get('source_meta') or {})
                           and tr.canonical(record['payload']['conditions'][key]) != tr.canonical(record['payload']['source_meta'][key])
                           for key in ('source_id', 'source_revision')) for record in related)
        if conflict:
            reason = 'source_metadata_conflict'
        elif any(_protected(record['payload']) for record in related):
            reason = 'protected_review_or_status'
        elif len(matches) != 1:
            reason = 'missing_existing_source' if not related else 'source_revision_mismatch' if not matches else 'duplicate_source_revision'
        elif matches[0]['payload'].get('type') != 'recipe':
            reason = 'not_recipe'
        else:
            candidate, reason = update_payload(matches[0]['payload'], translated, bundle['meta'])
            if candidate is not None:
                changes.append({'record_id': matches[0]['id'], 'source_id': translated['source_id'],
                                'source_revision': translated['source_revision'], 'payload': candidate})
        results.append({'source_id': translated['source_id'], 'source_revision': translated['source_revision'], 'reason': reason})
    report = {**bundle['meta'], 'database_snapshot_sha256': tr.digest(snapshot), 'database_written': False,
              'publication_changed': False, 'created': 0, 'deleted': 0, 'updated': len(changes),
              'counts': dict(Counter(result['reason'] for result in results)), 'results': results}
    return report, changes


def database_report(bundle: dict, *, apply=False, expected_input_sha256=None, expected_db_sha256=None, db=None) -> dict:
    # Application settings are deliberately lazy and reachable only via explicit DB mode.
    sys.path.insert(0, str(ROOT / 'backend'))
    from app import core
    from sqlalchemy import select, text
    if apply and (expected_input_sha256 != bundle['meta']['input_sha256'] or not expected_db_sha256):
        raise ImportError('Apply requires the reviewed exact input and database SHA-256')
    core.init_db()
    if db is None:
        with core.DB() as connection:
            report = database_report(bundle, apply=apply, expected_input_sha256=expected_input_sha256,
                                     expected_db_sha256=expected_db_sha256, db=connection)
            if apply:
                connection.commit()
            return report
    if apply:
        core.advisory_lock(db, 'knowledge-source-import')
    else:
        db.execute(text('SET TRANSACTION READ ONLY'))
    query = select(core.Record).where(core.Record.owner == 'system', core.Record.kind == 'knowledge').order_by(core.Record.id)
    rows = list(db.scalars(query.with_for_update() if apply else query))
    records = [{'id': row.id, 'owner': row.owner, 'kind': row.kind, 'payload': copy.deepcopy(row.payload)} for row in rows]
    fingerprint = sha(str(core.engine.url.render_as_string(hide_password=True)).encode())
    report, changes = plan_updates(records, bundle, fingerprint)
    if apply:
        if report['database_snapshot_sha256'] != expected_db_sha256:
            raise ImportError('Database changed since preview; regenerate and review the database plan')
        unchanged(bundle['meta']['proof_files'])
        existing = {row.id: row for row in rows}
        for change in changes:
            existing[change['record_id']].payload = change['payload']
        db.flush()
        unchanged(bundle['meta']['proof_files'])
        report['database_written'] = bool(changes)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--translated', type=Path, required=True)
    parser.add_argument('--proof-dir', type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--database-plan', action='store_true')
    mode.add_argument('--apply', action='store_true')
    parser.add_argument('--expected-input-sha256')
    parser.add_argument('--expected-db-sha256')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    if args.apply and not (args.expected_input_sha256 and args.expected_db_sha256):
        parser.error('--apply requires --expected-input-sha256 and --expected-db-sha256')
    try:
        bundle = load_inputs(args.plan, args.translated, args.proof_dir)
        destination = args.output.resolve() if args.output else None
        if destination and (str(destination) in bundle['meta']['proof_files'] or destination.exists()
                            or destination.suffix != '.json' or any(part.startswith('.env') for part in destination.parts)
                            or destination == ROOT / 'data' or ROOT / 'data' in destination.parents):
            raise ImportError('Report must be a new file and cannot overwrite inputs or proof')
        if args.database_plan or args.apply:
            report = database_report(bundle, apply=args.apply, expected_input_sha256=args.expected_input_sha256,
                                     expected_db_sha256=args.expected_db_sha256)
        else:
            report = {**bundle['meta'], 'mode': 'offline_preview', 'database_accessed': False,
                      'database_written': False, 'publication_changed': False, 'created': 0, 'deleted': 0,
                      'counts': dict(Counter(item['reason'] for item in bundle['meta']['input_skips'])),
                      'results': copy.deepcopy(bundle['meta']['input_skips'])}
        if destination:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open('x', encoding='utf-8') as handle:
                json.dump(report, handle, ensure_ascii=False, indent=2)
                handle.write('\n')
        print(json.dumps({key: value for key, value in report.items() if key not in ('proof_files', 'results')}, ensure_ascii=False))
        return 0
    except (ImportError, tr.TranslationError, ValueError, OSError, KeyError, TypeError) as error:
        print('Translation draft import refused: ' + str(error), file=sys.stderr)
        return 1
    except Exception:
        print('Database operation did not complete cleanly; inspect the target before any retry. No connection details printed.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
