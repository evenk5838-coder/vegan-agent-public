"""批量食材草稿工程验证：整批核对、补信息、确认后保存、部分失败重试与幂等。
不发真实外部调用；提醒任务仅在本地数据库验证。"""
from datetime import datetime, timedelta

from sqlalchemy import select

from app import core, drafts
from app.core import now


def iso(dt):
    return dt.isoformat()


def item(name='豆腐', **kw):
    return {'name': name, **kw}


def ready(name='豆腐', **kw):
    return {'name': name, 'storage': 'refrigerated', 'started_at': iso(now() - timedelta(hours=2)),
            'condition': 'unopened', 'date_type': 'purchase', 'deadline_source': 'unknown', **kw}


def create(client, items, **kw):
    return client.post('/api/v1/foods/drafts', json={'items': items, **kw})


def foods(client):
    return client.get('/api/v1/foods').json()['items']


def test_draft_creation_does_not_save_food(signed):
    r = create(signed, [item('豆腐'), item('西兰花')])
    assert r.status_code == 201
    draft = r.json()
    assert draft['status'] == 'pending_confirmation'
    assert len(draft['items']) == 2
    assert all(i['status'] == 'ready' for i in draft['items'])
    assert draft['items'][0]['missing'] == []
    assert draft['expires_at']
    assert foods(signed) == []


def test_draft_expiry_is_seven_days(signed):
    draft = create(signed, [item('豆腐')]).json()
    created = datetime.fromisoformat(draft['created'])
    expires = datetime.fromisoformat(draft['expires_at'])
    delta = expires - created
    assert abs(delta - timedelta(days=drafts.DRAFT_TTL_DAYS)) < timedelta(seconds=1)


def test_fill_missing_then_confirm_saves(signed):
    draft = create(signed, [item('豆腐')]).json()
    draft_id = draft['draft_id']
    item_id = draft['items'][0]['item_id']
    r = signed.put(f'/api/v1/foods/drafts/{draft_id}/items/{item_id}',
                   json={'storage': 'refrigerated', 'started_at': iso(now() - timedelta(hours=3))})
    assert r.status_code == 200
    assert r.json()['item']['status'] == 'ready'
    assert r.json()['item']['actual']['storage'] == 'refrigerated'
    # 补完必要信息但未确认 → 仍不入库
    assert foods(signed) == []
    confirm = signed.post(f'/api/v1/foods/drafts/{draft_id}/confirm', json={}).json()
    assert confirm['status'] == 'confirmed'
    assert confirm['results'][0]['status'] == 'saved'
    saved = foods(signed)
    assert len(saved) == 1
    assert saved[0]['name'] == '豆腐'
    assert saved[0]['draft_id'] == draft_id
    assert saved[0]['draft_item_id'] == item_id


def test_confirm_without_required_info_fails_item(signed):
    draft = create(signed, [item('')]).json()
    confirm = signed.post(f"/api/v1/foods/drafts/{draft['draft_id']}/confirm", json={}).json()
    assert confirm['status'] == 'partially_saved'
    assert confirm['results'][0]['status'] == 'failed'
    assert confirm['results'][0]['error']['code'] == 'NEEDS_INFO'
    assert foods(signed) == []


def test_confirm_is_idempotent(signed):
    draft = create(signed, [ready('豆腐'), ready('西兰花')]).json()
    draft_id = draft['draft_id']
    first = signed.post(f'/api/v1/foods/drafts/{draft_id}/confirm', json={'idempotency_key': 'k1'}).json()
    assert first['status'] == 'confirmed'
    assert len(foods(signed)) == 2
    again = signed.post(f'/api/v1/foods/drafts/{draft_id}/confirm', json={'idempotency_key': 'k1'}).json()
    assert again['results'] == first['results']
    assert len(foods(signed)) == 2
    # 不同幂等键、无 item_ids：已保存项按 draft_item_id 复用，不重复入库
    third = signed.post(f'/api/v1/foods/drafts/{draft_id}/confirm', json={'idempotency_key': 'k2'}).json()
    assert {r['food_id'] for r in third['results']} == {r['food_id'] for r in first['results']}
    assert len(foods(signed)) == 2


