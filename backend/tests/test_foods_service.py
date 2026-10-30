"""foods 共用服务回归：读取/保存/状态/重评/提醒与事务边界。

重点保护：
- insert_food 不自行 commit（草稿确认事务完整性）；
- 重评 409 先提交待核对、清旧期限、取消旧提醒，再抛错；
- 提醒去重与取消；账号隔离；字段未传/显式清空。
"""
import ast
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app import core, providers, storage_rules, worker
from app.services import food_service
from app.core import now


def food(**kw):
    return {'name': '蔬菜汤', 'food_class': 'cooked', 'storage': 'refrigerated', 'location': 'indoor', 'condition': 'cooked',
            'temperature_c': 4.0, 'date_type': 'prepared',
            'started_at': (now() - timedelta(hours=4)).isoformat(),
            'deadline_source': 'unknown', 'status': 'active', **kw}


def owner_id(client):
    with core.DB() as db:
        return db.scalar(select(core.User).where(core.User.username == 'alice')).id


# ---------- 结构约束 ----------
def test_service_does_not_import_routes_or_http():
    source = Path(food_service.__file__).read_text()
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    assert not any(name.endswith('main') for name in imported), imported
    assert 'httpx' not in imported and 'requests' not in imported and 'fastapi' not in imported


def test_insert_food_does_not_commit(signed):
    """insert_food 不得提交：调用方回滚后不应留下记录。"""
    owner = owner_id(signed)
    with core.DB() as db:
        food_service.insert_food(db, owner, food())
        db.rollback()
    with core.DB() as db:
        assert db.scalars(select(core.Record).where(core.Record.kind == 'food')).all() == []
        assert db.scalars(select(core.Job).where(core.Job.kind == 'reminder')).all() == []


def test_insert_food_creates_reminder_in_same_transaction(signed):
    owner = owner_id(signed)
    with core.DB() as db:
        food_service.insert_food(db, owner, food(remind_at=(now() + timedelta(hours=2)).isoformat(),
                                                deadline_source='user',
                                                expires_at=(now() + timedelta(hours=5)).isoformat()))
        db.commit()
    with core.DB() as db:
        jobs = db.scalars(select(core.Job).where(core.Job.kind == 'reminder')).all()
        assert len(jobs) == 1


# ---------- 读取与状态 ----------
def test_read_all_includes_status_and_progress(signed):
    created = signed.post('/api/v1/foods', json=food(deadline_source='user',
                                                    expires_at=(now() + timedelta(days=2)).isoformat())).json()
    items = signed.get('/api/v1/foods').json()['items']
    assert len(items) == 1
    item = items[0]
    assert item['id'] == created['id']
    assert item['display_status'] == created['display_status']
    assert item['assessment_status'] == 'user_set'
    assert item['estimated_remaining_days'] is not None
    assert item['stored_for_days'] is None  # 用户自设计算“已存多久”不成立


def test_load_enforces_ownership_404(signed):
    created = signed.post('/api/v1/foods', json=food()).json()
    signed.post('/api/v1/auth/logout')
    signed.post('/api/v1/auth/register', json={'username': 'bob', 'password': 'correct-horse-456'})
    bob = owner_id_bob = None
    with core.DB() as db:
        bob = db.scalar(select(core.User).where(core.User.username == 'bob')).id
    with core.DB() as db:
        with pytest.raises(core.AppError) as exc:
            food_service.load(db, created['id'], bob)
    assert exc.value.code == 'NOT_FOUND'
    assert signed.put(f"/api/v1/foods/{created['id']}", json=food()).status_code == 404


