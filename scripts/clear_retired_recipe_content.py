"""User-authorized local recipe cleanup, retaining exactly the approved twenty.

No provider requests or frontend changes. Only the twenty retained tutorials are
exported; retired bulk content is not copied into a new backup or archive.
"""
import hashlib
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def purge_records(db, approved):
    from app import core
    from sqlalchemy import delete, select, func
    core.advisory_lock(db, 'knowledge-source-import')
    rows = list(db.scalars(select(core.Record).where(
        core.Record.owner == 'system', core.Record.kind == 'knowledge',
        core.Record.payload['type'].as_string() == 'recipe').with_for_update()))
    ids = {v['entry_id'] for v in approved}
    if len(ids) != 20:
        raise ValueError('Exactly twenty distinct approved IDs required')
    kept = [r for r in rows if r.payload.get('entry_id') in ids]
    if len(kept) != 20:
        raise ValueError('Approved twenty could not all be matched; nothing deleted')
    by_id = {v['entry_id']: v for v in approved}
    for row in kept:
        expected = by_id[row.payload['entry_id']]
        if (row.payload.get('title') != expected['title'] or row.payload.get('source') != expected['source']
                or row.payload.get('status') != 'published'):
            raise ValueError('Approved identity/source/status changed; nothing deleted')
    preserved = {r.id: digest(r.payload) for r in kept}
    retained = [{'record_id': r.id, 'payload': r.payload} for r in kept]
    retired_ids = [r.id for r in rows if r.id not in preserved]
    if retired_ids:
        db.execute(delete(core.Record).where(core.Record.id.in_(retired_ids)))
    # User explicitly requested all other recipes/candidates removed. Delete
    # these kinds without loading their personal content or touching food/chat.
    extra = dict(db.execute(select(core.Record.kind, func.count()).where(
        core.Record.kind.in_(('recipe', 'candidate'))).group_by(core.Record.kind)).all())
    db.execute(delete(core.Record).where(core.Record.kind.in_(('recipe', 'candidate'))))
    db.flush()
    remaining = list(db.scalars(select(core.Record).where(
        core.Record.owner == 'system', core.Record.kind == 'knowledge',
        core.Record.payload['type'].as_string() == 'recipe')))
    if {r.id: digest(r.payload) for r in remaining} != preserved:
        raise ValueError('Preservation invariant failed; transaction must roll back')
    return retained, {'deleted_public_recipes': len(retired_ids), 'deleted_other_recipe_records': extra,
                      'retained': len(remaining), 'retained_payloads_unchanged': True}


def main():
    from app import core
    from sqlalchemy import create_engine
    from sqlalchemy.engine import URL
    from sqlalchemy.orm import Session
    if core.settings.app_environment != 'local-development':
        raise ValueError('This cleanup is authorized only for the local development project')
    receipt = ROOT / 'docs/evidence/存量小批试跑-2026-10-27/20道中文菜谱-实际发布.json'
    approval = json.loads(receipt.read_text())
    target = ROOT / 'docs/知识库/保留菜谱20道-2026-10-28'
    target.mkdir(parents=True, exist_ok=True)
    old = ROOT / 'docs/知识库/候选/HowToCook-内测来源核对20条-2026-10-26.json'
    audit = json.loads(old.read_text())
    # Preserve the complete original markdown only for the twenty being kept.
    shutil.copy2(old, target / '20道-原始来源核对.json')
    shutil.copy2(receipt, target / '20道-用户批准发布记录.json')
    for entry in audit['entries']:
        meta = entry['conditions']
        relative = meta['source_id'].removeprefix('howtocook:')
        raw = ROOT / 'docs/知识库/大批量建库-2026-10-26/raw/howtocook' / relative
        if hashlib.sha256(raw.read_bytes()).hexdigest() != meta['reviewed_source_sha256']:
            raise ValueError('Retained source checksum mismatch')
        destination = target / 'source-text' / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(raw, destination)
    url = core.settings.database_url or URL.create('postgresql+psycopg', username='postgres',
        database='postgres', query={'host': '/private/tmp/shushi-pg-d6cd8043d3989648'})
    engine = create_engine(url)
    with Session(engine) as db:
        try:
            retained, report = purge_records(db, approval['entries'])
            (target / '20道-当前公开记录.json').write_text(json.dumps(retained, ensure_ascii=False, indent=2)+'\n')
            db.commit()
        except Exception:
            db.rollback()
            raise
    paths = [ROOT / 'docs/知识库' / name for name in
             ('大批量建库-2026-10-26', '批量候选-2026-10-26', '全量审阅-2026-10-28')]
    paths.append(ROOT / 'data/recipe-sources')
    removed_files, removed_bytes = 0, 0
    removed_paths = []
    for path in paths:
        if path.is_symlink() or not path.is_relative_to(ROOT):
            raise ValueError('Refuse unexpected cleanup path')
        if not path.exists():
            continue
        files = [f for f in path.rglob('*') if f.is_file()]
        removed_files += len(files)
        removed_bytes += sum(f.stat().st_size for f in files)
        shutil.rmtree(path)
        removed_paths.append(str(path.relative_to(ROOT)))
    # Explicit recipe-only temporary exports/backups from this conversation.
    for name in ('shushi-before-translated-release-20261028.json',
                 'shushi-public-knowledge-review-20261028.json',
                 'shushi-before-20-approved-recipes-20261027.json',
                 'shushi-before-indian-cuisine-correction-20261027.json'):
        path = Path('/private/tmp') / name
        if path.is_file() and not path.is_symlink():
            removed_files += 1; removed_bytes += path.stat().st_size
            path.unlink(); removed_paths.append(str(path))
    report.update(at=core.now().isoformat(), authorization='用户明确要求删除所有旧菜谱，仅保留已批准20道，并回复开始',
                  deleted_files=removed_files, deleted_bytes=removed_bytes, deleted_roots=removed_paths,
                  external_calls=0, frontend_changed=False, retired_content_archived=False,
                  retained_titles=[v['payload']['title'] for v in retained])
    (target / '清理结果.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
