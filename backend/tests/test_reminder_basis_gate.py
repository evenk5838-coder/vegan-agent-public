from datetime import timedelta

import pytest
from sqlalchemy import select

from app import core, storage_rules, worker


@pytest.mark.parametrize('patch', [
    {'deadline_source': 'rule', 'rule_id': 'cooked-refrigerated', 'rule_version': 'old-rule'},
    {'deadline_source': 'rule', 'rule_id': 'cooked-frozen', 'rule_version': storage_rules.RULES_VERSION},
    {'deadline_source': 'user', 'needs_review': True},
    {'deadline_source': 'package', 'food_class': 'packaged', 'opened': False,
     'condition': 'unopened', 'date_type': 'production', 'temperature_c': 6,
     'package_note': '未开封4℃冷藏，保质期3天'},
])
def test_queued_reminder_does_not_send_after_rule_or_review_gate_changes(signed, patch):
    stamp = core.now()
    with core.DB() as db:
        owner = db.scalar(select(core.User)).id
        food = core.Record(owner=owner, kind='food', payload={
            'name': '蔬菜汤', 'food_class': 'cooked', 'condition': 'cooked',
            'date_type': 'prepared', 'started_at': stamp.isoformat(),
            'storage': 'refrigerated', 'temperature_c': 4,
            'status': 'active', 'version': 'v1',
            'expires_at': (stamp + timedelta(days=3)).isoformat(),
            'remind_at': stamp.isoformat(), **patch,
        })
        db.add(food); db.flush()
        task = core.Job(owner=owner, kind='reminder', status='pending', due=stamp,
                        payload={'food_id': food.id, 'version': 'v1'})
        db.add(task); db.commit()
        task_id = task.id
    assert worker.process_once() is True
    with core.DB() as db:
        task = db.get(core.Job, task_id)
        assert task.status == 'cancelled'
        assert task.result['reason'] == 'deadline_basis_no_longer_applicable'
