"""期限重评工程验证：规则查表、包装/用户依据、无依据待核对、进度结构与提醒同步。"""
from datetime import datetime, timedelta

from app import core, storage_rules
from app.core import now


def food(**kw):
    return {'name': '蔬菜汤', 'food_class': 'cooked', 'storage': 'refrigerated', 'location': 'indoor', 'condition': 'cooked',
            'temperature_c': 4.0,
            'date_type': 'prepared', 'started_at': (now() - timedelta(hours=6)).isoformat(),
            'deadline_source': 'unknown', 'status': 'active', **kw}


def create(client, **kw):
    r = client.post('/api/v1/foods', json=food(**kw))
    assert r.status_code == 201
    return r.json()


def test_unevidenced_food_keeps_unknown_progress(signed):
    item = create(signed)
    assert item['assessment_status'] == 'unknown'
    assert item['valid_deadline_at'] is None
    assert item['estimated_remaining_days'] is None
    assert item['deadline_basis']['type'] == 'none'


def test_rule_lookup_sets_traceable_deadline(signed, published_rules):
    item = create(signed)
    r = signed.post(f"/api/v1/foods/{item['id']}/reevaluate",
                    json={'evidence': {'deadline_source': 'rule'}})
    assert r.status_code == 200
    data = r.json()
    assert data['assessment_status'] == 'estimated'
    assert data['deadline_basis']['type'] == 'rule'
    assert data['deadline_basis']['rule_id'] == 'cooked-refrigerated'
    assert data['deadline_basis']['rule_version'] == storage_rules.RULES_VERSION
    assert data['deadline_basis']['rule_source'].startswith('https://www.foodsafety.gov')
    assert data['deadline_basis']['rules_status'] == 'published'
    assert data['valid_started_at']
    assert data['stored_for_days'] is not None
    assert data['estimated_remaining_days'] is not None


def test_no_applicable_rule_marks_needs_review(signed):
    item = create(signed, name='不明食材', storage='ambient', condition='raw')
    r = signed.post(f"/api/v1/foods/{item['id']}/reevaluate",
                    json={'evidence': {'deadline_source': 'rule'}})
    assert r.status_code == 409
    assert r.json()['error']['code'] == 'NO_APPLICABLE_RULE'
    after = signed.get('/api/v1/foods').json()['items'][0]
    assert after['display_status'] == 'needs_review'
    assert after['valid_deadline_at'] is None  # 不沿用旧期限
    assert after['assessment_status'] == 'needs_review'


def test_storage_change_requires_reevaluation_and_keeps_history(signed):
    item = create(signed, deadline_source='user', expires_at=(now() + timedelta(days=2)).isoformat())
    r = signed.post(f"/api/v1/foods/{item['id']}/reevaluate",
                    json={'storage': 'frozen', 'evidence': {'deadline_source': 'unknown'}})
    data = r.json()
    assert data['display_status'] == 'needs_review'
    assert data['valid_deadline_at'] is None
    assert len(data['storage_history']) == 1
    assert data['storage_history'][0]['storage'] == 'refrigerated'


def test_freezer_quality_reference_does_not_become_new_expiry(signed, published_rules):
    item = create(signed)
    signed.post(f"/api/v1/foods/{item['id']}/reevaluate",
                json={'evidence': {'deadline_source': 'rule'}})
    response = signed.post(f"/api/v1/foods/{item['id']}/reevaluate",
                        json={'storage': 'frozen', 'temperature_c': -18,
                              'evidence': {'deadline_source': 'rule'}})
    assert response.status_code == 409
    assert response.json()['error']['code'] == 'NO_APPLICABLE_RULE'
    moved = signed.get('/api/v1/foods').json()['items'][0]
    assert moved['storage'] == 'frozen'
    assert moved['deadline_basis']['type'] == 'none'
    assert moved['valid_deadline_at'] is None
    assert moved['display_status'] == 'needs_review'
    # 不清零：起点仍是原始记录起点（仅表示形式可能为 +00:00）
    assert datetime.fromisoformat(moved['started_at']) == datetime.fromisoformat(item['started_at'])
    assert moved['storage_history'][0]['storage'] == 'refrigerated'


def test_user_set_date_is_labelled_not_estimated(signed):
    target = now() + timedelta(hours=6)
    item = create(signed)
    r = signed.post(f"/api/v1/foods/{item['id']}/reevaluate",
                    json={'evidence': {'deadline_source': 'user', 'expires_at': target.isoformat()}}).json()
    assert r['assessment_status'] == 'user_set'
    assert r['deadline_basis']['type'] == 'user'
    # 用户自设：有剩余时间，但不计算“已存多久”
    assert r['estimated_remaining_days'] is not None
    assert r['stored_for_days'] is None


def test_package_evidence_requires_note_and_date(signed):
    item = create(signed)
    assert signed.post(f"/api/v1/foods/{item['id']}/reevaluate",
                       json={'evidence': {'deadline_source': 'package'}}).status_code == 422
    target = now() + timedelta(days=3)
    ok = signed.post(f"/api/v1/foods/{item['id']}/reevaluate",
                     json={'evidence': {'deadline_source': 'package', 'package_note': '未开封冷藏标示',
                                        'expires_at': target.isoformat()}}).json()
    assert ok['assessment_status'] == 'estimated'
    assert ok['deadline_basis']['type'] == 'package'
    assert ok['stored_for_days'] is not None


def test_reevaluate_syncs_reminder_tasks(signed):
    item = create(signed)
    target = now() + timedelta(hours=3)
    signed.post(f"/api/v1/foods/{item['id']}/reevaluate",
                json={'evidence': {'deadline_source': 'user', 'expires_at': target.isoformat(),
                                   'remind_at': (now() + timedelta(hours=1)).isoformat()}})
    tasks = signed.get('/api/v1/notifications/tasks').json()
    assert len(tasks) == 1
    # 改成待核对后，旧提醒任务应取消
    signed.post(f"/api/v1/foods/{item['id']}/reevaluate",
                json={'storage': 'ambient', 'evidence': {'deadline_source': 'unknown'}})
    statuses = [t['status'] for t in signed.get('/api/v1/notifications/tasks').json()]
    assert 'cancelled' in statuses


def test_reminder_after_deadline_rejected(signed):
    item = create(signed)
    target = now() + timedelta(hours=2)
    r = signed.post(f"/api/v1/foods/{item['id']}/reevaluate",
                    json={'evidence': {'deadline_source': 'user', 'expires_at': target.isoformat(),
                                       'remind_at': (now() + timedelta(hours=5)).isoformat()}})
    assert r.status_code == 422


def test_storage_rules_catalog_is_traceable(signed):
    data = signed.get('/api/v1/storage/rules').json()
    assert data['rules_version'] == storage_rules.RULES_VERSION
    assert '待复核' in data['rules_status']
    for rule in data['rules']:
        assert rule['rule_id'] and rule['source'].startswith('https://')
        assert rule['status'] == 'draft'


def test_reevaluate_is_account_scoped(signed):
    item = create(signed)
    signed.post('/api/v1/auth/logout')
    signed.post('/api/v1/auth/register', json={'username': 'bob', 'password': 'correct-horse-456'})
    assert signed.post(f"/api/v1/foods/{item['id']}/reevaluate",
                       json={'evidence': {'deadline_source': 'rule'}}).status_code == 404
