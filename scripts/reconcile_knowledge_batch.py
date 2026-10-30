"""Reconcile a frozen source batch against system drafts, without publishing.

Default is a read-only plan. --apply additionally requires --expected-db-sha256
from that plan, so a changed review or source snapshot cannot be overwritten.
Only source IDs present in the supplied corpus are managed; private recipes,
unknown sources and reviewed/published/archived records remain untouched.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'backend'))
from app import core, knowledge  # noqa: E402
from app.rules import VERSION  # noqa: E402


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def snapshot_sha256(records: list[dict]) -> str:
    stable = sorted(records, key=lambda row: str(row['id']))
    return sha256(json.dumps(stable, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode())


def read_batch(batch_path: Path, corpus_path: Path) -> tuple[list[dict], dict, dict]:
    batch_raw, corpus_raw = batch_path.read_bytes(), corpus_path.read_bytes()
    data = json.loads(batch_raw)
    entries = data.get('entries') if isinstance(data, dict) else data
    if not isinstance(entries, list):
        raise ValueError('draft batch must contain entries array')
    corpus_rows = [json.loads(line) for line in corpus_raw.splitlines() if line.strip()]
    corpus = {str(row['source_id']): row for row in corpus_rows}
    if len(corpus) != len(corpus_rows):
        raise ValueError('duplicate source_id in corpus')
    identities = set()
    cleaned = []
    meta = {'input_sha256': sha256(batch_raw), 'corpus_sha256': sha256(corpus_raw),
            'rules_version': VERSION,
            'rules_source_sha256': sha256((PROJECT / 'backend/app/rules.py').read_bytes())}
    for entry in entries:
        row = knowledge.validate_entry(entry)
        sid = str((row.get('source_meta') or {}).get('source_id') or '')
        if not sid or sid in identities or sid not in corpus:
            raise ValueError(f'missing, duplicate or out-of-corpus source_id: {sid}')
        if not corpus[sid].get('review_candidate_eligible'):
            raise ValueError(f'quarantined source is included in input batch: {sid}')
        stored_version = row['source_meta'].get('machine_rules_version')
        source_version = (corpus[sid].get('diet_assessment') or {}).get('rules_version')
        if stored_version != VERSION or source_version != VERSION:
            raise ValueError(f'stale or missing rule version; rebuild frozen source batch first: {sid}')
        identities.add(sid)
        # Exact input identity is retained; stale machine labels remain marked
        # with their original version until the shared rule rebuild replaces them.
        row['conditions'] = {**row['conditions'], 'input_sha256': meta['input_sha256'],
                             'corpus_sha256': meta['corpus_sha256'],
                             'import_batch_id': meta['input_sha256'],
                             'review_eligibility': 'candidate'}
        row['source_meta'] = {**row['source_meta'], 'input_sha256': meta['input_sha256'],
                              'corpus_sha256': meta['corpus_sha256'],
                              'import_batch_id': meta['input_sha256'],
                              'review_eligibility': 'candidate'}
        cleaned.append(row)
    expected = {sid for sid, row in corpus.items() if row.get('review_candidate_eligible')}
    if identities != expected:
        raise ValueError(f'input/corpus candidate mismatch: missing={len(expected - identities)}, extra={len(identities - expected)}')
    return cleaned, corpus, meta


def plan_reconciliation(records: list[dict], entries: list[dict], corpus: dict,
                        input_meta: dict) -> tuple[dict, list[dict]]:
    targets = {(entry['source_meta']['source_id']): entry for entry in entries}
    by_source = defaultdict(list)
    protected, untouched = [], 0
    for record in records:
        payload = record['payload']
        sid = str((payload.get('source_meta') or {}).get('source_id') or
                  (payload.get('conditions') or {}).get('source_id') or '')
        if record.get('owner') != 'system' or record.get('kind') != 'knowledge' or sid not in corpus:
            untouched += 1
            continue
        by_source[sid].append(record)
    changes = []
    for sid, source_row in corpus.items():
        prior = by_source.get(sid, [])
        reviewed = [row for row in prior if knowledge.is_review_protected(row['payload'])]
        protected.extend({'record_id': row['id'], 'source_id': sid,
                          'entry_id': row['payload'].get('entry_id'),
                          'status': row['payload'].get('status')} for row in reviewed)
        drafts = sorted((row for row in prior if row not in reviewed), key=lambda row: str(row['id']))
        if sid in targets and not reviewed:
            if drafts:
                first, *duplicates = drafts
                candidate = knowledge.draft_payload(targets[sid], first['payload'])
                content_keys = set(targets[sid]) | {'status'}
                if any(first['payload'].get(key) != candidate.get(key) for key in content_keys):
                    changes.append({'action': 'update', 'record_id': first['id'], 'source_id': sid,
                                    'payload': candidate})
            else:
                duplicates = []
                changes.append({'action': 'add', 'source_id': sid,
                                'payload': knowledge.draft_payload(targets[sid])})
        else:
            duplicates = drafts
        for draft in duplicates:
            reason = ('duplicate_source_of_protected_record' if reviewed else
                      'duplicate_source_id' if sid in targets else 'not_in_frozen_review_candidates')
            payload = copy.deepcopy(draft['payload'])
            existing_audit = (payload.get('source_meta') or {}).get('reconciliation') or {}
            # A rerun has no audit churn for the same already-quarantined input.
            if (payload.get('status') == 'draft' and
                    (payload.get('source_meta') or {}).get('review_eligibility') == 'quarantined' and
                    existing_audit.get('input_sha256') == input_meta['input_sha256'] and
                    existing_audit.get('reason') == reason):
                continue
            audit = {**input_meta, 'reason': reason,
                     'source_review_candidate_eligible': bool(source_row.get('review_candidate_eligible')),
                     'source_exclusion_evidence': source_row.get('explicit_animal_evidence') or [],
                     'source_category_exclusion_evidence': source_row.get('source_category_exclusion_evidence') or [],
                     'at': core.now().isoformat()}
            payload['source_meta'] = {**(payload.get('source_meta') or {}),
                                      'review_eligibility': 'quarantined', 'reconciliation': audit}
            payload['conditions'] = {**(payload.get('conditions') or {}), 'review_eligibility': 'quarantined'}
            payload['status'] = 'draft'
            payload['status_history'] = list(payload.get('status_history') or []) + [
                {'from': draft['payload'].get('status'), 'to': 'draft',
                 'reason': reason, 'at': audit['at']}]
            changes.append({'action': 'quarantine', 'record_id': draft['id'],
                            'source_id': sid, 'payload': payload})
    public = {'dry_run': True, 'publication_changed': False, **input_meta,
              'database_snapshot_sha256': snapshot_sha256(records),
              'candidate_input_count': len(entries), 'corpus_count': len(corpus),
              'counts': dict(Counter(change['action'] for change in changes)),
              'protected_count': len(protected), 'protected': protected,
              'unmanaged_untouched_count': untouched,
              'changes': [{key: value for key, value in change.items() if key != 'payload'} for change in changes]}
    return public, changes


def apply_reconciliation(db, changes: list[dict]):
    for change in changes:
        if change['action'] == 'add':
            db.add(core.Record(owner='system', kind='knowledge', payload=change['payload']))
        else:
            row = db.get(core.Record, change['record_id'])
            if row is None or row.owner != 'system' or row.kind != 'knowledge' or knowledge.is_review_protected(row.payload):
                raise ValueError('review status changed during reconciliation; apply aborted')
            row.payload = change['payload']
    db.flush()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('batch', type=Path)
    parser.add_argument('corpus', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--expected-db-sha256')
    args = parser.parse_args(argv)
    if args.apply and not args.expected_db_sha256:
        parser.error('--apply requires the reviewed --expected-db-sha256 from a dry run')
    entries, corpus, meta = read_batch(args.batch, args.corpus)
    core.init_db()
    from sqlalchemy import select
    with core.DB() as db:
        if args.apply:
            # Shares the import lock; row locks protect concurrently reviewed rows.
            core.advisory_lock(db, 'knowledge-source-import')
        query = select(core.Record).where(core.Record.kind == 'knowledge', core.Record.owner == 'system')
        if args.apply:
            query = query.with_for_update()
        records = [{'id': row.id, 'owner': row.owner, 'kind': row.kind, 'payload': row.payload}
                   for row in db.scalars(query)]
        report, changes = plan_reconciliation(records, entries, corpus, meta)
        if args.apply:
            if report['database_snapshot_sha256'] != args.expected_db_sha256:
                raise ValueError('database changed since dry run; regenerate and review the plan')
            apply_reconciliation(db, changes)
            db.commit()
            report['dry_run'] = False
            report['database_written'] = True
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=1) + '\n')
    print(json.dumps({key: value for key, value in report.items() if key not in ('changes', 'protected')}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
