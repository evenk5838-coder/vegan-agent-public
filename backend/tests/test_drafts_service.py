"""drafts 共用服务回归：创建/读取/补字段/确认与 HTTP↔Agent 复用。

重点保护：7 天留存、确认前不建正式食材、已保存项只读、部分失败只重试未保存项、
同 key 同内容重放、同 key 异内容 409、单事务与草稿锁、数据库异常不被吞成单项失败。
"""
import ast
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app import core, providers, worker
from app.services import draft_service
from app.core import now


def create(client, items, **kw):
    return client.post('/api/v1/foods/drafts', json={'items': items, **kw})


def ready(name='豆腐', **kw):
    return {'name': name, 'storage': 'refrigerated', 'date_type': 'purchase',
            'started_at': (now() - timedelta(hours=2)).isoformat(), 'condition': 'unopened',
            'deadline_source': 'unknown', **kw}


def foods(client):
    return client.get('/api/v1/foods').json()['items']


# ---------- 结构约束 ----------
def test_service_does_not_import_routes_or_http():
    source = Path(draft_service.__file__).read_text()
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    assert not any(n.endswith('main') for n in imported), imported
    assert 'httpx' not in imported and 'requests' not in imported and 'fastapi' not in imported


# ---------- 创建：HTTP 与 Agent 共用 ----------
def test_http_and_agent_create_produce_same_shape(signed, monkeypatch):
    via_http = create(signed, [{'name': '豆腐'}]).json()
    monkeypatch.setattr(core.settings, 'deepseek_api_key', 'test-only')
    monkeypatch.setattr(core.settings, 'external_calls_enabled', True)
    from app import agent
    with core.DB() as db:
        owner = db.scalar(select(core.User)).id
    _payload, card = agent._execute_tool('draft_foods', {'items': [{'name': '豆腐'}]}, owner,
                                         {'diet': 'vegan', 'avoid_coriander': False})
    via_agent = card['payload']
    # 新增 advice_digest/advice_version（确认前复核用），两端必须一致
    assert set(via_http) == set(via_agent) == {'draft_id', 'status', 'created', 'expires_at',
                                               'items', 'advice_digest', 'advice_version'}
    # 摘要是“同一份草稿内”的稳定值（含 item_id），跨草稿不要求相等
    assert len(via_http['advice_digest']) == 64 and len(via_agent['advice_digest']) == 64
    again = create(signed, [{'name': '豆腐'}]).json()
    assert again['advice_digest'] != via_http['advice_digest']
    assert via_http['status'] == via_agent['status'] == 'pending_confirmation'
    assert via_http['items'][0]['status'] == via_agent['items'][0]['status'] == 'ready'
    assert set(via_http['items'][0]['missing']) == set(via_agent['items'][0]['missing'])


def test_agent_tool_does_not_create_food(signed, monkeypatch):
    monkeypatch.setattr(core.settings, 'deepseek_api_key', 'test-only')
    monkeypatch.setattr(core.settings, 'external_calls_enabled', True)
    from app import agent
    with core.DB() as db:
        owner = db.scalar(select(core.User)).id
    agent._execute_tool('draft_foods', {'items': [ready()]}, owner, {'diet': 'vegan'})
    assert foods(signed) == []


def test_draft_expires_in_seven_days(signed):
    draft = create(signed, [{'name': '豆腐'}]).json()
    delta = core.now().__class__.fromisoformat(draft['expires_at']) - core.now().__class__.fromisoformat(draft['created'])
    assert abs(delta - timedelta(days=7)) < timedelta(seconds=2)


# ---------- 补字段 ----------
def test_fill_then_confirm_and_read_only_after_save(signed):
    draft = create(signed, [{'name': '豆腐'}]).json()
    item_id = draft['items'][0]['item_id']
    filled = signed.put(f"/api/v1/foods/drafts/{draft['draft_id']}/items/{item_id}",
                        json={'storage': 'refrigerated', 'started_at': (now() - timedelta(hours=1)).isoformat()}).json()
    assert filled['item']['status'] == 'ready'
    assert foods(signed) == []  # 补完信息但未确认
    assert signed.post(f"/api/v1/foods/drafts/{draft['draft_id']}/confirm", json={}).json()['status'] == 'confirmed'
    assert len(foods(signed)) == 1
    # 整批已确认后草稿关闭，编辑被拒绝（既有语义）
    closed = signed.put(f"/api/v1/foods/drafts/{draft['draft_id']}/items/{item_id}", json={'storage': 'frozen'})
    assert closed.status_code == 409
    assert closed.json()['error']['code'] == 'DRAFT_CLOSED'