def test_partial_failure_then_retry(signed):
    draft = create(signed, [ready('豆腐'), item('西兰花', storage='invalid')]).json()
    draft_id = draft['draft_id']
    ids = {i['name']: i['item_id'] for i in draft['items']}
    first = signed.post(f'/api/v1/foods/drafts/{draft_id}/confirm', json={}).json()
    assert first['status'] == 'partially_saved'
    states = {r['item_id']: r['status'] for r in first['results']}
    assert states[ids['豆腐']] == 'saved'
    assert states[ids['西兰花']] == 'failed'
    assert len(foods(signed)) == 1
    # 补齐后只重试失败项
    signed.put(f'/api/v1/foods/drafts/{draft_id}/items/{ids["西兰花"]}',
               json={'storage': 'refrigerated', 'started_at': iso(now())})
    retry = signed.post(f'/api/v1/foods/drafts/{draft_id}/confirm',
                        json={'item_ids': [ids['西兰花']], 'idempotency_key': 'retry-1'}).json()
    assert retry['status'] == 'confirmed'
    assert len(foods(signed)) == 2


def test_draft_creates_reminder_task_only_after_confirm(signed):
    remind = now() + timedelta(hours=5)
    draft = create(signed, [ready('豆浆', remind_at=iso(remind), deadline_source='user',
                                  expires_at=iso(now() + timedelta(hours=8)))]).json()
    assert signed.get('/api/v1/notifications/tasks').json() == []
    signed.post(f"/api/v1/foods/drafts/{draft['draft_id']}/confirm", json={}).json()
    tasks = signed.get('/api/v1/notifications/tasks').json()
    assert len(tasks) == 1


def test_draft_isolation(signed):
    draft = create(signed, [ready('豆腐')]).json()
    draft_id = draft['draft_id']
    signed.post('/api/v1/auth/logout')
    signed.post('/api/v1/auth/register', json={'username': 'bob', 'password': 'correct-horse-456'})
    assert signed.get(f'/api/v1/foods/drafts/{draft_id}').status_code == 404
    assert signed.post(f'/api/v1/foods/drafts/{draft_id}/confirm', json={}).status_code == 404
    assert signed.delete(f'/api/v1/foods/drafts/{draft_id}').status_code == 404
    assert foods(signed) == []


def test_saved_food_survives_draft_delete_and_expiry(signed):
    draft = create(signed, [ready('豆腐')]).json()
    draft_id = draft['draft_id']
    signed.post(f'/api/v1/foods/drafts/{draft_id}/confirm', json={}).json()
    assert len(foods(signed)) == 1
    assert signed.delete(f'/api/v1/foods/drafts/{draft_id}').json()['ok'] is True
    assert len(foods(signed)) == 1
    # 模拟草稿过期清理：只删草稿，不影响已保存食材与映射
    with core.DB() as db:
        food = db.scalar(select(core.Record).where(core.Record.kind == 'food'))
        assert food.payload['draft_id'] == draft_id
        for r in db.scalars(select(core.Record).where(core.Record.kind == 'food_draft')).all():
            db.delete(r)
        db.commit()
    assert len(foods(signed)) == 1


def test_reconfirm_after_draft_deleted_reuses_mapping(signed):
    draft = create(signed, [ready('豆腐')]).json()
    first = signed.post(f"/api/v1/foods/drafts/{draft['draft_id']}/confirm", json={}).json()
    food_id = first['results'][0]['food_id']
    with core.DB() as db:
        food = db.get(core.Record, food_id)
        assert food.payload['draft_item_id'] == draft['items'][0]['item_id']
    # 草稿被清理后，映射仍保存在食材记录上，不会因重试再入库
    assert food_id
    assert len(foods(signed)) == 1


def test_draft_validation_limits(signed):
    assert create(signed, []).status_code == 400
    assert create(signed, [item('x')] * (drafts.MAX_ITEMS + 1)).status_code == 422
    draft = create(signed, [item('豆腐')]).json()
    r = signed.put(f"/api/v1/foods/drafts/{draft['draft_id']}/items/nonexistent", json={'storage': 'frozen'})
    assert r.status_code == 404


def test_confirmed_draft_rejects_item_edit(signed):
    draft = create(signed, [ready('豆腐')]).json()
    signed.post(f"/api/v1/foods/drafts/{draft['draft_id']}/confirm", json={}).json()
    r = signed.put(f"/api/v1/foods/drafts/{draft['draft_id']}/items/{draft['items'][0]['item_id']}",
                   json={'storage': 'frozen'})
    assert r.status_code == 409
    assert r.json()['error']['code'] == 'DRAFT_CLOSED'


