"""Personal recipe writes: real isolated Postgres, no provider calls or business DB.

Exercise concurrent HTTP requests, version conflicts and interrupted transactions;
these are not assertions that a read-before-save UI alone protects a stale write.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import core
from app.main import app
from app.recipes import evaluate_recipe
from app.services import recipe_save


def recipe(title='番茄豆腐', **changes):
    return {'title': title, 'ingredients': '番茄、豆腐、橄榄油', 'steps': '切块后充分加热。',
            'meal': 'lunch', 'purpose': 'everyday', 'cuisine': 'chinese',
            'confirmed': True, **changes}


def rows(kind):
    with core.DB() as db:
        return list(db.scalars(select(core.Record).where(core.Record.kind == kind)))


def parallel_request(cookies, method, path, body=None, headers=None):
    client = TestClient(app)
    try:
        client.cookies.update(cookies)
        return client.request(method, path, json=body, headers=headers)
    finally:
        client.close()


def test_create_idempotency_is_persistent_and_does_not_duplicate(signed):
    header = {'Idempotency-Key': 'create-1'}
    first = signed.post('/api/v1/recipes', json=recipe(), headers=header)
    second = signed.post('/api/v1/recipes', json=recipe(), headers=header)
    assert (first.status_code, second.status_code) == (201, 200)
    assert second.json()['replayed'] is True
    assert first.json()['id'] == second.json()['id']
    assert first.json()['version'] == second.json()['version']
    assert first.headers['etag'] == f'"{first.json()["version"]}"'
    assert len(rows('recipe')) == len(rows('recipe_create')) == 1
    receipt = rows('recipe_create')[0]
    assert receipt.payload['key_hash'] == core.digest('create-1')
    assert 'create-1' not in str(receipt.payload)


def test_create_same_key_with_changed_content_conflicts(signed):
    header = {'Idempotency-Key': 'one-logical-save'}
    original = signed.post('/api/v1/recipes', json=recipe(), headers=header).json()
    changed = signed.post('/api/v1/recipes', json=recipe('另一个菜谱'), headers=header)
    assert changed.status_code == 409
    assert changed.json()['error']['code'] == 'IDEMPOTENCY_CONFLICT'
    assert signed.get('/api/v1/recipes').json()['items'][0]['id'] == original['id']
    assert len(rows('recipe')) == 1


def test_legacy_post_without_key_keeps_old_behavior(signed):
    a = signed.post('/api/v1/recipes', json=recipe())
    b = signed.post('/api/v1/recipes', json=recipe())
    assert a.status_code == b.status_code == 201
    assert a.json()['id'] != b.json()['id']
    assert rows('recipe_create') == []


@pytest.mark.parametrize('header', ['', 'bad key', 'x' * 129, '中文'])
def test_create_rejects_invalid_idempotency_key_without_writing(signed, header):
    # HTTP headers are latin-1; non-ASCII input is checked at the service boundary.
    if header == '中文':
        with core.DB() as db, pytest.raises(core.AppError) as error:
            recipe_save.create_personal(db, 'owner', recipe(), header)
        assert error.value.code == 'INVALID_IDEMPOTENCY_KEY'
    else:
        result = signed.post('/api/v1/recipes', json=recipe(), headers={'Idempotency-Key': header})
        assert result.status_code == 422
        assert result.json()['error']['code'] == 'INVALID_IDEMPOTENCY_KEY'
    assert rows('recipe') == rows('recipe_create') == []


def test_create_requires_confirmation_even_with_replayed_key(signed):
    first = signed.post('/api/v1/recipes', json=recipe(), headers={'Idempotency-Key': 'safe'})
    result = signed.post('/api/v1/recipes', json=recipe(confirmed=False), headers={'Idempotency-Key': 'safe'})
    assert first.status_code == 201
    assert result.status_code == 400
    assert result.json()['error']['code'] == 'CONFIRMATION_REQUIRED'
    assert len(rows('recipe')) == 1


def test_detail_and_idempotency_are_owner_scoped_and_public_readonly(signed):
    alice = signed.post('/api/v1/recipes', json=recipe(), headers={'Idempotency-Key': 'same'}).json()
    with core.DB() as db:
        public = core.Record(owner='public', kind='knowledge', payload={'title': '公共资料'})
        db.add(public)
        db.commit()
        public_id = public.id
    # Static candidates route must not be swallowed by the new dynamic GET.
    assert signed.get('/api/v1/recipes/candidates').status_code == 200
    assert signed.get('/api/v1/recipes/' + public_id).status_code == 404
    assert signed.put('/api/v1/recipes/' + public_id, json=recipe()).status_code == 404
    assert signed.delete('/api/v1/recipes/' + public_id).status_code == 404
    signed.post('/api/v1/auth/logout')
    assert signed.post('/api/v1/auth/register', json={'username': 'bob', 'password': 'correct-horse-456'}).status_code == 201
    assert signed.get('/api/v1/recipes/' + alice['id']).status_code == 404
    assert signed.put('/api/v1/recipes/' + alice['id'], json=recipe(), headers={'If-Match': alice['version']}).status_code == 404
    assert signed.delete('/api/v1/recipes/' + alice['id']).status_code == 404
    bob = signed.post('/api/v1/recipes', json=recipe(), headers={'Idempotency-Key': 'same'}).json()
    assert alice['id'] != bob['id']
    assert len(rows('recipe_create')) == 2


def test_detail_list_and_update_share_version_etag_and_reject_stale(signed):
    original = signed.post('/api/v1/recipes', json=recipe()).json()
    identifier = original['id']
    detail = signed.get('/api/v1/recipes/' + identifier)
    listing = signed.get('/api/v1/recipes').json()['items'][0]
    assert original['version'] == detail.json()['version'] == listing['version']
    updated = signed.put('/api/v1/recipes/' + identifier, json=recipe('新版豆腐'),
                         headers={'If-Match': detail.headers['etag']})
    assert updated.status_code == 200
    assert updated.json()['id'] == identifier
    assert updated.json()['version'] != original['version']
    stale = signed.put('/api/v1/recipes/' + identifier, json=recipe('陈旧覆盖'),
                       headers={'If-Match': original['version']})
    assert stale.status_code == 409
    assert stale.json()['error']['code'] == 'RECIPE_CHANGED'
    current = signed.get('/api/v1/recipes/' + identifier).json()
    assert current['title'] == '新版豆腐'
    assert current['version'] == updated.json()['version']


def test_legacy_recipe_version_is_stable_without_get_mutation(signed):
    original = signed.post('/api/v1/recipes', json=recipe()).json()
    with core.DB() as db:
        row = db.get(core.Record, original['id'])
        row.payload = {k: v for k, v in row.payload.items() if k != 'version'}
        db.commit()
        expected = recipe_save.recipe_version(row.payload)
    for _ in range(2):
        assert signed.get('/api/v1/recipes/' + original['id']).json()['version'] == expected
        assert signed.get('/api/v1/recipes').json()['items'][0]['version'] == expected
    with core.DB() as db:
        assert 'version' not in db.get(core.Record, original['id']).payload
    changed = signed.put('/api/v1/recipes/' + original['id'], json=recipe('已迁入版本保护'), headers={'If-Match': expected})
    assert changed.status_code == 200
    assert changed.json()['version'] != expected


def test_legacy_put_without_version_still_works(signed):
    first = signed.post('/api/v1/recipes', json=recipe()).json()
    result = signed.put('/api/v1/recipes/' + first['id'], json=recipe('旧客户端更新'))
    assert result.status_code == 200
    assert result.json()['version'] != first['version']


@pytest.mark.parametrize('token', ['', '*', 'W/"old"', 'a,b', 'a"b', 'x' * 129])
def test_invalid_if_match_is_explicit_and_does_not_write(signed, token):
    row = signed.post('/api/v1/recipes', json=recipe()).json()
    result = signed.put('/api/v1/recipes/' + row['id'], json=recipe('不应保存'), headers={'If-Match': token})
    assert result.status_code == 422
    assert result.json()['error']['code'] == 'INVALID_RECIPE_VERSION'
    assert signed.get('/api/v1/recipes/' + row['id']).json()['version'] == row['version']


def test_concurrent_if_match_only_one_update_wins(signed, monkeypatch):
    row = signed.post('/api/v1/recipes', json=recipe()).json()
    original_load = recipe_save.load_personal
    def slow_load(*args, **kwargs):
        result = original_load(*args, **kwargs)
        time.sleep(.15)
        return result
    monkeypatch.setattr(recipe_save, 'load_personal', slow_load)
    gate = Barrier(2)
    cookies = dict(signed.cookies)
    def change(title):
        gate.wait(timeout=10)
        return parallel_request(cookies, 'PUT', '/api/v1/recipes/' + row['id'],
                                recipe(title), {'If-Match': row['version']})
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(change, ['版本甲', '版本乙']))
    assert sorted(r.status_code for r in responses) == [200, 409], [r.text for r in responses]
    winner = next(r.json() for r in responses if r.status_code == 200)
    loser = next(r for r in responses if r.status_code == 409)
    assert loser.json()['error']['code'] == 'RECIPE_CHANGED'
    assert signed.get('/api/v1/recipes/' + row['id']).json()['title'] == winner['title']


def test_concurrent_create_same_key_creates_one_recipe_and_receipt(signed, monkeypatch):
    original_lock = recipe_save.advisory_lock
    gate = Barrier(2)
    def synchronized_lock(db, *parts):
        if parts[0] == 'recipe_create':
            gate.wait(timeout=10)
        return original_lock(db, *parts)
    monkeypatch.setattr(recipe_save, 'advisory_lock', synchronized_lock)
    cookies = dict(signed.cookies)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: parallel_request(
            cookies, 'POST', '/api/v1/recipes', recipe(), {'Idempotency-Key': 'concurrent'}), range(2)))
    assert sorted(r.status_code for r in responses) == [200, 201], [r.text for r in responses]
    assert len({r.json()['id'] for r in responses}) == 1
    assert len(rows('recipe')) == len(rows('recipe_create')) == 1


def test_interrupted_create_rolls_back_recipe_and_receipt_then_retries(signed, monkeypatch):
    original_flush = Session.flush
    def fail_receipt(db, *args, **kwargs):
        if any(isinstance(r, core.Record) and r.kind == 'recipe_create' for r in db.new):
            raise RuntimeError('isolated interruption before receipt flush')
        return original_flush(db, *args, **kwargs)
    with monkeypatch.context() as scoped:
        scoped.setattr(Session, 'flush', fail_receipt)
        result = signed.post('/api/v1/recipes', json=recipe(), headers={'Idempotency-Key': 'interrupted'})
    assert result.status_code == 500
    assert rows('recipe') == rows('recipe_create') == []
    retry = signed.post('/api/v1/recipes', json=recipe(), headers={'Idempotency-Key': 'interrupted'})
    assert retry.status_code == 201
    assert len(rows('recipe')) == len(rows('recipe_create')) == 1


def test_interrupted_update_rolls_back_body_and_version(signed, monkeypatch):
    first = signed.post('/api/v1/recipes', json=recipe()).json()
    original_flush = Session.flush
    def fail_update(db, *args, **kwargs):
        if any(isinstance(r, core.Record) and r.kind == 'recipe' and r.payload.get('title') == '中断更新'
               for r in db.dirty):
            raise RuntimeError('isolated interruption before update flush')
        return original_flush(db, *args, **kwargs)
    with monkeypatch.context() as scoped:
        scoped.setattr(Session, 'flush', fail_update)
        result = signed.put('/api/v1/recipes/' + first['id'], json=recipe('中断更新'),
                            headers={'If-Match': first['version']})
    assert result.status_code == 500
    current = signed.get('/api/v1/recipes/' + first['id']).json()
    assert current['title'] == first['title'] and current['version'] == first['version']


def test_delete_and_update_are_serialized_without_resurrection(signed, monkeypatch):
    first = signed.post('/api/v1/recipes', json=recipe()).json()
    original_load = recipe_save.load_personal
    def slow_load(*args, **kwargs):
        result = original_load(*args, **kwargs)
        time.sleep(.1)
        return result
    monkeypatch.setattr(recipe_save, 'load_personal', slow_load)
    cookies = dict(signed.cookies)
    gate = Barrier(2)
    def operate(method):
        gate.wait(timeout=10)
        return parallel_request(cookies, method, '/api/v1/recipes/' + first['id'],
                                recipe('并发修改') if method == 'PUT' else None,
                                {'If-Match': first['version']} if method == 'PUT' else None)
    with ThreadPoolExecutor(max_workers=2) as pool:
        update, deleted = list(pool.map(operate, ['PUT', 'DELETE']))
    assert deleted.status_code == 200, deleted.text
    assert update.status_code in (200, 404), update.text
    assert signed.get('/api/v1/recipes/' + first['id']).status_code == 404
    assert rows('recipe') == []


def test_interrupted_delete_rolls_back_and_preserves_version(signed, monkeypatch):
    first = signed.post('/api/v1/recipes', json=recipe()).json()
    original_flush = Session.flush
    def fail_delete(db, *args, **kwargs):
        if any(isinstance(r, core.Record) and r.kind == 'recipe' for r in db.deleted):
            raise RuntimeError('isolated interruption before delete flush')
        return original_flush(db, *args, **kwargs)
    with monkeypatch.context() as scoped:
        scoped.setattr(Session, 'flush', fail_delete)
        result = signed.delete('/api/v1/recipes/' + first['id'])
    assert result.status_code == 500
    current = signed.get('/api/v1/recipes/' + first['id']).json()
    assert current['version'] == first['version']


def test_create_replay_after_delete_does_not_resurrect(signed):
    header = {'Idempotency-Key': 'deleted-save'}
    row = signed.post('/api/v1/recipes', json=recipe(), headers=header).json()
    assert signed.delete('/api/v1/recipes/' + row['id']).status_code == 200
    replay = signed.post('/api/v1/recipes', json=recipe(), headers=header)
    assert replay.status_code == 410
    assert replay.json()['error']['code'] == 'RECIPE_DELETED'
    assert rows('recipe') == []
    assert len(rows('recipe_create')) == 1


def test_candidate_replay_after_delete_does_not_resurrect(signed):
    with core.DB() as db:
        owner = db.scalar(select(core.User)).id
        candidate = core.Record(owner=owner, kind='candidate', payload={
            'title': '原候选', 'source_url': 'https://example.org/recipe'}, expires=core.now() + timedelta(days=1))
        db.add(candidate)
        db.commit()
        identifier = candidate.id
    path = '/api/v1/recipes/candidates/' + identifier + '/save'
    row = signed.post(path, json=recipe()).json()
    assert row.get('version')
    assert signed.delete('/api/v1/recipes/' + row['id']).status_code == 200
    replay = signed.post(path, json=recipe())
    assert replay.status_code == 410
    assert replay.json()['error']['code'] == 'RECIPE_DELETED'
    assert rows('recipe') == []


def test_recipe_rule_input_keeps_parenthesized_animal_and_egg_milk():
    animal = evaluate_recipe({'ingredients': ['植物鸡（牛肉、鸡蛋）', '盐']}, {})
    assert all(r['status'] == 'not_suitable' for r in animal['evaluation']['results'].values())
    milk = evaluate_recipe({'ingredients': ['植物鸡（牛奶、鸡蛋）', '盐']}, {})
    assert milk['evaluation']['results']['pure']['status'] == 'not_suitable'
    assert milk['evaluation']['results']['vegan']['status'] == 'not_suitable'
    assert milk['evaluation']['results']['lacto_ovo']['status'] == 'uncertain'


def test_original_ingredients_override_lossy_cached_rule_names():
    result = evaluate_recipe({'ingredients': ['植物鸡（牛奶、鸡蛋）', '盐'],
                              'rule_ingredients': ['植物鸡', '盐']}, {})
    assert result['evaluation']['results']['pure']['status'] == 'not_suitable'
    assert result['evaluation']['results']['vegan']['status'] == 'not_suitable'
    animal = evaluate_recipe({'ingredients': ['植物鸡（牛肉）'], 'rule_ingredients': ['豆腐']}, {})
    assert all(value['status'] == 'not_suitable' for value in animal['evaluation']['results'].values())


def test_translated_display_cannot_erase_original_animal_ingredients():
    result = evaluate_recipe({'scope': 'public', 'ingredients': ['豆腐', '水'],
                              'original_ingredients': ['tofu', '1 cup beef']}, {})
    assert all(value['status'] == 'not_suitable' for value in result['evaluation']['results'].values())
    assert '牛肉' in result['evaluation']['results']['vegan']['denied']