def test_clear_semantics_and_required_fields(signed):
    draft = create(signed, [{'name': '豆腐', 'storage': 'frozen',
                             'started_at': (now() - timedelta(hours=1)).isoformat(),
                             'temperature_c': 4}]).json()
    path = f"/api/v1/foods/drafts/{draft['draft_id']}/items/{draft['items'][0]['item_id']}"
    cleared = signed.put(path, json={'clear': ['temperature_c']}).json()
    assert 'temperature_c' not in cleared['item']
    assert cleared['item']['cleared'] == ['temperature_c']
    assert signed.put(path, json={'clear': ['name']}).status_code == 422
    assert signed.put(path, json={'storage': None}).status_code == 200


# ---------- 部分失败与重试 ----------
def test_partial_failure_then_retry_only_failed(signed):
    draft = create(signed, [ready('豆腐'), {'name': '西兰花', 'storage': 'invalid'}]).json()
    ids = {i['name']: i['item_id'] for i in draft['items']}
    first = signed.post(f"/api/v1/foods/drafts/{draft['draft_id']}/confirm", json={}).json()
    assert first['status'] == 'partially_saved'
    assert {r['item_id']: r['status'] for r in first['results']} == {ids['豆腐']: 'saved', ids['西兰花']: 'failed'}
    signed.put(f"/api/v1/foods/drafts/{draft['draft_id']}/items/{ids['西兰花']}",
               json={'storage': 'refrigerated', 'started_at': (now() - timedelta(hours=1)).isoformat()})
    retry = signed.post(f"/api/v1/foods/drafts/{draft['draft_id']}/confirm",
                        json={'item_ids': [ids['西兰花']], 'idempotency_key': 'retry-1'}).json()
    assert retry['status'] == 'confirmed'
    assert len(foods(signed)) == 2


# ---------- 幂等 ----------
def test_same_key_same_content_replays(signed):
    draft = create(signed, [ready('豆腐')]).json()
    path = f"/api/v1/foods/drafts/{draft['draft_id']}/confirm"
    first = signed.post(path, json={'idempotency_key': 'k1'}).json()
    again = signed.post(path, json={'idempotency_key': 'k1'}).json()
    assert again == first
    assert len(foods(signed)) == 1


def test_same_key_different_content_conflicts(signed):
    draft = create(signed, [ready('豆腐'), ready('西兰花')]).json()
    ids = [i['item_id'] for i in draft['items']]
    path = f"/api/v1/foods/drafts/{draft['draft_id']}/confirm"
    signed.post(path, json={'item_ids': [ids[0]], 'idempotency_key': 'k1'})
    conflict = signed.post(path, json={'item_ids': [ids[1]], 'idempotency_key': 'k1'})
    assert conflict.status_code == 409
    assert conflict.json()['error']['code'] == 'IDEMPOTENCY_CONFLICT'


# ---------- 事务与并发 ----------
def test_food_and_reminder_and_draft_state_commit_together(signed):
    draft = create(signed, [ready('豆腐', deadline_source='user',
                                  expires_at=(now() + timedelta(hours=5)).isoformat(),
                                  remind_at=(now() + timedelta(hours=1)).isoformat())]).json()
    signed.post(f"/api/v1/foods/drafts/{draft['draft_id']}/confirm", json={}).json()
    with core.DB() as db:
        food = db.scalar(select(core.Record).where(core.Record.kind == 'food'))
        job = db.scalar(select(core.Job).where(core.Job.kind == 'reminder'))
        record = db.get(core.Record, draft['draft_id'])
        assert food and job and record
        assert job.payload['food_id'] == food.id
        assert record.payload['status'] == 'confirmed'
        assert record.payload['items'][0]['saved_food_id'] == food.id