# ---- G1~G5 回归（前端批量草稿核对） ----
def test_g1_input_values_read_back_after_reload(signed):
    expected = ready('豆腐')
    draft = create(signed, [expected]).json()
    loaded = signed.get(f"/api/v1/foods/drafts/{draft['draft_id']}").json()['items'][0]
    shown = {**(loaded.get('actual') or {}), **loaded}
    assert shown['storage'] == expected['storage']
    assert shown['started_at'] == expected['started_at']
    assert loaded['field_sources']['storage'] == 'input'
    assert loaded['suggested'] is None  # 未接入规则表前不做无依据建议


def test_g2_same_key_different_targets_conflicts(signed):
    draft = create(signed, [ready('豆腐'), ready('西兰花')]).json()
    ids = [i['item_id'] for i in draft['items']]
    path = f"/api/v1/foods/drafts/{draft['draft_id']}/confirm"
    assert signed.post(path, json={'item_ids': [ids[0]], 'idempotency_key': 'k1'}).status_code == 200
    r = signed.post(path, json={'item_ids': [ids[1]], 'idempotency_key': 'k1'})
    assert r.status_code == 409
    assert r.json()['error']['code'] == 'IDEMPOTENCY_CONFLICT'
    # 相同内容重放仍安全返回旧结果
    again = signed.post(path, json={'item_ids': [ids[0]], 'idempotency_key': 'k1'})
    assert again.status_code == 200
    assert len(foods(signed)) == 1


def test_g3_subset_confirm_keeps_batch_partially_saved(signed):
    draft = create(signed, [ready('豆腐'), ready('西兰花')]).json()
    r = signed.post(f"/api/v1/foods/drafts/{draft['draft_id']}/confirm",
                    json={'item_ids': [draft['items'][0]['item_id']]}).json()
    assert r['status'] == 'partially_saved'
    # 剩余未确认项仍可编辑并再次确认，最终转 confirmed
    second = draft['items'][1]['item_id']
    assert signed.put(f"/api/v1/foods/drafts/{draft['draft_id']}/items/{second}",
                      json={'storage': 'frozen'}).status_code == 200
    final = signed.post(f"/api/v1/foods/drafts/{draft['draft_id']}/confirm",
                        json={'item_ids': [second], 'idempotency_key': 'k2'}).json()
    assert final['status'] == 'confirmed'
    assert len(foods(signed)) == 2


def test_g4_saved_item_is_read_only(signed):
    draft = create(signed, [ready('豆腐'), item('西兰花', storage='invalid')]).json()
    draft_id = draft['draft_id']
    assert signed.post(f'/api/v1/foods/drafts/{draft_id}/confirm', json={}).json()['status'] == 'partially_saved'
    r = signed.put(f"/api/v1/foods/drafts/{draft_id}/items/{draft['items'][0]['item_id']}",
                   json={'storage': 'frozen'})
    assert r.status_code == 409
    assert r.json()['error']['code'] == 'ITEM_SAVED'
    # 正式记录未被草稿编辑污染；未保存项仍可编辑
    assert foods(signed)[0]['storage'] == 'refrigerated'
    assert signed.put(f"/api/v1/foods/drafts/{draft_id}/items/{draft['items'][1]['item_id']}",
                      json={'storage': 'frozen'}).status_code == 200


def test_g5_concurrent_confirm_creates_one_food(signed, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, BrokenBarrierError
    from fastapi.testclient import TestClient
    from app import drafts as drafts_module
    from app.main import app as app_obj

    draft = create(signed, [ready('豆腐')]).json()
    path = f"/api/v1/foods/drafts/{draft['draft_id']}/confirm"
    barrier = Barrier(2)
    original = drafts_module.to_food_payload

    def synchronized_validation(item):
        try:
            barrier.wait(timeout=1)
        except BrokenBarrierError:
            pass
        return original(item)

    monkeypatch.setattr(drafts_module, 'to_food_payload', synchronized_validation)
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
    matching = [f for f in foods(signed) if f.get('draft_id') == draft['draft_id']]
    assert len(matching) == 1