# ---------- 状态更新 ----------
def test_update_preserves_history_and_clears_reminder(signed):
    created = signed.post('/api/v1/foods', json=food(
        remind_at=(now() + timedelta(hours=1)).isoformat(),
        deadline_source='user', expires_at=(now() + timedelta(hours=6)).isoformat())).json()
    assert len(signed.get('/api/v1/notifications/tasks').json()) == 1
    body = {k: created[k] for k in ('name', 'storage', 'location', 'condition', 'temperature_c',
                                    'date_type', 'started_at')}
    body.update(storage='frozen', remind_at=None, deadline_source='user',
                expires_at=(now() + timedelta(hours=6)).isoformat())
    updated = signed.put(f"/api/v1/foods/{created['id']}", json=body).json()
    assert updated['storage'] == 'frozen'
    assert len(updated['storage_history']) == 1
    # 用户自设日期：条件变化不覆盖自设日期，也不作废
    assert updated['display_status'] == 'stored'
    assert updated['assessment_status'] == 'user_set'
    assert datetime.fromisoformat(updated['expires_at']) == datetime.fromisoformat(body['expires_at'])
    # 条件变更后不再沿用旧提醒
    assert all(t['status'] == 'cancelled' for t in signed.get('/api/v1/notifications/tasks').json())


def test_update_keeps_rule_basis_but_marks_review(signed, published_rules):
    created = signed.post('/api/v1/foods', json=food()).json()
    ruled = signed.post(f"/api/v1/foods/{created['id']}/reevaluate",
                        json={'evidence': {'deadline_source': 'rule'}}).json()
    body = {k: ruled[k] for k in ('name', 'storage', 'location', 'condition', 'temperature_c',
                                  'date_type', 'started_at', 'expires_at', 'deadline_source')}
    body['status'] = 'used'
    updated = signed.put(f"/api/v1/foods/{created['id']}", json=body).json()
    assert updated['status'] == 'used'
    assert updated['deadline_source'] == 'rule'
    assert updated['rule_id'] == 'cooked-refrigerated'
    # 用完即取消提醒
    assert all(t['status'] == 'cancelled' for t in signed.get('/api/v1/notifications/tasks').json())


def test_update_absent_field_preserved_null_clears(signed):
    created = signed.post('/api/v1/foods', json=food(temperature_c=4.0)).json()
    base = {k: created[k] for k in ('name', 'storage', 'location', 'condition', 'temperature_c',
                                    'date_type', 'started_at', 'deadline_source')}
    base['status'] = 'active'
    kept = signed.put(f"/api/v1/foods/{created['id']}",
                      json={k: v for k, v in base.items() if k != 'temperature_c'}).json()
    assert kept['temperature_c'] == 4.0
    cleared = signed.put(f"/api/v1/foods/{created['id']}", json={**base, 'temperature_c': None}).json()
    assert cleared['temperature_c'] is None


# ---------- 重评：409 语义不得改变 ----------
def test_rule_not_published_commits_pending_state_before_409(signed):
    created = signed.post('/api/v1/foods', json=food(
        deadline_source='user', expires_at=(now() + timedelta(hours=6)).isoformat(),
        remind_at=(now() + timedelta(hours=2)).isoformat())).json()
    assert len(signed.get('/api/v1/notifications/tasks').json()) == 1
    # 规则仍为 draft → 不允许生成正式期限
    r = signed.post(f"/api/v1/foods/{created['id']}/reevaluate",
                    json={'evidence': {'deadline_source': 'rule'}})
    assert r.status_code == 409
    assert r.json()['error']['code'] == 'RULE_NOT_PUBLISHED'
    # 409 之后：待核对状态已落库、旧期限已清、旧提醒已取消（不是统一回滚）
    after = signed.get('/api/v1/foods').json()['items'][0]
    assert after['display_status'] == 'needs_review'
    assert after['valid_deadline_at'] is None
    assert after['expires_at'] is None
    assert after['deadline_source'] == 'unknown'
    assert all(t['status'] == 'cancelled' for t in signed.get('/api/v1/notifications/tasks').json())


def test_no_applicable_rule_same_commit_semantics(signed, published_rules):
    created = signed.post('/api/v1/foods', json=food(name='苹果', condition='raw', storage='ambient',
                                                    temperature_c=None)).json()
    r = signed.post(f"/api/v1/foods/{created['id']}/reevaluate",
                    json={'evidence': {'deadline_source': 'rule', 'rule_id': 'cooked-refrigerated'}})
    assert r.status_code == 409
    assert r.json()['error']['code'] == 'NO_APPLICABLE_RULE'
    after = signed.get('/api/v1/foods').json()['items'][0]
    assert after['display_status'] == 'needs_review'
    assert after['valid_deadline_at'] is None