def test_concurrent_confirm_creates_one_food(signed, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, BrokenBarrierError
    from fastapi.testclient import TestClient
    from app import drafts as drafts_module
    from app.main import app as app_obj

    draft = create(signed, [ready('豆腐')]).json()
    path = f"/api/v1/foods/drafts/{draft['draft_id']}/confirm"
    barrier = Barrier(2)
    original = drafts_module.to_food_payload

    def synchronized(item):
        try:
            barrier.wait(timeout=1)
        except BrokenBarrierError:
            pass
        return original(item)

    monkeypatch.setattr(drafts_module, 'to_food_payload', synchronized)
    cookies = dict(signed.cookies)

    def confirm():
        client = TestClient(app_obj)
        try:
            client.cookies.update(cookies)
            return client.post(path, json={'idempotency_key': 'parallel'})
        finally:
            client.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: confirm(), range(2)))
    assert all(r.status_code == 200 for r in responses)
    assert len([f for f in foods(signed) if f.get('draft_id') == draft['draft_id']]) == 1


def test_database_error_not_swallowed_as_item_failure(signed, monkeypatch):
    """数据库异常必须表现为服务失败（500），不能伪装成单项校验失败，也不能半写入。"""
    draft = create(signed, [ready('豆腐')]).json()

    def boom(*args, **kwargs):
        raise RuntimeError('simulated db failure')

    monkeypatch.setattr(draft_service.food_service, 'insert_food', boom)
    r = signed.post(f"/api/v1/foods/drafts/{draft['draft_id']}/confirm", json={})
    assert r.status_code == 500
    assert r.json()['error']['code'] == 'INTERNAL_ERROR'  # 不是 NEEDS_INFO 之类单项失败
    assert 'simulated db failure' not in r.text            # 不泄露内部细节
    assert foods(signed) == []                              # 事务回滚，无半条正式食材
    assert signed.get(f"/api/v1/foods/drafts/{draft['draft_id']}").json()['status'] == 'pending_confirmation'


# ---------- 删除与隔离 ----------
def test_delete_draft_keeps_saved_food(signed):
    draft = create(signed, [ready('豆腐')]).json()
    signed.post(f"/api/v1/foods/drafts/{draft['draft_id']}/confirm", json={}).json()
    assert signed.delete(f"/api/v1/foods/drafts/{draft['draft_id']}").json()['ok'] is True
    assert len(foods(signed)) == 1


def test_draft_is_account_scoped(signed):
    draft = create(signed, [ready('豆腐')]).json()
    signed.post('/api/v1/auth/logout')
    signed.post('/api/v1/auth/register', json={'username': 'bob', 'password': 'correct-horse-456'})
    assert signed.get(f"/api/v1/foods/drafts/{draft['draft_id']}").status_code == 404
    assert signed.post(f"/api/v1/foods/drafts/{draft['draft_id']}/confirm", json={}).status_code == 404
    assert signed.get('/api/v1/foods/drafts').json()['items'] == []
    assert foods(signed) == []


# ---------- 排序回归（对应首批勘误） ----------
def test_search_order_matches_list_and_agent(signed, monkeypatch):
    """多条命中时：HTTP 搜索、列表、Agent 都按新建优先，顺序一致。"""
    for title in ('番茄一号', '番茄二号', '番茄三号'):
        signed.post('/api/v1/recipes', json={'title': title, 'ingredients': '番茄、豆腐',
                                             'steps': '炒熟', 'meal': 'main', 'confirmed': True})
    expected = ['番茄三号', '番茄二号', '番茄一号']  # created DESC

    listed = signed.get('/api/v1/recipes').json()['items']
    searched = signed.post('/api/v1/recipes/search', json={'query': '番茄'}).json()['items']

    monkeypatch.setattr(core.settings, 'deepseek_api_key', 'test-only')
    monkeypatch.setattr(core.settings, 'external_calls_enabled', True)
    from app import agent
    with core.DB() as db:
        owner = db.scalar(select(core.User)).id
    via_agent = agent._search_recipes(owner, '番茄', None, None, None, None,
                                      {'diet': 'vegan', 'avoid_coriander': False})

    assert [r['title'] for r in listed] == expected
    assert [r['title'] for r in searched] == expected
    assert [r['title'] for r in via_agent] == expected
