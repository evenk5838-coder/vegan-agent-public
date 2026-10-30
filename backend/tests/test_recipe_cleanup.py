"""Deletion is limited to recipes; the exact twenty and other business kinds survive."""
import copy

import pytest
from sqlalchemy import select

from app import core
from scripts.clear_retired_recipe_content import purge_records


def seed(db):
    approved = [{'entry_id': f'retained-{i}', 'title': f'测试保留{i}',
                 'source': f'https://example.org/keep/{i}'} for i in range(20)]
    for item in approved:
        db.add(core.Record(owner='system', kind='knowledge',
                           payload={**item, 'type': 'recipe', 'status': 'published', 'steps': ['测试步骤']}))
    db.add_all([
        core.Record(owner='system', kind='knowledge', payload={'type': 'recipe', 'entry_id': 'retired'}),
        core.Record(owner='system', kind='knowledge', payload={'type': 'nutrition', 'title': '其他知识'}),
        core.Record(owner='alice', kind='food', payload={'name': '保留食材'}),
        core.Record(owner='alice', kind='chat_message', payload={'text': '保留对话'}),
        core.Record(owner='alice', kind='recipe', payload={'title': '待清理测试菜谱'}),
        core.Record(owner='alice', kind='candidate', payload={'title': '待清理测试候选'}),
    ])
    db.commit()
    return approved


def test_cleanup_exact_twenty_preserves_unrelated_records_and_is_idempotent(client):
    with core.DB() as db:
        approved = seed(db)
        before = {r.id: copy.deepcopy(r.payload) for r in db.scalars(select(core.Record))
                  if r.kind in ('food', 'chat_message') or r.payload.get('type') == 'nutrition'}
        retained, result = purge_records(db, approved)
        assert len(retained) == 20 and result['deleted_public_recipes'] == 1
        assert result['deleted_other_recipe_records'] == {'candidate': 1, 'recipe': 1}
        db.commit()
        assert {r.id: r.payload for r in db.scalars(select(core.Record))
                if r.id in before} == before
        _, again = purge_records(db, approved)
        assert again['deleted_public_recipes'] == 0 and again['retained'] == 20


def test_cleanup_identity_mismatch_aborts_before_deletion(client):
    with core.DB() as db:
        approved = seed(db)
        approved[0]['source'] = 'https://example.org/wrong'
        with pytest.raises(ValueError, match='changed'):
            purge_records(db, approved)
        db.rollback()
        assert len(list(db.scalars(select(core.Record)))) == 26