def test_published_rule_path_still_works(signed, published_rules):
    created = signed.post('/api/v1/foods', json=food()).json()
    data = signed.post(f"/api/v1/foods/{created['id']}/reevaluate",
                       json={'evidence': {'deadline_source': 'rule',
                                          'remind_at': (now() + timedelta(hours=2)).isoformat()}}).json()
    assert data['assessment_status'] == 'estimated'
    assert data['deadline_basis']['rule_id'] == 'cooked-refrigerated'
    tasks = signed.get('/api/v1/notifications/tasks').json()
    assert len(tasks) == 1 and tasks[0]['status'] in ('pending', 'waiting_configuration',
                                                      'waiting_subscription')


def test_reevaluate_is_idempotent_on_reminders(signed):
    """连续重评不应堆积重复提醒任务（旧任务取消、新任务一条）。"""
    created = signed.post('/api/v1/foods', json=food()).json()
    for _ in range(3):
        signed.post(f"/api/v1/foods/{created['id']}/reevaluate",
                    json={'evidence': {'deadline_source': 'user',
                                       'expires_at': (now() + timedelta(hours=6)).isoformat(),
                                       'remind_at': (now() + timedelta(hours=1)).isoformat()}})
    tasks = signed.get('/api/v1/notifications/tasks').json()
    open_tasks = [t for t in tasks if t['status'] != 'cancelled']
    assert len(open_tasks) == 1
    assert len(tasks) == 3  # 每次重评取消上一条


# ---------- 删除 ----------
def test_delete_cancels_reminder_and_hides_record(signed):
    created = signed.post('/api/v1/foods', json=food(
        deadline_source='user', expires_at=(now() + timedelta(hours=6)).isoformat(),
        remind_at=(now() + timedelta(hours=1)).isoformat())).json()
    assert signed.delete(f"/api/v1/foods/{created['id']}").json()['ok'] is True
    assert signed.get('/api/v1/foods').json()['items'] == []
    assert all(t['status'] == 'cancelled' for t in signed.get('/api/v1/notifications/tasks').json())


# ---------- 草稿确认沿用同一事务 ----------
def test_draft_confirm_saves_food_and_reminder_atomically(signed):
    draft = signed.post('/api/v1/foods/drafts', json={'items': [{
        'name': '豆腐', 'condition': 'unopened', 'storage': 'refrigerated', 'date_type': 'purchase',
        'started_at': (now() - timedelta(hours=1)).isoformat(),
        'deadline_source': 'user', 'expires_at': (now() + timedelta(hours=5)).isoformat(),
        'remind_at': (now() + timedelta(hours=1)).isoformat()}]}).json()
    result = signed.post(f"/api/v1/foods/drafts/{draft['draft_id']}/confirm", json={}).json()
    assert result['status'] == 'confirmed'
    with core.DB() as db:
        foods = db.scalars(select(core.Record).where(core.Record.kind == 'food')).all()
        assert len(foods) == 1
        assert foods[0].payload['draft_id'] == draft['draft_id']
        jobs = db.scalars(select(core.Job).where(core.Job.kind == 'reminder')).all()
        assert len(jobs) == 1
        assert jobs[0].payload['food_id'] == foods[0].id


def test_draft_partial_failure_leaves_no_orphan_food(signed):
    draft = signed.post('/api/v1/foods/drafts', json={'items': [
        {'name': '豆腐', 'storage': 'refrigerated', 'date_type': 'purchase',
         'started_at': (now() - timedelta(hours=1)).isoformat()},
        {'name': '西兰花', 'storage': 'invalid'}]}).json()
    result = signed.post(f"/api/v1/foods/drafts/{draft['draft_id']}/confirm", json={}).json()
    assert result['status'] == 'partially_saved'
    assert [r['status'] for r in result['results']] == ['saved', 'failed']
    with core.DB() as db:
        assert len(db.scalars(select(core.Record).where(core.Record.kind == 'food')).all()) == 1
